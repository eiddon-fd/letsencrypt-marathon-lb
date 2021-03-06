import os
import sys
import subprocess
import time
import json
import requests
from auth import DCOSAuth


ENV_DCOS_SERVICE_ACCOUNT_CREDENTIAL = "DCOS_SERVICE_ACCOUNT_CREDENTIAL"
ENV_MARATHON_URL = "MARATHON_URL"
DEFAULT_MARATHON_URL = "https://marathon.mesos:8443/"
ENV_MARATHON_APP_ID = "MARATHON_APP_ID"
HAPROXY_SSL_CERT = "HAPROXY_SSL_CERT"
ENV_MARATHON_LB_ID = "MARATHON_LB_ID"
ENV_LETSENCRYPT_EMAIL = "LETSENCRYPT_EMAIL"
ENV_LETSENCRYPT_URL = "LETSENCRYPT_URL"
ENV_VERIFICATION_METHOD = "LETSENCRYPT_VERIFICATION_METHOD"
ENV_DOMAINS = "DOMAINS"
ENV_DNSPROVIDER = "DNSPROVIDER"
DEFAULT_LETSENCRYPT_URL = "https://acme-staging.api.letsencrypt.org/directory"

CERTIFICATES_DIR = ".lego/certificates/"
DOMAINS_FILE = ".lego/current_domains"


DEFAULT_LEGO_ARGS = [
    "./lego",
    "--server", os.environ.get(ENV_LETSENCRYPT_URL, DEFAULT_LETSENCRYPT_URL),
    "--email", os.environ.get(ENV_LETSENCRYPT_EMAIL),
    "--accept-tos",
    "--pem",
]
LEGO_ARGS_HTTP = [
    "--http", ":8080",
    "--exclude", "tls-sni-01" # To make lego use the http-01 resolver
]

LEGO_ARGS_DNS = [
    "--dns-resolvers", "8.8.8.8:53",
    "--exclude", "http-01",
]



def get_marathon_url():
    """Retrieves the marathon base url to use from an environment variable"""
    return os.environ.get(ENV_MARATHON_URL, DEFAULT_MARATHON_URL)


def get_authorization():
    """Initializes the authorization object from a secret"""
    if not ENV_DCOS_SERVICE_ACCOUNT_CREDENTIAL in os.environ:
        print("No service account provided. Not using authorization", flush=True)
        return None
    return DCOSAuth(os.environ.get(ENV_DCOS_SERVICE_ACCOUNT_CREDENTIAL), None)


auth = get_authorization()


def get_marathon_app(app_id):
    """Retrieve app definition for marathon-lb app"""
    response = requests.get("%(marathon_url)s/v2/apps/%(app_id)s" % dict(marathon_url=get_marathon_url(), app_id=app_id), auth=auth, verify=False)
    if not response.ok:
        raise Exception("Could not get app details from marathon")
    return response.json()


def update_marathon_app(app_id, **kwargs):
    """Post new certificate data (as environment variable) to marathon to update the marathon-lb app definition"""
    data = dict(id=app_id)
    for key, value in kwargs.items():
        data[key] = value
    headers = {'Content-Type': 'application/json'}
    response = requests.patch("%(marathon_url)s/v2/apps/%(app_id)s" % dict(marathon_url=get_marathon_url(), app_id=app_id),
                              headers=headers, data=json.dumps(data), auth=auth, verify=False)
    if not response.ok:
        print(response)
        print(response.text, flush=True)
        raise Exception("Could not update app. See response text for error message.")
    data = response.json()
    if not "deploymentId" in data:
        print(data, flush=True)
        raise Exception("Could not update app. Marathon did not return deployment id.  See response data for error message.")
    deployment_id = data['deploymentId']

    # Wait for deployment to complete
    deployment_exists = True
    sum_wait_time = 0
    while deployment_exists:
        time.sleep(5)
        sum_wait_time += 5
        print("Waiting for deployment to complete", flush=True)
        # Retrivee list of running deployments
        response = requests.get("%(marathon_url)s/v2/deployments" % dict(marathon_url=get_marathon_url()), auth=auth, verify=False)
        deployments = response.json()
        deployment_exists = False
        for deployment in deployments:
            # Check if our deployment is still in the list
            if deployment['id'] == deployment_id:
                deployment_exists = True
                break
        if sum_wait_time > 60*5:
            raise Exception("Failed to update app due to timeout in deployment.")


def get_domains():
    """Retrieve list of domains from own app definition or from environment variable based on verification method"""
    data = get_marathon_app(os.environ.get(ENV_MARATHON_APP_ID))
    verification_method = os.environ.get(ENV_VERIFICATION_METHOD, "http")
    if verification_method == "http":
        return data["app"]["labels"]["HAPROXY_0_VHOST"]
    elif verification_method == "dns":
        return os.environ.get(ENV_DOMAINS)
    else:
        raise Exception("Unknown verification method: " + verification_method)


def get_cert_filepath(domain_name):
    """Return path of combined cert"""
    if domain_name.startswith("*"):
        domain_name = domain_name.replace("*", "_")
    return "%(path)s/%(domain_name)s.pem" % dict(path=CERTIFICATES_DIR, domain_name=domain_name)


def read_domains_from_last_time():
    """Return list of domains used last time from file or empty sttring if file does not exist"""
    if os.path.exists(DOMAINS_FILE):
        with open(DOMAINS_FILE) as domains_file:
            return domains_file.read()
    else:
        return ""


def write_domains_to_file(domains):
    """Store list of domains in file to retrieve on next run"""
    with open(DOMAINS_FILE, "w") as domains_file:
        domains_file.write(domains)


def generate_letsencrypt_cert(domains):
    """Use lego to validate domains and retrieve letsencrypt certificates"""
    domains_changed = domains != read_domains_from_last_time()
    first_domain = domains.split(",")[0]
    args = list()
    for domain in domains.split(","):
        args.append("--domains")
        args.append(domain)

    verification_method = os.environ.get(ENV_VERIFICATION_METHOD, "http")
    if verification_method == "http":
        args = args + LEGO_ARGS_HTTP
    elif verification_method == "dns":
        args = args + ["--dns", os.environ.get(ENV_DNSPROVIDER, "route53")] + LEGO_ARGS_DNS
    # Check if certificate already exists
    if not domains_changed and os.path.exists("%(path)s/%(domain_name)s.crt" % dict(path=CERTIFICATES_DIR, domain_name=first_domain)):
        print("Renewing certificates", flush=True)
        args.append("renew")
        args.append("--days")
        args.append("80")
    else:
        print("Requesting new certificates", flush=True)
        args.append("run")
    # Start lego
    result = subprocess.run(DEFAULT_LEGO_ARGS + args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if result.returncode != 0:
        print(result)
        raise Exception("Obtaining certificates failed. Check lego output for error messages.")
    write_domains_to_file(domains)
    return first_domain


def upload_cert_to_marathon_lb(cert_filename):
    """Update the marathon-lb app definition and set the the generated certificate as environment variable HAPROXY_SSL_CERT"""
    with open(cert_filename) as cert_file:
        cert_data = cert_file.read()
    # Retrieve current app definition of marathon-lb
    marathon_lb_id = os.environ.get(ENV_MARATHON_LB_ID)
    app_data = get_marathon_app(marathon_lb_id)
    env = app_data["app"]["env"]
    # Compare old and new certs
    if env.get(HAPROXY_SSL_CERT, "") != cert_data:
        print("Certificate changed. Updating certificate", flush=True)
        env[HAPROXY_SSL_CERT] = cert_data
        # Provide env and secrets otherwise marathon will complain about a missing secret
        update_marathon_app(marathon_lb_id, env=env, secrets=app_data["app"].get("secrets", {}))
    else:
        print("Certificate not changed. Not doing anything", flush=True)


def run_client():
    """Generate certificates if necessary and update marathon-lb"""
    domains = get_domains()
    print("Requesting certificates for " + domains, flush=True)
    domain_name = generate_letsencrypt_cert(domains)
    cert_file = get_cert_filepath(domain_name)
    print("Uploading certificates", flush=True)
    upload_cert_to_marathon_lb(cert_file)


def run_client_with_backoff():
    """Calls run_client but catches exceptions and tries again for up to one hour.
        Use this variant if you don't want this app to fail (and redeploy) because of intermittent errors.
    """
    backoff_seconds = 30
    sum_wait_time = 0
    while True:
        try:
            run_client()
            return
        except Exception as ex:
            print(ex)
            if sum_wait_time >= 60*60:
                # Reraise exception after 1 hour backoff, will lead to task failure in marathon
                raise ex
            sum_wait_time += backoff_seconds
            time.sleep(backoff_seconds)
            backoff_seconds *= 2


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "service":
        while True:
            run_client()
            time.sleep(24*60*60) # Sleep for 24 hours
    elif len(sys.argv) > 1 and sys.argv[1] == "service_with_backoff":
        while True:
            run_client_with_backoff()
            time.sleep(24*60*60) # Sleep for 24 hours
    else:
        run_client()

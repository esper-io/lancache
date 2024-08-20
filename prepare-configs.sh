#!/bin/bash

# Exit if there is an error
set -e

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# If script is executed as an unprivileged user
# Execute it as superuser, preserving environment variables
if [ $EUID != 0 ]; then
    sudo -E "$0" "$@"
    exit $?
fi

# If there is an .env file use it
# to set the variables
if [ -f $SCRIPT_DIR/.env ]; then
    source $SCRIPT_DIR/.env
fi

# Check all required variables are set
: "${CACHE_DATA_DIRECTORY:?must be set}"
: "${CACHE_LOGS_DIRECTORY:?must be set}"
: "${CACHE_TEMP_DIRECTORY:?must be set}"
: "${CACHE_MAX_SIZE_GB:?must be set}"

DOMAINS_DIR="/var/git/lancache-cache-domains"
UPSTREAMS_DIR="/etc/nginx/upstreams"

# Create real configuration file using variables from template
/usr/bin/envsubst < /etc/nginx/includes/proxy-cache-paths.conf.templ > /etc/nginx/includes/proxy-cache-paths.conf
/usr/bin/envsubst '$CACHE_LOGS_DIRECTORY' < /etc/nginx/nginx.conf.templ > /etc/nginx/nginx.conf
for CACHE_CONFIG in /etc/nginx/caches-available/*.templ; do /usr/bin/envsubst '$CACHE_LOGS_DIRECTORY' < $CACHE_CONFIG > ${CACHE_CONFIG/.templ/}; done

# Get domains from GitHub repo
rm -rf /var/git/lancache-cache-domains
/usr/bin/git clone https://github.com/esper-io/cache-domains.git /var/git/lancache-cache-domains

# Create domain configs from text files
echo "server_name " | cat - $DOMAINS_DIR/esper.txt > $UPSTREAMS_DIR/esper.conf

# Add the required semicolon to the end of all domain config files
for DOMAIN_CONFIG in $UPSTREAMS_DIR/*-domains.conf; do echo ";" >> $DOMAIN_CONFIG; done

#!/bin/sh
# Starts as root only to hand the data over to the unprivileged "app" user
# (also fixes data created by older root-only images), then drops privileges.
set -e
if [ "$(id -u)" = "0" ]; then
    mkdir -p /data /config
    chown -R app:app /data
    # config.yaml is a bind-mounted file; the app also writes config.yaml.tmp next to it
    chown app:app /config
    if [ -e /config/config.yaml ]; then chown app:app /config/config.yaml; fi
    exec setpriv --reuid=app --regid=app --init-groups "$@"
fi
exec "$@"

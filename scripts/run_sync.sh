#!/bin/bash
# Generic wrapper for running in2xero from cron.
#
# If you use systemd, prefer the units in deploy/ instead - systemd reads the
# EnvironmentFile as root before dropping privileges, so the secrets file never
# has to be readable by the service account. This script exists for cron users.
#
# Uses absolute paths throughout: cron gives the job a near-empty environment,
# so nothing here may depend on an interactive shell's PATH.
#
# The env file holds the real secret values and should be root-owned, mode 0600.
# See README.md, "Secrets".
set -euo pipefail

APP_DIR="${IN2XERO_APP_DIR:-/opt/in2xero}"
CONFIG="${IN2XERO_CONFIG:-/etc/in2xero/config.yaml}"
ENV_FILE="${IN2XERO_ENV_FILE:-/etc/in2xero/in2xero.env}"

if [ ! -r "$ENV_FILE" ]; then
  echo "in2xero: cannot read $ENV_FILE" >&2
  exit 1
fi

# Export every KEY=VALUE in the env file without echoing any value.
set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a

exec "$APP_DIR/venv/bin/in2xero" -c "$CONFIG" sync

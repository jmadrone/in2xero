#!/bin/bash
# Daily in2xero sync, run by the com.emeraldsecurity.in2xero.sync LaunchAgent.
#
# Uses absolute paths throughout - launchd gives the job a near-empty
# environment (no shell profile, no pyenv/homebrew on PATH), so nothing here
# can depend on the interactive shell's PATH.
#
# Secrets come from 1Password via `op run` + .env (op:// references, no real
# values on disk). Requires the 1Password desktop app to be running and
# unlocked, with "Integrate with 1Password CLI" enabled - see README.md
# #Secrets. If the app isn't reachable, `op run` fails and this exits
# non-zero; `in2xero sync` is crosswalk-based and resumable, so a missed day
# is caught up on the next run, not lost.
set -euo pipefail

cd "/Users/josh/Developer/in2xero"

exec /opt/homebrew/bin/op run --env-file=.env -- /Users/josh/.pyenv/shims/in2xero sync

#!/usr/bin/env bash
# Start the dashboard: output on screen, and the same lines kept in logs/.
set -euo pipefail
cd "$(dirname "$0")"

# The application writes the log file itself -- dated, rotated, and colour
# stripped -- so it happens however you start it: this script, `python3 -m
# windmill`, launchd, or a container. This script deliberately does not tee, or
# every line would land in that file twice.
#
# `exec` so signals reach Python directly and Ctrl-C shuts it down cleanly
# rather than orphaning it behind the shell. `-u` so output appears as it
# happens rather than in 8 KB bursts.
# A copy already listening is by far the most common way this fails, and the
# app explains it properly. Nothing to do here but hand over.
exec python3 -u -m windmill "$@"

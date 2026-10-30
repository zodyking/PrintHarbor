#!/bin/sh
set -eu
mkdir -p /data/files /data/tmp /run/cups
chmod 700 /data/files /data/tmp
# CUPS accepts local requests only. Its port is not published by Docker.
cupsd -f &
CUPS_PID=$!
trap 'kill "$CUPS_PID" 2>/dev/null || true' EXIT INT TERM
for i in 1 2 3 4 5 6 7 8 9 10; do
  if lpstat -r >/dev/null 2>&1; then break; fi
  sleep 1
done
exec uvicorn app.main:app --host 0.0.0.0 --port 47831 --no-proxy-headers

#!/usr/bin/env bash
# tests/send_fixture.sh <fixture-name>
#
# Schickt tests/fixtures/<name>.json an den lokalen Bot mit korrekter
# HMAC-SHA256-Signatur. Beispiele:
#   tests/send_fixture.sh meta_ping
#   WHATSBOT_URL=http://127.0.0.1:8765/webhook tests/send_fixture.sh meta_help
#
# Im dev-mode (WHATSBOT_ENV=dev) ohne Keychain-Secret wird die Signatur vom
# Bot ignoriert, das Skript schickt sie trotzdem mit — so kannst du dieselbe
# Fixture später gegen einen prod-Bot schicken ohne sie anzupassen.

set -euo pipefail

NAME="${1:-}"
if [[ -z "$NAME" ]]; then
  echo "Usage: $0 <fixture-name without .json>" >&2
  echo "Available fixtures:" >&2
  find "$(dirname "$0")/fixtures" -name '*.json' -exec basename {} .json \; | sed 's/^/  /' >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
FIXTURE="${SCRIPT_DIR}/fixtures/${NAME}.json"

if [[ ! -f "$FIXTURE" ]]; then
  echo "Fixture not found: $FIXTURE" >&2
  exit 1
fi

URL="${WHATSBOT_URL:-http://127.0.0.1:8000/webhook}"
APP_SECRET="$(security find-generic-password -s whatsbot -a meta-app-secret -w 2>/dev/null || echo dev-no-secret)"
SIG="$(openssl dgst -sha256 -hmac "$APP_SECRET" -hex < "$FIXTURE" | awk '{print $NF}')"

curl -is -X POST "$URL" \
  -H "Content-Type: application/json" \
  -H "X-Hub-Signature-256: sha256=${SIG}" \
  --data-binary "@${FIXTURE}"
echo

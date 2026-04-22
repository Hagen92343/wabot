#!/usr/bin/env bash
# bin/setup-secrets.sh — interaktiv die 7 whatsbot-Secrets in macOS Keychain ablegen.
#
# Spec §4 listet die Pflicht-Einträge. Service-Name ist 'whatsbot'.
# Verifikation nach dem Setup z.B.:
#   security find-generic-password -s whatsbot -a meta-app-secret -w

set -euo pipefail

SERVICE="whatsbot"

# Format: "<key>|<friendly description>"
SECRETS=(
  "meta-app-secret|Meta App Secret (für Webhook-Signatur HMAC-SHA256)"
  "meta-verify-token|Meta Webhook Verify-Token (du wählst frei)"
  "meta-access-token|Meta Permanent Access Token (über System User erzeugt)"
  "meta-phone-number-id|Meta Phone-Number-ID des Bot-WhatsApp-Accounts"
  "allowed-senders|Erlaubte Absender-Nummern (kommasepariert, internationales Format)"
  "panic-pin|PIN für destruktive Ops (/rm, /force, /unlock)"
  "hook-shared-secret|Shared Secret für Hook ↔ Bot IPC (zufaellig generieren, >=32 char)"
)

bold()  { printf '\033[1m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
red()   { printf '\033[31m%s\033[0m\n' "$*" >&2; }
yellow() { printf '\033[33m%s\033[0m\n' "$*"; }

bold "whatsbot — Keychain-Setup (Service: ${SERVICE})"
echo "Du wirst gleich für jedes Secret zur Eingabe aufgefordert."
echo "Eingaben werden NICHT angezeigt. Leere Eingabe = überspringen."
echo

for entry in "${SECRETS[@]}"; do
  key="${entry%%|*}"
  desc="${entry#*|}"

  bold "→ ${key}"
  echo "   ${desc}"

  if security find-generic-password -s "${SERVICE}" -a "${key}" -w >/dev/null 2>&1; then
    read -r -p "   Existiert bereits. Überschreiben? [y/N] " confirm
    case "${confirm}" in
      y|Y|yes|YES) ;;
      *) yellow "   übersprungen"; echo; continue ;;
    esac
  fi

  read -r -s -p "   Wert: " value
  echo
  if [[ -z "${value}" ]]; then
    yellow "   leer — übersprungen"
    echo
    continue
  fi

  security add-generic-password -U -s "${SERVICE}" -a "${key}" -w "${value}"
  green "   ✓ gespeichert"
  echo
done

bold "Verifikation"
missing=0
for entry in "${SECRETS[@]}"; do
  key="${entry%%|*}"
  if security find-generic-password -s "${SERVICE}" -a "${key}" -w >/dev/null 2>&1; then
    green "   ✓ ${key}"
  else
    red "   ✗ ${key} (fehlt)"
    missing=$((missing + 1))
  fi
done

if [[ "${missing}" -gt 0 ]]; then
  red "${missing} Secret(s) fehlen — Bot startet nicht ohne sie."
  exit 1
fi

green "Alle 7 Secrets gesetzt. Du kannst jetzt mit dem nächsten Checkpoint weitermachen."

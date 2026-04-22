#!/usr/bin/env bash
# bin/render-launchd.sh — render plist templates and (un)register them with launchd.
#
# Usage:
#   bin/render-launchd.sh deploy   <DOMAIN> <ENV> <PORT> <REPO_DIR> <LAUNCH_DIR> [SSH_SOCK]
#   bin/render-launchd.sh undeploy <DOMAIN> <LAUNCH_DIR>
#
# Designed to be called by Makefile targets — not directly by humans normally.
# Kept idempotent: redeploys cleanly bootout the previous instance first.

set -euo pipefail

bold()  { printf '\033[1m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
red()   { printf '\033[31m%s\033[0m\n' "$*" >&2; }

ACTION="${1:-}"

case "$ACTION" in
deploy)
  DOMAIN="$2"
  ENV_NAME="$3"
  PORT="$4"
  REPO_DIR="$5"
  LAUNCH_DIR="$6"
  SSH_SOCK="${7:-}"

  UVICORN="$REPO_DIR/venv/bin/uvicorn"
  LOG_DIR="$HOME/Library/Logs/whatsbot"
  USER_PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

  if [[ ! -x "$UVICORN" ]]; then
    red "uvicorn nicht gefunden unter $UVICORN — bitte erst 'make install' laufen lassen."
    exit 1
  fi

  mkdir -p "$LAUNCH_DIR" "$LOG_DIR"

  BOT_PLIST="$LAUNCH_DIR/com.${DOMAIN}.whatsbot.plist"
  BAK_PLIST="$LAUNCH_DIR/com.${DOMAIN}.whatsbot.backup.plist"

  bold "Rendering plists for domain '${DOMAIN}' (env=${ENV_NAME}, port=${PORT})"
  sed \
    -e "s|__DOMAIN__|${DOMAIN}|g" \
    -e "s|__UVICORN__|${UVICORN}|g" \
    -e "s|__REPO_DIR__|${REPO_DIR}|g" \
    -e "s|__WHATSBOT_PORT__|${PORT}|g" \
    -e "s|__WHATSBOT_ENV__|${ENV_NAME}|g" \
    -e "s|__SSH_AUTH_SOCK__|${SSH_SOCK}|g" \
    -e "s|__LOG_DIR__|${LOG_DIR}|g" \
    -e "s|__PATH__|${USER_PATH}|g" \
    -e "s|__HOME__|${HOME}|g" \
    "${REPO_DIR}/launchd/com.DOMAIN.whatsbot.plist.template" > "$BOT_PLIST"

  sed \
    -e "s|__DOMAIN__|${DOMAIN}|g" \
    -e "s|__REPO_DIR__|${REPO_DIR}|g" \
    -e "s|__LOG_DIR__|${LOG_DIR}|g" \
    -e "s|__PATH__|${USER_PATH}|g" \
    -e "s|__HOME__|${HOME}|g" \
    "${REPO_DIR}/launchd/com.DOMAIN.whatsbot.backup.plist.template" > "$BAK_PLIST"

  # Validate that nothing was left unfilled — refuse to load broken plists.
  for f in "$BOT_PLIST" "$BAK_PLIST"; do
    if grep -q '__[A-Z_]\+__' "$f"; then
      red "Unfilled placeholders in $f"
      exit 1
    fi
    plutil -lint "$f" >/dev/null
  done
  green "  ✓ plists rendered + lint clean"

  UID_NUM=$(id -u)
  DOMAIN_TGT="gui/${UID_NUM}"

  # Idempotent: bootout previous instance if any (suppress 'No such process').
  launchctl bootout "${DOMAIN_TGT}/com.${DOMAIN}.whatsbot" 2>/dev/null || true
  launchctl bootout "${DOMAIN_TGT}/com.${DOMAIN}.whatsbot.backup" 2>/dev/null || true

  launchctl bootstrap "${DOMAIN_TGT}" "$BOT_PLIST"
  launchctl bootstrap "${DOMAIN_TGT}" "$BAK_PLIST"
  launchctl enable "${DOMAIN_TGT}/com.${DOMAIN}.whatsbot"
  launchctl enable "${DOMAIN_TGT}/com.${DOMAIN}.whatsbot.backup"
  launchctl kickstart -k "${DOMAIN_TGT}/com.${DOMAIN}.whatsbot"

  green "✅ Deployed:"
  green "   com.${DOMAIN}.whatsbot         (RunAtLoad, KeepAlive on crash)"
  green "   com.${DOMAIN}.whatsbot.backup  (daily 03:00)"
  echo  "   Bot plist:    $BOT_PLIST"
  echo  "   Backup plist: $BAK_PLIST"
  echo  "   Logs:         $LOG_DIR/"
  ;;

undeploy)
  DOMAIN="$2"
  LAUNCH_DIR="$3"
  UID_NUM=$(id -u)
  DOMAIN_TGT="gui/${UID_NUM}"

  launchctl bootout "${DOMAIN_TGT}/com.${DOMAIN}.whatsbot" 2>/dev/null || true
  launchctl bootout "${DOMAIN_TGT}/com.${DOMAIN}.whatsbot.backup" 2>/dev/null || true

  rm -f "${LAUNCH_DIR}/com.${DOMAIN}.whatsbot.plist"
  rm -f "${LAUNCH_DIR}/com.${DOMAIN}.whatsbot.backup.plist"

  green "✅ Undeployed: com.${DOMAIN}.whatsbot[.backup]"
  ;;

*)
  red "Usage: $0 deploy|undeploy ..."
  exit 2
  ;;
esac

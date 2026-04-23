#!/usr/bin/env bash
# bin/watchdog.sh — Phase 6 C6.4 dead-man's-switch watchdog.
#
# Spec §4 + §7 + FMEA #4:
#   - heartbeat: $WHATSBOT_HEARTBEAT (default: /tmp/whatsbot-heartbeat)
#   - panic marker: $WHATSBOT_PANIC_MARKER (default: /tmp/whatsbot-PANIC)
#   - log: $WHATSBOT_WATCHDOG_LOG (default: ~/Library/Logs/whatsbot/watchdog.jsonl)
#   - stale threshold: $WHATSBOT_WATCHDOG_STALE_SECONDS (default: 120)
#
# The script is invoked every $StartInterval seconds by the
# com.<DOMAIN>.whatsbot.watchdog LaunchAgent (default 30 s — see plist).
#
# Logic:
#   1. If the panic marker exists, the bot itself engaged lockdown.
#      Log + exit 0 — no action needed; the bot already tore everything
#      down.
#   2. If the heartbeat file is missing or older than the stale
#      threshold, take action:
#        a. tmux kill-session for every wb-* session.
#        b. pkill -9 -f safe-claude as a backstop.
#        c. Touch the panic marker so the bot, when it comes back,
#           refuses to auto-recover until the user `/unlock`s.
#        d. Send a macOS notification so the human sees this happened.
#   3. Otherwise, log a quiet "alive" event and exit.
#
# Sleep awareness lands in C6.5 — for now we accept that a long
# laptop sleep can trip the watchdog once, after which the bot's
# lockdown cleanly stops everything until the user clears it.
#
# Pure POSIX shell + macOS-standard tools (tmux, pkill, osascript,
# stat, date). Deliberately no Python — the watchdog must work
# even if the venv is broken.

set -euo pipefail

HEARTBEAT="${WHATSBOT_HEARTBEAT:-/tmp/whatsbot-heartbeat}"
PANIC_MARKER="${WHATSBOT_PANIC_MARKER:-/tmp/whatsbot-PANIC}"
LOG="${WHATSBOT_WATCHDOG_LOG:-${HOME}/Library/Logs/whatsbot/watchdog.jsonl}"
THRESHOLD="${WHATSBOT_WATCHDOG_STALE_SECONDS:-120}"
TMUX="${WHATSBOT_WATCHDOG_TMUX:-tmux}"
PKILL="${WHATSBOT_WATCHDOG_PKILL:-pkill}"
NOTIFIER="${WHATSBOT_WATCHDOG_NOTIFIER:-osascript}"
CLAUDE_PATTERN="${WHATSBOT_CLAUDE_PATTERN:-safe-claude}"

mkdir -p "$(dirname "$LOG")" 2>/dev/null || true

now_epoch() { date -u +%s; }

now_iso() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }

# Portable mtime in epoch seconds. macOS uses BSD stat (-f %m),
# Linux uses GNU stat (-c %Y). Try BSD first because LaunchAgent
# is macOS-only in production; Linux dev/CI falls through to GNU.
file_mtime() {
    local path="$1"
    if stat -f %m "$path" 2>/dev/null; then return 0; fi
    if stat -c %Y "$path" 2>/dev/null; then return 0; fi
    return 1
}

log_json() {
    local event="$1"; shift
    local extra=""
    while [ "$#" -gt 0 ]; do
        local k="$1"; shift
        local v="$1"; shift
        extra="${extra},\"${k}\":${v}"
    done
    printf '{"ts":"%s","logger":"whatsbot.watchdog","level":"info","event":"%s"%s}\n' \
        "$(now_iso)" "$event" "$extra" >> "$LOG"
}

# Seconds since system boot. Used by C6.5 boot-grace.
# macOS: ``sysctl -n kern.boottime`` returns ``{ sec = 1700000000, usec = 0 } ...``.
# Linux: ``/proc/uptime`` (first column is float uptime seconds).
# Tests can override via ``WHATSBOT_WATCHDOG_FAKE_UPTIME``.
system_uptime_seconds() {
    if [ -n "${WHATSBOT_WATCHDOG_FAKE_UPTIME:-}" ]; then
        echo "${WHATSBOT_WATCHDOG_FAKE_UPTIME}"
        return 0
    fi
    local boottime
    boottime="$( { sysctl -n kern.boottime 2>/dev/null \
                   | grep -oE 'sec = [0-9]+' \
                   | head -1 \
                   | awk '{print $3}'; } || true)"
    if [ -n "$boottime" ]; then
        local now
        now="$(now_epoch)"
        echo $((now - boottime))
        return 0
    fi
    if [ -r /proc/uptime ]; then
        awk '{print int($1)}' /proc/uptime
        return 0
    fi
    return 1
}

# 1. Panic-marker short-circuit. The bot itself initiated /panic;
#    nothing for us to do.
if [ -f "$PANIC_MARKER" ]; then
    log_json "watchdog_skip_panic_active" \
        "marker" "\"$PANIC_MARKER\""
    exit 0
fi

# 2. Heartbeat presence + freshness check.
if [ ! -f "$HEARTBEAT" ]; then
    AGE="missing"
    STALE=1
    BOT_PID=""
else
    MTIME="$(file_mtime "$HEARTBEAT")"
    NOW="$(now_epoch)"
    AGE=$((NOW - MTIME))
    if [ "$AGE" -ge "$THRESHOLD" ]; then
        STALE=1
    else
        STALE=0
    fi
    # Pull the bot's PID out of the heartbeat body (C6.4 format:
    # "pid=<n>"). Empty string if the line isn't there. The
    # ``|| true`` keeps ``set -e + pipefail`` from aborting when
    # grep finds no match.
    BOT_PID="$( { grep -E '^pid=' "$HEARTBEAT" 2>/dev/null \
                  | head -1 | cut -d= -f2 | tr -d ' \r\n'; } || true)"
fi

if [ "$STALE" -eq 0 ]; then
    log_json "watchdog_alive" \
        "age_seconds" "$AGE" \
        "threshold_seconds" "$THRESHOLD"
    exit 0
fi

# 3. C6.5 sleep-grace: if the bot's PID from the heartbeat is still
#    alive, the heartbeat staleness is most likely a Mac-Sleep
#    artifact (the bot was suspended, not dead). Give it grace —
#    the next 30 s tick will catch a real death by re-checking.
if [ -n "$BOT_PID" ] && kill -0 "$BOT_PID" 2>/dev/null; then
    log_json "watchdog_grace_pid_alive" \
        "age_seconds" "$AGE" \
        "pid" "$BOT_PID"
    exit 0
fi

# 4. C6.5 boot-grace: if the system itself only just booted, the bot
#    might still be coming up (LaunchAgent restart can take longer
#    than the watchdog's 30 s tick). Skip the engage on missing
#    heartbeat during the first $BOOT_GRACE_SECONDS after boot.
BOOT_GRACE="${WHATSBOT_WATCHDOG_BOOT_GRACE_SECONDS:-300}"
SYS_UPTIME="$(system_uptime_seconds || echo 0)"
if [ "$AGE" = "missing" ] && [ "$SYS_UPTIME" -lt "$BOOT_GRACE" ]; then
    log_json "watchdog_grace_recent_boot" \
        "uptime_seconds" "$SYS_UPTIME" \
        "boot_grace_seconds" "$BOOT_GRACE"
    exit 0
fi

# 5. Stale + bot dead + not just-booted → emergency tear-down.
log_json "watchdog_engaged" \
    "age" "\"$AGE\"" \
    "threshold_seconds" "$THRESHOLD"

KILLED=0
if command -v "$TMUX" >/dev/null 2>&1; then
    while IFS= read -r session; do
        if [ -z "$session" ]; then continue; fi
        if "$TMUX" kill-session -t "$session" 2>/dev/null; then
            KILLED=$((KILLED + 1))
        fi
    done < <("$TMUX" list-sessions -F '#{session_name}' 2>/dev/null \
             | grep '^wb-' || true)
fi
log_json "watchdog_sessions_killed" \
    "count" "$KILLED"

# pkill -9 -f safe-claude — narrow pattern (Spec §21 Phase 6
# abbruch criterion: don't mass-kill foreign claude installs).
PKILL_RC=0
if command -v "$PKILL" >/dev/null 2>&1; then
    "$PKILL" -9 -f "$CLAUDE_PATTERN" >/dev/null 2>&1 || PKILL_RC=$?
fi
log_json "watchdog_pkill_done" \
    "pattern" "\"$CLAUDE_PATTERN\"" \
    "exit_code" "$PKILL_RC"

# Touch the panic marker so the bot — when it comes back up —
# refuses to auto-recover until the user runs /unlock.
: > "$PANIC_MARKER" 2>/dev/null || true

# Best-effort macOS notification.
if command -v "$NOTIFIER" >/dev/null 2>&1; then
    "$NOTIFIER" -e \
        "display notification \"watchdog tore down ${KILLED} sessions\" with title \"🚨 whatsbot watchdog engaged\" sound name \"Submarine\"" \
        >/dev/null 2>&1 || true
fi

exit 0

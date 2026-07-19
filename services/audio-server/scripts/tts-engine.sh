#!/usr/bin/env bash
# One-flip TTS-engine switch for the MeowVoice audio-server (ticket 6-1 rollback).
#
#   tts-engine.sh crispasr   # flip audio-server to the resident CrispASR
#   tts-engine.sh mlx         # flip back to in-process MLX (frees CrispASR)
#   tts-engine.sh status      # show config + which engine process is resident
#
# Regression drills for the lock / stop-gate live in scripts/tts-engine.drills.sh
# (sourced-function form, run: bash scripts/tts-engine.drills.sh).
#
# MUTUAL EXCLUSION (C2): the flip is ordered so the two engines are NEVER
# resident at the same instant. Whatever the direction, the audio-server is
# booted OUT first (dropping any loaded MLX weights) before the CrispASR launchd
# job is started/stopped, and only then is the audio-server booted back in with
# the new engine. A directory lock (atomic mkdir — macOS has no flock(1)) makes
# concurrent flips fail fast so two flips cannot interleave. tts.json is written
# tmp+mv so a reader never sees a half-written config.
set -euo pipefail

CONFIG="$HOME/.meowvoice/tts.json"
LOCKDIR="$HOME/.meowvoice/tts-flip.lock.d"
UID_NUM="$(id -u)"
CRISPASR="dev.nerigate.meowvoice.crispasr"
AUDIO="dev.nerigate.meowvoice.audio-server"
CRISPASR_PLIST="$HOME/Library/LaunchAgents/${CRISPASR}.plist"
AUDIO_PLIST="$HOME/Library/LaunchAgents/${AUDIO}.plist"
AUDIO_PORT="${MEOWVOICE_PORT:-8400}"
CRISPASR_HEALTH="http://127.0.0.1:8123/health"
AUDIO_HEALTH="http://127.0.0.1:${AUDIO_PORT}/health"

write_config_atomic() {  # $1 = engine
    local tmp; tmp="$(mktemp "${CONFIG}.XXXXXX")"
    printf '{"engine": "%s"}\n' "$1" > "$tmp"
    mv -f "$tmp" "$CONFIG"
}

wait_proc_gone() {  # $1 = pgrep pattern, $2 = timeout secs
    local i; for ((i=0; i<${2:-20}*2; i++)); do
        pgrep -f "$1" >/dev/null 2>&1 || return 0
        sleep 0.5
    done
    echo "warn: '$1' still present after ${2:-20}s"; return 1
}

wait_health() {  # $1 = url, $2 = grep needle, $3 = timeout secs
    local i; for ((i=0; i<${3:-60}; i++)); do
        curl -s "$1" 2>/dev/null | grep -q "$2" && return 0
        sleep 1
    done
    echo "warn: $1 did not report '$2' within ${3:-60}s"; return 1
}

bootout_audio() {
    # Hard gate (R3 C2): the flip must NOT proceed while an old audio-server is
    # still resident — it may still hold MLX, and loading a second engine on top
    # would violate mutual exclusion. Verify the process is actually gone; on
    # failure, abort with a manual-remediation hint (cf. ticket 1-10 STOPPED_OK).
    launchctl bootout "gui/${UID_NUM}/${AUDIO}" 2>/dev/null || true
    if ! wait_proc_gone "audio-server/server.py" 20; then
        echo "ABORT: audio-server did not stop; NOT flipping (MLX may still be resident)." >&2
        echo "  manual: launchctl bootout gui/${UID_NUM}/${AUDIO}; pkill -f audio-server/server.py" >&2
        return 1
    fi
    return 0
}

bootin_audio() {  # $1 = expected engine needle
    launchctl bootstrap "gui/${UID_NUM}" "$AUDIO_PLIST" 2>/dev/null \
        || launchctl kickstart -k "gui/${UID_NUM}/${AUDIO}"
    wait_health "$AUDIO_HEALTH" "\"tts_engine\":\"$1\"" 90
}

start_crispasr() {
    # bootstrap only if not already loaded; do NOT kickstart a healthy resident
    # (needless restart churns the port and can lose the bind race)
    launchctl bootstrap "gui/${UID_NUM}" "$CRISPASR_PLIST" 2>/dev/null || true
    wait_health "$CRISPASR_HEALTH" '"status": "ok"' 60
}

stop_crispasr() {
    # bootout can lag or leave an orphan; force any survivor dead so CrispASR can
    # never overlap the MLX load (C2 no-coexistence guarantee, not timing-luck).
    launchctl bootout "gui/${UID_NUM}/${CRISPASR}" 2>/dev/null || true
    local i; for ((i=0; i<40; i++)); do
        pgrep -f "crispasr --server" >/dev/null 2>&1 || return 0
        pkill -9 -f "crispasr --server" 2>/dev/null || true
        sleep 0.5
    done
    echo "error: crispasr would not die; refusing to load MLX on top of it" >&2
    return 1
}

acquire_lock() {
    # Atomic non-blocking lock via mkdir (macOS has no flock). The holder writes
    # its PID so a crashed flip (abnormal exit that skipped the EXIT trap) leaves
    # a recoverable lock: a would-be flipper that finds a DEAD holder clears the
    # stale lock and retries; a LIVE holder means a real concurrent flip -> yield.
    if mkdir "$LOCKDIR" 2>/dev/null; then echo $$ > "$LOCKDIR/pid"; return 0; fi
    local holder; holder="$(cat "$LOCKDIR/pid" 2>/dev/null || true)"
    # ps -p is a positive existence check regardless of owner (R4 C2): kill -0
    # fails with EPERM for a LIVE process owned by another user, which must NOT be
    # mistaken for a dead holder. Three-way verdict (R4-final): exit 0 = alive ->
    # yield; exit 1 = confirmed absent -> clearable stale; any other rc (ps
    # blocked/missing, e.g. sandbox 127) = query failure -> conservative abort —
    # never clear a lock whose holder we cannot positively judge.
    if [ -n "$holder" ]; then
        local ps_rc=0
        ps -p "$holder" >/dev/null 2>&1 || ps_rc=$?
        if [ "$ps_rc" -eq 0 ]; then
            echo "another flip in progress (holder pid $holder); aborting"; return 1
        elif [ "$ps_rc" -ne 1 ]; then
            echo "cannot verify holder pid $holder (ps rc=$ps_rc); refusing to clear lock"; return 1
        fi
    fi
    echo "clearing stale flip lock (dead holder ${holder:-unknown})"
    rm -rf "$LOCKDIR"
    if mkdir "$LOCKDIR" 2>/dev/null; then echo $$ > "$LOCKDIR/pid"; return 0; fi
    echo "could not acquire flip lock after clearing stale"; return 1
}

case "${1:-status}" in
  crispasr|mlx)
    acquire_lock || exit 1
    trap 'rm -rf "$LOCKDIR" 2>/dev/null || true' EXIT

    # Flip atomicity (R4): stop-verify the old audio-server FIRST, then write the
    # engine config. If bootout aborts, tts.json is untouched, so config and the
    # still-running service stay consistent (no "config says X, service runs Y").
    bootout_audio || exit 3             # step 1: drop MLX (if any); abort if it won't stop
    write_config_atomic "$1"            # commit config only after the stop succeeded
    if [ "$1" = "crispasr" ]; then
        start_crispasr                  # step 2: CrispASR resident before audio-server returns
    else
        stop_crispasr                   # step 2: CrispASR fully dead before MLX loads
    fi
    bootin_audio "$1"                   # step 3: bring audio-server back on the new engine
    echo "flipped -> $1"
    ;;
  status)
    echo "config: $(cat "$CONFIG" 2>/dev/null || echo '(none, default mlx)')"
    echo -n "crispasr process: "; pgrep -fl "crispasr --server" || echo "(not running)"
    echo -n "audio-server: "; pgrep -fl "audio-server/server.py" || echo "(not running)"
    ;;
  *)
    echo "usage: $0 {crispasr|mlx|status}" >&2; exit 2 ;;
esac

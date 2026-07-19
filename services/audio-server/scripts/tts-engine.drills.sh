#!/usr/bin/env bash
# Regression drills for tts-engine.sh lock + stop-gate (ticket 6-1 C2, sourced-
# function form like the 1-10 rollback drills). Sources the flip script to get
# its functions/vars, then exercises them in isolation — no launchctl, no prod
# restart. Run: bash scripts/tts-engine.drills.sh
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FAIL=0

# Drill 1: stale lock (DEAD holder PID) auto-recovers; LIVE holder yields.
( source "$HERE/tts-engine.sh" >/dev/null 2>&1
  rm -rf "$LOCKDIR"; mkdir "$LOCKDIR"; echo 999999 > "$LOCKDIR/pid"   # 999999 > macOS max pid = dead
  if acquire_lock; then rc=0; else rc=$?; fi
  holder="$(cat "$LOCKDIR/pid" 2>/dev/null)"; rm -rf "$LOCKDIR"
  [ "$rc" = "0" ] && [ "$holder" = "$$" ] && echo "STALE_RECOVERY_PASS" || { echo "STALE_RECOVERY_FAIL"; exit 1; }
) || FAIL=1

( source "$HERE/tts-engine.sh" >/dev/null 2>&1
  rm -rf "$LOCKDIR"; mkdir "$LOCKDIR"; echo $$ > "$LOCKDIR/pid"       # live holder = this shell
  if acquire_lock; then rc=0; else rc=$?; fi; rm -rf "$LOCKDIR"
  [ "$rc" != "0" ] && echo "LIVE_HOLDER_YIELD_PASS" || { echo "LIVE_HOLDER_YIELD_FAIL"; exit 1; }
) || FAIL=1

# Drill 2: bootout stop-failure aborts (does not proceed). AUDIO points at a
# nonexistent label so launchctl bootout is a no-op; the real audio-server keeps
# matching the pgrep pattern -> the gate must time out and refuse to flip.
( source "$HERE/tts-engine.sh" >/dev/null 2>&1
  AUDIO="dev.nerigate.meowvoice.NONEXISTENT"
  proceeded=0; if bootout_audio >/dev/null 2>&1; then proceeded=1; fi
  [ "$proceeded" = "0" ] && echo "STOP_ABORT_PASS" || { echo "STOP_ABORT_FAIL"; exit 1; }
) || FAIL=1

[ "$FAIL" = "0" ] && echo "ALL_DRILLS_PASS" || echo "SOME_DRILLS_FAILED"
exit "$FAIL"

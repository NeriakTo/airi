#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# Ticket 1-10 一次性 cutover 腳本（B 案：nohup/setsid 脫離 session 執行）
#
# 目的：把 meowvoice 由「skills-dir plugin ＋ --dangerously-load-development-channels
#       旗標＋watcher 送鍵」根治為正式 local marketplace 安裝（launcher 回純 --channels）。
#       權威規格＝docs/ticket-1-10-cutover-runbook.md（步驟 0～8）。本腳本把 runbook
#       步驟 0→6 機械化，逐步硬閘門、失敗自動補償/回滾、全程落 log。
#
# ── 模式 ──────────────────────────────────────────────────────────────────────
#   --dry-run   只跑唯讀檢查（步驟0 健康 + D1/D3/D4/D5 + 變更前狀態硬閘 + 產物盤點）
#               ＋列印將執行動作清單。零 cutover 變更、零停機。
#   （無旗標）  實跑。於 live session 內會自動 B 案背景脫離。步驟 6 通過寫 AWAITING_E2E。
#   --finalize  E2E（步驟7）由人工驗證通過後執行：讀最新 AWAITING_E2E marker、再次確認
#               語音鏈為 marketplace 版且 live，通過才把該 marker 升為 SUCCESS。
#
# ── 為何要「自我脫離」（B 案核心）─────────────────────────────────────────────
#   runbook「⚠ 執行環境」節硬性規定第 1 步起禁止由 claude-code-discord session（tmux 內）
#   執行，因步驟 1 的 `tmux kill-session -t claude-code-discord` 會殺死執行者本身。
#   B 案等效滿足（非繞過）：偵測自己是否掛在該 tmux session 下；若是，以 setsid（本機無
#   setsid → 退回 python3 os.setsid()）＋nohup 於背景重新 exec 自身（stdin←/dev/null，
#   stdout/stderr→log），父程序退出。脫離後子程序成為新 session leader（reparent 至
#   launchd），步驟 1 殺 tmux session 波及不到它——等於「不在該 session 內執行」被等效滿足。
#   父程序在退出前會等子程序寫出 started marker（有界握手），確認 setsid/execv 確實成功。
#
# ── 硬閘門、失敗補償與回滾（R1 審查後強化）──────────────────────────────────
#   實跑開頭：原子執行鎖（mkdir）＋變更前狀態硬閘（launcher 仍舊版、skills live 存在、
#   plugin 未安裝、監督 running、無 SUCCESS/AWAITING_E2E marker）。偵測已完成/部分完成/
#   狀態不符 → 零變更退出、寫 ABORTED_STATE_MISMATCH，絕不自動套舊備份（防重跑反向拆除）。
#
#   步驟 0(preflight)：0a/0b/0c 健康（curl --fail + 驗 JSON 狀態欄位）＋ D1(marketplace
#   validate --strict) / D3(新 launcher grep 計數＋bash -n) / D4(rollback bash -n) /
#   D5(監督 running＋program 路徑)。全在 MUTATED=1 之前，失敗＝零變更中止。
#
#   步驟 1→6 依序執行，每步先驗前置後驗結果。失敗補償按「實際改了什麼」分流（不硬套 rollback）：
#     • launcher 尚未換裝（步驟 1/2 失敗，或步驟 3 失敗）：best-effort 清 marketplace；
#       若 skills 已搬離則搬回；直接 bootstrap 舊監督；不做 launcher 檔案還原。
#     • launcher 已換裝（步驟 4 備份一建立即進此路徑）：呼叫完整 rollback.sh（帶本次 STAMP）。
#       步驟 4 換裝＝寫同目錄暫存檔＋chmod 700＋mv -f 原子替換；備份建立成功即 LAUNCHER_SWAPPED=1
#       （保守 checkpoint 前移，之後任何失敗都用備份還原，不容早期補償漏還原 launcher）。
#   兩條補償路徑事後都用同一「三合一硬閘」獨立驗證語音鏈恢復——supervisor running ＋ session
#   alive ＋ 8401 health ok，任一未過即視為未恢復——通過才寫 FAILED_ROLLED_BACK；否則寫
#   FAILED_NEED_MANUAL（防 rollback exit 0 或監督未起的假成功）。
#   變更階段裝 EXIT/TERM/INT/HUP trap；非預期 EXIT 亦先嘗試同路徑補償再兜底寫終態 marker，並釋放鎖。
#   終態 marker 寫入 fail-loud（主路徑失敗改試 /tmp 備用路徑；兩者皆敗才保留「未寫」狀態並明示）。
#
# ── finalize 反向狀態閘 ────────────────────────────────────────────────────────
#   --finalize 升 SUCCESS 前逐項硬驗：launcher 已 marketplace 版、live skills 已搬離、plugin
#   installed＋enabled、supervisor running、session＋8401 ok、6c 負向硬判（重跑，SUCCESS 永遠經
#   一次有效 6c）、無晚於 AWAITING 的失敗／回滾 marker。任一不符 → 非零退出、寫獨立
#   ABORTED_FINALIZE_MISMATCH marker、保留原 AWAITING 不動。
#
# ── set -e 紀律（見 memory 教訓）──────────────────────────────────────────────
#   `set -euo pipefail` 為底；EXIT trap 兜底補寫終態 marker（連 set -e 中止也涵蓋）。
#   所有可預期非零（grep 找不到、curl 失敗、launchctl print 未載入…）一律先擷取到變數再
#   於 if 判定，不靠 `| tail`／`| grep` 管線退出碼做關鍵決策。step 以 `step_x || recover`
#   呼叫（此語境 set -e 於函式內抑制），函式內每個判別顯式處理、明確 return，不吞錯。
#
# 用法：
#   ticket-1-10-cutover.sh --dry-run     # 唯讀預演（安全，任何 shell 可跑；預期 exit 0）
#   ticket-1-10-cutover.sh               # 實跑（live session 內自動背景脫離）
#   ticket-1-10-cutover.sh --finalize    # E2E 通過後補寫 SUCCESS
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

export HOME="${HOME:-/Users/nerigate}"
export PATH="${HOME}/.local/bin:${HOME}/.bun/bin:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export LANG="en_US.UTF-8"

# ── 外部依賴與路徑（比照 runbook／rollback 命名；${VAR:-預設}＝預設真實值、供隔離測試 override）──
LAUNCHCTL="${LAUNCHCTL:-launchctl}"
TMUXBIN="${TMUXBIN:-/opt/homebrew/bin/tmux}"
CLAUDE_BIN="${CLAUDE_BIN:-claude}"
CURL_BIN="${CURL_BIN:-curl}"
BUN_BIN="${BUN_BIN:-bun}"
PY_BIN="${PY_BIN:-python3}"
UID_NUM="${UID_NUM:-$(id -u)}"
LC_TARGET="gui/${UID_NUM}/com.claude-code.discord"
LC_DOMAIN="gui/${UID_NUM}"
PLIST="${PLIST:-${HOME}/Library/LaunchAgents/com.claude-code.discord.plist}"
BIN="${BIN:-${HOME}/.local/bin}"
LAUNCHER="${LAUNCHER:-${BIN}/claude-code-discord}"
NEW_LAUNCHER="${NEW_LAUNCHER:-${BIN}/claude-code-discord.new-1-10}"
ROLLBACK="${ROLLBACK:-${BIN}/ticket-1-10-rollback.sh}"
MARKETPLACE_ROOT="${MARKETPLACE_ROOT:-${HOME}/github/meowvoice/plugin-marketplace}"
SKILLS_LIVE="${SKILLS_LIVE:-${HOME}/.claude/skills/meowvoice}"
SKILLS_ARCHIVE="${SKILLS_ARCHIVE:-${HOME}/.claude/skills-archive}"
ENVF="${ENVF:-${HOME}/.claude/channels/meowvoice/.env}"
INSTALLED_PLUGINS_JSON="${INSTALLED_PLUGINS_JSON:-${HOME}/.claude/plugins/installed_plugins.json}"
SESSION="${SESSION:-claude-code-discord}"
MONITORED_LOG="${MONITORED_LOG:-/tmp/claude-code-discord.log}"
AUDIO_HEALTH="${AUDIO_HEALTH:-http://127.0.0.1:8400/health}"
BRIDGE_HEALTH="${BRIDGE_HEALTH:-http://127.0.0.1:8401/health}"
LOGDIR_DEFAULT="${LOGDIR_DEFAULT:-${HOME}/.meowvoice/logs}"
LOCKROOT="${LOCKROOT:-${HOME}/.meowvoice/locks}"
LOCKDIR="${LOCKDIR:-${LOCKROOT}/ticket-1-10-cutover.lock}"
# 終態 marker 主路徑不可寫時的備用 namespace（預設 /tmp；隔離測試可 override 以維持 hermetic）。
# 警語：真實 /tmp 於 macOS 是 symlink → private/tmp，凡以此為 find root 者必用 `find -H`（見 F8）。
MARKER_FALLBACK_DIR="${MARKER_FALLBACK_DIR:-/tmp}"
# 已知的 dev-channels 互動確認框 TUI 關鍵字（舊 launcher watcher 也用它偵測；MAJOR #7 負向硬判）
DEVDIALOG_MARKER="${DEVDIALOG_MARKER:-Loading development channels}"
# 恢復/回滾後語音鏈恢復的有界等待（session alive + 8401 ok）
VERIFY_ITERS="${VERIFY_ITERS:-6}"
VERIFY_SLEEP="${VERIFY_SLEEP:-10}"
# 步驟 6 前等待新 launcher 拉起 session 的秒數（runbook=40；隔離 mock 測試可 override 為 0）
STEP6_WAIT="${STEP6_WAIT:-40}"
# 父子握手等待秒數
HANDSHAKE_LIMIT="${HANDSHAKE_LIMIT:-20}"

# ── 執行期狀態 ────────────────────────────────────────────────────────────────
MUTATED=0                 # 0＝尚未進入變更階段；1＝已進入
SKILLS_MOVED=0            # step3 成功搬離後＝1（補償時需搬回）
LAUNCHER_SWAPPED=0        # 保守 checkpoint：step4 備份一建立成功即＝1（其後失敗走完整 rollback、用備份還原）
LAUNCHER_BAK_STAMP=""     # step4 備份 launcher 後填入
LOG_LINES_BEFORE=0        # step1a 記 monitored log 行數，供 step6 只看新增行
STEP6C_RESULT="n/a"       # step6 6c 判定結果：PASS/FAIL/INCONCLUSIVE（INCONCLUSIVE 記入 marker，finalize 重跑硬判）
IN_TRAP_RECOVERY=0        # 全域防遞迴：補償一旦啟動即＝1，EXIT trap 不得再次啟動補償
CUR_STEP="init"
TERMINAL_MARKER_WRITTEN=0
TERMINAL_MARKER=""
LOCK_HELD_BY_US=0
LAST_HEALTH_BODY=""
RUN_TS=""
STAMP=""
LOGDIR=""
LOG=""
SELF=""

ts(){ date '+%Y-%m-%d %H:%M:%S'; }
log(){ printf '[%s] %s\n' "$(ts)" "$*"; }

resolve_self(){
  case "$0" in
    /*) SELF="$0" ;;
    */*) SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")" ;;
    *) SELF="$(command -v "$0" 2>/dev/null || true)"; [ -n "$SELF" ] || SELF="${BIN}/ticket-1-10-cutover.sh" ;;
  esac
}

in_live_session(){
  [ -n "${TMUX:-}" ] || return 1
  local cur
  cur="$("$TMUXBIN" display-message -p '#S' 2>/dev/null || echo "")"
  [ "$cur" = "$SESSION" ]
}

# ── 健康檢查（MAJOR #4：curl --fail --max-time；以 JSON 狀態欄位判定，非「非空」）──
http_get(){ "$CURL_BIN" --fail --silent --show-error --max-time 5 "$1" 2>/dev/null || true; }

check_bridge_health(){   # 0＝ok；填 LAST_HEALTH_BODY
  local body; body="$(http_get "$BRIDGE_HEALTH")"; LAST_HEALTH_BODY="$body"
  [ -n "$body" ] || return 1
  printf '%s' "$body" | "$PY_BIN" -c 'import sys,json
try:
    d=json.load(sys.stdin)
except Exception:
    sys.exit(1)
ok = d.get("status")=="ok" and d.get("plugin")=="meowvoice" and int(d.get("port",0) or 0)==8401
sys.exit(0 if ok else 1)'
}

check_audio_health(){   # 0＝ok；填 LAST_HEALTH_BODY
  local body; body="$(http_get "$AUDIO_HEALTH")"; LAST_HEALTH_BODY="$body"
  [ -n "$body" ] || return 1
  printf '%s' "$body" | "$PY_BIN" -c 'import sys,json
try:
    d=json.load(sys.stdin)
except Exception:
    sys.exit(1)
sys.exit(0 if d.get("status")=="ok" else 1)'
}

supervisor_running(){   # 0＝已載入且 state=running
  local prt; prt="$("$LAUNCHCTL" print "$LC_TARGET" 2>/dev/null || true)"
  [ -n "$prt" ] || return 1
  printf '%s' "$prt" | grep -qE '^[[:space:]]*state = running'
}

# ── 6c 負向硬判（裁決 #4）：TUI pane 不得卡在 dev-channels 確認框 ─────────────────
#   0＝擷取成功且無確認框（放行）；非 0＝偵測到確認框，或 capture 失敗＝無法判定。
#   finalize 階段 session 應已起、capture 應可用，故此處 capture 失敗採 fail-closed（回非 0）。
#   訊息走 stdout（finalize 未重導 log 時可見），step6 端自行決定如何記錄。
check_6c_no_dialog(){
  local pane cap_rc
  pane="$("$TMUXBIN" capture-pane -pt "$SESSION" 2>/dev/null)"; cap_rc=$?
  if [ "$cap_rc" -ne 0 ]; then
    echo "  6c capture-pane 失敗 rc=${cap_rc}（此階段 session 應已起，無法擷取＝fail-closed）"
    return 1
  fi
  if printf '%s' "$pane" | grep -qF "$DEVDIALOG_MARKER"; then
    echo "  6c [FAIL] TUI 仍卡在 dev-channels 確認框（'${DEVDIALOG_MARKER}'）"
    return 1
  fi
  echo "  6c TUI 無 dev-channels 確認框: yes"
  return 0
}

# ── 終態 marker（MAJOR：寫入 fail-loud——寫失敗不得誆稱已寫，先試備用路徑）──────
write_terminal_marker(){
  local state="$1" detail="$2"
  local target="${LOGDIR}/ticket-1-10-cutover-${RUN_TS}.${state}"
  local body
  body="$(
    echo "state=${state}"
    echo "run_ts=${RUN_TS}"
    echo "finished_at=$(ts)"
    echo "failed_step=${detail}"
    echo "mutated=${MUTATED}"
    echo "launcher_swapped=${LAUNCHER_SWAPPED}"
    echo "skills_moved=${SKILLS_MOVED}"
    echo "step6c=${STEP6C_RESULT}"
    echo "cutover_stamp=${STAMP}"
    echo "log=${LOG}"
  )"
  if printf '%s\n' "$body" > "$target" 2>/dev/null; then
    TERMINAL_MARKER="$target"; TERMINAL_MARKER_WRITTEN=1
    log "[MARKER] ${state} → ${TERMINAL_MARKER}"
    return 0
  fi
  # 主路徑寫入失敗（磁碟滿／權限／IO）：fail-loud，不得設 WRITTEN=1；改試備用路徑。
  log "[MARKER][ERR] 主 marker 寫入失敗：${target}；改試備用路徑"
  echo "[MARKER][ERR] 主 marker 寫入失敗：${target}（state=${state}）" >&2
  local fallback="${MARKER_FALLBACK_DIR}/ticket-1-10-marker-fallback-${RUN_TS}.${state}"
  if printf '%s\n' "$body" > "$fallback" 2>/dev/null; then
    TERMINAL_MARKER="$fallback"; TERMINAL_MARKER_WRITTEN=1
    log "[MARKER] ${state} → ${TERMINAL_MARKER}（備用路徑）"
    echo "[MARKER] 已改寫備用路徑：${fallback}" >&2
    return 0
  fi
  # 主與備用皆失敗：保持 WRITTEN=0（讓 EXIT trap 兜底再試），並明示。
  log "[MARKER][FATAL] 主與備用 marker 皆寫入失敗（state=${state}）——TERMINAL_MARKER_WRITTEN 保持 0"
  echo "[MARKER][FATAL] 主與備用 marker 皆寫入失敗（state=${state}）——請人工檢查磁碟/權限" >&2
  return 1
}

# ── 有界等待驗證語音鏈恢復（裁決 #1：三合一硬閘——supervisor running ＋ session alive ＋ 8401 ok）──
#   補償路徑與完整 rollback 路徑共用本函式；任一未過＝視為未恢復（回 1），caller 寫 FAILED_NEED_MANUAL。
#   為何納入 supervisor：R2 實證——bootstrap 後 session/8401 恢復但 LaunchAgent 監督仍 stopped＝孤兒
#   session（無 KeepAlive），marker 卻誆稱回滾成功。監督未 running ⇒ 語音鏈未真正恢復。
verify_voice_chain_restored(){
  local i sup_ok=0 sess_ok=0 health_ok=0
  for i in $(seq 1 "$VERIFY_ITERS"); do
    [ "$VERIFY_SLEEP" -gt 0 ] && sleep "$VERIFY_SLEEP"
    if supervisor_running; then sup_ok=1; else sup_ok=0; fi
    if "$TMUXBIN" has-session -t "$SESSION" 2>/dev/null; then sess_ok=1; else sess_ok=0; fi
    if check_bridge_health; then health_ok=1; else health_ok=0; fi
    if [ "$sup_ok" -eq 1 ] && [ "$sess_ok" -eq 1 ] && [ "$health_ok" -eq 1 ]; then
      log "[VERIFY] 語音鏈已恢復：supervisor running ＋ session alive ＋ 8401 ok（${LAST_HEALTH_BODY}）"
      return 0
    fi
  done
  log "[VERIFY] 有界等待逾時：supervisor_ok=${sup_ok} session_ok=${sess_ok} health_ok=${health_ok} bridge='${LAST_HEALTH_BODY}'"
  return 1
}

# ── 失敗補償：依實際改了什麼分流（BLOCKING 1：不硬套需備份的 rollback）──────────
do_full_rollback(){   # launcher 已換裝（step≥4）→ 完整 rollback（帶本次 STAMP）
  local ctx="$1" rb_rc
  if [ ! -x "$ROLLBACK" ]; then write_terminal_marker "FAILED_NEED_MANUAL" "${ctx}（無可執行 rollback 腳本）"; return 0; fi
  log "[RECOVER] launcher 已換裝 → 完整 rollback：${ROLLBACK} ${STAMP}"
  if "$ROLLBACK" "$STAMP"; then rb_rc=0; else rb_rc=$?; fi
  log "[RECOVER] rollback 退出碼 rc=${rb_rc}"
  if [ "$rb_rc" -ne 0 ]; then write_terminal_marker "FAILED_NEED_MANUAL" "${ctx}（rollback rc=${rb_rc}；見其手動處置）"; return 0; fi
  if verify_voice_chain_restored; then
    write_terminal_marker "FAILED_ROLLED_BACK" "${ctx}（rollback rc=0 且語音鏈已獨立驗證恢復：supervisor＋session＋8401）"
  else
    write_terminal_marker "FAILED_NEED_MANUAL" "${ctx}（rollback rc=0 但語音鏈未在有界等待內恢復——疑監督未起／舊 launcher 持續崩潰／8401 未起）"
  fi
  return 0
}

do_early_compensation(){   # launcher 未換裝（step1/2/3）→ 分步補償，不做 launcher 檔案還原
  local ctx="$1"
  log "[RECOVER] launcher 未換裝 → 分步補償（SKILLS_MOVED=${SKILLS_MOVED}）"
  # 1. best-effort 清除 marketplace（step2 可能已 add/install）
  log "[RECOVER] best-effort 清除 marketplace 安裝"
  "$CLAUDE_BIN" plugin uninstall meowvoice@meowvoice-local >/dev/null 2>&1 || true
  "$CLAUDE_BIN" plugin marketplace remove meowvoice-local >/dev/null 2>&1 || true
  # 2. 若 skills 已搬離（step3 成功後才失敗），把本次 archive 搬回
  if [ "$SKILLS_MOVED" -eq 1 ]; then
    local sb="${SKILLS_ARCHIVE}/meowvoice-pre-1-10-${STAMP}"
    if [ ! -e "$SKILLS_LIVE" ] && [ -d "$sb" ]; then
      log "[RECOVER] 搬回 skills：${sb} → ${SKILLS_LIVE}"
      mv "$sb" "$SKILLS_LIVE" 2>/dev/null || log "[RECOVER][WARN] skills 搬回 mv 失敗"
    fi
    if [ -f "${SKILLS_LIVE}/server.ts" ]; then log "[RECOVER] skills 還原確認（server.ts 存在）"; else log "[RECOVER][WARN] skills 還原後缺 server.ts"; fi
  fi
  # 3. 直接 bootstrap 舊監督（launcher 仍是舊版，未動）
  log "[RECOVER] bootstrap 恢復舊監督：${LC_DOMAIN} ${PLIST}"
  "$LAUNCHCTL" bootstrap "$LC_DOMAIN" "$PLIST" >/dev/null 2>&1 || true
  # 4. 獨立驗證語音鏈恢復
  if verify_voice_chain_restored; then
    write_terminal_marker "FAILED_ROLLED_BACK" "${ctx}（分步補償完成且語音鏈已獨立驗證恢復：supervisor＋session＋8401）"
  else
    write_terminal_marker "FAILED_NEED_MANUAL" "${ctx}（分步補償後語音鏈未在有界等待內恢復——如監督未 running）"
  fi
  return 0
}

# 依 LAUNCHER_SWAPPED 分流補償；兩路徑內部都會寫終態 marker。
# 進入即設 IN_TRAP_RECOVERY=1（全域防遞迴旗標）——之後任何 EXIT trap 見此即不再重入補償。
perform_compensation(){
  IN_TRAP_RECOVERY=1
  local ctx="$1"
  if [ "$LAUNCHER_SWAPPED" -eq 1 ]; then do_full_rollback "$ctx"; else do_early_compensation "$ctx"; fi
}

recover_and_mark(){
  local ctx="$1"
  log "[FAIL] ${ctx}"
  if [ "$MUTATED" -ne 1 ]; then
    # 理論上不會走到（變更前失敗都走 abort_no_change）；保險起見零變更收尾
    write_terminal_marker "FAILED_NEED_MANUAL" "${ctx}（變更階段旗標未設；請人工檢查）" || true
    exit 1
  fi
  perform_compensation "$ctx"
  exit 1
}

# 變更前失敗（含狀態閘、鎖、step0）：零變更、寫指定 marker、退出
abort_no_change(){
  local marker="$1" reason="$2"
  log "[ABORT] ${reason}（未進入變更階段，零 cutover 變更）"
  write_terminal_marker "$marker" "$reason" || true
  exit 1
}

# ── trap ──────────────────────────────────────────────────────────────────────
on_exit(){
  local rc=$?
  # 裁決 #5：非預期 EXIT（set -e／未攔截錯誤）於變更階段且尚無終態 marker → 先嘗試與 TERM 路徑
  # 相同的補償，再兜底寫 marker。IN_TRAP_RECOVERY 防遞迴（補償若自身再觸發 EXIT 不得重入）；
  # 補償內走 set +e，任一步失敗不再中斷 trap；有界性由 verify 的 VERIFY_ITERS 保證。
  if [ "${IN_TRAP_RECOVERY:-0}" -ne 1 ] \
     && [ "${TERMINAL_MARKER_WRITTEN:-0}" -ne 1 ] \
     && [ "${MUTATED:-0}" -eq 1 ] \
     && [ -n "${RUN_TS:-}" ] && [ "$rc" -ne 0 ]; then
    set +e
    log "[EXIT] 非預期退出 rc=${rc}（step=${CUR_STEP}）；MUTATED=1 且無終態 marker → 觸發補償"
    perform_compensation "非預期 EXIT rc=${rc}（step=${CUR_STEP}）"
  fi
  # 補償後（或本就非變更階段的非零退出）仍無 marker → 兜底寫 FAILED_NEED_MANUAL。
  if [ "${TERMINAL_MARKER_WRITTEN:-0}" -ne 1 ] && [ -n "${RUN_TS:-}" ] && [ "$rc" -ne 0 ]; then
    write_terminal_marker "FAILED_NEED_MANUAL" "非預期退出 rc=${rc}（MUTATED=${MUTATED}, step=${CUR_STEP}）；補償未留 marker，請人工檢查系統狀態與 log" || true
  fi
  if [ "${LOCK_HELD_BY_US:-0}" -eq 1 ]; then
    rmdir "$LOCKDIR" 2>/dev/null || rm -rf "$LOCKDIR" 2>/dev/null || true
    LOCK_HELD_BY_US=0
  fi
}
on_signal(){
  local sig="$1"
  log "[SIGNAL] 收到 ${sig}；於 step=${CUR_STEP} 中止"
  if [ "${TERMINAL_MARKER_WRITTEN:-0}" -eq 1 ]; then exit 1; fi
  if [ "${MUTATED:-0}" -eq 1 ]; then
    recover_and_mark "訊號 ${sig} 中斷（step=${CUR_STEP}）"
  else
    write_terminal_marker "FAILED_NEED_MANUAL" "訊號 ${sig}（變更前，零變更）"
    exit 1
  fi
}

# ── 原子執行鎖（BLOCKING 2：防並行/重跑）──────────────────────────────────────
acquire_lock(){
  mkdir -p "$LOCKROOT" 2>/dev/null || true
  if mkdir "$LOCKDIR" 2>/dev/null; then
    LOCK_HELD_BY_US=1
    { echo "pid=$$"; echo "run_ts=${RUN_TS}"; echo "at=$(ts)"; } > "${LOCKDIR}/owner" 2>/dev/null || true
    log "[LOCK] 取得執行鎖：${LOCKDIR}"
    return 0
  fi
  local owner; owner="$(cat "${LOCKDIR}/owner" 2>/dev/null || echo '未知')"
  log "[LOCK] 取鎖失敗——已有實例持有 ${LOCKDIR}（owner: ${owner}）。若確認無執行中實例（殘留鎖），手動移除該目錄後重試。"
  return 1
}

# ── detach（B 案；含父子握手 MAJOR #8）────────────────────────────────────────
detach_reexec(){
  local started="${LOGDIR}/ticket-1-10-cutover-${RUN_TS}.started"
  if command -v setsid >/dev/null 2>&1; then
    setsid nohup "$SELF" "$@" </dev/null >>"$LOG" 2>&1 &
  else
    nohup "$PY_BIN" -c 'import os,sys; os.setsid(); os.execv(sys.argv[1], sys.argv[1:])' \
      /bin/bash "$SELF" "$@" </dev/null >>"$LOG" 2>&1 &
  fi
  local child_pid=$!
  disown "$child_pid" 2>/dev/null || disown 2>/dev/null || true
  echo "[B案自我脫離] 背景 wrapper 已啟動（PID ${child_pid}）；等子程序 started marker 握手…"
  local waited=0
  while [ "$waited" -lt "$HANDSHAKE_LIMIT" ]; do
    if [ -f "$started" ]; then
      echo "[握手成功] 子程序已啟動並完成初始化：${started}"
      echo "log：${LOG}"
      echo "終態 marker 將出現在：${LOGDIR}/ticket-1-10-cutover-${RUN_TS}.{AWAITING_E2E|FAILED_ROLLED_BACK|FAILED_NEED_MANUAL|ABORTED_STATE_MISMATCH}"
      exit 0
    fi
    sleep 1; waited=$((waited+1))
  done
  echo "[握手逾時] ${HANDSHAKE_LIMIT}s 內未見 started marker——子程序可能 setsid/execv 失敗或立即退出。請檢查 log：${LOG}" >&2
  exit 1
}

# ── 唯讀檢查群（dry-run 與實跑步驟0 共用；純判別、不做 cutover 變更）─────────────
do_preflight_health(){
  local ok=0 mode="?"
  if [ -f "$ENVF" ]; then echo "  0a env 存在: yes"; else echo "  0a env 存在: NO（停）"; ok=1; fi
  [ -f "$ENVF" ] && mode="$(stat -f '%Lp' "$ENVF" 2>/dev/null || echo '?')"
  if [ "$mode" = "600" ]; then echo "  0a mode 600: yes"; else echo "  0a mode: ${mode}（應 600，停）"; ok=1; fi
  if [ -f "$ENVF" ] && grep -q '^MEOWVOICE_PIN=.\+' "$ENVF"; then echo "  0a PIN 非空: yes"; else echo "  0a PIN 非空: NO（停）"; ok=1; fi
  if check_audio_health; then echo "  0b audio-server(:8400) status=ok: ${LAST_HEALTH_BODY}"; else echo "  0b audio-server(:8400) 非 ok（curl --fail / JSON status）：'${LAST_HEALTH_BODY}'（停）"; ok=1; fi
  if check_bridge_health; then echo "  0c bridge(:8401) status/plugin/port 符合: ${LAST_HEALTH_BODY}"; else echo "  0c bridge(:8401) 非預期 JSON（status=ok,plugin=meowvoice,port=8401）：'${LAST_HEALTH_BODY}'（停）"; ok=1; fi
  return "$ok"
}

do_d_checks(){   # runbook 窗口前演練 D1/D3/D4/D5，實跑前唯讀硬驗證（MAJOR #5）
  local ok=0
  # D1：marketplace manifest validate --strict
  local d1out d1rc
  d1out="$("$CLAUDE_BIN" plugin validate "$MARKETPLACE_ROOT" --strict 2>&1)"; d1rc=$?
  if [ "$d1rc" -eq 0 ]; then echo "  D1 marketplace validate --strict: exit 0"; else echo "  D1 validate 失敗 rc=${d1rc}：${d1out}（停）"; ok=1; fi
  # D3：新 launcher bash -n ＋ 非註解行 dev 旗標 0、marketplace 通道 1
  if bash -n "$NEW_LAUNCHER" 2>/dev/null; then echo "  D3 新 launcher bash -n: OK"; else echo "  D3 新 launcher 語法失敗（停）"; ok=1; fi
  local ndev nmkt
  ndev="$(grep -v '^[[:space:]]*#' "$NEW_LAUNCHER" 2>/dev/null | grep -c 'dangerously-load-development-channels' || true)"
  nmkt="$(grep -v '^[[:space:]]*#' "$NEW_LAUNCHER" 2>/dev/null | grep -c 'plugin:meowvoice@meowvoice-local' || true)"
  ndev="$(printf '%s' "$ndev" | tr -d '[:space:]')"; nmkt="$(printf '%s' "$nmkt" | tr -d '[:space:]')"
  if [ "$ndev" = "0" ]; then echo "  D3 新 launcher dev 旗標計數: 0"; else echo "  D3 新 launcher dev 旗標計數=${ndev}（應 0，停）"; ok=1; fi
  if [ "$nmkt" = "1" ]; then echo "  D3 新 launcher marketplace 通道計數: 1"; else echo "  D3 新 launcher marketplace 通道計數=${nmkt}（應 1，停）"; ok=1; fi
  # D4：rollback bash -n
  if bash -n "$ROLLBACK" 2>/dev/null; then echo "  D4 rollback bash -n: OK"; else echo "  D4 rollback 語法失敗或不存在（停）"; ok=1; fi
  # D5：監督 running ＋ program 指向 launcher
  local prt
  prt="$("$LAUNCHCTL" print "$LC_TARGET" 2>/dev/null || true)"
  if printf '%s' "$prt" | grep -qE '^[[:space:]]*state = running'; then echo "  D5 監督 state=running: yes"; else echo "  D5 監督非 running（停）"; ok=1; fi
  if printf '%s' "$prt" | grep -qE 'program = .*claude-code-discord'; then echo "  D5 program 指向 claude-code-discord: yes"; else echo "  D5 program 未指向 launcher（停）"; ok=1; fi
  return "$ok"
}

do_state_gate(){   # 變更前狀態硬閘（BLOCKING 2）；純判別
  local ok=0
  # G1：live launcher 仍為舊版（含 dev 旗標）
  local ldev
  ldev="$(grep -v '^[[:space:]]*#' "$LAUNCHER" 2>/dev/null | grep -c 'dangerously-load-development-channels' || true)"
  ldev="$(printf '%s' "$ldev" | tr -d '[:space:]')"; [ -n "$ldev" ] || ldev=0
  if [ "$ldev" -ge 1 ]; then echo "  G1 live launcher 仍為舊版（dev 旗標=${ldev}）: yes"; else echo "  G1 live launcher 已非舊版（dev 旗標=${ldev}）——疑已 cutover（停）"; ok=1; fi
  # G2：skills live 存在
  if [ -e "$SKILLS_LIVE" ]; then echo "  G2 skills live 存在: yes"; else echo "  G2 skills live 不存在——疑已搬離（停）"; ok=1; fi
  # G3：plugin/marketplace 尚未安裝（查詢失敗＝fail-closed 視同不乾淨）
  local pl plrc mp mprc
  pl="$("$CLAUDE_BIN" plugin list 2>&1)"; plrc=$?
  mp="$("$CLAUDE_BIN" plugin marketplace list 2>&1)"; mprc=$?
  if [ "$plrc" -ne 0 ]; then echo "  G3 plugin list 查詢失敗 rc=${plrc}（fail-closed 視同不乾淨，停）"; ok=1
  elif printf '%s' "$pl" | grep -q 'meowvoice@meowvoice-local'; then echo "  G3 plugin 已安裝 meowvoice@meowvoice-local——疑已 cutover（停）"; ok=1
  else echo "  G3 plugin 尚未安裝: yes"; fi
  if [ "$mprc" -ne 0 ]; then echo "  G3 marketplace list 查詢失敗 rc=${mprc}（fail-closed，停）"; ok=1
  elif printf '%s' "$mp" | grep -q 'meowvoice-local'; then echo "  G3 marketplace 已註冊 meowvoice-local——疑已 cutover（停）"; ok=1
  else echo "  G3 marketplace 尚未註冊: yes"; fi
  # G4：監督 running
  if supervisor_running; then echo "  G4 監督 running: yes"; else echo "  G4 監督非 running（停）"; ok=1; fi
  # G5：無本票 SUCCESS/AWAITING_E2E marker
  local existing
  existing="$(ls "${LOGDIR}"/ticket-1-10-cutover-*.SUCCESS "${LOGDIR}"/ticket-1-10-cutover-*.AWAITING_E2E 2>/dev/null || true)"
  if [ -n "$existing" ]; then echo "  G5 已存在完成 marker（停）:"; printf '        %s\n' $existing; ok=1; else echo "  G5 無 SUCCESS/AWAITING_E2E marker: yes"; fi
  return "$ok"
}

check_artifacts_readonly(){
  local ok=0
  if [ -e "$MARKETPLACE_ROOT" ]; then echo "  [有] marketplace 根目錄: $MARKETPLACE_ROOT"; else echo "  [缺] marketplace 根目錄: $MARKETPLACE_ROOT"; ok=1; fi
  if [ -e "$NEW_LAUNCHER" ]; then echo "  [有] 新 launcher: $NEW_LAUNCHER"; else echo "  [缺] 新 launcher: $NEW_LAUNCHER"; ok=1; fi
  if [ -x "$ROLLBACK" ]; then echo "  [可執行] 回滾腳本: $ROLLBACK"; else echo "  [缺/不可執行] 回滾腳本: $ROLLBACK"; ok=1; fi
  if [ -e "$PLIST" ]; then echo "  [有] LaunchAgent plist: $PLIST"; else echo "  [缺] LaunchAgent plist: $PLIST"; ok=1; fi
  if [ -e "$LAUNCHER" ]; then echo "  [有] 現行 launcher: $LAUNCHER"; else echo "  [缺] 現行 launcher: $LAUNCHER"; ok=1; fi
  if [ -e "$SKILLS_LIVE" ]; then echo "  [有] 現行 skills-dir: $SKILLS_LIVE"; else echo "  [缺] 現行 skills-dir: $SKILLS_LIVE"; ok=1; fi
  return "$ok"
}

print_action_list(){
  cat <<'EOF'
  [B 案入場] 位於 claude-code-discord session 內 → 先 setsid/nohup 背景脫離（含父子握手）再跑。
  [鎖＋狀態閘] 取執行鎖；驗 launcher 仍舊版、skills live 存在、plugin 未裝、監督 running、無完成 marker。
  步驟 1 受控停機【監督 RUNNING→STOPPED】
    - 記 LOG_LINES_BEFORE = wc -l /tmp/claude-code-discord.log
    - launchctl bootout gui/<uid>/com.claude-code.discord ; tmux kill-session -t claude-code-discord
    - 驗證：launchctl print 非零＝監督已停 (STOPPED)
  步驟 2 marketplace 註冊/安裝/disable【監督 STOPPED】
    - claude plugin marketplace add ~/github/meowvoice/plugin-marketplace
    - claude plugin install / disable meowvoice@meowvoice-local ; 驗 list ✘ disabled
    - 2c 預熱：bun install --no-summary（installPath）→ 驗 @modelcontextprotocol/sdk
  步驟 3 舊 skills 源目錄搬離【監督 STOPPED】
    - mv ~/.claude/skills/meowvoice ~/.claude/skills-archive/meowvoice-pre-1-10-<STAMP> ; 驗不存在
  步驟 4 launcher 換裝【監督 STOPPED】
    - cp 現行 → bak-pre-1-10-<STAMP>（備份一成功即 LAUNCHER_SWAPPED=1，其後失敗走完整 rollback）
    - 原子替換：cp new-1-10 → 暫存檔 ; chmod 700 暫存（失敗即停）; mv -f 暫存 → 現行 ; stat 驗 mode=700
    - 驗證：bash -n OK；dev 旗標 0、marketplace 通道 1
  步驟 4.5 enable【監督 STOPPED】：claude plugin enable ; 驗 list ✔ enabled
  步驟 5 恢復監督 bootstrap【監督 STOPPED→RUNNING】：launchctl bootstrap ; 驗 state=running
  步驟 6 重啟後驗證【監督 RUNNING】
    - sleep 40 ; tmux has-session ; monitored log 新增行含「Launcher invoked (1-10 marketplace variant)」
      且不含「dev-channels dialog detected」；TUI pane 不得出現「Loading development channels」確認框；
      claude plugin list ✔ enabled ; 8401/health JSON status=ok
    - 過 → 寫 AWAITING_E2E（非 SUCCESS）
  步驟 7 E2E 語音實測：人工（runbook §7 方式 A/B）。通過後跑 `--finalize` 才升 SUCCESS。
  失敗補償：launcher 未換→清 marketplace＋(必要時)搬回 skills＋bootstrap 舊監督；
            launcher 已換→完整 rollback。兩者皆獨立驗證語音鏈恢復才記 FAILED_ROLLED_BACK。
EOF
}

# ── 實跑步驟（每個回 0＝通過；非 0＝失敗，主流程路由到 recover_and_mark）──────────
step1_shutdown(){
  log "== 步驟 1：受控停機（停監督 + 殺現行 session）=="
  LOG_LINES_BEFORE="$(wc -l < "$MONITORED_LOG" 2>/dev/null || echo 0)"
  LOG_LINES_BEFORE="$(printf '%s' "$LOG_LINES_BEFORE" | tr -d '[:space:]')"; [ -n "$LOG_LINES_BEFORE" ] || LOG_LINES_BEFORE=0
  log "  LOG_LINES_BEFORE=${LOG_LINES_BEFORE}"
  log "  launchctl bootout ${LC_TARGET}"
  "$LAUNCHCTL" bootout "$LC_TARGET" >/dev/null 2>&1 || true
  log "  tmux kill-session -t ${SESSION}"
  "$TMUXBIN" kill-session -t "$SESSION" 2>/dev/null || true
  if "$LAUNCHCTL" print "$LC_TARGET" >/dev/null 2>&1; then log "  [ERR] 監督仍在，bootout 未生效"; return 1; fi
  log "  監督已停 (STOPPED)"
  return 0
}

step2_marketplace(){
  log "== 步驟 2：註冊 marketplace、安裝、disable =="
  if ! "$CLAUDE_BIN" plugin marketplace add "$MARKETPLACE_ROOT"; then log "  [ERR] marketplace add 非零"; return 1; fi
  if ! "$CLAUDE_BIN" plugin install meowvoice@meowvoice-local; then log "  [ERR] install 非零"; return 1; fi
  if ! "$CLAUDE_BIN" plugin disable meowvoice@meowvoice-local; then log "  [ERR] disable 非零"; return 1; fi
  local pl plrc mv_line
  pl="$("$CLAUDE_BIN" plugin list 2>&1)"; plrc=$?
  log "  --- claude plugin list ---"; log "$pl"
  if [ "$plrc" -ne 0 ]; then log "  [ERR] plugin list 查詢失敗 rc=${plrc}（fail-closed）"; return 1; fi
  mv_line="$(printf '%s' "$pl" | grep 'meowvoice@meowvoice-local' || true)"
  if [ -z "$mv_line" ]; then log "  [ERR] plugin list 未見 meowvoice@meowvoice-local"; return 1; fi
  if ! printf '%s' "$mv_line" | grep -qi 'disabl'; then log "  [ERR] meowvoice 未呈 disabled：${mv_line}"; return 1; fi
  log "  list 顯示 ✘ disabled：${mv_line}"
  local install_path
  install_path="$("$PY_BIN" -c "import json,os;print(json.load(open(os.path.expanduser('${INSTALLED_PLUGINS_JSON}')))['plugins']['meowvoice@meowvoice-local'][0]['installPath'])" 2>/dev/null || echo "")"
  if [ -z "$install_path" ] || [ ! -d "$install_path" ]; then log "  [ERR] 取不到 installPath 或目錄不存在：'${install_path}'"; return 1; fi
  log "  installPath=${install_path}；( cd && bun install --no-summary )"
  if ! ( cd "$install_path" && "$BUN_BIN" install --no-summary ); then log "  [ERR] bun install 非零"; return 1; fi
  if [ -e "${install_path}/node_modules/@modelcontextprotocol/sdk" ]; then log "  deps OK"; else log "  [ERR] 預熱後仍缺 @modelcontextprotocol/sdk"; return 1; fi
  return 0
}

step3_move_skills(){
  log "== 步驟 3：舊 skills 源目錄搬離（防雙載）=="
  mkdir -p "$SKILLS_ARCHIVE"
  local dest="${SKILLS_ARCHIVE}/meowvoice-pre-1-10-${STAMP}"
  if [ ! -e "$SKILLS_LIVE" ]; then log "  [ERR] 待搬離的 ${SKILLS_LIVE} 不存在（狀態異常）"; return 1; fi
  if [ -e "$dest" ]; then log "  [ERR] 目的地已存在 ${dest}（避免 mv 落入子目錄）"; return 1; fi
  log "  mv ${SKILLS_LIVE} ${dest}"
  if ! mv "$SKILLS_LIVE" "$dest"; then log "  [ERR] mv 失敗"; return 1; fi
  if [ -e "$SKILLS_LIVE" ]; then log "  [ERR] 搬離後 ${SKILLS_LIVE} 仍存在"; return 1; fi
  SKILLS_MOVED=1
  log "  moved → ${dest}；${SKILLS_LIVE} 已不存在"
  return 0
}

step4_launcher(){
  log "== 步驟 4：Launcher 換裝（暫存檔＋chmod 700＋mv -f 原子替換）=="
  if [ ! -f "$NEW_LAUNCHER" ]; then log "  [ERR] 新 launcher 不存在：${NEW_LAUNCHER}"; return 1; fi
  local bak="${BIN}/claude-code-discord.bak-pre-1-10-${STAMP}"
  log "  cp ${LAUNCHER} ${bak}"
  if ! cp "$LAUNCHER" "$bak"; then log "  [ERR] 備份現行 launcher 失敗"; return 1; fi
  LAUNCHER_BAK_STAMP="$STAMP"
  # 裁決 #3：保守 checkpoint 前移——備份一建立成功即設 SWAPPED=1。其後任何失敗（含尚未真正
  # 替換的暫存寫入/chmod/mv 失敗）都走完整 rollback、用本備份還原 launcher；不容早期補償漏還原。
  LAUNCHER_SWAPPED=1
  # 原子替換：寫同目錄暫存檔 → chmod 700 → mv -f。暫存與目標同一檔案系統，mv＝rename(2) 原子操作，
  # 避免 cp 直接覆蓋在磁碟滿／IO 錯誤時半截斷 live launcher。
  local tmp="${LAUNCHER}.swap-${STAMP}"
  rm -f "$tmp" 2>/dev/null || true
  log "  寫暫存 ${tmp} ← ${NEW_LAUNCHER}"
  if ! cp "$NEW_LAUNCHER" "$tmp"; then log "  [ERR] 寫 launcher 暫存檔失敗"; rm -f "$tmp" 2>/dev/null || true; return 1; fi
  # MAJOR #6：chmod 失敗即失敗（不吞）——先對暫存檔設 700，再原子替換，事後仍正向驗 mode=700。
  if ! chmod 700 "$tmp"; then log "  [ERR] chmod 700 暫存檔失敗"; rm -f "$tmp" 2>/dev/null || true; return 1; fi
  log "  mv -f ${tmp} ${LAUNCHER}（原子替換）"
  if ! mv -f "$tmp" "$LAUNCHER"; then log "  [ERR] 原子替換 mv -f 失敗"; rm -f "$tmp" 2>/dev/null || true; return 1; fi
  local mode; mode="$(stat -f '%Lp' "$LAUNCHER" 2>/dev/null || echo '?')"
  if [ "$mode" != "700" ]; then log "  [ERR] launcher mode=${mode}（應 700）"; return 1; fi
  log "  chmod 700 驗證: ${mode}"
  if ! bash -n "$LAUNCHER"; then log "  [ERR] 換裝後 launcher 語法失敗"; return 1; fi
  local devcnt mktcnt
  devcnt="$(grep -v '^[[:space:]]*#' "$LAUNCHER" | grep -c 'dangerously-load-development-channels' || true)"
  mktcnt="$(grep -v '^[[:space:]]*#' "$LAUNCHER" | grep -c 'plugin:meowvoice@meowvoice-local' || true)"
  devcnt="$(printf '%s' "$devcnt" | tr -d '[:space:]')"; mktcnt="$(printf '%s' "$mktcnt" | tr -d '[:space:]')"
  log "  dev 旗標計數=${devcnt}（期望 0）；marketplace 通道計數=${mktcnt}（期望 1）"
  if [ "$devcnt" != "0" ]; then log "  [ERR] dev 旗標計數非 0"; return 1; fi
  if [ "$mktcnt" != "1" ]; then log "  [ERR] marketplace 通道計數非 1"; return 1; fi
  log "  syntax OK；換裝驗證通過"
  return 0
}

step45_enable(){
  log "== 步驟 4.5：Enable marketplace 版 =="
  if ! "$CLAUDE_BIN" plugin enable meowvoice@meowvoice-local; then log "  [ERR] enable 非零"; return 1; fi
  local pl plrc mv_line
  pl="$("$CLAUDE_BIN" plugin list 2>&1)"; plrc=$?
  log "  --- claude plugin list ---"; log "$pl"
  if [ "$plrc" -ne 0 ]; then log "  [ERR] plugin list 查詢失敗 rc=${plrc}（fail-closed）"; return 1; fi
  mv_line="$(printf '%s' "$pl" | grep 'meowvoice@meowvoice-local' || true)"
  if [ -z "$mv_line" ]; then log "  [ERR] plugin list 未見 meowvoice"; return 1; fi
  if printf '%s' "$mv_line" | grep -qi 'enabl' && ! printf '%s' "$mv_line" | grep -qi 'disabl'; then log "  list 顯示 ✔ enabled：${mv_line}"; return 0; fi
  log "  [ERR] meowvoice 未呈 enabled：${mv_line}"; return 1
}

step5_bootstrap(){
  log "== 步驟 5：恢復監督（bootstrap，以新 launcher 拉起）=="
  log "  launchctl bootstrap ${LC_DOMAIN} ${PLIST}"
  "$LAUNCHCTL" bootstrap "$LC_DOMAIN" "$PLIST" >/dev/null 2>&1 || true
  if supervisor_running; then log "  監督已恢復 (RUNNING)"; return 0; fi
  local prt; prt="$("$LAUNCHCTL" print "$LC_TARGET" 2>/dev/null || true)"
  log "  [ERR] bootstrap 後監督非 running"; log "$prt"; return 1
}

step6_verify(){
  log "== 步驟 6：重啟後驗證（等 ${STEP6_WAIT}s 讓新 launcher 拉起 session）=="
  [ "$STEP6_WAIT" -gt 0 ] && sleep "$STEP6_WAIT"
  local ok=0
  if "$TMUXBIN" has-session -t "$SESSION" 2>/dev/null; then log "  6a session alive"; else log "  6a [FAIL] session 未起"; ok=1; fi
  local new_log
  new_log="$(tail -n +$((LOG_LINES_BEFORE+1)) "$MONITORED_LOG" 2>/dev/null || echo "")"
  log "  --- monitored log 新增行 ---"; log "$new_log"
  if printf '%s' "$new_log" | grep -q 'Launcher invoked (1-10 marketplace variant)'; then log "  6b 新 launcher 生效"; else log "  6b [FAIL] 未見新 launcher 標記"; ok=1; fi
  if printf '%s' "$new_log" | grep -q 'dev-channels dialog detected'; then log "  6b [FAIL] 仍走舊 dev 路徑（log）"; ok=1; else log "  6b log 無 dev-channels 卡點"; fi
  # 6c 裁決 #4：對 TUI pane 的 dev-channels 確認框做負向硬判。capture 失敗 → 記 6c=INCONCLUSIVE，
  # 不擋此處到 AWAITING_E2E（session 可能剛起、pane 尚未穩定）；但 finalize 會再重跑一次負向硬判，
  # 使 SUCCESS 永遠經過一次有效 6c 判定。偵測到確認框文字 → 直接 FAIL（擋 step6）。
  local pane cap_rc
  pane="$("$TMUXBIN" capture-pane -pt "$SESSION" 2>/dev/null)"; cap_rc=$?
  if [ "$cap_rc" -ne 0 ]; then
    STEP6C_RESULT="INCONCLUSIVE"
    log "  6c [WARN] capture-pane 失敗 rc=${cap_rc}（記 6c=INCONCLUSIVE，不擋 AWAITING；finalize 會重跑負向硬判）"
  else
    log "  --- TUI 末 15 行 ---"; log "$(printf '%s' "$pane" | tail -n 15)"
    if printf '%s' "$pane" | grep -qF "$DEVDIALOG_MARKER"; then STEP6C_RESULT="FAIL"; log "  6c [FAIL] TUI 仍卡在 dev-channels 確認框（'${DEVDIALOG_MARKER}'）"; ok=1; else STEP6C_RESULT="PASS"; log "  6c TUI 無 dev-channels 確認框"; fi
  fi
  local pl plrc mv_line
  pl="$("$CLAUDE_BIN" plugin list 2>&1)"; plrc=$?
  if [ "$plrc" -ne 0 ]; then log "  6d [FAIL] plugin list 查詢失敗 rc=${plrc}"; ok=1; else
    mv_line="$(printf '%s' "$pl" | grep 'meowvoice@meowvoice-local' || true)"
    if printf '%s' "$mv_line" | grep -qi 'enabl' && ! printf '%s' "$mv_line" | grep -qi 'disabl'; then log "  6d enabled：${mv_line}"; else log "  6d [FAIL] 未 enabled：${mv_line}"; ok=1; fi
  fi
  if check_bridge_health; then log "  6e bridge :8401 ok：${LAST_HEALTH_BODY}"; else log "  6e [FAIL] bridge :8401 非預期：'${LAST_HEALTH_BODY}'"; ok=1; fi
  return "$ok"
}

# ── dry-run（唯讀）─────────────────────────────────────────────────────────────
dry_run(){
  LOGDIR="$LOGDIR_DEFAULT"   # 供 G5 marker 掃描；dry-run 不建目錄、不寫檔
  echo "===== Ticket 1-10 Cutover —— DRY-RUN（唯讀，零 cutover 變更、零停機）====="
  echo "時間：$(ts)"
  echo "本機 setsid：$(command -v setsid >/dev/null 2>&1 && echo '有' || echo '無（實跑用 python3 os.setsid()）')"
  if in_live_session; then echo "目前位置：claude-code-discord session 內（實跑會自動 B 案背景脫離）"; else echo "目前位置：非 claude-code-discord session（實跑會前景執行）"; fi
  local rc=0
  echo; echo "----- 步驟 0a-0c：健康（curl --fail + JSON 狀態）-----"; do_preflight_health || rc=1
  echo; echo "----- D1/D3/D4/D5：前置產物硬驗證 -----"; do_d_checks || rc=1
  echo; echo "----- 變更前狀態硬閘（G1-G5）-----"; do_state_gate || rc=1
  echo; echo "----- 產物存在性盤點 -----"; check_artifacts_readonly || rc=1
  echo; echo "----- 實跑將依序執行（唯讀預覽）-----"; print_action_list
  echo
  if [ "$rc" -eq 0 ]; then echo "[DRY-RUN 結論] 全綠——實跑前置就緒（實跑仍會於變更前再驗一次）。"; else echo "[DRY-RUN 結論] 未全綠——實跑會在變更前中止、零變更。"; fi
  return "$rc"
}

# ── finalize（E2E 通過後升 SUCCESS）——裁決 #2 反向狀態閘 ─────────────────────
#   升 SUCCESS 前逐項硬驗：launcher 已是 marketplace 版（內容特徵）、live skills 已搬離、plugin
#   installed＋enabled、supervisor running、session＋8401 ok、6c 負向硬判（裁決 #4，重跑）、無晚於
#   目標 AWAITING 的失敗／回滾 marker。任一不符 → 非零退出、寫獨立 ABORTED_FINALIZE_MISMATCH
#   marker、保留原 AWAITING 不動（R2 重現的「混合回滾誤升」情境因此變成被拒）。
finalize_success(){
  LOGDIR="$LOGDIR_DEFAULT"
  echo "===== Ticket 1-10 Cutover —— FINALIZE（E2E 通過後升 SUCCESS；反向狀態閘）====="
  local awaiting
  # R3 MAJOR：AWAITING 可能因主 marker 路徑不可寫而落到 fallback namespace（見 write_terminal_marker）。
  # 兩處 namespace 都要找，跨兩處以 mtime 取最新一筆。本搜尋用 shell glob——glob 展開會透明解析路徑
  # 元件的 symlink（macOS /tmp→private/tmp 也照樣命中），故「不需」-H；F8 的 find 反之需 -H（機制差異見 F8）。
  awaiting="$(ls -t "${LOGDIR}"/ticket-1-10-cutover-*.AWAITING_E2E \
                     "${MARKER_FALLBACK_DIR}"/ticket-1-10-marker-fallback-*.AWAITING_E2E 2>/dev/null | head -1 || true)"
  if [ -z "$awaiting" ]; then echo "[FINALIZE] 找不到 AWAITING_E2E marker（LOGDIR 與 /tmp fallback 皆無；需先實跑至步驟 6 通過）。中止。" >&2; return 1; fi
  echo "[FINALIZE] 目標 marker：${awaiting}"

  local fail=0
  # F1：launcher 已是 marketplace 版（內容特徵：非註解行 dev 旗標 0、marketplace 通道 1）
  local fdev fmkt
  fdev="$(grep -v '^[[:space:]]*#' "$LAUNCHER" 2>/dev/null | grep -c 'dangerously-load-development-channels' || true)"
  fmkt="$(grep -v '^[[:space:]]*#' "$LAUNCHER" 2>/dev/null | grep -c 'plugin:meowvoice@meowvoice-local' || true)"
  fdev="$(printf '%s' "$fdev" | tr -d '[:space:]')"; fmkt="$(printf '%s' "$fmkt" | tr -d '[:space:]')"
  [ -n "$fdev" ] || fdev=0; [ -n "$fmkt" ] || fmkt=0
  if [ "$fdev" = "0" ] && [ "$fmkt" = "1" ]; then echo "  F1 launcher 為 marketplace 版（dev=0 mkt=1）: yes"; else echo "  F1 launcher 非 marketplace 版（dev=${fdev} mkt=${fmkt}）——拒升"; fail=1; fi
  # F2：live skills 已搬離
  if [ ! -e "$SKILLS_LIVE" ]; then echo "  F2 live skills 已搬離: yes"; else echo "  F2 live skills 仍存在（${SKILLS_LIVE}）——拒升"; fail=1; fi
  # F3：plugin marketplace 版 installed 且 enabled。查詢退出碼顯式擷取——非零＝查詢失敗＝fail-closed
  # 拒升（R3 BLOCKING：`... || true` 會吞掉退出碼，plugin list 先印 enabled 行再 exit 1 時仍誤升）。
  # 以 `if ! pl="$(...)"` 於 if 條件擷取，set -e 不會提前中止，pl 仍取得輸出供後續文字判定。
  local pl mv_line
  if ! pl="$("$CLAUDE_BIN" plugin list 2>&1)"; then
    echo "  F3 plugin list 查詢失敗（rc 非零，fail-closed）——拒升"; fail=1
  else
    mv_line="$(printf '%s' "$pl" | grep 'meowvoice@meowvoice-local' || true)"
    if [ -z "$mv_line" ]; then echo "  F3 plugin 未見 meowvoice@meowvoice-local——拒升"; fail=1
    elif printf '%s' "$mv_line" | grep -qi 'enabl' && ! printf '%s' "$mv_line" | grep -qi 'disabl'; then echo "  F3 plugin marketplace 版 enabled: yes（${mv_line}）"
    else echo "  F3 plugin 非 enabled（${mv_line}）——拒升"; fail=1; fi
  fi
  # F4：supervisor running（裁決 #2 明列——避免對監督停止的孤兒狀態誤升）
  if supervisor_running; then echo "  F4 supervisor running: yes"; else echo "  F4 supervisor 非 running——拒升"; fail=1; fi
  # F5：session alive
  if "$TMUXBIN" has-session -t "$SESSION" 2>/dev/null; then echo "  F5 session alive: yes"; else echo "  F5 session 不存在——拒升"; fail=1; fi
  # F6：8401 health ok
  if check_bridge_health; then echo "  F6 8401 health ok: ${LAST_HEALTH_BODY}"; else echo "  F6 8401 非 ok（${LAST_HEALTH_BODY}）——拒升"; fail=1; fi
  # F7：6c 負向硬判重跑（裁決 #4：SUCCESS 永遠經過一次有效 6c 判定；此時 session 已起、capture 應可用）
  if ! check_6c_no_dialog; then echo "  F7 6c 負向硬判未過——拒升"; fail=1; fi
  # F8：無晚於目標 AWAITING 的失敗／回滾 marker（偵測 AWAITING 之後發生的外部回滾／失敗）。
  # R3 MAJOR：失敗／回滾 marker 同樣可能落在 LOGDIR 或 fallback namespace，且兩處各有不同檔名前綴
  # （cutover-* 與 marker-fallback-*）；兩 namespace × 兩前綴全查。
  # R4 BLOCKING：fallback namespace 預設 /tmp，macOS /tmp 是 symlink → private/tmp，而 find 預設「不」
  # 跟隨命令列 root symlink（探針：find /tmp 數到 0、find -H /tmp 數到 1）→ 同 namespace 的 FAILED 被漏掃
  # 而誤升 SUCCESS。故本 find 必用 -H（只跟隨命令列列出的 root symlink，不改變深層 symlink 遍歷語意）。
  local later_markers
  later_markers="$(find -H "${LOGDIR}" "${MARKER_FALLBACK_DIR}" -maxdepth 1 -type f \
      \( -name 'ticket-1-10-cutover-*.FAILED_ROLLED_BACK'         -o -name 'ticket-1-10-cutover-*.FAILED_NEED_MANUAL' \
         -o -name 'ticket-1-10-marker-fallback-*.FAILED_ROLLED_BACK' -o -name 'ticket-1-10-marker-fallback-*.FAILED_NEED_MANUAL' \) \
      -newer "$awaiting" 2>/dev/null || true)"
  if [ -n "$later_markers" ]; then echo "  F8 偵測晚於 AWAITING 的失敗／回滾 marker（疑外部已回滾）——拒升:"; printf '      %s\n' $later_markers; fail=1; else echo "  F8 無晚於 AWAITING 的失敗／回滾 marker: yes"; fi

  if [ "$fail" -ne 0 ]; then
    # 反向狀態閘未過：寫獨立 ABORTED marker，保留原 AWAITING 不動（不誤刪、不升 SUCCESS）。
    local fts mm
    fts="$(date +%Y%m%d-%H%M%S)-$$"
    mm="${LOGDIR}/ticket-1-10-cutover-finalize-${fts}.ABORTED_FINALIZE_MISMATCH"
    {
      echo "state=ABORTED_FINALIZE_MISMATCH"
      echo "finalize_at=$(ts)"
      echo "target_awaiting=${awaiting}"
      echo "reason=反向狀態閘未過（launcher/skills/plugin/supervisor/session/8401/6c/後續回滾 marker 任一不符）"
      echo "note=保留原 AWAITING_E2E 不動；請依 runbook 釐清實際狀態後再處置"
    } > "$mm" 2>/dev/null || echo "[FINALIZE] 連 ABORTED marker 都寫入失敗：${mm}" >&2
    echo "[FINALIZE] 反向狀態閘未過，拒升 SUCCESS。marker：${mm}；原 AWAITING 保留：${awaiting}" >&2
    return 1
  fi

  local success="${awaiting%.AWAITING_E2E}.SUCCESS"
  {
    echo "state=SUCCESS"
    echo "finalized_at=$(ts)"
    echo "from=${awaiting}"
    echo "note=E2E(步驟7) 人工驗證通過，語音鏈 marketplace 版 live（反向狀態閘全過）"
    echo "bridge=${LAST_HEALTH_BODY}"
  } > "$success" 2>/dev/null || { echo "[FINALIZE] 寫 SUCCESS marker 失敗" >&2; return 1; }
  rm -f "$awaiting" 2>/dev/null || true
  echo "[FINALIZE] 已升 SUCCESS：${success}（AWAITING_E2E 已移除）"
  return 0
}

# ── 實跑主流程（logged run；detached child 或 direct 前景）─────────────────────
real_run(){
  # 此時 stdout/stderr 已導入 LOG。脫離後 tmux 環境失效，unset 之（讓 rollback 守衛正確放行）。
  unset TMUX
  # 握手：盡早寫 started marker，證明 setsid/execv/init 成功（父程序在等它）
  : > "${LOGDIR}/ticket-1-10-cutover-${RUN_TS}.started" 2>/dev/null || true
  log "==== Ticket 1-10 cutover 實跑開始（RUN_TS=${RUN_TS}, STAMP=${STAMP}）===="
  log "log 檔：${LOG}"

  # 鎖（BLOCKING 2）
  if ! acquire_lock; then abort_no_change "ABORTED_LOCK_HELD" "取執行鎖失敗（疑並行實例或殘留鎖）"; fi
  # trap：釋放鎖 + 兜底終態 marker（含 set -e 中止）；變更階段訊號 → 補償
  trap on_exit EXIT
  trap 'on_signal TERM' TERM
  trap 'on_signal INT' INT
  trap 'on_signal HUP' HUP

  # 變更前狀態硬閘（BLOCKING 2）
  CUR_STEP="state-gate"
  log "-- 變更前狀態硬閘（G1-G5）--"
  if ! do_state_gate; then abort_no_change "ABORTED_STATE_MISMATCH" "變更前狀態硬閘未過（疑已完成/部分完成/狀態不符），零變更退出、不套任何舊備份"; fi
  log "  狀態硬閘全綠"

  # 步驟 0：健康 + D1/D3/D4/D5（全在 MUTATED=1 之前）
  CUR_STEP="step0"
  log "-- 步驟 0：健康 + D1/D3/D4/D5（唯讀）--"
  if ! do_preflight_health; then abort_no_change "FAILED_NEED_MANUAL" "步驟 0 健康檢查未過（零變更）"; fi
  if ! do_d_checks; then abort_no_change "FAILED_NEED_MANUAL" "步驟 0 前置產物硬驗證(D1/D3/D4/D5)未過（零變更）"; fi
  log "  步驟 0 全綠"

  MUTATED=1
  log "-- 進入變更階段（此後任一步失敗將自動補償/回滾）--"
  CUR_STEP="step1"; step1_shutdown    || recover_and_mark "步驟 1 受控停機"
  CUR_STEP="step2"; step2_marketplace || recover_and_mark "步驟 2 marketplace 註冊/安裝/disable"
  CUR_STEP="step3"; step3_move_skills || recover_and_mark "步驟 3 舊 skills 源目錄搬離"
  CUR_STEP="step4"; step4_launcher    || recover_and_mark "步驟 4 launcher 換裝"
  CUR_STEP="step4.5"; step45_enable   || recover_and_mark "步驟 4.5 enable"
  CUR_STEP="step5"; step5_bootstrap   || recover_and_mark "步驟 5 恢復監督 bootstrap"
  CUR_STEP="step6"; step6_verify      || recover_and_mark "步驟 6 重啟後驗證"

  # 步驟 0-6 已通過（cutover 成功）。以 if 消化返回碼：即便 marker 主／備用寫入皆失敗也不得讓
  # set -e 於此中止——否則 rc≠0 會誤觸 EXIT trap 補償，把已成功的 cutover 拆掉。
  if ! write_terminal_marker "AWAITING_E2E" "步驟 0-6 通過（6c=${STEP6C_RESULT}）；步驟 7 E2E 待人工，通過後跑 --finalize 升 SUCCESS"; then
    log "[WARN] AWAITING_E2E marker 主／備用寫入皆失敗；步驟 0-6 已通過，請人工補寫 marker（見上 log）。"
  fi
  log "==== 步驟 0-6 通過：AWAITING_E2E ===="
  log "步驟 7（E2E 語音實測）不自動化：由 Kevin／新 session 依 runbook §7 方式 A/B 實測留證，通過後執行 '${SELF} --finalize' 升 SUCCESS。"
  exit 0
}

# ── 進入點 ────────────────────────────────────────────────────────────────────
MODE="run"
case "${1:-}" in
  --dry-run) MODE="dry" ;;
  --finalize) MODE="finalize" ;;
  -h|--help)
    echo "用法：$(basename "$0") [--dry-run|--finalize]"
    echo "  --dry-run   唯讀預演（健康+D1/D3/D4/D5+狀態閘+盤點+動作清單）；零變更零停機"
    echo "  --finalize  E2E 通過後把 AWAITING_E2E marker 升為 SUCCESS"
    echo "  （無旗標）  實跑；於 live session 內自動 B 案背景脫離；步驟 6 過寫 AWAITING_E2E"
    exit 0 ;;
  "") : ;;
  *) echo "未知參數：$1（僅支援 --dry-run / --finalize / --help）" >&2; exit 64 ;;
esac

resolve_self

if [ "$MODE" = "dry" ]; then dry_run; exit $?; fi
if [ "$MODE" = "finalize" ]; then finalize_success; exit $?; fi

# ── 實跑：先決定 RUN_TS/LOG，再處理 B 案自我脫離 ──────────────────────────────
if [ "${TICKET_1_10_DETACHED:-0}" = "1" ]; then
  RUN_TS="${TICKET_1_10_RUN_TS:?detached but RUN_TS 未傳}"
  STAMP="${TICKET_1_10_STAMP:-$RUN_TS}"
  LOG="${TICKET_1_10_LOG:?detached but LOG 未傳}"
  LOGDIR="$(dirname "$LOG")"
else
  # 裁決 #7：RUN_TS 加 $$（PID）成唯一 STAMP，去除同秒並行實例共享 log／started／終態 marker
  # namespace 的碰撞（秒級時間戳不足以區分同秒啟動的兩實例）。log／started／終態 marker 同步採用。
  RUN_TS="$(date +%Y%m%d-%H%M%S)-$$"
  STAMP="$RUN_TS"
  LOGDIR="$LOGDIR_DEFAULT"
  LOG="${LOGDIR}/ticket-1-10-cutover-${RUN_TS}.log"
  mkdir -p "$LOGDIR"
  if in_live_session; then
    echo "[偵測] 位於 '${SESSION}' tmux session 內。步驟 1 會殺掉本 session，故啟動 B 案自我脫離："
    export TICKET_1_10_DETACHED=1 TICKET_1_10_RUN_TS="$RUN_TS" TICKET_1_10_STAMP="$STAMP" TICKET_1_10_LOG="$LOG"
    detach_reexec "$@"   # 背景重跑自身 + 父子握手；成功 exit 0、逾時 exit 1
  else
    echo "[偵測] 不在 '${SESSION}' session 內；於前景執行（輸出寫入 log）。追蹤：tail -f \"${LOG}\""
  fi
fi

exec >>"$LOG" 2>&1
real_run

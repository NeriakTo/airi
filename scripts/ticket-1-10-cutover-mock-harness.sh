#!/bin/bash
# 隔離 mock 驗證 ticket-1-10-cutover.sh 的新行為（BLOCKING 1/2/3 + MAJOR 補償/回滾 + R4 F3 查詢 rc）。
# 全程用 stub 二進位＋sandbox 路徑，絕不碰 live 系統（launchctl/tmux/claude/curl/bun 皆替身）。
# 執行法（兩種皆可）：`bash cutover-mock-harness.sh`（推薦），或已 chmod 755 後直接 `./cutover-mock-harness.sh`。
# 守門自證：SCRIPT env override 指向變異副本，例 `SCRIPT=/tmp/mut.sh bash cutover-mock-harness.sh`。
set -uo pipefail
# SCRIPT：受測腳本；預設 repo 內最新版，可用 env override 指向變異副本做守門自證。
SCRIPT="${SCRIPT:-$(cd "$(dirname "$0")" && pwd)/ticket-1-10-cutover.sh}"
# ROOT：隔離 mock 工作根，預設 mktemp 唯一暫存目錄（可攜、不綁 session），可用 env override。
ROOT="${ROOT:-$(mktemp -d -t ticket-1-10-mock)}"
chmod -R u+rwx "$ROOT" 2>/dev/null || true   # 情境 I 會建 500 唯讀目錄，先解權再清，避免殘留 rm 失敗
rm -rf "$ROOT"; mkdir -p "$ROOT"

PASS=0; FAIL=0
check(){ # $1 desc  $2 expected  $3 actual
  if [ "$2" = "$3" ]; then echo "  PASS: $1（$3）"; PASS=$((PASS+1)); else echo "  FAIL: $1（期望 $2，實得 $3）"; FAIL=$((FAIL+1)); fi
}

make_stubs(){ # $1 = SBX
  local SBX="$1" b="$1/bin"; mkdir -p "$b" "$SBX/state" "$SBX/installpath" "$SBX/bin_backups"
  cat > "$b/launchctl" <<EOF
#!/bin/bash
S="$SBX/state"
case "\$1" in
  print) [ "\$(cat \$S/supervisor 2>/dev/null)" = running ] && { printf '\tstate = running\n\tprogram = /x/claude-code-discord\n'; exit 0; } || exit 1 ;;
  bootout) echo stopped > \$S/supervisor; exit 0 ;;
  bootstrap)
     if [ "\${FAIL_AT:-}" = bootstrap ]; then echo stopped > \$S/supervisor; exit 0; fi
     if [ "\${BOOTSTRAP_LEAVE_SUPERVISOR_STOPPED:-}" = 1 ]; then
       # 補償 bootstrap 誤未拉起監督：session/8401 恢復但 supervisor 保持 stopped（固化 R2 變異，情境 H）
       echo alive > \$S/session; echo "\${BRIDGE_AFTER_RECOVER:-up}" > \$S/bridge; exit 0;
     fi
     echo running > \$S/supervisor; echo alive > \$S/session; echo "\${BRIDGE_AFTER_RECOVER:-up}" > \$S/bridge
     printf '[ts] Launcher invoked (1-10 marketplace variant)\n' >> "$SBX/monitored.log"
     exit 0 ;;
  *) exit 0 ;;
esac
EOF
  cat > "$b/tmux" <<EOF
#!/bin/bash
S="$SBX/state"
case "\$1" in
  kill-session) echo dead > \$S/session; exit 0 ;;
  has-session) [ "\$(cat \$S/session 2>/dev/null)" = alive ] && exit 0 || exit 1 ;;
  capture-pane) cat "\$S/pane" 2>/dev/null; exit 0 ;;
  display-message) echo notlive; exit 0 ;;
  *) exit 0 ;;
esac
EOF
  cat > "$b/claude" <<EOF
#!/bin/bash
S="$SBX/state"
[ "\$1" = plugin ] || exit 0
case "\$2" in
  validate) exit 0 ;;
  list)
    # R6 真實格式：claude plugin list 為多行區塊——「  ❯ <id>」標題行＋獨立「    Status:」行，區塊間空行分隔。
    # 舊 stub 誤把 id 與 status 印成同一行（假綠源頭），令 grep 標題行剛好命中 disabled/enabled。
    # 此處另併一個永遠 enabled 的 sibling（discord）→ 逼 parser 必須「隔離出 meowvoice 區塊」才讀對狀態；
    # disabled 情境整份輸出同時含 enabled(discord)/disabled(meowvoice)，盲 grep 整份必誤判，唯區塊解析正確。
    p=\$(cat \$S/plugin 2>/dev/null)
    printf 'Installed plugins:\n\n'
    printf '  \xE2\x9D\xAF discord@claude-plugins-official\n    Version: 0.0.4\n    Scope: user\n    Status: \xE2\x9C\x94 enabled\n\n'
    if [ "\$p" = installed_disabled ]; then
      printf '  \xE2\x9D\xAF meowvoice@meowvoice-local\n    Version: 0.1.0\n    Scope: user\n    Status: \xE2\x9C\x98 disabled\n'
    elif [ "\$p" = installed_enabled ]; then
      printf '  \xE2\x9D\xAF meowvoice@meowvoice-local\n    Version: 0.1.0\n    Scope: user\n    Status: \xE2\x9C\x94 enabled\n'
    fi
    exit \${LIST_RC:-0} ;;   # LIST_RC 可令 list 印出 enabled 文字卻 exit 非零（固化 R3 F3 變異，情境 L）
  install) [ "\${FAIL_AT:-}" = install ] && exit 1; echo installed_disabled > \$S/plugin; echo "Successfully installed"; exit 0 ;;
  disable) echo installed_disabled > \$S/plugin; exit 0 ;;
  enable) [ "\${FAIL_AT:-}" = enable ] && exit 1; echo installed_enabled > \$S/plugin; echo "Successfully enabled"; exit 0 ;;
  uninstall) echo none > \$S/plugin; exit 0 ;;
  marketplace)
    case "\$3" in
      add) [ "\${FAIL_AT:-}" = mktadd ] && exit 1; echo added > \$S/marketplace; echo "Successfully added"; exit 0 ;;
      list) [ "\$(cat \$S/marketplace 2>/dev/null)" = added ] && echo "meowvoice-local"; exit 0 ;;
      remove) echo none > \$S/marketplace; exit 0 ;;
    esac; exit 0 ;;
esac
exit 0
EOF
  cat > "$b/curl" <<EOF
#!/bin/bash
S="$SBX/state"; url=""
for a in "\$@"; do url="\$a"; done
case "\$url" in
  *8401*) [ "\$(cat \$S/bridge 2>/dev/null)" = up ] && { echo '{"status":"ok","plugin":"meowvoice","port":8401}'; exit 0; } || exit 22 ;;
  *8400*) echo '{"status":"ok"}'; exit 0 ;;
esac
exit 22
EOF
  cat > "$b/bun" <<'EOF'
#!/bin/bash
mkdir -p "node_modules/@modelcontextprotocol/sdk"; exit 0
EOF
  cat > "$b/rollback" <<EOF
#!/bin/bash
S="$SBX/state"
echo none > \$S/plugin; echo none > \$S/marketplace
echo running > \$S/supervisor; echo alive > \$S/session; echo "\${BRIDGE_AFTER_RECOVER:-up}" > \$S/bridge
: > \$S/rollback_called; echo "\${1:-}" > \$S/rollback_stamp   # sentinel：證明走了完整 rollback（情境 I 用）
exit \${ROLLBACK_RC:-0}
EOF
  chmod +x "$b"/*
  # 舊 launcher（含 1 條非註解 dev 旗標行）
  cat > "$SBX/launcher" <<'EOF'
#!/bin/bash
# comment: dangerously-load-development-channels 應被排除
claude --channels --dangerously-load-development-channels plugin:meowvoice@skills-dir
EOF
  # 新 launcher（0 dev 旗標、1 marketplace 通道）
  cat > "$SBX/new-launcher" <<'EOF'
#!/bin/bash
# variant note
claude --channels plugin:meowvoice@meowvoice-local
EOF
  chmod 644 "$SBX/launcher" "$SBX/new-launcher"
  mkdir -p "$SBX/skills-live"; echo "server" > "$SBX/skills-live/server.ts"
  mkdir -p "$SBX/skills-archive"
  mkdir -p "$SBX/fb"   # 空的 fallback namespace（hermetic：讓 finalize 的 fallback 搜尋不碰真實 /tmp）
  printf 'PLIST\n' > "$SBX/plist"
  printf 'MEOWVOICE_PIN=secret123\n' > "$SBX/env"; chmod 600 "$SBX/env"
  printf '{"plugins":{"meowvoice@meowvoice-local":[{"installPath":"%s"}]}}\n' "$SBX/installpath" > "$SBX/installed_plugins.json"
  printf 'normal claude tui\n> ready\n' > "$SBX/state/pane"
  printf '[old] line1\n[old] line2\n' > "$SBX/monitored.log"
  # 初始 live 狀態
  echo running > "$SBX/state/supervisor"; echo alive > "$SBX/state/session"
  echo none > "$SBX/state/plugin"; echo none > "$SBX/state/marketplace"; echo up > "$SBX/state/bridge"
}

run_scenario(){ # $1 name ; 其餘＝額外 env（如 FAIL_AT=install）
  local name="$1"; shift
  local SBX="$ROOT/$name"; make_stubs "$SBX"
  local b="$SBX/bin"
  env -u TMUX \
    LAUNCHCTL="$b/launchctl" TMUXBIN="$b/tmux" CLAUDE_BIN="$b/claude" CURL_BIN="$b/curl" BUN_BIN="$b/bun" \
    PY_BIN="python3" \
    LAUNCHER="$SBX/launcher" NEW_LAUNCHER="$SBX/new-launcher" ROLLBACK="$b/rollback" \
    MARKETPLACE_ROOT="$SBX" SKILLS_LIVE="$SBX/skills-live" SKILLS_ARCHIVE="$SBX/skills-archive" \
    ENVF="$SBX/env" INSTALLED_PLUGINS_JSON="$SBX/installed_plugins.json" PLIST="$SBX/plist" \
    BIN="$SBX/bin_backups" MONITORED_LOG="$SBX/monitored.log" \
    AUDIO_HEALTH="http://127.0.0.1:8400/health" BRIDGE_HEALTH="http://127.0.0.1:8401/health" \
    LOGDIR_DEFAULT="$SBX/logs" LOCKROOT="$SBX/locks" LOCKDIR="$SBX/locks/lock" \
    MARKER_FALLBACK_DIR="$SBX/fb" \
    VERIFY_ITERS=2 VERIFY_SLEEP=0 STEP6_WAIT=0 HANDSHAKE_LIMIT=3 \
    "$@" \
    bash "$SCRIPT" >/dev/null 2>&1
  # 回傳終態 marker 的 state 後綴
  ls "$SBX/logs"/ticket-1-10-cutover-*.* 2>/dev/null | grep -v '\.log$' | grep -v '\.started$' | sed 's/.*\.//' | head -1
}

echo "===== 情境 A：變更前狀態不符（launcher 已是新版）→ ABORTED_STATE_MISMATCH、零變更 ====="
SBX="$ROOT/A"; make_stubs "$SBX"; cp "$SBX/new-launcher" "$SBX/launcher"   # 讓 G1 偵測「已 cutover」
b="$SBX/bin"
env -u TMUX LAUNCHCTL="$b/launchctl" TMUXBIN="$b/tmux" CLAUDE_BIN="$b/claude" CURL_BIN="$b/curl" BUN_BIN="$b/bun" PY_BIN="python3" \
  LAUNCHER="$SBX/launcher" NEW_LAUNCHER="$SBX/new-launcher" ROLLBACK="$b/rollback" MARKETPLACE_ROOT="$SBX" \
  SKILLS_LIVE="$SBX/skills-live" SKILLS_ARCHIVE="$SBX/skills-archive" ENVF="$SBX/env" \
  INSTALLED_PLUGINS_JSON="$SBX/installed_plugins.json" PLIST="$SBX/plist" BIN="$SBX/bin_backups" \
  MONITORED_LOG="$SBX/monitored.log" LOGDIR_DEFAULT="$SBX/logs" LOCKROOT="$SBX/locks" LOCKDIR="$SBX/locks/lock" \
  VERIFY_ITERS=2 VERIFY_SLEEP=0 STEP6_WAIT=0 bash "$SCRIPT" >/dev/null 2>&1
A_state=$(ls "$SBX/logs"/*.* 2>/dev/null | grep -v '\.log$'|grep -v '\.started$'|sed 's/.*\.//'|head -1)
check "情境A marker" "ABORTED_STATE_MISMATCH" "$A_state"
check "情境A 未建 launcher 備份（零變更）" "0" "$(ls "$SBX/bin_backups"/claude-code-discord.bak-* 2>/dev/null | wc -l | tr -d ' ')"
check "情境A 監督未被停（仍 running）" "running" "$(cat "$SBX/state/supervisor")"

echo "===== 情境 B：執行鎖已被持有 → ABORTED_LOCK_HELD ====="
SBX="$ROOT/B"; make_stubs "$SBX"; mkdir -p "$SBX/locks/lock"   # 預先佔鎖
b="$SBX/bin"
env -u TMUX LAUNCHCTL="$b/launchctl" TMUXBIN="$b/tmux" CLAUDE_BIN="$b/claude" CURL_BIN="$b/curl" BUN_BIN="$b/bun" PY_BIN="python3" \
  LAUNCHER="$SBX/launcher" NEW_LAUNCHER="$SBX/new-launcher" ROLLBACK="$b/rollback" MARKETPLACE_ROOT="$SBX" \
  SKILLS_LIVE="$SBX/skills-live" SKILLS_ARCHIVE="$SBX/skills-archive" ENVF="$SBX/env" \
  INSTALLED_PLUGINS_JSON="$SBX/installed_plugins.json" PLIST="$SBX/plist" BIN="$SBX/bin_backups" \
  MONITORED_LOG="$SBX/monitored.log" LOGDIR_DEFAULT="$SBX/logs" LOCKROOT="$SBX/locks" LOCKDIR="$SBX/locks/lock" \
  VERIFY_ITERS=2 VERIFY_SLEEP=0 STEP6_WAIT=0 bash "$SCRIPT" >/dev/null 2>&1
B_state=$(ls "$SBX/logs"/*.* 2>/dev/null | grep -v '\.log$'|grep -v '\.started$'|sed 's/.*\.//'|head -1)
check "情境B marker" "ABORTED_LOCK_HELD" "$B_state"

echo "===== 情境 C：完整快樂路徑 → AWAITING_E2E（非 SUCCESS）====="
C_state=$(run_scenario C)
check "情境C marker" "AWAITING_E2E" "$C_state"
check "情境C 無殘留 SUCCESS" "0" "$(ls "$ROOT/C/logs"/*.SUCCESS 2>/dev/null | wc -l | tr -d ' ')"
check "情境C 鎖已釋放" "0" "$(ls -d "$ROOT/C/locks/lock" 2>/dev/null | wc -l | tr -d ' ')"

echo "===== 情境 D：步驟3(marketplace) install 失敗、launcher 未換、skills 已於步驟2 搬離 → 分步補償(含搬回 skills)+語音恢復 → FAILED_ROLLED_BACK ====="
# 重排後 FAIL_AT=install 令 step3 失敗（非舊 step2）；step2 已先搬 skills（SKILLS_MOVED=1），補償必須把 skills 搬回。
D_state=$(run_scenario D FAIL_AT=install)
check "情境D marker" "FAILED_ROLLED_BACK" "$D_state"
check "情境D 未換 launcher（無備份）" "0" "$(ls "$ROOT/D/bin_backups"/claude-code-discord.bak-* 2>/dev/null | wc -l | tr -d ' ')"
check "情境D marketplace 已清" "none" "$(cat "$ROOT/D/state/marketplace")"
check "情境D skills 已搬回（server.ts 復位）" "1" "$(ls "$ROOT/D/skills-live/server.ts" 2>/dev/null | wc -l | tr -d ' ')"
check "情境D archive 無殘留本次備份（搬回非複製）" "0" "$(ls -d "$ROOT/D/skills-archive"/meowvoice-pre-1-10-* 2>/dev/null | wc -l | tr -d ' ')"

echo "===== 情境 E：步驟5 bootstrap 失敗、launcher 已換 → 完整 rollback + 語音恢復 → FAILED_ROLLED_BACK ====="
E_state=$(run_scenario E FAIL_AT=bootstrap)
check "情境E marker" "FAILED_ROLLED_BACK" "$E_state"

echo "===== 情境 F：步驟5 失敗、rollback rc0 但語音鏈未恢復（bridge down）→ FAILED_NEED_MANUAL（BLOCKING 3）====="
F_state=$(run_scenario F FAIL_AT=bootstrap BRIDGE_AFTER_RECOVER=down)
check "情境F marker" "FAILED_NEED_MANUAL" "$F_state"

echo "===== 情境 G：detach + 父子握手 端到端（模擬 in-session）→ 子程序背景跑到 AWAITING_E2E ====="
SBX="$ROOT/G"; make_stubs "$SBX"; b="$SBX/bin"
# 不 unset TMUX、SESSION=notlive、stub display-message 回 notlive → in_live_session 為真 → 觸發 detach
env TMUX="/fake/tmux,1,0" SESSION="notlive" \
  LAUNCHCTL="$b/launchctl" TMUXBIN="$b/tmux" CLAUDE_BIN="$b/claude" CURL_BIN="$b/curl" BUN_BIN="$b/bun" PY_BIN="python3" \
  LAUNCHER="$SBX/launcher" NEW_LAUNCHER="$SBX/new-launcher" ROLLBACK="$b/rollback" MARKETPLACE_ROOT="$SBX" \
  SKILLS_LIVE="$SBX/skills-live" SKILLS_ARCHIVE="$SBX/skills-archive" ENVF="$SBX/env" \
  INSTALLED_PLUGINS_JSON="$SBX/installed_plugins.json" PLIST="$SBX/plist" BIN="$SBX/bin_backups" \
  MONITORED_LOG="$SBX/monitored.log" LOGDIR_DEFAULT="$SBX/logs" LOCKROOT="$SBX/locks" LOCKDIR="$SBX/locks/lock" \
  VERIFY_ITERS=2 VERIFY_SLEEP=0 STEP6_WAIT=0 HANDSHAKE_LIMIT=10 \
  bash "$SCRIPT" >/dev/null 2>&1
G_parent_rc=$?
# 父程序握手後 exit；子程序在背景續跑，等它寫出終態 marker
for _ in $(seq 1 20); do ls "$SBX/logs"/*.AWAITING_E2E >/dev/null 2>&1 && break; ls "$SBX"/logs/*.FAILED* >/dev/null 2>&1 && break; sleep 1; done
G_state=$(ls "$SBX/logs"/*.* 2>/dev/null | grep -v '\.log$'|grep -v '\.started$'|sed 's/.*\.//'|head -1)
check "情境G 父程序握手成功 exit 0" "0" "$G_parent_rc"
check "情境G started marker 出現（握手證據）" "1" "$(ls "$SBX/logs"/*.started 2>/dev/null | wc -l | tr -d ' ')"
check "情境G 子程序背景跑到 marker" "AWAITING_E2E" "$G_state"

echo "===== 情境 H：步驟3(marketplace install) 失敗、分步補償後 supervisor 仍 stopped → FAILED_NEED_MANUAL（裁決 #1/#9a）====="
# 固化 R2 變異：補償 bootstrap 讓 session/8401 恢復但 supervisor 保持 stopped；三合一硬閘須抓出。
H_state=$(run_scenario H FAIL_AT=install BOOTSTRAP_LEAVE_SUPERVISOR_STOPPED=1)
check "情境H marker" "FAILED_NEED_MANUAL" "$H_state"
check "情境H supervisor 確實 stopped（補償未拉起）" "stopped" "$(cat "$ROOT/H/state/supervisor" 2>/dev/null)"

echo "===== 情境 I：launcher 換裝中途失敗（備份已建、暫存寫入失敗）→ 完整 rollback 路徑（裁決 #3/#9c）====="
SBX="$ROOT/I"; make_stubs "$SBX"; b="$SBX/bin"
# launcher 置獨立子目錄；換裝暫存檔 ${LAUNCHER}.swap-* 需寫入該目錄，設 500（唯讀）→ 備份成功後暫存 cp
# 失敗＝「備份已建、換裝中途失敗」。備份寫 bin_backups、skills 在別處，均不受此唯讀目錄影響。
mkdir -p "$SBX/launcherdir"; cp "$SBX/launcher" "$SBX/launcherdir/claude-code-discord"; chmod 500 "$SBX/launcherdir"
env -u TMUX LAUNCHCTL="$b/launchctl" TMUXBIN="$b/tmux" CLAUDE_BIN="$b/claude" CURL_BIN="$b/curl" BUN_BIN="$b/bun" PY_BIN="python3" \
  LAUNCHER="$SBX/launcherdir/claude-code-discord" NEW_LAUNCHER="$SBX/new-launcher" ROLLBACK="$b/rollback" MARKETPLACE_ROOT="$SBX" \
  SKILLS_LIVE="$SBX/skills-live" SKILLS_ARCHIVE="$SBX/skills-archive" ENVF="$SBX/env" \
  INSTALLED_PLUGINS_JSON="$SBX/installed_plugins.json" PLIST="$SBX/plist" BIN="$SBX/bin_backups" \
  MONITORED_LOG="$SBX/monitored.log" LOGDIR_DEFAULT="$SBX/logs" LOCKROOT="$SBX/locks" LOCKDIR="$SBX/locks/lock" \
  VERIFY_ITERS=2 VERIFY_SLEEP=0 STEP6_WAIT=0 bash "$SCRIPT" >/dev/null 2>&1
chmod 700 "$SBX/launcherdir" 2>/dev/null || true   # 解權供 harness 收尾清理
I_state=$(ls "$SBX/logs"/*.* 2>/dev/null | grep -v '\.log$'|grep -v '\.started$'|sed 's/.*\.//'|head -1)
check "情境I marker" "FAILED_ROLLED_BACK" "$I_state"
check "情境I 備份已建（checkpoint 前移證據）" "1" "$(ls "$SBX/bin_backups"/claude-code-discord.bak-* 2>/dev/null | wc -l | tr -d ' ')"
check "情境I 走完整 rollback（sentinel 存在，非早期補償）" "1" "$(ls "$SBX/state/rollback_called" 2>/dev/null | wc -l | tr -d ' ')"

echo "===== 情境 J：finalize 反向狀態閘全過 → SUCCESS（happy；K 的鑑別對照）====="
SBX="$ROOT/J"; make_stubs "$SBX"; b="$SBX/bin"; mkdir -p "$SBX/logs"
cp "$SBX/new-launcher" "$SBX/launcher"; rm -rf "$SBX/skills-live"   # launcher=marketplace 版、skills 已搬離
echo installed_enabled > "$SBX/state/plugin"
echo running > "$SBX/state/supervisor"; echo alive > "$SBX/state/session"; echo up > "$SBX/state/bridge"
: > "$SBX/logs/ticket-1-10-cutover-20260101-000000-111.AWAITING_E2E"
env -u TMUX LAUNCHCTL="$b/launchctl" TMUXBIN="$b/tmux" CLAUDE_BIN="$b/claude" CURL_BIN="$b/curl" PY_BIN="python3" \
  LAUNCHER="$SBX/launcher" SKILLS_LIVE="$SBX/skills-live" SESSION="claude-code-discord" \
  BRIDGE_HEALTH="http://127.0.0.1:8401/health" AUDIO_HEALTH="http://127.0.0.1:8400/health" LOGDIR_DEFAULT="$SBX/logs" MARKER_FALLBACK_DIR="$SBX/fb" \
  bash "$SCRIPT" --finalize >/dev/null 2>&1
J_rc=$?
check "情境J finalize exit 0" "0" "$J_rc"
check "情境J SUCCESS marker 建立" "1" "$(ls "$SBX/logs"/*.SUCCESS 2>/dev/null | wc -l | tr -d ' ')"
check "情境J AWAITING 已移除" "0" "$(ls "$SBX/logs"/*.AWAITING_E2E 2>/dev/null | wc -l | tr -d ' ')"

echo "===== 情境 K：finalize 混合回滾狀態（launcher 回舊版＋skills 搬回，marketplace 仍 enabled）→ 拒升 + ABORTED_FINALIZE_MISMATCH（裁決 #2/#9b）====="
SBX="$ROOT/K"; make_stubs "$SBX"; b="$SBX/bin"; mkdir -p "$SBX/logs"
# launcher 維持 make_stubs 舊版（含 dev 旗標）、skills-live 仍在＝已搬回；但 plugin 仍 enabled、session/8401 live
echo installed_enabled > "$SBX/state/plugin"
echo running > "$SBX/state/supervisor"; echo alive > "$SBX/state/session"; echo up > "$SBX/state/bridge"
: > "$SBX/logs/ticket-1-10-cutover-20260101-000000-222.AWAITING_E2E"
env -u TMUX LAUNCHCTL="$b/launchctl" TMUXBIN="$b/tmux" CLAUDE_BIN="$b/claude" CURL_BIN="$b/curl" PY_BIN="python3" \
  LAUNCHER="$SBX/launcher" SKILLS_LIVE="$SBX/skills-live" SESSION="claude-code-discord" \
  BRIDGE_HEALTH="http://127.0.0.1:8401/health" AUDIO_HEALTH="http://127.0.0.1:8400/health" LOGDIR_DEFAULT="$SBX/logs" MARKER_FALLBACK_DIR="$SBX/fb" \
  bash "$SCRIPT" --finalize >/dev/null 2>&1
K_rc=$?
check "情境K finalize 拒升 exit 非零" "1" "$K_rc"
check "情境K 無 SUCCESS marker" "0" "$(ls "$SBX/logs"/*.SUCCESS 2>/dev/null | wc -l | tr -d ' ')"
check "情境K 原 AWAITING 保留不動" "1" "$(ls "$SBX/logs"/*.AWAITING_E2E 2>/dev/null | wc -l | tr -d ' ')"
check "情境K 寫 ABORTED_FINALIZE_MISMATCH marker" "1" "$(ls "$SBX/logs"/*.ABORTED_FINALIZE_MISMATCH 2>/dev/null | wc -l | tr -d ' ')"

echo "===== 情境 L：finalize 時 plugin list 印 enabled 卻 exit 1（LIST_RC=1）→ F3 fail-closed 拒升（固化 R3 BLOCKING/#4）====="
SBX="$ROOT/L"; make_stubs "$SBX"; b="$SBX/bin"; mkdir -p "$SBX/logs"
# 其餘閘門全綠（launcher marketplace 版、skills 已搬離、supervisor/session/8401/6c 皆過），只讓 F3 的
# 查詢退出碼非零＝唯一失敗因子，隔離證明 F3 真的把 plugin list 非零當失敗（非只看輸出文字）。
cp "$SBX/new-launcher" "$SBX/launcher"; rm -rf "$SBX/skills-live"
echo installed_enabled > "$SBX/state/plugin"
echo running > "$SBX/state/supervisor"; echo alive > "$SBX/state/session"; echo up > "$SBX/state/bridge"
: > "$SBX/logs/ticket-1-10-cutover-20260101-000000-333.AWAITING_E2E"
env -u TMUX LAUNCHCTL="$b/launchctl" TMUXBIN="$b/tmux" CLAUDE_BIN="$b/claude" CURL_BIN="$b/curl" PY_BIN="python3" \
  LAUNCHER="$SBX/launcher" SKILLS_LIVE="$SBX/skills-live" SESSION="claude-code-discord" \
  BRIDGE_HEALTH="http://127.0.0.1:8401/health" AUDIO_HEALTH="http://127.0.0.1:8400/health" LOGDIR_DEFAULT="$SBX/logs" MARKER_FALLBACK_DIR="$SBX/fb" \
  LIST_RC=1 \
  bash "$SCRIPT" --finalize >/dev/null 2>&1
L_rc=$?
check "情境L finalize 拒升 exit 非零" "1" "$L_rc"
check "情境L 無 SUCCESS marker" "0" "$(ls "$SBX/logs"/*.SUCCESS 2>/dev/null | wc -l | tr -d ' ')"
check "情境L 原 AWAITING 保留不動" "1" "$(ls "$SBX/logs"/*.AWAITING_E2E 2>/dev/null | wc -l | tr -d ' ')"
check "情境L 寫 ABORTED_FINALIZE_MISMATCH marker" "1" "$(ls "$SBX/logs"/*.ABORTED_FINALIZE_MISMATCH 2>/dev/null | wc -l | tr -d ' ')"

echo "===== 情境 M：fallback root 為 symlink，其上有較新『本票』FAILED → finalize 必拒升（固化 R4 BLOCKING：F8 find 需 -H）====="
SBX="$ROOT/M"; make_stubs "$SBX"; b="$SBX/bin"; mkdir -p "$SBX/logs" "$SBX/fbreal"
ln -sfn "$SBX/fbreal" "$SBX/fblink"   # fallback root 為 symlink，重現 macOS /tmp→private/tmp
cp "$SBX/new-launcher" "$SBX/launcher"; rm -rf "$SBX/skills-live"   # 其餘閘門全綠，隔離出 F8 為唯一失敗因子
echo installed_enabled > "$SBX/state/plugin"
echo running > "$SBX/state/supervisor"; echo alive > "$SBX/state/session"; echo up > "$SBX/state/bridge"
# LOGDIR 無 AWAITING；AWAITING 只在 fallback；本票 FAILED 較新（touch -t 定序，避免 sleep）
touch -t 202601010000 "$SBX/fbreal/ticket-1-10-marker-fallback-20260101-000000-777.AWAITING_E2E"
touch -t 202601020000 "$SBX/fbreal/ticket-1-10-marker-fallback-20260102-000000-777.FAILED_ROLLED_BACK"
env -u TMUX LAUNCHCTL="$b/launchctl" TMUXBIN="$b/tmux" CLAUDE_BIN="$b/claude" CURL_BIN="$b/curl" PY_BIN="python3" \
  LAUNCHER="$SBX/launcher" SKILLS_LIVE="$SBX/skills-live" SESSION="claude-code-discord" \
  BRIDGE_HEALTH="http://127.0.0.1:8401/health" AUDIO_HEALTH="http://127.0.0.1:8400/health" LOGDIR_DEFAULT="$SBX/logs" \
  MARKER_FALLBACK_DIR="$SBX/fblink" \
  bash "$SCRIPT" --finalize >/dev/null 2>&1
M_rc=$?
check "情境M finalize 拒升 exit 非零" "1" "$M_rc"
check "情境M 無 SUCCESS marker（fallback）" "0" "$(ls "$SBX/fbreal"/*.SUCCESS 2>/dev/null | wc -l | tr -d ' ')"
check "情境M 原 AWAITING 保留不動（fallback）" "1" "$(ls "$SBX/fbreal"/*.AWAITING_E2E 2>/dev/null | wc -l | tr -d ' ')"
check "情境M 寫 ABORTED_FINALIZE_MISMATCH marker" "1" "$(ls "$SBX/logs"/*.ABORTED_FINALIZE_MISMATCH 2>/dev/null | wc -l | tr -d ' ')"

echo "===== 情境 M2（對照）：fallback 上較新的『他票』FAILED 精確前綴不匹配 → 不得誤擋 → finalize 應升 SUCCESS ====="
SBX="$ROOT/M2"; make_stubs "$SBX"; b="$SBX/bin"; mkdir -p "$SBX/logs" "$SBX/fbreal"
ln -sfn "$SBX/fbreal" "$SBX/fblink"
cp "$SBX/new-launcher" "$SBX/launcher"; rm -rf "$SBX/skills-live"
echo installed_enabled > "$SBX/state/plugin"
echo running > "$SBX/state/supervisor"; echo alive > "$SBX/state/session"; echo up > "$SBX/state/bridge"
touch -t 202601010000 "$SBX/fbreal/ticket-1-10-marker-fallback-20260101-000000-888.AWAITING_E2E"
# 他票（ticket-1-11-*）較新 FAILED——精確前綴不匹配本票 ticket-1-10-* 樣式，F8 不得計入
touch -t 202601020000 "$SBX/fbreal/ticket-1-11-marker-fallback-20260102-000000-888.FAILED_ROLLED_BACK"
env -u TMUX LAUNCHCTL="$b/launchctl" TMUXBIN="$b/tmux" CLAUDE_BIN="$b/claude" CURL_BIN="$b/curl" PY_BIN="python3" \
  LAUNCHER="$SBX/launcher" SKILLS_LIVE="$SBX/skills-live" SESSION="claude-code-discord" \
  BRIDGE_HEALTH="http://127.0.0.1:8401/health" AUDIO_HEALTH="http://127.0.0.1:8400/health" LOGDIR_DEFAULT="$SBX/logs" \
  MARKER_FALLBACK_DIR="$SBX/fblink" \
  bash "$SCRIPT" --finalize >/dev/null 2>&1
M2_rc=$?
check "情境M2 他票 FAILED 不誤擋 → finalize exit 0" "0" "$M2_rc"
check "情境M2 SUCCESS marker 建立（fallback）" "1" "$(ls "$SBX/fbreal"/*.SUCCESS 2>/dev/null | wc -l | tr -d ' ')"
check "情境M2 AWAITING 已移除（fallback）" "0" "$(ls "$SBX/fbreal"/*.AWAITING_E2E 2>/dev/null | wc -l | tr -d ' ')"

echo "===== 情境 N：步驟2(搬 skills) 自身失敗（archive 唯讀致 mv 失敗）→ skills 留原位、marketplace 未建、分步補償 → FAILED_ROLLED_BACK ====="
# 重排後 step2 先於 step3；此情境守門「step2 自身失敗」路徑：skills 不得被搬走、marketplace 不得被建立。
SBX="$ROOT/N"; make_stubs "$SBX"; b="$SBX/bin"
chmod 500 "$SBX/skills-archive"   # 目的地父目錄唯讀 → rename(2) EACCES → step2 mv 失敗
env -u TMUX LAUNCHCTL="$b/launchctl" TMUXBIN="$b/tmux" CLAUDE_BIN="$b/claude" CURL_BIN="$b/curl" BUN_BIN="$b/bun" PY_BIN="python3" \
  LAUNCHER="$SBX/launcher" NEW_LAUNCHER="$SBX/new-launcher" ROLLBACK="$b/rollback" MARKETPLACE_ROOT="$SBX" \
  SKILLS_LIVE="$SBX/skills-live" SKILLS_ARCHIVE="$SBX/skills-archive" ENVF="$SBX/env" \
  INSTALLED_PLUGINS_JSON="$SBX/installed_plugins.json" PLIST="$SBX/plist" BIN="$SBX/bin_backups" \
  MONITORED_LOG="$SBX/monitored.log" LOGDIR_DEFAULT="$SBX/logs" LOCKROOT="$SBX/locks" LOCKDIR="$SBX/locks/lock" \
  MARKER_FALLBACK_DIR="$SBX/fb" VERIFY_ITERS=2 VERIFY_SLEEP=0 STEP6_WAIT=0 \
  bash "$SCRIPT" >/dev/null 2>&1
chmod 700 "$SBX/skills-archive" 2>/dev/null || true   # 解權供 harness 收尾清理
N_state=$(ls "$SBX/logs"/*.* 2>/dev/null | grep -v '\.log$'|grep -v '\.started$'|sed 's/.*\.//'|head -1)
check "情境N marker" "FAILED_ROLLED_BACK" "$N_state"
check "情境N skills 留原位（server.ts 未動）" "1" "$(ls "$SBX/skills-live/server.ts" 2>/dev/null | wc -l | tr -d ' ')"
check "情境N marketplace 未建（step2 先於 step3，未進安裝）" "none" "$(cat "$SBX/state/marketplace")"
check "情境N 未換 launcher（無備份）" "0" "$(ls "$SBX/bin_backups"/claude-code-discord.bak-* 2>/dev/null | wc -l | tr -d ' ')"

echo "===== 守門自證 R6：反轉 parser 為『舊 grep 標題行』→ happy-path 必不得綠（證多行 stub＋斷言能抓 regression）====="
# 產生變異副本：把 plugin_block_status 還原成舊 bug 版（grep 出含 id 的行＝標題行，無狀態字）。
# python 內 assert 變異確有套用，避免「變異其實沒改到」使自證本身變假綠（守門測試須自證能抓錯）。
MUT="$ROOT/mutant-oldparser.sh"
python3 - "$SCRIPT" "$MUT" <<'PY'
import sys, re
src = open(sys.argv[1]).read()
mut = re.sub(
    r'plugin_block_status\(\)\{.*?\n\}',
    'plugin_block_status(){\n  printf \'%s\\n\' "$2" | grep "$1" || true\n}',
    src, count=1, flags=re.S)
assert mut != src, "R6 反轉變異未套用（plugin_block_status 未被替換）——自證失效"
open(sys.argv[2], 'w').write(mut)
PY
if [ -s "$MUT" ] && bash -n "$MUT" 2>/dev/null; then
  OLDSCRIPT="$SCRIPT"; SCRIPT="$MUT"
  R6_state=$(run_scenario R6mut)
  SCRIPT="$OLDSCRIPT"
  check "R6自證 舊 parser 下 happy-path 不再 AWAITING_E2E（gate 抓到 regression）" "true" "$([ "$R6_state" != "AWAITING_E2E" ] && echo true || echo false)"
  check "R6自證 舊 parser 下改走補償終態（FAILED_ROLLED_BACK）" "FAILED_ROLLED_BACK" "$R6_state"
else
  check "R6自證 變異副本產生且語法可執行" "yes" "no"
fi

echo "===== helper 單元測試（R6b）：區塊隔離 + fail-closed（直接對 plugin_present/plugin_block_status）====="
# 直接 source 兩個 helper，對「隔離」「無空行不洩漏」「非預期格式拒絕」「撞名說明行不誤判」逐點斷言。
eval "$(awk '/^plugin_present\(\)\{/,/^\}/' "$SCRIPT")"
eval "$(awk '/^plugin_block_status\(\)\{/,/^\}/' "$SCRIPT")"
G=$(printf '\xE2\x9D\xAF'); OKG=$(printf '\xE2\x9C\x94'); NOG=$(printf '\xE2\x9C\x98')
ISO_BLANK="$(printf "  $G discord@x\n    Status: $OKG enabled\n\n  $G meowvoice@meowvoice-local\n    Status: $NOG disabled\n")"
ISO_NOBLANK="$(printf "  $G discord@x\n    Status: $OKG enabled\n  $G meowvoice@meowvoice-local\n    Status: $NOG disabled\n")"
LEAK="$(printf "  $G meowvoice@meowvoice-local\n    Version: 0.1.0\n  $G discord@x\n    Status: $OKG enabled\n")"
NOTEN="$(printf "  $G meowvoice@meowvoice-local\n    Status: $NOG not enabled\n")"
FAILDIS="$(printf "  $G meowvoice@meowvoice-local\n    Status: failed to disable\n")"
COLL="$(printf "  $G meowvoice@skills-dir: $NOG Not loaded (meowvoice@meowvoice-local), precedence.\n")"
check "helper 隔離(有空行) 回 disabled" "disabled" "$(plugin_block_status meowvoice@meowvoice-local "$ISO_BLANK")"
check "helper 隔離(無空行) 回 disabled（不洩漏 sibling enabled）" "disabled" "$(plugin_block_status meowvoice@meowvoice-local "$ISO_NOBLANK")"
check "helper 目標無Status+sibling enabled 回空(fail-closed，不誤取 sibling)" "" "$(plugin_block_status meowvoice@meowvoice-local "$LEAK")"
check "helper 'not enabled' 回空(fail-closed)" "" "$(plugin_block_status meowvoice@meowvoice-local "$NOTEN")"
check "helper 'failed to disable' 回空(fail-closed)" "" "$(plugin_block_status meowvoice@meowvoice-local "$FAILDIS")"
check "helper present 撞名說明行(末欄非id)不誤判存在" "" "$(plugin_present meowvoice@meowvoice-local "$COLL")"

echo "===== 守門自證 R6b-1：盲回整份 plugin list（不做區塊隔離）→ happy-path 必不得綠 ====="
# 證「盲 grep 整份」變異會被抓：精確 token 比對下，st=整份多行 ≠ disabled/enabled → step3 拒 → 補償。
BLINDMUT="$ROOT/mutant-blindstatus.sh"
python3 - "$SCRIPT" "$BLINDMUT" <<'PY'
import sys, re
src = open(sys.argv[1]).read()
mut = re.sub(r'plugin_block_status\(\)\{.*?\n\}',
             'plugin_block_status(){\n  printf \'%s\\n\' "$2"\n}',
             src, count=1, flags=re.S)
assert mut != src, "盲變異未套用（plugin_block_status 未被替換）——自證失效"
open(sys.argv[2], 'w').write(mut)
PY
if [ -s "$BLINDMUT" ] && bash -n "$BLINDMUT" 2>/dev/null; then
  OLDS="$SCRIPT"; SCRIPT="$BLINDMUT"; BLIND_state=$(run_scenario blindmut); SCRIPT="$OLDS"
  check "R6b-1自證 盲回整份(不隔離)→happy-path 不再 AWAITING_E2E（gate 抓到）" "true" "$([ "$BLIND_state" != "AWAITING_E2E" ] && echo true || echo false)"
else
  check "R6b-1自證 盲變異副本產生且可執行" "yes" "no"
fi

echo "===== 守門自證 R6b-2：略過 skills 搬回 → 還原不完整必記 FAILED_NEED_MANUAL（非假報 ROLLED_BACK）====="
# 黑喵 round-2 抓到：舊碼 skills 搬回失敗只記 WARN，語音鏈健康就寫 FAILED_ROLLED_BACK＝假報回滾完成。
# 修正後：把搬回 mv 變異成 no-op（重現搬回失敗）→ server.ts 不復位 且 終態必為 FAILED_NEED_MANUAL。
NOMOVEBACK="$ROOT/mutant-nomoveback.sh"
python3 - "$SCRIPT" "$NOMOVEBACK" <<'PY'
import sys
src = open(sys.argv[1]).read()
old = 'mv "$sb" "$SKILLS_LIVE" 2>/dev/null'
assert old in src, "找不到搬回 mv——自證失效"
# 令搬回 mv 恆失敗（false）：SKILLS_LIVE 不建、restore_ok 保持 0 → 應記 FAILED_NEED_MANUAL
open(sys.argv[2], 'w').write(src.replace(old, 'false', 1))
PY
if [ -s "$NOMOVEBACK" ] && bash -n "$NOMOVEBACK" 2>/dev/null; then
  OLDS="$SCRIPT"; SCRIPT="$NOMOVEBACK"; NM_state=$(run_scenario nomoveback FAIL_AT=install); SCRIPT="$OLDS"
  check "R6b-2自證 略過搬回→server.ts 未復位" "0" "$(ls "$ROOT/nomoveback/skills-live/server.ts" 2>/dev/null | wc -l | tr -d ' ')"
  check "R6b-2自證 還原不完整→終態 FAILED_NEED_MANUAL（不假報 ROLLED_BACK）" "FAILED_NEED_MANUAL" "$NM_state"
else
  check "R6b-2自證 nomoveback 副本產生且可執行" "yes" "no"
fi

echo "===== 守門自證 R6c：SKILLS_LIVE 既存(含另一份 server.ts)→不得因它誤判還原成功，須 FAILED_NEED_MANUAL ====="
# 黑喵三審抓到：既存目標分支若靠「任一 server.ts 存在」判定，會在本次 archive 沒搬回時假報 ROLLED_BACK。
# 變異：在 do_early_compensation 的 SKILLS_MOVED 區塊起頭注入「重建 stale SKILLS_LIVE(含 server.ts)」，
# 使還原邏輯遇到既存目標 → 修正後應保持 restore_ok=0 → 終態 FAILED_NEED_MANUAL 且本次 archive 仍在。
STALELIVE="$ROOT/mutant-stalelive.sh"
python3 - "$SCRIPT" "$STALELIVE" <<'PY'
import sys
src = open(sys.argv[1]).read()
anchor = 'if [ "$SKILLS_MOVED" -eq 1 ]; then\n    restore_ok=0'
assert anchor in src, "找不到 SKILLS_MOVED 區塊——自證失效"
inject = anchor + '\n    mkdir -p "$SKILLS_LIVE" 2>/dev/null; printf \'stale\\n\' > "$SKILLS_LIVE/server.ts" 2>/dev/null   # R6c 注入：模擬 live 已被重建(既有 server.ts)'
open(sys.argv[2], 'w').write(src.replace(anchor, inject, 1))
PY
if [ -s "$STALELIVE" ] && bash -n "$STALELIVE" 2>/dev/null; then
  OLDS="$SCRIPT"; SCRIPT="$STALELIVE"; SL_state=$(run_scenario stalelive FAIL_AT=install); SCRIPT="$OLDS"
  check "R6c自證 既存 live 有 server.ts→終態 FAILED_NEED_MANUAL（不假報 ROLLED_BACK）" "FAILED_NEED_MANUAL" "$SL_state"
  check "R6c自證 本次 archive 未被搬回（仍殘留 → 證未真正還原）" "1" "$(ls -d "$ROOT/stalelive/skills-archive"/meowvoice-pre-1-10-* 2>/dev/null | wc -l | tr -d ' ')"
else
  check "R6c自證 stalelive 副本產生且可執行" "yes" "no"
fi

echo
echo "===== 總計 PASS=$PASS FAIL=$FAIL ====="
# 裁決 #8：有 FAIL 必須 exit 1（守門假綠修正——結尾 echo 之前會使 rc 恆為 0）。
if [ "$FAIL" -eq 0 ]; then echo "ALL GREEN"; exit 0; else echo "HAS FAILURES"; exit 1; fi

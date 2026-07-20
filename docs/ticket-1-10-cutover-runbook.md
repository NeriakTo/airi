# Ticket 1-10 Cutover Runbook — MeowVoice 語音頻道轉正式 local marketplace

| 項目 | 內容 |
|---|---|
| 票 | TriClaw Sprint 1 票 1-10「語音根治：正式 local marketplace」 |
| 性質 | 根治（非搶修）——現行「dev 旗標＋watcher」組合運作中，本 cutover 為換裝根治 |
| 目標 | meowvoice 由 `skills-dir` plugin＋`--dangerously-load-development-channels` 旗標，改為正式 marketplace 安裝，launcher 回純 `--channels` |
| 前置產物（staging 已完成） | ① marketplace `~/github/meowvoice/plugin-marketplace/`（`validate --strict` 通過、隔離 config dir add/install/disable/enable 皆 exit 0）② 新 launcher `~/.local/bin/claude-code-discord.new-1-10`（`bash -n` 通過）③ 回滾腳本 `~/.local/bin/ticket-1-10-rollback.sh`（三階段狀態機＋硬閘門；`bash -n` 通過、chmod 700、六情境 mock 實測） |

> 根因回顧：claude ≥2.1.211 對 `--dangerously-load-development-channels` 每次啟動跳互動確認框，headless 無人按 Enter 會卡死；現行 launcher 靠 watcher 送鍵止血。正式 marketplace 安裝的 channel plugin 走純 `--channels` 解析，不觸發該確認框，dev 旗標與 watcher 皆可移除。

---

## ⚠ 執行環境（硬性前提，違反即失敗）

**本 runbook 第 1 步起的所有指令，只能由「獨立 Terminal.app／SSH shell」執行，明文禁止由 `claude-code-discord` session（tmux 內）執行。**

理由：第 1 步 `tmux kill-session` 會殺死執行者本身；若由 live session 內跑，後續步驟（含回滾）將無人執行，且新 launcher 若啟動失敗，Discord 端已死無從救援。

判別指令（開工前先跑）：
```bash
echo "TMUX=${TMUX:-<empty>}"
[ -n "${TMUX:-}" ] && tmux display-message -p '本 shell session=#S' 2>/dev/null
# 期望：TMUX=<empty>；若在 tmux，session 名稱不得為 claude-code-discord
```

不可逆操作（停監督、重啟、搬離源目錄、launcher 換裝）皆須 Kevin 明示批准的維護窗口內執行。

> **受控停機設計（為何不靠 KeepAlive 自然重啟）**：`claude plugin install` 預設即 `✔ enabled`，disable 是下一步才生效；install→disable 之間仍有窗口，期間若 LaunchAgent KeepAlive 因任何原因自然重啟，新舊兩個 meowvoice server 會搶 `127.0.0.1:8401` → `EADDRINUSE` 崩潰。根治法：cutover 全程先 `launchctl bootout` **停掉 LaunchAgent 監督**，換裝完成後才 `launchctl bootstrap` 恢復；停監督期間 KeepAlive 不會自然重啟，install→disable 窗口無害。下列每步標注當下 **LaunchAgent 監督狀態**。

---

## 窗口前演練（唯讀／隔離，不碰活體——全部前置必跑）

不殺 live session、不寫正式 config、不動 LaunchAgent。括號內為 staging 段已實跑取得的真實輸出。

```bash
ROOT=~/github/meowvoice/plugin-marketplace

# D1. manifest schema（含 --strict）
claude plugin validate "$ROOT" --strict          # 實測：✔ Validation passed（exit 0）

# D2. 隔離 config dir 全流程空跑（add/install/disable/enable，不污染正式 config）
TMPCFG=$(mktemp -d)
CLAUDE_CONFIG_DIR="$TMPCFG" claude plugin marketplace add "$ROOT"                 # 實測：Successfully added marketplace: meowvoice-local，exit 0
CLAUDE_CONFIG_DIR="$TMPCFG" claude plugin install meowvoice@meowvoice-local       # 實測：Successfully installed，exit 0（即 ✔ enabled）
CLAUDE_CONFIG_DIR="$TMPCFG" claude plugin disable meowvoice@meowvoice-local       # 實測：Successfully disabled，list 顯示 ✘ disabled
CLAUDE_CONFIG_DIR="$TMPCFG" claude plugin enable  meowvoice@meowvoice-local       # 實測：Successfully enabled，list 顯示 ✔ enabled
rm -rf "$TMPCFG"

# D3. staged launcher 驗證指令空跑（排除註解行——搜尋字串同時出現在檔頭差異說明的註解，全文計數會誤判）
NEW=~/.local/bin/claude-code-discord.new-1-10
grep -v '^[[:space:]]*#' "$NEW" | grep -c 'dangerously-load-development-channels'  # 實測：0（全文含註解則為 2）
grep -v '^[[:space:]]*#' "$NEW" | grep -c 'plugin:meowvoice@meowvoice-local'       # 實測：1（全文含註解則為 2）
bash -n "$NEW" && echo "launcher syntax OK"

# D4. 回滾腳本語法
bash -n ~/.local/bin/ticket-1-10-rollback.sh && echo "rollback syntax OK"

# D5. LaunchAgent 監督現況（唯讀，確認 label 已載入、記下 domain target）
launchctl print gui/$(id -u)/com.claude-code.discord | grep -E '^\s*(state|program) '   # 實測：state = running；program = ~/.local/bin/claude-code-discord
```

全部符合實測值 → 進正式 cutover。

---

## 0. Preflight 檢查（唯讀，窗口內開頭先跑）【監督：RUNNING】

```bash
# 0a. 執行期 env 檔在位、mode 600、PIN 非空（server.ts 啟動時載入；.mcp.json env block 對 plugin-spawned server 不生效，PIN 只此一來源）
ENVF=~/.claude/channels/meowvoice/.env
[ -f "$ENVF" ]                          && echo "env 存在: yes"   || echo "env 存在: NO（停）"                        # 實測：yes
[ "$(stat -f '%Lp' "$ENVF")" = "600" ]  && echo "mode 600: yes"   || echo "mode: $(stat -f '%Lp' "$ENVF")（應 600）"  # 實測：600
grep -q '^MEOWVOICE_PIN=.\+' "$ENVF"    && echo "PIN 非空: yes"   || echo "PIN 非空: NO（停）"                        # 實測：yes

# 0b. audio-server 在線
curl -s http://127.0.0.1:8400/health || echo "audio-server 未回應——先修"

# 0c. 現行語音鏈健康（現行 skills-dir 版 bridge）
curl -s http://127.0.0.1:8401/health          # 期望：{"status":"ok","plugin":"meowvoice","port":8401}
```

驗證：0a 三項皆 yes、0b／0c 皆 `ok`。任一不符 → 停。

> 建議（非必需）：cutover 前將 `plugin-marketplace/` commit 進 meowvoice repo（未 commit 亦可安裝，commit 後 `gitCommitSha` 可追溯）。commit 屬需 Kevin 批准操作。

---

## 1. 受控停機（停 LaunchAgent 監督 + 殺現行 session）【監督：RUNNING → STOPPED】

```bash
# 1a. 記錄 log 現有行數（供第 6 步只檢查 bootstrap 後新增行，避免歷史行誤判）
LOG=/tmp/claude-code-discord.log
LOG_LINES_BEFORE=$(wc -l < "$LOG" 2>/dev/null || echo 0)
echo "LOG_LINES_BEFORE=$LOG_LINES_BEFORE"

# 1b. bootout 停掉 KeepAlive 監督（此後至第 5 步 bootstrap，序列中無自然重啟窗口）
launchctl bootout gui/$(id -u)/com.claude-code.discord
#   bootout 對「未載入」目標會報 "No such process / Boot-out failed: 5"——若確認先前為 running（見 D5）則應成功；
#   若報未載入錯誤，代表監督本就未起，視為已停繼續。

# 1c. 殺現行 session（監督已停，不會被自然拉回）
tmux kill-session -t claude-code-discord 2>/dev/null || true

# 驗證：監督確實停止（print 應報找不到 service）
launchctl print gui/$(id -u)/com.claude-code.discord >/dev/null 2>&1 && echo "[WARN] 監督仍在，bootout 未生效" || echo "監督已停 (STOPPED)"
```

驗證：末行印「監督已停 (STOPPED)」。**保留本 shell 的 `$LOG_LINES_BEFORE`。** 此後所有換裝步驟在無監督狀態進行。

---

## 2. 舊 skills 源目錄搬離（防雙載；先於 marketplace 安裝，消撞名）【監督：STOPPED】

> plugin 名稱取自 `plugin.json`（非目錄名），僅改目錄名不足——必須移出 skills-dir 掃描根（`~/.claude/skills/`）之外。
> 重排（R6，Kevin 選 1）：搬離提前到 marketplace 安裝之前，確保安裝當下 skills-dir 無同名副本，`claude plugin install` 不再產生「name already taken／takes precedence」撞名行。

```bash
STAMP=$(date +%Y%m%d-%H%M%S)
mkdir -p ~/.claude/skills-archive
mv ~/.claude/skills/meowvoice ~/.claude/skills-archive/meowvoice-pre-1-10-$STAMP
echo "moved → ~/.claude/skills-archive/meowvoice-pre-1-10-$STAMP"
ls ~/.claude/skills/meowvoice 2>&1        # 期望：No such file or directory
```

驗證：`ls ~/.claude/skills/meowvoice` 回 not found。（回滾腳本自動找最新 `meowvoice-pre-1-10-*` 搬回。）

---

## 3. 註冊 marketplace、安裝、disable（skills 已搬離，安裝無撞名）【監督：STOPPED（install→disable 窗口無害）】

```bash
ROOT=~/github/meowvoice/plugin-marketplace
claude plugin marketplace add "$ROOT"                    # 期望：Successfully added marketplace: meowvoice-local，exit 0
claude plugin install meowvoice@meowvoice-local          # 期望：Successfully installed，exit 0（此刻即 enabled，但監督已停，無雙載）
claude plugin disable meowvoice@meowvoice-local          # 期望：Successfully disabled
claude plugin list                                       # 期望：meowvoice@meowvoice-local 區塊 Status 行為 ✘ disabled（多行格式，見下）

# 3c. 預熱依賴（避免首次載入時 `bun install` 受網路影響延遲語音就緒）
INSTALL_PATH=$(python3 -c "import json;print(json.load(open('$HOME/.claude/plugins/installed_plugins.json'))['plugins']['meowvoice@meowvoice-local'][0]['installPath'])")
echo "installPath=$INSTALL_PATH"
( cd "$INSTALL_PATH" && bun install --no-summary )
ls "$INSTALL_PATH/node_modules/@modelcontextprotocol/sdk" >/dev/null && echo "deps OK"
```

> ⚠ `claude plugin list` 為**多行區塊**格式，狀態在**獨立的 `Status:` 行**，非與 plugin id 同一行：
> ```
>   ❯ meowvoice@meowvoice-local
>     Version: 0.1.0
>     Status: ✘ disabled          ← 狀態在此行，非標題行
> ```
> 腳本以 `plugin_block_status` 抽該 id 區塊的 Status 行判定（R6 真因修復；舊碼 grep 標題行找 disabl 於真實輸出永遠 miss，且 harness mock 誤用單行格式而假綠）。

驗證：add/install/disable exit 0；meowvoice@meowvoice-local 區塊 Status 行為 **✘ disabled**；3c 印 `deps OK`。

---

## 4. Launcher 換裝【監督：STOPPED】

```bash
STAMP=$(date +%Y%m%d-%H%M%S)
cp ~/.local/bin/claude-code-discord ~/.local/bin/claude-code-discord.bak-pre-1-10-$STAMP   # 備份供回滾
cp ~/.local/bin/claude-code-discord.new-1-10 ~/.local/bin/claude-code-discord
chmod 700 ~/.local/bin/claude-code-discord
# 驗證（排除註解行——搜尋字串亦出現在檔頭差異說明的註解，全文 grep -c 會回 2/2 誤判）：
bash -n ~/.local/bin/claude-code-discord && echo "syntax OK"
grep -v '^[[:space:]]*#' ~/.local/bin/claude-code-discord | grep -c 'dangerously-load-development-channels'   # 期望：0
grep -v '^[[:space:]]*#' ~/.local/bin/claude-code-discord | grep -c 'plugin:meowvoice@meowvoice-local'        # 期望：1
```

驗證：`syntax OK`；命令列 dev 旗標計數 **0**、marketplace 通道計數 **1**（實測值，見 D3）。記下 `$STAMP`。

> LaunchAgent plist 不動——它呼叫的路徑仍是 `~/.local/bin/claude-code-discord`，內容已換。

### 4.5 Enable marketplace 版【監督：STOPPED】

```bash
claude plugin enable meowvoice@meowvoice-local
claude plugin list        # 期望：meowvoice@meowvoice-local 區塊 Status 行為 ✔ enabled（多行格式，見 §3）
```

驗證：meowvoice@meowvoice-local 區塊 Status 行為 **✔ enabled**。（skills-dir 版已於 §2 搬離、launcher 已換、監督仍停，enable 後無雙載風險。）

---

## 5. 恢復監督（bootstrap，以新 launcher 拉起）【監督：STOPPED → RUNNING】

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.claude-code.discord.plist
#   bootstrap 成功即以「已換裝的新 launcher」啟動；RunAtLoad=true 會立即拉起 tmux session。
# 驗證監督恢復：
launchctl print gui/$(id -u)/com.claude-code.discord | grep -E '^\s*state ' && echo "監督已恢復 (RUNNING)"
```

驗證：`state = running`、印「監督已恢復 (RUNNING)」。等 ~40s 進第 6 步。

---

## 6. 重啟後驗證【監督：RUNNING】

```bash
sleep 40
LOG=/tmp/claude-code-discord.log

# 6a. session 已由新 launcher 拉起
/opt/homebrew/bin/tmux has-session -t claude-code-discord && echo "session alive"

# 6b. 只檢查本次 bootstrap 後「新增」的 log 行（offset 法，非 tail——歷史行可能落在 tail 窗內誤判）
NEW_LOG=$(tail -n +$((LOG_LINES_BEFORE+1)) "$LOG")
echo "$NEW_LOG"
echo "$NEW_LOG" | grep -q 'Launcher invoked (1-10 marketplace variant)' && echo "新 launcher 生效"     # 期望：命中
echo "$NEW_LOG" | grep -q 'dev-channels dialog detected' && echo "[FAIL] 仍走舊 dev 路徑" || echo "無 dev-channels 卡點"

# 6c. TUI 未卡在確認框
/opt/homebrew/bin/tmux capture-pane -pt claude-code-discord | tail -n 15

# 6d. plugin 已載入
claude plugin list        # 期望：meowvoice@meowvoice-local 區塊 Status 行為 ✔ enabled（多行格式，見 §3）

# 6e. bridge MCP server 已開機、HTTP inject endpoint 在線
curl -s http://127.0.0.1:8401/health   # 期望：{"status":"ok","plugin":"meowvoice","port":8401}
```

驗證：6a `session alive`；6b 新增行含「Launcher invoked (1-10 marketplace variant)」且**不含**「dev-channels dialog detected」；6c 無確認框；6d enabled；6e 回 `ok`。任一不符 → 進第 8 步回滾。

---

## 7. E2E 語音實測（以 transcript 或 bridge log 為證）【監督：RUNNING】

**判準：一次真實語音往返成功，且留下可查證據。**

方式 A（真實語音，首選）：Kevin 對麥克風說一句指令。
- 入站證據：bridge stderr 出現 `meowvoice: injected: <前 60 字>`（`server.ts:99`）；或 session transcript 出現 `<channel source="plugin:meowvoice:meowvoice">`。
- 回覆證據：session 呼叫 `voice_reply` → audio-server `/voice/reply-callback` log 有對應 `message_id` → TTS 播回可聽。

方式 B（合成注入，無需麥克風）：
```bash
PIN=$(grep '^MEOWVOICE_PIN=' ~/.claude/channels/meowvoice/.env | cut -d= -f2-)
curl -s -X POST http://127.0.0.1:8401/inject \
  -H "X-Voice-Pin: $PIN" -H "Content-Type: application/json" \
  -d '{"text":"票 1-10 cutover 合成注入測試","user":"Kevin"}'
# 期望：{"ok":true,"message_id":"voice-<ts>-<n>"}
```
- 證據：回傳 `ok:true`＋`message_id`；該文字於 session 以 voice channel 訊息現身、session 產生 `voice_reply` 回覆。

驗證：方式 A 或 B 至少一次成功且證據留存。

---

## 8. 回滾（cutover 任一步失敗或 E2E 不過時執行）

**執行預先布署的一鍵回滾腳本（受控停機包裹、自含式、不依賴任何 session）：**

```bash
# 由獨立 Terminal.app／SSH shell 執行（腳本內建守衛：在 claude-code-discord session 內會 exit 2 拒絕）
~/.local/bin/ticket-1-10-rollback.sh                 # 自動選最新 launcher／skills 備份
# 或指定第 4 步的 launcher 備份 STAMP：
# ~/.local/bin/ticket-1-10-rollback.sh 20260719-113000
```

腳本為**三階段狀態機＋硬閘門**（全面正向驗證，禁錯誤字串／錯誤碼特判；best-effort、每步失敗計數、結尾非零退出列失敗清單與手動處置）：
- 守衛：在 `claude-code-discord` session 內 → `exit 2` 拒絕。
- **停機硬閘門**：`launchctl bootout` 停監督 → `launchctl print` 正向判定監督已停（print 非零＝未載入；不 parse bootout 退出碼／訊息）。**未停＝零換檔中止、非零退出、不進 Phase**（防在監督存活時換檔雙載）。停成功才 `tmux kill-session`。
- **Phase 1（清除 marketplace）**：`uninstall`＋`marketplace remove` → 正向驗證 `claude plugin list` 與 `marketplace list` 皆查無 `meowvoice`；查得殘留＝Phase 1 失敗。**查詢指令本身非零＝查詢失敗＝fail-closed 視同殘留**（不因空輸出誤判清除成功）。
- **Phase 2（檔案還原）**：launcher `cp`＋還原後 **SHA-256 比對備份**；skills-dir `mv` 回＋`ls server.ts` 驗證。cp 失敗即計失敗、不誤驗舊檔；**skills-dir 目標已存在＝fail-closed 拒絕覆蓋、計失敗**（避免 mv 落入子目錄，待人工確認）。
- **Phase 3（恢復監督）硬閘門**：**只有 Phase 1 與 Phase 2 全數正向驗證通過才 `launchctl bootstrap`**（再以 `launchctl print` 正向驗證已載入）。任一 Phase 失敗＝不 bootstrap、非零退出、**監督刻意保持停止（防雙載）**並印明手動處置指令。

> 回滾腳本已於 staging 段用**六情境 mock** 實測（隔離、外部指令 no-op／可控替身、不碰活體）：①Phase 1 清除失敗（list 殘留）→ exit 1、bootstrap 0；②Phase 2 cp 失敗 → exit 1、bootstrap 0；③全成功 → exit 0、bootstrap 恰 1；④bootout-stuck（監督恆載入）→ 零換檔中止、exit 1、bootstrap 0（launcher 現場哨兵未動）；⑤list 查詢回非零 → fail-closed 視同殘留、Phase 1 失敗、exit 1、bootstrap 0；⑥既存 skills-dir → 拒絕覆蓋計失敗、exit 1、bootstrap 0。
> 註：launcher／skills 皆為 shell 腳本與檔案（非 binary），還原不違反「絕不回退舊 binary」鐵律。回滾後語音鏈路復原仍照 §7 方式 A/B。

---

## 9. Cutover 後（可選硬化，非本票必做）

- 評估重裝 managed-settings allowlist（票文：「完成後評估重裝 managed-settings 硬化」）。
- 漂移偵測（argv 正規化＋settings hash）併入 1-11 後之 doctor 票。
- 執行期 PIN 以 `~/.claude/channels/meowvoice/.env` 為準；staged `.mcp.json` 已移除硬編碼 PIN。開源前憑證掃描併 Sprint 3。

---

## 附錄：改動檔案清單（staging 段，全部在禁區外）

| 路徑 | 說明 |
|---|---|
| `~/github/meowvoice/plugin-marketplace/.claude-plugin/marketplace.json` | marketplace 清單（name: `meowvoice-local`） |
| `~/github/meowvoice/plugin-marketplace/meowvoice/.claude-plugin/plugin.json` | plugin manifest |
| `~/github/meowvoice/plugin-marketplace/meowvoice/.mcp.json` | MCP server 定義（**已移除硬編碼 `MEOWVOICE_PIN`**；env 僅留 PORT/AUDIO_SERVER） |
| `~/github/meowvoice/plugin-marketplace/meowvoice/server.ts` | 與現行版 SHA-256 一致（不動） |
| `~/github/meowvoice/plugin-marketplace/meowvoice/SKILL.md` | 與現行版 SHA-256 一致 |
| `~/github/meowvoice/plugin-marketplace/meowvoice/package.json` | `start` 改 `bun install --no-summary && bun server.ts`（cache 位置無 node_modules，比照官方 discord plugin） |
| `~/github/meowvoice/plugin-marketplace/meowvoice/bun.lock` | 與現行版一致 |
| `~/github/meowvoice/plugin-marketplace/meowvoice/.gitignore` | 忽略 node_modules |
| `~/github/meowvoice/plugin-marketplace/README.md` | 說明 |
| `~/.local/bin/claude-code-discord.new-1-10` | 新 launcher（第 4 步換裝來源） |
| `~/.local/bin/ticket-1-10-rollback.sh` | 一鍵回滾腳本（三階段狀態機＋硬閘門、受控停機包裹；`bash -n` 通過、chmod 700、六情境 mock 實測） |
| `~/github/meowvoice/docs/ticket-1-10-cutover-runbook.md` | 本檔 |

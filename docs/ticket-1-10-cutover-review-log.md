# 票 1-10 一次性 cutover 腳本——審修歷程與交付紀錄

日期：2026-07-19 ~ 07-20。腳本：`scripts/ticket-1-10-cutover.sh`（902 行；安裝副本 `~/.local/bin/ticket-1-10-cutover.sh`，chmod 700，SHA-256 同源）。守門：`scripts/ticket-1-10-cutover-mock-harness.sh`（57 斷言）。規格：`docs/ticket-1-10-cutover-runbook.md`。

## 目的

Kevin 裁定 cutover 切換方式選 **B 案**（一次性腳本、自我脫離背景執行）。本腳本把 MeowVoice Discord 語音頻道從 `--dangerously-load-development-channels` 旗標載入，切換為正式 local marketplace 安裝，一次性執行、失敗自動回滾。執行時會殺掉發起它的 claude session，故設計自我脫離（`os.setsid()`＋nohup，macOS 無 setsid 二進位）。

## 審修迴圈（Opus 實作 → 黑喵 Codex CLI 跨模型審查，五輪收斂）

| 輪 | 判定 | 抓到的 BLOCKING（收窄軌跡） |
|---|---|---|
| R1 | FAIL | 3 B：早步失敗回滾必缺備份致監督停擺／成功後重跑反向拆除正式版／rollback 可在語音鏈未恢復時假成功 ＋6 MAJOR |
| R2 | FAIL | 3 新 B（集中在 R1 修復新增碼）：補償未驗監督恢復／finalize 誤升混合回滾態（mock 實證）／launcher cp 非原子＋checkpoint 過晚 ＋harness 自身 exit-code 假綠 |
| R3 | FAIL | 1 B：finalize F3 吞 `claude plugin list` 退出碼（enabled 文字＋exit 1 仍升 SUCCESS） |
| R4 | FAIL | 1 B：F8 `find /tmp` 因 macOS `/tmp→private/tmp` symlink 漏掃 fallback FAILED marker → fail-open 誤升 |
| R5 | **PASS** | R4 BLOCKING 完整閉環（真實 symlink 重現確認拒升）；無新 blocking/major/minor |

每輪修復均由黑喵以獨立 mock 變異重現／反轉自證把關；主控在連兩輪 blocking（R2）時做停損裁決並輪換 fresh 分身。

> ⚠ **R5 PASS 是假綠**：2026-07-20 07:02 首次實跑於步驟 2 失敗回滾。真因見下 R6。R1–R5 的「守門非假綠」結論本身即建立在一個格式錯誤的 mock 之上（詳 R6），是本案最大教訓。

## R6：實跑失敗、假綠根因與四輪複審修復（2026-07-20）

**實跑結果**：`FAILED_ROLLED_BACK`，乾淨回滾、語音鏈零損傷（回滾機制本身如設計運作）。失敗點：步驟 2「plugin 呈 disabled」驗證。

**真因（對 live `claude plugin list` 實證）**：狀態驗證四處（step2/4.5/6d、finalize F3）都 `grep 'meowvoice@meowvoice-local'` 抓到**標題行**後在該行找 `disabl`/`enabl`；但真實輸出是**多行區塊**格式，狀態在獨立的 `Status:` 行——標題行永遠沒有狀態字，真實輸出必 miss。

**為何 R1–R5 沒抓到（假綠根因）**：`cutover-mock-harness.sh` 的 `claude` stub 把 id 與 status 印成**單行**（`meowvoice@meowvoice-local  Status: ✘ disabled`），與真實多行格式不符。於是 `grep 標題行` 在 mock 下剛好含 `disabled`＝命中，真實下 miss。38 斷言＋三組反轉變異全是對著錯誤格式驗——mock 格式偏離真實 CLI 輸出＝經典假綠。**runbook 的「期望輸出」與腳本 parser 也同步採了這個錯誤單行假設，三者互相自洽但都錯。**

**R6 修復（三軸）**：
1. **parser（真因）**：新增 `plugin_present`／`plugin_block_status`（awk）——以 marker `$1=="❯"` 辨識任一 plugin 標題、遇任一標題即關區塊（杜絕無空行時洩漏 sibling 狀態）、只「marker＋末欄==id」開目標區塊；回傳正規化狀態 token（Status 行「至多 2 欄」才取末欄轉小寫，否則回空），四處 caller 改精確相等比對（`= "disabled"`／`= "enabled"`），空／非預期格式一律 fail-closed 拒。
2. **步驟重排（Kevin 選 1，defense-in-depth）**：skills 搬離提前到 marketplace 安裝之前，安裝當下無同名 skills-dir＝無撞名；runbook 步驟序、格式期望同步更正。
3. **補償完整性**：`do_early_compensation` 新增 `restore_ok`——只有「本次 mv 明確成功 且 搬回後 server.ts 存在」才算還原完成；既存目標／備份不存在／mv 失敗一律強制 `FAILED_NEED_MANUAL`，不得因語音鏈健康或「另一份既存 server.ts」而假報 `FAILED_ROLLED_BACK`。

**守門重建**：`scripts/ticket-1-10-cutover-mock-harness.sh`（落地進 repo，不再只存 scratchpad）stub 改**真實多行格式**＋併一個永遠 enabled 的 sibling 逼區塊隔離；57 斷言，含四組變異／反轉自證——R6（退回舊標題 grep）、R6b-1（盲回整份不隔離）、R6b-2（搬回 mv 換 `false`）、R6c（注入既存 stale live）——每組變異後守門確實轉紅／轉對應終態，證非假綠。SCRIPT／ROOT 皆自動解析可攜。

**四輪跨模型複審（黑喵 Codex CLI，逐輪窄化）**：

| 輪 | 判定 | 內容 |
|---|---|---|
| R6-1 | FAIL | 4 MAJOR：parser 只靠空行分區(fail-open)／狀態 substring 非 fail-closed／harness 隔離自證無效／重排後補償未守門 ＋1 MINOR |
| R6-2 | FAIL | 確認 1/2/3 已解；抓新 MAJOR：skills 搬回失敗只記 WARN，語音鏈健康就假報 `FAILED_ROLLED_BACK`（且 harness 把此錯誤終態列為預期＝自製假綠）＋1 MINOR |
| R6-3 | FAIL | 唯一 MAJOR：既存目標分支靠「任一 server.ts 存在」判定，本次 archive 沒搬回仍假報回滾完成（殘留 fail-open）|
| R6-4 | **PASS** | 逐項確認 R6-3 MAJOR 已解、無誤傷其他路徑、R6c/R6b-2 變異非假綠、無新 blocking/major |

## 最終設計要點

- **狀態機保證**：LaunchAgent 監督 RUNNING→STOPPED→RUNNING 全程正向驗證；六種終態 marker（`SUCCESS` 僅 `--finalize` 寫／`AWAITING_E2E`／`FAILED_ROLLED_BACK`／`FAILED_NEED_MANUAL`／`ABORTED_STATE_MISMATCH`／`ABORTED_LOCK_HELD`／`ABORTED_FINALIZE_MISMATCH`）。
- **防重跑**：`mkdir` 原子執行鎖＋變更前五道狀態閘（G1-G5：launcher 仍舊版／skills live 在／plugin 未裝／監督 running／無完成 marker），不符零變更退出。
- **分步補償**：步驟 1/2/3 各自失敗走對應補償（不要求尚未產生的備份）；步驟 ≥4 失敗才呼叫完整 rollback。任何補償/回滾後必驗語音鏈三合一（supervisor＋session＋8401）才記成功，否則 `FAILED_NEED_MANUAL`。
- **SUCCESS 兩段制**：步驟 6 過只寫 `AWAITING_E2E`；步驟 7 人工 E2E 通過後跑 `--finalize`，八道反向狀態閘（含 6c 重判）全過才升 `SUCCESS`。
- **rollback.sh 不改**：補償邏輯全在 cutover 側，避免重跑既有回滾腳本的六情境 mock。

## 守門測試（R6 後現況）

`scripts/ticket-1-10-cutover-mock-harness.sh`（全 stub、不碰活體，已落地進 repo）**57 斷言**，涵蓋狀態閘、執行鎖、快樂路徑停 AWAITING_E2E、步驟 2/3 早期補償（含 skills 搬回守門）、step2 自身失敗（情境 N）、步驟 5 完整回滾、rollback 假成功攔截、detach 握手、supervisor-stopped 補償、finalize 混合回滾拒升、launcher 換裝中途失敗、F8 symlink fallback 攔截、helper 區塊隔離＋fail-closed 單元測試，及四組變異自證（R6/R6b-1/R6b-2/R6c）。

> R5 時期的「38 斷言」版本 stub 採**錯誤單行格式**，是假綠來源（見 R6），已被本版取代——切勿回退。

## 驗證狀態（主控本機權威，R6 後）

- `bash -n` 腳本＋harness 皆過；`--dry-run` exit 0（D1-D5＋G1-G5 全綠）。
- harness **57/57 全綠** exit 0；四組變異／反轉自證（含盲回整份、搬回失敗、既存 live）皆確實轉紅／轉對應終態，證守門非假綠。
- parser 對真實 live `claude plugin list`、含撞名行區塊、sibling 隔離、非預期格式（`not enabled`／`failed to disable`）單元測試皆正確。
- 安裝副本 `~/.local/bin/ticket-1-10-cutover.sh` 已與 repo 同步（SHA-256 一致、chmod 700）。
- 四輪黑喵跨模型複審收斂至 PASS（見上 R6 表）。

## 已知資訊性小項（PASS 後不動，避免破壞已審狀態）

- `MARKER_FALLBACK_DIR` 的錯誤訊息文字寫死「/tmp fallback」；production 預設即 /tmp，不影響判定或路徑行為。

## 待辦

- **執行需 Kevin 批維護窗口**（會重啟本 session；LaunchAgent 30s 內自動拉起新 session）。R6 修復已完成、四輪跨模型複審 PASS、install 副本已同步；就緒待批。
- 執行完步驟 7 人工 E2E 後，跑 `ticket-1-10-cutover.sh --finalize` 收尾升 SUCCESS。

## 教訓（R6）

- **mock 格式必須貼合真實 CLI 輸出**：stub 印單行、真實多行，令 grep 標題行的 parser 在 mock 命中、真實 miss。守門測試若對著錯誤格式驗，斷言再多、反轉自證再密都是假綠。跨模型審查也只能在「格式正確」的前提下才有意義。
- **不得把 buggy 行為編碼成測試預期**：R6b-2 初版把「搬回失敗仍 FAILED_ROLLED_BACK」列為預期斷言＝自製假綠，由黑喵 R6-2 抓出。守門斷言的「預期值」本身要能被獨立質疑。
- **診斷要追到生產路徑**：表面撞名（選 1 重排）只是次因，真兇是 parser 讀錯行；光修表面症狀不驗生產路徑會漏真因。

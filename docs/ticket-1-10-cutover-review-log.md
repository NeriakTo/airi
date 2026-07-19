# 票 1-10 一次性 cutover 腳本——審修歷程與交付紀錄

日期：2026-07-19 ~ 07-20。腳本：`scripts/ticket-1-10-cutover.sh`（850 行；安裝副本 `~/.local/bin/ticket-1-10-cutover.sh`，chmod 700，SHA-256 同源）。規格：`docs/ticket-1-10-cutover-runbook.md`。

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

## 最終設計要點

- **狀態機保證**：LaunchAgent 監督 RUNNING→STOPPED→RUNNING 全程正向驗證；六種終態 marker（`SUCCESS` 僅 `--finalize` 寫／`AWAITING_E2E`／`FAILED_ROLLED_BACK`／`FAILED_NEED_MANUAL`／`ABORTED_STATE_MISMATCH`／`ABORTED_LOCK_HELD`／`ABORTED_FINALIZE_MISMATCH`）。
- **防重跑**：`mkdir` 原子執行鎖＋變更前五道狀態閘（G1-G5：launcher 仍舊版／skills live 在／plugin 未裝／監督 running／無完成 marker），不符零變更退出。
- **分步補償**：步驟 1/2/3 各自失敗走對應補償（不要求尚未產生的備份）；步驟 ≥4 失敗才呼叫完整 rollback。任何補償/回滾後必驗語音鏈三合一（supervisor＋session＋8401）才記成功，否則 `FAILED_NEED_MANUAL`。
- **SUCCESS 兩段制**：步驟 6 過只寫 `AWAITING_E2E`；步驟 7 人工 E2E 通過後跑 `--finalize`，八道反向狀態閘（含 6c 重判）全過才升 `SUCCESS`。
- **rollback.sh 不改**：補償邏輯全在 cutover 側，避免重跑既有回滾腳本的六情境 mock。

## 守門測試

`cutover-mock-harness.sh`（全 stub、不碰活體）11+情境 38 斷言，涵蓋狀態閘、執行鎖、快樂路徑停 AWAITING_E2E、步驟 2 早期補償、步驟 5 完整回滾、rollback 假成功攔截、detach 握手、supervisor-stopped 補償、finalize 混合回滾拒升、launcher 換裝中途失敗、F8 symlink fallback 攔截（情境 M/M2）。每項關鍵修復均以反轉變異自證（退回修復→對應斷言 FAIL→harness exit 1）。

## 驗證狀態（主控本機權威）

- `bash -n` 兩件產物＋rollback 皆過；`--dry-run` exit 0（D1-D5 前置硬驗證＋G1-G5 狀態閘全綠）。
- harness 38/38 全綠 exit 0；三組反轉變異證明守門非假綠。
- 活體零寫入：六保護檔 SHA-256 全 MATCH 基準、skills 未動、live 8401 仍 ok。

## 已知資訊性小項（PASS 後不動，避免破壞已審狀態）

- `MARKER_FALLBACK_DIR` 的錯誤訊息文字寫死「/tmp fallback」；production 預設即 /tmp，不影響判定或路徑行為。

## 待辦

- **執行需 Kevin 批維護窗口**（會重啟本 session；LaunchAgent 30s 內自動拉起新 session）。
- 執行完步驟 7 人工 E2E 後，跑 `ticket-1-10-cutover.sh --finalize` 收尾升 SUCCESS。

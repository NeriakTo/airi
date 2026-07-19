# meowvoice-local marketplace

正式 Claude Code plugin marketplace，收錄 MeowVoice 語音頻道 plugin（TriClaw Sprint 1 票 1-10「語音根治」交付物）。

## 用途

取代舊有的 `~/.claude/skills/meowvoice/`（skills-dir plugin＋`--dangerously-load-development-channels` dev 旗標）形態。轉為正式 local marketplace 後，launcher 可回純 `--channels`，不再需要 dev 旗標與 tmux watcher 送鍵解卡（claude ≥2.1.211 dev-channels 確認框卡死 headless 的根因）。

## 結構

```
plugin-marketplace/
├── .claude-plugin/
│   └── marketplace.json     # marketplace 清單（name: meowvoice-local）
└── meowvoice/               # plugin 本體（source: ./meowvoice）
    ├── .claude-plugin/
    │   └── plugin.json      # plugin manifest（name: meowvoice, channel: meowvoice）
    ├── .mcp.json            # MCP server 定義（bun run start）
    ├── server.ts            # MCP stdio relay ＋ HTTP inject endpoint（與現行版 byte-identical）
    ├── SKILL.md             # 語音頻道 skill（與現行版 byte-identical）
    ├── package.json         # start = "bun install --no-summary && bun server.ts"
    └── bun.lock             # 鎖定依賴版本（與現行版一致）
```

## 安裝與切換

見 `../docs/ticket-1-10-cutover-runbook.md`（含 marketplace add／install、舊源目錄搬離、launcher 換裝、E2E 驗證與回滾步驟）。

## 執行期外部依賴（非本 repo 管理）

- `~/.claude/channels/meowvoice/.env`：`server.ts` 於啟動時載入 `MEOWVOICE_PIN` 等（plugin-spawned MCP server 收不到 `.mcp.json` env block，故改由檔案載入）。cutover 前必須確認此檔存在（mode 600）。
- audio-server 於 `127.0.0.1:8400`（`voice_reply` TTS callback 目標）。

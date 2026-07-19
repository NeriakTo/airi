---
name: meowvoice
description: MeowVoice voice channel — 語音指令自動注入，不需手動觸發。
---

# MeowVoice Voice Channel

語音指令會自動以 `<channel source="plugin:meowvoice:meowvoice">` 格式出現在對話中。

收到語音訊息時：
1. 用 `voice_reply` tool 回覆（文字會轉 TTS 播回）
2. 回覆保持口語化、2-4 句、不用 markdown
3. 需要長時間處理的任務，先 voice_reply 簡短確認再動手

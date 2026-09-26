---
name: health-check
description: 在 AlphaEdge 專案中做健檢／審查／檢查有無問題時使用。分 diff（只審這次變更）與 milestone（全專案，只在里程碑用）兩種模式。
---

# AlphaEdge 健檢

`.claude/skills/health-check/SKILL.md` 是本專案健檢規範的**唯一權威文件**，完整定義了：
兩種模式的範圍、自動檢查指令、依風險排序的檢查清單、嚴重度門檻、輸出格式與通過條件。

本檔只是指標，**不重複任何規則內容**——規範一旦兩邊各存一份就會漂移。

## 執行步驟

1. 一律先完整讀取 [`.claude/skills/health-check/SKILL.md`](../../../.claude/skills/health-check/SKILL.md)，
   再開始健檢，不要只憑記憶或猜測範圍。
2. 依該文件的模式、清單與門檻執行並回報。

> 規則需要修改時，改 `.claude/skills/health-check/SKILL.md`，不要改本檔。
> 本檔僅在指標失效（例如權威文件搬家）時才需要更新。

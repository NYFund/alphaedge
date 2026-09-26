# ideas — 策略構想 / 文獻筆記

策略還沒被驗證、但**值得記下來**的想法都放這裡。

## 適合放這裡的東西

- 看論文 / 部落格 / 推特之後想到的訊號
- 「如果 X 大跌、Y 是不是會 Z？」這類待驗證假設
- 對既有策略的改良方向（樣本切分、特徵、停損規則）
- 失敗的實驗結論（**很重要**：避免之後重蹈覆轍）

## 建議結構

每一個想法一個 `snake_case` 主題子資料夾（與其他分類命名對齊）：

```
ideas/
├── README.md
└── momentum_breakout/          ← 例
    └── README.md               # 或 notes.md
```

## 寫作模板

在 `ideas/<topic>/README.md` 中使用：

```markdown
# <一句話標題>

## 想法
（一段話講清楚要做什麼）

## 假設與直覺
- 為什麼會 work？市場無效率在哪？
- 跟誰競爭？容量大概多大？

## 需要的資料
- 標的、頻率、來源
- 是否需要新加爬蟲 / API？

## 驗證步驟
1. 先做 ___ 的相關性檢驗（→ data_analysis/<topic>/）
2. 通過後做 ___ 的小型回測（→ strategies/<topic>/）
3. 通過後再考慮 ___

## 風險 / 已知反例
- ...

## 連結
- 文獻、相關 ideas/、相關 data_analysis/、相關 strategies/
```

## 流轉路徑

```
ideas/<topic>/ ──驗證可行──▶ data_analysis/<topic>/ ──訊號穩定──▶ strategies/<topic>/ ──成熟──▶ core/strategies/{stock,futures}/<name>.py
                                                                                                      │
                                                                                                      ▼
                                                                                             run.py --strategy <name>
```

失敗的想法也要保留檔案，標註 **「結論：不 work，原因：⋯⋯」**。

# strategy_lab 目錄規範（Claude Code）

> **本檔是 `strategy_lab/` 目錄規範的唯一來源**；`.cursor/rules/strategy-lab-layout.mdc`
> （globs: `strategy_lab/**`, alwaysApply: false）只是指向本檔的指標，內容不得複製一份。
> Claude Code 沒有 glob 條件規則機制，改用「目錄專屬 CLAUDE.md」達到同等效果：只有在 `strategy_lab/` 底下讀寫檔案時才會載入本規則。

`strategy_lab/` 是 R&D 工作區。四大分類為**大分類**，每個研究主題用**一個 `snake_case` 子資料夾**區分。

## 分類決策

| 工作性質                       | 放置位置                 |
| ------------------------------ | ------------------------ |
| 未驗證假設、文獻筆記、失敗結論 | `ideas/<topic>/`         |
| EDA、IC、特徵探索、非完整策略  | `data_analysis/<topic>/` |
| 探索性 Jupyter、快速視覺化     | `notebooks/<topic>/`     |
| 完整策略研究、可重現 pipeline  | `strategies/<topic>/`    |

同一主題跨分類時，**資料夾名稱保持一致**（例如 `ideas/momentum_breakout/` 與 `data_analysis/momentum_breakout/`）。

## 硬性規則

- **執行一律用 `-m`**（例如 `.venv/bin/python -m strategy_lab.data_analysis.<topic>.run`）。
  `pyproject.toml` 的套件 `include` 只收 `core*`、`tasks*`、`tests*`，**不含 `strategy_lab`**，
  直接跑檔案路徑時它不在 `sys.path` 上，腳本 import 不到自己所在的套件。
  **這條會讓新腳本直接跑不起來**，所以列在硬性規則而不是只寫在 README。
- 新檔案必須落在 `<category>/<topic>/` 內，不要在 `strategy_lab/` 頂層新增 script / notebook / md。
  頂層目前只有 `CLAUDE.md`、`README.md` 與 `__init__.py` 三個檔，那是目錄本身需要的，不是研究產出。
- 主題資料夾命名：`snake_case`，語意清楚（例：`tech_new_high_continuation`、`tsmc_overnight_signal`）。
- 研究產出（圖表、CSV）放對應主題的 `output/`；Word/PDF 報告放 `reports/`。
- 優先複用 `core/api/` 與 `core/utils/`，不在 lab 內重複實作資料讀取、手續費、交易日邏輯。
- 成熟策略最終搬到 `core/strategies/stock/` 或 `core/strategies/futures/`（**兩條分支**，
  依策略宣告的商品類別決定），用 `run.py --strategy <類別名>` 跑正式回測。

詳細說明、API 用法、工作流 → [`strategy_lab/README.md`](README.md)

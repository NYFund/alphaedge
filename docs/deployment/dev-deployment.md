# 開發部署（Dev Deployment）

本文件是本機的日常流程：啟用環境 → 更新資料 → 執行回測 → 檢視結果。
環境建置見 [開發環境設定](../setup/dev-setup.md)；每個指令的完整參數見 [指令教學](../commands/command-usage.zh-TW.md)。

## 1) 啟用環境

```bash
source .venv/bin/activate
```

不啟用的話改在指令前加 `uv run --no-sync`：不帶 `--no-sync` 的 `uv run` 會先同步環境，把裝過的 extra（`frontend`／`lab`）移除。

## 2) 更新資料庫

```bash
python -m apps.update_db                                  # 等同 --target no_tick：全部資料，不含 tick 與只能點名的 target
python -m apps.update_db --target chip price              # 只更新指定資料
python -m apps.update_db --target tick                    # 台股 tick（需執行中的 TimescaleDB、`tick` extra 與 Shioaji 金鑰）
python -m apps.update_db --retry-failed 4 --retry-wait 60 # 失敗的 target 每 60 分鐘只重跑失敗的那幾個，最多 4 輪
```

- 候選日期是差集，中間缺的日子會自動補回，平常不需要指定起日；要把起點往前拉時才用 `--from YYYY-MM-DD`。
- 任一 target 失敗不中斷其餘 target，但整批（含重試）跑完仍有失敗時以**結束碼 1** 收場，log 尾端列出失敗與成功的 target。
- `futures_stock_price` 與 FinMind 系列（`finmind`、`stock_info`、`stock_info_with_warrant`、`broker_info`、`broker_trading`）
  不含在 `all`／`no_tick` 內，要以 `--target` 明確點名才會跑；`all` 與 `no_tick` 的差別只在 `tick`。
- 完整 target 對照表見 `python -m apps.update_db --help`。
- **長跑的 ETL 進行中不要動 `logs/` 與 `data/db/`**。

## 3) 執行回測

策略名稱使用「類別名稱」：

```bash
python -m apps.backtest --strategy VolumeBreakoutMomentumStrategy                  # 台股做多動能
python -m apps.backtest --strategy InvestmentTrustMomentumSwingStrategy        # 台股投信認同強勢股短波段
python -m apps.backtest --strategy ForeignSellingReversalShortStrategy   # 台股外資大賣強勢股當沖放空
python -m apps.backtest --strategy IntradayMomentumStrategy           # 台股盤中動能（預設日 K 近似）
python -m apps.backtest --strategy MomentumFuturesStrategy            # 台指期動能（示範用，非交易邏輯）

# 覆寫回測區間與初始資金（皆選填，不帶時沿用策略預設）
python -m apps.backtest --strategy VolumeBreakoutMomentumStrategy --start 2024-01-01 --end 2024-12-31 --capital 500000

# 逐筆回測（只有 supported_scales 含 TICK 的策略可用；需 TimescaleDB 裡有 tick 資料）
python -m apps.backtest --strategy IntradayMomentumStrategy --scale TICK
```

其他旗標：`--tick-universe strategy|all`（TICK 回測的標的範圍，預設只取策略的 `get_live_symbols()` 加持倉）、
`--show`／`--no-show`（畫完圖要不要開瀏覽器，預設不開，也可用環境變數 `ALPHAEDGE_SHOW_FIGURES`）。

策略放在 `strategies/stock/` 與 `strategies/futures/`，由 `StrategyLoader` 自動收錄；
怎麼寫策略見 [策略開發指南](../../strategies/README.md)。

動到 `core/backtest/`、`core/position/`、`core/models/` 之後，先跑回歸雙線：

```bash
./scripts/run_regression.sh
```

## 4) 檢視結果

回測結果輸出在 `results/<StrategyName>/`，所有檔名都以策略名稱為前綴：

| 檔案 | 內容 |
|------|------|
| `<StrategyName>_trading_report.csv` | 已平倉交易的逐筆明細與損益 |
| `<StrategyName>_direction_summary.csv` | 多空分開的勝率、損益與成本統計 |
| `<StrategyName>_event_report.csv` | 強制回補、斷頭、拒單等事件計數 |
| `<StrategyName>_daily_equity.csv` | 含未實現損益的逐日權益序列 |
| `<StrategyName>_metrics_summary.csv` | Sharpe／Sortino／IR／MDD／波動度等整體指標（`Metric`／`Value`／`Note` 長表）|
| `<StrategyName>_balance_curve.png` | 資產曲線 |
| `<StrategyName>_networth.png` | 策略與 benchmark 淨值比較（台股為 `0050` 還原價，期貨為所交易商品的日盤連續合約） |
| `<StrategyName>_mdd.png` | 最大回撤 |
| `<StrategyName>_everyday_profit.png` | 每日損益 |
| `<StrategyName>_everyday_equity_change.png` | 每日權益變化 |

日誌落在 `logs/backtest/`。用前端瀏覽（需 `uv sync --extra frontend`）：

```bash
streamlit run frontend/app.py
```

不設環境變數時前端讀 `PROJECT_ROOT/results`，與回測輸出同一處。

用容器跑時**先跑回測、再開前端**，兩者沒有執行期相依：

```bash
docker compose up core        # 一次性批次容器，跑完就結束（會先帶起 postgres 並等它健康）
docker compose up -d frontend # 常駐 Web，只讀 volume 裡已落地的 CSV
```

容器版的回測結果寫在具名 volume，不在主機的 `results/`；細節見 [正式環境部署](prod-deployment.md)。

`frontend` 刻意**不宣告** `depends_on: [core]`——宣告了的話 `docker compose up frontend`
會順手跑一整場回測。結果目錄是空的時候前端會顯示「找不到報表」而不是當掉。

## 5) 常用維運指令

```bash
# 刪除指定日期的 price 資料（預設只預覽；加 --apply 才刪，會再互動確認，排程用時再加 --yes）
python -m apps.delete_price_data --date 2025-07-13

# 清理超過保留天數（預設 30 天）的日誌（預設只預覽；--apply 才刪，--days／--bucket 調範圍）
python -m apps.clean_logs
```

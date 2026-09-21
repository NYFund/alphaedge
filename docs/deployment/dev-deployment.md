# 開發部署（Dev Deployment）

本文件是本機的日常流程：啟用環境 → 更新資料 → 執行回測 → 檢視結果。
環境建置見 [開發環境設定](../setup/dev-setup.md)；每個指令的完整參數見 [指令教學](../commands/command-usage.zh-TW.md)。

## 1) 啟用環境

```bash
source .venv/bin/activate
```

## 2) 更新資料庫

```bash
python -m tasks.update_db                          # 全部資料（不含兩種 tick），等同 --target no_tick
python -m tasks.update_db --target chip price      # 只更新指定資料
python -m tasks.update_db --target tick            # 台股 tick（需 DolphinDB 與 Shioaji 金鑰）
```

- 候選日期是差集，中間缺的日子會自動補回，平常不需要指定起日。
- 任一 target 失敗不中斷其餘 target，但整批跑完會以**結束碼 1** 收場；請看 log 尾端的統計行。
- **長跑的 ETL 進行中不要動 `logs/` 與 `data/db/`**。

## 3) 執行回測

策略名稱使用「類別名稱」：

```bash
python run.py --strategy MomentumStrategy1                  # 台股做多動能
python run.py --strategy ForeignSellShortDayTradeStrategy   # 台股外資大賣強勢股當沖放空
python run.py --strategy MomentumFuturesStrategy            # 台指期動能（示範用，非交易邏輯）
```

策略放在 `core/strategies/stock/` 與 `core/strategies/futures/`，由 `StrategyLoader` 自動收錄；
怎麼寫策略見 [策略開發指南](../../core/strategies/README.md)。

動到 `core/backtest/`、`core/managers/`、`core/models/` 之後，先跑回歸雙線：

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
| `<StrategyName>_balance_curve.png` | 資產曲線 |
| `<StrategyName>_networth.png` | 策略與 benchmark（`0050`）淨值比較 |
| `<StrategyName>_mdd.png` | 最大回撤 |
| `<StrategyName>_everyday_profit.png` | 每日損益 |
| `<StrategyName>_everyday_equity_change.png` | 每日權益變化 |

日誌落在 `logs/backtest/`。用前端瀏覽（需 `uv sync --extra frontend`）：

```bash
streamlit run frontend/app.py
```

不設環境變數時前端讀 `PROJECT_ROOT/results`，與回測輸出同一處。

## 5) 常用維運指令

```bash
# 刪除指定日期的 price 資料（預設只預覽，加 --apply 才刪）
python -m tasks.delete_price_data --date 2025-07-13

# 清理已輪替的日誌（預設只預覽）
python -m tasks.clean_logs
```

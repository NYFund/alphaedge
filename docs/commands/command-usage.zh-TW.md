# 指令教學

本文件整理常用執行指令，包含資料更新（`tasks.update_db`）、資料維護與回測（`run.py`）。

## 資料更新：`python -m tasks.update_db`

### 功能說明

`tasks.update_db` 是資料更新系統入口，透過 `--target` 指定要更新的資料類型，可單一或多選。
未指定 `--target` 時，預設為 `no_tick`（更新全部資料，但不含**兩種** tick）。

### 參數

- `--target <target> [<target> ...]`：欲更新的資料類型，可指定一個或多個。
- `--from YYYY-MM-DD`：把起日往前拉（見下方說明）。

### Target 對照表

| 選項                        | 說明                            |
| ------------------------- | ----------------------------- |
| `tick`                    | 逐筆成交（Shioaji ticks）           |
| `chip`                    | 三大法人籌碼                        |
| `price`                   | 收盤價                           |
| `margin`                  | 信用交易（融資融券餘額）              |
| `dividend`                | 除權除息計算結果表（含還原係數、現金股利） |
| `corporate_action`        | 非除權息的公司行動（減資、分割、面額變更） |
| `fs`                      | 財報（Financial Statement，含逐檔查詢的權益變動表） |
| `mrr`                     | 月營收報表（Monthly Revenue Report） |
| `finmind`                 | 全部 FinMind（台股總覽 + 證券商 + 券商分點） |
| `stock_info`              | FinMind 台股總覽（不含權證）            |
| `stock_info_with_warrant` | FinMind 台股總覽（含權證）             |
| `broker_info`             | FinMind 證券商資訊                 |
| `broker_trading`          | FinMind 券商分點統計                |
| `futures_price`           | 台期貨每日行情（寫入 `tw_futures.db`；商品見 `FUTURES_TARGET_PRODUCTS`） |
| `futures_stock_universe`  | 股票期貨標的池（寫入 `tw_futures.db`；每次執行留下一份當日快照） |
| `futures_stock_price`     | 股票期貨行情（商品清單取自標的池，預設只爬流動性前 N 檔） |
| `futures_continuous`      | 台期貨連續合約（由 `futures_price_daily` 建出，不連網路） |
| `futures_margin`          | 台期貨保證金（變動序列，寫入 `tw_futures.db`） |
| `futures_chip`            | 台期貨籌碼（三大法人、大額交易人、選擇權 PCR） |
| `market_holiday`          | 市場開休市日期（TWSE 公告；每次重抓去年／今年／明年，寫入 `tw_stock.db`）。實盤盤前判定交易日的主來源 |
| `futures_tick`            | 台期貨逐筆成交（Shioaji → DolphinDB；需 `[tick]` 相依與金鑰）。**不含在 `all`／`no_tick` 內**：沒有續跑紀錄、重跑會重複寫入，只在明確點名時才跑 |
| `all`                     | 全部資料（含 tick；不含 `futures_tick` 與 `futures_stock_price`） |
| `no_tick`                 | 全部資料（不含 `tick` **與** `futures_tick`，預設）。兩者都需要 Shioaji 金鑰與 `[tick]` 選用相依，不排除的話，沒有金鑰的機器每晚都會以結束碼 1 收場 |

### 單一 target 範例

```bash
# 逐筆成交
python -m tasks.update_db --target tick

# 三大法人籌碼
python -m tasks.update_db --target chip

# 收盤價
python -m tasks.update_db --target price

# 信用交易（融資融券餘額）
python -m tasks.update_db --target margin

# 除權除息計算結果表（上市走證交所、上櫃走櫃買中心，皆為全歷史）
python -m tasks.update_db --target dividend

# 非除權息的公司行動（每次掃整個區間：這類事件是事後公告）
python -m tasks.update_db --target corporate_action

# 財報
# 權益變動表（equity_change）是逐檔查詢：一個年季約 2,000 次請求。
# 重跑只補差集（已入庫與已確認沒資料的公司都不會重打）。
# 資料形狀與已知限制見 docs/pipeline/equity-change.md
python -m tasks.update_db --target fs

# 月營收報表
python -m tasks.update_db --target mrr

# 全部 FinMind（台股總覽 + 證券商 + 券商分點）
python -m tasks.update_db --target finmind

# FinMind 台股總覽（不含權證）
python -m tasks.update_db --target stock_info

# FinMind 台股總覽（含權證）
python -m tasks.update_db --target stock_info_with_warrant

# FinMind 證券商資訊
python -m tasks.update_db --target broker_info

# FinMind 券商分點統計
python -m tasks.update_db --target broker_trading

# 台期貨每日行情（寫入 tw_futures.db，非 tw_stock.db）
# 一次只能查一個商品、日盤與夜盤要分開查，故請求數 = 商品數 × 2 × 交易日數；
# 起點為 DEFAULT_FUTURES_START_DATE（2015-01-01），單檔 TX 首次回補約 6,100 次請求。
python -m tasks.update_db --target futures_price

# 股票期貨標的池（寫入 tw_futures.db）
# 整份清單一次 GET 就結束，同一天重跑不會產生第二份快照。
# 來源沒有掛牌日／下市日欄位，兩者由快照序列差分推得，故建議每日更新——
# 快照愈稀疏，推出來的日期誤差愈大。
# 下游要取商品清單一律用 FuturesStockUniverseUpdater.get_active_products()，不要另外手寫清單。
python -m tasks.update_db --target futures_stock_universe

# 市場開休市日期（寫入 tw_stock.db 的 market_holiday 表）
# 一年一次請求，每次重抓去年、今年、明年並整年替換。明年的公告通常 12 月才出來，
# 那之前明年會被記為「尚未公告」跳過，屬正常；實盤查到未入庫年度的日期會拒絕啟動。
python -m tasks.update_db --target market_holiday

# 全部資料（含 tick）
python -m tasks.update_db --target all

# 全部資料（不含 tick，等同預設）
python -m tasks.update_db --target no_tick

# 預設（等同 no_tick）
python -m tasks.update_db
```

期貨的其他 target（連續合約、保證金、籌碼、tick）與回補注意事項見 [台期貨平台](../futures/tw-futures-platform.md)〈指令〉。

### 多個 target 組合範例

```bash
python -m tasks.update_db --target chip price
python -m tasks.update_db --target chip price tick
python -m tasks.update_db --target stock_info broker_trading
```

### `--from`：把起日往前拉

```bash
python -m tasks.update_db --target price --from 2013-01-01
```

**平常不需要用**：updater 的候選日期是「日曆 − 表內已有 − 已確認沒有資料」的
差集，中間缺的日子會自動被補回來。
`--from` 是給「要把起點拉到比預設更早」的情境用的，只影響以**日期**為單位的
target；`fs`／`mrr` 這種以年季／年月為單位的不受影響。

## 刪除單日行情：`python -m tasks.delete_price_data`

**預設只預覽不刪除**——打錯一個日期就少掉一整天、上千檔的收盤行情，
而且要重跑 ETL 才補得回來。

```bash
# 只報告會刪幾筆，不寫入
python -m tasks.delete_price_data --date 2025-07-13

# 實際刪除；會要求輸入完整日期做確認
python -m tasks.delete_price_data --date 2025-07-13 --apply

# 排程用：跳過互動確認
python -m tasks.delete_price_data --date 2025-07-13 --apply --yes
```

非互動環境（無 tty）若沒有 `--yes` 一律拒絕執行，不會默默刪掉。

## 清理已輪替的日誌：`python -m tasks.clean_logs`

```bash
python -m tasks.clean_logs                     # 預覽（不刪）
python -m tasks.clean_logs --apply             # 實際刪除，預設保留 30 天
python -m tasks.clean_logs --apply --bucket api --days 7
```

只刪檔名帶時間戳的已輪替檔，使用中的 `xxx.log` 一律保留。

## 回測：`python run.py --strategy <StrategyClassName>`

將 `<StrategyClassName>` 替換為你的策略類別名稱。

```bash
python run.py --strategy <StrategyClassName>
python run.py --strategy <StrategyClassName> --show   # 畫完圖在瀏覽器開起來
```

策略名稱找不到時以結束碼 2 結束。結果輸出在 `results/<StrategyName>/`。

實盤是另一條路徑（`--mode live`，必須帶 `--phase`），**它從不回傳 1**——`1` 保留給未預期的例外。
排程要攔的是下列各碼：

| 退出碼 | 意義 |
|:---:|---|
| 0 | 正常結束 |
| 1 | 未預期的例外 |
| 2 | 用法錯誤：策略名找不到，或 `--production` 沒帶 `--confirm-production` |
| 3 | 資料未更新到前一個交易日 |
| 4 | 對帳不一致，或以券商部位重建被拒絕 |
| 5 | kill switch 生效 |
| 6 | 上次結束時帳戶層交易模式非 NORMAL，本次未帶 `--resume-trading` |
| 7 | `--resync-from-broker` 只列出重建計畫、沒有寫入（未帶 `--confirm-resync`） |
| 143 | 收到 SIGTERM：已撤未成交單、寫完結束紀錄才離開 |

**`6` 要和 `4`、`5` 分開**：排程看到 4／5 是「今天剛出事」，看到 6 是「昨天出的事還沒有人處理」。
完整的實盤用法見[實盤部署與排程](../deployment/live-deployment.md)。

# Data Coverage — AlphaEdge 資料覆蓋範圍

本文件描述 `AlphaEdge` 程式碼中**實際可用**的資料來源與對應模組。
本地資料庫的實際內容可能與預設起日不同，一律以庫內最早一筆為準。

## 總覽

| 類別 | 來源 | 主要模組 | 儲存位置 | 起始日期 | 備註 |
| --- | --- | --- | --- | --- | --- |
| 股票日線 | TWSE／TPEX 每日收盤行情 | `StockPriceAPI` | SQLite `price` | 2013-01-01（`DEFAULT_PRICE_START_DATE`） | 回測常用 |
| 三大法人籌碼 | TWSE／TPEX | `StockChipAPI` | SQLite `chip` | 2013-01-01（`DEFAULT_CHIP_START_DATE`） | 回測／選股可用 |
| 除權除息 | **上市**：證交所 `TWT49U`；**上櫃**：櫃買中心 `bulletin/exDailyQ` | `StockDividendAPI` | SQLite `dividend` | 2013-01-01（`DEFAULT_DIVIDEND_START_DATE`） | 股價還原係數、現金股利、開盤競價基準 |
| 公司行動（減資、分割、面額變更） | **上市**：證交所 `TWTAUU`；**上櫃**：櫃買中心 `bulletin/revivt`；另有由行情偵測並人工確認的事件 | `StockDividendAPI`（併入還原係數） | SQLite `corporate_action` | 2013-01-01 | 見 [非除權息的公司行動](../pipeline/corporate-action.md) |
| 融資融券餘額 | **上市**：證交所 `MI_MARGN`；**上櫃**：櫃買中心 `margin/balance` | `StockMarginAPI` | SQLite `margin` | 2013-01-01（`DEFAULT_MARGIN_START_DATE`） | 券源檢核（`check_borrowable`）、券資比 |
| 月營收 | MOPS | `MonthlyRevenueReportAPI` | SQLite `monthly_revenue` | 2013-01（`DEFAULT_START_YEAR` 起、1 月） | 基本面可用 |
| 財報 | MOPS | `FinancialStatementAPI` | SQLite `balance_sheet`、`comprehensive_income`、`cash_flow`、`equity_change` | 2013 年第 1 季（`DEFAULT_START_YEAR`） | 權益變動表的資料形狀與限制見 [權益變動表](../pipeline/equity-change.md) |
| FinMind 參考資料 | FinMind API | `FinMindAPI` | SQLite FinMind 相關表 | 券商分點：`2021-06-30`（`FINMIND_BROKER_TRADING_START_DATE`）；台股總覽／證券商為 API 快照 | 股票、券商、分點 |
| 台期貨日線 | TAIFEX 每日行情頁（POST） | `FuturesPriceAPI` | SQLite `tw_futures.db` `futures_price_daily` | 2015-01-01（`DEFAULT_FUTURES_START_DATE`） | 日盤／夜盤分列存（`session` 欄位）；指數期貨 7 檔（`FUTURES_TARGET_PRODUCTS`）＋ 股票期貨（預設流動性前 20 檔） |
| 台期貨連續合約 | 由 `futures_price_daily` 衍生（不連網路） | `FuturesContinuousAPI` | SQLite `tw_futures.db` `futures_continuous` | 同上 | 3 種調整方式 × 3 種換月規則；每次整段重建 |
| 台期貨保證金 | TAIFEX 公告附件（CSV） | `FuturesMarginAPI` | SQLite `tw_futures.db` `futures_margin_history`（指數類每口金額）、`stock_futures_margin_rate_history`（股票類比例） | 2020-03（更早為掃描影像，見 [台期貨平台](../futures/tw-futures-platform.md)〈已知限制〉） | 變動序列，達門檻才有新列 |
| 台期貨籌碼 | TAIFEX 三大法人／大額交易人／選擇權 PCR | `FuturesChipAPI` | SQLite `tw_futures.db` `futures_institutional_chip`、`futures_large_trader`、`futures_put_call_ratio` | 三大法人：2023-09-04（來源只保留約三年）；其餘以庫內最早一筆為準 | `get_available()` 只回傳查詢日之前已公布者（避免前視） |
| 股票期貨標的池 | TAIFEX 標的證券一覽表（GET） | `FuturesStockUniverseAPI` | SQLite `tw_futures.db` `futures_stock_universe` | 2026-08-29（首份快照） | **快照序列**：來源無掛牌／下市日欄位，兩者由差分推得；商品清單取用一律走 `FuturesStockUniverseAPI.get_products()` |
| 市場開休市日期 | TWSE `holidaySchedule`（JSON，一年一次請求） | `MarketHolidayAPI` | SQLite `market_holiday` | 每次更新去年／今年／明年（明年的公告通常 12 月才出來） | 實盤盤前判定交易日的主來源；「開始交易日／最後交易日」等提醒列標為交易日（`is_trading_day=1`），其餘（含「市場無交易，僅辦理結算交割作業」）為休市；颱風等臨時停市不在表上 |
| Tick 逐筆 | Shioaji + DolphinDB | `StockTickAPI` | DolphinDB `tickDB` | 預設更新起日 `2024-05-10`（`TICK_UPDATE_START_DATE`）；Shioaji 可查區間約自 **2020-03-02** | 需 DDB 環境 |

## API 與資料表對照

| API 類別 | 檔案 | 後端 | 關鍵資料表／資料庫 |
| --- | --- | --- | --- |
| `StockPriceAPI` | `core/api/tw/stock_price_api.py` | SQLite | `price` |
| `StockChipAPI` | `core/api/tw/stock_chip_api.py` | SQLite | `chip` |
| `StockDividendAPI` | `core/api/tw/stock_dividend_api.py` | SQLite | `dividend`、`corporate_action` |
| `StockMarginAPI` | `core/api/tw/stock_margin_api.py` | SQLite | `margin` |
| `MonthlyRevenueReportAPI` | `core/api/tw/monthly_revenue_report_api.py` | SQLite | `monthly_revenue` |
| `FinancialStatementAPI` | `core/api/tw/financial_statement_api.py` | SQLite | `balance_sheet`、`comprehensive_income`、`cash_flow`、`equity_change` |
| `FinMindAPI` | `core/api/tw/finmind_api.py` | SQLite | `taiwan_stock_info`、`taiwan_stock_info_with_warrant`、`taiwan_securities_trader_info`、`taiwan_stock_trading_daily_report_secid_agg` |
| `StockTickAPI` | `core/api/tw/stock_tick_api.py` | DolphinDB | `tickDB` / `tick` |
| `FuturesPriceAPI` | `core/api/tw/futures_price_api.py` | SQLite | `futures_price_daily`（`tw_futures.db`） |
| `FuturesContinuousAPI` | `core/api/tw/futures_continuous_api.py` | SQLite | `futures_continuous`（`tw_futures.db`） |
| `FuturesMarginAPI` | `core/api/tw/futures_margin_api.py` | SQLite | `futures_margin_history`、`stock_futures_margin_rate_history` |
| `FuturesChipAPI` | `core/api/tw/futures_chip_api.py` | SQLite | `futures_institutional_chip`、`futures_large_trader`、`futures_put_call_ratio` |
| `FuturesStockUniverseAPI` | `core/api/tw/futures_stock_universe_api.py` | SQLite | `futures_stock_universe` |
| `MarketHolidayAPI` | `core/api/tw/market_holiday_api.py` | SQLite | `market_holiday` |

起始日期常數皆定義於 `core/config/settings.py`。

## 更新入口

資料更新統一入口：

```bash
python -m tasks.update_db --target <targets...>
```

各 target 的預設起始（與 `get_update_time_config`／各 updater 行為一致）：

| `--target` | 預設起始日期 |
| --- | --- |
| `price` | 2013-01-01 |
| `chip` | 2013-01-01 |
| `dividend` | 2013-01-01 |
| `corporate_action` | 2013-01-01；每次掃整個區間（事件是事後公告） |
| `margin` | 2013-01-01（`DEFAULT_MARGIN_START_DATE`） |
| `fs` | 2013 年第 1 季；`equity_change` 為逐檔查詢，整段回補以十萬次請求計 |
| `mrr` | 2013 年 1 月 |
| `tick` | 2024-05-10（`TICK_UPDATE_START_DATE`）；Shioaji 可查約自 2020-03-02 |
| `finmind` | `broker_trading` 區間自 2021-06-30；其餘子項見下 |
| `stock_info` | 快照更新，無日期區間 |
| `stock_info_with_warrant` | 快照更新，無日期區間 |
| `broker_info` | 快照更新，無日期區間 |
| `broker_trading` | 2021-06-30 |
| `futures_price` | 2015-01-01（`DEFAULT_FUTURES_START_DATE`）；寫入 `tw_futures.db` |
| `futures_stock_universe` | 快照更新，無日期區間；同一天重跑不產生第二份快照 |
| `futures_stock_price` | 2015-01-01；商品清單取自標的池、預設流動性前 20 檔（`STOCK_FUTURES_TOP_N`） |
| `futures_continuous` | 無區間；由 `futures_price_daily` 整段重建 |
| `futures_margin` | 快照更新（現行一覽表），沒調整時不新增列 |
| `futures_chip` | 各表最新日 +1（盤後公布，盤中跑到「無資料」屬正常） |
| `market_holiday` | 無區間；固定抓去年／今年／明年，每年整年替換；尚未公告的年度跳過 |
| `futures_tick` | 2015-01-01；需 `[tick]` 相依與 Shioaji 金鑰，DolphinDB 寫入路徑未實測 |

完整參數與範例見 [指令教學](../commands/command-usage.zh-TW.md)。

## 股價還原（除權息與公司行動調整）

回測的**訊號計算預設使用還原價**（後復權），成交與成本一律使用原始價。

### 用途界線（取錯會直接算錯錢）

| 用途 | 該用哪個 | 取值方式 |
|------|----------|----------|
| 策略訊號（漲跌幅、均線、動能） | **還原價** | `StockQuote.signal_close`、`BaseStockStrategy.get_signal_close_map()`、`StockPriceAPI.get_adjusted_close_*()` |
| 成交價、手續費、證交稅 | **原始價** | `StockQuote.close`、`StockPriceAPI.get_close_map()` |
| 漲跌停與價格檔位判定 | **原始價** | 同上；除權息日的基準改用 `dividend` 表的**開盤競價基準** |

**「今日價走 quote、昨日價走 API」是兩條不同的路徑**，只還原一邊會比完全不還原更糟且不會報錯。
策略請一律以 `get_signal_close_map()` 取昨日價，它會依引擎的還原模式自動配對。

### 還原方式

- **後復權**：以最早日為基準，歷史價格不變、事件之後的價格往回調。
  係數 = Π（事件前收盤價 / 事件參考價），取所有「事件日 ≤ 查詢日」；除權息取自 `dividend.還原係數`，
  減資與分割取自 `corporate_action.調整倍率`，兩者語意一致，同一套累乘直接吃得下。
- **查詢時即時還原**，不在 `price` 表落地還原欄位——避免出現「還原價與原始價不同步」
  且不會報錯的資料腐化。
- **不用前復權**：前復權會讓同一個歷史日期的價格隨每次新事件而改變，回歸 baseline 會自動失效。

### 開關

`build_backtester(strategy, adjusted_price=True)`，預設啟用。設為 `False` 則訊號直接使用原始價。

## 已知限制

- 專案目前無內建加密貨幣 / prediction market collectors。
- **`equity_change`（權益變動表）的形狀與其他三張財報表不同**：長表、單位仟元。
  爬取清單取自 `taiwan_stock_info` 現況，**不含已下市公司與興櫃**（倖存者偏誤來源）；
  另有少數公司的 2013~2014 年季站方導向「採 IFRSs 前」的另一組端點、本表沒有。
  詳見 [權益變動表](../pipeline/equity-change.md)〈已知限制〉。
- **`balance_sheet`、`comprehensive_income`、`cash_flow` 的主鍵含 `公司名稱`**：來源在公司名稱後加註 `*`，
  或公司更名後重爬，同一檔同一年季就會多一列（例如 `5904` 的 `寶雅`／`寶雅*`）。以 `stock_id` 聚合的查詢需自行去重；
  改主鍵屬 schema 變更，排在 [PostgreSQL 遷移計畫](../../backlog/PostgreSQL遷移計畫.md) 評估。
- `tick` 依賴 DolphinDB 環境與對應連線參數，未設定時無法使用 tick 相關流程。

### 指數期貨日行情

| 商品 | 日盤起始 | 夜盤起始 |
|------|----------|----------|
| TX | 2015-01-05（本專案回補起點） | 2017-05-16 |
| MTX | 2015-01-05 | 2017-05-16 |
| TE | 2015-01-05 | 2018-11-20 |
| TF | 2015-01-05 | **2025-06-24** |
| ZEF | 2021-06-28（上市日） | 2021-06-29 |
| ZFF | 2021-12-06（上市日） | **2025-06-24** |
| TMF | 2024-07-29（上市日） | 2024-07-30 |

- **夜盤的涵蓋率遠低於日盤，是來源如此，不是漏抓**：各商品被納入盤後交易的時間不同。
  crawler 對每個日期都會打日盤、夜盤兩次，空結果只記 `no data` 而**不落檔**。
  **要用夜盤資料的策略必須先確認該商品的夜盤起始日**，否則會把「制度上還沒有夜盤」
  誤讀成「當天沒有夜盤成交」。
- 日盤出現超過 5 天的斷點時，基準日曆上同樣沒有交易日（農曆年、清明等連假）。
- **`收盤價 IS NULL` 恆等於「該契約當天零成交」**。無成交的契約**不寫 0 元**——0 元會被下游當成真實價格。
  冷門遠月契約的 NULL 占比高，屬正常。
- **`expiry` 格式**為 `YYYYMM`（月契約）或 `YYYYMMWn`（週契約）。

**兩種 `結算價 IS NULL` 都是來源特性，不是缺漏**：

1. **夜盤全部為 NULL**——結算價由日盤決定，盤後交易時段沒有這個欄位。
2. **日盤每月一筆**，落在**該月契約的最後交易日**。到期契約當天的最後結算價由**特別結算價**
   另行公布，每日行情頁給「-」。日期會隨連假順延。
   回測端的 `TwFuturesSettlementModel.get_quote_mark_price()` 取價順序為「結算價 → 收盤價」，到期日自動退回收盤價。

### 股價還原的已知限制

- **`tick` 不做還原**：tick 為當日盤中資料，跨日還原無意義。日內策略若需跨日比較，須自行處理。
- **公司行動只涵蓋減資端點與偵測到的分割**：合併換股、私募、股權轉換、股票代號變更、下市
  **不在** `corporate_action` 內，這些事件的價格不連續仍會留在還原序列裡。
- **上市的現金股利在「權息並存」時為 `NULL`**：證交所 `TWT49U` 只提供「權值+息值」合計，
  其「減除股利參考價」欄在權息並存時語意不一致，無法可靠拆分。**上櫃不受影響**——櫃買中心直接提供
  `現金股利` 與 `每仟股無償配股`。需要現金股利的用途（如放空的股利補償）遇到 `NULL` 時不猜 0。
  **還原係數不受此限制影響**，它只用到前收盤價與參考價。
- **查無資料與「無事件」不可區分**：`get_cumulative_factor()` 兩種情況都回傳 `1.0`。
  若某段期間的 `dividend`／`corporate_action` 資料未更新，該期間會安靜地使用未還原價格。
  更新資料後請確認兩張表的日期涵蓋整個回測區間。

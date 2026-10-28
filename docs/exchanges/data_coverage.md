# Data Coverage — AlphaEdge 資料覆蓋範圍

本文件描述 `AlphaEdge` 程式碼中**實際可用**的資料：每張表從哪裡抓、用哪個類別讀、從哪天開始有資料。

## 資料表一覽

每張表從哪裡抓、用哪個類別讀，一張表看完。**Crawler** 在 `core/pipeline/tw/crawlers/`、**API** 在 `core/api/tw/`；
更新指令是 `python -m apps.update_db --target <target>`。

**`data/db/tw_stock.db`（台股）**

| 資料表 | 內容 | `--target` | Crawler | API |
| --- | --- | --- | --- | --- |
| `price` | 每日收盤行情（日 K） | `price` | `StockPriceCrawler` | `StockPriceAPI` |
| `chip` | 三大法人買賣超 | `chip` | `StockChipCrawler` | `StockChipAPI` |
| `margin` | 融資融券餘額 | `margin` | `StockMarginCrawler` | `StockMarginAPI` |
| `short_sale_list` | 平盤下得融（借）券賣出名單 | `short_sale_list` | `StockTradingListCrawler` | `StockShortSaleListAPI` |
| `day_trade_list` | 現股當沖標的名單 | `day_trade_list` | `StockTradingListCrawler` | `StockDayTradeListAPI` |
| `dividend` | 除權除息（還原係數、現金股利） | `dividend` | `StockDividendCrawler` | `StockDividendAPI` |
| `corporate_action` | 減資、分割、面額變更 | `corporate_action` | `CorporateActionCrawler` | `StockDividendAPI`（併入還原係數） |
| `monthly_revenue` | 月營收 | `mrr` | `MonthlyRevenueReportCrawler` | `MonthlyRevenueReportAPI` |
| `balance_sheet`、`comprehensive_income`、`cash_flow`、`equity_change` | 財報四表 | `fs` | `FinancialStatementCrawler` | `FinancialStatementAPI` |
| `market_holiday` | 開休市日曆 | `market_holiday` | `MarketHolidayCrawler` | `MarketHolidayAPI` |
| `taiwan_stock_info`、`taiwan_stock_info_with_warrant` | 台股總覽（不含／含權證） | `stock_info`、`stock_info_with_warrant`* | `FinMindCrawler` | `FinMindAPI` |
| `taiwan_securities_trader_info` | 證券商資訊 | `broker_info`* | `FinMindCrawler` | `FinMindAPI` |
| `taiwan_stock_trading_daily_report_secid_agg` | 券商分點進出 | `broker_trading`* | `FinMindCrawler` | `FinMindAPI` |

**`data/db/tw_futures.db`（台期貨）**

| 資料表 | 內容 | `--target` | Crawler | API |
| --- | --- | --- | --- | --- |
| `futures_price_daily` | 各月份契約日 K（日盤、夜盤分列） | `futures_price`；股期另跑 `futures_stock_price`* | `FuturesPriceCrawler` | `FuturesPriceAPI` |
| `futures_continuous` | 連續合約（換月接續） | `futures_continuous` | 無（由 `futures_price_daily` 建出） | `FuturesContinuousAPI` |
| `futures_institutional_chip`、`futures_large_trader`、`futures_put_call_ratio` | 三大法人、大額交易人、選擇權 PCR | `futures_chip` | `FuturesChipCrawler` | `FuturesChipAPI` |
| `futures_margin_history`、`stock_futures_margin_rate_history` | 保證金（指數類每口金額、股票類比例） | `futures_margin` | `FuturesMarginCrawler` | `FuturesMarginAPI` |
| `futures_stock_universe` | 股票期貨標的池（快照） | `futures_stock_universe` | `FuturesStockUniverseCrawler` | `FuturesStockUniverseAPI` |

**TimescaleDB（台股 tick，需 `docker compose up -d postgres`）**

| 資料表 | 內容 | `--target` | Crawler | API |
| --- | --- | --- | --- | --- |
| `stock_tick`、`stock_tick_load_log` | 逐筆成交、各「股票 × 交易日」的入庫紀錄 | `tick` | `StockTickCrawler` | `StockTickAPI` |

**`data/db/tw_trading.db`（實盤紀錄庫）**：不由 `update_db` 更新，由實盤引擎寫入、以 `LiveTradeDAO` 讀寫，
共 11 張 `live_*` 表（啟動紀錄、委託、成交、部位歸屬、快照、風控事件、待辦、parity 等）。

> \* 標 `*` 的 target 只在明確點名時才跑，`all`／`no_tick` 不含（FinMind 帳號權限不足、股期行情缺口，見下方〈更新入口〉）。
> 每張表的來源網址與起始日期見下一節〈資料來源與起始日期〉。

## 資料來源與起始日期

各表的資料來源與預設起始日期（常數除另行註明者外皆定義於 `core/config/settings.py`）。本地資料庫的實際內容可能與預設起日不同，一律以庫內最早一筆為準。

| 資料表 | 來源 | 起始日期 | 備註 |
| --- | --- | --- | --- |
| `price` | TWSE／TPEX 每日收盤行情 | 2013-01-01（`DEFAULT_PRICE_START_DATE`） | 回測常用 |
| `chip` | TWSE／TPEX | 2013-01-01（`DEFAULT_CHIP_START_DATE`） | 回測／選股可用 |
| `dividend` | **上市**：證交所 `TWT49U`；**上櫃**：櫃買中心 `bulletin/exDailyQ` | 2013-01-01（`DEFAULT_DIVIDEND_START_DATE`） | 股價還原係數、現金股利、開盤競價基準 |
| `corporate_action` | **上市**：證交所 `TWTAUU`；**上櫃**：櫃買中心 `bulletin/revivt`；另有由行情偵測並人工確認的事件 | 2013-01-01 | 見 [非除權息的公司行動](../pipeline/corporate-action.md) |
| `margin` | **上市**：證交所 `MI_MARGN`；**上櫃**：櫃買中心 `margin/balance` | 2013-01-01（`DEFAULT_MARGIN_START_DATE`） | 券源檢核（`TwStockFillModel.check_short_borrowable()`）、券資比 |
| `short_sale_list` | **上市**：證交所 `TWT92U`；**上櫃**：櫃買中心 `margin/mark` | 2013-09-23（`SHORT_SALE_LIST_START_DATE`；更早來源無資料） | 回測放空檢核（`ShortConstraint.check_short_sale_list`，預設關閉）；含暫停融券／暫停借券／禁止平盤下三個註記；來源損毀的市場日（`SHORT_SALE_LIST_CORRUPTED_DAYS`）以前後交易日推估 |
| `day_trade_list` | **上市**：證交所 `TWTB4U`；**上櫃**：櫃買中心 `intraday/list` | 2014-01-06（`DAY_TRADE_LIST_START_DATE`）；先賣後買自 2014-06-30（`SELL_FIRST_DAY_TRADE_START_DATE`） | 回測現股當沖先賣檢核（`ShortConstraint.check_day_trade_list`，預設關閉）；含暫停先賣後買註記 |
| `monthly_revenue` | MOPS | 2013-01（`DEFAULT_START_YEAR` 起、1 月） | 基本面可用 |
| `balance_sheet`、`comprehensive_income`、`cash_flow`、`equity_change` | MOPS | 2013 年第 1 季（`DEFAULT_START_YEAR`） | 權益變動表的資料形狀與限制見 [權益變動表](../pipeline/equity-change.md) |
| FinMind 四表 | FinMind API | 券商分點：`2021-06-30`（`FINMIND_BROKER_TRADING_START_DATE`）；台股總覽／證券商為 API 快照 | 股票、券商、分點 |
| `futures_price_daily` | TAIFEX 每日行情頁（POST） | 2015-01-01（`DEFAULT_FUTURES_START_DATE`） | 日盤／夜盤分列存（`session` 欄位）；指數期貨 7 檔（`FUTURES_TARGET_PRODUCTS`）＋ 股票期貨（預設流動性前 20 檔） |
| `futures_continuous` | 由 `futures_price_daily` 衍生（不連網路） | 同上 | 3 種調整方式 × 換月規則（`--target futures_continuous` 預設只建日盤 ＋ `LAST_TRADING_DAY`）；每次整段重建 |
| `futures_margin_history`、`stock_futures_margin_rate_history` | TAIFEX 保證金一覽表（CSV，日常更新）；2020-03 起的歷史取自調整公告附件（`FuturesMarginUpdater.update_history()`，不在 `update_db` 內） | 2020-03（更早為掃描影像，見 [台期貨平台](../futures/tw-futures-platform.md)〈已知限制〉） | 變動序列，達門檻才有新列 |
| `futures_institutional_chip`、`futures_large_trader`、`futures_put_call_ratio` | TAIFEX 三大法人／大額交易人／選擇權 PCR | 三大法人：2023-09-04（來源只保留約三年）；大額交易人、PCR：2015-01-01（`FuturesChipUpdater.DEFAULT_START_DATE`） | `get_available()` 只回傳查詢日之前已公布者（避免前視）；三張表各自以交易日曆的差集決定要請求的月份，盤後才公布 |
| `futures_stock_universe` | TAIFEX 標的證券一覽表（GET） | 2026-08-29（首份快照） | **快照序列**：來源無掛牌／下市日欄位，兩者由差分推得；商品清單取用一律走 `FuturesStockUniverseAPI.get_products()` |
| `market_holiday` | TWSE `holidaySchedule`（JSON，一年一次請求） | 每次更新去年／今年／明年（明年的公告通常 12 月才出來） | 實盤盤前判定交易日的主來源；「開始交易日／最後交易日」等提醒列標為交易日（`is_trading_day=1`），其餘（含「市場無交易，僅辦理結算交割作業」）為休市；颱風等臨時停市不在表上，改登記在 `market_adhoc_closure` |
| `market_adhoc_closure` | 人工登記（`scripts/manual/manual_market_closure.py`） | 有登記才有列 | 颱風等臨時休市；查休市日時與 `market_holiday` 合併。**ETL 不建也不寫**，不受 `market_holiday` 整年替換影響 |
| `stock_tick` | Shioaji（每日更新）＋歷史 CSV（一次匯入） | 歷史存檔 2020-04-01～2024-05-10（`scripts/manual/manual_tick_history_import.py` 匯入後才有資料）；Shioaji 可查區間約自 **2020-03-02**；每日更新自 2024-05-10（`TICK_UPDATE_START_DATE`）起接續 | 需執行中的 TimescaleDB（開 Docker Desktop 後 `docker compose up -d postgres`）；興櫃時期不入庫 |

## 更新入口

資料更新統一入口：

```bash
python -m apps.update_db --target <targets...>
```

不帶 `--target` 時等同 `--target no_tick`。各 target 註冊在 `apps/update_db.py` 的 `TARGETS`，
執行順序固定為註冊表順序（與點名順序無關）。

- `all`／`no_tick` 兩個捷徑**都不含** `futures_stock_price` 與 `finmind`（含 `stock_info`、`stock_info_with_warrant`、
  `broker_info`、`broker_trading` 四個子項）；這些 target 標為 `explicit_only`，只在明確點名時才跑。
- `no_tick` 另外排除 `tick`（需要 Shioaji 金鑰、`[tick]` 選用相依與執行中的 TimescaleDB）。
- `--from YYYY-MM-DD` 可覆寫以日期為單位的 target 的起日（`fs`、`mrr` 與自行決定區間的 `futures_chip`、`futures_continuous`、
  `futures_margin`、`futures_stock_universe`、`market_holiday`、`stock_info`、`stock_info_with_warrant`、`broker_info` 不受影響）。
- `--retry-failed N` 讓失敗的 target 最多再重跑 N 輪（只重跑失敗的那幾個），每輪前等 `--retry-wait` 分鐘（預設 60）。

各 target 的起始日期見上方〈資料來源與起始日期〉；請求量、回補時間與各 target 的更新行為見 [指令教學](../commands/command-usage.zh-TW.md)〈各 target 的注意事項〉。

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
  爬取清單是 `taiwan_stock_info` 現況聯集當季資產負債表的公司，**不含興櫃**；已下市公司只補得到
  當季資產負債表有的那些（倖存者偏誤的殘餘來源）；
  另有少數公司的 2013~2014 年季站方導向「採 IFRSs 前」的另一組端點、本表沒有。
  詳見 [權益變動表](../pipeline/equity-change.md)〈已知限制〉。
- **`balance_sheet`、`comprehensive_income`、`cash_flow` 的主鍵含 `公司名稱`**：來源在公司名稱後加註 `*`，
  或公司更名後重爬，同一檔同一年季就會多一列（例如 `5904` 的 `寶雅`／`寶雅*`）。以 `stock_id` 聚合的查詢需自行去重；
  改主鍵屬 schema 變更，尚未處理。
- `tick` 依賴執行中的 TimescaleDB 與 `TICK_DATABASE_URL`，未設定時無法使用 tick 相關流程（連不上時錯誤訊息會說明啟動方式）。
  `stock_tick` 沒有主鍵，冪等靠「整天刪除後重寫 ＋ `stock_tick_load_log`」；儲存與匯入設計見 [ETL 入庫約定](../pipeline/etl-ingestion.md)〈台股 tick〉。

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

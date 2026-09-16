# ETL 入庫約定

> 本文件描述 `core/pipeline/` **入庫階段**的現行約定：分批時機、冪等性、失敗語意與結束碼。
>
> **各項設計的理由寫在程式碼的 docstring**（`core.dao.base.insert_or_ignore()` / `BaseDAO.savepoint()`、
> `BaseDataLoader.finish_load()` / `select_csv_files()`、`DataLoadError`、`tasks.update_db.target_guard()`、
> `core/pipeline/shared/date_planner.py`）。本文件只放**跨檔案的全貌**與新增 updater 時的檢查表。
>
> 連線、交易與 SQL 一律在 `core/dao/`，loader／updater 不寫 SQL；連線所有權與 commit 時點的全貌見
> [資料存取層（DAO）](../dev/data-access-layer.md)。

---

## 一、四層流程與入庫時機

pipeline 為 crawler → cleaner → loader → updater 四層：crawler 回傳 DataFrame、
cleaner 落地成 `downloads/<source>/{market}_{YYYYMMDD}.csv`、loader 把 CSV 寫進資料庫、
updater 負責串起流程與決定要處理哪些日期。

**入庫時機是這一層最關鍵的設計選擇。** 「整段日期全部爬完才一次 `add_to_db()`」
會讓中斷成本等於全部重來——已爬了幾千個 CSV，程序一中斷，資料庫仍是 0 列。

高風險的三個來源（price／chip／margin）因此**每 100 天入庫一次**
（`LOAD_BATCH_SIZE`），中斷最多只損失最後一批。

---

## 二、各 updater 對照

新增 updater 時對照本表，確認四個欄位都有著落。

| Updater | 入庫時機 | Resume 依據 | 重載防護 | 失敗可見度 |
|---------|----------|-------------|----------|-----------|
| `StockPriceUpdater` | **每 100 天** | **差集**（見下方說明） | `INSERT OR IGNORE` | `DataLoadError` |
| `StockChipUpdater` | **每 100 天** | **差集**（日曆取自 `price` 表） | `INSERT OR IGNORE` | `DataLoadError` |
| `StockMarginUpdater` | **每 100 天** | **差集**（日曆取自 `price` 表） | `INSERT OR IGNORE` | `DataLoadError` |
| `StockDividendUpdater` | 全部跑完 | **每次都掃整個區間**（一年一次請求） | `INSERT OR REPLACE` | `DataLoadError` |
| `CorporateActionUpdater` | 全部跑完 | **每次都掃整個區間**（事件是事後公告） | `INSERT OR REPLACE` | `DataLoadError` |
| `MonthlyRevenueReportUpdater` | 全部跑完 | 年 × 月的差集 | `INSERT OR IGNORE` | `DataLoadError` |
| `FinancialStatementUpdater`（前三張報表） | 每種報表一次 | 年 × 季的差集 | `INSERT OR IGNORE` | `DataLoadError` |
| `FinancialStatementUpdater`（equity_change） | **每 100 檔** ＋ 收到中止訊號時 | **差集**（表內已有 ＋ `SeasonProgressStore`） | `INSERT OR IGNORE` | `DataLoadError`（整段跑完才拋） |
| `FinMindUpdater`（broker_trading） | 逐組合、每 50 組 commit | metadata ＋ DB | `INSERT OR IGNORE` | `DataLoadError` |
| `StockTickUpdater` | 全部跑完 | 固定起日 ＋ `tick_metadata.json` | **無**（`keepDuplicates=ALL`） | `DataLoadError` |
| `FuturesPriceUpdater` | **每 100 天** | 逐**商品**查該商品在表內的最新 `date` +1 | `INSERT OR IGNORE` | `DataLoadError` |
| `FuturesStockUniverseUpdater` | 一次（單次請求） | 當日快照是否已入庫 | `INSERT OR IGNORE` | `DataLoadError` |
| `FuturesPriceUpdater.update_stock_futures()`（股期） | **每 100 天** | 逐商品最新 `date` +1；商品清單取自標的池前 N 檔 | `INSERT OR IGNORE` | `DataLoadError` |
| `FuturesMarginUpdater` | 一次（單次請求） | 主鍵 `(effective_date, product)` 相同即略過 | `INSERT OR IGNORE` | `DataLoadError` |
| `FuturesContinuousUpdater` | 每組（商品, 換月規則）寫完 commit | 無 resume（逆向調整量會隨後續換月改變，一律重建） | `INSERT OR REPLACE` | `DataLoadError`（有行情卻排不出換月表時） |
| `FuturesChipUpdater` | 每個月批次寫完 commit | 三張表各自最新 `date` +1 | `INSERT OR IGNORE` | `DataLoadError`（該有資料卻沒拿到時） |
| `FuturesTickUpdater` | 全部跑完 | 以日線行情表決定契約、預設只爬近月 | **無**（DolphinDB `keepDuplicates=ALL`，寫入路徑尚未實測） | `DataLoadError` |

**未分批的幾個並非疏漏**：dividend／mrr／fs 的量級是十餘年 × 數十個年月或年季，
單次執行以分鐘計，中斷重跑的成本可接受。tick 走 DolphinDB，語意與 SQLite 組不同。

### Resume 為什麼是「差集」而不是 `MAX(date) + 1`

台股三支日更 updater 的候選日期是：

    候選 ＝ 日曆 − 表內已有的日期 − 已確認沒有資料的日期 ＋ 上次沒跑完的日期

`MAX(date) + 1` 的問題是**中間缺的日子永遠不會再被嘗試**：某天因為連線失敗
沒抓到，隔天照樣從新的 `MAX(date)+1` 起跑，那個洞就留在資料庫裡；
而回測遇到缺日會當成休市靜默跳過。

最後一項尤其關鍵：price／chip／margin 每天都打**上市與上櫃兩次**請求。
任一邊沒有完整取得時整天不入庫（§3.5），但表內仍可能已有該日的部分資料
（例如早期寫入的半份批次）——差集會把這天當成「已經有了」而排除，
**缺的那半永遠補不回來**。故失敗的日期另外記在 `DateProgressStore` 的
`incomplete` 集合，用它把「表內已有」的排除翻回去。

**只有兩個市場都明確回覆「查無資料」才會寫進 `no_data`**（見 `CrawlResult`）；
連線失敗、被擋、版面解析不出來都不會，那些日子下次還會再試。
且**當天（含未來）一律不寫入** `no_data`——`NO_DATA` 同時代表「休市」與
「盤後尚未公布」，盤中跑一次就把今天寫進永久名單的話，收盤後那天的資料
再也不會被抓。

### `equity_change` 是 fs 裡的例外

MOPS 的權益變動表端點（`ajax_t164sb06`）是**逐檔查詢**，一個年季就要打兩千多次請求，
整段回補以十萬次計——量級跟 price／chip／margin 同一等級，所以入庫時機與 resume 依據
都得比照它們，而不是比照同一支 updater 裡的另外三張報表。
Resume 尤其不能用「表最大年季 +1」：一個年季爬到一半中斷時，該年季已經有資料，
會被判定為已完成而整季跳過，沒爬到的公司永遠補不回來。

**它也是唯一「中斷是常態」的來源**，故另外有三件逐日來源不需要的機制，
實作見 `core/pipeline/shared/graceful_stop.py` 與 `season_planner.py` 的模組說明：

| 機制 | 沒有它會發生什麼 |
|------|------------------|
| `GracefulStop`：訊號只立旗標，本檔跑完才收工 | `KeyboardInterrupt` 從當下那行炸出去，記憶體裡等湊滿一批的最多 100 檔全部作廢 |
| `SeasonProgressStore`：「年季 × 個股」的 `no_data`／`incomplete` | 查無資料的公司在表裡不留列，與「還沒爬」無法區分，每次重跑重打 |
| 磁碟 CSV 對帳 | 「CSV 已落地、尚未入庫」時被中止，那批的請求成本白付 |

`no_data` 的寫入條件是**該年季的申報期已關閉**（各行業最晚期限 ＋ 30 天寬限），
與逐日來源「當天（含未來）不寫入」同源：財報逐家公司申報，申報期間的「查無資料」
多半只代表那家還沒送件，這時寫進永久名單，它送件之後再也不會被抓。

### 券商分點（`broker_trading`）：不寫 CSV、不並行

券商分點是 FinMind 四個資料集裡唯一逐 **(券商 × 股票)** 組合請求的，組合數以百萬計。

**批量更新清洗後直接入庫，不寫 `broker_trading/{broker_id}/{stock_id}.csv`**：
resume 依據是 DB ＋ metadata，用不到 CSV；而寫 CSV 要把同組合的舊檔整份讀進來合併再寫回，
每個組合都付一次檔案 I/O。**已知限制**：既有的 CSV 因此只涵蓋當初仍寫 CSV 時期的資料，
不能拿來重建 DB，DB 才是券商分點的唯一來源。
`FinMindCleaner.clean_broker_trading_daily_report()` 預設仍寫 CSV，單獨呼叫時行為不變。

**刻意不做並行爬取與跨組合批次寫入**：瓶頸是 FinMind 每小時的 API quota，
不是網路延遲——循序執行時就已經會把 quota 用完（`wait_for_quota_reset()` 被觸發），
並行只會更早撞到上限再一起等，總耗時幾乎不變；SQLite 寫入也不是瓶頸。
並行還要付出的代價：SQLite 連線不能跨 thread 寫入、FinMind 的
`api_usage`／`api_usage_limit` 每讀一次就是一個 HTTP 請求（不適合當每次請求前的閘門）、
quota 耗盡瞬間多個在途組合要重試。**真正能縮短回補時間的是減少請求數**——
例如已確認無資料的區間不再重打、或只回補主力券商。

### 期貨線

**期貨的 updater 寫的是 `tw_futures.db` 不是 `tw_stock.db`**（主鍵語意不同，見
`futures_price_loader` 的說明）。`FuturesPriceUpdater` 的 resume **以商品為單位而非
全表最新日**：各商品上市日不同、且會陸續加進爬取範圍，用全表最新日會讓新加的商品
被既有商品的進度擋住而整段歷史都補不到。`FuturesStockUniverseUpdater` 則沒有回補
區間——來源是一張當下的完整清單，一次請求就結束，故「resume」退化成「今天抓過沒有」。

**`StockTickUpdater` 是唯一沒有重載防護的**：DolphinDB 建表時
`keepDuplicates=ALL` 是 tick 語意的刻意選擇（同一時間戳可以有多筆成交），
代價是同一批 CSV 重複 load 會產生重複 tick，需由入庫流程自行把關。

---

## 三、必須守住的性質

### 3.1 冪等：重跑不得產生重複列，也不得被誤判為失敗

loader **每次都掃整個 `downloads/` 目錄**，已入庫的檔案必然會再被送一次。
三種結果要分清楚，否則「重跑」會被當成「出錯」：

| 結果 | 判定 | 處置 |
|------|------|------|
| 整檔 0 列寫入、全部跳過 | 重跑，正常 | 計入「已存在跳過」，不出聲 |
| 部分寫入、部分跳過 | 同鍵不同值，資料可能有衝突 | `logger.warning` 列出檔名 |
| 拋出例外 | 欄位不符、檔案損毀 | 計入失敗，最終讓行程非零結束 |

分批入庫讓「重載已入庫檔案」從偶發變成**每批都會發生**，所以分批與冪等必須成對——
只做分批不做冪等，每批都會撞鍵。

**用 DAO 的 `insert_or_ignore()`，不要用 `to_sql(append)`**：後者整批送出，一列撞鍵就整檔失敗，
其餘幾百列跟著沒進資料庫；而且 pandas 的 `to_sql` 寫完會**自行 commit**，呼叫端無從控制交易。

### 3.1.1 單檔失敗整檔不留、一次執行一條連線

- **每個檔案包在 `dao.savepoint()` 內**：檔案寫到一半出錯時整檔回滾，前面已寫入的列不會被迴圈結束後的
  `commit()` 一起寫進去——否則資料表多出半份檔案，`finish_load()` 回報的卻是「這個檔案失敗」。
- **updater 持有 DAO（或連線），loader 共用**：一次更新對同一個 DB 只開一條連線，`updater.close()` 負責關閉；
  loader 在共用模式下不關連線，updater 入庫後還要用它查最新日期。
- **查詢錯誤不吞**：只有「表不存在」可以回空值，其餘錯誤往外拋——吞掉會讓 updater 從預設起日靜默重跑整段回補。

### 3.2 失敗必須浮出來

單檔失敗**不中止整批**（其餘檔案仍該入庫），但整批跑完後若有任何失敗，
`finish_load()` 會拋出 `DataLoadError`，最終讓 `tasks/update_db.py` 以**結束碼 1** 結束
且不印 `✅`。`except` 之後只留 warning、行程照樣回報成功，比不 catch 更危險——
缺的列只能靠事後逐日比對列數才發現。

同時，單一 target 失敗**不中斷其餘 target**（`target_guard()`）——一次
`--target no_tick` 會跑十來個 updater、耗時數小時，若其中一個失敗就中止整批，
等於拿可用性換可見度。

**提高失敗可見度之前，要先讓「正常的重複」不算失敗**（§3.1）。兩者必須同時做，
否則日常更新會因為每次重載已入庫檔案而天天以結束碼 1 收場。

### 3.3 交易日判定不可用「非週末」近似

台股有**補行交易日**（補班的週六照常開市）。以 `date.weekday()` 判斷會整天漏掉這些日子。
正確做法是以 `price` 表實際有資料的日期為準（見 `StockMarginUpdater.get_candidate_dates()`）。

`price` 自己就是日曆來源，候選母集合只能是平日，於是**被刪掉的補行交易日不會再被請求**。
故 `StockPriceUpdater` 以 `chip` ∪ `margin` 在區間內的週末日期當 `plan()` 的 `extra_dates`
（`DatePlanner.get_weekend_dates()`）。**已知限制**：尚未出現在任何一張表的新補行交易日仍補不到
（日曆尾端只補平日）；台股自 2019 年起已無補行交易日，實務影響低。

### 3.4 欄位語言跟著資料來源走

**資料表的欄位語言由來源決定，不由市場決定。**

| 來源 | 欄位語言 | 現有例子 |
|------|----------|----------|
| 交易所網頁／檔案（TWSE、TPEX、TAIFEX、MOPS） | **保留來源的中文欄名** | `price` 的 `開盤價`、`chip` 的 `外資買進股數`、`futures_price_daily` 的 `結算價` |
| API（FinMind、未來的美股 provider） | **用來源的英文欄名** | `taiwan_stock_info` 的 `stock_id`／`stock_name`、規劃中的 `us_price_daily` 的 `ticker`／`trade_date` |

兩者皆**以英文命名主鍵欄**（`date`、`stock_id`、`product`、`session`），理由是主鍵會出現在每一句查詢裡。

**為什麼不統一成英文**：多數表是中文欄，程式側的中文欄位字面值橫跨數十個檔。改成英文要同時動 schema、
資料與所有下游，而 [PostgreSQL 遷移](../../backlog/PostgreSQL遷移計畫.md) 本來就會重寫這一層——
真要收斂就在那個批次做，不值得為它單獨開一次遷移。

**為什麼不讓美股用中文**：`開盤價` 這種欄名對 AAPL 沒有來源依據（美股 provider 回的
本來就是 `open`／`close`），硬翻是憑空造一套對照表；而且專案裡已經有全英文的表，
美股用英文是延用既有的規則。

### 3.5 多來源拼成的一份資料：要嘛全部入庫，要嘛全部不入庫

台股的日頻表（price／chip／margin）與財報三表都是**上市＋上櫃兩個市場拼成一份**。
只入庫問到的那一邊時，資料庫裡就是半個市場，而且**不會有任何錯誤**——回測只會安靜地少算。
缺的那半就算事後重試補得回來，重試成功前讀到的也是半份。

| 範圍 | 判定為「沒有完整取得」 | 處置 | 實作 |
|------|------------------------|------|------|
| 日頻三表（逐日） | 任一市場 `FAILED`、任一市場清洗失敗、**一邊 `NO_DATA` 一邊 `OK`** | 兩邊都不清洗／不入庫，記 `incomplete` 下次重試 | `BaseDataUpdater.record_market_day()`、`report_partial_day()` |
| 財報三表（逐年季） | 任一市場請求失敗、拋例外或解析不出表格 | crawler 整季回 `None`；年季改差集，失敗的那季下次仍會被請求 | `FinancialStatementCrawler._crawl_listing_boards()`、`FinancialStatementUpdater.plan_pending_year_seasons()` |

**一邊 `NO_DATA`、一邊 `OK` 也算沒有完整取得**：站方的「查無資料」涵蓋「尚未公布」，
而兩個市場的公布時間不同，收盤後先公布的那一邊若照常入庫，當日就只有半個市場。
代價是若真有「一個市場開市、另一個休市」的日子，它會每輪被重試；那只是多幾次請求。
這條判定只放在日頻三表的 updater，**不改共用的 `UpdateStats.record()`**——除權息、減資、
月營收是區間查詢，一個市場在整段區間內查無資料是正常的。

**不在這條規則內**：申報期內的部分申報（財報、月營收在申報期間只拿得到已送件的公司）
是「來源當下就只有這麼多」，不是市場失敗，需要另一套判準，目前未處理。

期貨行情則是**多個商品各自獨立**的一批：`FuturesPriceUpdater.update()` 逐商品隔離例外，
一個商品觸發空產出保險絲不擋其他商品；全部跑完、印出統計後才拋 `ProductUpdateError`
列出失敗商品，讓 `target_guard()` 仍把整個 target 記為失敗（與 §3.2 的 `finish_load()` 同一個原則）。

---

## 四、新增或修改 updater 的檢查表

以下每一條都對應過真實發生、且**當下沒有任何錯誤訊息**的資料缺漏：

1. **長時間回補必須分批入庫**，不要等整段爬完才寫資料庫。
2. **入庫失敗要拋 `DataLoadError`**，不可只記 warning；重複鍵要走 `INSERT OR IGNORE`，不可算成失敗（§3.1、§3.2）。
3. **主鍵必須能唯一識別商品。** `price` 把上市與上櫃合併存放卻沒有市場欄位，兩邊曾有代號相撞（上櫃 ETF 早年用 4 碼代號）；主鍵含證券名稱的表會兩者並存、不含的表會直接撞鍵。`StockQuoteAdapter.warn_duplicate_symbols()` 讓同一 bar 內的重複代號不再靜默。
4. **不要用「連續 N 筆都沒資料」判斷「整批都沒資料」。** 股票代號是排序過的，某個號段連續都是新上市股完全正常；早退條件一旦與順序耦合，連 resume 都會在同一個地方再次誤判。要判斷整批是否為空，就去找能代表整批的樣本（`EQUITY_CHANGE_PROBE_STOCK_IDS`：三檔長期上市的權值股全部查無資料才判定未申報）。
5. **每批結束留一行統計，而且要數到最後一層。** 統計只數請求層（`N requested / N no data / N unreachable`）時，「兩千次請求全部成功、清洗後零列入庫」會長得跟正常一模一樣。現行格式是 `N requested / N ok / N no data / N unreachable / N cleaned empty`，`cleaned empty` 非 0 即升為 warning。
6. **跨期間的來源要逐期間實查，測試 fixture 要涵蓋每一種期間。** MOPS 權益變動表的本期標籤 Q1 是「第N季」、Q2／Q3／Q4 分別是「上半年度／前3季／年度」；只用 Q1 驗證時，測試與實跑會同時漏掉另外三季。
7. **把暫時性失敗記成 `FAILED`，不要記成「沒有資料」。** 連線失敗、被擋、版面解析不出來都要讓那一天或那一年下次重試；記成 `NO_DATA` 會讓它永遠不再被補。
8. **多個來源拼成一份的資料，任一來源沒有完整取得就整份不入庫**（§3.5）。只入庫問到的那一邊不會有任何錯誤；同理，resume 不可用 `MAX + 1`，否則失敗的那一期被後面成功的期間越過之後永遠不會再被請求。
9. **SQL 寫進 DAO，loader 逐檔包 savepoint，updater 提供 `close()` 並在 `tasks/update_db.py` 以 `try/finally` 呼叫**（§3.1.1）。新增資料表的完整檢查表見[資料存取層 §七](../dev/data-access-layer.md#七新增一張資料表的檢查表)。

## 相關文件

- [指令教學](../commands/command-usage.zh-TW.md)——`update_db` 的完整 target 對照與範例
- [權益變動表](equity-change.md)——`equity_change` 的資料形狀、涵蓋範圍、已知限制與爬取節流
- [非除權息的公司行動](corporate-action.md)——`corporate_action` 表的資料源與調整倍率
- [資料存取層（DAO）](../dev/data-access-layer.md)——連線所有權、savepoint、commit 時點與錯誤語意
- [程式碼品質工具鏈](../dev/code-quality.md)——盲捕 `except Exception` 的收斂方向
- [資料覆蓋範圍](../exchanges/data_coverage.md)——各資料來源的時間涵蓋與已知限制

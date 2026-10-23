# 資料存取層（DAO）

> 本文件描述 `core/dao/` 的現行設計：誰持有連線、誰負責 commit、錯誤怎麼浮出來，以及為什麼這樣切。
> 各項規則的理由同時寫在程式碼的 docstring（`core/dao/base.py`、`core/dao/connection.py`、`core/dao/timescale.py` 與各 DAO）；
> 本文件只放**跨檔案的全貌**與新增資料表時的檢查表。

---

## 一、分層與職責

```
core.config (0) ← core.utils (1) ← core.dao (2) ← core.api / core.pipeline (3) ← ...
```

**SQL、連線與交易只寫在 `core/dao/`。** `core/`、`apps/`、`strategies/` 內 `core/dao/` 以外的檔案不得
import 資料庫驅動（`sqlite3`、`psycopg`、`connectorx`），由 `scripts/check_layer_deps.py` 的
「E''. DAO 以外 import 資料庫驅動」強制。DAO 以外需要型別或捕捉錯誤時：

| 用途 | SQLite | TimescaleDB（台股 tick） |
|------|--------|--------------------------|
| 連線型別 | `core.dao.connection.DBConnection` | 不外露；由 `StockTickDAO` 持有 |
| 捕捉資料庫錯誤 | `DBError`（`sqlite3.Error` 別名） | `core.dao.timescale.tick_db_error_types()` 回傳可放進 `except (...)` 的型別 |

`core/dao` **只 import `core.config`**，不 import `core.utils`、`core.models`、`core.api`、`core.pipeline`。
需要 Enum 的地方（例如期貨交易時段）由上層轉成字串值再傳進來：業務規則（單位換算、交易時段語意）屬於上層。
機器護欄只涵蓋一部分：import `core.api`／`core.pipeline` 會被分層檢查判成反向相依、import `core.models`
（同層）會被列進「同層互相 import」供人工判讀；import `core.utils` 則純屬慣例，不會被擋。
另外 `core/adapters/` 是純轉換層，不得 import `core.dao`（同一支腳本擋）。

| 層 | 負責 | 不負責 |
|----|------|--------|
| DAO | 建表與索引、寫入、查詢、交易（savepoint／commit）、表不存在時的回傳約定 | 業務規則（還原係數、保證金公式、流動性排名）、日誌檔設定 |
| API（`core/api/`） | 公開查詢介面、業務規則、組 map／Series | SQL、連線生命週期細節 |
| loader | 讀 CSV、檔內去重、逐檔彙報（`finish_load`） | SQL、建表 DDL |
| updater | 決定日期、爬取、清洗、分批入庫、**持有並關閉**連線或 DAO | SQL |

---

## 二、目錄與 DAO 清單

一張表（或一組緊密相關的表）一個 DAO。`base.py`、`connection.py`、`timescale.py` 是市場無關的底座；
各市場的 DAO 放在市場軸子目錄 `tw/`（見[命名軸線](naming-axes.md)）。

| DAO | 資料表 | 資料庫 |
|-----|--------|--------|
| `StockPriceDAO` | `price`（同時是台股交易日曆的來源） | `tw_stock.db` |
| `StockChipDAO`／`StockMarginDAO` | `chip`／`margin` | `tw_stock.db` |
| `StockDividendDAO`／`CorporateActionDAO` | `dividend`／`corporate_action` | `tw_stock.db` |
| `StockShortSaleListDAO`／`StockDayTradeListDAO` | `short_sale_list`（平盤下融資融券名單）／`day_trade_list`（現股當沖名單） | `tw_stock.db` |
| `MonthlyRevenueDAO` | `monthly_revenue` | `tw_stock.db` |
| `FinancialStatementDAO(table_name)` | 財報四表（表名白名單） | `tw_stock.db` |
| `StockInfoDAO`／`StockInfoWithWarrantDAO`／`SecuritiesTraderInfoDAO`／`BrokerTradingDAO` | FinMind 四表 | `tw_stock.db` |
| `MarketHolidayDAO` | `market_holiday` | `tw_stock.db` |
| `FuturesPriceDAO` | `futures_price_daily` | `tw_futures.db` |
| `FuturesStockUniverseDAO` | `futures_stock_universe`（快照序列） | `tw_futures.db` |
| `FuturesMarginDAO` | `futures_margin_history`（金額表） | `tw_futures.db` |
| `StockFuturesMarginRateDAO` | `stock_futures_margin_rate_history`（股期比例表；與金額表同一支 ETL，一張表一個 DAO） | `tw_futures.db` |
| `FuturesChipDAO(table_name)` | 期貨籌碼三表（表名白名單） | `tw_futures.db` |
| `FuturesContinuousDAO` | `futures_continuous`（衍生表） | `tw_futures.db` |
| `LiveTradeDAO` | 實盤紀錄各表（`live_run`／`live_order`／`live_fill`／`live_position_lot` 等），自行開連線時走 `connect_live_trading()` | `tw_trading.db` |
| `StockTickDAO` | `stock_tick`（hypertable）／`stock_tick_load_log` | TimescaleDB |

### 台股 tick：TimescaleDB

`StockTickDAO`（`core/dao/tw/stock_tick_dao.py`）**不繼承 `BaseDAO`**（後者綁 `sqlite3.Connection`），
但遵守同一個 `owns_conn` 慣例。連線入口是 `core/dao/timescale.py`，連線字串來自 `TICK_DATABASE_URL`：

- **寫入走 psycopg 3 的 `COPY`**（`connect_tick_db()`），讀取走 **ConnectorX**（`get_connectorx_uri()`，以 Arrow 回傳再轉 pandas）。
  `pd.read_sql` 逐列轉 Python 物件，全市場一天百萬列時慢上數十倍。
- 兩個驅動屬 `tick` extra，**只在函式內 import**：沒裝時 `import core.dao` 照樣成功，真的要連線時才拋出附安裝指令的錯誤；
  連不上資料庫時轉成附「開 Docker、`docker compose up -d postgres`」提示的 `ConnectionError`。
- **冪等靠整天重寫**：`replace_day()` 在同一個交易內刪除該檔該日、`COPY` 寫入、登記 `stock_tick_load_log`。
  同一瞬間的多筆相同成交是真實資料，沒有自然唯一鍵可以 `ON CONFLICT`；整天被排除的日子也登記（`row_count = 0`），續跑時才不會重做。
- 表名一律帶 schema：正式資料在 `public`，整合測試（`tests/test_stock_tick_timescale.py`）在各自的暫存 schema 建表。
- 壓縮：7 天一個 chunk，壓縮 policy 只壓 14 天前的 chunk；大量回補時可暫停 policy 並手動壓縮（`pause_compression_policy()`／`compress_chunks_before()`）。

---

## 三、連線所有權

**`owns_conn` 慣例**：建構時傳入 `conn` 就不擁有它，`close()` 不會關；沒傳才自己開、自己關。
`BaseDataAPI`、`BaseDAO`、`StockTickDAO`、各 loader 都遵守同一條，同一條連線因此可以安全地分給多個 DAO／API。

全專案的 SQLite 連線都從 `core.dao.connection` 開：

| 入口 | 用途 |
|------|------|
| `connect_sqlite(path)` | 一般讀寫 |
| `connect_sqlite(path, read_only=True)` | 只讀場合：以 `mode=ro` URI 開啟，不與背景 ETL 搶寫入鎖，檔案不存在時也不會被 `sqlite3.connect()` 默默建出空 DB |
| `connect_live_trading(path)` | 實盤紀錄庫（見下） |
| `BaseDataFeed.connect_db(path, read_only=...)` | 資料源用（`core/datafeed/base.py`）：讀寫模式下**先建好父目錄**再呼叫 `connect_sqlite()`，全新環境沒有 `data/db/` 時才不會得到看不出原因的 `unable to open database file` |

| 場景 | 誰開連線 | 誰關 | 其他持有者 |
|------|----------|------|------------|
| 回測 | `DataFeed.setup()` 經 `connect_db()`，每個資料源一條（期貨資料源需要現股價格時另開一條到 `tw_stock.db`） | `DataFeed.close()` | 全部 API 以 `conn=` 共用；API 再以 `conn=` 建自己的 DAO |
| 實盤資料源 | 同上，但一律 `read_only=True` | `DataFeed.close()` | 同上 |
| 單一表的 ETL（price、chip、margin、dividend、期貨行情…） | updater 建立 DAO | `updater.close()` | loader 以 `dao=` 共用 |
| 多表 ETL（財報四表、FinMind 四表、期貨籌碼三表） | updater 開連線 | `updater.close()` | loader 以 `conn=` 共用，按表名就地建 DAO |
| 跨庫只讀（期貨 ETL 讀台股交易日曆、標的池比對現股） | updater 第一次用到時以 `read_only=True` 開 | `updater.close()` | — |
| 研究腳本、手動檢查 | `connect_sqlite(..., read_only=True)` | 腳本自己 | — |

**一次執行中同一個 DB 只開一條連線**：updater 與 loader 各開一條到同一個 DB 時，兩條連線會互搶寫入鎖，
且其中一條常常沒人關。`apps/update_db.py` 由 `TARGETS` 註冊表驅動，對每個 target 一律 `try/finally: updater.close()`，
不分 SQLite 或 TimescaleDB（tick 的 `close()` 另會登出 Shioaji，可重複呼叫）。

**實盤紀錄庫一律走 `connect_live_trading()`**：在 `connect_sqlite()` 之上加 `journal_mode=WAL`
（日報、前端與存活監控會在實盤行程寫入的同時讀取，rollback journal 會讓讀寫互相阻塞）與
`synchronous=FULL`（「先寫 DB 再送單」的恢復保證要求那筆 commit 真的落地）。PRAGMA 下在連線入口
而不是讓各處自己下：漏掉一次不會報錯、只會在斷電時丟掉最後一筆 commit。實盤行程是唯一寫入者，
存活監控與報表以 `read_only=True` 開（唯讀連線不改 journal_mode）。

---

## 四、交易與寫入

### 4.1 寫入方法

| 方法 | 語意 | 用在 |
|------|------|------|
| `insert_or_ignore(df)` | 同主鍵忽略，回傳（寫入, 跳過）列數 | 既成事實的資料：行情、籌碼、財報、FinMind |
| `insert_or_replace(df)` | 同主鍵覆蓋 | 區間查詢、站方會更正的資料：除權息、公司行動；衍生表：連續合約 |

兩者都**不 commit**。**不要用 `DataFrame.to_sql`**：它寫完會自行 commit，呼叫端的交易邊界因此形同虛設；
`append` 模式遇到主鍵衝突也會整批拋錯，讓「重跑」與「真的出錯」無法區分。

### 4.2 savepoint：單檔失敗整檔不留

loader 逐檔寫入時把每個檔案包在 `with dao.savepoint():` 內。不包的話，某個檔案寫到一半出錯，
前面已寫入的列仍留在交易裡，會被迴圈結束後的 `commit()` 一起寫進資料庫——資料表多了半份檔案，
回報卻說這個檔案失敗。

`savepoint()` 會先確保交易已開始（Python `sqlite3` 不會為 `SAVEPOINT` 自動開交易，交易外的
savepoint 一經 `RELEASE` 就等於 commit），例外時 `ROLLBACK TO` 再往外拋，同一交易內先前完成的檔案不受影響。
巢狀使用時取不同名稱（例如連續合約：外層包一組調整方式、內層包單一批次）。

### 4.3 commit 時點由擁有交易的一方決定

| 模式 | commit 時點 |
|------|-------------|
| 檔案型 loader（price、chip、margin、平盤下融券／當沖名單、財報、月營收、期貨行情…） | 整批檔案處理完 `commit()` 一次，再 `finish_load()` 彙報（共用骨架為 `BaseDataLoader.load_csv_directory()`）|
| 參考表與 CSV 目錄（FinMind） | 每張表／整個目錄處理完即 commit——後一張失敗拋 `DataLoadError` 時，先成功的不可跟著消失 |
| 券商分點批次更新 | updater 每 50 個組合 commit；重建 metadata 與等待配額前先 commit |
| 期貨籌碼 | loader 不 commit，updater 每個月批次寫完 commit |
| 連續合約 | 同一組（商品, 換月規則）的各種調整方式包一個 savepoint，寫完才 commit |
| 台股 tick | `replace_day()` 每檔每日一個交易 |

**查詢失敗不會動到交易**：`BaseDAO.query_df()` 以 cursor 執行，而不是 `pd.read_sql_query`——
後者查詢失敗時會對整條連線 `rollback()`，共用連線上尚未 commit 的寫入會一起消失。
因此「寫入後、commit 前」夾查詢是安全的；**DAO 內不要再引入 `pd.read_sql_query`**。

---

## 五、錯誤語意：只有「表不存在」可以回空

**「表還沒建」與「查詢出錯」必須分得開。** 前者是全新環境或尚未跑過該 ETL 的正常狀態，回 `None`／空表／空清單；
後者（`database is locked`、欄名打錯、schema 壞掉）一律往外拋。判斷一律先 `table_exists()`，不以 `except` 收掉整類錯誤。

**哪些表可以不存在，由 DAO 以 `OPTIONAL_TABLE = True` 宣告**：選用或可獨立回補的資料集（名單、融資融券、
保證金、公告、財報、月營收、期貨各表、FinMind 券商資料等）在 `get_by_date()`／`get_range()`／`get_by_stock()`／
`get_distinct_dates()` 與各自的自訂查詢裡經 `missing_optional_table()` 回空；不可缺的表（例如台股 `price`、`chip`）
缺了就讓 SQLite 的 `no such table` 直接現形——行情表缺了整場回測沒有意義。
API 層不自己檢查表存不存在，同一張表在不同入口才會有同一個答案。例外兩處各有理由：
`StockDividendAPI` 缺 `corporate_action` 表時要發 warning 並提示回補指令；`StockTickAPI` 的 TimescaleDB 表缺了
tick 回測根本跑不了，直接拋錯。

被吞掉的代價都是靜默的，例如：

| 若把錯誤當成「表不存在」 | 會發生什麼 |
|--------------------------|------------|
| 續跑起點（期貨行情、月營收） | 從預設起日靜默重跑整段回補（數千次請求） |
| 股票／券商清單（財報、FinMind） | 「沒有目標股票，略過」，整段回補一檔都沒跑，行程結束碼 0 |
| 逐檔 resume（權益變動表） | 整季被當成「一檔都還沒爬」，重打兩千多次請求 |
| 交易日（期貨籌碼判斷「被擋」） | 被擋的月份被記成「那幾個月沒有籌碼」 |

所有 SQLite 讀取（`query_df()`、`fetch_one()`、`conn.execute()`）失敗時拋的都是 `sqlite3.Error` 的子類別
（呼叫端以 `DBError` 捕捉），不會被 pandas 包成其他型別。

---

## 六、設計決策

- **表名與欄名不從外部參數組 SQL**：`TABLE_NAME` 是類別常數；多表共用一組查詢的 DAO（財報、期貨籌碼）
  以白名單檢查表名，不在白名單內**在開連線之前**就拋 `ValueError`。以欄名為參數的 `_get_latest_value()` 設為受保護方法。
- **共用查詢的欄名由類別屬性宣告**：`DATE_COLUMN`（預設 `date`）與 `KEY_COLUMNS`（預設 `("stock_id",)`）。
  期貨表的鍵是 `(product, expiry, session)` 這類組合、根本沒有單一標的欄，沿用台股預設去查一呼叫就是 SQL 錯誤；
  只有單一鍵欄的表支援 `get_by_stock()`，否則拋出說明鍵欄的錯誤。日後 `stock_id` 改名時也只動這兩個屬性。
- **組 SQL 時欄名不加雙引號**：SQLite 遇到雙引號包住、卻不存在的欄名，會退回當成字串字面值——欄名打錯時查詢不報錯、
  回傳欄名字串本身。`INSERT` 的欄位清單例外（那裡的雙引號只能是識別字）。
- **API 持有連線、DAO 不持有**：API 依 `owns_conn` 自行開連線，再以 `conn=` 交給 DAO。
- **欄位清單由 pipeline 傳入的表**（財報、月營收）：欄位定義存在清洗器產出的 `*_cleaned_columns.json`，
  讀檔屬於 pipeline；DAO 的 `ensure_table_from_columns(columns)` 只負責欄名到 SQL 型別的對應
  （與基底無參數的 `ensure_table()` 分開命名；這類 DAO 的 `ensure_table()` 會明確拋 `NotImplementedError`）。
- **同一個查詢刻意保留兩種語意時以參數表達，並寫進 docstring**：
  - `FuturesMarginDAO.get_margin_in_effect(inclusive=...)`：回測問「這一天適用多少」用 `<=`；
    updater 驗證公告問「這次調整之前是多少」用 `<`（同一生效日已有列時 `<=` 會拿到調整後的值）。
  - `FuturesStockUniverseDAO.get_contract_size(per_product=...)`：回測乘數看「該日全表快照」，
    快照中沒有該商品就回 None；保證金試算看「該商品自己的快照序列」。
  - `BaseDAO.get_latest_date_before(date)` 是**嚴格小於**：籌碼、融資融券餘額盤後才公布，`<=` 那一個等號就是前視偏差。
    API 層以 `PUBLISHED_AFTER_CLOSE` 宣告盤後公布的資料集（`StockChipAPI`、`StockMarginAPI`、`FuturesChipAPI`），
    回測一律經 `get_available(date)`／`get_available_short_balance_map(date)` 取前一次公布的那份；沒宣告的資料集呼叫它會拋錯。
- **區間查詢不可年、月（季）各自 `BETWEEN`**：月營收與財報的 `get_range` 以 `year * 100 + month`／
  `year * 10 + season` 比較，否則 2023-11～2024-02 這類跨年區間一筆都查不到。
- **來源優先序去重是清洗規則，不放 DAO**：`core/pipeline/shared/source_priority.py`。

---

## 七、新增一張資料表的檢查表

1. **在 `core/dao/tw/` 新增 DAO**，`TABLE_NAME`／`DEFAULT_DB_PATH` 為類別常數；建表 DDL 放 `create_table()`
   （基底不提供實作，沒覆寫會 `NotImplementedError`）。`ensure_table()` 由基底提供、可重複呼叫。依表的性質宣告：
   - `KEY_COLUMNS`（鍵不是 `stock_id` 時）、`DATE_COLUMN`（日期欄不叫 `date` 時）；
   - `OPTIONAL_TABLE = True`（選用或可獨立回補的資料集）；
   - `NEEDS_SYMBOL_DATE_INDEX = True`（一列一（標的, 日期）的明細表），`(stock_id, date)` 複合索引以 `IF NOT EXISTS` 每次補；
   - `PRIMARY_KEY_COLUMNS`（走 `load_csv_directory()` 的表；與 DDL 的 PRIMARY KEY 一致，供檔內去重）。
2. **表不存在時的回傳約定寫進 docstring**，其餘錯誤往外拋（§五）。
3. **loader 收 `dao=`（或多表時收 `conn=`），不擁有時不關閉**；逐檔包 savepoint，commit 時點依 §4.3 擇一。
4. **updater 持有 DAO／連線並提供 `close()`**（基底有預設；日誌檔名以 `LOG_FILE_NAME` 宣告），在 `apps/update_db.py` 的 `TARGETS` 註冊表加一列，即由同一個迴圈 `try/finally` 收尾。
5. **API 以 `conn=` 建 DAO**，公開方法只做業務轉換，不寫 SQL。
6. **測試建表走 `tests/conftest.py` 的 `dao_factory`**：以 DAO 自己的建表方法建出正式 schema，
   `complete_rows()` 補齊沒給值的 NOT NULL／主鍵欄，**給了 schema 沒有的欄位直接報錯**（欄名打錯或 schema 改名時測試會紅）。
   一個 DAO 管多張表時以 `records_table=` 指定寫入哪一張。只有刻意壞掉的 schema（缺欄位、被鎖住）才手寫 `CREATE TABLE`；
   測試不以 `DataFrame.to_sql` 建表——它依資料推導欄位型別，不會隨正式 schema 改動而同步。
7. 新增 DAO 測試至少涵蓋：建表冪等、查詢區間邊界、表不存在、查詢錯誤外拋、共用連線不被 loader 關掉、寫到一半失敗整檔回滾。

---

## 八、已知限制

| 項目 | 影響 |
|------|------|
| 日線資料的 DAO 內部是 `sqlite3` | 換資料庫時要改寫 `core/dao/` 內部與 `core/dao/connection.py`（API、loader、updater 不必改）；`DBConnection`／`DBError` 目前是 `sqlite3` 型別的別名 |
| 兩套驅動並存 | 台股 tick 在 TimescaleDB、其餘在 SQLite，`StockTickDAO` 不共用 `BaseDAO` 的查詢與 savepoint 機制 |
| 台股表名缺 `stock_` 前綴、識別欄仍是 `stock_id` | 與期貨表、`symbol` 命名不對稱（見[命名軸線](naming-axes.md)） |

## 相關文件

- [ETL 入庫約定](../pipeline/etl-ingestion.md)——各 updater 的入庫時機、resume 依據與失敗語意
- [模組使用關係](../backtest/module-map.md)——回測路徑上的連線持有者
- [命名軸線](naming-axes.md)——`core/dao/tw/` 的市場軸目錄
- [程式碼品質工具鏈](code-quality.md)——分層檢查在 CI 的位置

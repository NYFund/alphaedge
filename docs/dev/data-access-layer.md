# 資料存取層（DAO）

> 本文件描述 `core/dao/` 的現行設計：誰持有連線、誰負責 commit、錯誤怎麼浮出來，以及為什麼這樣切。
> 各項規則的理由同時寫在程式碼的 docstring（`core/dao/base.py`、`core/dao/connection.py` 與各 DAO）；
> 本文件只放**跨檔案的全貌**與新增資料表時的檢查表。

---

## 一、分層與職責

```
core.config (0) ← core.utils (1) ← core.dao (2) ← core.api / core.pipeline (3) ← ...
```

**SQL、連線與交易只寫在 `core/dao/`。** `core/`、`tasks/` 內 `core/dao/` 以外的檔案不得
`import sqlite3`，由 `scripts/check_layer_deps.py` 的「E''. DAO 以外 import 資料庫驅動」強制；
型別標註用 `core.dao.connection.DBConnection`，捕捉資料庫錯誤用 `DBError`。

`core/dao` **只 import `core.config`**，不 import `core.utils`（`core.utils.instrument` 會反向拉進回測層）、
`core.models`、`core.api`、`core.pipeline`。需要 Enum 的地方（例如期貨交易時段）由上層轉成字串值再傳進來。

| 層 | 負責 | 不負責 |
|----|------|--------|
| DAO | 建表與索引、寫入、查詢、交易（savepoint／commit）、表不存在時的回傳約定 | 業務規則（還原係數、保證金公式、流動性排名）、日誌檔設定 |
| API（`core/api/`） | 公開查詢介面、業務規則、組 map／Series | SQL、連線生命週期細節 |
| loader | 讀 CSV、檔內去重、逐檔彙報（`finish_load`） | SQL、建表 DDL |
| updater | 決定日期、爬取、清洗、分批入庫、**持有並關閉**連線或 DAO | SQL |

---

## 二、目錄與 DAO 清單

一張表（或一組緊密相關的表）一個 DAO。`base.py`、`connection.py` 是市場無關的底座；
各市場的 DAO 放在市場軸子目錄 `tw/`（見[命名軸線](naming-axes.md)）。

| DAO | 資料表 | 資料庫 |
|-----|--------|--------|
| `StockPriceDAO` | `price`（同時是台股交易日曆的來源） | `tw_stock.db` |
| `StockChipDAO`／`StockMarginDAO` | `chip`／`margin` | `tw_stock.db` |
| `StockDividendDAO`／`CorporateActionDAO` | `dividend`／`corporate_action` | `tw_stock.db` |
| `MonthlyRevenueDAO` | `monthly_revenue` | `tw_stock.db` |
| `FinancialStatementDAO(table_name)` | 財報四表（表名白名單） | `tw_stock.db` |
| `StockInfoDAO`／`StockInfoWithWarrantDAO`／`SecuritiesTraderInfoDAO`／`BrokerTradingDAO` | FinMind 四表 | `tw_stock.db` |
| `FuturesPriceDAO` | `futures_price_daily` | `tw_futures.db` |
| `FuturesStockUniverseDAO` | `futures_stock_universe`（快照序列） | `tw_futures.db` |
| `FuturesMarginDAO` | `futures_margin_history` ＋ `stock_futures_margin_rate_history` | `tw_futures.db` |
| `FuturesChipDAO(table_name)` | 期貨籌碼三表（表名白名單） | `tw_futures.db` |
| `FuturesContinuousDAO` | `futures_continuous`（衍生表） | `tw_futures.db` |

**tick 不在此列**：台股與期貨 tick 仍走 DolphinDB，改用 TimescaleDB 的規劃見
[台股tick改用TimescaleDB](../../backlog/台股tick改用TimescaleDB.md)，其連線層同樣放在 `core/dao/`。

---

## 三、連線所有權

**`owns_conn` 慣例**：建構時傳入 `conn` 就不擁有它，`close()` 不會關；沒傳才自己開、自己關。
`BaseDataAPI`、`BaseDAO`、各 loader 都遵守同一條。

| 場景 | 誰開連線 | 誰關 | 其他持有者 |
|------|----------|------|------------|
| 回測 | `DataFeed.setup()`（台股、期貨各一條） | `DataFeed` | 全部 API 以 `conn=` 共用；API 再以 `conn=` 建自己的 DAO |
| 單一表的 ETL（price、chip、margin、dividend、期貨行情…） | updater 建立 DAO | `updater.close()` | loader 以 `dao=` 共用 |
| 多表 ETL（財報四表、FinMind 四表、期貨籌碼三表） | updater 開連線 | `updater.close()` | loader 以 `conn=` 共用，按表名就地建 DAO |
| 跨庫只讀（期貨 ETL 讀台股交易日曆、標的池比對現股） | updater 第一次用到時以 `read_only=True` 開 | `updater.close()` | — |
| 研究腳本、手動檢查 | `connect_sqlite(..., read_only=True)` | 腳本自己 | — |

**一次執行中同一個 DB 只開一條連線**：舊版 updater 與 loader 各開一條到同一個 DB，
updater 那條從不關閉，兩條連線還會互搶寫入鎖（券商分點曾得先 commit loader 那條才查得動）。
`tasks/update_db.py` 的每個 SQLite target 一律 `try/finally: updater.close()`（tick 走 DolphinDB，另以 `logout()` 收尾）。

**唯讀連線**（`connect_sqlite(path, read_only=True)`）用在只讀的場合：不會與背景 ETL 搶寫入鎖，
檔案不存在時也不會被 `sqlite3.connect()` 默默建出一個空 DB。

---

## 四、交易與寫入

### 4.1 寫入方法

| 方法 | 語意 | 用在 |
|------|------|------|
| `insert_or_ignore(df)` | 同主鍵忽略，回傳（寫入, 跳過）列數 | 既成事實的資料：行情、籌碼、財報、FinMind |
| `insert_or_replace(df)` | 同主鍵覆蓋 | 區間查詢、站方會更正的資料：除權息、公司行動；衍生表：連續合約 |

兩者都**不 commit**。**不要用 `DataFrame.to_sql`**：pandas 的 `to_sql` 寫完會自行 commit，
呼叫端傳的 `commit=False` 因此形同虛設（券商分點批次更新曾是如此）；`append` 模式遇到主鍵衝突也會整批拋錯，
讓「重跑」與「真的出錯」無法區分。

### 4.2 savepoint：單檔失敗整檔不留

loader 逐檔寫入時把每個檔案包在 `with dao.savepoint():` 內。舊寫法下某個檔案寫到一半出錯，
前面已寫入的列仍留在交易裡，被迴圈結束後的 `commit()` 一起寫進資料庫——資料表多了半份檔案，
回報卻說這個檔案失敗。

`savepoint()` 會先確保交易已開始（Python `sqlite3` 不會為 `SAVEPOINT` 自動開交易，交易外的
savepoint 一經 `RELEASE` 就等於 commit），例外時 `ROLLBACK TO` 再往外拋，同一交易內先前完成的檔案不受影響。
巢狀使用時取不同名稱（例如連續合約：外層包一組調整方式、內層包單一批次）。

### 4.3 commit 時點由擁有交易的一方決定

| 模式 | commit 時點 |
|------|-------------|
| 檔案型 loader（price、chip、margin、財報、月營收、期貨行情…） | 整批檔案處理完 `commit()` 一次，再 `finish_load()` 彙報 |
| 參考表與 CSV 目錄（FinMind） | 每張表／整個目錄處理完即 commit——後一張失敗拋 `DataLoadError` 時，先成功的不可跟著消失 |
| 券商分點批次更新 | updater 每 50 個組合 commit；重建 metadata 與等待配額前先 commit |
| 期貨籌碼 | loader 不 commit，updater 每個月批次寫完 commit |
| 連續合約 | 同一組（商品, 換月規則）的各種調整方式包一個 savepoint，寫完才 commit |

⚠️ **`pd.read_sql_query` 查詢失敗時會對整條連線 `rollback()`**，再把錯誤包成
`pandas.errors.DatabaseError`。共用連線上若有未 commit 的寫入會一起消失——
**不要在寫入與 commit 之間夾查詢**；必須夾的（券商分點重建 metadata）先 commit。

---

## 五、錯誤語意：只有「表不存在」可以回空

**「表還沒建」與「查詢出錯」必須分得開。** 前者是全新環境或尚未跑過該 ETL 的正常狀態，回 `None`／空表／空清單；
後者（`database is locked`、欄名打錯、schema 壞掉）一律往外拋。判斷一律先 `table_exists()`，不以 `except` 收掉整類錯誤。

被吞掉的代價都是靜默的：

| 被吞的地方（已修） | 會發生什麼 |
|--------------------|------------|
| 續跑起點（期貨行情、月營收） | 從預設起日靜默重跑整段回補（數千次請求） |
| 股票／券商清單（財報、FinMind） | 「沒有目標股票，略過」，整段回補一檔都沒跑，行程結束碼 0 |
| 逐檔 resume（權益變動表） | 整季被當成「一檔都還沒爬」，重打兩千多次請求 |
| 交易日（期貨籌碼判斷「被擋」） | 被擋的月份被記成「那幾個月沒有籌碼」 |

注意 pandas 會把 `sqlite3.OperationalError` 包成 `pandas.errors.DatabaseError`；
走 `query_df()` 的查詢拋的是後者，走 `fetch_one()`／`conn.execute()` 的拋的是前者。

---

## 六、設計決策

- **表名與欄名不從外部參數組 SQL**：`TABLE_NAME` 是類別常數；多表共用一組查詢的 DAO（財報、期貨籌碼、保證金）
  以白名單檢查表名，不在白名單內**在開連線之前**就拋 `ValueError`。以欄名為參數的 `_get_latest_value()` 設為受保護方法。
- **組 SQL 時欄名不加雙引號**：SQLite 遇到雙引號包住、卻不存在的欄名，會退回當成字串字面值——欄名打錯時查詢不報錯、
  回傳欄名字串本身。`INSERT` 的欄位清單例外（那裡的雙引號只能是識別字）。
- **API 持有連線、DAO 不持有**：API 仍依 `owns_conn` 自行開連線，再以 `conn=` 交給 DAO。
  `BaseDataAPI.close()`、DataFeed 的共用連線與 reporter 的關閉邏輯因此都不用改。
- **欄位清單由 pipeline 傳入的表**（財報、月營收）：欄位定義存在清洗器產出的 `*_cleaned_columns.json`，
  讀檔屬於 pipeline；DAO 的 `ensure_table(columns)` 只負責欄名到 SQL 型別的對應。
- **同一個查詢刻意保留兩種語意時以參數表達，並寫進 docstring**：
  - `FuturesMarginDAO.get_margin_in_effect(inclusive=...)`：回測問「這一天適用多少」用 `<=`；
    updater 驗證公告問「這次調整之前是多少」用 `<`（同一生效日已有列時 `<=` 會拿到調整後的值）。
  - `FuturesStockUniverseDAO.get_contract_size(per_product=...)`：回測乘數看「該日全表快照」，
    快照中沒有該商品就回 None；保證金試算看「該商品自己的快照序列」。
  - `FuturesChipDAO.get_latest_date_before(date)` 是**嚴格小於**：籌碼盤後才公布，`<=` 那一個等號就是前視偏差。
- **區間查詢不可年、月（季）各自 `BETWEEN`**：月營收與財報的 `get_range` 以 `year * 100 + month`／
  `year * 10 + season` 比較，否則 2023-11～2024-02 這類跨年區間一筆都查不到。
- **來源優先序去重是清洗規則，不放 DAO**：`core/pipeline/shared/source_priority.py`。

---

## 七、新增一張資料表的檢查表

1. **在 `core/dao/tw/` 新增 DAO**，`TABLE_NAME`／`DEFAULT_DB_PATH` 為類別常數；建表 DDL 放 `create_table()`，
   `ensure_table()` 可重複呼叫（`(stock_id, date)` 類的索引用 `IF NOT EXISTS` 每次補）。
2. **表不存在時的回傳約定寫進 docstring**，其餘錯誤往外拋（§五）。
3. **loader 收 `dao=`（或多表時收 `conn=`），不擁有時不關閉**；逐檔包 savepoint，commit 時點依 §4.3 擇一。
4. **updater 持有 DAO／連線並提供 `close()`**，`tasks/update_db.py` 以 `try/finally` 呼叫。
5. **API 以 `conn=` 建 DAO**，公開方法只做業務轉換，不寫 SQL。
6. **測試建表走 `tests/conftest.py` 的 `dao_factory`**：以 DAO 自己的建表方法建出正式 schema，
   `complete_rows()` 補齊沒給值的 NOT NULL／主鍵欄。只有刻意壞掉的 schema（缺欄位、被鎖住）才手寫 `CREATE TABLE`。
7. 新增 DAO 測試至少涵蓋：建表冪等、查詢區間邊界、表不存在、查詢錯誤外拋、共用連線不被 loader 關掉、寫到一半失敗整檔回滾。

---

## 八、已知限制

| 項目 | 影響 | 解除條件 |
|------|------|----------|
| DAO 內部仍是 `sqlite3` | 換 PostgreSQL 時要改寫 `core/dao/` 內部（API、loader、updater 不必改） | [PostgreSQL遷移計畫](../../backlog/PostgreSQL遷移計畫.md) |
| `pd.read_sql_query` 失敗會 rollback 整條連線 | 共用連線上「寫入後、commit 前」的查詢一旦失敗，未 commit 的寫入會消失 | 目前維持 §4.3 的 commit 時點；結構性修正（`query_df` 改用 cursor）見 [DAO重構後續收斂](../../backlog/DAO重構後續收斂.md) S1 |
| 部分測試仍以 `DataFrame.to_sql` 推導 schema 建表 | `test_api_public_interfaces`、`test_finmind_api`、`test_stock_data_api`、`test_corporate_action`（偵測器只需三欄的最小 `price`）、`backtest/test_reporting`，以及 DAO 測試中的 `test_dao_financial_statement`（最小 `taiwan_stock_info`）、`test_dao_stock_dividend_corporate_action`（偵測器用的最小 `price`）；它們沒有抄 schema，但不會隨正式 schema 改動而同步 | [DAO重構後續收斂](../../backlog/DAO重構後續收斂.md) S3 |
| 台股表名缺 `stock_` 前綴、識別欄仍是 `stock_id` | 與期貨表、`symbol` 命名不對稱 | 歸 PostgreSQL 遷移的 schema 批次 |

## 相關文件

- [ETL 入庫約定](../pipeline/etl-ingestion.md)——各 updater 的入庫時機、resume 依據與失敗語意
- [模組使用關係](../backtest/module-map.md)——回測路徑上的連線持有者
- [命名軸線](naming-axes.md)——`core/dao/tw/` 的市場軸目錄
- [程式碼品質工具鏈](code-quality.md)——分層檢查在 CI 的位置

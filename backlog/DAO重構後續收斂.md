# DAO 重構後續收斂

## Abstract

- **背景／問題**：DAO 資料存取層重構於 2026-09-16 完成（設計見 [資料存取層](../docs/dev/data-access-layer.md)），驗收全數通過，但過程中發現幾件當下不適合一起修的事：
  1. **`pd.read_sql_query` 查詢失敗會對整條連線 `rollback()`**：共用連線上「寫入後、commit 前」的查詢一旦失敗，未 commit 的寫入會消失。目前只靠 commit 時點的約定避開，沒有結構上的保證。
  2. **期貨保證金歷史回補每次重抓全部公告**：`FuturesMarginUpdater.update_history()` 算出 `loaded_dates` 卻沒拿來跳過，`stats["skipped_existing"]` 恆為 0，每次回補約數百次請求。
  3. **7 個測試檔仍以 `DataFrame.to_sql` 推導 schema 建表**，不會隨正式 schema 改動而同步。
  4. **FinMind 沒有連網的冒煙測試腳本**：舊的三支 `scripts/manual/` 腳本因過時而刪除，pytest 只涵蓋離線行為。
  5. **`PostgreSQL遷移計畫.md` 的產出欄未依 DAO 完成後的實況重新盤點**（仍列著已刪除的 `sqlite_utils.py`、`finmind/schema.py` 等）。
  6. 既有的 lint 警告 `tests/backtest/test_market_calendar_bounds.py:40` F841（非 DAO 重構造成，但它讓 `ruff check --select F tests` 無法當成零警告護欄）。

  2026-09-16 對正式 DB 實跑 `update_db`（14 個 target，結束碼 0）時另外發現兩件既有問題（非 DAO 重構造成）：

  7. **FinMind 券商分點 crawler 把任何例外都當成「沒有資料」**：`FinMindCrawler.crawl_broker_trading_daily_report()` 除配額用盡外一律 `except Exception: return None`，updater 記成 `NO_DATA`。實測帳號等級不足（`Your level is register`）時整批組合都會被算成「無資料」、行程以結束碼 0 成功。
  8. **dividend、corporate_action、月營收的入庫摘要把「整檔已存在」算成「新寫入」**：實跑月營收 14 個 CSV 全部 `all records already exist`，摘要卻是「新寫入 14 檔、已存在跳過 0 檔」。
- **目標**：DAO 讀取失敗不再影響同一條連線上的交易；保證金回補只抓未入庫的公告；測試建表全部走正式 schema；FinMind 有可重複執行的連網冒煙檢查；PostgreSQL 計畫的改動面與實況一致；`ruff check --select F` 全專案零警告；FinMind 權限／連線錯誤不再被算成「沒有資料」；入庫摘要的「新寫入」與「已存在跳過」分得開。
- **範圍界線**：**不改**資料表 schema、API 公開介面與回測記帳；**不做** PostgreSQL 遷移本身（只重新盤點計畫文件）；**不動** tick（DolphinDB／TimescaleDB 另有文件）；FinMind 冒煙檢查**不回補**券商分點（只驗單一組合）。
- **驗收標準**：S1～S8 皆 ✅（S4 可依使用者裁示標 ⏸）；`pytest -m "not slow"` 與 `-m slow` 全綠；`./scripts/run_regression.sh` 雙線通過；`python scripts/check_layer_deps.py` 違規 0；`ruff check --select F core tasks tests scripts` 零警告。

## 進度追蹤表

| 編號 | 步驟名稱 | 產出檔案 | 驗證方式 | 狀態 | 備註／中斷點 |
|------|----------|----------|----------|:----:|--------------|
| S1 | DAO 讀取改用 cursor，查詢失敗不 rollback 連線 | `core/dao/base.py`、`tests/test_dao_base.py`、捕捉 `pd.errors.DatabaseError` 的 7 處測試（4 個 `tests/test_dao_*.py`） | 新增「寫入未 commit → 查詢失敗 → 寫入仍在」測試；既有測試全過 | ✅ | 2026-09-16 完成：`pytest` 快測 1146／慢測 18 全過、回歸雙線通過；兩個正式 DB 每張表與邊界查詢共 78 組比對 `read_sql_query` 結果逐欄相同 |
| S2 | 保證金歷史回補跳過已入庫公告 | `core/pipeline/tw/updaters/futures_margin_updater.py`、`tests/test_futures_margin.py` | 替身 crawler 記錄請求：已入庫生效日的公告不再下載附件，`skipped_existing` 正確計數 | ✅ | 2026-09-16 完成；**偏離原規格**：以公告連結為鍵另存處理紀錄 JSON，不以生效日判斷（同日常有多則公告）。未對正式來源實跑 |
| S3 | 以 `to_sql` 建表的測試改走 `dao_factory` | `tests/test_api_public_interfaces.py`、`tests/test_finmind_api.py`、`tests/test_stock_data_api.py`、`tests/test_corporate_action.py`、`tests/backtest/test_reporting.py`、`tests/test_dao_financial_statement.py`、`tests/test_dao_stock_dividend_corporate_action.py` | `grep -rn "\.to_sql(" tests` 無結果；`pytest` 全綠 | ⬜ | — |
| S4 | FinMind 連網冒煙檢查腳本 | `scripts/manual/manual_finmind_smoke.py`、`scripts/manual/README.md` | 有 token 時對暫存 DB 跑完 stock_info／broker_info／單一券商分點組合並印出列數；無 token 時清楚提示後結束 | ⏸ | 2026-09-16 使用者裁示暫緩：帳號等級 `register` 無券商分點權限，腳本最關鍵的一段無法實跑；帳號升級或確實需要連網檢查時再做 |
| S5 | PostgreSQL 遷移計畫重新盤點改動面 | `backlog/PostgreSQL遷移計畫.md`、`backlog/index.md` | 各步驟產出欄的檔案皆存在（`check_doc_paths.py` 通過），改動面數字與 `grep` 實測一致 | ⬜ | — |
| S6 | 清掉既有 F841 | `tests/backtest/test_market_calendar_bounds.py` | `ruff check --select F core tasks tests scripts` 零警告 | ✅ | 2026-09-16 完成：未使用的 `api` 是早期草稿殘留，改為斷言回推次數等於上界 |
| S7 | FinMind 券商分點 crawler 不再把錯誤當成沒有資料 | `core/pipeline/tw/crawlers/finmind_crawler.py`、`core/pipeline/tw/updaters/finmind/broker_trading_updater.py`、`tests/test_finmind_broker_trading_batch.py` | 替身 API 拋一般例外時組合記為 `ERROR`、整批跑完拋 `DataLoadError`；回空表時仍是 `NO_DATA` | ⬜ | 2026-09-16 實跑發現（帳號等級 `register` 無此資料集權限） |
| S8 | 入庫摘要區分「新寫入」與「整檔已存在」 | `core/pipeline/tw/loaders/stock_dividend_loader.py`、`corporate_action_loader.py`、`monthly_revenue_report_loader.py` 與對應測試 | 重跑同一批 CSV 時摘要為「新寫入 0 檔、已存在跳過 N 檔」 | ⬜ | 2026-09-16 實跑發現 |

## S1. DAO 讀取改用 cursor，查詢失敗不 rollback 連線 ✅

- **目的**：讓「查詢失敗」不會連帶丟掉同一條連線上尚未 commit 的寫入，把目前的約定變成結構保證。
- **做法**：
  - `BaseDAO.query_df()` 改為 `cursor = self.conn.execute(sql, to_sql_params(*params))`，再以 `pd.DataFrame(cursor.fetchall(), columns=[d[0] for d in cursor.description])` 組表。2026-09-16 已實測：`conn.execute()` 失敗只拋 `sqlite3.OperationalError`，`in_transaction` 仍為 True、未 commit 的列仍在；`pd.read_sql_query` 則會 rollback。
  - 型別差異要處理：`read_sql_query` 會把整欄 `None` 推成 `object`、數值欄推成 `int64`／`float64`，`DataFrame(rows)` 的推斷結果可能不同——以 `pd.DataFrame.from_records()` 並在必要時比對回歸雙線，**回歸必須逐筆相同**。
  - 空結果時仍要帶欄名（`cursor.description` 在零列時依然存在）。
  - 錯誤型別因此由 `pandas.errors.DatabaseError` 變回 `sqlite3.Error`：更新 7 處 `pytest.raises(pd.errors.DatabaseError)`，並改寫 `docs/dev/data-access-layer.md` §4.3 的警告與 §五 的錯誤型別說明、`BaseDAO.query_df()` 的 docstring。
  - 券商分點「重建 metadata 前先 commit」的註解理由隨之改寫（仍保留 commit，理由變成定期落地）。
- **產出**：`core/dao/base.py`、`tests/test_dao_base.py`、`tests/test_dao_*.py` 中 7 處錯誤型別斷言、`docs/dev/data-access-layer.md`、`core/pipeline/tw/updaters/finmind/broker_trading_updater.py` 的註解。
- **驗證方式**：新增測試「insert 未 commit → `query_df()` 查不存在的欄 → 拋 `sqlite3.OperationalError` → commit 後列仍在」；`pytest -m "not slow"`、`-m slow` 全綠；回歸雙線逐筆相同。
- **相依**：無。
- **完成紀錄（2026-09-16）**：
  - `query_df()` 以 `conn.execute()` 取 cursor，再以 `DataFrame.from_records(..., coerce_float=True)` 組表——與 pandas 3.0.2 `read_sql_query` 內部的組表方式相同。
  - 對 `tw_stock.db`、`tw_futures.db` 每張表（前 3000 列、零列、最後一列）與 NULL／混合型別／重複欄名等查詢，共 78 組以 `assert_frame_equal` 比對兩種寫法，全數相同。
  - 新增 `test_failed_query_keeps_uncommitted_writes`、`test_query_df_types_and_empty_columns`；測試中 7 處 `pytest.raises(pd.errors.DatabaseError)` 改為 `sqlite3.OperationalError`（分布在 5 個檔，**偏離原規格**所寫的 4 個檔，數量相同）。
  - `docs/dev/data-access-layer.md` §4.3、§五改寫，〈已知限制〉刪除對應列；券商分點重建 metadata 前 commit 的註解理由改為「反映已落地資料、中斷時不必重抓」。

## S2. 保證金歷史回補跳過已入庫公告 ✅

- **目的**：回補只下載尚未入庫的公告附件，不再每次重抓數百則。
- **做法**：
  - `update_history()` 在解析附件網址前，以 `loaded_dates`（`source='announcement'` 的生效日）跳過已入庫者並累加 `stats["skipped_existing"]`。
  - **注意鍵的對應**：`loaded_dates` 是**生效日**，但公告列表給的是**公告日**，生效日要下載附件才知道。可行做法擇一：在 metadata 記下「公告日 → 生效日」對照，或以附件網址（`resolve_csv_urls()` 已去重）為鍵記錄已處理的公告。實作前先確認哪一種能在不下載附件的前提下判斷。
  - 同一則公告可能同時寫入金額表與比例表，兩張表都要算進「已入庫」。
  - 提供 `force=True` 參數保留全量重抓的能力（站方更正附件時用）。
- **產出**：`core/pipeline/tw/updaters/futures_margin_updater.py`、`tests/test_futures_margin.py`；`docs/futures/tw-futures-platform.md`〈已知限制〉刪除對應列。
- **驗證方式**：替身 crawler 記錄 `crawl_announcement_csv` 的呼叫——第二次回補對已入庫公告零呼叫、`skipped_existing` 等於已入庫則數；`force=True` 時全部重抓。
- **相依**：無。
- **完成紀錄（2026-09-16）**：
  - **偏離原規格**：不以生效日（`loaded_dates`）判斷。實查正式 DB 有 15 個生效日同時出現在金額表與比例表——同一生效日常有指數類、股票類兩則公告，以生效日跳過會把沒入庫的那一則整則漏掉；生效日雖可由標題解析，也有同樣問題。
  - 改為 `AnnouncementMetadataStore`（`futures_margin_updater.py`）：以公告連結為鍵記錄 `loaded`（含生效日與實際寫入的表）與 `no_futures_rows`，存於 `FUTURES_MARGIN_ANNOUNCEMENT_METADATA_PATH`；下載失敗、沒有附件的公告不記。
  - `loaded` 的紀錄要生效日**逐表**確實存在才跳過（資料庫還原時照常重抓）；以兩表聯集判斷的初版被測試抓到會誤判。
  - 已處理的公告**連明細頁也不再開**（原本只打算省附件下載），但以紀錄中的原始網址參與 `resolve_csv_urls()` 的共用網址判斷。
  - 新增 `update_history(force=True)`；統計多一項「附件下載失敗」（原本與「無期貨列」混在一起）。
  - 新增 5 個測試（`tests/test_futures_margin.py`）；`pytest -m "not slow"` 1151 全過。**未對 TAIFEX 實跑**：第一次執行沒有紀錄檔，仍會全量抓一次。
  - `docs/futures/tw-futures-platform.md` §2.6 補上說明，〈已知限制〉刪除對應列。

## S3. 以 `to_sql` 建表的測試改走 `dao_factory` ⬜

- **目的**：測試資料表一律是正式 schema，schema 改動時測試跟著變。
- **做法**：逐檔把 `DataFrame(...).to_sql(table, conn)` 換成 `dao_factory(DAO, records=[...])`（`tests/conftest.py`）。偵測器測試（`test_corporate_action.py`、`test_dao_stock_dividend_corporate_action.py`）只需要 `date`／`stock_id`／`收盤價` 三欄，`dao_factory` 的 `complete_rows()` 會補齊其餘 NOT NULL 欄，可以直接換。`test_stock_data_api.py` 的 fixture 餵的是 API 的具名查詢，換完要確認數值型別（`to_sql` 推導的欄型與正式 schema 的 `REAL`／`INTEGER` 可能不同）不影響斷言。
- **產出**：上表 S3 列出的 7 個測試檔；`docs/dev/data-access-layer.md`〈已知限制〉刪除對應列。
- **驗證方式**：`grep -rn "\.to_sql(" tests` 無結果（註解裡提到 `to_sql` 的不算）；`pytest -m "not slow"` 全綠。
- **相依**：無。

## S4. FinMind 連網冒煙檢查腳本 ⏸

- **目的**：補回「真的打 FinMind API、真的寫進 DB」的手動檢查，但不再用 mock 改寫 `core.config`。
- **做法**：新增 `scripts/manual/manual_finmind_smoke.py`：
  - 以 `tempfile` 建暫存 DB 與暫存 downloads，**不動 `sys.modules`、不以 mock 改寫 `core.config`**。
    `FinMindUpdater.__init__()` 目前直接讀模組層級的 `TW_STOCK_DB_PATH`，沒有路徑參數：
    先評估是替它加一個 `db_path: Optional[Path] = None` 參數（改動小、測試也能用），
    還是腳本內組 `FinMindContext`＋`FinMindLoader(conn=connect_sqlite(temp_db))` 自行串流程。
  - 依序執行 `update_stock_info()`、`update_broker_info()`、`broker_trading.update_combination("2330", "1020", 近一週, do_commit=False)` 後 `loader.commit()`，各印出 DAO 查到的列數。
  - 無 `FINMIND_API_TOKEN` 時印出設定方式並以非零結束碼結束；跑完刪除暫存目錄。
- **產出**：`scripts/manual/manual_finmind_smoke.py`、`scripts/manual/README.md`。
- **驗證方式**：有 token 時實跑一次，三個資料集列數皆 > 0；無 token 時結束碼非零且訊息清楚。
- **相依**：無。**需使用者確認是否需要**；不需要則本步驟標 ⏸ 並註明原因。
- **暫緩（2026-09-16）**：使用者裁示暫緩。FinMind 帳號等級為 `register`，無券商分點資料集權限，腳本最關鍵的單一組合寫入無法實跑。**解除條件**：帳號升級，或確實需要可重複執行的連網檢查。

## S5. PostgreSQL 遷移計畫重新盤點改動面 ⬜

- **目的**：讓遷移計畫的步驟與產出欄反映 DAO 完成後的實況，避免照舊清單施工。
- **做法**：以 `grep` 實測 `core/`、`tasks/`、`tests/` 的 SQLite 專屬語法（`sqlite_master`、`PRAGMA`、`INSERT OR IGNORE／REPLACE`、`SAVEPOINT`、`GLOB`、`CAST`）與 `import sqlite3` 分布；依結果改寫 `PostgreSQL遷移計畫.md` 的改動面表、各步驟產出欄與相依；Phase1-2、Phase2-1~Phase2-3 縮減為「改寫 `core/dao/` 內部」並列出具體 DAO 檔案；同步 `backlog/index.md` 該列的說明與進度。
- **產出**：`backlog/PostgreSQL遷移計畫.md`、`backlog/index.md`。
- **驗證方式**：`python scripts/check_doc_paths.py` 通過；文件列出的檔案皆存在；改動面數字與實測指令輸出一致（指令寫進文件）。
- **相依**：S1（`query_df` 的實作方式會影響遷移時的讀取層改寫）。

## S6. 清掉既有 F841 ✅

- **目的**：讓 `ruff check --select F` 在全專案零警告，可以直接當護欄。
- **做法**：`tests/backtest/test_market_calendar_bounds.py:40` 的 `api` 變數未使用——先確認測試是否本來打算用它做斷言（若是，補上斷言；若否，刪除賦值），不要只為了消警告而刪。
- **產出**：`tests/backtest/test_market_calendar_bounds.py`。
- **驗證方式**：`ruff check --select F core tasks tests scripts` 零警告；該測試仍通過。
- **相依**：無。
- **完成紀錄（2026-09-16）**：`api`（`_EmptyAPI` 實例）是早期草稿殘留——`get_last_trading_date()` 以 `isinstance(api, StockPriceAPI)` 分派，測試後來改用 `_Typed` 子類別，替身就沒再用到。刪除替身，改在 `has_data` 替身記錄被查的日期，斷言「恰好查滿 `MAX_LOOKBACK_DAYS` 天就停」，把測試名稱承諾的「有界」直接驗掉。`ruff check --select F core tasks tests scripts strategy_lab` 零警告。

## S7. FinMind 券商分點 crawler 不再把錯誤當成沒有資料 ⬜

- **目的**：權限不足、連線失敗、FinMind 回傳非預期格式時，讓那個組合記成失敗、行程非零結束，而不是安靜地記成「沒有資料」。
- **做法**：
  - `FinMindCrawler.crawl_broker_trading_daily_report()`：保留配額用盡轉成 `FinMindQuotaExhaustedError` 的分支；其餘例外**往外拋**（或包成專用例外），只有 API 正常回傳空表才回 `None`。
  - `BrokerTradingUpdater.update_combination()` 既有的 `except Exception` 會把拋出的例外記成 `UpdateStatus.ERROR`，`update()` 跑完後依 `error_count` 拋 `DataLoadError`——確認這條路徑因此生效。
  - **權限不足會對每個組合都失敗**：考慮在批次開始前先以單一組合探測，權限錯誤時直接中止並給出清楚訊息，不要讓數十萬個組合各失敗一次。
  - 同檔其他 `crawl_*` 方法若有相同的吞錯誤寫法，一併檢查。
- **產出**：`core/pipeline/tw/crawlers/finmind_crawler.py`、`core/pipeline/tw/updaters/finmind/broker_trading_updater.py`、`tests/test_finmind_broker_trading_batch.py`。
- **驗證方式**：替身 API 拋 `Exception("Your level is register")` 時，組合狀態為 `ERROR`、`update()` 拋 `DataLoadError`；替身回空 DataFrame 時仍為 `NO_DATA`；配額用盡仍走等待重試。
- **相依**：無；S4 的冒煙腳本在帳號等級不足時應能據此給出清楚錯誤。

## S8. 入庫摘要區分「新寫入」與「整檔已存在」 ⬜

- **目的**：`finish_load()` 的摘要要能分辨「真的寫進新資料」與「重跑、全部已存在」，否則讀 log 看不出這次更新有沒有東西進來。
- **做法**：
  - `MonthlyRevenueReportLoader.add_to_db()`：`insert_or_ignore()` 回傳 `inserted == 0` 時計入 `skipped_files`，不計入 `file_cnt`（比照 price／chip／margin loader）。
  - `StockDividendLoader`／`CorporateActionLoader`：兩者是「跨檔合併後整批 `insert_or_replace`」，`file_cnt` 是讀檔數。改為以寫入前後的列數差（或 `insert_or_replace` 前查既有鍵數）回報「新增列數」，並在摘要中把「讀取 N 檔」與「新增 M 列」分開，不要再用 `succeeded` 表達讀檔數。
- **產出**：上述三支 loader 與對應測試（`tests/test_dao_monthly_revenue.py`、`tests/test_dao_stock_dividend_corporate_action.py`）。
- **驗證方式**：同一批 CSV 入庫兩次，第二次摘要的新寫入為 0；擷取 loguru 訊息斷言摘要字串。
- **相依**：無。

# ETL整批取不到資料記為失敗

## Abstract

- **背景／問題**：`update-db` 的 `target_guard()` 只在 updater 拋例外時才把 target 記進失敗清單；
  `unreachable`（站方被擋、逾時、HTTP 錯誤）只會變成一行 WARNING。2026-10-02（台北 08:00）的排程在主機開著 VPN 時執行，
  櫃買中心與期交所的請求全部回 HTTP 403，`price`、`chip`、`margin`、`dividend`、`corporate_action` 的本批統計全是
  `0 ok / N unreachable`，**卻都列在「成功」**。`price` 一筆都沒更新，是靠實盤尾盤段的資料新鮮度檢查才擋下。
- **目標**：拿不到「最近一個應有資料的交易日」時，該 target 進入失敗清單、`update-db` 非零結束；
  失敗清單可以拿來判斷資料到底有沒有更新。
- **範圍界線**：不改重試策略——單日偶發的 `unreachable` 仍維持「下次重試」；不處理 `finmind` 恆失敗造成退出碼恆為 1 的問題；
  期貨線（`futures_price` 的「未取得日盤」）只做檢查，有同樣問題時另立步驟。
- **驗收標準**：整批 403 時 target 進失敗清單且 `main()` 非零結束；休市日與單日偶發 `unreachable` 不觸發；既有測試全數通過；期貨行情同樣不再假綠燈（S2）。

> 本文件的唯一步驟原為 `ETL休市日誤判與回應日期核對.md` 的 S6（2026-10-02 立項）。該文件在 S1～S5 完成後於 2026-10-04
> 整份移出 backlog，S6 當時在另一條分支上、沒有一起帶進 main，2026-10-05 移到本文件並改編為 S1。

## 進度追蹤

| 編號 | 步驟名稱 | 產出檔案 | 驗證方式 | 狀態 | 備註／中斷點 |
|------|----------|----------|----------|:----:|--------------|
| S1 | 整批 unreachable 的 target 記為失敗 | `core/pipeline/shared/base_updater.py`、三支區間 updater、`tests/test_unreachable_batch_fails.py` | 13 條新測試＋四種突變各自轉紅；`tests/test_partial_market_guard.py` 五條改為預期失敗；全套 2,562 passed | ✅ | 2026-10-05 完成。判準與原規格略有偏離，見步驟章節 |
| S2 | 期貨行情被擋時不再當成非交易日 | `futures_price_crawler.py`、`futures_price_updater.py`、`tests/test_futures_price_*.py` | 8 條新測試＋七種突變各自轉紅；正式資料驗證日曆前提；全套通過 | ✅ | 2026-10-05 完成。另補「HTTP 200 空頁」的擋流量（原規格只涵蓋請求失敗） |

## S1. 整批 unreachable 的 target 記為失敗 ✅

- **目的**：2026-10-02 的排程 `update-db` 在主機開著 VPN 時執行，櫃買中心 301 次、期交所 34 次請求全部回 HTTP 403，
  證交所的頁面解析不出表格。`price`、`chip`、`margin`、`dividend`、`corporate_action` 的本批統計全是 `0 ok / N unreachable`，
  但 `target_guard()` 只在 updater 拋例外時才把 target 記為失敗，所以收尾的失敗清單只有 `finmind`、`futures_margin`、
  `futures_stock_universe`，**五個實際上一筆都沒更新的 target 被列在「成功」**。同一天的實盤尾盤段被資料新鮮度擋下
  （`price` 停在 9/30），見 `實盤下單架構規劃.md` Phase7-1 的演練紀錄。
  `update-db` 的退出碼本來就因為 `finmind` 恆為 1，再加上這個假綠燈，失敗清單與退出碼都無法拿來判斷資料到底有沒有更新。
- **做法**：
  - updater 統計結束後，若**最近一個應有資料的交易日**被判為 `unreachable`（或本批 `ok == 0` 且 `unreachable > 0`），
    拋出既有的 `DataLoadError` 或回傳失敗旗標，讓 `target_guard()` 把 target 記進失敗清單；
    單日偶發的 `unreachable` 仍維持「下次重試」，只有拿不到最新一天才算失敗。
  - 這條判準要在休市日改判為查無資料之後才有意義：之前 243 個平日休市日天天被判成 `unreachable`，會讓 `price` 每天失敗。
    該前置已完成（`edb8be6`），施作前先翻主目錄最近幾天的 `update-db` 日誌，確認每日統計不再出現休市日的 `unreachable`。
  - 期貨線（`futures_price` 的「未取得日盤」）同一晚也全數失敗而被列為成功；做這一步時順帶確認期貨 updater
    是否有同樣的問題，有的話另立步驟。
- **產出**：`core/pipeline/shared/base_updater.py`（統計物件提供判斷）、`tasks/update_db.py` 或各 updater 的收尾、測試。
- **驗證方式**：新測試，以假 crawler 讓整批回 403，target 進入失敗清單、`main()` 非零結束；
  休市日與單日偶發 `unreachable` 不觸發；既有測試（`test_crawl_result_semantics.py`、`test_date_gap_backfill.py`、
  `test_entrypoint_and_logging.py` 等）全數通過。
- **相依**：無未完成的前置。會改每日資料更新，**在 worktree 開發，避開 `實盤下單架構規劃.md` Phase7-1 演練的排程時段部署到主目錄**。

> **✅ 完成紀錄（2026-10-05）**
> - 前置確認：主目錄 10/5 的排程統計沒有休市日被判成 `unreachable`（`price` 0 requested、`day_trade_list` 4 ok）。
> - 實作：`UpdateStats.mark_day()` 記下逐日來源每天的最終結果，`failure_reason()` 判斷；
>   `BaseDataUpdater.raise_if_unreachable()` 在入庫、統計行之後拋 `DataLoadError`。
>   `DailyTwoMarketUpdater`（price／chip／margin／兩份名單）與 dividend、corporate_action、mrr 的收尾各呼叫一次。
> - **偏離原規格**：
>   1. 逐日來源的判準是「最近一個**應有資料**的日子取不到」，排除休市日與**台北的今天**——原規格的「`ok == 0` 且 `unreachable > 0`」
>      會在盤中手動執行時誤報：上櫃未公布當日資料時回別天的頁面，日期核對不符而判為取不到。
>      排程主機在美東時區，`date.today()` 是台北的前一天，所以排程跑的最新候選日不會被這條排除。
>   2. 「整批 0 ok」判準只留給以年、月為單位查詢的來源，它們每次請求都涵蓋到最新日期。
> - 驗證：`tests/test_unreachable_batch_fails.py` 13 條（判準 9 條、updater 3 條、`update_db.main()` 端到端 1 條）；
>   突變——拿掉逐日收尾拋出、不排除今天、不排除休市日、拿掉整批判準、拿掉 dividend／corporate_action 的拋出，各自轉紅。
>   `tests/test_partial_market_guard.py` 中「最新一天取不到」的五條改為預期 `DataLoadError`（不入庫的斷言保留）。
> - 現行做法寫入 `docs/pipeline/etl-ingestion.md` §3.2。mrr 只有判準層測試，updater 層未另寫。

## S2. 期貨行情被擋時不再當成非交易日 ✅

- **目的**：`FuturesPriceUpdater.update_product()` 的候選日拿不到日盤時，等待後重試一次，仍為空就當成非交易日：
  不入庫、不記失敗，只有連續 `EMPTY_PRODUCT_ABORT_THRESHOLD` 個候選日都空才中止。日常一次只跑一兩天，碰不到門檻，
  所以 2026-10-02 期交所全數回 HTTP 403 時，`futures_price` 同樣列在成功。
- **做法**：crawler 回報「被擋／連不上」與「站方明確無資料」的區分（比照台股的 `CrawlResult` 三態）；
  updater 收尾時若**最近一個應有資料的候選日**是被擋而非無資料，拋 `ProductUpdateError` 或 `DataLoadError`。
  交易日判斷可借 `market_holiday` 表，避免把真正的非交易日算成失敗。
- **產出**：`core/pipeline/tw/crawlers/futures_price_crawler.py`、`core/pipeline/tw/updaters/futures_price_updater.py`、測試。
- **驗證方式**：新測試——最新候選日兩次皆被擋時 target 失敗；休市日、只缺夜盤以外的正常情況不觸發；既有期貨 ETL 測試全數通過。
- **相依**：S1（共用判準的思路）。會改每日資料更新，在 worktree 開發、避開 `實盤下單架構規劃.md` Phase7-1 演練時段部署。

> **✅ 完成紀錄（2026-10-05）**
> - crawler：`crawl_futures_price()` 改走 `RequestUtils.fetch()` 並回 `CrawlResult`；請求失敗與解析器出錯為 `FAILED`，
>   頁面無行情表為 `NO_DATA`（`parse_quote_page()`；`extract_quote_table()` 保留為只回表的薄包裝）。
> - updater：`crawl_and_clean_date()` 多回報「是否有時段取不到」；`classify_day()` 把重試後的一天歸類，
>   `update_product()` 以 `UpdateStats.mark_day()` 記下、收尾呼叫 `raise_if_unreachable()`，由 `update()` 收齊成 `ProductUpdateError`。
> - **偏離原規格（補強）**：`docs/futures/tw-futures-platform.md` 記載期交所擋流量時回 **HTTP 200 ＋ 沒有行情表的頁面**，
>   光分辨請求失敗抓不到。改以現貨交易日曆（`get_stock_trading_days()`，取自 `price` 表）判斷：兩個時段都無行情表但現貨有開市，記為取不到。
>   正式資料實查：2015 年起七個指數期貨商品在 2,863 個現貨交易日（TMF、ZEF、ZFF 自各自起點）全部有日盤，缺 0 天。
>   原規格提的 `market_holiday` 表未採用：`price` 已是本 updater 補缺口用的日曆，再引一份會有兩個日曆來源。
> - 只拿到夜盤也記為取不到（過去日期缺日盤，表停在前一天）；台北的今天照 S1 排除。表內中間的缺口不當成最近一天。
> - 驗證：新增 8 條（crawler 三態 4 條；updater 被擋、休市、空頁擋流量、台北當天只有夜盤 4 條）；全套 2,570 passed；
>   `test_night_only_day_is_not_loaded` 改為預期 `ProductUpdateError` 並改用可正常清洗的夜盤列（原本走的是清洗失敗那條路）。
>   突變——拿掉商品收尾檢查、被擋當休市、只有夜盤當休市、不看現貨日曆、缺口當最新一天、HTTP 錯誤當查無資料、解析器出錯當查無資料，各自轉紅。
> - 已知限制寫入 `docs/pipeline/etl-ingestion.md`：取不到現貨日曆或 `price` 同樣被擋時退回舊行為；股票期貨冷門合約未驗證。

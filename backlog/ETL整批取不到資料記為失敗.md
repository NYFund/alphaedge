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
- **驗收標準**：整批 403 時 target 進失敗清單且 `main()` 非零結束；休市日與單日偶發 `unreachable` 不觸發；既有測試全數通過。

> 本文件的唯一步驟原為 `ETL休市日誤判與回應日期核對.md` 的 S6（2026-10-02 立項）。該文件在 S1～S5 完成後於 2026-10-04
> 整份移出 backlog，S6 當時在另一條分支上、沒有一起帶進 main，2026-10-05 移到本文件並改編為 S1。

## 進度追蹤

| 編號 | 步驟名稱 | 產出檔案 | 驗證方式 | 狀態 | 備註／中斷點 |
|------|----------|----------|----------|:----:|--------------|
| S1 | 整批 unreachable 的 target 記為失敗 | `core/pipeline/shared/base_updater.py`、`tasks/update_db.py`、測試 | 模擬整批 `0 ok / N unreachable` 時該 target 進失敗清單、`update_db` 非零結束；休市日不觸發 | ⬜ | 2026-10-02 發現。前置的休市日改判為查無資料已完成並部署到主目錄 |

## S1. 整批 unreachable 的 target 記為失敗 ⬜

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

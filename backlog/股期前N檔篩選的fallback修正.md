# 股期前 N 檔篩選的 fallback 修正與殘留行情處置

## Abstract

- **背景／問題**：`FuturesPriceUpdater.resolve_stock_futures_products()` 在排不出流動性時，會把 `top_n` 這個**上限**整個拿掉、退回全部 320 檔標的池——限制被放大 16 倍，而且只印一行 WARNING。2026-09-18 實際踩中：預設的 `python -m tasks.update_db` 當時含 `futures_stock_price`，於是對 320 檔從 2015 年起回補，實測約 330 個交易日／小時、單一商品 11 年需 8.6 小時，全部要 100 天以上；因為它是同步阻塞，排在後面的 `futures_chip`、`futures_continuous`、`fs`、`mrr` 等 target 在 5.5 小時內完全沒被執行。該次執行留下一檔 CAF 的半段行情（1,700 個交易日，2015-01-05 ~ 2021-12-14）在 `futures_price_daily` 裡。
- **目標**：`top_n` 在任何路徑下都是上限而非建議值；排不出流動性時要嘛收緊、要嘛明確中止，不得無聲擴大請求量。同時把 CAF 的半段行情處置掉，讓「表內有沒有股期行情」重新是一個乾淨的判斷。
- **範圍界線**：**不做**股期行情的正式回補（那是 [暫緩工作彙整.md](暫緩工作彙整.md) S4，解除條件是開始開發股期策略）。**不改** `get_top_liquid_products()` 的排序口徑與 `min_days=5` 門檻。**不碰**指數期貨路徑（`FUTURES_TARGET_PRODUCTS`）。`tasks/update_db.py` 把 `futures_stock_price` 移出 `all`／`no_tick` 的部分已於 2026-09-18 完成，不列入本文件。
- **驗收標準**：S1~S3 全部 ✅ 後移出 `backlog/`。核心驗收是：在表內沒有任何股期行情的狀態下呼叫 `update_stock_futures(top_n=20)`，請求量不得超過 20 檔所需。

## 進度追蹤表

| 編號 | 步驟名稱 | 產出檔案 | 驗證方式 | 狀態 | 備註／中斷點 |
|------|----------|----------|----------|:----:|--------------|
| S1 | fallback 改為收緊而非放大 | `core/pipeline/tw/updaters/futures_price_updater.py`、`tests/pipeline/tw/updaters/test_futures_price_updater.py` | 新增單元測試：表內無股期行情時，`top_n=20` 回傳的商品數 ≤ 20 | ⬜ | 可直接開工，無前置相依 |
| S2 | CAF 半段行情的處置 | `data/db/tw_futures.db`、`data/downloads/tw_futures/price/` | `get_top_liquid_products(20)` 的回傳與預期一致 | ✅ | 2026-09-18 使用者裁示刪除，已執行並驗證（見 S2 章節） |
| S3 | 同步改寫 S4 的暖身做法 | [暫緩工作彙整.md](暫緩工作彙整.md) S4 章節 | S4 的做法第 1 點與 S1 修好後的實際行為一致 | ⬜ | 相依 S1、S2 |

## S1. fallback 改為收緊而非放大 ⬜

- **目的**：讓 `top_n` 在所有路徑下都是請求量的上限。

- **現況（2026-09-18 對照程式）**：`resolve_stock_futures_products()` 的邏輯是

  ```python
  if top_n:
      liquid: List[str] = universe_api.get_top_liquid_products(top_n, end_date=date)
      if liquid:
          return liquid
      logger.warning("表內還沒有股期行情，排不出流動性；本次改取整份標的池（之後再用 top_n 篩）")
  return universe_api.get_products(date)
  ```

  `get_top_liquid_products()` 的成交量取自 `futures_price_daily`，且要求每檔至少 `min_days=5` 個交易日，所以**表內沒有股期行情時必然回空**，接著就落到整份 320 檔。這個雞生蛋是設計上已知的，問題在**解法選錯方向**：呼叫端要的是「最多 20 檔」，拿不到排序依據時給出 320 檔，是把限制放大而不是縮小。

- **為什麼一行 WARNING 擋不住**：它被淹沒在每個「商品 × 日」兩行的 INFO 裡（當天 `update_futures_price.log` 長到 2.6 MB），而且警告文字「之後再用 top_n 篩」讀起來像是良性降級，不像是「本次請求量放大 16 倍」。

- **做法**：三選一，建議走第一種。
  1. **退回標的池的前 `top_n` 檔**（改動最小）：`return universe_api.get_products(date)[:top_n]`。標的池順序不是流動性順序，所以這批只是暖身樣本，但請求量受 `top_n` 約束；warning 文字要改寫成「本次改取標的池前 N 檔暖身，順序非流動性」。
  2. **直接中止並要求明確指定**：回空 list，由 `update_stock_futures()` 既有的「標的池是空的」分支延伸出一個新訊息，要求呼叫端給 `products=`。語意最清楚，但會讓「什麼都不想，先跑起來」變成不可能。
  3. 兩者合併：`top_n` 有值就走 1，並在 log 印出本次實際會送出的請求數估算（商品數 × 交易日數 × 2），讓量級在開跑前就看得見。
- **產出**：`core/pipeline/tw/updaters/futures_price_updater.py` 的 `resolve_stock_futures_products()`；`update_stock_futures()` docstring 中「⚠️ 不要一次爬 320 檔」那段要改寫——修好之後那個警告不再需要靠人記得。
- **驗證方式**：新增單元測試，以空的 `futures_price_daily` 呼叫 `resolve_stock_futures_products(top_n=20, date)`，斷言回傳長度 ≤ 20（走做法 2 則斷言為空）。既有測試不得回歸。
- **相依**：無。

## S2. CAF 半段行情的處置 ✅

> **✅ 完成紀錄（2026-09-18）**
> - 使用者裁示**只刪 CAF**，`CDF`／`EEF`／`NYF` 的 5 列驗證足跡保留。
> - 已執行：`DELETE FROM futures_price_daily WHERE product='CAF'`（8,500 列）、刪除 `data/downloads/tw_futures/price/CAF_day_*.csv`（1,730 檔、6.8 MB）。
> - 驗證結果：表內 CAF 列數為 0；`get_top_liquid_products(20)` 回傳 `[]`（刪除前是 `['CAF']`）；七檔指數期貨最新日仍為 2026-09-18，未受影響。

- **目的**：決定 2026-09-18 那次中斷留下的 CAF 資料要刪還是留，並讓決定留下紀錄。

- **現況（2026-09-18 刪除前實測，保留為背景）**：

  | 位置 | 內容 |
  |------|------|
  | `futures_price_daily` | CAF 1,700 個交易日，2015-01-05 ~ 2021-12-14，區間內對照 TX 日曆**零缺口**，2021-12-14 之後到今日全無 |
  | `data/downloads/tw_futures/price/` | 1,730 個 `CAF_day_*.csv`（到 2022-01-26），其中約 30 檔已爬未入庫；無 `CAF_night_*.csv` |
  | `futures_continuous` | 只有 7 檔指數期貨，**未受污染** |

- **問題在哪**：這是「看起來有、其實只有半套」的資料，而且它正好污染了判斷股期能不能開跑的那個判準。修正前 `get_top_liquid_products(20)` 回空、會印警告；現在它回傳 `['CAF']`——只有 CAF 過得了 `min_days=5` 門檻（CDF 2 日、NYF 2 日、EEF 1 日都不夠）。於是：
  - fallback 不再觸發，警告消失，問題從「很吵但看得見」變成「安靜且看不見」。
  - [暫緩工作彙整.md](暫緩工作彙整.md) S4 的做法第 2 點寫「確認 `get_top_liquid_products()` 回傳非空之後，定 N 與起點再開正式回補」——這個判準現在會被一檔殘料滿足，而 CAF 之所以第一個被爬，只是因為它排在標的池順序的最前面，不是因為它流動性高。
  - 任何以 `futures_price_daily` 為來源的下游（回測、連續合約建構）若取到 CAF，2021-12-14 之後看到的是「沒有資料」而不是「沒有交易」，兩者在回測裡的行為不同。

- **做法**：二擇一。
  - **刪除（建議）**：`DELETE FROM futures_price_daily WHERE product = 'CAF'`，並清掉 `data/downloads/tw_futures/price/CAF_day_*.csv`。狀態回到乾淨的「表內完全沒有股期行情」，S1 的修正也才能在真實情境下被驗證。代價只有重爬那 1,700 天所需的約 4.5 小時，而 CAF 不見得在真正想要的前 N 檔名單內。
  - **保留**：留著當續跑起點（`update()` 的 `resume=True` 會從 2021-12-14 接續）。省下 4.5 小時，但必須接受上述判準被污染，且 S3 要改寫 S4 的判準，不能再用「回傳非空」當開跑條件。
- **產出**：刪除則是資料庫與 downloads 的清理；保留則在本節補一段裁示紀錄與絕對日期。
- **驗證方式**：刪除後 `SELECT COUNT(*) FROM futures_price_daily WHERE product='CAF'` 為 0，且 `get_top_liquid_products(20)` 回空（此時應觸發 S1 修好的收緊路徑，而非退回 320 檔）。
- **相依**：無。建議在 S1 之前或同批處理——S1 的驗證需要一個沒有股期行情的表。

## S3. 同步改寫暫緩工作彙整 S4 的暖身做法 ⬜

- **目的**：S1 修好之後，[暫緩工作彙整.md](暫緩工作彙整.md) S4 的暖身指示會過期，必須同步改寫，否則解除暫緩時會照著舊做法走。
- **做法**：S4 現行做法第 1 點的兩個暖身選項，前者是「`--from <近 5 個交易日前>`，退回整份標的池是預期行為，320 檔 × 5 日約 3,200 次請求」——S1 修好後 fallback 不再退回 320 檔，這個選項的前提消失，要改寫成走 `products=` 明確指定，或依 S1 實際採用的做法重寫。做法第 2 點的「確認 `get_top_liquid_products()` 回傳非空」若 S2 選擇保留 CAF，也要改成更嚴格的判準（例如要求至少 N 檔通過 `min_days`）。
- **產出**：[暫緩工作彙整.md](暫緩工作彙整.md) S4 章節的做法第 1、2 點。
- **驗證方式**：人工對讀——S4 的做法敘述與 `resolve_stock_futures_products()` 的實際行為一致。
- **相依**：S1；S2 已於 2026-09-18 完成。

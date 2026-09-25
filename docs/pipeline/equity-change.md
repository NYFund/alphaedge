# 權益變動表（equity_change）

> 本文件描述 `equity_change` 表的**資料形狀、涵蓋範圍與已知限制**——也就是「要用這張表之前
> 必須先知道的事」。入庫批次、resume 依據與失敗語意屬於 pipeline 的共同約定，
> 寫在 [ETL 入庫約定](etl-ingestion.md)（§二對照表），本文件不重複。
>
> 各項設計的理由寫在程式碼 docstring（`FinancialStatementCleaner.clean_equity_changes()` /
> `select_equity_changes_period_table()`、`FinancialStatementUpdater.is_season_filed()`）。

---

## 一、資料形狀：長表，不是寬表

MOPS 的權益變動表原始版面是**二維矩陣**——欄是權益項目（普通股股本、資本公積…），
列是變動原因（期初餘額、本期淨利…）。入庫時攤平成長表，**一列 = 一個「權益項目 × 變動原因」的組合**。

| 欄位 | 型別 | 說明 |
|------|------|------|
| `year` | `INT NOT NULL` | 年 |
| `season` | `INT NOT NULL` | 季（1~4） |
| `stock_id` | `TEXT NOT NULL` | 股票代號 |
| `權益項目` | `TEXT NOT NULL` | 來源表的**欄**：普通股股本、資本公積、保留盈餘合計、庫藏股票… |
| `變動原因` | `TEXT NOT NULL` | 來源表的**列**：期初餘額、本期淨利（淨損）、普通股現金股利、期末餘額… |
| `金額` | `REAL` | **單位：新台幣仟元** |

**主鍵**：`(year, season, stock_id, 權益項目, 變動原因)`。

**為什麼不用寬表**：各公司的權益項目集合差異極大——例如 2330 台積電 15 項、
2891 中信金 17 項（多出「特別股股本」「採用覆蓋法重分類之其他綜合損益」）、6488 環球晶 14 項。
寬表得存全市場的欄位聯集，且每遇到一個新項目就要改 schema；長表的欄位固定六個，不隨資料成長。

**為什麼主鍵不含 `公司名稱`**（其他三張財報表有）：`ajax_t164sb06` 是逐檔查詢，
回傳的只有報表矩陣本身，**來源端點根本不給公司名稱**；其他三張報表的 `公司名稱` 來自全市場表格的欄位。
若硬要補，只能去 `taiwan_stock_info` 查現名，那是「現在的名稱」而非「當季的名稱」，
寫進主鍵反而會製造假的唯一性。需要公司名稱請自行 join `taiwan_stock_info`，並知道它是現名。

### 讀取方式

`FinancialStatementAPI` 以 `table_name` 為參數，`equity_change` 沿用同一組介面：

```python
from core.api.tw.financial_statement_api import FinancialStatementAPI
from core.config import EQUITY_CHANGE_TABLE_NAME

api = FinancialStatementAPI()
df = api.get(EQUITY_CHANGE_TABLE_NAME, year=2020, season=1)
```

### 「沒有這一列」與「金額是 0」是兩件事

- **來源表的空白儲存格不入表**：該公司若根本沒有這個權益項目，就完全不會有那些列。
  例如 2330 台積電沒有庫藏股票，`WHERE stock_id='2330' AND 權益項目='庫藏股票'` 查出來是 **0 列**。
- **來源表明確填 0 的會入表為 `0.0`**，而且占多數——大多數「權益項目 × 變動原因」的組合
  在單一季度並沒有發生變動。
- **`金額` 不會出現 `NULL`**，沒有整排 `NULL` 的佔位列。

所以 `COUNT(*)` 不等於「有變動的項目數」，要濾 `金額 != 0`。
而「查無此列」有兩種可能——該公司沒有這個項目、或**該年季根本還沒回補**（見§二），
兩者在 DB 裡長得一樣。**做除法或累加前先確認該年季已入庫。**

---

## 二、涵蓋範圍

| 項目 | 說明 |
|------|------|
| 目標區間 | 2013Q1 起（`DEFAULT_START_YEAR`）至最新已申報年季 |
| 尚未申報的年季 | 由 updater 以 `not yet filed, skipped` 自動跳過 |
| 每季檔數 | 隨上市家數逐年遞增；與 `balance_sheet` 同季的檔數可當成涵蓋率的獨立對照 |

確認本地涵蓋：

```sql
SELECT year, season, COUNT(DISTINCT stock_id) AS stocks, COUNT(*) AS rows
FROM equity_change GROUP BY year, season ORDER BY year, season;
```

---

## 三、已知限制

以下是資料本身的限制，不是待修的 bug。

1. **已下市公司只補得到資產負債表有的那些**。權益變動表是逐檔查詢，每一季的爬取清單是
   `taiwan_stock_info` 的**現況**（`type IN ('twse','tpex')`、排除 ETF、代號為 4 碼數字）
   **聯集當季資產負債表的 4 碼代號**（`get_season_target_stock_ids()`）。只用現況清單會有
   **倖存者偏誤**：FinMind 台股總覽只收錄一部分已下市公司，當年存在、後來下市的公司會整段
   缺權益變動表，而且不會有任何錯誤；資產負債表是全市場查詢，當季有申報的公司都在裡面，
   併進來就補得到。當季資產負債表還沒入庫、或該公司連資產負債表都沒有時，仍然漏；
   呼叫端明確指定 `stock_ids` 時只爬那份清單，不做聯集。
2. **不含興櫃**（`emerging`）。
3. **「查無資料」的公司在表裡不留列**，與「還沒爬」無法從資料表區分。
   查無資料的年季 × 個股另外記在
   `data/downloads/tw_stock/meta/no_data/equity_change_season_progress.json`，重跑不再重打。
   **只有申報期已關閉的年季才記**（各行業最晚期限 ＋ 30 天寬限）：財報逐家公司申報，
   申報期間的「查無資料」多半只代表那家還沒送件，這時寫進永久名單，它送件之後再也不會被抓。
4. **站方過載造成的暫時性失敗只在 log 尾端彙總**（`N requests unreachable after retries`）。
   重跑即可補上，但**必須真的去看那行 log**——不看就會變成「跑完了卻莫名少了幾百檔」。
5. **少數公司的 2013~2014 年季，本端點不供應**。1107 建台、1606 歌林、2381 華宇、2396 精碟、
   3426 台興、6457 紘康的 2013~2014 年季向 `ajax_t164sb06` 查詢時，站方回 HTTP 200 並明示：

   ```
   公開發行公司103年(含)以前之財報資料請至採IFRSs前之 個別報表 或 合併報表 查詢！
   ```

   民國 103 年 ＝ 西元 2014 年。這**不是暫時性失敗，重跑無用**；同一組參數查這幾檔的 2015 年之後
   一切正常。要取得這些年季需改用「採 IFRSs 前」的另一組端點（新端點、新版面、新 cleaner），
   **目前不在任何計畫內**。實務影響很小：只有 Q2／Q4 撞到（這幾檔當年只申報半年報與年報）。
6. **上面那幾筆記為「確認無資料」**，寫進進度檔的 `no_data`，重跑不再重打。
   crawler 的判準是**頁面解不出任何表格、且含「採IFRSs前」片段**
   （`FinancialStatementCrawler.EQUITY_CHANGE_PRE_IFRS_MARKER`）。兩個條件缺一不可：
   - 只看片段的話，正常報表頁的導覽或註腳出現這幾個字，就會把有資料的公司永久記成查無資料。
   - 解不出表格但不含這個片段時，照樣算「待重試」——那代表版面改了或拿到錯誤頁，要人看。

### 兩道刻意設計的防線

- **抓錯期別寧可回空表**：MOPS 同一頁附了去年同季的比較表，版面與本期表一模一樣，
  只有 column 第一層的期別標籤能區分。`select_equity_changes_period_table()`
  以該標籤比對，**比對不到就回傳空表，不做退而求其次的 fallback**——抓錯會把去年的數字記成今年，
  且因主鍵相同會安靜地佔住正確資料的位置。

  **標籤只有 Q1 是「第N季」**：

  | 季別 | MOPS 實際標籤 |
  |:----:|---------------|
  | Q1 | `民國109年第1季` |
  | Q2 | `民國109年上半年度` |
  | Q3 | `民國109年前3季` |
  | Q4 | `民國109年度` |

  對照表在 `FinancialStatementCleaner.EQUITY_CHANGE_PERIOD_LABELS`。
  寫死成「第N季」會讓 Q2~Q4 全數清成空表卻不報錯——統計行的 `cleaned empty` 就是為此而加。
- **全形／半形括號正規化**：同一項目跨年度會擺盪（「權益增加（減少）總額」與
  「權益增加(減少)總額」），一律過 `DataUtils.standardize_column_name()`，
  否則同一張表裡會變成兩個項目。

---

## 四、爬取節流

`equity_change` 是 fs 裡唯一逐檔查詢的報表，一個年季要打兩千多次請求，
節流直接決定回補要跑幾天。**常數為權益變動表專用**，與其他三張報表不共用——
那三張是「全市場一次查完」，整段回補才幾十次請求，沒有放寬的必要。

| 常數（`FinancialStatementUpdater`） | 現行值 |
|---|---|
| `EQUITY_CHANGE_RANDOM_DELAY_MIN` / `_MAX` | 0.5 / 1.5 秒 |
| `EQUITY_CHANGE_BATCH_SLEEP_EVERY_N_FILES` | 50 檔 |
| `EQUITY_CHANGE_BATCH_SLEEP_DURATION_SECONDS` | 15 秒 |

一個年季（約兩千檔）約 1.5 小時。現行值在全市場回補中實測 `unreachable = 0`，不需要調回更保守的設定。

⚠️ **每個年季跑完要看收尾那行**
`N requested / N ok / N no data / N unreachable / N cleaned empty`：
`unreachable` 明顯大於 0 代表節流放太寬，退回中間值（1~3 秒 / 每 30 檔睡 15 秒）；
`cleaned empty` 大於 0 代表版面與預期不符，那是**程式要修**，不是重跑能解決的。

### 中斷與續跑

整段回補以十萬次請求、數十小時計，中斷是常態。**直接 Ctrl+C 或 `kill <pid>` 即可**：

- 手上那批（最多 100 檔）會**先入庫**、進度存檔、印完統計行才離開，結束碼 0。
- 急著離開就再按一次 Ctrl+C——那時尚未入庫的那批會遺失，下次重跑會重爬。
- 重跑不需要任何參數：已入庫的公司（查資料表）與已確認沒資料的公司（查進度檔）
  都不會重打，已落地未入庫的 CSV 會先自動對帳補進資料庫。

實作見 `core/pipeline/shared/graceful_stop.py` 與 `core/pipeline/shared/season_planner.py`。

**第一批資料要等 100 檔之後才落地**（`EQUITY_CHANGE_LOAD_BATCH_SIZE`），約 3 分鐘。
啟動後前幾分鐘查 DB 是 0 列屬正常，不是失敗。

---

## 相關文件

- [ETL 入庫約定](etl-ingestion.md)——§二各 updater 對照表、§四新增 updater 的檢查表
- [資料覆蓋範圍](../exchanges/data_coverage.md)——各資料來源的時間涵蓋與已知限制
- [指令教學](../commands/command-usage.zh-TW.md)——`update_db --target fs` 的用法

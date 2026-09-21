# Shioaji 升級至 1.7

## Abstract

- **背景／問題**：本機 `.venv` 與鎖定檔的 shioaji 是 **1.3.3**，PyPI 最新為 **1.7.5**（2026-09-21 查詢），中間跨了 1.5、1.7 兩個系列。實盤下單（[實盤下單架構規劃.md](實盤下單架構規劃.md)）已完成的步驟中，有一批結論是**讀 1.3.3 原始碼或以 1.3.3 實測得出的**，例如 `MultiContract.__getitem__` 查不到回 `None`、`StockOrderCond` 沒有 `SBLShort`、`login()` 的 `receive_window`。版本越晚升，要重新驗證的結論越多；而借券（Phase6-1）在 1.3.3 下根本送不出去。
- **目標**：shioaji 升到 1.7.x 最新版，`core/` 所有用到 shioaji 的路徑（`core/broker/tw/`、`core/utils/` 的回呼與常數、`core/pipeline/tw/` 的 tick 爬取）都在新版下驗證過；實盤文件中依賴 1.3.3 行為的結論逐條重驗並改寫。
- **範圍界線（不做）**：
  - **不借升版之便加新功能**：新版的新能力（Contract V2 的 lazy lookup／update events、即時 KBar、市場訊號等）若值得採用，記在本文件末尾另案處理，不在升版時順手接上。
  - **不重寫實盤架構**：只修因 API 改變而失效的地方，`BaseBroker` 介面維持不變。
  - **不升級其他套件**：只以 `uv lock --upgrade-package shioaji` 升 shioaji 與它強制要求的傳遞相依，其他套件版本不動。
- **驗收標準**：
  1. `uv.lock` 的 shioaji 為 1.7.x，`pyproject.toml` 的下限同步提高。
  2. `uv run pytest`（含 `slow`）全綠，包含 `tests/live/` 與 `tests/test_order_state_parity.py`。
  3. 模擬環境實連：`manual_shioaji_login.py` 登入成功、`manual_shioaji_test_order.py` 股票委託送出並收到回報、`manual_shioaji_quote_record.py` 錄到的新行情能以 `core/broker/tw/quote_replay.py` 重放。
  4. S2 的重驗清單每一條都有新版結論，並已回寫到實盤文件。

---

## 進度追蹤表

| 編號 | 步驟名稱 | 產出檔案 | 驗證方式 | 狀態 | 備註／中斷點 |
|------|----------|----------|----------|:----:|--------------|
| S1 | 1.3.3 → 1.7.5 差異盤點 | 本文件〈差異盤點〉章節 | 差異表涵蓋 `core/` 每個用到的 shioaji 符號 | ⬜ | 相依 [改用uv管理套件.md](改用uv管理套件.md) S3 |
| S2 | 依賴 1.3.3 行為的結論逐條重驗 | 本文件〈重驗清單〉章節 | 清單每一條都有 1.7.5 下的結論與佐證（原始碼位置或實測輸出） | ⬜ | 相依 S1 |
| S3 | 升級鎖定版本並修正程式 | `pyproject.toml`、`uv.lock`、`core/broker/tw/*`、`core/utils/callback.py`、`core/utils/constant.py`、相關測試 | `uv run pytest -m "not slow"` 全綠 | ⬜ | 相依 S2；`tests/test_order_state_parity.py` 預期會先紅 |
| S4 | 離線驗證：測試全套與行情重放 | 無（驗證步驟） | `uv run pytest` 含 `slow` 全綠；1.3.3 時期錄的行情檔可重放，或不能重放的原因已記錄 | ⬜ | 相依 S3 |
| S5 | 模擬環境實連驗證 | 本文件實測紀錄 | 驗收標準第 3 條 | ⬜ | 相依 S4；**必須在交易日盤中**跑 |
| S6 | tick 爬取路徑驗證 | `core/pipeline/tw/crawlers/*`（如需修正） | `scripts/manual/manual_tick_crawler.py` 抓得到一檔股票與一檔期貨的 tick，欄位與舊版一致 | ⬜ | 相依 S3；可與 S5 同一天做 |
| S7 | 回寫實盤文件並解除借券阻塞 | `backlog/實盤下單架構規劃.md`、`backlog/index.md` | 實盤文件不再有「鎖定版 1.3.3」的過時敘述；Phase6-1 的借券前提更新 | ⬜ | 相依 S2、S5 |

---

## 步驟詳述

### S1. 1.3.3 → 1.7.5 差異盤點 ⬜

- **目的**：升版前先知道哪些東西會變，而不是升完看測試哪裡紅。測試只覆蓋得到有寫測試的路徑，回呼、行情推播這類要實連才會跑到的地方，紅燈不會出現。
- **做法**：
  - 列出 repo 內所有 import shioaji 的檔案與用到的符號（`grep -rn "shioaji\|sj\." core scripts tests run.py`），整理成「符號 → 使用位置」表。目前涉及 `core/broker/tw/` 的六個模組、`core/utils/`（`account.py`、`callback.py`、`constant.py`、`instrument.py`、`order.py`）、`core/pipeline/tw/` 的 tick 爬取與更新、`core/live/factory.py`、`core/dao/tw/live_trade_dao.py`、`core/config/settings.py`、`run.py`。
  - 取得兩個版本的原始碼並排比對：`uv venv <scratch>/sj133 && uv pip install --python <scratch>/sj133 shioaji==1.3.3`，1.7.5 同理。只比上表用到的符號：類別與欄位（`Contract`、`StockOrder`／`FuturesOrder`、`stream_data_type`）、常數 Enum（`OrderState`、`StockOrderCond` 等）、方法簽章（`login`、`place_order`、`update_status`、`quote.subscribe`、`ticks`）。
  - 查閱官方 release notes／changelog，補上原始碼比對看不出的行為變動（例如預設值、伺服器端規則）。
  - **使用 Shioaji Claude Code plugin**（`claude plugin install shioaji`，已安裝）：它的 skill 涵蓋新版 API 用法、Contract V2 lazy lookup、migration 與 troubleshooting，適合回答「1.7.5 的正確寫法是什麼」。**它描述的是最新版，不會告訴你 1.3 與 1.7 之間改了什麼**，所以差異本身仍以原始碼比對與 changelog 為準，plugin 用來確認新寫法。
  - 已知的一項：`OrderState` 在 1.7 起改由原生模組提供、不再是 Python Enum（`tests/test_order_state_parity.py` 與 CI 註解已記錄），會影響 `core/utils/callback.py` 的 `order_cb` 比較前提。
- **產出**：本文件新增〈差異盤點〉章節（符號、1.3.3 行為、1.7.5 行為、影響位置、處置）。
- **驗證方式**：「符號 → 使用位置」表中每個符號都在差異表出現，註明「無變動」或具體變動。
- **相依**：[改用uv管理套件.md](改用uv管理套件.md) S3（以 uv 建立對照環境並用 `uv lock --upgrade-package` 升版）。

### S2. 依賴 1.3.3 行為的結論逐條重驗 ⬜

- **目的**：實盤文件中有一批設計決定建立在 1.3.3 的行為上，行為變了，決定可能跟著失效，而且多半是安靜地失效。
- **做法**：以 1.7.5 逐條重驗下表（來源皆為 [實盤下單架構規劃.md](實盤下單架構規劃.md)），能離線查的讀原始碼，要連線的併入 S5：

  | 項目 | 1.3.3 的結論 | 出處 | 重驗方式 |
  |------|--------------|------|----------|
  | `OrderState` 型別 | 是 `str` Enum，`order_cb` 以 `==` 比對字串值 | Phase1-1、`tests/test_order_state_parity.py` | 讀原始碼；已知 1.7 改變 |
  | `StockOrderCond` 成員 | 只有 `Cash`／`MarginTrading`／`ShortSelling`，沒有 `SBLShort`／`SBLShortPriceExempt` | Phase1-1、Phase2-4、Phase6-1 | 讀原始碼 |
  | 其他下單參數 Enum 值 | 與專案自訂 Enum 字串值一致 | Phase1-1 | `tests/test_order_state_parity.py` |
  | `MultiContract.__getitem__` | 查不到回 `None`、不拋例外 | Phase2-3 | 讀原始碼；**Contract V2 可能改變查詢方式** |
  | 合約查詢鍵 | slot 以 `symbol` 命名、`_code2contract` 以 `code` 索引 | Phase2-3 | 讀原始碼 |
  | `api.Contracts.Stocks[...]` | 一層涵蓋上市與上櫃 | Phase2-3 | 讀原始碼 |
  | 合約檔下載阻塞 | `_block()` 最多等 30 秒，決定 `contracts_timeout` | Phase2-3 | 讀原始碼；lazy lookup 下可能不再整批下載 |
  | `login(receive_window=)` | 伺服器把關秒級時鐘偏差 | Phase2-2 | 讀簽章＋S5 實連 |
  | `Contract.update_date` | `'2026/09/21'` 斜線格式字串 | Phase0-1、Phase2-2 | S5 實連 |
  | `Contract` 的 10 個欄位 | `reference`／`limit_up`／`limit_down`／`update_date`／`day_trade`／`margin_trading_balance`／`short_selling_balance`／`unit`／`multiplier`／`underlying_code` 皆存在 | Phase2-2 | 讀 pydantic 欄位 |
  | `custom_field` 限制 | 最多 6 字元、pattern `^[ -~]*$` | Phase2-4 | 讀原始碼 |
  | 行情推播型別 | `stream_data_type` 的 `TickSTKv1`／`BidAskSTKv1`／`TickFOPv1`／`BidAskFOPv1` | Phase2-7、`core/broker/tw/quote_replay.py` | 讀原始碼＋S4 重放 |

  實盤文件中其他標註「實測」「讀原始碼」的段落若在 S1 發現相關，一併加入本表。
- **產出**：本文件〈重驗清單〉章節（上表加兩欄：1.7.5 結論、佐證）。
- **驗證方式**：每一列都填了 1.7.5 結論與佐證；要實連的列標明「待 S5」。
- **相依**：S1。

### S3. 升級鎖定版本並修正程式 ⬜

- **目的**：套用升版，並修掉 S1、S2 找出的不相容處。
- **做法**：
  - `pyproject.toml` 的 `shioaji==1.3.3` 改回下限寫法 `shioaji>=1.7`，執行 `uv lock --upgrade-package shioaji`，確認 `uv.lock` 的 diff 只動到 shioaji 與它強制要求的傳遞相依。
  - 依差異表修正 `core/broker/tw/*`、`core/utils/callback.py`（`order_cb` 在 `OrderState` 不再是 Enum 下的比較方式）、`core/utils/constant.py`（下單參數 Enum 補上新版有的值，例如 `SBLShort`）。
  - `tests/test_order_state_parity.py` 依新版型別改寫比對方式：它的用途是讓「值對不上」的安靜失效變成紅燈，改寫後這個保證必須還在。
  - 修正時查新版寫法可使用 Shioaji plugin；寫法的根據仍要回到原始碼或實測，不以 plugin 的敘述當作唯一依據。
- **產出**：`pyproject.toml`、`uv.lock`、`core/broker/tw/*`、`core/utils/callback.py`、`core/utils/constant.py`、`tests/test_order_state_parity.py` 及其他因此調整的測試。
- **驗證方式**：`uv run pytest -m "not slow" -rs` 全綠；`uv run ruff check .` 全綠。
- **相依**：S2。

### S4. 離線驗證：測試全套與行情重放 ⬜

- **目的**：在不連線的前提下盡量把問題擋在實連之前。
- **做法**：
  - `uv run pytest -rs`（含 `slow`），`tests/live/` 229 條與 `./scripts/run_regression.sh` 雙線。
  - 以 `core/broker/tw/quote_replay.py` 重放 1.3.3 時期錄製的行情檔。重放器的欄位型別取自**安裝中的** `stream_data_type`，若新版改了欄位，舊錄製檔可能無法還原：能重放 → 轉換層在新版下一致；不能重放 → 記錄是哪個欄位變了，並決定舊錄製檔要轉換還是作廢。
- **產出**：無（驗證步驟）；結果寫在本步驟章節末。
- **驗證方式**：同上兩項。
- **相依**：S3。

### S5. 模擬環境實連驗證 ⬜

- **目的**：回呼、推播、委託回報這些路徑只有實連才會跑到，也是 S2 中要連線才能重驗的那幾列。
- **做法**（交易日盤中，模擬環境）：
  1. `uv run python -m scripts.manual.manual_shioaji_login`：登入、合約檔日期檢查、印出合約 10 個欄位的值與型別，與 1.3.3 時期的實測表對照。
  2. `uv run python -m scripts.manual.manual_shioaji_test_order --confirm`：送一筆股票 ROD 限價單（跌停價買進，不會成交），確認 `order_cb` 收到回報且狀態比對成立，再撤單。期貨權限若仍未開通，期貨那筆記為「待權限」。
  3. `uv run python -m scripts.manual.manual_shioaji_quote_record`：錄一段新版行情，再以 `quote_replay.py` 重放，確認訊號與即時跑的一致。
- **產出**：本步驟章節末的實測紀錄（日期、shioaji 版本、各腳本輸出重點）。
- **驗證方式**：驗收標準第 3 條。
- **相依**：S4；Phase0-1 的帳號權限（期貨部分）。

### S6. tick 爬取路徑驗證 ⬜

- **目的**：`core/pipeline/tw/crawlers/stock_tick_crawler.py`、`futures_tick_crawler.py` 與兩個 updater 用 shioaji 抓歷史 tick，是實盤以外另一條依賴 shioaji 的路徑，容易在升版時被忽略。
- **做法**：`uv run python -m scripts.manual.manual_tick_crawler` 抓一檔股票與一檔期貨的單日 tick，比對欄位名稱與型別是否與升版前的中繼檔一致；`shioaji.data.Ticks` 若有變動，修正爬蟲與清洗器。
- **產出**：如需修正，為 `core/pipeline/tw/crawlers/*` 與對應 cleaner。
- **驗證方式**：抓得到資料，欄位與舊版一致或差異已處理；`uv run pytest tests/test_futures_tick.py` 全綠。
- **相依**：S3。

### S7. 回寫實盤文件並解除借券阻塞 ⬜

- **目的**：讓實盤文件反映新版事實，避免之後有人照著 1.3.3 的結論寫程式。
- **做法**：
  - 依 S2 重驗清單，改寫 [實盤下單架構規劃.md](實盤下單架構規劃.md) 中各項結論的版本與內容，仍成立的補註「1.7.x 重驗仍成立（日期）」，不成立的改寫並說明影響。
  - Phase6-1 的「借券要先升 shioaji」前提依 S2 結論更新；Phase0-1 記錄的 shioaji 版本改為新版。
  - `backlog/index.md` 實盤那一列的相依欄同步更新。
- **產出**：`backlog/實盤下單架構規劃.md`、`backlog/index.md`。
- **驗證方式**：`grep -n "1\.3\.3" backlog/實盤下單架構規劃.md` 的每一處都已加上新版結論，或只留作歷史脈絡並註明。
- **相依**：S2、S5。

# 台股 tick 級回測框架

## Abstract

- **背景／問題**：策略已可宣告 `self.scale`（`Scale.DAY`／`Scale.TICK`），回測引擎照它跑；但現行 `Scale.TICK` 不是逐筆模擬——
  `Backtester.run_tick_backtest()` 把**一整天的 tick 當成一根 K 棒**一次交給策略與成交模型（2026-10-10 檢查結果）：
  1. **前視**：成交驗證用全日高低點，早盤的委託可以用下午才出現的價位成交（`FillModel.on_bar_open()` 註解已自承）。
  2. **沒有時間軸**：委託不帶時間，成交對照的報價一律是該檔當天最後一筆（`execute_open_signal()` 的 `quote_map` 後蓋前）。
  3. **同一檔的報價數千筆**：停損與平倉把持倉標的整天的 tick 全交給策略，策略不自己去重就會重複送平倉單，引擎沒擋。
  4. **成交量上限未實作**：TICK 的 `volume` 是單筆量，`get_filled_volume()` 只在 DAY 生效。
  5. **`volume` 語意與實盤不同**：實盤 TICK 報價的 `volume` 是券商給的**當日累計量**（`IntradayMomentumStrategy` 依此判斷 5,000 張門檻），
     回測的 `StockQuoteAdapter.from_tick_row()` 卻是單筆量。同一個欄位兩邊意思不同，訊號不可比。
  6. 宣告 `is_tick_triggered` 的策略（實盤逐筆觸發）因為語意不同，被 `Backtester._reject_intraday_tick_backtest()` 擋下，不能做 tick 回測。
  台股 tick 歷史已在 TimescaleDB（`台股tick改用TimescaleDB.md`，2020-04-01～2024-05-10、10.5 億列），資料面已經就緒。
- **目標**：一套**逐筆重放**的 tick 級回測框架：
  - 級別由**策略宣告預設值與支援範圍**，回測時可用 `--scale` 覆寫（只能選策略支援的，否則當場報錯）。
  - TICK 回測**照時間順序一筆一筆**呼叫策略，每次 `List[StockQuote]` 長度 1——與實盤 `is_tick_triggered` 同一個契約，
    同一支策略不改程式就能回測與實盤，盤中動能策略可以直接回測。
  - **下單後同一檔的下一筆 tick 才成交**：市價買吃 `ask_price`、賣吃 `bid_price`，限價單等之後的成交價穿越才成交；沒有前視。
  - 標的範圍**兩套都做、回測前自己選**：策略盤前宣告的清單（與實盤盤前篩選一致），或全市場。
- **範圍界線**：
  - **只做台股**。期貨沒有 tick 資料（`台股tick改用TimescaleDB.md` 的期貨 tick 已裁示不做）。
  - **不做時間切片**（每 N 秒把全市場最新報價打包一次）：2026-10-10 使用者選逐筆。需要橫斷面比較的策略照實盤規則，自己在策略內保存各標的最新報價。
  - **不改 DAY 回測的結果**：DAY 路徑行為不變，回歸雙線 baseline 零變動。
  - **不做委託簿撮合、排隊位置、市場衝擊**：成交只看下一筆 tick 的成交價與 bid/ask；依下單量放大的衝擊是 `滑價模型後續優化.md` S4 的範圍。
  - 不做盤後零股、定價交易；不做實盤側的任何修改。
- **驗收標準**：
  1. 手寫 tick 的端到端測試證明：沒有前視（委託只會用下單之後的 tick 成交）、下一筆成交與 bid/ask 規則、同一份資料跑兩次結果逐位元相同。
  2. `IntradayMomentumStrategy` 以 `--scale tick` 在真實資料上跑完一段區間，兩種標的範圍各跑一次，結果與 `盤中動能策略.md` S2 的日 K 近似逐項對照、差異記錄回該文件。
  3. 現有 DAY 策略回歸雙線零變動；宣告只支援 DAY 的策略加 `--scale tick` 時當場報錯。
  4. 效能已量測並記錄：兩種標的範圍各自的「每交易日耗時」與峰值記憶體。

---

> **與其他文件的關係**
> - **資料前置**：`台股tick改用TimescaleDB.md`（16/16 完成，程式在 `feature/tick-timescaledb`，要等 `feature/post-rehearsal` 合併後才能進 `main`）。
>   本文件的實作分支要從 `feature/tick-timescaledb` 分出，合併順序跟在它後面。
> - **吸收 `滑價模型後續優化.md` S3**（tick 級改用 bid/ask 成交）：Phase3-2 實作的就是它；完成時該步驟一併標記 ✅。
> - **盤中動能策略的校準**：`盤中動能策略.md` S2 的日 K 近似「不可直接採信」；本文件 Phase5-2 用逐筆重放重跑，結果回饋該文件 S4 的判讀。
> - **與實盤的 parity**：TICK 回測的段落（盤前、盤中逐筆、尾盤）對照實盤的 `ExecutionTiming`（`AT_OPEN`／`IMMEDIATE`／`AT_CLOSE`），
>   回測與實盤用同一套鉤子時點，才能拿回測當實盤的對照。

## 進度追蹤表

| 編號 | 步驟名稱 | 產出檔案 | 驗證方式 | 狀態 | 備註／中斷點 |
|------|----------|----------|----------|:----:|--------------|
| Phase0-1 | 定案介面細節 | 本文件〈待定案〉 | 使用者確認 | ⬜ | 2026-10-10 已定案四點（見〈已定案〉）；〈待定案〉三點要在動工前確認 |
| Phase1-1 | 策略宣告支援的級別＋回測 `--scale` 覆寫 | `core/strategies/base.py`、`apps/backtest.py`、`core/backtest/backtester.py`、各策略 | 支援範圍外的 `--scale` 當場報錯；不帶 `--scale` 時行為不變 | ⬜ | 相依 Phase0-1 |
| Phase1-2 | 標的範圍選項（策略清單／全市場） | `apps/backtest.py`、`core/strategies/base.py` | 兩個選項各自只讀到預期的標的 | ⬜ | 相依 Phase0-1 |
| Phase2-1 | tick 查詢支援多檔與當日累計量 | `core/dao/tw/stock_tick_dao.py`、`core/api/tw/stock_tick_api.py` | 暫存 schema 測試：多檔查詢結果與逐檔查詢相同；累計量與逐筆加總一致 | ⬜ | — |
| Phase2-2 | 逐筆重放資料源 | `core/backtest/datafeed/tw/stock_tick_replay.py`（新） | 依 `(time, stock_id, seq)` 順序逐筆產生報價；`volume` 為當日累計量、`tick.volume` 為單筆量 | ⬜ | 相依 Phase2-1 |
| Phase3-1 | 逐筆回測迴圈與段落 | `core/backtest/backtester.py`（或拆出 `tick_backtester.py`） | 盤前、盤中逐筆、尾盤三段依策略宣告的時點呼叫鉤子；取消對 `is_tick_triggered` 的拒絕 | ⬜ | 相依 Phase1-1、Phase2-2 |
| Phase3-2 | 逐筆成交模型 | `core/backtest/models/fill_model.py`（或新增 `tick_fill_model.py`） | 下一筆成交、bid/ask、限價穿越、漲跌停鎖死、成交量上限的單元測試 | ⬜ | 相依 Phase3-1；吸收 `滑價模型後續優化.md` S3 |
| Phase3-3 | 盤中帳務與日終結算 | `core/backtest/backtester.py`、`core/backtest/models/settlement_model/tw_stock.py` | 盤中成交即時入帳（可用餘額、持倉檔數）；日終當沖回補、盯市、權益快照與 DAY 口徑一致 | ⬜ | 相依 Phase3-2 |
| Phase4-1 | `IntradayMomentumStrategy` 支援 TICK 回測 | `strategies/stock/intraday_momentum_strategy.py` | 移除 `setup_apis()` 的 `NotImplementedError`；逐筆分支在回測與實盤共用 | ⬜ | 相依 Phase3-3 |
| Phase4-2 | 既有 DAY 策略宣告支援範圍 | `strategies/**/*.py`、`strategies/README.md` | 每支策略都宣告支援的級別；只支援 DAY 者加 `--scale tick` 報錯 | ⬜ | 相依 Phase1-1 |
| Phase5-1 | 端到端測試 | `tests/backtest/test_tick_replay_backtest.py`（新） | 手寫 tick：無前視、下一筆成交、決定性（跑兩次逐位元相同）、多檔交錯順序 | ⬜ | 相依 Phase3-3 |
| Phase5-2 | 真實資料實跑與效能量測 | 本文件（實跑紀錄）、`盤中動能策略.md` | 兩種標的範圍各跑一段區間；記錄每日耗時、峰值記憶體；與 S2 日 K 近似逐項對照 | ⬜ | 相依 Phase4-1、Phase5-1 |
| Phase5-3 | 文件 | `core/backtest/README.md`、`strategies/README.md`、`docs/backtest/module-map.md` | 文件描述逐筆重放的語意、段落、成交規則與已知限制 | ⬜ | 相依 Phase5-2 |

---

## 已定案（2026-10-10 使用者確認）

1. **級別決定方式**：策略宣告預設級別與支援的級別；回測可用 `--scale` 覆寫，只能選策略支援的，否則當場報錯。
2. **餵入單位**：逐筆、一次一筆，`List[StockQuote]` 長度 1，與實盤 `is_tick_triggered` 同契約。
3. **成交規則**：下單後同一檔的下一筆 tick 才成交；市價買吃 `ask_price`、賣吃 `bid_price`；限價單等之後的成交價穿越才成交。
4. **標的範圍**：策略盤前宣告的清單與全市場兩套都做，回測前自己選。

## 待定案（Phase0-1，動工前要確認）

以下三點是策略作者會直接碰到的介面，依「策略介面先問再做」的約定，先列建議、確認後才實作：

1. **支援級別的宣告方式**：建議 `self.supported_scales: Set[Scale]`，預設 `{self.scale}`（只支援自己宣告的那一個）；
   `--scale` 指定的級別不在集合內就報錯。替代方案：不另設欄位，改由策略覆寫 `supports_scale(scale) -> bool`。
2. **盤中各鉤子的時點從哪裡來**：TICK 回測要知道 `open`／`close`／`stop_loss` 各在哪一段呼叫。建議**沿用 `live_schedule`**
   （例如盤中動能是 `{"open": IMMEDIATE, "stop_loss": IMMEDIATE, "close": AT_OPEN}`），回測與實盤才是同一套時點；
   代價是 `BaseStrategy` 的 docstring「回測完全不讀這一區」要改寫，`live_schedule` 變成回測與實盤共用的設定（可能要改名，例如 `execution_schedule`）。
   替代方案：另設回測專用的 `tick_schedule`，但兩份設定會漂移。
3. **策略清單的鉤子**：建議沿用 `get_live_symbols(latest_date)` 當「策略清單」的來源（盤中動能已實作：前一交易日當沖名單內成交量前 190 檔），
   回測時每個交易日盤前以「前一交易日」呼叫一次。替代方案：另設 `get_tick_universe(date)`，預設呼叫 `get_live_symbols()`。
   策略沒有覆寫（回傳 `None`）時，選「策略清單」要報錯還是退回全市場也要定。

## 步驟詳述

### Phase0-1. 定案介面細節 ⬜

- **目的**：〈待定案〉三點都是策略作者會寫的欄位或鉤子，定了就難改。
- **做法**：逐點與使用者確認，結果寫回〈已定案〉，並刪掉〈待定案〉中已決定的項目。
- **產出**：本文件。
- **驗證方式**：使用者確認。
- **相依**：無。

### Phase1-1. 策略宣告支援的級別＋回測 `--scale` 覆寫 ⬜

- **目的**：同一支策略可以選擇用日 K 或逐筆回測，但不能讓只寫了日 K 邏輯的策略吃到 tick 而默默算錯。
- **做法**：
  - `BaseStrategy` 依 Phase0-1 的定案加上支援級別的宣告，預設只支援自己的 `self.scale`。
  - `apps/backtest.py` 新增 `--scale {day,tick}`；未帶時沿用策略的 `self.scale`。
  - `Backtester.__init__` 驗證：級別不在支援範圍內時拋出明確錯誤（列出策略支援哪些級別）。
  - 現行 `_reject_intraday_tick_backtest()` 的拒絕條件，改到 Phase3-1 逐筆迴圈完成時才移除；這一步只先加驗證。
- **產出**：上列檔案與測試。
- **驗證方式**：不帶 `--scale` 時所有既有回測行為不變（回歸零變動）；`--scale` 在範圍外時當場報錯、錯誤訊息列出支援的級別。
- **相依**：Phase0-1。

### Phase1-2. 標的範圍選項（策略清單／全市場） ⬜

- **目的**：策略清單貼近實盤（訂閱上限 200 檔、前一天冷門當天才爆量的股票會漏掉），全市場則看策略本身的潛力；兩者都要能跑。
- **做法**：
  - `apps/backtest.py` 新增 `--tick-universe {strategy,all}`，只在 TICK 級別有效（DAY 帶了就報錯或警告，Phase0-1 一併定）。預設值建議 `strategy`（與實盤一致，也比較快）。
  - `strategy`：每個交易日盤前以前一交易日呼叫策略清單的鉤子（Phase0-1 第 3 點），再加上**當時的持倉標的**（實盤 `LiveTrader.quote_symbols()` 也一律替持倉訂閱）。
  - `all`：當天 tick 表裡出現的全部股票。
- **產出**：上列檔案與測試。
- **驗證方式**：兩個選項各自只讀到預期的標的；`strategy` 模式下持倉標的即使掉出清單也會被讀到。
- **相依**：Phase0-1。

### Phase2-1. tick 查詢支援多檔與當日累計量 ⬜

- **目的**：策略清單模式要一次查「一天 × 數百檔」；實盤報價的 `volume` 是當日累計量，回測要能給出同一個值。
- **做法**：
  - `StockTickDAO.query_ticks()` 新增 `stock_ids: Optional[Sequence[str]]`。ConnectorX 不支援參數佔位符，值要嵌進 SQL：**每個代號都走現有的英數字驗證**，
    再組成 `stock_id IN (...)`；清單為空時直接回空表、不送查詢。
  - 累計量用 `sum(volume) OVER (PARTITION BY stock_id, time::date ORDER BY time, seq)` 在資料庫算好，回傳多一欄 `cum_volume`；
    或在 Python 端 `groupby().cumsum()`——兩者擇一，以 Phase5-2 的量測決定。
  - `StockTickAPI` 新增對應方法（名稱沿用 `get_` 前綴），回傳欄位契約寫進 docstring。
- **產出**：上列檔案；`tests/test_stock_tick_timescale.py`、`tests/test_stock_tick_api.py` 補測試。
- **驗證方式**：多檔查詢結果與逐檔查詢合併後相同；`cum_volume` 等於逐筆加總；不合法代號在送出查詢前就被拒絕。
- **相依**：無（可最先做）。

### Phase2-2. 逐筆重放資料源 ⬜

- **目的**：把一天的 tick 依時間順序逐筆交給引擎，語意與實盤推播一致。
- **做法**：
  - 新增 `StockTickReplayFeed`（`core/backtest/datafeed/tw/stock_tick_replay.py`），每個交易日：
    1. 依 Phase1-2 的範圍查當天 tick（Phase2-1）。
    2. 依 `(time, stock_id, seq)` 排序，逐筆產生 `StockQuote`（`scale=TICK`、`cur_price=close`、**`volume=當日累計量`**、`tick` 掛單筆的 `TickQuote`）。
       同一時間戳記跨股票的順序以代號固定，回測才可重現。
    3. **不要一次建完一整天的物件**：現行 `from_tick_rows()` 全市場一天要 3.5 秒、0.64 GB。改成產生器逐筆建，或先以向量化欄位處理、迴圈時才組物件。
  - 另外提供段落需要的快照：盤前的參考價（前一交易日收盤）、開盤價（當天第一筆）、尾盤快照（13:20 前每檔最後一筆）、收盤價（最後一筆）。
  - 現行 `StockDataFeed.get_quotes(date, Scale.TICK)` 整天一次給的路徑，在 Phase3-1 完成後移除。
- **產出**：新檔案與測試。
- **驗證方式**：順序、累計量、`tick.volume` 為單筆量、各快照的值都以手寫資料驗證。
- **相依**：Phase2-1。

### Phase3-1. 逐筆回測迴圈與段落 ⬜

- **目的**：取代 `run_tick_backtest()` 的「整天一根 K 棒」，讓策略只看得到當下以前的資料。
- **做法**：每個交易日依序跑三段，各鉤子在哪一段呼叫依 Phase0-1 第 2 點的定案：
  1. **盤前（對應 `AT_OPEN`）**：以盤前快照（參考價）呼叫排在這段的鉤子；委託進開盤集合競價，以開盤價成交。
  2. **盤中逐筆（對應 `IMMEDIATE`）**：每一筆 tick 先撮合在途委託（Phase3-2），再呼叫排在這段的鉤子，傳入長度 1 的 list。
     停損與平倉只傳**該筆 tick 的那一檔**，解決「同一檔數千筆報價重複平倉」。
  3. **尾盤（對應 `AT_CLOSE`）**：以 13:20 快照呼叫排在這段的鉤子；委託進收盤集合競價，以收盤價成交。
  - 委託帶下單時間（觸發那一筆 tick 的時間），報表的成交紀錄也記時間。
  - 移除 `_reject_intraday_tick_backtest()` 與它的測試，改成新契約的測試。
- **產出**：上列檔案（`backtester.py` 已 648 行，逐筆迴圈建議拆成獨立類別）。
- **驗證方式**：手寫兩檔交錯的 tick，驗證鉤子呼叫順序、每次只拿到一筆、段落時點；策略在第 N 筆時看不到第 N+1 筆。
- **相依**：Phase1-1、Phase2-2。

### Phase3-2. 逐筆成交模型 ⬜

- **目的**：沒有前視、貼近實盤的成交規則。
- **做法**：
  - 在途委託佇列：委託送出後，**同一檔的下一筆 tick** 才嘗試撮合。
  - 市價（`ExecutionStyle.MARKET`）：買吃 `ask_price`、賣吃 `bid_price`；該值為 0（漲停鎖死沒有委賣、跌停鎖死沒有委買）時不成交。
    實盤盤中的市價是「決策價加保護價的限價＋IOC」，所以下一筆沒成交就作廢，不追價（與 `實盤委託價格類型設計.md` 的執行層一致）。
  - 限價（`ExecutionStyle.LIMIT`）：之後的成交價穿越限價才成交，成交價取限價；ROD 留到收盤未成交作廢。
  - 成交量上限：以下一筆 tick 的 `ask_volume`／`bid_volume` 或成交量為上限，超過的部分依 `VolumeCapPolicy` 截斷或拒單——具體分母在實作時定並寫回本步驟。
  - 漲跌停、價格對齊檔位、可當沖／可融券名單等既有檢查沿用現行 `TwStockFillModel` 的邏輯。
  - 滑價：bid/ask 已經包含價差，預設不再額外加 bps 滑價；策略明確設定時才疊加。
  - **`get_filled_price(self, order)` 要先改簽章**（`滑價模型後續優化.md` S3 的盤點）：它只收訂單、不收報價，
    而它是策略委託（`fill()`）與引擎強制出場（`BaseSettlementModel.apply_fill_price()`）唯一的滑價入口；要吃 bid/ask 就得把 `quote` 傳進去，
    股票與期貨兩條路徑、兩處呼叫點都受影響。停牌／下市時 `apply_fill_price()` 拿到的報價是 `None`，新簽章要允許 `None` 並退回 bps。
  - **tick 路徑補上一般股過濾**：檔位表 `PRICE_TICK_TABLE` 只適用普通股，日 K 路徑在 `StockQuoteAdapter` 以 `filter_common_stocks()` 濾掉 ETF 與權證，
    tick 路徑沒有這道過濾（tick 庫含 ETF），不補的話檔位對齊會失真。過濾放在 Phase2-2 的資料源或這一步，實作時擇一。
- **產出**：上列檔案與單元測試；`滑價模型後續優化.md` S3 同步標記。
- **驗證方式**：下一筆成交、吃 bid/ask、鎖死不成交、IOC 作廢、限價穿越、成交量截斷，各有單元測試。
- **相依**：Phase3-1。

### Phase3-3. 盤中帳務與日終結算 ⬜

- **目的**：盤中的成交要即時反映在可用餘額與持倉檔數上，否則同一天後面的訊號會用錯的資金。
- **做法**：
  - 每筆成交立即更新帳戶與持倉（`position_manager`）；`check_max_holdings` 以當下持倉計算。
  - 日終沿用現行 `settlement.on_bar_close()`：當沖未回補處理、借券費、盯市（以收盤價）、權益快照、次日漲跌停基準。傳入的報價改成「每檔一筆收盤快照」，不再是整天的 tick。
- **產出**：上列檔案與測試。
- **驗證方式**：同一天先買後賣的當沖，帳務與 DAY 路徑同一筆交易的口徑一致（稅費、當沖減半）；盤中資金不足時後面的委託被擋。
- **相依**：Phase3-2。

### Phase4-1. `IntradayMomentumStrategy` 支援 TICK 回測 ⬜

- **目的**：讓盤中動能策略用逐筆資料回測，取代偏樂觀的日 K 近似。
- **做法**：
  - `supported_scales`（依 Phase0-1 的名稱）設為 `{DAY, TICK}`，預設維持 `DAY`，不改既有 S2 的結果。
  - 移除 `setup_apis()` 對 TICK 的 `NotImplementedError`；逐筆分支 `generate_tick_open_signals()` 已是實盤用的，回測直接共用。
  - 停損、隔天開盤出場在 TICK 回測照 `live_schedule` 的時點執行。
- **產出**：策略檔與 `tests/test_intraday_momentum_strategy.py`。
- **驗證方式**：手寫 tick 驗證觸發、停損、隔天開盤出場；DAY 回測結果不變。
- **相依**：Phase3-3。

### Phase4-2. 既有 DAY 策略宣告支援範圍 ⬜

- **目的**：日 K 策略依賴 OHLC，吃到 tick 會默默算錯；要明確宣告。
- **做法**：預設值已經是「只支援自己的 `self.scale`」，這一步逐支確認並在 `strategies/README.md` 寫清楚怎麼宣告。
- **產出**：`strategies/README.md`；必要時調整策略檔。
- **驗證方式**：每支策略加 `--scale tick` 時，只支援 DAY 者當場報錯。
- **相依**：Phase1-1。

### Phase5-1. 端到端測試 ⬜

- **目的**：把五個現存問題（見 Abstract）各自釘一個會紅的測試。
- **做法**：手寫小量 tick（不讀正式資料表），用測試替身的資料源跑完整回測：
  - 無前視：在第 N 筆觸發的委託，只能用第 N+1 筆以後的價格成交。
  - 下一筆成交、吃 bid/ask。
  - 決定性：同一份資料跑兩次，成交紀錄逐位元相同。
  - 多檔交錯：同一時間戳記跨股票的順序固定。
  - 停損只對該筆的那一檔觸發一次。
- **產出**：`tests/backtest/test_tick_replay_backtest.py`。
- **驗證方式**：測試全綠；逐一突變對應的實作（例如改回當筆成交）時對應測試會紅。
- **相依**：Phase3-3。

### Phase5-2. 真實資料實跑與效能量測 ⬜

- **目的**：確認框架在真實資料上可用，並回答盤中動能策略的真實表現。
- **做法**：
  - `IntradayMomentumStrategy` 以 `--scale tick` 跑一段區間（先一個月量效能，再決定是否跑完整 2020-05～2024-05），兩種標的範圍各一次。
  - 記錄每交易日耗時、峰值記憶體；全市場模式若太慢，記下瓶頸（查詢、建物件、策略呼叫）。
  - 與 `盤中動能策略.md` S2 的日 K 近似逐項對照：交易筆數、觸發時間、進場價、停損次數、平均報酬，差異寫回該文件。
- **產出**：本文件末尾的實跑紀錄、`盤中動能策略.md` 的對照。
- **驗證方式**：兩種模式都跑完、0 錯誤；紀錄完整。
- **相依**：Phase4-1、Phase5-1。

### Phase5-3. 文件 ⬜

- **目的**：現行說明文件描述新的 TICK 回測語意。
- **做法**：更新 `core/backtest/README.md`（`Scale.TICK` 列、成交規則、已知限制）、`strategies/README.md`（支援級別的宣告、逐筆契約、標的範圍）、`docs/backtest/module-map.md`；
  移除各處「TICK 回測整天一次給、有前視」的註解與說明。
- **產出**：上列文件。
- **驗證方式**：`scripts/check_doc_paths.py` 通過；`grep -rn "整天的 tick 一次" core strategies docs` 無結果。
- **相依**：Phase5-2。

## 風險與對策

- **全市場模式太慢**：每天約 120 萬筆，Python 逐筆呼叫策略一天估數十秒。對策：預設用策略清單模式；Phase5-2 量測後若有需要，再做「沒有任何在途委託與持倉、且策略不關心的標的跳過」之類的最佳化。
- **與實盤的已知差異**：回測的跨股票到達順序以 `(time, stock_id, seq)` 固定，實盤取決於券商推送；回測沒有網路延遲（委託在下一筆就可能成交）。這兩點寫進 Phase5-3 的已知限制。
- **歷史資料止於 2024-05-10**：tick 回補不在本文件範圍；回測區間受限於資料範圍。

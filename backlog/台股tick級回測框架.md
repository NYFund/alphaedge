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
  - **日 K 與 tick 結構對稱**：`Backtester` 只留逐日迴圈與每日前後的共用處理，「一天怎麼跑」交給 `BarSimulator`（日 K）或 `TickSimulator`（逐筆），兩者繼承 `BaseSimulator`、介面相同（〈已定案〉第 8 點）。
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
  5. 日 K 抽成 `BarSimulator` 後回歸雙線零變動。

---

> **與其他文件的關係**
> - **資料前置**：`台股tick改用TimescaleDB.md`（16/16 完成，程式在 `feature/tick-timescaledb`，要等 `feature/post-rehearsal` 合併後才能進 `main`）。
>   本文件的實作分支 `feature/tick-backtest`（worktree：`AlphaEdge/worktrees/tick-backtest`）2026-10-10 從 `feature/tick-timescaledb` 分出，合併順序跟在它後面。
> - **吸收 `滑價模型後續優化.md` S3**（tick 級改用 bid/ask 成交）：Phase3-2 實作的就是它；完成時該步驟一併標記 ✅。
> - **盤中動能策略的校準**：`盤中動能策略.md` S2 的日 K 近似「不可直接採信」；本文件 Phase5-2 用逐筆重放重跑，結果回饋該文件 S4 的判讀。
> - **與實盤的 parity**：TICK 回測的段落（盤前、盤中逐筆、尾盤）對照實盤的 `ExecutionTiming`（`AT_OPEN`／`IMMEDIATE`／`AT_CLOSE`），
>   回測與實盤用同一套鉤子時點，才能拿回測當實盤的對照。

## 進度追蹤表

| 編號 | 步驟名稱 | 產出檔案 | 驗證方式 | 狀態 | 備註／中斷點 |
|------|----------|----------|----------|:----:|--------------|
| Phase0-1 | 定案介面細節與命名 | 本文件〈已定案〉 | 使用者確認 | ✅ | **✅ 2026-10-10**：介面七點與架構命名（〈已定案〉第 8 點）全數定案；同日追加第 9 點引擎內部改名 |
| Phase1-1 | 策略宣告支援的級別（`supported_scales`）＋回測 `--scale` 覆寫 | `core/strategies/base.py`、`apps/backtest.py`、`core/backtest/backtester.py`、各策略 | 支援範圍外的 `--scale` 當場報錯；不帶 `--scale` 時行為不變 | ✅ | **✅ 2026-10-10**（`feature/tick-backtest`）：`supported_scales`、`--scale`、`Backtester.validate_scale()` 完成；策略檔不必改（預設只支援自己的 `scale`），逐支宣告留給 Phase4-1／Phase4-2 |
| Phase1-2 | 標的範圍選項（策略清單／全市場） | `apps/backtest.py`、`core/backtest/` | 兩個選項各自只讀到預期的標的；策略沒有清單時選策略清單當場報錯 | ✅ | **✅ 2026-10-10**（`feature/tick-backtest`）：選項、驗證、每日標的規則完成並接上 `setup_day()`；每天由 `TickSimulator` 呼叫在 Phase3-1 接上（在那之前沒有策略宣告支援 TICK，選項不會被默默忽略）。原排在 Phase2-2 之後（選項要靠 `setup_day(date, symbols)` 才有作用） |
| Phase1-3 | `live_schedule` 改名為 `execution_schedule`（回測與實盤共用） | `core/strategies/base.py`、`core/live/`（含 `strategy_guard.py`）、`strategies/`、`tests/`、相關文件 | `grep -rn live_schedule` 無結果；實盤與回測測試全數通過 | ✅ | **✅ 2026-10-10**（`feature/tick-backtest`）：全專案改名完成，舊名啟動即報錯；`resolve_hook_timing()` 移到 `core/strategies/base.py`（偏離原規格，見步驟詳述）。動到實盤程式：部署跟著本分支合併（在 `實盤下單架構規劃.md` Phase7-1 演練結束後），合併後要重啟常駐行程 |
| Phase1-4 | 日 K 路徑抽成 `BarSimulator`（純重構）＋引擎內部兩處改名 | `core/backtest/simulator/`（新：`base.py`、`bar_simulator.py`）、`core/backtest/backtester.py`、`core/backtest/factory.py`、`core/backtest/models/`、`tests/backtest/` | 回歸雙線零變動；`Backtester` 只剩逐日迴圈、`prepare_day()`／`finish_day()`、報表；`on_bar_close`、`prev_close` grep 為零 | ✅ | **✅ 2026-10-10**（`feature/tick-backtest`）：改名 `48752a8` 與抽 simulator 各一個 commit；回歸雙線零變動、5 支策略 2024 全年 25 個報表 CSV 逐位元相同。偏離原規格：多一個 `load_day()`（見步驟詳述） |
| Phase1-5 | 每日市場狀態改由各 model 自己準備 | `core/backtest/backtester.py`（`prepare_day()`）、`core/backtest/models/`、`core/backtest/datafeed/` | 回歸雙線零變動；`prepare_day()` 不再逐一推台股專屬 hook | ✅ | **✅ 2026-10-11**（`feature/tick-backtest`）：`FillModel`／`SettlementModel` 各一個 `prepare_day(feed, date, allow_short)`，基底七個 no-op 鉤子移除；回歸雙線與五支策略 25 個報表 CSV 零變動；補上停券擋板的引擎層測試 |
| Phase2-1 | tick 查詢支援多檔與當日累計量 | `core/dao/tw/stock_tick_dao.py`、`core/api/tw/stock_tick_api.py` | 暫存 schema 測試：多檔查詢結果與逐檔查詢相同；累計量與逐筆加總一致 | ✅ | **✅ 2026-10-10**（`feature/tick-backtest`）：`stock_ids` 與 `cum_volume` 完成；累計量定案在 pandas 端算（實測較快，見步驟詳述）；真實資料 200 檔清單一天 0.73 秒 |
| Phase2-2 | 台股逐筆資料源 `StockTickDataFeed` | `core/backtest/datafeed/tw/stock_tick_datafeed.py`（新，繼承 `StockDataFeed`） | 依 `(time, stock_id, seq)` 順序逐筆產生報價；`volume` 為當日累計量、`tick.volume` 為單筆量 | ✅ | **✅ 2026-10-10**（`feature/tick-backtest`）：資料源與快照完成，一般股過濾放在這一步；尚未接上引擎（Phase3-1 由 factory 在 TICK 時組出）。偏離原規格兩處見步驟詳述 |
| Phase3-1 | `TickSimulator`：一天三段與逐筆迴圈 | `core/backtest/simulator/tick_simulator.py`（新）、`core/backtest/factory.py` | 開盤集合競價、逐筆、收盤集合競價三段依 `execution_schedule` 呼叫鉤子；取消對 `is_tick_triggered` 的拒絕 | ✅ | **✅ 2026-10-10**（`feature/tick-backtest`）：與 Phase3-2 同一個 commit（沒有撮合規則就產生不了任何成交，兩步無法分開驗證）；factory 在 TICK 時組出 `TickSimulator`＋`StockTickDataFeed`，整天一次給的舊路徑與 `IntradayScaleMismatchError` 已移除 |
| Phase3-2 | 逐筆成交模型與在途委託 | `core/backtest/models/fill_model.py`、`core/backtest/simulator/pending_orders.py`（新：`PendingOrderManager`、`PendingOrder`） | 下一筆成交、bid/ask、限價穿越、漲跌停鎖死、IOC 作廢、成交量上限的單元測試 | ✅ | **✅ 2026-10-10**（`feature/tick-backtest`）：規則依〈已定案〉第 10 點；**偏離原規格：`get_filled_price()` 不改簽章**（成交價在呼叫它之前就由這一筆決定，見步驟詳述）；`滑價模型後續優化.md` S3 一併 ✅ |
| Phase3-3 | 盤中帳務與日終結算 | `core/backtest/simulator/tick_simulator.py`、`core/backtest/backtester.py`（`finish_day()`）、`core/backtest/models/settlement_model/tw_stock.py` | 盤中成交即時入帳（可用餘額、持倉檔數）；日終當沖回補、盯市、權益快照與 DAY 口徑一致 | ✅ | **✅ 2026-10-10**（`feature/tick-backtest`）：不需改程式（成交即時經 `position_manager` 入帳、日終走 `finish_day()`），補三條測試釘住 |
| Phase4-1 | `IntradayMomentumStrategy` 支援 TICK 回測 | `strategies/stock/intraday_momentum_strategy.py` | 移除 `setup_apis()` 的 `NotImplementedError`；逐筆分支在回測與實盤共用 | ✅ | **✅ 2026-10-10**（`feature/tick-backtest`）：`supported_scales = {DAY, TICK}`、移除 `NotImplementedError`；逐筆分支與實盤同一份；手寫兩天 tick 驗證觸發、停損、隔天開盤出場 |
| Phase4-2 | 既有 DAY 策略宣告支援範圍 | `strategies/**/*.py`、`strategies/README.md` | 每支策略都宣告支援的級別；只支援 DAY 者加 `--scale tick` 報錯 | ✅ | **✅ 2026-10-10**（`feature/tick-backtest`）：其餘策略都維持預設（只支援自己的 `scale`），不必改檔；以 `StrategyLoader` 逐支驗證 |
| Phase5-1 | 端到端測試 | `tests/backtest/test_tick_simulator.py`（新） | 手寫 tick：無前視、下一筆成交、決定性（跑兩次逐位元相同）、多檔交錯順序 | ✅ | **✅ 2026-10-10**（`feature/tick-backtest`）：`tests/backtest/test_tick_simulator.py` 19 條，隨 Phase3-1～3-3 寫成；清單逐項對照見步驟詳述 |
| Phase5-2 | 真實資料實跑與效能量測 | 本文件（實跑紀錄）、`盤中動能策略.md` | 兩種標的範圍各跑一段區間；記錄每日耗時、峰值記憶體；與 S2 日 K 近似逐項對照 | ✅ | **✅ 2026-10-11**（`feature/tick-backtest`）：一個月量效能後，依使用者裁示跑一年（2023-05-01～2024-05-10）兩種範圍＋同區間日 K，0 錯誤；日 K 近似一年 +218%、逐筆約持平，對照寫回 `盤中動能策略.md` S2。完整區間（2020-05 起）未跑 |
| Phase5-3 | 文件 | `core/backtest/README.md`、`strategies/README.md`、`docs/backtest/module-map.md` | 文件描述逐筆重放的語意、段落、成交規則與已知限制 | ✅ | **✅ 2026-10-11**（`feature/tick-backtest`）：文件隨各步驟寫入（`core/backtest/README.md`〈`Scale.TICK`：逐筆重放〉含段落、成交規則、效能與已知限制；`strategies/README.md`、`docs/backtest/` 三份）；舊的「整天一次給」說明已全數移除 |

---

## 已定案（2026-10-10 使用者確認）

1. **級別決定方式**：策略宣告預設級別與支援的級別；回測可用 `--scale` 覆寫，只能選策略支援的，否則當場報錯。
2. **餵入單位**：逐筆、一次一筆，`List[StockQuote]` 長度 1，與實盤 `is_tick_triggered` 同契約。
3. **成交規則**：下單後同一檔的下一筆 tick 才成交；市價買吃 `ask_price`、賣吃 `bid_price`；限價單等之後的成交價穿越才成交。
4. **標的範圍**：策略盤前宣告的清單與全市場兩套都做，回測前自己選（`--tick-universe {strategy,all}`）。
5. **支援級別的宣告方式**：`self.supported_scales: Set[Scale]`，預設 `{self.scale}`（只支援自己宣告的那一個）；`--scale` 指定的級別不在集合內就報錯。
   盤中動能策略設為 `{Scale.DAY, Scale.TICK}`。
6. **鉤子時點**：TICK 回測沿用實盤的段落設定，**`live_schedule` 改名為 `execution_schedule`**，成為回測與實盤共用的設定
   （`BaseStrategy` docstring「回測完全不讀這一區」一併改寫）。例如盤中動能是 `{"open": IMMEDIATE, "stop_loss": IMMEDIATE, "close": AT_OPEN}`。
   改名獨立成 Phase1-3。DAY 回測仍不讀它（日 K 回測一次拿到整天，沒有段落）。
7. **策略清單的來源**：沿用 `get_live_symbols(latest_date)`，回測每個交易日盤前以「前一交易日」呼叫一次，再加上當時的持倉標的（與實盤 `LiveTrader.quote_symbols()` 一致）。
   **策略沒有覆寫（回傳 `None`）時，選 `--tick-universe strategy` 當場報錯**，訊息提示改用 `--tick-universe all`；不自動退回全市場（避免在不知情下改跑一天 120 萬筆的全市場）。

8. **架構與命名**（2026-10-10 使用者逐項選定）：日 K 與 tick 結構對稱，日 K 也從 `Backtester` 抽成獨立的 simulator。

   ```
   Backtester.run()                       逐日迴圈（日 K 與 tick 共用）
    └─ 每個交易日：
        prepare_day(date)                 每日準備：漲跌停基準、融券餘額、停券、交易所名單、除權息
        close_quotes = simulator.run_day(date)
        finish_day(date, close_quotes)    收盤結算：當沖回補、借券費、盯市、權益快照、次日漲跌停基準

   BarSimulator.run_day(date)             TickSimulator.run_day(date)
       process_bar(quotes)                    process_opening_auction()   開盤集合競價，對應 AT_OPEN
                                              process_tick(quote)         逐筆迴圈，對應 IMMEDIATE
                                              process_closing_auction()   收盤集合競價，對應 AT_CLOSE
   ```

   | 名稱 | 檔案 | 新增／修改 | 工作內容 |
   |------|------|:---------:|----------|
   | `Backtester` | `core/backtest/backtester.py` | 修改 | 逐日迴圈、`prepare_day()`／`finish_day()`、報表；依 scale 交給 `self.simulator` |
   | `BaseSimulator` | `core/backtest/simulator/base.py` | 新增 | 兩個 simulator 的共同介面 `run_day(date) -> List[BaseQuote]`；共用的 `execute_open_signal()`／`execute_close_signal()`（自 `Backtester` 搬來） |
   | `BarSimulator` | `core/backtest/simulator/bar_simulator.py` | 新增 | 日 K：一天一根 bar，`process_bar(quotes)` 依 `allow_day_trade` 開平倉、當根成交（自 `execute_bar()` 搬來） |
   | `TickSimulator` | `core/backtest/simulator/tick_simulator.py` | 新增 | 逐筆：開盤集合競價 → 逐筆 `process_tick(quote)`（先撮合在途委託、再把這一筆交給策略）→ 收盤集合競價 |
   | `PendingOrderManager` | `core/backtest/simulator/pending_orders.py` | 新增 | 在途委託：依股票查詢、IOC 下一筆沒成交作廢、收盤清空 ROD |
   | `PendingOrder` | `core/backtest/simulator/pending_orders.py` | 新增 | 一張在途委託：訂單、階段（開倉／停損／平倉）、送出時間、IOC 或 ROD |
   | `StockTickDataFeed` | `core/backtest/datafeed/tw/stock_tick_datafeed.py` | 新增 | 繼承 `StockDataFeed`（日頻資料照用），加上逐筆產生報價與開盤、13:20、收盤快照 |
   | `TwStockFillModel` | `core/backtest/models/fill_model.py` | 修改 | 加上逐筆撮合：下一筆成交、市價吃 bid/ask、限價穿越、鎖漲跌停不成交；`on_bar_close`／`prev_close` 改名見第 9 點 |
   | `StockTickAPI`／`StockTickDAO` | `core/api/tw/`、`core/dao/tw/` | 修改 | `get_ordered_ticks()` 加選填 `stock_ids`；回傳當日累計量 `cum_volume` |

   - **選名理由**：Simulator 沿用 Zipline `AlgorithmSimulator` 的慣例；`process_*` 是 NautilusTrader 撮合引擎的寫法，表示引擎內部處理，
     不用業界常當成「策略回呼」的 `on_bar`／`on_tick`；集合競價用交易所術語 opening／closing auction，也避開本專案代表開倉的 open。
   - 未成交委託不叫 open order（業界慣例），因為本專案的 open 指開倉。
   - 既有的 `Backtester`、`TwStockFillModel`、`StockTickAPI`、`StockTickDAO` 維持原名。
   - 移除：`Backtester.run_day_backtest()`、`run_tick_backtest()`、`execute_bar()`、`_reject_intraday_tick_backtest()`，以及 `FillModel.on_bar_open()` 的全日區間邏輯。

9. **引擎內部兩處改名**（2026-10-10 使用者選定，即 `框架對稱化重構.md`〈待定案〉N10、N11）：引擎內部方法不用 `on_*`（保留給策略回呼），「close」也和平倉撞名。
   - `settlement.on_bar_close()` → **`settle_day()`**：與 model 名稱、`finish_day()` 同一組詞。
   - `fill_model.on_bar_close()` → **`update_reference_prices()`**，屬性 `prev_close` → **`reference_prices`**：它存的是漲跌停基準（除權息日被公告的參考價覆寫），
     不是單純的前收；「參考價」是交易所用語，也與實盤的 `get_reference_price` 對齊。`Backtester.update_prev_close()` 與台股 settlement 共用的同一個 dict 一併改名。
   - 在 Phase1-4 實作，與抽 `BarSimulator` 分成不同 commit（先改名、再搬移），每一步各自驗證零變動。

10. **逐筆回測的三項成交規則**（2026-10-10 使用者選定）：
    - **盤中段落的時間窗照實盤段落表**：逐筆鉤子只在 09:05–13:18 的 tick 呼叫（開盤段等回報到 09:05、13:18 後停止送單），收盤段的快照取 13:25 前的最後一筆。
      台股段落時間表由 `core/live/factory.py` 移到 `core/market/tw/` 讓回測與實盤共用。
    - **開盤段（`AT_OPEN`）的委託以當天第一筆成交價成交**：市價單以開盤集合競價的成交價（當天第一筆 tick）成交；限價單要開盤價優於或等於限價才成交，否則依 ROD 留到盤中由後續 tick 穿越成交。
    - **逐筆成交量上限的分母是下一筆的對手方委託量**：買單看 `ask_volume`、賣單看 `bid_volume`，超過的部分依策略的 `VolumeCapPolicy` 截斷或拒單。

## 步驟詳述

### Phase0-1. 定案介面細節與命名 ✅

- **目的**：級別宣告、鉤子時點、策略清單都是策略作者會寫的欄位或鉤子，定了就難改。
- **做法**：逐點與使用者確認，結果寫回〈已定案〉。
- **結果（2026-10-10）**：前四點使用者直接選定；後三點（支援級別的宣告、鉤子時點、策略清單）原列為〈待定案〉，使用者採用建議，已併入〈已定案〉第 5～7 點；
  架構與命名（第 8 點）由使用者從業界慣例的候選中逐項選定。
- **產出**：本文件。
- **驗證方式**：使用者確認。
- **相依**：無。

### Phase1-1. 策略宣告支援的級別＋回測 `--scale` 覆寫 ✅

- **目的**：同一支策略可以選擇用日 K 或逐筆回測，但不能讓只寫了日 K 邏輯的策略吃到 tick 而默默算錯。
- **做法**：
  - `BaseStrategy` 新增 `self.supported_scales: Set[Scale]`。預設值要在子類設定完 `self.scale` 之後才決定（子類在 `super().__init__()` 之後才改 `self.scale`），
    所以基底不在 `__init__` 寫死，改在驗證時以「未設定就視為 `{self.scale}`」處理。
  - `apps/backtest.py` 新增 `--scale {day,tick}`；未帶時沿用策略的 `self.scale`。
  - `Backtester.validate_scale()`（在 `__init__` 呼叫）：級別不在支援範圍內時拋出明確錯誤（列出策略支援哪些級別）；`self.scale` 本身不在 `supported_scales` 內也報錯，兩個欄位寫錯不會被默默放過。
  - 現行 `_reject_intraday_tick_backtest()` 的拒絕條件，改到 Phase3-1 逐筆迴圈完成時才移除；這一步只先加驗證。
- **產出**：上列檔案與測試。
- **驗證方式**：不帶 `--scale` 時所有既有回測行為不變（回歸零變動）；`--scale` 在範圍外時當場報錯、錯誤訊息列出支援的級別。
- **相依**：Phase0-1。
- **結果（2026-10-10，`feature/tick-backtest`）**：
  - `BaseStrategy.supported_scales: Optional[Set[Scale]] = None`（None ＝ 只支援 `self.scale`），`get_supported_scales()` 取實際範圍，`validate_scale(scale=None)` 檢查宣告一致與指定級別；新增 `UnsupportedScaleError`。
  - `--scale` 走既有的 `BacktestOverrides`（新增 `scale` 欄位），與 `--start`／`--end`／`--capital` 同樣在組裝前寫回策略：**先驗策略自己的宣告、再驗覆寫值**——覆寫會蓋掉 `scale`，先蓋再驗就看不出策略寫錯。
    範圍外的覆寫是用法錯誤（`InvalidBacktestOverridesError`，退出碼 2，訊息列出支援的級別）；宣告矛盾是策略的錯（`UnsupportedScaleError`，退出碼 1，與「設了已移除欄位」同一類）。
  - `Backtester.validate_scale()` 在 `__init__` 呼叫，守不經覆寫的路徑（實盤 parity、直接呼叫 factory）。
  - CLI 的選項顯示為 `{TICK,DAY}`，輸入不分大小寫（`--scale tick` 可用）。
  - 測試 `tests/backtest/test_backtest_scale.py` 9 條：預設範圍、子類在 `super().__init__()` 後才改 `scale` 時範圍跟著變、宣告矛盾（含帶合法覆寫時）、範圍外覆寫被拒且策略不變、範圍內覆寫傳到引擎、不帶覆寫不變、CLI 退出碼與 stderr。
    突變「拿掉宣告先驗」「`validate_scale()` 改成 no-op」「覆寫不寫回策略」各讓一條轉紅。回歸雙線零變動；完整 pytest 2876 passed。
  - 文件：`strategies/README.md` 設定表與級別說明、`core/backtest/README.md`、根目錄兩份 README、`docs/backtest/module-map.md`。

### Phase1-2. 標的範圍選項（策略清單／全市場） ✅

- **目的**：策略清單貼近實盤（訂閱上限 200 檔、前一天冷門當天才爆量的股票會漏掉），全市場則看策略本身的潛力；兩者都要能跑。
- **做法**：
  - `apps/backtest.py` 新增 `--tick-universe {strategy,all}`，只在 TICK 級別有效（DAY 帶了就報錯，避免以為有作用）。預設 `strategy`（與實盤一致，也比較快）。
  - `strategy`：每個交易日盤前以前一交易日呼叫 `get_live_symbols()`，再加上**當時的持倉標的**（實盤 `LiveTrader.quote_symbols()` 也一律替持倉訂閱）。
    回傳 `None`（策略沒有覆寫）時當場報錯，訊息提示改用 `--tick-universe all`。
  - `all`：當天 tick 表裡出現的全部股票。
- **產出**：上列檔案與測試。
- **驗證方式**：兩個選項各自只讀到預期的標的；`strategy` 模式下持倉標的即使掉出清單也會被讀到；策略沒有清單時選 `strategy` 當場報錯；DAY 帶 `--tick-universe` 報錯。
- **相依**：Phase0-1。
- **結果（2026-10-10，`feature/tick-backtest`）**：
  - `TickUniverse`（`strategy`／`all`）放在 `core/utils/constant/engine.py`；`--tick-universe` 經 `BacktestOverrides.tick_universe` 由 factory 交給 `Backtester`（不寫回策略：它不是策略的設定）。
  - `Backtester.validate_tick_universe()`：DAY 帶了就拒絕；TICK 未指定時預設 `strategy`；策略沒覆寫 `get_live_symbols()` 卻用 `strategy` 時拒絕並提示 `--tick-universe all`（`InvalidTickUniverseError`，CLI 退出碼 2）。
    逐筆觸發策略的拒絕（`_reject_intraday_tick_backtest()`）排在它之前，那個錯誤更根本。
  - 每日標的規則 `core/backtest/simulator/tick_universe.py` 的 `resolve_strategy_symbols(strategy, account, previous_trading_day)`：清單在前、清單外的持倉依代號排序附後，與實盤 `LiveTrader.quote_symbols()` 相同；
    有覆寫卻在某天回傳 None 時報錯，不改讀全市場。`get_live_symbols()` 的 docstring 從「回測不呼叫」改寫。
  - 「前一交易日」怎麼取留給 Phase3-1（`TickSimulator` 每天盤前呼叫時才需要）。
  - 驗證：`tests/backtest/test_tick_universe.py` 10 條（DAY 帶選項被拒、DAY 預設 None、無清單的 TICK 被拒、全市場不需清單、有清單時預設策略清單、覆寫偵測、清單＋持倉、回傳 None 被拒、
    兩種範圍各自只查預期的標的（接上 `StockTickDataFeed.setup_day()`）、CLI 退出碼）。突變「DAY 不拒絕」「覆寫偵測恆真」「不加持倉」各讓兩條轉紅。回歸雙線零變動；完整 pytest 2896 passed。

### Phase1-3. `live_schedule` 改名為 `execution_schedule` ✅

- **目的**：TICK 回測與實盤讀同一份段落設定（〈已定案〉第 6 點），名稱不能再寫 live。
- **做法**：
  - `BaseStrategy` 的 `live_schedule` 改名為 `execution_schedule`，docstring 從 `=== Live Setting ===` 區塊移出、改寫成「實盤與 TICK 回測共用；DAY 回測不讀」。
  - 全專案改名：2026-10-10 實查 `.py` 13 檔、24 處（含 `core/live/strategy_guard.py`、`core/live/trader.py`、`strategies/` 各策略與 `tests/`），文件 3 份。
  - **舊名不保留別名**：沿用 `BaseStrategy.check_removed_settings()` 的機制，策略還設定 `live_schedule` 時啟動當場報錯並提示新名稱，不讓舊設定被默默忽略。
  - `LiveHook` 列舉名稱不改（它描述的是鉤子，不是設定）。
  - **`resolve_hook_timing()` 搬到回測與實盤共用的位置**（例如 `core/portfolio/order_rules.py`）：它在 `core/live/strategy_guard.py`，`TickSimulator` 要用它判斷鉤子排在哪一段，
    但回測不可 import 實盤模組（`scripts/check_layer_deps.py` 會擋）。
- **產出**：上列檔案。
- **驗證方式**：`grep -rn "live_schedule" core apps strategies tests docs` 只剩 `check_removed_settings()` 的對照表；`tests/live/` 與 `strategy_guard` 測試全數通過；設定舊名的策略啟動即報錯。
- **相依**：Phase0-1。動到實盤程式：部署跟著本分支合併，排在 `實盤下單架構規劃.md` Phase7-1（模擬環境端到端演練）結束之後；
  launchd 排程跑的是主目錄的程式，合併後要重啟常駐行程。
- **結果（2026-10-10，`feature/tick-backtest`）**：
  - 17 檔改名（含 `core/live/strategy_guard.py`、`core/live/trader.py`、`strategies/` 四支策略、`tests/live/` 五檔、文件三份）。`_REMOVED_SETTINGS` 新增 `live_schedule`，設定舊名時回測（`build_backtester()`）與實盤（`strategy_guard`）啟動都會報錯。
  - `BaseStrategy` 的說明移出 `=== Live Setting ===`，改成獨立的「Execution Schedule：實盤與 TICK 回測共用，DAY 回測不讀」區塊（依 Coding Style 用 `#` 註解，不再新增三引號分組）；`Live Setting` 的「回測完全不讀這一區」因此仍然成立。
  - **偏離原規格：`resolve_hook_timing()` 放在 `core/strategies/base.py`（模組層級函式），不是 `core/portfolio/order_rules.py`**：它要讀策略，而 `core.strategies` 已經 import `core.portfolio`，放進 `order_rules` 會循環相依；
    `core/strategies/` 是回測與實盤都能 import 的策略契約層，分層檢查也只准它放契約檔，所以不另開新檔。`trader.py`、`strategy_guard.py`、測試改從這裡 import。
  - 驗證：`grep -rn live_schedule` 只剩移除對照表、`strategies/README.md` 的舊名說明與移除欄位測試；`tests/live/` 950 passed；回歸雙線零變動；完整 pytest 2877 passed；突變「拿掉對照表的 `live_schedule`」移除欄位測試轉紅。

### Phase1-4. 日 K 路徑抽成 `BarSimulator`（純重構）＋引擎內部兩處改名 ✅

- **目的**：讓日 K 與 tick 結構對稱（〈已定案〉第 8 點）。先把日 K 抽出來、證明零變動，tick 再疊上去，兩件事不混在同一個 diff。
- **做法**：
  - 新增 `core/backtest/simulator/`：`BaseSimulator`（`run_day(date) -> List[BaseQuote]` 抽象方法；`execute_open_signal()`／`execute_close_signal()` 自 `Backtester` 搬來共用）、
    `BarSimulator`（`run_day()` 取當天日 K 呼叫 `process_bar(quotes)`；`process_bar()` 即現行 `execute_bar()` 中間的開平倉段）。
  - `Backtester`：`execute_bar()` 的開頭抽成 `prepare_day(date)`、結尾抽成 `finish_day(date, close_quotes)`；`run()` 改成「`prepare_day` → `self.simulator.run_day` → `finish_day`」；
    刪除 `run_day_backtest()`、`execute_bar()`。`factory.py` 依 scale 組出 simulator 並注入。
  - simulator 需要的引擎服務（`validate_orders`、`enrich_orders`、`sort_orders`、`check_max_holdings`、`position_manager`、`fill_model`、`submitted_orders`）由 `Backtester` 傳入；
    介面寫法在實作時定，原則是 simulator 不持有帳戶以外的狀態。
  - **對外介面不變**：實盤 parity（`core/live/factory.py`）呼叫 `Backtester.run()`，並讀 `submitted_orders`、`enrich_orders()`、`position_manager`；這幾個留在 `Backtester`。
    `settlement_model/tw_stock.py` 的 docstring 提到 `Backtester.execute_close_signal()`，跟著改指向。
  - **期貨走同一條路**：`execute_bar()` 是市場無關的，期貨回測（每日結算、保證金追繳、換月）也經過它，`BarSimulator` 同樣要涵蓋期貨，`tests/backtest/test_futures_backtest.py` 是期貨側的守門。
  - **每日流程的順序一個都不能動**：`cur_date` 前進 → 漲跌停基準 → 融券餘額 → 停券與名單（只在允許放空時）→ 除權息 → 開平倉（依 `allow_day_trade`）→ `settlement.settle_day()` → 權益快照 → 次日漲跌停基準。
  - 測試：2026-10-10 實查 `execute_bar` 36 處、`execute_open_signal` 9 處、`execute_close_signal` 3 處、`run_day_backtest` 1 處，分布在 `tests/backtest/` 7 檔與 `make_baseline.py`／`make_short_baseline.py`，一併改寫。
  - `scripts/check_layer_deps.py` 登記 `core.backtest.simulator`（與 `core.backtest.models` 同層）。
  - **改名（〈已定案〉第 9 點）先做、獨立 commit**：`settlement.on_bar_close` → `settle_day`、`fill_model.on_bar_close` → `update_reference_prices`、`prev_close` → `reference_prices`（含 `Backtester.update_prev_close()` 與台股 settlement 的同名屬性），docstring 與註解提到舊名的地方一併改。
  - `框架對稱化重構.md`〈與其他文件的交集〉的其他項目不在本步驟：每日市場狀態改由 model 自己準備拆成 Phase1-5；回測 factory 改用共用組裝函式相依該文件 Phase1-1、報表對標序列去掉鴨子型別相依該文件 Phase3-2，都還不能做，`getattr(self.data_feed, "price", None)` 原樣留在 `Backtester` 的報表段。
- **產出**：上列檔案。
- **驗證方式**：`scripts/run_regression.sh` 回歸雙線零變動（SHORT、LONG baseline，**結束碼 3（有 skip）不算通過**）；`tests/backtest/`、`tests/live/` 全數通過；
  重構前後各跑一次幾支現有策略的完整回測，交易紀錄與每日權益 CSV 逐位元相同；`backtester.py` 不再出現開平倉細節。
  worktree 沒有 `data/db/`，LONG 線會被 skip：要讓它唯讀讀到主目錄的研究庫才算真的跑過。
- **相依**：無（可與 Phase1-1～Phase2-1 平行）。
- **結果（2026-10-10，`feature/tick-backtest`）**：
  - 兩個 commit：先改名（`48752a8`：`settle_day()`、`update_reference_prices()`、`reference_prices`），再抽 simulator。`InstrumentSpec` 的單價參數 `prev_close` 不在改名範圍。
  - 新增 `core/backtest/simulator/`：`BaseSimulator`（`execute_open_signal()`／`execute_close_signal()`、`check_max_holdings()`、`validate_fill_price()`、`apply_fill_model()` 自 `Backtester` 搬來）與 `BarSimulator`（`process_bar()`）。
    `Backtester` 剩逐日迴圈、`prepare_day()`／`finish_day()`、方向與排序等委託前處理、報表；`run_day_backtest()`、`run_tick_backtest()`、`execute_bar()` 已刪除。factory 以 `simulator_cls=BarSimulator` 注入，引擎在建構時以自己為參數建立 simulator。
  - **偏離原規格一：每日迴圈多一個 `simulator.load_day(date) -> bool`**，實際流程是 `load_day` → `prepare_day` → `run_day` → `finish_day`。
    原本 `run_day_backtest()` 在當天沒有任何報價時**整天跳過**（連每日準備與收盤結算都不做）；若照原圖先 `prepare_day()` 再由 `run_day()` 取報價，
    沒有報價的日子也會覆寫漲跌停參考價，回歸可能看不出來。`TickSimulator` 的「當天查 tick」同樣放在 `load_day()`。
  - **偏離原規格二：simulator 經由 `SimulationEngine`（Protocol）取用引擎**，不 import `Backtester`：simulator 與 `core.backtest.models` 同層（第 4 層），引擎本體在第 5 層。
  - **`Scale.TICK` 暫時也走 `BarSimulator`**（整天的 tick 當一根 bar、`on_bar_open()` 照舊），行為與重構前相同；Phase3-1 改由 factory 依級別選 `TickSimulator`。
  - 分層檢查：`core.backtest.simulator` 登記為第 4 層與回測內部零件；「引擎不得出現市場字樣」的檢查範圍加上 `simulator/base.py`、`bar_simulator.py`（開平倉執行搬過去了，護欄要跟著走）。
  - 測試：`tests/backtest/conftest.py` 新增 `run_bar()`（每日準備 → `process_bar()` → 收盤結算，等同舊 `execute_bar()`）；`make_baseline.py` 改走真正的 `Backtester.run()`，回歸線守的就是正式迴圈；
    新增 `test_run_skips_market_day_without_any_quote`（健檢時發現「無報價日整天跳過」沒有任何測試守著，突變 `load_day()` 恆回 True 時只有這條會紅）。
  - 驗證：`run_regression.sh` 兩線實際執行、結束碼 0（研究庫以 APFS copy-on-write 複本經 `ALPHAEDGE_DATA_DIR` 提供，不碰主目錄）；含 slow 的完整 pytest 2866 passed；
    `VolumeBreakoutMomentumStrategy`、`ForeignSellingReversalShortStrategy`、`InvestmentTrustMomentumSwingStrategy`、`IntradayMomentumStrategy`、`MomentumFuturesStrategy` 以 2024 全年在重構前（`48752a8`）與重構後各跑一次，
    交易紀錄、每日權益、多空統計、事件、績效 5 種 CSV 共 25 個逐位元相同。突變「開平倉先後對調」5 條測試轉紅、「simulator 出現市場字樣」分層檢查報錯。
  - 文件：`docs/backtest/module-map.md`、`multi-market-engine.md`、`short-selling-framework.md`、`core/backtest/README.md`、根目錄兩份 README 的目錄樹同步。

### Phase1-5. 每日市場狀態改由各 model 自己準備 ✅

- **目的**：`prepare_day()` 目前由引擎逐一把台股專屬的每日狀態（融券餘額、停券、交易所名單、除權息參考價等）推給各 model，三個基底為此掛了約 15 個台股專屬的 no-op hook；
  期貨則在 factory 建構時注入 resolver。傳遞方式不對稱是漂移，內容本身才是市場差異（`框架對稱化重構.md`〈盤點結果〉第 2 組）。
- **做法**：
  - `fill_model.prepare_day(feed, date)`、`settlement.prepare_day(feed, date)`，基底預設 no-op；台股實作自己向 DataFeed 取當天需要的狀態。
  - `Backtester.prepare_day()` 改成依序呼叫各 model 的 `prepare_day()`，移除只為了推狀態而存在的 no-op hook；策略會呼叫的 DataFeed getter 保留。
  - **每日流程的順序一個都不能動**（見 Phase1-4）。
  - `框架對稱化重構.md` 提到的融券檢核改取 T−1（同樣在 `prepare_day()`）**會改變結果**，不在本步驟，等該文件〈待定案〉裁示。
- **產出**：上列檔案與測試。
- **驗證方式**：`scripts/run_regression.sh` 回歸雙線零變動（結束碼 3 不算通過）；期貨回測測試全綠；被移除的 hook 名稱 grep 為零。
- **相依**：Phase1-4。
- **結果（2026-10-11，`feature/tick-backtest`）**：
  - `BaseFillModel.prepare_day()`、`BaseSettlementModel.prepare_day()` 預設 no-op；台股兩個 model 覆寫：成交模型取漲跌停基準、融券餘額，可能放空時再取停券標的與（開啟檢核時）交易所名單；
    結算模型可能放空時取停券回補標的，兩個方向都取除權息與股數倍率。`Backtester.prepare_day()` 只剩 `cur_date` 前進與兩行委派，引擎只告訴 model「策略可不可能放空」。
  - 基底移除七個只為推狀態而存在的 no-op 鉤子（成交模型 4 個、結算模型 3 個）；台股實作的 `apply_*` 保留（單元測試直接驅動），grep 基底已無這些名稱。DataFeed 的 getter 不動（策略也會呼叫）。
  - **順序**：成交模型的取用先、結算模型後；原本兩者交錯（停券回補排在成交模型的停券之前），但兩邊只各自寫自己的狀態、資料源的 getter 不受呼叫順序影響，同一個 model 內的順序維持不變。
  - 驗證：回歸雙線零變動；五支策略 2024 全年 25 個報表 CSV 與 Phase1-4 後逐位元相同；完整 pytest 2922 passed（含期貨回測測試）。
    突變「結算模型不取股利」3 條轉紅；突變「成交模型不取停券」**原本沒有任何測試會紅**——停券擋板只有直接呼叫 `apply_short_suspended_symbols()` 的單元測試，
    引擎每天有沒有真的取停券標的沒人守（重構前把引擎那行拿掉同樣不會紅，屬既有的假綠燈），已補 `test_daily_preparation_feeds_suspension_to_fill_model` 走完整每日流程，突變轉紅。
  - 實作過程 2026-10-11 被中斷一次（驗證跑到一半）：程式改動完整保留，中斷的驗證已重跑。

### Phase2-1. tick 查詢支援多檔與當日累計量 ✅

- **目的**：策略清單模式要一次查「一天 × 數百檔」；實盤報價的 `volume` 是當日累計量，回測要能給出同一個值。
- **做法**：
  - `StockTickDAO.query_ticks()` 新增 `stock_ids: Optional[Sequence[str]]`。ConnectorX 不支援參數佔位符，值要嵌進 SQL：**每個代號都走現有的英數字驗證**，
    再組成 `stock_id IN (...)`；清單為空時直接回空表、不送查詢。
  - 累計量用 `sum(volume) OVER (PARTITION BY stock_id, time::date ORDER BY time, seq)` 在資料庫算好，回傳多一欄 `cum_volume`；
    或在 Python 端 `groupby().cumsum()`——兩者擇一，以 Phase5-2 的量測決定。
  - `StockTickAPI.get_ordered_ticks()` 加選填參數 `stock_ids: Optional[List[str]] = None`（不另開方法），回傳欄位契約寫進 docstring。
- **產出**：上列檔案；`tests/test_stock_tick_timescale.py`、`tests/test_stock_tick_api.py` 補測試。
- **驗證方式**：多檔查詢結果與逐檔查詢合併後相同；`cum_volume` 等於逐筆加總；不合法代號在送出查詢前就被拒絕。
- **相依**：無（可最先做）。
- **結果（2026-10-10，`feature/tick-backtest`）**：
  - **偏離原規格（累計量的算法提前定案）**：原訂兩種算法擇一、等 Phase5-2 量測再決定；實作時直接在真實資料（2024-05-08 全市場 1,256,763 筆）量了兩種：
    資料庫 window function 查詢 1.99 秒；不帶 window 查詢 0.81 秒＋pandas 依「股票 × 日期」`cumsum` 0.19 秒，兩者結果逐筆相同。採 pandas 端，Phase5-2 不必再比。
  - 因為累加依結果的列序進行，`query_ticks()` 的排序欄位去掉 `stock_id` 後必須是 `("time", "seq")`，其他排序一律 `ValueError`（現有三個呼叫端都符合）。
  - DAO 原本的 `stock_id` 參數改成 `stock_ids`（`get_stock_ticks()` 改傳單元素清單），不並存兩個參數；傳成單一字串時 `TypeError`（字串也是 Sequence，會被拆成單一字元）。重複代號在組 SQL 前去重。
  - 所有讀取介面都多回傳 `cum_volume`；`scripts/manual/manual_tick_history_import.py` 的抽樣比對改成只比原始欄位，`strategy_lab/README.md` 的欄位說明一併更新。
  - 驗證：`tests/test_stock_tick_api.py`（無資料庫：代號驗證、單一字串、排序限制、空清單不送查詢）與 `tests/test_stock_tick_timescale.py`（暫存 schema：多檔等於逐檔合併、重複與不存在的代號、累計量含當筆／跨股票獨立／隔天歸零）共 33 條全綠；
    突變「累計量不分日」「空清單照送查詢」各自讓對應測試轉紅。真實資料：成交量前 200 檔一天 827,836 筆 0.73 秒（全市場 0.91 秒），抽 3 檔比對清單查詢、全市場查詢與逐檔查詢逐列相同，當日最後一筆 `cum_volume` 等於成交量加總。

### Phase2-2. 台股逐筆資料源 `StockTickDataFeed` ✅

- **目的**：把一天的 tick 依時間順序逐筆交給引擎，語意與實盤推播一致。
- **做法**：
  - 新增 `StockTickDataFeed`（`core/backtest/datafeed/tw/stock_tick_datafeed.py`），**繼承 `StockDataFeed`**：TICK 回測同樣需要日頻資料
    （參考價、漲跌停基準、除權息、融券與當沖名單），沿用父類別；新增的只有逐筆部分：
    - `setup_day(date, symbols: Optional[List[str]])`：依 Phase1-2 的範圍查當天 tick（Phase2-1），每天只查一次；`symbols=None` 為全市場。
    - `iter_day_ticks() -> Iterator[StockQuote]`：依 `(time, stock_id, seq)` 逐筆產生 `StockQuote`（`scale=TICK`、`cur_price=close`、**`volume=當日累計量`**、`tick` 掛單筆的 `TickQuote`）。
      同一時間戳記跨股票的順序以代號固定，回測才可重現。用產生器而不是 `get_` 回傳 list：現行 `from_tick_rows()` 全市場一天要 3.5 秒、0.64 GB。
    - 段落快照：`get_reference_quotes()`（參考價）、`get_open_quotes()`（每檔第一筆）、`get_close_snapshot_quotes()`（13:20 前每檔最後一筆）、`get_close_quotes()`（每檔最後一筆）。
  - 現行 `StockDataFeed.get_quotes(date, Scale.TICK)` 整天一次給的路徑，在 Phase3-1 完成後移除。
- **產出**：新檔案與測試。
- **驗證方式**：順序、累計量、`tick.volume` 為單筆量、各快照的值都以手寫資料驗證。
- **相依**：Phase2-1。
- **結果（2026-10-10，`feature/tick-backtest`）**：
  - 新增 `core/backtest/datafeed/tw/stock_tick_datafeed.py` 的 `StockTickDataFeed(TwStockDataFeed)`：`setup_day(date, symbols)`（回傳筆數）、`iter_day_ticks()`（產生器）、
    `get_reference_quotes()`、`get_snapshot_quotes(cutoff)`、`get_close_quotes()`；快照一律依代號排序。（`get_open_quotes()` 在 Phase3-1 刪除：開盤集合競價就是逐筆裡各檔的第一筆。）
  - `StockQuoteAdapter.from_tick_row()` 的 `volume` 改成 `cum_volume`（單筆量留在 `tick_quote.volume`）。**現行 TICK 回測（整天一根 bar）的 `volume` 語意也跟著變成累計量**；
    目前沒有策略跑 TICK，`FillModel` 的成交量上限也只在 DAY 生效，不影響任何結果。
  - **偏離原規格一：`get_close_snapshot_quotes()`（13:20）改成 `get_snapshot_quotes(cutoff)`**，時點由呼叫端傳入：段落時點是市場特性，實盤也由外部注入（`core/live/factory.py` 的段落表，台股收盤段 13:25 才送單），
    資料源寫死 13:20 會和實盤的段落對不上。Phase3-1 的 `TickSimulator` 依段落設定傳入時點。
  - **偏離原規格二：`get_reference_quotes(reference_prices)` 由呼叫端傳入參考價**（引擎成交模型持有的 `reference_prices`），漲跌停用 `TwStockSpec.get_price_limits()` 算；
    不在資料源另算一份前收，兩份來源遲早不一致。沒有參考價的標的略過（回測第一天），與實盤取不到參考價時的處理相同。產出 `PreOpenStockQuote`，讀 OHLC 會拋出，與實盤開盤段一致。
  - **定案 Phase3-2 留下的選擇：一般股過濾放在資料源**（`setup_day()` 內，指定清單先濾再查）：日 K 路徑在報價轉換時就濾掉 ETF 與權證，兩種級別的策略看到的標的池才一致。
    實測 2024-05-08 的 tick 庫沒有 ETF（1,811 檔全是一般股），過濾目前是防線而非必要。
  - 驗證：`tests/backtest/test_stock_tick_datafeed.py` 8 條（手寫 tick：交錯順序、累計量與單筆量、清單去重排序與一般股過濾、全市場範圍、開盤／快照／收盤、快照不含時點本身、盤前報價與 OHLC 拋出、空日）；
    `tests/test_stock_tick_timescale.py` 新增接真 API 的逐筆測試。突變「拿掉一般股過濾」「volume 改回單筆量」「快照改成含時點」各自轉紅。
    真實資料 2024-05-08 全市場：1,256,763 筆、1,811 檔，`setup_day()` 0.99 秒、逐筆轉成報價 2.29 秒，收盤報價的 `volume` 等於當日成交量加總。回歸雙線零變動；完整 pytest 2886 passed。

### Phase3-1. `TickSimulator`：一天三段與逐筆迴圈 ✅

- **目的**：取代 `run_tick_backtest()` 的「整天一根 K 棒」，讓策略只看得到當下以前的資料。
- **做法**：新增 `TickSimulator(BaseSimulator)`（`core/backtest/simulator/tick_simulator.py`），`run_day(date)` 依序跑三段，各鉤子在哪一段呼叫依策略的 `execution_schedule`（〈已定案〉第 6 點）：
  1. **`process_opening_auction()`（對應 `AT_OPEN`）**：以參考價快照呼叫排在這段的鉤子；委託進開盤集合競價，以開盤價成交。
  2. **`process_tick(quote)`（對應 `IMMEDIATE`，逐筆迴圈）**：每一筆 tick 先撮合在途委託（Phase3-2），再呼叫排在這段的鉤子，傳入長度 1 的 list；
     開倉與停損、平倉的先後照 `allow_day_trade`，與實盤 `LiveTrader._invoke_hooks()` 相同。停損與平倉只傳**該筆 tick 的那一檔**，解決「同一檔數千筆報價重複平倉」。
  3. **`process_closing_auction()`（對應 `AT_CLOSE`）**：以 13:20 快照呼叫排在這段的鉤子；委託進收盤集合競價，以收盤價成交。
  - `run_day()` 回傳每檔一筆的收盤報價，交給 `Backtester.finish_day()`。
  - 委託帶下單時間（觸發那一筆 tick 的時間），報表的成交紀錄也記時間。
  - 標的範圍（Phase1-2）由 `TickSimulator` 每天盤前算好交給 `StockTickDataFeed.setup_day()`。
  - 移除 `Backtester.run_tick_backtest()`、`_reject_intraday_tick_backtest()` 與它的測試，改成新契約的測試；`factory.py` 在 TICK 時組出 `TickSimulator` 與 `StockTickDataFeed`。
- **產出**：上列檔案。
- **驗證方式**：手寫兩檔交錯的 tick，驗證鉤子呼叫順序、每次只拿到一筆、段落時點；策略在第 N 筆時看不到第 N+1 筆。
- **相依**：Phase1-1、Phase1-3、Phase1-4、Phase2-2。
- **結果（2026-10-10，`feature/tick-backtest`，與 Phase3-2 同一個 commit）**：
  - 前置重構（獨立 commit）：段落時窗 `SegmentWindow` 與台股／期貨段落表由 `core/live/` 移到 `core/market/segment.py`、`core/market/tw/segment_schedule.py`，回測與實盤共用同一張表。
  - `core/backtest/simulator/tick_simulator.py` 的 `TickSimulator(engine, schedule)`：`load_day()` 依標的範圍查 tick（策略清單模式以 `StockTickDataFeed.get_previous_trading_day()` 取前一交易日）；
    `run_day()` = `process_opening_auction()` → 逐筆 `process_tick()`（先更新當日區間、再撮合在途委託、最後在 `IMMEDIATE` 時窗 09:05–13:18 內呼叫鉤子）→ 13:25 時插入 `process_closing_auction()` → 收盤作廢未成交委託。
  - **收盤段插在逐筆迴圈中間**：讀到第一筆 ≥ 13:25 的 tick 時才呼叫 `AT_CLOSE` 的鉤子，之後的 tick 才撮合；整天跑完再呼叫的話，鉤子會看到 13:30 才發生的成交。
  - **開盤集合競價＝各檔當天第一筆**（不另查開盤快照，Phase2-2 的 `get_open_quotes()` 因此刪除）；該檔第一筆若不在 09:00（開盤沒撮合），仍以它當開盤成交，屬已知近似。
  - **策略清單模式下清單與持倉都是空的日子照樣跑**（權益照記）；有標的卻沒有 tick、或全市場模式沒有資料，才整天跳過。
  - 引擎介面：`Backtester` 的 `simulator_cls` 改為 `build_simulator: Callable[[Backtester], BaseSimulator]`（逐筆 simulator 要多帶段落表）；simulator 改在級別與標的範圍檢查之後才建立。
  - 移除：`Backtester._reject_intraday_tick_backtest()`、`IntradayScaleMismatchError`、`FillModel.on_bar_open()`、`TwStockDataFeed.get_quotes(date, Scale.TICK)`（改成拋錯並指向 `StockTickDataFeed`）、`StockQuoteAdapter.from_tick_rows()`。
  - 逐筆才有的事件 `expired_ioc`、`expired_at_close`、`rejected_duplicate_pending` 放在 `TICK_EVENT_KEYS`，由 `TickSimulator` 建立時加進計數器；**不放進 `EVENT_KEYS`**，日 K 的事件報表與 SHORT 回歸基準不受影響。
  - 開倉與一般平倉的執行方式取策略的 `live_execution`（停損一律市價），沒宣告就拒絕建立 `TickSimulator`。
  - 真實資料（2024-05-08 全市場、不交易的策略）：鉤子呼叫 1,103,956 次，一天 9.0 秒，峰值記憶體 639 MB。

### Phase3-2. 逐筆成交模型 ✅

- **目的**：沒有前視、貼近實盤的成交規則。
- **做法**：
  - 在途委託：`PendingOrderManager`（`core/backtest/simulator/pending_orders.py`）保存 `PendingOrder`（訂單、階段、送出時間、IOC／ROD），
    提供 `add()`、`get_orders(symbol)`、`remove()`、`expire_ioc(symbol)`、`cancel_all()`。委託送出後，**同一檔的下一筆 tick** 才嘗試撮合。
  - `TwStockFillModel` 新增 `match_tick(pending, quote) -> Optional[BaseOrder]` 與 `get_tick_fill_price(order, quote) -> Optional[float]`。
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
- **結果（2026-10-10，`feature/tick-backtest`）**：
  - `core/backtest/simulator/pending_orders.py`：`PendingOrder`（訂單、階段、執行方式、IOC／ROD、送出時間、是否開盤段）與 `PendingOrderManager`（`add`／`get_orders`／`remove`／`has_open`／`has_exit`／`open_symbols`／`cancel_all`）。
  - `BaseFillModel.get_tick_fill_price()`：對手方為 0 不成交（集合競價也一樣）；集合競價以該筆成交價、限價要不劣於限價；連續競價市價吃 `ask_price`／`bid_price`；限價要成交價**穿越**（不含碰到）才以限價成交。
  - `TwStockFillModel.fill_tick()`：名單 → 券源 → 停券（**以實際成交價判斷平盤下**，市價放空吃委買價可能落到平盤下）→ 滑價（預設 0，bid/ask 已含價差）→ 成交量上限；策略的委託價留在副本的 `reference_price`。
    **不經 `validate()` 的區間檢查與夾價**：市價買在委賣價本來就可能高於目前為止的最高成交價，用成交區間去擋會拒掉正常成交或夾成沒人賣的價格。
  - 成交量上限（〈已定案〉第 10 點）：連續競價以這一筆的對手方委託量為分母、集合競價以這一筆的成交量為分母（撮合後對手方掛單常是 0）；截斷的部分直接放棄，不留在途。
  - 重複委託：已有在途出場委託的標的不再拿去問停損與平倉；已有在途開倉委託時新的開倉單略過並計入 `rejected_duplicate_pending`；持倉檔數上限把在途開倉委託也算進名額（與實盤相同）。
  - **偏離原規格：`get_filled_price(order)` 不改簽章**。原規劃要把報價傳進去讓它吃 bid/ask，但逐筆的成交價在呼叫它之前就由 `get_tick_fill_price()` 決定，它只負責疊加策略明確設定的 bps；
    結算層的強制出場在日終以收盤報價執行，用不到 bid/ask。期貨與 `apply_fill_price()` 因此都不用動。
  - 驗證：`tests/backtest/test_tick_simulator.py` 16 條端到端（手寫 tick、完整跑 `Backtester.run()`）：鉤子每次一筆且只在時窗內、看不到之後的 tick、市價買吃下一筆委賣、市價賣吃委買、鎖死時 IOC 作廢、
    限價碰到不成交／穿越才成交、限價收盤作廢、成交量上限分母、開盤段拿盤前報價並以第一筆成交、收盤段看 13:25 前快照並以收盤集合競價成交、停損只送一次、掛著的平倉不再被問、
    在途開倉佔名額、重複開倉被略過、跑兩次結果相同；`test_trading_list_constraints.py` 新增逐筆放空以成交價判斷平盤下。
    七個突變（當筆成交、市價改用成交價、限價碰到就成交、不擋重複出場、在途不佔名額、收盤段整天跑完才呼叫、IOC 不作廢）全部轉紅；前兩版測試漏掉的兩個突變已補測試。回歸雙線零變動；完整 pytest 2910 passed。

### Phase3-3. 盤中帳務與日終結算 ✅

- **目的**：盤中的成交要即時反映在可用餘額與持倉檔數上，否則同一天後面的訊號會用錯的資金。
- **做法**：
  - 每筆成交立即更新帳戶與持倉（`position_manager`）；`check_max_holdings` 以當下持倉計算。
  - 日終走 Phase1-4 抽出的 `Backtester.finish_day()`（內含 `settlement.settle_day()`：當沖未回補處理、借券費、盯市、權益快照、次日漲跌停基準），傳入 `TickSimulator.run_day()` 回傳的每檔一筆收盤報價，不再是整天的 tick。
- **產出**：上列檔案與測試。
- **驗證方式**：同一天先買後賣的當沖，帳務與 DAY 路徑同一筆交易的口徑一致（稅費、當沖減半）；盤中資金不足時後面的委託被擋。
- **相依**：Phase3-2。
- **結果（2026-10-10，`feature/tick-backtest`）**：程式不必改——`TickSimulator.execute_fill()` 成交當下就呼叫 `position_manager.open_position()`／`close_position()`，
  帳務（含「開平倉同一天且策略允許當沖才減半」的判斷）與日 K 共用同一份；持倉檔數以當下部位加在途開倉委託計算（Phase3-2）；日終由 `finish_day()` 以每檔最後一筆結算。
  新增三條測試：同價同量的當沖來回，逐筆與日 K 的手續費、證交稅、損益完全相同（並確認證交稅確實減半為 153）；第一筆成交用掉資金後，後面的開倉單以 `rejected_insufficient_balance` 被擋；
  持有到收盤的部位以最後一筆盯市、次日參考價記成最後一筆。完整 pytest 2914 passed；回歸雙線零變動。

### Phase4-1. `IntradayMomentumStrategy` 支援 TICK 回測 ✅

- **目的**：讓盤中動能策略用逐筆資料回測，取代偏樂觀的日 K 近似。
- **做法**：
  - `supported_scales` 設為 `{Scale.DAY, Scale.TICK}`，預設維持 `DAY`，不改既有 S2 的結果。
  - 移除 `setup_apis()` 對 TICK 的 `NotImplementedError`；逐筆分支 `generate_tick_open_signals()` 已是實盤用的，回測直接共用。
  - 停損、隔天開盤出場在 TICK 回測照 `execution_schedule` 的時點執行。
- **產出**：策略檔與 `tests/test_intraday_momentum_strategy.py`。
- **驗證方式**：手寫 tick 驗證觸發、停損、隔天開盤出場；DAY 回測結果不變。
- **相依**：Phase3-3。
- **結果（2026-10-10，`feature/tick-backtest`）**：策略只改三處（`supported_scales`、刪掉 `setup_apis()` 的拒絕、class docstring 補〈逐筆回測〉），逐筆分支 `generate_tick_*` 原封不動。
  `tests/test_intraday_momentum_tick_backtest.py`：累計量未達門檻不進場、達門檻以下一筆委賣價買進、沒停損就以隔天第一筆（開盤集合競價）賣出；進場當天跌破停損價以下一筆委買價賣出、之後再跌不重送。
  突變「不宣告 TICK」三條全紅。**DAY 結果不變**：五支策略 2024 全年重跑，與 Phase1-4 重構後的 25 個報表 CSV 逐位元相同（涵蓋 Phase1-1 以來所有變更）。
  移除 `test_tick_scale_is_rejected`（契約已改）。

### Phase4-2. 既有 DAY 策略宣告支援範圍 ✅

- **目的**：日 K 策略依賴 OHLC，吃到 tick 會默默算錯；要明確宣告。
- **做法**：預設值已經是「只支援自己的 `self.scale`」，這一步逐支確認並在 `strategies/README.md` 寫清楚怎麼宣告。
- **產出**：`strategies/README.md`；必要時調整策略檔。
- **驗證方式**：每支策略加 `--scale tick` 時，只支援 DAY 者當場報錯。
- **相依**：Phase1-1。
- **結果（2026-10-10，`feature/tick-backtest`）**：逐支確認其餘五支（含兩支期貨）都沒有覆寫 `supported_scales`，預設只支援自己的 `scale`，不必改策略檔。
  `tests/backtest/test_backtest_scale.py` 新增以 `StrategyLoader` 逐支驗證：清單（`TICK_CAPABLE_STRATEGIES`，目前只有 `IntradayMomentumStrategy`）以外的策略要求 TICK 一律 `UnsupportedScaleError`；
  新增支援 TICK 的策略忘了更新清單也會轉紅。`strategies/README.md` 寫明怎麼宣告。

### Phase5-1. 端到端測試 ✅

- **目的**：把五個現存問題（見 Abstract）各自釘一個會紅的測試。
- **做法**：手寫小量 tick（不讀正式資料表），用測試替身的資料源跑完整回測：
  - 無前視：在第 N 筆觸發的委託，只能用第 N+1 筆以後的價格成交。
  - 下一筆成交、吃 bid/ask。
  - 決定性：同一份資料跑兩次，成交紀錄逐位元相同。
  - 多檔交錯：同一時間戳記跨股票的順序固定。
  - 停損只對該筆的那一檔觸發一次。
- **產出**：`tests/backtest/test_tick_simulator.py`。
- **驗證方式**：測試全綠；逐一突變對應的實作（例如改回當筆成交）時對應測試會紅。
- **相依**：Phase3-3。
- **結果（2026-10-10，`feature/tick-backtest`）**：清單逐項對應 `tests/backtest/test_tick_simulator.py`——無前視（`test_hooks_never_see_a_later_tick`、`test_market_buy_fills_at_next_tick_ask_of_same_stock`）、
  下一筆成交吃 bid/ask（市價買、市價賣兩條）、決定性（`test_same_data_twice_gives_identical_trades`）、多檔交錯順序（`test_immediate_hooks_get_one_quote_per_call_inside_the_window`）、
  停損只觸發一次（`test_stop_loss_is_submitted_once_per_position`，另有 ROD 平倉不被重問）。突變紀錄見 Phase3-2 的結果（七個突變全紅）。

### Phase5-2. 真實資料實跑與效能量測 ✅

- **目的**：確認框架在真實資料上可用，並回答盤中動能策略的真實表現。
- **做法**：
  - `IntradayMomentumStrategy` 以 `--scale tick` 跑一段區間（先一個月量效能，再決定是否跑完整 2020-05～2024-05），兩種標的範圍各一次。
  - 記錄每交易日耗時、峰值記憶體；全市場模式若太慢，記下瓶頸（查詢、建物件、策略呼叫）。
  - 與 `盤中動能策略.md` S2 的日 K 近似逐項對照：交易筆數、觸發時間、進場價、停損次數、平均報酬，差異寫回該文件。
- **產出**：本文件末尾的實跑紀錄、`盤中動能策略.md` 的對照。
- **驗證方式**：兩種模式都跑完、0 錯誤；紀錄完整。
- **相依**：Phase4-1、Phase5-1。

> **✅ 實跑紀錄（2026-10-10～10-11，`feature/tick-backtest`，研究庫為主目錄的 copy-on-write 複本）**
> - **一個月效能**（2024-04-01～04-30，21 個交易日，`IntradayMomentumStrategy --scale TICK`，初始資金 100 萬）：
>
>   | 標的範圍 | 總耗時 | 每交易日 | 峰值記憶體 |
>   |----------|:--:|:--:|:--:|
>   | `strategy`（前一交易日當沖名單內成交量前 190 檔＋持倉） | 211 秒 | 約 10 秒 | 774 MB |
>   | `all`（全市場） | 375 秒 | 約 18 秒 | 1.12 GB |
>
>   對照：不交易的策略跑全市場一天 9 秒（Phase3-1 量測），多出的時間花在策略每筆計算訊號。策略清單模式沒有比全市場快一半，因為成交量前 190 檔就佔了大部分的 tick。
> - **同月份與日 K 近似的初步對照**：
>
>   | 版本 | 平倉筆數 | 當天停損出場 | 勝率 | 平均每筆 ROI | 月底權益 | 逐筆事件 |
>   |------|:--:|:--:|:--:|:--:|:--:|------|
>   | DAY（日 K 近似，S2 採用版本） | 116 | 40 | 57.8% | +2.07% | 123.7 萬 | 鎖漲停拒單 2 |
>   | TICK `strategy` | 133 | 62 | 38.3% | +0.14% | 100.9 萬 | IOC 作廢 24、成交量截斷 25 |
>   | TICK `all` | 201 | 81 | 39.3% | +0.39% | 107.2 萬 | IOC 作廢 59、成交量截斷 42 |
>
>   一個月的樣本太小，但方向與 `盤中動能策略.md` S2 的判斷一致：日 K 近似明顯偏樂觀（勝率高 18～20 個百分點、平均每筆高 1.7～1.9 個百分點），
>   當天停損多了五成以上（日 K 的陽線路徑會漏判盤中跌回停損價）。策略清單模式比全市場少三分之一的交易，印證「前一天冷門、當天才爆量的股票會漏掉」。
> - **一年對照（2026-10-11）**：使用者看過一個月的量測後裁示「先改成跑一年」。
>   一年對照的完整數字（2023-05-01～2024-05-10，初始資金 100 萬，複利；完整區間 2020-05 起依使用者裁示先不跑）：
>
>   | 版本 | 平倉筆數 | 當天停損（平均 ROI） | 隔天出場（平均 ROI） | 勝率 | 平均每筆 | 期末權益 | MDD | Sharpe |
>   |------|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
>   | DAY（日 K 近似） | 1,473 | 541（−1.39%） | 932（+2.14%） | 47.3% | +0.84% | 317.7 萬 | −4.9% | 9.56 |
>   | TICK `strategy` | 1,231 | 523（−1.44%） | 708（+0.87%） | 36.5% | −0.12% | 99.7 萬 | −8.8% | 0.03 |
>   | TICK `all` | 1,791 | 679（−1.47%） | 1,112（+0.73%） | 37.0% | −0.11% | 98.7 萬 | −15.6% | −0.03 |
>
>   同一檔同一天兩邊都進場的交易（`strategy` 477 筆、`all` 725 筆）逐筆配對：
>   - **進場價幾乎相同**（逐筆對日 K 平均 +0.06%／+0.11%，中位數 0）——日 K「以觸發價成交」不是主要偏差。
>   - **出場日相同的只有 60%／64%**，這批共同交易的平均報酬從 +1.23%／+1.02% 掉到 +0.05%／−0.06%：日 K「陽線只看收盤」把盤中其實跌回停損價的交易當成撐住了。
>   - **兩邊重疊的只有三到五成**：日 K 用全日總量判斷 5,000 張門檻（偷看了收盤，挑到的是收盤仍強的股票），逐筆以觸發當下的累計量判斷，進場的是另一批股票。
>
>   **結論：日 K 近似的報酬幾乎全來自偏樂觀的假設，逐筆重放下這支策略一年是持平（不計交易成本以外的任何損失）。**
>   逐筆事件：IOC 作廢 300／692 次；成交量上限拒單 423／668 次、截斷 263／412 次。拒單幾乎都是開倉單——這支策略的 10% 上限套到「下一筆的委賣量」時，委賣不到 10 張就整筆拒單；
>   出場被拒造成部位多留的只有 6／11 筆（持有 15～16 個工作日，跨 2024 春節連假後才出得掉），佔不到 1%。
>   效能（一年）：`strategy` 1,726 秒（每交易日約 7 秒）、峰值 971 MB；`all` 2,917 秒（約 12 秒）、峰值 1.37 GB；DAY 18 秒。
> - **偏離原規格**：完整區間（2020-05～2024-05-10）沒有跑，依使用者裁示以一年代替；要補跑時兩種範圍合計約 8 小時。
> - **待使用者裁示（不影響本文件完成）**：策略的 `MAX_VOLUME_SHARE`（10%）是為日 K「當日總量」設計的，逐筆以「下一筆對手方委託量」為分母時常常不足 1 張而拒單。要不要讓逐筆另設比例、或不足 1 張時至少成交 1 張，屬策略與成交假設的口徑，記在 `盤中動能策略.md` S4 一併檢討。

### Phase5-3. 文件 ✅

- **目的**：現行說明文件描述新的 TICK 回測語意。
- **做法**：更新 `core/backtest/README.md`（`Scale.TICK` 列、成交規則、已知限制）、`strategies/README.md`（支援級別的宣告、逐筆契約、標的範圍）、`docs/backtest/module-map.md`；
  移除各處「TICK 回測整天一次給、有前視」的註解與說明。
- **產出**：上列文件。
- **驗證方式**：`scripts/check_doc_paths.py` 通過；`grep -rn "整天的 tick 一次" core strategies docs` 無結果。
- **相依**：Phase5-2。
- **結果（2026-10-11，`feature/tick-backtest`）**：說明文件在各步驟實作時就同步寫入，本步驟補上效能數字並做最後檢查。
  `core/backtest/README.md`〈`Scale.TICK`：逐筆重放〉：三段與時點、成交規則、累計量語意、成交量上限分母、重複委託與名額、已知與實盤的差異、效能；〈回測參數〉列出 `--scale`、`--tick-universe`。
  `strategies/README.md`：`supported_scales`、執行段落設定表、`--tick-universe`、逐筆觸發策略可跑 TICK。`docs/backtest/` 三份的引擎流程與名稱同步。
  `check_doc_paths.py` 通過；`grep` 舊說明（整天的 tick 一次、全日高低點、`IntradayScaleMismatchError`）無結果。
- **整份完成後**：程式全在 `feature/tick-backtest`，要等 `feature/post-rehearsal` 與 `feature/tick-timescaledb` 依序合併後才能進 main（Phase1-3 動到實盤，合併後要重啟常駐行程）；
  進 main 之後整份移出 `backlog/`，現行說明已在上列文件，不需另寫 `docs/`。

## 風險與對策

- **全市場模式太慢**：每天約 120 萬筆，Python 逐筆呼叫策略一天估數十秒。對策：預設用策略清單模式；Phase5-2 量測後若有需要，再做「沒有任何在途委託與持倉、且策略不關心的標的跳過」之類的最佳化。
- **與實盤的已知差異**：回測的跨股票到達順序以 `(time, stock_id, seq)` 固定，實盤取決於券商推送；回測沒有網路延遲（委託在下一筆就可能成交）。這兩點寫進 Phase5-3 的已知限制。
- **歷史資料止於 2024-05-10**：tick 回補不在本文件範圍；回測區間受限於資料範圍。

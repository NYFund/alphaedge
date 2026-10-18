# 台股 tick 級回測框架

## Abstract

- **背景／問題**：2026-10-10 檢查發現當時的 `Scale.TICK` 回測不是逐筆模擬——`Backtester.run_tick_backtest()` 把**一整天的 tick 當成一根 K 棒**一次交給策略與成交模型：
  1. **前視**：成交驗證用全日高低點，早盤的委託可以用下午才出現的價位成交。
  2. **沒有時間軸**：委託不帶時間，成交對照的報價一律是該檔當天最後一筆。
  3. **同一檔的報價數千筆**：停損與平倉把整天的 tick 全交給策略，會重複送平倉單。
  4. **成交量上限未實作**：TICK 的 `volume` 是單筆量，`get_filled_volume()` 只在 DAY 生效。
  5. **`volume` 語意與實盤不同**：實盤 TICK 報價的 `volume` 是當日累計量，回測卻是單筆量。
  6. 宣告 `is_tick_triggered` 的策略被 `_reject_intraday_tick_backtest()` 擋下，不能做 tick 回測。
- **目標**：**逐筆重放**的 tick 級回測框架——策略宣告支援的級別、回測 `--scale` 覆寫；一次一筆、與實盤 `is_tick_triggered` 同契約；下單後同一檔的下一筆 tick 才成交、市價吃 bid/ask；標的範圍「策略清單／全市場」回測前自選；日 K 與 tick 結構對稱（`Backtester` 只留逐日迴圈，一天怎麼跑交給 `BarSimulator`／`TickSimulator`）。
- **目前狀態（2026-10-18 實查）**：**16 / 16 項都在 `feature/tick-backtest` 完成，剩下的只有〈合併部署收尾〉**。上述六個問題在 main 上仍存在（main 也沒有 tick 資料源，見 [台股tick改用TimescaleDB.md](台股tick改用TimescaleDB.md)）。
- **範圍界線**：
  - **只做台股**（期貨 tick 已裁示不做）。
  - **不做時間切片**（2026-10-10 使用者選逐筆）；需要橫斷面比較的策略照實盤規則自己保存各標的最新報價。
  - **不改 DAY 回測的結果**（回歸雙線零變動）。
  - **不做委託簿撮合、排隊位置、市場衝擊**：市場衝擊是 `滑價模型後續優化.md` S4 的範圍。
  - 不做盤後零股、定價交易；不做實盤側的行為修改（Phase1-3 只是改名）。
- **驗收標準**（五項都已在功能分支驗過；整份要等〈合併部署收尾〉做完才結案）：
  1. 手寫 tick 的端到端測試證明沒有前視、下一筆成交與 bid/ask 規則、跑兩次逐位元相同。**已驗**（Phase5-1）。
  2. `IntradayMomentumStrategy` 以 `--scale TICK` 在真實資料上兩種標的範圍各跑一段，與日 K 近似對照、差異寫回 `盤中動能策略.md`。**已驗**（Phase5-2，一年）。
  3. 現有 DAY 策略回歸雙線零變動；只支援 DAY 的策略加 `--scale TICK` 當場報錯。**已驗**（Phase1-1、Phase4-2）。
  4. 兩種標的範圍的每交易日耗時與峰值記憶體已記錄。**已驗**（Phase5-2）。
  5. 日 K 抽成 `BarSimulator` 後回歸雙線零變動。**已驗**（Phase1-4）。

---

## 合併部署收尾（剩下的工作）

### 時機與合併順序

- 分支鏈 `feature/post-rehearsal` → `feature/tick-timescaledb` → **`feature/tick-backtest`** → `feature/framework-symmetry`，每一條包含前一條的全部內容（2026-10-18 實查都不落後 main）。
- 本分支排在 `feature/tick-timescaledb` 之後合併，全部都要等 [實盤下單架構規劃.md](實盤下單架構規劃.md) Phase7-1（模擬環境端到端演練）結束。
- **前提**：`台股tick改用TimescaleDB.md`〈合併部署收尾〉第 1～3 項已做（主目錄裝好 `[tick]` 相依、TimescaleDB 在跑），否則 TICK 回測連不上資料。

### 合併後的動作清單（在主目錄執行）

| # | 動作 | 驗證 |
|:-:|------|------|
| 1 | **`live_schedule` → `execution_schedule` 改名要涵蓋合併當下 main 上的所有策略**（Phase1-3 動到實盤：設定舊名的策略在回測與實盤啟動時都會報錯）。2026-10-18 實查：`feature/stock-rehearsal`（`實盤下單架構規劃.md` Phase7-27，預計 2026-10-21 盤後部署進 main）新增的 `core/strategies/stock/stock_round_trip_rehearsal_strategy.py` 仍寫 `self.live_schedule`、也還在舊目錄。合併 `feature/post-rehearsal` 時要把它搬到 `strategies/stock/`（那一步的事），合併本分支時要改成 `execution_schedule`，它的測試（`tests/test_stock_round_trip_rehearsal_strategy.py`、`tests/live/test_stock_round_trip_rehearsal_live.py`）一併同步 | `grep -rn "live_schedule" strategies core apps tests` 只剩 `_REMOVED_SETTINGS` 對照表與移除欄位測試；`pytest tests/live` 全綠 |
| 2 | 完整檢查：`pytest`、`scripts/run_regression.sh`（回歸雙線零變動，結束碼 3 不算通過）、`python scripts/check_layer_deps.py` | 全綠 |
| 3 | 主目錄冒煙：`python -m apps.backtest --strategy IntradayMomentumStrategy --scale TICK --tick-universe strategy --start 2024-05-06 --end 2024-05-10` | 0 錯誤跑完，每交易日約 7～10 秒 |

- **不需要重啟行程**：原文寫「合併後要重啟常駐行程」，2026-10-18 實查 `~/Library/LaunchAgents/com.alphaedge.live.*.plist` 都沒有 `KeepAlive`，每個段落與 watchdog 都是每次新起行程，下一個段落就會用新程式；只要合併避開段落執行中的時間。
- **已知的小落差（合併前後都可以修，不擋移出）**：`core/utils/constant/live.py` 的 `ExecutionStyle`／`ExecutionTiming` docstring 仍寫「回測忽略此欄位」，但 TICK 回測已依 `live_execution` 與 `execution_schedule` 執行（Phase3-1、Phase3-2），只有 DAY 回測仍忽略。

### 整份移出的條件

上表 3 項做完即整份移出 `backlog/`：

1. 現行說明已在 `core/backtest/README.md`〈`Scale.TICK`：逐筆重放〉、`strategies/README.md`、`docs/backtest/` 三份（Phase5-3），不必另寫 `docs/`。
2. 刪除 `backlog/index.md` 對應列。
3. 其他 backlog 連到本文件的地方改成純文字或指向 `core/backtest/README.md`（2026-10-18 有連結的：`框架對稱化重構.md`、`滑價模型後續優化.md`、`盤中動能策略.md`、`策略與執行分層.md`、`台股tick改用TimescaleDB.md`），跑 `scripts/check_doc_paths.py` 確認沒有斷鏈。
4. 移出不需要等的事：完整區間（2020-05～2024-05）實跑依使用者裁示以一年代替（要補跑時兩種範圍合計約 8 小時）；逐筆成交量上限與 IOC 作廢頻率的檢討已轉到 `盤中動能策略.md`〈逐筆回測對照〉的「給 S4 的待確認事項」。

---

## 進度追蹤表

| 編號 | 步驟名稱 | 產出檔案 | 驗證方式 | 狀態 | 備註／中斷點 |
|------|----------|----------|----------|:----:|--------------|
| Phase0-1 | 定案介面細節與命名 | 本文件〈已定案〉 | 使用者確認 | ✅ | 2026-10-10：介面七點、架構命名、引擎內部改名、成交規則全數定案 |
| Phase1-1 | 策略宣告支援的級別（`supported_scales`）＋回測 `--scale` 覆寫 | `core/strategies/base.py`、`apps/backtest.py`、`core/backtest/backtester.py` | 範圍外的 `--scale` 當場報錯；不帶時行為不變 | ✅ | 2026-10-10（`fb950c4`） |
| Phase1-2 | 標的範圍選項（策略清單／全市場） | `apps/backtest.py`、`core/backtest/simulator/tick_universe.py` | 兩個選項各自只讀到預期的標的；無清單選 `strategy` 當場報錯 | ✅ | 2026-10-10（`0c0248d`） |
| Phase1-3 | `live_schedule` 改名為 `execution_schedule`（回測與實盤共用） | `core/strategies/base.py`、`core/live/`、`strategies/`、`tests/` | `grep live_schedule` 只剩對照表；實盤與回測測試通過 | ✅ | 2026-10-10（`74296b0`）：動到實盤，合併時見〈合併部署收尾〉第 1 項 |
| Phase1-4 | 日 K 路徑抽成 `BarSimulator`（純重構）＋引擎內部兩處改名 | `core/backtest/simulator/`、`core/backtest/backtester.py`、`core/backtest/factory.py`、`core/backtest/models/` | 回歸雙線零變動；25 個報表 CSV 逐位元相同 | ✅ | 2026-10-10（`48752a8`、`1502a87`）：多一個 `load_day()`（偏離原規格） |
| Phase1-5 | 每日市場狀態改由各 model 自己準備 | `core/backtest/backtester.py`、`core/backtest/models/` | 回歸雙線零變動；基底 no-op hook 移除 | ✅ | 2026-10-11（`3ef3f38`） |
| Phase2-1 | tick 查詢支援多檔與當日累計量 | `core/dao/tw/stock_tick_dao.py`、`core/api/tw/stock_tick_api.py` | 多檔＝逐檔合併；累計量＝逐筆加總 | ✅ | 2026-10-10（`d0e4002`）：累計量在 pandas 端算 |
| Phase2-2 | 台股逐筆資料源 `StockTickDataFeed` | `core/backtest/datafeed/tw/stock_tick_datafeed.py` | 依 `(time, stock_id, seq)` 逐筆；`volume` 為累計量 | ✅ | 2026-10-10（`7e84742`）：快照時點與參考價改由呼叫端傳入（偏離原規格） |
| Phase3-1 | `TickSimulator`：一天三段與逐筆迴圈 | `core/backtest/simulator/tick_simulator.py`、`core/backtest/factory.py` | 三段依 `execution_schedule` 呼叫鉤子；取消對 `is_tick_triggered` 的拒絕 | ✅ | 2026-10-10（`07d0821`，與 Phase3-2 同一個 commit）：全市場一天 9 秒 |
| Phase3-2 | 逐筆成交模型與在途委託 | `core/backtest/models/fill_model.py`、`core/backtest/simulator/pending_orders.py` | 下一筆成交、bid/ask、限價穿越、鎖死、IOC、成交量上限 | ✅ | 2026-10-10（`07d0821`）：`get_filled_price()` 不改簽章（偏離原規格）；`滑價模型後續優化.md` S3 一併 ✅ |
| Phase3-3 | 盤中帳務與日終結算 | `tests/backtest/test_tick_simulator.py` | 當沖口徑與 DAY 一致；盤中資金不足時後面的委託被擋 | ✅ | 2026-10-10（`6b887bc`）：不需改程式，補三條測試 |
| Phase4-1 | `IntradayMomentumStrategy` 支援 TICK 回測 | `strategies/stock/intraday_momentum_strategy.py` | 手寫 tick 驗證觸發、停損、隔天開盤出場 | ✅ | 2026-10-10（`b1f18e4`） |
| Phase4-2 | 既有 DAY 策略宣告支援範圍 | `strategies/README.md`、`tests/backtest/test_backtest_scale.py` | 只支援 DAY 者加 `--scale TICK` 報錯 | ✅ | 2026-10-10（`b1f18e4`）：其餘策略維持預設，不必改檔 |
| Phase5-1 | 端到端測試 | `tests/backtest/test_tick_simulator.py` | 無前視、下一筆成交、決定性、多檔交錯 | ✅ | 2026-10-10：19 條 |
| Phase5-2 | 真實資料實跑與效能量測 | 本文件、`盤中動能策略.md` | 兩種範圍各跑一段；記錄耗時與記憶體；與日 K 對照 | ✅ | 2026-10-11：一年（使用者裁示，非完整區間）日 K 近似 +218%、逐筆約持平 |
| Phase5-3 | 文件 | `core/backtest/README.md`、`strategies/README.md`、`docs/backtest/` | 描述逐筆語意、成交規則與已知限制 | ✅ | 2026-10-11（`6271094`） |

---

## 已定案（2026-10-10 使用者確認）

1. **級別決定方式**：策略宣告預設級別與支援的級別；回測可用 `--scale` 覆寫，只能選策略支援的，否則當場報錯。
2. **餵入單位**：逐筆、一次一筆，`List[StockQuote]` 長度 1，與實盤 `is_tick_triggered` 同契約。
3. **成交規則**：下單後同一檔的下一筆 tick 才成交；市價買吃 `ask_price`、賣吃 `bid_price`；限價單等之後的成交價穿越才成交。
4. **標的範圍**：策略盤前宣告的清單與全市場兩套都做，回測前自己選（`--tick-universe {strategy,all}`）。
5. **支援級別的宣告方式**：`self.supported_scales: Set[Scale]`，未設定視為 `{self.scale}`；盤中動能策略設為 `{Scale.DAY, Scale.TICK}`。
6. **鉤子時點**：TICK 回測沿用實盤的段落設定，**`live_schedule` 改名為 `execution_schedule`**，回測與實盤共用；DAY 回測仍不讀它。
7. **策略清單的來源**：沿用 `get_live_symbols(latest_date)`，每個交易日盤前以前一交易日呼叫，再加上當時的持倉標的（與實盤 `LiveTrader.quote_symbols()` 一致）；策略沒有覆寫時選 `strategy` 當場報錯，不自動退回全市場。
8. **架構與命名**：日 K 與 tick 結構對稱。

   ```
   Backtester.run()                       逐日迴圈（日 K 與 tick 共用）
    └─ 每個交易日：
        simulator.load_day(date)          當天沒有報價就整天跳過（Phase1-4 偏離原規格新增）
        prepare_day(date)                 cur_date 前進、各 model 的 prepare_day()（Phase1-5）
        close_quotes = simulator.run_day(date)
        finish_day(date, close_quotes)    收盤結算：當沖回補、借券費、盯市、權益快照、次日漲跌停基準

   BarSimulator.run_day(date)             TickSimulator.run_day(date)
       process_bar(quotes)                    process_opening_auction()   開盤集合競價，對應 AT_OPEN
                                              process_tick(quote)         逐筆迴圈，對應 IMMEDIATE
                                              process_closing_auction()   收盤集合競價，對應 AT_CLOSE
   ```

   | 名稱 | 檔案 | 工作內容 |
   |------|------|----------|
   | `Backtester` | `core/backtest/backtester.py` | 逐日迴圈、`prepare_day()`／`finish_day()`、委託前處理、報表 |
   | `BaseSimulator` | `core/backtest/simulator/base.py` | `run_day(date)` 介面；共用的 `execute_open_signal()`／`execute_close_signal()` |
   | `BarSimulator` | `core/backtest/simulator/bar_simulator.py` | 日 K：一天一根 bar，台股與期貨共用 |
   | `TickSimulator` | `core/backtest/simulator/tick_simulator.py` | 逐筆：開盤集合競價 → 逐筆 → 收盤集合競價 |
   | `PendingOrderManager`／`PendingOrder` | `core/backtest/simulator/pending_orders.py` | 在途委託、IOC 作廢、收盤清空 |
   | `StockTickDataFeed` | `core/backtest/datafeed/tw/stock_tick_datafeed.py` | 繼承 `TwStockDataFeed`，加上逐筆產生報價與段落快照 |

   - **選名理由**：Simulator 沿用 Zipline `AlgorithmSimulator` 的慣例；`process_*` 是 NautilusTrader 撮合引擎的寫法，不用常被當成策略回呼的 `on_bar`／`on_tick`；集合競價用 opening／closing auction，避開本專案代表開倉的 open。
9. **引擎內部兩處改名**（即 `框架對稱化重構.md`〈待定案〉N10、N11）：`settlement.on_bar_close()` → `settle_day()`；`fill_model.on_bar_close()` → `update_reference_prices()`、`prev_close` → `reference_prices`。
10. **逐筆回測的三項成交規則**：
    - 盤中段落的時間窗照實盤段落表（逐筆鉤子只在 09:05–13:18 呼叫，收盤段取 13:25 前的快照）；段落表由 `core/live/` 移到 `core/market/` 讓回測與實盤共用。
    - 開盤段（`AT_OPEN`）的委託以當天第一筆成交價成交；限價單開盤價不劣於限價才成交，否則依 ROD 留到盤中。
    - 逐筆成交量上限的分母：連續競價是下一筆的對手方委託量（買看 `ask_volume`、賣看 `bid_volume`），集合競價是該筆成交量。

---

## 步驟詳述

施作細節（突變清單、測試條數、pytest 總數）在 git 歷史；以下只留做了什麼、偏離原規格之處與驗證結論。

### Phase0-1. 定案介面細節與命名 ✅

- **結果（2026-10-10）**：前四點使用者直接選定；第 5～7 點採用建議；第 8 點由使用者從業界慣例的候選中逐項選定；第 9、10 點同日追加選定。
- **相依**：無。

### Phase1-1. 策略宣告支援的級別＋回測 `--scale` 覆寫 ✅

- **結果（2026-10-10）**：`BaseStrategy.supported_scales`（None＝只支援 `self.scale`）、`get_supported_scales()`、`validate_scale()`、`UnsupportedScaleError`；`--scale` 經 `BacktestOverrides` 寫回策略，**先驗策略自己的宣告、再驗覆寫值**。範圍外的覆寫是用法錯誤（退出碼 2），宣告矛盾是策略的錯（退出碼 1）。`Backtester.validate_scale()` 守不經覆寫的路徑。
- **驗證**：`tests/backtest/test_backtest_scale.py`；回歸雙線零變動。
- **相依**：Phase0-1。

### Phase1-2. 標的範圍選項（策略清單／全市場） ✅

- **結果（2026-10-10）**：`TickUniverse` 在 `core/utils/constant/engine.py`；`Backtester.validate_tick_universe()`：DAY 帶了就拒絕、TICK 預設 `strategy`、策略沒覆寫 `get_live_symbols()` 卻選 `strategy` 時拒絕並提示 `all`（`InvalidTickUniverseError`）。每日標的規則 `resolve_strategy_symbols()`：清單在前、清單外的持倉依代號附後。
- **驗證**：`tests/backtest/test_tick_universe.py` 10 條；回歸雙線零變動。
- **相依**：Phase0-1。

### Phase1-3. `live_schedule` 改名為 `execution_schedule` ✅

- **結果（2026-10-10）**：17 檔改名；`_REMOVED_SETTINGS` 新增 `live_schedule`，設定舊名的策略在回測（`build_backtester()`）與實盤（`strategy_guard`）啟動都會報錯。
- **偏離原規格**：`resolve_hook_timing()` 放在 `core/strategies/base.py`（模組層級函式），不是 `core/portfolio/order_rules.py`——它要讀策略，而 `core.strategies` 已 import `core.portfolio`，放進去會循環相依。
- **驗證**：`tests/live/` 全綠；回歸雙線零變動。
- **相依**：Phase0-1。動到實盤程式，合併時注意〈合併部署收尾〉第 1 項。

### Phase1-4. 日 K 路徑抽成 `BarSimulator`（純重構）＋引擎內部兩處改名 ✅

- **結果（2026-10-10）**：先改名（`48752a8`），再抽 simulator（`1502a87`）。`Backtester` 剩逐日迴圈、`prepare_day()`／`finish_day()`、委託前處理與報表；`run_day_backtest()`、`run_tick_backtest()`、`execute_bar()` 刪除。期貨回測同樣走 `BarSimulator`。
- **偏離原規格**：① 每日迴圈多一個 `simulator.load_day(date) -> bool`，保留「當天沒有任何報價就整天跳過」的舊行為（新增 `test_run_skips_market_day_without_any_quote` 守住）；② simulator 經 `SimulationEngine`（Protocol）取用引擎，不 import `Backtester`（分層）。
- **驗證**：`run_regression.sh` 兩線結束碼 0；五支策略 2024 全年重構前後 25 個報表 CSV 逐位元相同。
- **相依**：無。

### Phase1-5. 每日市場狀態改由各 model 自己準備 ✅

- **結果（2026-10-11）**：`BaseFillModel.prepare_day()`、`BaseSettlementModel.prepare_day()` 預設 no-op，台股覆寫自己向 DataFeed 取狀態；基底七個 no-op 鉤子移除。融券檢核改取 T−1 不在本步驟（之後由 `框架對稱化重構.md` Phase4-6 完成）。
- **驗證**：回歸雙線與 25 個報表 CSV 零變動；補上 `test_daily_preparation_feeds_suspension_to_fill_model`（原本「引擎每天取停券標的」沒有任何測試守著）。
- **相依**：Phase1-4。

### Phase2-1. tick 查詢支援多檔與當日累計量 ✅

- **結果（2026-10-10）**：`StockTickDAO.query_ticks()` 的 `stock_ids`（每個代號都驗證英數字、空清單不送查詢、單一字串 `TypeError`）；所有讀取介面多回傳 `cum_volume`。
- **偏離原規格**：累計量的算法提前定案在 pandas 端（實測全市場一天：window function 1.99 秒；不帶 window 0.81 秒＋pandas 0.19 秒，結果逐筆相同）。
- **驗證**：`tests/test_stock_tick_api.py`、`tests/test_stock_tick_timescale.py`；真實資料 200 檔一天 0.73 秒。
- **相依**：無。

### Phase2-2. 台股逐筆資料源 `StockTickDataFeed` ✅

- **結果（2026-10-10）**：`StockTickDataFeed(TwStockDataFeed)`：`setup_day(date, symbols)`、`iter_day_ticks()`（產生器）、`get_reference_quotes()`、`get_snapshot_quotes(cutoff)`、`get_close_quotes()`；`StockQuoteAdapter.from_tick_row()` 的 `volume` 改成 `cum_volume`、單筆量留在 `tick_quote.volume`；一般股過濾放在資料源。
- **偏離原規格**：① 收盤快照時點由呼叫端傳入（段落時點是市場特性，與實盤段落表對齊）；② 參考價由呼叫端傳入（引擎成交模型的 `reference_prices`），不在資料源另算一份。
- **驗證**：`tests/backtest/test_stock_tick_datafeed.py` 8 條；真實資料全市場一天 `setup_day()` 0.99 秒、轉成報價 2.29 秒。
- **相依**：Phase2-1。

### Phase3-1. `TickSimulator`：一天三段與逐筆迴圈 ✅

- **結果（2026-10-10，與 Phase3-2 同一個 commit）**：前置重構把段落時窗與台股／期貨段落表移到 `core/market/segment.py`、`core/market/tw/segment_schedule.py`。`run_day()` = 開盤集合競價 → 逐筆 `process_tick()`（先撮合在途委託、再在 09:05–13:18 時窗內呼叫鉤子）→ 讀到第一筆 ≥ 13:25 時插入收盤段 → 收盤作廢未成交委託。開盤集合競價＝各檔當天第一筆。
- **移除**：`_reject_intraday_tick_backtest()`、`IntradayScaleMismatchError`、`FillModel.on_bar_open()`、`StockQuoteAdapter.from_tick_rows()`；`TwStockDataFeed.get_quotes(date, Scale.TICK)` 改成拋錯並指向 `StockTickDataFeed`。
- **其他**：逐筆才有的事件（`expired_ioc` 等）放在 `TICK_EVENT_KEYS`，不影響日 K 事件報表；開倉與平倉的執行方式取策略的 `live_execution`（停損一律市價），沒宣告就拒絕建立。
- **驗證**：真實資料全市場、不交易的策略一天 9.0 秒、峰值 639 MB。
- **相依**：Phase1-1、Phase1-3、Phase1-4、Phase2-2。

### Phase3-2. 逐筆成交模型 ✅

- **結果（2026-10-10）**：`PendingOrderManager`／`PendingOrder`；`BaseFillModel.get_tick_fill_price()`（對手方為 0 不成交、市價吃 bid/ask、限價要穿越才成交、集合競價以該筆成交價）；`TwStockFillModel.fill_tick()`（名單 → 券源 → 以實際成交價判斷平盤下 → 滑價預設 0 → 成交量上限），不經 `validate()` 的區間檢查與夾價；策略的委託價留在副本的 `reference_price`。重複委託不重送、在途開倉委託佔持倉名額。
- **偏離原規格**：`get_filled_price(order)` 不改簽章——逐筆成交價在呼叫它之前就由 `get_tick_fill_price()` 決定，它只負責疊加策略明確設定的 bps；結算層的強制出場在日終以收盤報價執行，用不到 bid/ask。
- **驗證**：`tests/backtest/test_tick_simulator.py` 端到端，七個突變全紅；回歸雙線零變動。`滑價模型後續優化.md` S3 一併 ✅。
- **相依**：Phase3-1。

### Phase3-3. 盤中帳務與日終結算 ✅

- **結果（2026-10-10）**：程式不必改——成交當下就經 `position_manager` 入帳，與日 K 共用同一份帳務；日終由 `finish_day()` 以每檔最後一筆結算。新增三條測試：當沖來回的稅費與日 K 完全相同、資金用完後的開倉被 `rejected_insufficient_balance` 擋下、收盤盯市與次日參考價。
- **相依**：Phase3-2。

### Phase4-1. `IntradayMomentumStrategy` 支援 TICK 回測 ✅

- **結果（2026-10-10）**：只改三處（`supported_scales = {DAY, TICK}`、刪掉 `setup_apis()` 的拒絕、docstring 補〈逐筆回測〉），逐筆分支與實盤同一份。`tests/test_intraday_momentum_tick_backtest.py` 驗證觸發、停損、隔天開盤出場；DAY 結果不變（25 個報表 CSV 逐位元相同）。
- **相依**：Phase3-3。

### Phase4-2. 既有 DAY 策略宣告支援範圍 ✅

- **結果（2026-10-10）**：其餘策略都維持預設（只支援自己的 `scale`），不必改檔；`tests/backtest/test_backtest_scale.py` 以 `StrategyLoader` 逐支驗證，`TICK_CAPABLE_STRATEGIES` 清單以外的策略要求 TICK 一律報錯。
- **相依**：Phase1-1。

### Phase5-1. 端到端測試 ✅

- **結果（2026-10-10）**：`tests/backtest/test_tick_simulator.py` 19 條，涵蓋無前視、下一筆成交吃 bid/ask、決定性、多檔交錯順序、停損只觸發一次。
- **相依**：Phase3-3。

### Phase5-2. 真實資料實跑與效能量測 ✅

- **結果（2026-10-10～10-11，研究庫為主目錄的 copy-on-write 複本）**：
  - **效能（一年）**：`strategy` 每交易日約 7 秒、峰值 971 MB；`all` 約 12 秒、峰值 1.37 GB；同區間 DAY 18 秒。
  - **對照**（2023-05-01～2024-05-10，完整數字與逐筆配對分析在 `盤中動能策略.md`〈逐筆回測對照〉）：日 K 近似期末權益 317.7 萬，逐筆兩種範圍約 99 萬（持平）；偏差主要來自日 K「陽線只看收盤」的停損路徑假設與全日總量門檻，進場價幾乎相同。
- **偏離原規格**：完整區間（2020-05～2024-05-10）依使用者裁示以一年代替。
- **轉出的待確認事項**：逐筆成交量上限（10% 套到下一筆委賣量常不足 1 張而拒單）與 IOC 作廢頻率，已記在 `盤中動能策略.md`〈逐筆回測對照〉給 S4 的待確認事項。
- **相依**：Phase4-1、Phase5-1。

### Phase5-3. 文件 ✅

- **結果（2026-10-11，`6271094`）**：`core/backtest/README.md`〈`Scale.TICK`：逐筆重放〉（段落、成交規則、累計量語意、成交量上限分母、已知與實盤的差異、效能）、〈回測參數〉列出 `--scale`、`--tick-universe`；`strategies/README.md`、`docs/backtest/` 三份同步。舊說明（整天的 tick 一次、全日高低點、`IntradayScaleMismatchError`）grep 無結果。
- **相依**：Phase5-2。

## 風險與對策

- **全市場模式慢**：一年約 12 秒／交易日。對策：預設策略清單模式；需要時再做「無持倉、無在途委託且策略不關心的標的跳過」的最佳化。
- **與實盤的已知差異**：跨股票到達順序以 `(time, stock_id, seq)` 固定、沒有網路延遲、開盤段以該檔第一筆當開盤成交；已寫進 `core/backtest/README.md` 的已知限制。
- **歷史資料止於 2024-05-10**：tick 回補不在本文件範圍。

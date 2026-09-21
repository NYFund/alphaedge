---
description: 在 AlphaEdge 專案中開發／撰寫／新增交易策略時使用。當使用者提到「開發策略」、「寫一個策略」、「新增策略」、「策略邏輯」、「策略回測」，或要在 core/strategies/{stock,futures}/ 底下新增/修改繼承 BaseStockStrategy 或 BaseFuturesStrategy 的策略類別時觸發，不需要使用者明講「去讀 README」。
when_to_use: 使用者想要新增一支新策略、修改既有策略的開倉/平倉/停損邏輯、詢問策略要怎麼寫、詢問策略參數/資料 API 怎麼用、或要用 run.py --strategy 執行回測時。
---

# AlphaEdge 策略開發（SDD）

`core/strategies/README.md` 是本專案策略開發的**唯一權威文件**，完整定義了 SDD（策略開發文件）流程：目錄結構、策略要實作的五個方法（`setup_account`、`setup_apis`、`generate_open_signals`、`generate_close_signals`、`generate_stop_loss_signals`）、策略參數表、資料 API（`StockPriceAPI`/`StockTickAPI`/`StockChipAPI`/`MonthlyRevenueReportAPI`/`FinancialStatementAPI`）用法、自動載入規則與回測執行方式。

**策略只寫 Alpha 層**（選標的、定方向、給價）。`check_open_signal`／`check_close_signal`／`check_stop_loss_signal` 是引擎的契約，由 `BaseStrategy` 提供，**不要覆寫**——覆寫會讓基底的實作（訊號交給 portfolio 層換算張數、平倉依持倉組單）靜默失效，而且不會有任何錯誤訊息。`calculate_position_size()` 也不必寫，開倉張數由 `core/portfolio/` 決定。

## 執行步驟

1. **一律先完整讀取 `core/strategies/README.md`**（不要只憑記憶或猜測），再開始撰寫或修改策略程式碼。
2. 新策略檔案依商品類別放：台股放 `core/strategies/stock/` 繼承 `BaseStockStrategy`；台期貨放 `core/strategies/futures/` 繼承 `BaseFuturesStrategy`（口數、保證金、換月的差異見該基底 docstring 與 `docs/futures/tw-futures-platform.md`）。類別名稱即為 `python run.py --strategy <ClassName>` 使用的識別名稱；**`max_holdings` 記得設**（基底預設 `None` ＝ 不限制檔數，引擎不會替你把關）。
3. 依 README 的方法簽章與範例實作五個方法；不要自創介面或跳過任一個。特別注意三條收斂過的邊界：
   - **還原價**：算漲跌幅一律用 `self.get_signal_close_map(stock_quotes, date)` 搭配 `quote.signal_close`，兩者必須成對使用。「今日價」來自 `StockQuote`、「昨日價」來自 `StockPriceAPI` 是兩條不同路徑，只有一邊套用股價還原會讓比值混用還原價與原始價，**比完全不還原更糟，而且不會報錯**。成交價、手續費、證交稅、漲跌停與檔位判定則一律走原始價（`quote.close`）。
   - **資料取用**：不要直接對 raw `DataFrame` 取中文欄位（`"收盤價"`、`"成交股數"`），一律走 `core/api/` 的具名查詢方法（`get_close_map()`／`get_volume_lots_map()`／`get_close_series()`／`get_trust_net_shares_map()`）。`tests/test_strategy_data_access.py` 會擋下違規。
   - **部位大小**：開倉訊號**不決定張數**，只在訊號裡給 `sizing_price`；張數由 `core/portfolio/` 的等權切分換算（見 README〈部位大小由 portfolio 層決定〉）。要換配置演算法時覆寫 `make_portfolio_constructor()`，不要在策略裡自己算「可開檔數 ÷ 餘額 ÷ 張數」。`max_holdings` 另有引擎側硬上限，超額開倉單會被剔除並計數。
4. 若使用者的需求涉及尚未在 README 涵蓋的資料源或功能，先確認是否該複用既有 API／管理器慣例；期貨相關以 `docs/futures/tw-futures-platform.md` 為準，美股相關參考 `backlog/美股ETL與回測架構規劃.md`。
5. 完成後提醒使用者可用 `python run.py --strategy <ClassName>` 執行回測，結果會落在 `results/<strategy_name>/`——資料夾名稱取自策略的 `self.strategy_name`（例如 `Momentum-1`），不是類別名稱。

不要向使用者要求先手動貼上 README 內容——這份文件的讀取是本 skill 的第一步，自動完成。

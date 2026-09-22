# 多市場回測引擎架構（單一引擎 ＋ 可插拔 model）

> 本文件描述 `core/backtest/` 的現行架構與其設計理由。
> 回測路徑上逐檔案的呼叫關係見 [模組使用關係](module-map.md)。

---

## 概觀

`Backtester` 是**唯一的回測引擎，市場與商品皆無關，沒有子類**。兩者的差異全部下沉為五個可插拔的 model，由 factory 依策略宣告的 `market` ＋ `instrument_type` 組合組裝：

```
Backtester                      ← 唯一引擎，市場無關，無子類
  ├─ InstrumentSpec             ← 乘數、tick size、漲跌停規則、報價單位換算
  ├─ FillModel                  ← 成交價可信度（前視偏誤與不可能成交的擋板）
  ├─ CostModel                  ← 手續費／稅／借券費（期貨為期交稅）
  ├─ SettlementModel            ← 每根 bar 收盤後由市場規則強制執行的動作
  └─ DataFeed                   ← 資料載入、報價轉換、交易日判定
```

這是 Backtrader `Cerebro`、Zipline `run_algorithm`、QuantConnect Lean `Engine`、Nautilus `BacktestEngine` 的一致做法：**引擎唯一，行為注入**。

**新增一個（市場, 商品）組合不需要修改 `core/backtest/backtester.py` 一行。**

---

## 一、設計決策：為什麼不是每個市場一支引擎

把引擎的職責逐段分類，可分成三種性質：

| 性質 | 內容 |
|------|------|
| **市場無關**（複製會浪費、且會漂移） | `run()` 日期迴圈、`execute_bar()` 執行順序、`validate_orders()` 方向白名單、`resolve_open/close_action()`、`execute_open/close_signal()`、`snapshot_daily_equity()` 骨架、`event_counts`、`generate_backtest_report()` |
| **市場規則**（介面共用、實作不同） | 成交價驗證、價格區間、開盤判定 |
| **台股信用交易專屬** | 成本設定推導、隨單欄位補值、當沖強制回補、轉融券留倉、持有成本計提、維持率追繳 |

第三類看似「非分家不可」，但逐一檢查後**全部都能對應到 model 掛點**，沒有一項需要靠繼承分支：

| 職責 | 下沉到 | 業界對應（Lean） |
|------|--------|------|
| 成交價驗證／價格區間 | `FillModel` | `FillModel` |
| 成本設定推導／隨單欄位補值 | `CostModel` ＋ factory | `FeeModel` ＋ `SymbolProperties` |
| 維持率追繳 | `SettlementModel` | `BuyingPowerModel` / `MarginCallModel` |
| 持有成本計提 | `SettlementModel` | `MarginInterestRateModel` |
| 當沖強制回補／轉融券留倉 | `SettlementModel` | `SettlementModel` |
| 權益快照的單位換算 | `InstrumentSpec.to_units()` | `SymbolProperties.ContractMultiplier` |
| 權益快照的**部位計價** | `SettlementModel.mark_position()` | `BuyingPowerModel` |

**關鍵洞察**：台股的「當沖日終強制回補」與期貨的「每日結算」，在架構上是**同一個掛點的兩種實作**——「一根 bar 收盤後，市場規則強制對部位做的事」。看出這點之後，切兩個引擎就沒有理由了。

---

## 二、架構

### 2.1 引擎的建構子

```python
class Backtester:
    def __init__(
        self,
        strategy: BaseStrategy,
        account: BaseAccount,
        position_manager: BasePositionManager,
        instrument: InstrumentSpec,
        fill_model: BaseFillModel,
        cost_model: BaseCostModel,
        settlement: BaseSettlementModel,
        data_feed: BaseDataFeed,
        reporter_cls: Type[BaseBacktestReporter],
        event_counts: Optional[Dict[str, int]] = None,
    ):
```

`account`、`position_manager` 與 `reporter_cls` 也在注入之列——不一併注入就達不到「引擎不認識任何市場」。`reporter_cls` 傳的是**類別而非實例**，避免與 `strategy_result_dir` 的建立順序打結。

### 2.2 單根 bar 的流程

```python
def execute_bar(self, date: datetime.date, quotes: List[BaseQuote]) -> None:
    if self.get_execution_order() == BarExecutionOrder.OPEN_THEN_CLOSE:
        self.execute_open_signal(quotes)
        self.execute_close_signal(quotes)
    else:
        self.execute_close_signal(quotes)
        self.execute_open_signal(quotes)

    # 台股：當沖強制回補 ＋ 借券費計提 ＋ 維持率追繳
    # 期貨：每日結算 ＋ 保證金追繳 ＋ 到期換月
    self.settlement.on_bar_close(date, quotes, self.account, self.event_counts)

    self.snapshot_daily_equity(date, quotes)
    self.update_prev_close(quotes)
```

### 2.2.1 單根 bar 的委託順序

單根 bar 內的順序有**三個互相獨立的層次**，缺一層就會出現「同樣的訊號跑出不同結果」：

| 層次 | 由誰決定 | 規則 |
|------|----------|------|
| 開倉階段 vs 平倉階段 | `BarExecutionOrder`（策略宣告或引擎推導） | `CLOSE_THEN_OPEN`（預設）／`OPEN_THEN_CLOSE` |
| 平倉階段內部 | 引擎寫死 | 停損 → 一般平倉；停損執行完會重掃剩餘部位 |
| 同一階段內的多筆委託 | `Backtester.sort_orders()` | 依 `(date, symbol)` **穩定**排序 |

**為什麼第三層要由引擎自己排**：`check_max_holdings()` 的截斷與 `PositionManager` 的餘額不足檢查，都會讓「先處理誰」直接改變成交結果。而委託的到達順序完全繼承自報價順序，報價又來自 `SELECT * FROM price WHERE date = ?`——這句沒有 `ORDER BY`，實際列順序取決於 SQLite 選到哪個索引。多加一個索引就可能翻掉，且翻掉時不會報錯，只會讓回測結果無聲改變。

排序是**穩定**的，同一標的的多筆委託維持策略給定的先後，分批建倉與部分平倉的意圖不會被打散。

**同標的開平倉並存不做 net 合併。** 同一根 bar 內同一標的同時出現在開倉與平倉訊號時，兩腿分別成交：證交稅只課賣出腿、當沖稅率減半也只認當沖的那一腿，合併成淨額委託會讓兩腿的費用與稅無法各自計算；且平倉腿必須實際成交才會產生 `TradeRecord`，net 掉等於整筆交易在報表上消失。兩腿的先後由 `BarExecutionOrder` 決定，這正是它存在的理由。

**已知限制**：Tick 級別的 `order.date` 只到「日」（`StockQuote.date` 對 tick 也是 `datetime.date`），因此同一 bar 內的 tick 委託無法依成交時間排序，會被壓成依代號排序。要恢復真正的時間序，得讓 `check_*_signal` 回傳帶時間戳的委託事件——屬事件迴圈的範圍，見 [§5.1](#51-事件驅動迴圈長期方向)。

### 2.3 方向與商品類別是兩條獨立的軸

**方向（LONG／SHORT）與商品類別（股票／期貨）互不相干。** `validate_orders()`、`resolve_open_action()`、`resolve_close_action()` 與商品類別無關（期貨的多空語意與股票相同），一律留在引擎內。

[放空回測框架規格](short-selling-framework.md) §1 原則 2「方向來自訂單，策略只做白名單」是本架構的**基礎**。

### 2.4 檔案位置

| 層 | 路徑 | 內容 |
|---|---|---|
| 引擎 | `core/backtest/backtester.py` | 唯一引擎，不含任何 `Stock*` |
| 組裝 | `core/backtest/factory.py` | `build_backtester()`／`build_tw_stock_backtester()`／`build_cost_config()` |
| 行為 model | `core/backtest/models/instrument_spec.py` | `InstrumentSpec` ＋ `TwStockSpec`／`TwFuturesSpec` |
| | `core/backtest/models/fill_model.py` | `BaseFillModel` ＋ `TwStockFillModel`／期貨實作 |
| | `core/backtest/models/cost_model.py` | `BaseCostModel` ＋ `CostConfig`／`ShortConstraint`／`StockCostModel`／期貨實作 |
| | `core/backtest/models/settlement_model/` | `BaseSettlementModel` ＋ `TwStockSettlementModel`／`TwFuturesSettlementModel` |
| 資料源 | `core/datafeed/base.py`（契約）／`core/backtest/datafeed/tw/stock_datafeed.py`／`tw/futures_datafeed.py`／`tw/market_calendar.py`／`tw/futures_calendar.py` | `BaseDataFeed` ＋ `TwStockDataFeed`／`TwFuturesDataFeed` |
| 資料模型 | `core/models/base/` | `BaseQuote`／`BaseOrder`／`BasePosition`／`BaseTradeRecord`／`BaseAccount`，識別欄位一律 `symbol` |
| 策略 | `core/strategies/base.py` | `BaseStrategy`，`market` ＋ `instrument_type` 兩欄位為 factory 的分派鍵 |
| 部位 | `core/managers/base/position_manager.py` | FIFO 拆單主幹 ＋ `settle_daily()` 掛點 |

---

## 三、各 model 的職責

| Model | 必須回答的問題 | 台股實作的重點 |
|---|---|---|
| `InstrumentSpec` | 一張／一口是多少計價單位？價格要對齊什麼跳動點？漲跌停在哪？ | 1 張 ＝ 1000 股、六段跳動點、前收 ±10%（漲停捨去、跌停進位，方向不可對調） |
| `FillModel` | 這張單在這根 bar 有可能以這個價格成交嗎？ | 日 K 以 OHLC 為界、Tick 以當日已發生的累計高低為界；超出漲跌停拒單；檔位未對齊僅警告 |
| `CostModel` | 這筆交易要付多少錢？損益怎麼算？ | 手續費／證交稅（當沖減半）／融券手續費／SBL 借券費／保證金／融券利息 |
| `SettlementModel` | 這根 bar 收盤後，市場規則強制要做什麼？ | 當沖日終強制回補、漲停鎖死轉融券留倉、SBL 借券費逐日計提、維持率追繳、停券強制回補、除息日的股利補償 |
| `DataFeed` | 今天有開市嗎？報價從哪來？ | 當日有日 K 即視為開市；資料 API 共用單一 SQLite 連線 |

### 兩個跨 model 的共用狀態

model 之間刻意**不互相依賴**，需要共享的狀態以 dict 參照傳遞：

- **`event_counts`**：由 factory 建立，同時交給引擎、`FillModel` 與 `SettlementModel`。既有 key 與報表相容，不可更名（新增可以）。
- **`prev_close`**：由 `FillModel` 持有（記錄前收是成交價模型的職責），`SettlementModel` 建構時取得同一個 dict 的參照，用於停牌盯市與漲停判定。

`get_mark_price()` 屬 `BaseSettlementModel` 的介面方法而非 `FillModel`——**期貨的盯市價就是每日結算價**，本來就是結算模型的職責；引擎的 `snapshot_daily_equity()` 也用它算未實現損益。

### `mark_position()`：部位怎麼計入權益

做多部位計入權益的方式依交易型態而不同：**現金帳戶**買進即把現金換成標的，部位價值是「市價 × 計價單位」；**期貨是保證金交易**，契約價值本身不佔用資金，部位價值只有「保證金 ＋ 尚未結算的損益」（TX 一口契約價值數百萬、保證金只佔一小部分，套用現金口徑會讓權益曲線整段偏高一個數量級）。

故這段計價由 `BaseSettlementModel.mark_position()` 決定：預設實作是現金口徑（台股走預設），`TwFuturesSettlementModel` 覆寫成保證金口徑。`snapshot_daily_equity()` 其餘部分（逐日記錄、盯市價取得）維持市場無關。

---

## 四、新增一個（市場, 商品）組合要做什麼

1. `core/models/<instrument>/`：繼承 `core/models/base/` 的五個 model（識別欄位用 `symbol`）。
2. `core/strategies/<instrument>/base.py`：繼承 `BaseStrategy`，設定 `self.market` 與 `self.instrument_type`。
3. `core/backtest/models/`：實作該組合的 `InstrumentSpec`／`FillModel`／`CostModel`／`SettlementModel`（命名帶市場前綴，如 `TwStockSpec`）。
4. `core/backtest/datafeed/<market>/`：實作該組合的 `DataFeed`。
5. `core/managers/<instrument>/position_manager.py`：繼承 `BasePositionManager`，實作 `close_single_position()` 與 `settle_daily()`。
6. `core/backtest/factory.py`：加一個 `elif (strategy.market, strategy.instrument_type) == (...)` 分支。

**既有檔案的改動量：`factory.py` 一個分支。** `backtester.py`、`StrategyLoader`、`run.py` 皆為 0 行——`StrategyLoader` 會自動掃描 `core/strategies/` 下的所有子套件，CLI 也不需要 `--market`（市場與商品皆由策略類別自己宣告）。

> **注意**：`core/backtest/__init__.py` 與 `core/strategies/__init__.py` 刻意**不做套件層 eager import**。任何在此 re-export 的模組都會讓「引擎的相依項無法反向 import 引擎底下的模組」，形成循環 import。呼叫端一律使用完整模組路徑。

---

## 五、已知簡化

| 項目 | 影響 | 為何不做 |
|------|------|----------|
| **per-instrument 粒度的 model 掛載** | 無法在同一次回測同時持有台股與台指期（跨市場組合／避險） | 業界（Lean 掛在 `Security`、Nautilus 掛在 `Instrument`）確實是這個粒度，本專案採 per-run 簡化。升級路徑乾淨：把 model 從 `Backtester` 移到 `InstrumentSpec` 物件上，引擎迴圈不動 |
| 事件驅動 order queue（T+1 延遲成交、限價單未成交、部分成交） | 追繳仍只能以觸發當日收盤價回補 | 本質是引擎典範轉移，見 [§5.1](#51-事件驅動迴圈長期方向) |
| `core/utils/instrument.py` 未移出 | `core/utils/` 仍留一個領域模組 | `StockUtils` 有 `core/backtest/` 以外的使用者（pipeline、adapters、`strategy_lab`）。移進 `core/backtest/` 會讓資料管線反過來相依於回測引擎，是更嚴重的層級問題；其各函式的歸屬需先拆解 |
| 漲跌停價以公式推算 | `TwStockSpec.get_price_limits()` 以「前收 ±幅度後往內對齊檔位」推算，與交易所公告值多數差一檔，影響 `validate()` 的邊界拒單與 `limit_up_cover_failed` 計數 | 公告值可經 `DataFeed.get_price_limit_basis()` 同一掛點推入（該掛點已存在且已被引擎呼叫）、公式版退為 fallback；待辦見 [暫緩工作彙整](../../backlog/暫緩工作彙整.md) S6 |
| `--mode live` 實盤路徑 | `run.py --mode live` 會拋 `NotImplementedError` | factory 已預留讓實盤共用同一組 model |

### 5.1 事件驅動迴圈（長期方向）

現行引擎在單根 bar 內是「訊號產生 → 立即撮合」，`check_*_signal()` 回傳的委託沒有時間戳，
帳戶狀態在同一個呼叫堆疊內就更新完畢。事件驅動的版本會把它拆成事件流：
`check_*_signal()` 改為回傳**帶時間／階段的委託事件**，由 engine 依時間戳排序後依序撮合，
`PositionManager` 只負責「收到已成交事件後的狀態更新」。

**為什麼暫緩**：範圍等同重寫回測引擎，且目前沒有任何策略需要它。
**解除條件**：美股的多市場需求成形、[美股ETL與回測架構規劃](../../backlog/美股ETL與回測架構規劃.md)
的事件迴圈目錄定案之後再啟動。

**已經因此受限的具體項目**（動工時這幾條會一起解掉）：

| 受限項目 | 現況 |
|----------|------|
| Tick 級別的委託排序 | `Backtester.sort_orders()` 對同一 bar 的 tick 委託只能退回依代號排序，無法還原成交時間序（見 [§2.2.1](#221-單根-bar-的委託順序)） |
| T+1 延遲成交 | 維持率追繳只能以觸發當日收盤價立即回補，少了一天的補繳緩衝（見 [放空回測框架規格](short-selling-framework.md) §5.2） |
| 限價單未成交／部分成交 | 無 pending order 機制，每天從頭跑、訊號當下就撮合完畢 |

**升級路徑**：`sort_orders()` 的排序鍵目前是 `(date, symbol)`；一旦委託帶上真正的時間戳，
該鍵可直接擴充為時間序，引擎的分派結構不需重寫。

---

## 六、回歸護欄

任何動到 `core/backtest/`、`core/managers/`、`core/models/` 的改動，都應通過**回歸雙線**：

```bash
./scripts/run_regression.sh    # 任一條失敗即以非零狀態碼結束，且不續跑；有 skip 時結束碼 3
```

| 回歸線 | 內容 | 需求 |
|---|---|---|
| SHORT | 12 組腳本情境、3 份快照（交易紀錄／期末未平倉部位／帳戶與事件計數） | 純記憶體，不連 DB（CI 有跑） |
| LONG | `MomentumStrategy1` 2024-01~06 的交易紀錄逐筆比對 | 需 `data/db/tw_stock.db`（僅本機） |

SHORT 的 12 組情境刻意各只動一個變因，任一情境快照有變即可直接指向出問題的掛點：當沖同日回補（稅率減半）、融券留倉 10 天、FIFO 部分回補的等比例攤提、維持率斷頭、當沖鎖漲停轉留倉、當沖遇停券回補日（釘住結算順序）、SBL 與 MARGIN 借券費對照、除權息停券日的 MARGIN／SBL 對照、跨除息日的股利補償（含部分回補攤提）。

停券日與除息股利由 `DataFeed` 每根 bar 推給 `SettlementModel`，故相關情境的腳本掛在
`tests/backtest/conftest.py` 的 `ScriptedDataFeed` 上——直接設 `SettlementModel` 的欄位
會在下一根 bar 被引擎覆寫。

動手前要知道的三件事：

1. **回歸雙線不經過 reporter。** `make_baseline.py` 與 `make_short_baseline.py` 自行從 `account.trade_records` 組 DataFrame，報表層只靠 `tests/backtest/test_reporting.py` 與 `test_reporter_timeline.py` 把關。
2. **結算動作的執行順序只有 SHORT 快照釘得住。** 把「當沖強制回補」與「每日部位檢查」對調，單元測試全數通過，只有 `day_trade_on_force_cover_date` 情境會紅。
3. **重產 baseline 是有代價的**：一旦重產，先前每一次「逐筆相同」的驗證都失去意義。會改變回測結果的工作應合併排程、只重產一次。

## 相關文件

- [模組使用關係](module-map.md)——回測路徑上誰呼叫誰、逐檔案職責、輸出檔案與動手前的注意事項
- [放空回測框架規格](short-selling-framework.md)——方向驅動的記帳原則，是本架構的基礎
- [台期貨平台](../futures/tw-futures-platform.md)——期貨 model 組的語意（盯市、保證金、換月、日夜盤）
- [`core/backtest/README.md`](../../core/backtest/README.md)〈成交假設〉——滑價、成交量上限與券源檢核的使用說明

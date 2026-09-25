# 回測系統說明

AlphaEdge 的回測系統提供策略回測與績效分析。支援哪些市場、商品與方向，見根目錄 `README.md`〈回測支援範圍〉。

## 目錄

- [回測系統說明](#回測系統說明)
  - [目錄](#目錄)
  - [回測級別](#回測級別)
  - [回測流程](#回測流程)
  - [價格口徑：訊號用還原價、成交用原始價](#價格口徑訊號用還原價成交用原始價)
  - [成交假設：滑價、成交量上限、券源](#成交假設滑價成交量上限券源)
  - [回測結果](#回測結果)
  - [績效指標](#績效指標)
  - [使用方式](#使用方式)

## 回測級別

`Scale` 只有兩種級別（KBar 級別）：

| 級別 | 資料來源 | 適用商品 | 說明 |
| ---- | -------- | -------- | ---- |
| `Scale.DAY` | 台股：`StockPriceAPI` 日線；台期貨：`tw_futures.db` 日行情 | 台股、台期貨 | 預設值；範例見 `core/strategies/stock/momentum_strategy_1.py` |
| `Scale.TICK` | `StockTickAPI` 逐筆成交（DolphinDB） | 僅台股 | 需 `[tick]` 相依與 DolphinDB；期貨 Tick 回測未實作，`TwFuturesDataFeed` 會回空報價 |

在策略中設定回測級別：

```python
self.scale: str = Scale.DAY  # 或 Scale.TICK
```

### 逐筆觸發的策略跑不了 `Scale.TICK`

宣告 `is_intraday = True`（實盤逐筆觸發）的策略，建立 `Backtester` 時會直接拋
`IntradayScaleMismatchError`。**這不是限制，是擋住一種查不出來的錯**：

| | 每次鉤子拿到的報價 |
|---|---|
| 實盤逐筆 | 一檔的**一筆** tick（list 長度 1） |
| `Scale.TICK` 回測 | 該日**整天**的 tick（list 長度上萬） |

同一個 `generate_open_signals(stock_quotes)`，兩邊的 list 語意根本不同。需要橫斷面的
邏輯（挑當下最強的前 N 檔）在回測裡看起來完全正常，上了實盤每次只看得到一檔，
訊號完全不同——**而且兩邊都跑得完、都不報錯**。

要用回測估量級，把 `scale` 設成 `Scale.DAY`，或明確關掉 `is_intraday`。

### 盤中策略的回測結果只能當量級參考

即使改用 `Scale.DAY`，**逐筆策略的回測績效不可當實盤預估**：回測沒有 wall clock，
跨股票的到達順序取決於券商推送、本來就不保證重現。實盤與回測的實際落差由每日
訊號 parity 比對量出來（`core/live/report/parity_checker.py`）。

## 回測流程

回測系統的執行流程如下：

1. **初始化策略**: 載入策略類別並初始化
2. **設定帳戶**: 建立虛擬帳戶，設定初始資金
3. **載入資料 API**: 根據回測級別載入對應的資料 API
4. **資料適配**: 透過 `core/adapters/tw/` 的 `StockQuoteAdapter`／`FuturesQuoteAdapter` 將 API 資料轉成統一的 `StockQuote`／`FuturesQuote`
5. **執行回測**: 逐日（或逐筆）執行策略邏輯
   - 檢查停損訊號
   - 檢查平倉訊號
   - 檢查開倉訊號
   - 執行訂單
6. **生成報告**: 計算績效指標並生成視覺化圖表

## 價格口徑：訊號用還原價、成交用原始價

`price` 表存的是**原始成交價**。未經還原時，除權息造成的價格跳空會被策略當成真實漲跌
（做多憑空虧損、放空憑空獲利）。因此自 2026-08-15 起，**回測的訊號計算預設使用還原價**
（後復權），成交與成本則一律使用原始價。

| 用途 | 該用哪個 | 取值方式 |
|------|----------|----------|
| 策略訊號（漲跌幅、均線、動能） | **還原價** | `StockQuote.signal_close`、`BaseStockStrategy.get_signal_close_map()` |
| 成交價、手續費、證交稅 | **原始價** | `StockQuote.close`、`StockPriceAPI.get_close_map()` |
| 漲跌停與價格檔位判定 | **原始價** | 除權息日的基準改用 `dividend` 表的**開盤競價基準** |

### 寫策略時最容易踩的坑

「今日價」來自引擎傳入的 `StockQuote`、「昨日價」來自 `StockPriceAPI`，**是兩條不同的路徑**。
只還原其中一邊，比值會同時混用還原價與原始價——**比完全不還原更糟，而且不會報錯**。

```python
# ✅ 正確：兩邊由同一個來源（引擎傳入的報價）決定是否還原
close_map = self.get_signal_close_map(stock_quotes, yesterday)
price_chg = quote.signal_close / close_map[quote.stock_id] - 1

# ❌ 錯誤：今日走還原價、昨日走原始價
price_chg = quote.signal_close / self.price.get_close_map(yesterday)[...] - 1
```

### 開關

`build_backtester(strategy, adjusted_price=True)`，預設啟用。`Backtester` 那一層的預設為
`False`——引擎不預設任何政策，要用哪種價格由 factory 這個政策層決定。

還原方式、涵蓋範圍與已知限制（tick 不還原、不處理減資／合併／代號變更）見
[`docs/exchanges/data_coverage.md`](../../docs/exchanges/data_coverage.md)。

## 成交假設：滑價、成交量上限、券源

`FillModel` 回答「這張單成不成交、以什麼價量成交」。三項假設**預設全部關閉**，
未啟用時回測結果與導入前逐筆相同。

設定在 `FillConfig`（`core/backtest/models/fill_model.py`），由策略的 `fill_config` 帶入：

```python
from core.backtest.models.fill_model import FillConfig, VolumeCapPolicy

class MyStrategy(BaseStockStrategy):
    def __init__(self):
        super().__init__()
        self.fill_config = FillConfig(
            slippage_bps_buy=10.0,      # 買進滑價 10 bps（0.1%）
            slippage_bps_sell=10.0,     # 賣出滑價 10 bps
            max_volume_share=0.1,       # 單筆不超過當日成交量 10%
            volume_cap_policy=VolumeCapPolicy.TRUNCATE,
        )
```

| 參數 | 預設 | 說明 |
|------|------|------|
| `slippage_bps_buy` / `slippage_bps_sell` | `0.0` | 滑價基點（1 bps = 0.01%）。**買進加價、賣出減價**，方向寫死不可由呼叫端指定符號 |
| `max_volume_share` | `None` | 單筆訂單張數上限＝當日成交量 × 此比例。`None` 為關閉 |
| `volume_cap_policy` | `TRUNCATE` | 超量時縮量（預設）或整張拒單（`REJECT`） |

券源檢核另由 `ShortConstraint.check_borrowable` 開啟（預設 `False`），資料來自
`margin` 表的融券今日餘額。**現股當沖沖賣（`ShortMethod.DAY_TRADE`）不經過券源，
一律放行**；判準看 `short_method` 而非 `is_day_trade`——融券當沖的 `is_day_trade`
同樣是 True，但它確實借了券，仍須檢核。

#### 期貨：滑價以**跳動點**表達（`FuturesFillConfig`）

```python
from core.backtest.models.fill_model import FuturesFillConfig

class MyFuturesStrategy(BaseFuturesStrategy):
    def __init__(self):
        super().__init__()
        self.fill_config = FuturesFillConfig(
            slippage_ticks_buy=1,                        # 買進滑一檔
            slippage_ticks_sell=1,                       # 賣出滑一檔
            slippage_ticks_by_product={"MTX": 2},        # 小台流動性較差，滑兩檔
            max_volume_share=0.1,                        # 單筆不超過當日成交量（口）10%
        )
```

**為什麼不沿用基點**：期貨的價差本來就以「幾檔」報價，而同一個基點數在不同價位
換算出的檔數不同——TX 在 12,000 點時 1 bps 是 1.2 點、24,000 點時是 2.4 點，
同一組設定跨年份回測會靜默變成不同的滑價假設。兩種都設時**以跳動點為準**。

**跳動點逐商品查表**（`FUTURES_TICK_SIZE`）：TX／MTX／TMF 為 1 點、TE／ZEF 為 0.05 點、
TF／ZFF 為 0.2 點，同一個 `slippage_ticks=1` 在不同商品是不同的價差。只登錄已查證的
商品，未登錄者退回 1 點並記 warning——要用它們得先查證後登錄，或在建構 `TwFuturesSpec`
時明確指定 `tick_size`（明確指定一律蓋過查表）。

**期貨的 `fill_config` 必須是 `FuturesFillConfig`**：給基底的 `FillConfig` 會在
`build_tw_futures_backtester()` 當場拋 `TypeError`。型別標註擋不住這件事——寫成
`FillConfig(slippage_bps_buy=10)` 照樣跑得完，只是整組假設默默換成基點。

### 滑價套用在哪些路徑

| 路徑 | 台股 | 期貨 |
|------|:----:|:----:|
| 策略開倉單、平倉單與停損單 | ✅ `fill()` | ✅ `fill()` |
| 引擎強制出場（當沖日終、追繳、持有天數、停券、無報價、到期兜底） | ✅ `apply_fill_price()` | ✅ `apply_fill_price()` |
| 換月轉倉（平舊月、開新月**兩腿**） | — | ✅ `apply_fill_price()` |
| 逐日盯市／每日權益快照 | ⛔ 刻意不套 | ⛔ 刻意不套 |

三者用的是**同一組 `fill_config`**，不另開「強制出場專用滑價」旋鈕。

**強制出場只取成交價、不走 `fill()`**：`fill()` 會做券源檢核與成交量上限，那兩項會
拒單或縮量——而強制出場是市場規則強加的，拒掉它等於讓部位違規留倉，那比「拿不到
理想價」嚴重得多。逐日盯市不套滑價則是因為它是**評價不是成交**。

**強制出場的成交價超出當日區間時只警告並計數，不夾回**（計入
`close_price_out_of_range`，與策略平倉腿同一個 key）：強制出場的時點是市場規則決定
的，夾回等於換一個價格假設，而且一律把成交價推向對持有者有利的一側，正好抵銷滑價
的保守意義。開倉腿則仍然夾回（`fill_price_clamped`）——拒單會讓「加了滑價之後訊號
數反而變少」，比價格偏一點更難解釋。

### 計算順序：先滑價，再算費用

```
策略委託價 → 滑價調整 → 對齊檔位 → 成交價
                                    └→ 手續費、證交稅皆以此價計算
```

手續費與證交稅一律以**含滑價的成交價**計算，兩者的假設因此一致。

期貨同理，但收的是**期交稅（買賣各一次、稅基為契約價值）與每口手續費**，
與證交稅沒有一項共用——設定見 `FuturesCostConfig`，費率常數見 `FuturesCost`。
`FuturesCostConfig.free()` 是零成本口徑，**只用於驗證引擎接線**，不可拿來評估績效。

### 兩個容易誤解的地方

1. **檔位會放大小額 bps，不只是吸收**：調整後必須對齊台股升降單位，且取對下單者
   **不利**的一側。100 元的股票（檔位 0.5）設 10 bps 與 50 bps 都會得到 100.5
   ——低於半個檔位的差異被吸收掉，但**實際付出的是半個檔位以上**，比設定值大。

   **實測**（`ForeignSellShortDayTradeStrategy`，2024-01-02 ~ 03-29，雙邊各 10 bps）：
   名目 0.10%，實際吃掉兩腿名目總額的 **0.2439%（約 2.4 倍）**，
   金額 39,900 元，**佔該期間已實現損益的 42.31%**。
   標的價格越低（檔位佔價格的比例越大），放大越明顯。

   這個數字現在由報表的 `Slippage Cost` 直接輸出，不必自己回推。
2. **成交量上限只在日 K 生效**：`quote.volume` 在日 K 是當日總量、在 tick 是單筆成交量，
   以單筆量當分母沒有意義。tick 級別的累計量檢查尚未實作。

### 事件計數

三項假設觸發時皆計入 `*_event_report.csv`，不會靜默發生：

| 事件 | 意義 |
|------|------|
| `rejected_no_borrow` | 融券餘額不足，放空開倉被拒 |
| `rejected_limit_up_locked` | 全日鎖漲停（開高低收皆為漲停價），買進開倉被拒 |
| `rejected_limit_down_locked` | 全日鎖跌停，放空開倉被拒 |
| `rejected_volume_cap` | 超過成交量上限且政策為拒單（或上限不足一張） |
| `rejected_insufficient_balance` | 餘額不足以支應**做多**開倉（部位價值 ＋ 開倉成本）；放空另有自己的 warning，不計入本 key |
| `rejected_no_quote` | 當日查不到報價（停牌、不在股票池）的開倉單被拒；平倉腿不拒，由連續無報價出場處理 |
| `truncated_by_volume` | 超過成交量上限被縮量 |
| `forced_cover_suspended` | 觸及停券強制回補日（除權息推導或手動指定） |
| `rejected_short_suspended` | 停券期間（回補日 ~ 除權息交易日）的融券放空開倉被拒 |
| `forced_cover_insufficient_margin` | 當沖轉融券留倉時餘額不足，改為強制回補 |
| `dividend_compensation_paid` | 跨除息日的空單補償出借方現金股利 |
| `dividend_compensation_unknown` | 權息並存拆不出現金股利，該筆補償被跳過（成本低估） |
| `dividend_received` | 跨除息日的做多部位收到現金股利 |
| `share_adjustment_applied` | 配股、分割、減資造成的股數與每股成本調整 |
| `share_adjustment_unknown` | 權息並存拆不出配股率，股數未調整（帳面低估配股價值） |
| `forced_exit_no_quote` | 做多部位連續無報價（停牌／下市）達上限被強制出場 |
| `fill_price_clamped` | 滑價把**開倉**成交價推出當日區間，被夾回 |
| `close_price_out_of_range` | **平倉腿與引擎強制出場**的成交價超出當日區間（只計數不夾回） |

### 公司行動的記帳口徑

盯市與平倉一律用 `quote.close` 這條**未還原**的原始價，除權息的跳空因此留在
帳面損益裡；記帳端要把「那段跳空該歸誰」還原回去，兩者相抵後除權息本身不產生損益：

| 事件 | 做多 | 放空 |
|------|------|------|
| 現金股利 | 除息日入帳（`dividend_received`） | 除息日補償出借方（`dividend_compensation_paid`） |
| 配股、分割、減資 | 股數 × 倍率、每股成本 ÷ 倍率 | 同左（義務等比例增加） |

**還原價只用於訊號**（`Backtester.adjusted_price`），不參與記帳；兩者都要做，
少了任何一邊都會有一段假損益。不足一張的零股以調整後的每股成本折現，
不四捨五入吞掉——吞掉會讓權益在每次配股時跳動一小段。

**`forced_exit_no_quote` 的出場價是「最後可得收盤價」**，這仍然高估下市股的回收價
（實務上多為部分償還甚至歸零），但歸零會系統性低估。要保守估計者可依本計數自行調整。

**查無融券資料時一律放行並 warning**，不會把「查不到」當成「借不到」——
`margin` 表的歷史回補是獨立作業，尚未執行時整場回測都會查無資料。

### 漲跌停的已知限制

漲跌停幅度已依年代分段（**2015-06-01 前為 7%**，之後 10%）。以 23,972 筆交易所
公告值比對，相符率 61.6%（分段前 54.5%）。

**剩餘落差來自檔位對齊規則**：現行做法是「基準價 ±幅度後往內對齊檔位」，與交易所
實際的升降單位取值規則不完全一致，多數不符者相差一個檔位。此項**尚未解決**。

影響有限——漲跌停只在拒單時用到，多數訂單不在邊界上；但放空的
`limit_up_cover_failed`（漲停鎖死無法回補）直接依賴此判定，該計數會有偏差。

## 部位大小與檔數上限

「這一單買幾張」與「總共可以持有幾檔」是**兩層把關**，責任分工如下：

| 層級 | 由誰負責 | 做什麼 |
|------|----------|--------|
| 策略（Alpha） | `generate_open_signals()` | 選標的、決定 `sizing_price`（算量價）與 `order_price`（委託價） |
| 部位建構 | `StockPortfolioConstructor`（`core/portfolio/construction.py`） | 把訊號交給 sizer，再組成訂單 |
| 部位大小模型 | `EqualWeightSizer`（`core/portfolio/sizing.py`） | 依剩餘名額均分餘額、換算張數 |
| 引擎 | `order_preprocess.check_max_holdings()`（回測經 `Backtester.check_max_holdings()` 呼叫） | **硬上限**：超過 `max_holdings` 的開倉單一律剔除並計數 |

> **sizer 與引擎的 `max_holdings` 檢查刻意不合併。** 兩者回答不同問題：sizer 問
> 「資金要切成幾份」（訊號階段，張數不足 1 張的候選不佔名額），引擎問「這張單送出去
> 會不會讓帳戶超過上限」（逐單階段，看即時持倉數，未成交的單不增加持倉）。
> 兩者不等價，少任何一道都會漏掉對方擋得住的情況。

### 策略要寫的部分

策略**只回傳訊號**，張數由部位建構層換算：

```python
def generate_open_signals(self, stock_quotes: List[StockQuote]) -> List[Signal]:
    return [
        Signal(
            quote=stock_quote,
            action=Action.BUY,
            position_type=PositionType.LONG,
            order_price=stock_quote.cur_price,  # 委託價
            sizing_price=stock_quote.close,  # 算張數用的價格，由策略決定
        )
        for stock_quote in candidates
    ]
```

要換配置演算法（波動度加權等），覆寫 `make_portfolio_constructor()` 傳入別的 sizer 即可，
呼叫端不動。**建構器每次組裝都重建**：`max_holdings` 是策略在 `super().__init__()` 之後
才填的，存成欄位會永遠讀到舊值。

**平倉與停損不走這一層**：張數取自持倉（`position.volume`）、價格由策略的交易邏輯決定，
由 `build_close_orders()` 直接組單。

### 預設的等權公式

```
可開檔數 = max(0, max_holdings - 現有持倉檔數)   # max_holdings 為 None 時不限制
每檔資金 = account.balance / 可開檔數
張數     = int(每檔資金 / (sizing_price × Units.LOT))   # 無條件捨去
下單條件 = 張數 >= 1；sizing_price <= 0 者跳過
```

**`int()` 的無條件捨去與「至少 1 張」的門檻不可改動**——它們直接決定 LONG 回歸 baseline 的 915 筆結果。

這段公式原本在五支策略內各寫一遍，收斂時發現兩處已經漂移，一併定案（2026-08-09）：

| 項目 | 收斂前 | 收斂後 |
|------|--------|--------|
| `max_holdings is None` | 收斂前五支動能策略中有一支為「不開倉」，其餘四支為「不限制」 | 一律**不限制**（多數派 4:1，且與 `Optional[int]` 的直覺一致） |
| 參考價 `<= 0` | `_1`／`_3` 無檢查，遇收盤價為 0 會 `ZeroDivisionError` **中斷整場回測** | 一律**跳過該檔**，不影響其他候選 |

兩者都不改變既有回測結果：前者的分支在五支策略上皆跑不到（都在 `__init__` 明確設了 `max_holdings`），後者只在資料異常時觸發。

### 引擎側硬上限

`max_holdings` 是真正的風控，不是「策略願意遵守才生效」的建議值。策略即使不呼叫 sizer，超額的開倉單仍會在 `execute_open_signal()` 內被剔除，並以 `logger.warning` ＋ `event_counts["rejected_max_holdings"]` 留痕——**禁止靜默丟棄**。`max_holdings` 為 `None` 時不做任何截斷。

## 回測結果

回測完成後，系統會自動產生以下內容：

**檔名一律帶策略名前綴**（以下 `<策略>` 指 `strategy.strategy_name`，見〈儲存位置〉）。

### 1. CSV（五份）

| 檔名 | 內容 |
|------|------|
| `<策略>_trading_report.csv` | 逐筆交易紀錄與損益 |
| `<策略>_direction_summary.csv` | 多空分別的統計 |
| `<策略>_event_report.csv` | `event_counts` 的明細（拒單、鎖漲停回補失敗等）|
| `<策略>_metrics_summary.csv` | 整體績效指標（長表，欄位見〈績效指標〉）|
| `<策略>_daily_equity.csv` | 每日權益序列 |

### 2. 圖表（五張）

| 檔名 | 內容 |
|------|------|
| `<策略>_balance_curve.png` | 資產隨時間的變化 |
| `<策略>_networth.png` | 策略與基準（大盤）的淨值比較 |
| `<策略>_mdd.png` | 最大回撤 |
| `<策略>_everyday_profit.png` | 每日損益分布 |
| `<策略>_everyday_equity_change.png` | 每日權益變化 |

### 3. 日誌檔案

`logs/backtest/<策略>.log`——**不在 `results/` 底下**。
`core/config/paths.py` 把 `LOGS_DIR_PATH` 與 `RESULTS_DIR_PATH` 訂成兩個平行的根。

### 儲存位置

回測結果儲存路徑：`results/<策略>/`，其中 `<策略>` 取的是
**`strategy.strategy_name`**（例如 `Momentum-1`、`Momentum-Futures`、
`Foreign-Sell-Short-Day-Trade`）。

⚠️ **那不是類別名**。`--strategy` 吃的是類別名（`MomentumStrategy1`），
輸出目錄吃的是 `strategy_name`，**兩者可以不同**——跑
`--strategy MomentumStrategy1` 會產出 `results/Momentum-1/`。

## 績效指標

整體指標由 reporter 輸出成 `<策略>_metrics_summary.csv`（`Metric`／`Value`／`Note`
**長表**），**不開前端也看得到**：勝率、勝敗比、獲利因子、平均 ROI、平均持有天數、
最大回撤、年化波動度、Sharpe、Sortino、Information Ratio 與權益口徑。

**公式只有一份**，全部在 `analysis/performance_metrics.py`（純函式，只相依 `math`
與 `typing`）。前端一律讀這份 CSV、**不自行重算任何一條公式**——同一個指標算在兩個
地方，最後一定會出現「報表說 1.2、前端說 0.8」而沒有人知道哪個對（MDD 曾經就有
reporter 與前端兩份實作）。MDD 圖的逐日序列與 CSV 的最深點同樣共用
`compute_drawdown_series()`。

兩種情況會讓指標**留空**，`Note` 欄一律寫明原因：

| 情況 | 留空的指標 | 原因 |
|------|------------|------|
| `Equity Basis` 為 `Realized only`（沒有 `daily_equity`） | 年化波動度、Sharpe、Sortino、Information Ratio | 該口徑只在平倉日有節點，拿逐筆報酬乘 √252 等於宣稱一年有 252 筆交易 |
| 期貨對標退回近月拼接 | Information Ratio | 換月接點的基準日報酬含展期假跳空，逐日相減會被那幾天帶偏 |

**留空不是 0**：獲利因子與勝敗比在「沒有虧損筆數」時同樣留空——「從沒虧過」與
「因子為零」意思完全相反。

### 滑價成本

`Slippage Cost` 是策略委託、引擎強制出場與換月轉倉的價差總額，
`Slippage Cost / |Total PnL| (%)` 是它佔已實現損益的比例。

⚠️ **這筆錢不計入 `total_transaction_cost`**：滑價是**內含在成交價裡**的
——成交價已經比委託價差了，損益早就反映了它，再加進交易成本就是重複計算。
手續費與稅則相反，那是真的另外從餘額扣的一筆。

換算方式是「（成交價 − 委託價）× 數量 × 計價單位」，台股的計價單位是股
（1 張 ＝ 1,000 股）、期貨是契約乘數（逐契約不同）。委託價由 `FillModel` 與
`SettlementModel` 寫在成交副本的 `BaseOrder.reference_price` 上，
**被拒的單不計入**（沒有成交，價差也就不存在）。

回測系統會自動計算以下績效指標：

- **總報酬率**: 策略的總收益
- **年化報酬率**: 年化後的報酬率
- **Sharpe Ratio**: 風險調整後報酬率
- **最大回撤 (MDD)**: 從高點到低點的最大跌幅
- **勝率**: 獲利交易的比例
- **平均獲利/虧損**: 平均每筆交易的獲利和虧損
- **交易次數**: 總交易筆數

## 使用方式

### 基本語法

```bash
python run.py --strategy <StrategyName>
```

### 參數說明

- `--mode`: 執行模式，可選 `backtest` 或 `live`，預設為 `backtest`
- `--strategy`: 指定要使用的策略類別名稱（必填）
- `--show` / `--no-show`: 回測結束後要不要在瀏覽器開圖。**預設不開**——圖本來就會
  存成 PNG，批次掃參數時一次開幾十個分頁，無頭環境（CI、容器、`nohup`）更會直接失敗。
  未指定時依環境變數 `ALPHAEDGE_SHOW_FIGURES`

### 使用範例

```bash
# 執行回測模式，使用名為 "MomentumStrategy1" 的策略
python run.py --strategy MomentumStrategy1

# 執行實盤（必須指定段落；目前只在模擬環境演練過）
python run.py --mode live --strategy MomentumStrategy1 --phase open
```

### 注意事項

- `--strategy` 吃的是**類別名稱**（例如 `MomentumStrategy1`）
- 策略由 `strategy_loader` 逐一掃描 `core/strategies/` 底下的商品類別子目錄
  （`stock/`、`futures/`）載入，新增商品類別不需要改程式
- 回測前請確認資料庫中有所需的資料（使用 `python -m tasks.update_db` 更新資料）
- 回測結果會儲存在 `results/<策略>/` 目錄，其中 `<策略>` 是 **`strategy.strategy_name`**（例如 `Momentum-1`），**與 `--strategy` 吃的類別名不同**

## 相關文檔

- [策略開發指南](../strategies/README.md)
- [專案 README](../../README.md)

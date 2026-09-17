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

1. **檔位會吸收小額 bps**：調整後必須對齊台股升降單位，且取對下單者不利的一側。
   100 元的股票（檔位 0.5）設 10 bps 與 50 bps 都會得到 100.5——**低於半個檔位的
   滑價設定不會有額外效果**。要讓 bps 精細生效，標的價格需落在較小的檔位級距。
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
| `truncated_by_volume` | 超過成交量上限被縮量 |
| `forced_cover_suspended` | 觸及停券強制回補日（除權息推導或手動指定） |
| `rejected_short_suspended` | 停券期間（回補日 ~ 除權息交易日）的融券放空開倉被拒 |
| `forced_cover_insufficient_margin` | 當沖轉融券留倉時餘額不足，改為強制回補 |
| `dividend_compensation_paid` | 跨除息日的空單補償出借方現金股利 |
| `dividend_compensation_unknown` | 權息並存拆不出現金股利，該筆補償被跳過（成本低估） |
| `dividend_received` | 跨除息日的做多部位收到現金股利 |
| `share_adjustment_applied` | 配股、分割、減資造成的股數與每股成本調整 |
| `forced_exit_no_quote` | 做多部位連續無報價（停牌／下市）達上限被強制出場 |

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
| 策略 | `check_open_signal()` | 選標的、決定**參考價**（`close`／`open`／tick 價皆可） |
| 部位大小模型 | `EqualWeightSizer`（`core/backtest/models/sizing.py`） | 依剩餘名額均分餘額、換算張數 |
| 引擎 | `Backtester.check_max_holdings()` | **硬上限**：超過 `max_holdings` 的開倉單一律剔除並計數 |

### 策略要寫的部分

`calculate_position_size()` 的 `BUY` 分支**不需要自己算張數**，交給 `self.sizer`：

```python
candidates: List[Tuple[StockQuote, float]] = [
    (stock_quote, stock_quote.close) for stock_quote in stock_quotes  # 參考價由策略決定
]

for stock_quote, ref_price, open_volume in self.sizer.size(
    self.account, candidates, self.max_holdings
):
    orders.append(StockOrder(..., price=ref_price, volume=open_volume))
```

`self.sizer` 由 `BaseStockStrategy` 預設為 `EqualWeightSizer()`；要換配置演算法（波動度加權等），在策略的 `__init__` 覆寫該欄位即可，呼叫端不動。

### 預設的等權公式

```
可開檔數 = max(0, max_holdings - 現有持倉檔數)   # max_holdings 為 None 時不限制
每檔資金 = account.balance / 可開檔數
張數     = int(每檔資金 / (參考價 × Units.LOT))   # 無條件捨去
下單條件 = 張數 >= 1；參考價 <= 0 者跳過
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

### 1. 交易報告 (`trading_report.csv`)

包含所有交易記錄、損益統計等詳細資訊。

### 2. 圖表分析

- **資產曲線圖** (`balance_curve.png`): 顯示資產隨時間的變化
- **資產與基準比較圖** (`balance_and_benchmark_curve.png`): 比較策略表現與基準（如大盤指數）
- **最大回撤圖** (`balance_mdd.png`): 顯示最大回撤情況
- **每日損益圖** (`everyday_profit.png`): 顯示每日損益分布

### 3. 日誌檔案 (`<StrategyName>.log`)

記錄回測過程中的所有資訊、警告和錯誤。

### 儲存位置

回測結果儲存路徑：`results/<StrategyName>/`

## 績效指標

正式回測輸出（報表 CSV 與圖表）由 `report/reporter.py` 產生，reporter 目前**不計算**
Sharpe／Sortino／Information Ratio 等風險調整後報酬。

這類公式集中在 `analysis/risk_metrics.py`（純函式），目前只有前端
（`frontend/services/metrics.py`）呼叫它算 Sharpe 與 Sortino；Information Ratio 的函式已備好，
但報表沒有輸出基準日報酬，前端無從計算。日後要讓報表輸出這些指標，一律呼叫
`risk_metrics.py`，不要在 reporter 另寫一份。

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

### 使用範例

```bash
# 執行回測模式，使用名為 "MomentumStrategy1" 的策略
python run.py --strategy MomentumStrategy1

# 執行實盤模式（目前尚未實作）
python run.py --mode live --strategy MomentumStrategy1
```

### 注意事項

- Strategy Name 是 Class 的名稱
- 策略會自動從 `core/strategies/stock/` 目錄載入
- 回測前請確認資料庫中有所需的資料（使用 `python -m tasks.update_db` 更新資料）
- 回測結果會儲存在 `results/<StrategyName>/` 目錄

## 相關文檔

- [策略開發指南](../strategies/README.md)
- [專案 README](../../README.md)

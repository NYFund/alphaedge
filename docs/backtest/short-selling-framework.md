# Short Selling Framework — 台股放空回測框架規格

`core/backtest` 的放空（SHORT）機制規格：方向中立的引擎設計、台股放空的摩擦成本與回補限制、成本公式與手算驗收範例。

---

## 一、設計原則

1. **單一引擎**：`core/backtest/backtester.py` 的 `Backtester` 統一處理多空，不另開 `ShortBacktester`。方向差異全部收斂到「成本模型 + 部位管理器 + 結算模型」。
2. **方向來自訂單，策略只做白名單**：記帳與成本路徑一律依**每一張 `StockOrder` 自己的 `position_type`** 決定；策略層的 `allowed_directions` 只用於驗證與提供預設值。這與業界框架（Lean／Backtrader／Zipline 的 signed quantity 模型）一致——方向是部位的屬性，不是策略的屬性——要做多空並存的市場中性策略時不需重構引擎。
3. **成本模型可插拔**：手續費／稅／券費／利息／保證金集中在 `StockCostModel`，由「放空管道（`ShortMethod`）+ 是否當沖」決定參數組合，不在 `PositionManager` 裡散落 if-else。
4. **當沖與留倉同骨架**：兩者差別只是「是否收借券費／利息、是否佔用保證金、稅率是否減半」，共用同一組開倉／平倉流程。
5. **保守預設、可調參數**：所有費率集中在 `constant.py` 與策略可覆寫的 config，不寫死在公式裡；預設值取市場常見值（見 §2.3）。
6. **不可靜默失敗**：放空路徑任何無法成交（券源不足、非可空標的、漲停無法回補、保證金不足）都必須 log warning 或計入統計，禁止讓單安靜地消失。
7. **成交價必須可信**：任何成交價都要落在當日 `[low, high]` 與漲跌停區間內，違反即拒單。同一根 bar 內「先開後平」的當沖模式最容易混入前視偏誤（策略看得到當日收盤，卻宣稱以開盤價放空），這道檢查是唯一防線。
8. **明確標示未模擬的部分**：T+2 交割、券源可得性的個股費率等一律列入「已知簡化」（§5.7），不假裝有做。

---

## 二、台股放空機制全覽

### 2.1 三種放空管道對照

| 管道 | `ShortMethod` | 典型用途 | 是否需信用帳戶 | 保證金 | 借券成本 | 稅率 | 留倉 |
|------|---------------|----------|----------------|--------|----------|------|------|
| 現股當沖沖賣（先賣後買） | `DAY_TRADE` | 當沖放空 | 需信用帳戶資格＋簽署當沖風險預告書 | 無（同日沖銷） | 無 | 賣出 0.15%（減半） | ❌ 必須當日回補 |
| 融券賣出 | `MARGIN` | 留倉放空（主流） | 需 | 賣出價金 90% | 融券手續費 0.08%（一次性） | 賣出 0.3% | ✅ |
| 借券賣出（SBL） | `SBL` | 法人／大額留倉放空 | 需借券契約 | 依券商（常見 100%+） | 年化議定費率（0.01%~16%，實務多落 2~5%） | 賣出 0.3% | ✅ |

### 2.2 各階段的摩擦成本（放空完整生命週期）

**開倉（賣出）**
1. 賣出手續費：`成交價 × 股數 × 0.1425% × 折扣`，最低 20 元。
2. 證交稅：當沖 0.15%、非當沖 0.3%（**放空的稅在「賣出」這端就課，與做多相反**）。
3. 融券手續費／借券費：`MARGIN` 於賣出時一次收 0.08%；`SBL` 依持有天數年化計費（逐日計提）。
4. 保證金佔用：`MARGIN` 為賣出價金 × 90%，從 `balance` 扣除並記入 `margin_used`。

**持有期間（留倉才有）**

5. 融券利息**收入**：`(融券賣出擔保價款 + 保證金) × 年利率 0.2% × 天數 / 365`（券商付給客戶，正值，很小）。
6. 借券費**支出**（`SBL`）：`每日收盤價 × 股數 × 年化費率 × 天數 / 365`。
7. 維持率：`(擔保價款 + 保證金) / (現價 × 股數)`，低於 130% 追繳，未補則斷頭回補。
8. **股利補償**：融券在除息／除權前會被**強制回補**（停券），故一般碰不到除息日；不受強制回補約束的 `SBL` 借券則需於除息日補償出借人現金股利（見 §5.3）。

**平倉（回補買進）**

9. 買進手續費：`成交價 × 股數 × 0.1425% × 折扣`，最低 20 元。
10. 買進**不課證交稅**。
11. 歸還保證金：`margin_used` 釋回 `balance`。

### 2.3 統一預設參數（本框架採用值）

以下為框架的單一預設組合，全部放在 `core/utils/constant.py`，策略可覆寫。

| 參數 | 常數名 | 預設值 | 依據／備註 |
|------|--------|--------|------------|
| 手續費率 | `Commission.CommRate` | 0.1425% | — |
| 手續費折扣 | `Commission.Discount` | 0.3 | 約 3 折，電子下單常見 |
| 最低手續費 | `Commission.MinFee` | 20 元 | — |
| 一般證交稅 | `Commission.TaxRate` | 0.3% | — |
| 現股當沖證交稅 | `Commission.DayTradeTaxRate` | **0.15%** | 減半優惠，**適用至 2027-12-31**（`DAY_TRADE_TAX_EXPIRY`，逐筆依成交日判斷） |
| 融券保證金成數 | `ShortCost.MarginRate` | **90%** | 現行法定成數 |
| 融券手續費（借券費） | `ShortCost.MarginBorrowFeeRate` | **0.08%** | 賣出價金一次性 |
| 融券利息（客戶收） | `ShortCost.MarginInterestRate` | **年 0.2%** | 保守可設 0 |
| 融券維持率門檻 | `ShortCost.MaintenanceRatio` | **130%** | 低於則追繳／斷頭 |
| 借券（SBL）費率 | `ShortCost.SBLFeeRate` | **年 3.0%** | 議定區間 0.01%~16%，多數券商上限 7%，取市場常見中位 |
| 融資利率（LONG 用） | `MarginCost.FinancingRate` | **年 6.35%** | 券商常見 6.15%~6.5%，取中間值 |
| 融資成數 | `MarginCost.ListedFinancingRatio` / `OTCFinancingRatio` | 上市 60% / 上櫃 50% | 供未來做多槓桿用，尚未啟用 |
| 計息基準日數 | `DAYS_PER_YEAR` | 365 | 台股慣例 |

> **決策**：留倉放空的**預設管道是 `MARGIN`（融券）**——散戶可用、成本結構明確、資料可得。`SBL` 保留為可選參數。

### 2.4 台股放空的「不可成交」限制

| 限制 | 影響 | 框架處理 |
|------|------|----------|
| 平盤下放空 | 原則全面開放；**警示／處置股期間禁止** | `ShortConstraint.allow_below_reference` 開關，預設 True（**尚無撮合呼叫端**，見下方說明） |
| 可當沖標的清單 | 證交所每日公告，處置股停止先賣後買 | `ShortConstraint.day_trade_whitelist`（**尚無撮合呼叫端**） |
| 券源／融券餘額上限 | 借不到券就無法放空 | `ShortConstraint.check_borrowable`，預設關閉；資料來自 `margin` 表，拒單計入 `rejected_no_borrow` |
| 停券期間（除權息） | 強制回補 | 由 `dividend` 表推導融券最後回補日（`auto_force_cover_on_ex_dividend`，預設開啟）；另可用 `ShortConstraint.force_cover_dates` 手動指定 |
| 停券期間（股東會） | 強制回補 | **無資料源**，仍以 `max_holding_days` 保險絲近似 |
| 除息日的股利補償 | 放空者須補償出借方現金股利 | `CostConfig.compensate_cash_dividend`（預設開啟），逐筆計入 `dividend_compensation` |
| 漲停無法回補 | 當沖放空無法平倉，實務轉借券 | 見 §5.1 |
| 券資比／單一標的空單上限 | 風控 | `max_short_exposure_ratio` 部位上限檢查 |

**券源檢核只作用在需要券源的放空管道**（`TwStockFillModel.check_short_borrowable()`）：`ShortMethod.DAY_TRADE`（現股當沖沖賣，先賣後買）直接放行，`MARGIN`／`SBL` 才比對餘額——判準是 `short_method`，不是 `is_day_trade`（融券當沖的 `is_day_trade` 也是 True，卻確實借了券）。

`allow_below_reference`、`day_trade_whitelist` 兩個欄位**只有定義沒有呼叫端**——設定後不會生效，但 `StockCostModel` 建構時會發出警告（`check_unimplemented_constraints()`），不會靜默。

### 2.5 價格檔位（tick size）

台股採分段檔位，回補價、強制回補價、滑價調整後的價格都必須**對齊檔位**，否則會算出不可能成交的價格（業界框架以 `SymbolProperties.MinimumPriceVariation` 表達，例如 Lean）。

| 價格區間 | 檔位 |
|---|---|
| < 10 | 0.01 |
| 10 ~ < 50 | 0.05 |
| 50 ~ < 100 | 0.1 |
| 100 ~ < 500 | 0.5 |
| 500 ~ < 1000 | 1 |
| ≥ 1000 | 5 |

`StockUtils.round_to_tick(price, direction)` 以 `Decimal` 運算，避免 0.05 檔位的二進位誤差。放空情境的取整方向：**開倉（賣出）向下、回補（買進）向上**，保守估計。與滑價係數共用，順序為「滑價 → 檔位取整 → 算成本」。

---

## 三、框架設計

### 3.1 分層

```
strategy (宣告方向與管道)
    ↓ position_type / short_method / enable_intraday
Backtester (統一迴圈、方向驗證、執行順序、逐日盯市)
    ↓ StockOrder
StockPositionManager (方向分派：open/close × LONG/SHORT)
    ↓
StockCostModel (成本與損益公式；依 ShortMethod + is_day_trade 決定參數)
    ↓
StockUtils (純數學工具：手續費、稅、股數換算)

TwStockSettlementModel (每根 bar 收盤後：當沖強制回補、借券費計提、維持率追繳、停券回補、股利補償)
```

### 3.2 型別（`core/utils/constant.py`）

| 型別 | 成員 | 用途 |
|------|------|------|
| `ShortMethod(str, Enum)` | `DAY_TRADE` / `MARGIN` / `SBL` | 放空管道 |
| `BarExecutionOrder(str, Enum)` | `CLOSE_THEN_OPEN` / `OPEN_THEN_CLOSE` | 單 bar 內執行順序 |
| `DayTradeUncoveredPolicy(str, Enum)` | `FORCE_COVER_AT_CLOSE`（預設）/ `CONVERT_TO_MARGIN` / `RAISE` | 當沖日終未回補（§5.1） |
| `MarginCallPolicy(str, Enum)` | `FORCE_COVER`（預設）/ `WARN_ONLY` | 維持率追繳（§5.2） |
| `ShortCost(float, Enum)` / `MarginCost(float, Enum)` | 見 §2.3 | 放空與融資費率 |

⚠️ **型別注意**：`Commission` / `ShortCost` 是 `(float, Enum)`，塞不進 `date` 或 `int` 語意的成員。因此 `DAY_TRADE_TAX_EXPIRY = datetime.date(2027, 12, 31)` 與 `DAYS_PER_YEAR = 365` 一律放 **module-level 常數**。回測區間跨過 `DAY_TRADE_TAX_EXPIRY` 時，`StockCostModel` 會 `logger.warning` 提醒「當沖稅率減半假設可能已失效」——**看的是回測區間，不是執行當下的日期**。

### 3.3 成本模型（`core/backtest/models/cost_model.py`）

`CostConfig` 為一次回測固定的成本參數，`StockCostModel` 為方向感知的成本／損益計算，`PositionManager` 只呼叫這一層。

```python
class StockCostModel:
    # --- 單邊成本 ---
    def commission(self, price, volume) -> int: ...
    def tax(self, price, volume, *, action, position_type, is_day_trade) -> int:
        """買進恆為 0；賣出依 is_day_trade 選 0.15% / 0.3%"""

    # --- 放空專屬 ---
    def borrow_fee(self, price, volume, holding_days) -> int:
        """MARGIN: 賣出價金 × 0.08%（一次性，holding_days 無關）
           SBL:    Σ 每日市值 × 年化率 / 365（逐日計提）
           DAY_TRADE: 0"""
    def margin_required(self, price, volume) -> int:
        """MARGIN: 賣出價金 × 90%；SBL: 依設定；DAY_TRADE: 0"""
    def short_interest(self, proceeds, margin, holding_days) -> int:
        """(擔保價款 + 保證金) × 年利率 × 天數 / 365；DAY_TRADE / SBL 為 0"""
    def maintenance_ratio(self, proceeds, margin, cur_price, volume) -> float: ...

    # --- 損益（方向統一入口）---
    def realized_pnl(self, *, position_type, entry_price, exit_price, volume,
                     entry_cost, exit_cost, carry_cost=0) -> float:
        """LONG : (exit - entry) × shares - 成本
           SHORT: (entry - exit) × shares - 成本"""
    def roi(self, ...) -> float:
        """名目報酬率：分母一律為「開倉價金 + 開倉成本」→ 存入 record.roi"""
    def roi_on_capital(self, ...) -> float:
        """資金效率：分母為「實際佔用的資金」→ 存入 record.roi_on_capital"""
```

**兩個 ROI 的用途分工（必須遵守）**：任何跨交易的聚合統計（平均報酬、獲利因子、勝率分組）**一律使用 `record.roi`**。若當沖用名目、融券用保證金當分母，兩者混在同一個平均值裡毫無意義（保證金基準的數值天生高一截）。`roi_on_capital` 只出現在報表的獨立欄位，供評估資金運用效率。

**券費與利息的計算時點（單一 source of truth，避免重複計費）**

| 項目 | 計算時點 | 負責函式 |
|------|----------|----------|
| `MARGIN` 融券手續費 0.08% | **開倉時**一次收 | `open_position()` |
| `MARGIN` 融券利息（收入） | **平倉時**用 `exit_date − entry_date` 一次算 | `close_position()` |
| `SBL` 借券費（年化） | **逐日計提**到 `position.accrued_borrow_fee` | `accrue_holding_cost()` |
| 維持率／強制回補判定 | 每日收盤 | `check_margin_call()` |

即：每日部位檢查**不碰融券利息**（`MARGIN` 的利息只在平倉時算一次），`accrued_borrow_fee` 欄位**只有 `SBL` 會用**。這條規則同時寫在 `StockPosition` 的 docstring，否則很容易在兩個地方各算一次。

`StockUtils` 為純工具（股數換算、單邊手續費／稅的原始公式），**不承載方向邏輯**。

### 3.4 策略層宣告（`core/strategies/stock/base.py`）

| 欄位 | 型別 | 預設 | 說明 |
|------|------|------|------|
| `allowed_directions` | `Optional[Set[PositionType]]` | `None` → `{position_type}` | 方向白名單 |
| `short_method` | `ShortMethod` | `MARGIN` | 放空管道 |
| `cost_config` | `Optional[CostConfig]` | `None` → `CostConfig.default(...)` | 成本參數覆寫 |
| `short_constraint` | `Optional[ShortConstraint]` | `None` → `ShortConstraint()` | 可成交限制（§2.4） |
| `max_holding_days` | `Optional[int]` | `None` | 留倉放空保險絲 |
| `bar_execution_order` | `Optional[BarExecutionOrder]` | `None` → 依下表推導 | 執行順序 |
| `day_trade_uncovered_policy` | `DayTradeUncoveredPolicy` | `FORCE_COVER_AT_CLOSE` | §5.1 |
| `margin_call_policy` | `MarginCallPolicy` | `FORCE_COVER` | §5.2 |

**推導規則（引擎執行）**

| `position_type` | `enable_intraday` | 實際 `short_method` | 預設 `bar_execution_order` |
|-----------------|-------------------|---------------------|----------------------------|
| LONG | 任意 | — | `CLOSE_THEN_OPEN` |
| SHORT | True | 強制 `DAY_TRADE` | `OPEN_THEN_CLOSE` |
| SHORT | False | 用策略宣告（預設 `MARGIN`） | `CLOSE_THEN_OPEN` |

策略顯式設定 `bar_execution_order` 時，一律以策略為準。

**方向的責任分工（呼應 §1 原則 2）**
- `position_type`：只用來推導預設值（執行順序、`short_method`、`allowed_directions`）。
- `allowed_directions`：`validate_orders` 的白名單；不在名單內的 order 被 warning 剔除。
- **實際記帳與成本路徑**：一律看 `order.position_type` / `position.position_type`，引擎與 `PositionManager` **不得**回頭讀 `strategy.position_type` 做分支。
- 要寫市場中性策略，只需 `allowed_directions = {LONG, SHORT}`，引擎不用改（同標的雙向仍禁止，見 §5.5）。

### 3.5 `Backtester` 的方向中立機制

```python
resolve_open_action(position_type)   # LONG → BUY；SHORT → SELL
resolve_close_action(position_type)  # LONG → SELL；SHORT → BUY
validate_orders(orders, stage)       # 白名單 + action 與方向是否相符，不符 warning 剔除
enrich_orders(orders)                # 依 §3.4 推導表補 short_method / is_day_trade
validate_fill_price(order, quote)    # §5.6 的三道檢查
snapshot_daily_equity(...)           # 含未實現損益的逐日權益
```

單根 K 棒的流程固定為（`execute_bar()`，日 K 與 Tick 共用）：

```python
if order == BarExecutionOrder.OPEN_THEN_CLOSE:
    execute_open_signal(quotes); execute_close_signal(quotes)
else:
    execute_close_signal(quotes); execute_open_signal(quotes)
settlement.on_bar_close(date, quotes, account, event_counts)
    # 當沖日終強制回補 → 借券費計提 ＋ 維持率追繳 → 停券回補 → 股利補償
snapshot_daily_equity(date, quotes)
```

訂單處理順序固定為 **`check_*_signal` → `validate_orders` → `enrich_orders` → `validate_fill_price` → `PositionManager`**。

**逐日權益快照是放空的必要條件**：每日部位檢查本來就要取當日收盤價算維持率，順手產出「含未實現損益的每日權益快照」成本極低但價值很高——放空最大的風險就是持倉期間的逆勢，只認已實現損益的權益曲線會把這段完全抹平。

### 3.6 `StockOrder` 的隨單欄位

`StockPositionManager` 只拿得到 `account` 與 `StockOrder`，沒有策略物件，因此稅率／保證金所需的資訊必須**隨單帶入**：`short_method: Optional[ShortMethod] = None`、`is_day_trade: bool = False`。

**策略不需要自己填**——`Backtester.enrich_orders()` 在 `validate_orders` 之後、送進 `PositionManager` 之前依 §3.4 推導表統一補值（LONG 兩欄維持預設）。這樣既符合「方向與屬性隨 order 走」（§1 原則 2），又不增加策略作者的負擔；要逐單混用不同管道也不必改介面。

---

## 四、公式與手算驗收範例

§4.1／§4.2 的數字即 `tests/backtest/test_cost_model.py` 與 `test_position_manager.py` 的斷言值。

### 4.0 數值處理規則（先讀，§4.1／§4.2 的每個數字都依賴這節）

**取整**

| 項目 | 規則 | 理由 |
|------|------|------|
| 手續費 | `max(MinFee, int(價金 × 費率 × 折扣))`——**無條件捨去** | 沿用 `calculate_transaction_commission` |
| 證交稅 | `max(1, int(價金 × 稅率))`——無條件捨去，最低 1 元 | 沿用 `calculate_transaction_tax` |
| 融券手續費／借券費 | `int(...)` 無條件捨去，**無最低金額** | 與其他費用一致 |
| 融券利息（收入） | `int(...)` 無條件捨去 | 收入捨去 = 保守 |
| 保證金 | `math.ceil(...)` 無條件進位 | 佔用資金進位 = 保守 |
| 損益 `realized_pnl` | `round(x, 2)` | 沿用 `calculate_net_profit` |
| ROI | `round(x, 2)`（單位 %） | 沿用 `calculate_roi` |

> ⚠️ 這是 §4.1 得到 42（`42.75` 捨去）而非 43、§4.2 利息得到 10（`10.41` 捨去）的原因。**用 `round()` 會讓驗收全數失敗**。
>
> 保證金進位前須先 `round(x, 6)` 消除浮點尾數：`33.33 × 1000 × 0.9` 的實際值為 `29996.999999999996`，直接 `ceil` 會多收一元。

**天數**

- `holding_days = (exit_date − entry_date).days`，即**曆日**（calendar days），非交易日、也非 bar 數。同日開平倉 = 0 天。
- 利息與 `SBL` 借券費一律以曆日 ÷ 365 計算（§2.3）。

**價格**

- 任何進入成本計算的價格，都必須**先**經滑價調整、**再**經 `round_to_tick`（§2.5），最後才算手續費／稅。

### 4.1 當沖放空（`DAY_TRADE`）

100 元放空 1 張（1,000 股），95 元回補。折扣 0.3，最低 20 元。

| 項目 | 計算 | 金額 |
|------|------|------|
| 賣出價金 | 100 × 1000 | 100,000 |
| 賣出手續費 | max(20, 100000 × 0.001425 × 0.3) | 42 |
| 證交稅（當沖 0.15%） | 100000 × 0.0015 | 150 |
| 回補價金 | 95 × 1000 | 95,000 |
| 回補手續費 | max(20, 95000 × 0.001425 × 0.3) | 40 |
| **已實現損益** | (100 − 95) × 1000 − 42 − 150 − 40 | **4,768** |
| `roi`（名目：賣出價金 + 開倉成本 = 100,192） | 4768 / 100192 × 100 | **4.76%** |
| `roi_on_capital`（佔用資金 = 開倉成本 192） | 4768 / 192 × 100 | 2483%（當沖幾乎不佔資金，僅供參考，不進聚合統計） |

### 4.2 融券留倉放空（`MARGIN`，持有 10 天）

100 元放空 1 張，95 元回補。

| 項目 | 計算 | 金額 |
|------|------|------|
| 賣出價金（擔保品） | 100 × 1000 | 100,000 |
| 保證金（90%） | 100000 × 0.9 | 90,000 |
| 賣出手續費 | — | 42 |
| 證交稅（0.3%） | 100000 × 0.003 | 300 |
| 融券手續費（0.08%） | 100000 × 0.0008 | 80 |
| 回補手續費 | — | 40 |
| 融券利息收入 | (100000 + 90000) × 0.002 × 10 / 365 | 10 |
| **已實現損益** | 5000 − 42 − 300 − 80 − 40 + 10 | **4,548** |
| `roi`（名目：賣出價金 + 開倉成本 = 100,422） | 4548 / 100422 × 100 | **4.53%** |
| `roi_on_capital`（保證金 + 開倉成本 = 90,422） | 4548 / 90422 × 100 | 5.03% |
| 開倉時 `balance` 變化 | −(90000 + 42 + 300 + 80) | −90,422 |
| 平倉時 `balance` 變化 | +90000 + 4548 | +94,548 |

維持率檢查（股價漲到 130 元）：`(100000 + 90000) / (130 × 1000) = 146%` → 安全；漲到 146 元時 `190000 / 146000 = 130%` → 觸發追繳。

**資金規則**：賣出價款**不**計入 `balance`（留作擔保品，記在 `position.short_proceeds`）；`balance -= 保證金 + 開倉成本`；`account.margin_used += 保證金`。當沖（`DAY_TRADE`）保證金為 0，`balance -= 開倉成本`。

---

## 五、回補與風控的邊界情況

### 5.1 當沖放空日終未回補

處理策略由 `day_trade_uncovered_policy` 決定：

| 政策 | 行為 |
|------|------|
| `FORCE_COVER_AT_CLOSE`（預設） | 以當日收盤價強制回補，`logger.warning` 記錄，並在報表統計「強制回補次數」 |
| `CONVERT_TO_MARGIN` | 轉為融券留倉：補收保證金與券費，並補徵當沖減半與全額證交稅的差額 |
| `RAISE` | 直接 raise，用於嚴格驗證策略邏輯 |

**漲停無法回補的判定規則**：`limit_up = round_to_tick(前一交易日收盤 × 1.1, "down")`。

| 級別 | 判定條件 | 處理 |
|------|----------|------|
| 日 K | `close == high == limit_up` 且 `low == limit_up`（全日鎖死） | 視為**無法回補**，走 `CONVERT_TO_MARGIN` |
| 日 K | `close == limit_up` 但 `low < limit_up`（盤中曾打開） | 視為**可回補**，以 `limit_up` 成交（最壞價格），記為「漲停回補」事件 |
| Tick | 當日最後一筆成交價 == `limit_up` 且該價位無賣方成交量 | 無法回補 |

兩種情況都計入報表的獨立事件計數——這是放空策略最致命的尾部風險，不能被平均掉。

> **注意**：轉融券留倉時的 `CostConfig` 必須帶融券的保證金成數。當沖設定把 `margin_rate` 歸零，直接沿用會讓保證金算成 0、維持率立即誤觸斷頭。

### 5.2 維持率追繳／斷頭

`check_margin_call` 每日以收盤價計算維持率；低於 `MaintenanceRatio`（130%）時依 `margin_call_policy`：

- `FORCE_COVER`（預設）：**以觸發當日收盤價立即回補**，記為斷頭事件。
- `WARN_ONLY`：僅記錄，適合研究純訊號績效。

> **為什麼不是「次一交易日開盤價」**：實務上追繳有 T+1 補繳期，但現行引擎**沒有跨日的 pending order 機制**（每天從頭跑，訊號當下就撮合完畢），要做 T+1 延遲成交必須先設計 order queue，等同引擎重構。因此一律**當日收盤價立即回補**，並註明「斷頭價格較實務樂觀（少了一天的補繳緩衝，但也少了一天的續跌／續漲風險）」。見 [多市場回測引擎架構 §5.1](multi-market-engine.md#51-事件驅動迴圈長期方向)。

停牌無報價時，盯市沿用前一交易日收盤價並記 warning。

### 5.3 強制回補日與股利補償

`TwStockDataFeed` 由 `dividend` 表推導融券最後回補日，每根 bar 把當日觸及回補日的標的推給 `SettlementModel`：

```
融券最後回補日 = 除權息交易日往前推 4 個營業日
```

法規原文是「停止過戶日前 6 個營業日」，而停止過戶日 = 除權息交易日 + 2 個營業日，
兩者相減即得 4。`dividend` 表只有除權息交易日，故一律以此換算；營業日必須取自
`price` 表的實際開盤日（`StockPriceAPI.get_trading_days()`），用曆日相減會在連假整段位移。

**三個管道的適用範圍不同**，這是本節最容易搞錯的地方：

| 放空管道 | 除權息停券強制回補 | 除息日股利補償 |
|----------|--------------------|----------------|
| `MARGIN`（融券） | ✅ 於回補日以收盤價回補，計入 `forced_cover_suspended` | 正常情況下碰不到（已先被回補） |
| `SBL`（借券） | ❌ 不受強制回補約束 | ✅ 於除息日扣 `每股現金股利 × 股數` |
| `DAY_TRADE`（現股當沖） | 當日已由當沖強制回補處理完畢 | 同左 |

使用者透過 `ShortConstraint.force_cover_dates` **手動指定**的日期則**不分管道一律適用**
——引擎不替使用者的政策再加條件。

**股利補償的記帳**（`compensate_cash_dividend()`）：

- 只補償**除息日之前就在倉**的部位：除權息交易日當天賣出者已不含權。
- 除息當日即從 `balance` 扣款，平倉時再把攤提進 `realized_pnl` 的那一份加回，
  避免同一筆被扣兩次。**刻意不比照 `accrued_borrow_fee` 的「只在平倉扣一次」**：
  部位損益一律以未還原的 `quote.close` 盯市，除息跳空會讓空單當天憑空多出一段
  未實現獲利，唯有同日的現金流出才能把它抵銷掉，逐日權益曲線才不會失真。
- 現金股利為 `NULL`（上市權息並存無法拆分）時**不猜 0**，記 warning 並計入
  `dividend_compensation_unknown`。

**仍未涵蓋**：股東會停券（需股東會行事曆資料源），故推導出的回補日是實際停券日的
**子集**，留倉放空的持有天數仍會被高估一部分——`max_holding_days` 保險絲請保留。

### 5.4 部分回補與 FIFO

比照 LONG 的拆單邏輯，保證金、擔保價款、券費、利息、稅一律**按張數等比例攤提**。FIFO 迴圈的 `open_positions` 篩選帶 `position_type` 條件，同標的多空不會互相污染。

> **注意**：平倉時須把 `entry_cost` 加回現金流——`realized_pnl` 已含開倉成本，不加回會雙重扣費。

### 5.5 同標的雙向持倉

**禁止，兩個方向都擋**。`open_position` 若發現同一 `stock_id` 已有反向未平倉部位（先多後空、先空後多皆然）→ `logger.warning` 並拒單；已平倉的部位（`is_closed`）不算佔位。理由：`check_has_position` 與報表層假設單一方向，放寬需要一整套 net position 語意。

這與 §3.4 的 `allowed_directions` 不衝突：策略**可以**同時持有 A 股的多單與 B 股的空單（市場中性），只是**同一檔**不能雙向。

### 5.6 成交價合理性（前視偏誤防線）

`validate_fill_price` 對每一張 order 檢查三件事：

1. `low <= price <= high`（不得成交在當日沒出現過的價格）→ 不符即拒單；滑價把價格推出區間時先夾回再驗
2. 落在漲跌停區間內 → 不符即拒單
3. 已對齊價格檔位（§2.5）→ **僅 warning 不拒單**（既有資料可能有還原價精度問題，拒單會誤擋正常回測）

當日無成交量（OHLC 為 `NULL`）的標的一律拒單。

**兩種 scale 的價格基準不同（`TickQuote` 沒有 OHLC，必須分流）**

| Scale | 高低點來源 | 說明 |
|-------|-----------|------|
| `DAY` | `StockQuote.high` / `.low` | 已有 OHLC 欄位，直接用 |
| `TICK` | `FillModel` 維護的**當日累計高低點** | `intraday_range` 記錄各檔當日累計 low/high。**目前有前視**：引擎把整天的 tick 一次交給 `on_bar_open()`，區間是全日高低點而非「該 tick 之前」，盤中較早的委託會通過稍後才出現的價位；TICK 回測只能當量級參考 |

漲跌停基準一律取前一交易日收盤（`prev_close`），首個交易日無前收時跳過該項檢查。

特別針對 `OPEN_THEN_CLOSE`：策略在日 K 級別拿得到當日 `close`，卻可以宣稱「以 `open` 放空」。引擎無法從價格本身分辨這是合理假設還是前視偏誤，因此**當沖放空策略的文件中必須明確宣告成交價假設**（建議：開倉用 `open`，回補用 `close`），並在 `generate_open_signals` 的實作中只使用該時點之前可得的資訊。這條屬於策略紀律，引擎只能擋掉「不可能的價格」，擋不掉「可能但不誠實的價格」。

### 5.7 已知簡化（明確不做的部分）

| 項目 | 影響 | 為何不做／解除條件 |
|------|------|--------------------|
| T+2 交割（Lean 的 `SettlementModel`） | 資金可用時點被高估；現股當沖實際是淨額交割不需全額現金 | 對日頻策略影響小，實作成本高 |
| 股東會停券 | 留倉放空的持有天數仍被高估 | 缺股東會行事曆資料源；除權息停券已接上，見 §5.3 |
| `SBL` 議定費率的個股差異 | 熱門空方標的實際費率遠高於 3%（實務可達 16%） | `accrue_holding_cost()` 已能逐日計提，缺的只是每檔的實際議定費率。**卡借券成交資料源** |
| 平盤下放空限制與每日可當沖清單 | 高估可放空／可當沖的機會數 | `allow_below_reference` 與 `day_trade_whitelist` **有定義、無撮合呼叫端**（見 §2.4）。**卡處置股／警示股公告與每日可當沖清單資料源**。接上後須移除建構期警告並改寫 `tests/backtest/test_unimplemented_constraints.py`；待辦見 [暫緩工作彙整](../../backlog/暫緩工作彙整.md) S5 |
| 融資做多槓桿 | LONG 一律以現金全額買進，資金效率被低估 | 會動到 LONG 的資金計算、破壞回歸保護線，且**目前無策略需求** |
| 同一標的雙向持倉（net position 語意） | 無法對同一檔做多空轉換 | 跨標的的多空並存**不受限**。放寬需要 `StockPosition` 改成淨部位語意、成本攤提與報表全部連動，且**目前無策略需求** |
| TICK 級別的成交量上限 | Tick 回測的下單張數不受累計成交量約束 | 日 K 已有 `FillConfig.max_volume_share`，見 [`core/backtest/README.md`](../../core/backtest/README.md)〈成交假設〉 |

---

## 六、與業界回測框架的對照

用來確認本設計沒有偏離主流做法（QuantConnect Lean / Backtrader / Zipline / Nautilus Trader）：

| 本框架元件 | 業界對應 | 對齊程度 |
|------------|----------|----------|
| `StockCostModel` + `CostConfig` | Lean `FeeModel`；Backtrader `CommissionInfo`；Zipline `CommissionModel` | ✅ 一致。Lean 另把 `BuyingPowerModel`／`MarginInterestRateModel` 拆開，本專案合併，需留意 `CostConfig` 不要膨脹成 god object |
| `SettlementModel` 的每日部位檢查 | Lean `MarginCallModel` + `MarginInterestRateModel`；Backtrader `interest_long`；Nautilus 每日 mark-to-market | ✅ 一致 |
| `ShortConstraint.check_borrowable` | Lean `IShortableProvider` | ✅ 概念一致 |
| `validate_fill_price` | 各框架的 fill model 都限制成交價於 bar range 內 | ✅ 一致 |
| `round_to_tick` | Lean `SymbolProperties.MinimumPriceVariation` | ✅ 一致 |
| 方向來自 order（§1 原則 2） | 業界一律 signed quantity（`order(-100)` 即放空），方向屬於部位 | ✅ 一致；若把方向綁在策略層則會偏離 |
| `BarExecutionOrder` 全域順序旗標 | 業界是 order queue + fill model，無此旗標 | ⚠️ 簡化版，屬過渡設計 |
| 無 T+2 交割 | Lean `DelayedSettlementModel` | ⚠️ 已列已知簡化（§5.7） |
| `ShortMethod` 三管道（當沖／融券／借券） | 業界無對應 | 台股在地化，合理 |

---

## 程式落點

| 檔案 | 內容 |
|------|------|
| `core/backtest/backtester.py` | 方向驅動、訂單驗證與補值、成交價驗證、`execute_bar()`、逐日權益 |
| `core/backtest/models/cost_model.py` | `CostConfig`／`ShortConstraint`／`StockCostModel` |
| `core/backtest/models/fill_model.py` | 成交價驗證、券源檢核、當日累計高低點 |
| `core/backtest/models/settlement_model/` | 當沖強制回補、借券費計提、維持率追繳、停券回補、股利補償 |
| `core/managers/stock/position_manager.py` | 放空開平倉兩個分支、FIFO 方向篩選、雙向持倉拒單 |
| `core/backtest/report/reporter.py` | 時間軸用 `exit_date`、放空欄位、多空統計、事件報表 |
| `core/utils/constant.py`／`instrument.py` | enum、費率、檔位表、`round_to_tick` |
| `core/models/stock/*` | 放空欄位、`entry/exit` 實體欄位、方向感知查詢 |
| `tests/backtest/` | 單元／整合測試、SHORT 快照回歸、LONG 逐筆回歸 |

**時間軸的重要約定**：`StockTradeRecord` 的 `buy_*` / `sell_*` 以「動作」對應（SHORT 的 `sell_*` 是**開倉**），因此**報表時間軸一律使用 `exit_date`，不可用 `sell_date`**，否則 3 月放空、5 月回補的交易會被畫在 3 月。`entry_date`／`entry_price`／`exit_date`／`exit_price` 是實體欄位而非 property——reporter 以 `pd.DataFrame` 組報表，property 取值會被繞過。

**策略撰寫指南**：`core/strategies/README.md` 的放空策略章節（設定欄位表、訊號方向對照、完整範例）。

**費率資料來源**（2026-07 查核，引用前請重新確認是否變動）

- [當沖降稅優惠延長至 2027 年底](https://tw.stock.yahoo.com/news/%E7%95%B6%E6%B2%96%E9%99%8D%E7%A8%85%E5%84%AA%E6%83%A0-%E5%BB%B6%E9%95%B7%E8%87%B32027%E5%B9%B4%E5%BA%95-201000635.html)
- [融券保證金成數、融券手續費與利息說明（StockFeel）](https://www.stockfeel.com.tw/%E8%9E%8D%E5%88%B8-%E4%BF%A1%E7%94%A8%E4%BA%A4%E6%98%93-%E8%9E%8D%E5%88%B8%E7%B6%AD%E6%8C%81%E7%8E%87/)
- [信用交易與融券維持率（臺灣證券交易所）](https://shl.twse.com.tw/page/library/trade/9.html)
- [借券出借費率區間 0.01%~16%（台新證券）](https://www.tssco.com.tw/SBL-intro/)
- [現股當沖資格與處置股限制（永豐豐雲學堂）](https://www.sinotrade.com.tw/richclub/hotstock/%E8%82%A1%E7%A5%A8%E7%8F%BE%E8%82%A1%E7%95%B6%E6%B2%96%E6%9C%893%E7%A8%AE%E9%A1%9E%E5%88%A5-%E5%B0%8D%E5%80%8B%E8%82%A1%E5%B8%B6%E4%BE%86%E4%BB%80%E9%BA%BC%E5%BD%B1%E9%9F%BF-%E6%83%B3%E7%9F%A5%E9%81%93%E8%82%A1%E7%A5%A8%E6%9A%AB%E5%81%9C%E5%85%88%E8%B3%A3%E5%BE%8C%E8%B2%B7%E5%8E%9F%E5%9B%A0-%E5%90%8D%E5%96%AE-%E5%8F%AF%E5%BE%9E2%E7%B6%B2%E7%AB%99%E6%9F%A5%E8%A9%A2-6673e74b260da31ad44e6da0)

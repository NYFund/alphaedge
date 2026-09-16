# 台期貨平台：資料、回測與策略

> 本文件描述台期貨（TAIFEX）這條線的**現行能力**：資料表與指令、回測時的期貨語意、策略介面與已知限制。
> 回測引擎本身的架構（單一引擎 ＋ 可插拔 model）見 [多市場回測引擎架構](../backtest/multi-market-engine.md)。

---

## 一、能做什麼

| 面向 | 現況 |
|------|------|
| 資料 | `tw_futures.db`：日行情（7 檔指數期貨 ＋ 股期）、連續合約（3 種調整 × 3 種換月）、保證金歷史（金額表 ＋ 比例表）、籌碼（三大法人／大額交易人／PCR）、股期標的池；逐筆成交走 DolphinDB |
| 指令 | `--target futures_price`／`futures_continuous`／`futures_margin`／`futures_chip`／`futures_stock_universe`／`futures_stock_price`／`futures_tick` |
| 回測 | 與台股**共用同一支引擎**：`(TW, FUTURE)` 的 model 組（規格／成交／成本／結算／資料源）由 `core/backtest/factory.py` 注入 |
| 期貨語意 | 逐日盯市、保證金查表與追繳、換月轉倉、日夜盤整併、期交稅與跳動點滑價 |
| 策略 | `MomentumFuturesStrategy`（示範），`python run.py --strategy MomentumFuturesStrategy` |

---

## 二、資料

### 2.1 落地原則：一律先寫入 `tw_futures.db`

**所有期貨資料在進入 API／回測之前，必須先經 loader 寫入 `data/db/tw_futures.db`。**
API、策略、回測、前端都不得直接讀 `data/downloads/tw_futures/` 下的中繼檔——中繼檔只是
crawler 到 loader 之間的暫存，不是資料真相來源。理由：唯一鍵約束在 DB 層擋掉重複與重跑污染、
同一份 DB 快照 ＝ 同一份回測輸入、API 層只需面對 SQL。

`tw_futures.db` 與 `tw_stock.db` **分開**存放，避免 `stock_id` 與契約代碼語意混用。
期貨表名一律帶 `futures_` 前綴，股期除權息需要與 `tw_stock.db` 對照時（`ATTACH`）不會混淆。

### 2.2 資料表

| 資料表 | 內容 | 主鍵 |
|--------|------|------|
| `futures_price_daily` | 各月份契約日 K（日盤、夜盤各一列） | `(date, product, expiry, session)` |
| `futures_continuous` | 連續合約，衍生自 `futures_price_daily` | `(date, product, session, method, roll_rule)` |
| `futures_margin_history` | 指數類與 ETF 股期的**每口金額**（原始／維持），變動序列 | `(effective_date, product)` |
| `stock_futures_margin_rate_history` | 股票類的**適用比例**，變動序列 | `(effective_date, product_id)` |
| `futures_institutional_chip` | 三大法人期貨買賣超與未平倉 | `(date, 商品名稱, 身份別)` |
| `futures_large_trader` | 大額交易人未平倉 | 另含到期月份與交易人類別 |
| `futures_put_call_ratio` | 台指選擇權 PCR | 一天一列 |
| `futures_stock_universe` | 股期標的池**快照序列** | `(snapshot_date, product_id)` |

`futures_price_daily` 的欄位：

| 欄位 | 型別 | NULL | 說明 |
|------|------|:----:|------|
| `date` | TEXT | ✕ | **以官方歸屬日為準**，不可從 timestamp 自行推算（見 §3.5） |
| `product` | TEXT | ✕ | 商品代碼（TX、MTX…） |
| `expiry` | TEXT | ✕ | 月契約 `202609`、**週契約 `202609W1`**，故為 TEXT |
| `session` | TEXT | ✕ | `day` ／ `night` |
| 開盤價／最高價／最低價／收盤價 | REAL | ✓ | 該時段完全無成交時為 NULL，**不寫 0** |
| 成交量 | INT | ✕ | **該時段自己的量**（日盤存「一般」、夜盤存「盤後」，兩列相加即「合計」） |
| 結算價 | REAL | ✓ | **夜盤恆為 NULL**；到期契約最後交易日也是 NULL（另行公布特別結算價） |
| 未沖銷契約量 | INT | ✓ | 同上 |
| 最後最佳買價／賣價 | REAL | ✓ | 供滑價與成交模型使用 |

**價格欄一律允許 NULL**：宣告 NOT NULL 會逼 cleaner 填 0 才寫得進來，而結算價 0 會讓損益與維持率整段歸零且無任何徵兆。

**`session` 為什麼資料層一律分開存**：合併是有損操作——分開存隨時可以合併，合併存回不去。
2017-05-15 之前沒有夜盤、留倉策略的隔夜風險真的發生在夜盤、當沖策略只該看日盤，
這三件事都要求資料層保留兩個時段的原貌，合併與否交給回測層（§3.8）。

### 2.3 指令

```bash
python -m tasks.update_db --target futures_price           # 指數期貨日行情（FUTURES_TARGET_PRODUCTS）
python -m tasks.update_db --target futures_stock_universe  # 股期標的池當日快照
python -m tasks.update_db --target futures_stock_price     # 股期行情（標的池流動性前 N 檔）
python -m tasks.update_db --target futures_continuous      # 由日行情整段重建連續合約（不連網路）
python -m tasks.update_db --target futures_margin          # 保證金變動序列
python -m tasks.update_db --target futures_chip            # 三大法人、大額交易人、PCR
python -m tasks.update_db --target futures_tick            # 逐筆成交（需 [tick] 相依、Shioaji 金鑰、DolphinDB）
```

- `futures_price` 已被 `--target all` 與 `no_tick` 涵蓋；日常更新以**商品為單位**從表內該商品的最新日接續。
- **往前擴張回補區間不能用日常指令**：resume 會從表內最新日接續，整段歷史補不到而只顯示「已是最新」。
  要以明確區間呼叫 `FuturesPriceUpdater.update(start_date=..., end_date=..., resume=False)`。
- **新商品的回補起點要用它的上市日**（`FUTURES_PRODUCT_LISTING_DATES`），不可沿用 `DEFAULT_FUTURES_START_DATE`：
  上市前每一天都查無資料，累積到 `EMPTY_PRODUCT_ABORT_THRESHOLD` 會中止並 raise——這道保險絲是為了擋
  「代碼拼錯而每天安靜地查無資料」。
- 新商品的連續合約要等該商品的行情補完後再跑 `--target futures_continuous`。
- 股期**不要一次爬 320 檔**：預設 `STOCK_FUTURES_TOP_N=20`，尾端有整批一天成交個位數口的商品，
  納入回測只會製造「回測賺錢、實際掛不到單」的假訊號。排序依已入庫行情的平均日成交量，
  第一次跑還沒有行情時會退回整份清單並提醒。

### 2.4 商品與乘數

| 商品 | 代碼 | 契約乘數 |
|------|------|----------|
| 台股期貨（大台） | TX | 200 元/點 |
| 小型台指期貨（小台） | MTX | 50 元/點 |
| 微型台指期貨 | TMF | 10 元/點 |
| 電子期貨 | TE | 4000 元/點 |
| 小型電子期貨 | ZEF | 500 元/點 |
| 金融期貨 | TF | 1000 元/點 |
| 小型金融期貨 | ZFF | 250 元/點 |

- **代碼與乘數的權威來源是 `core/utils/constant.py`**（`FuturesProduct` ＋ `FUTURES_MULTIPLIER`），本表僅供閱讀。
- **收錄門檻是「乘數已查證」**：`FUTURES_TARGET_PRODUCTS` 內的每一檔都必須在 `FUTURES_MULTIPLIER` 登錄（有測試）。
  乘數錯了不會報錯，只會讓 PnL 靜默偏掉——同樣漲 100 點，TX 賺 20,000、TMF 只賺 1,000。
- **XIF 非金電不納入**：其乘數曾由 100 元／點改為 10 元／點，需先以帶生效日的方式表達。
- **兩種乘數來源不可混用**：指數期貨走 `FUTURES_MULTIPLIER` 常數；股票期貨走 `futures_stock_universe` 的契約單位（**會因除權息調整，寫死必錯**，見 §3.10）。
- ETF 期貨（0050 期貨 `NYF` 等）與股期同樣走標的池，不寫死。

### 2.5 來源格式上的坑

以下每一條都**不會報錯，只會讓資料靜默缺漏或偏掉**，皆已固化為測試：

| 來源 | 坑 | 處理 |
|------|----|------|
| TAIFEX 行情頁 | 擋流量時回 **HTTP 200 ＋ 沒有行情表的頁面**，與非交易日長得一樣 | 空產出一律等待後重試一次才算數，等待隨連續空產出遞增；節流約 0.4 req/s |
| TAIFEX 行情頁 | 頁面第二張表是「價差對價差成交」 | 以「是否有單層 `契約` 欄」辨識，不用位置索引 |
| TAIFEX 行情頁 | `到期月份` 被 pandas 讀成 `202609.0` | 以字串讀入 |
| TAIFEX 行情頁 | 商品下拉選單內容不穩定，股期代碼含逗號（`EE1,EEF`） | 不以下拉當白名單；股期代碼改取自標的一覽表 |
| 標的一覽表 | 穩懋的商品代碼就是 `NA`，被 pandas 當成缺值 | `pd.read_html`／`pd.read_csv` 兩處都要 `keep_default_na=False`（`dtype=str` 擋不住） |
| 籌碼 CSV | 非交易日回 HTTP 200 ＋ 一整頁 HTML | 檢查第一行是不是真的 CSV 表頭 |
| 籌碼 CSV | PCR 每列結尾多一個逗號，整列往左位移 | `index_col=False` |
| 籌碼 CSV | 檔尾三行說明文字被解析成資料列 | 清洗層濾掉 |
| 保證金公告 | 附件用固定檔名（`保證金調整情形列表.csv`），站方覆寫後下載舊公告會拿到新內容 | 同一附件網址被多則公告引用時只信最新那則（`resolve_csv_urls()`），不依賴數值大小判斷 |
| 保證金公告 | 標題措辭不固定（「調整」「回調」「調高」、換行造成的空白），用標題篩選必漏 | 取回所有提到「保證金」的公告，由附件結構收斂：須為 CSV 且表頭相符、`契約ABC值` 非空（選擇權）剔除 |
| 保證金公告 | 金額欄順序與一覽表相反；同一個表頭底下，股票期貨給的是適用比例而非金額 | 依欄名取值；以值有沒有小數點區分比例與金額 |
| 保證金一覽表（股票類） | 一份檔案四段（股期比例／ETF 股期金額／兩段選擇權），每段各有更新日期；公司名含逗號、代碼帶尾端空白 | 先切掉選擇權段、逐段解析生效日；以 `csv` 模組解析並 strip 代碼 |
| Shioaji | 契約代碼與 TAIFEX 沒有規律（MTX→MXF、TE→EXF、TF→FXF；TMF／ZEF／ZFF 兩邊同名） | `SHIOAJI_FUTURES_CATEGORY` 是實際登入逐一核對的對照表 |
| Shioaji | `code` 是「月份字母 ＋ 年末碼」，每 10 年重複 | 一律用 `symbol`（`TXF202609`） |
| Shioaji | 回傳整個交易日的逐筆，**含前一日 15:00 開始的夜盤** | 時段一律由時間戳判定 |

籌碼代碼的正式定義（取自來源檔尾）：到期月份 `999999` ＝ 所有契約合計、`666666` ＝ 所有週到期契約合計；
交易人類別 `0` ＝ 前五／十大交易人、`1` ＝ 其中的**特定法人**（`1` 是 `0` 的子集，兩者相加沒有意義）。
三大法人的商品以**中文名稱**入庫不轉代碼，要接回行情表時以 `futures_margin_history` 的 `product`／`product_name` 對照。

### 2.6 保證金序列

- **分表依據是「金額 vs 比例」，不是「指數 vs 股票」**：指數期貨與 ETF 股期給每口固定金額（`futures_margin_history`）；
  股票股期給適用比例 ＋ 級距（`stock_futures_margin_rate_history`），比例存**小數**（`0.1350`）——
  存百分比時忘記除 100 會讓保證金差 100 倍而不報錯。股期的每口保證金 ＝ 標的股價 × 契約單位 × 比例，
  由 `FuturesMarginAPI.calculate_stock_futures_margin()` 計算。
- **表是變動序列，不是每日快照**：`effective_date` 是這組保證金開始適用的日子，查詢一律取
  `effective_date <= 該日` 的最大者。TAIFEX 的調整**溯及既往**（未沖銷部位一併適用新標準），
  所以回測不能把開倉當天的保證金鎖死。
- **兩條寫入路徑**：`--target futures_margin` 抓現行一覽表（`source='snapshot'`，保證金沒變就新增 0 列）；
  歷史走調整公告，須直接呼叫 `FuturesMarginUpdater.update_history(start_date=..., end_date=...)`
  （`source='announcement'`）。同主鍵時**公告覆蓋 snapshot**——公告明載生效日與調整前後值，比一覽表權威。
- **「查不到」有兩種成因**：查詢日早於 2020-03（來源限制），或該商品從未被調整過——公告只列有調整的商品，
  級距穩定的股期整段只有一覽表那一列。兩者無法區分，故 `fallback_to_earliest=True`（取最早一列當近似）預設關閉。
- **鏈式比對只記缺口、不拒收**：同一商品第 N 則公告的「調整前」應等於第 N−1 則的「調整後」（`check_margin_chain()`）。
  對不上代表我們的歷史有缺口，不代表這一則有錯——拒收會讓一個缺口連鎖到後面所有公告。
  少數商品（NYF、SRF、OAF 等）各有一處斷點，全部對應附件已被站方覆寫的公告，是來源本身的損失。

---

## 三、回測語意

### 3.1 model 組

| 掛點 | 期貨實作 | 重點 |
|------|----------|------|
| `InstrumentSpec` | `TwFuturesSpec` | 契約乘數、跳動點 |
| `FillModel` | `TwFuturesFillModel` | 成交價須落在 bar range；滑價以**跳動點**表達 |
| `CostModel` | `TwFuturesCostModel` | 期交稅 ＋ 每口手續費（§3.3） |
| `SettlementModel` | `TwFuturesSettlementModel` | 逐日結算、保證金追繳、換月轉倉、保證金口徑的權益計價 |
| `DataFeed` | `TwFuturesDataFeed` | `tw_futures.db`、期貨交易日曆、乘數解析、日夜盤整併 |
| 報表 | `FuturesBacktestReporter` | 繼承台股報表，只覆寫交易明細欄位（`Contract ID`）、多空統計欄位、對標序列 |

**對標序列是「近月拼接」，不是連續合約**：換月當天有一段展期價差造成的假跳空，只能當粗略參考。

### 3.2 記帳：保證金交易與逐日盯市

1. **開倉只凍結保證金，不買下契約價值**。`FuturesAccount.margin_used` 記凍結的保證金，
   `equity` ＝ `balance` ＋ `margin_used`，開倉當下 `equity` 不變。部位計入權益的方式由
   `TwFuturesSettlementModel.mark_position()` 覆寫為保證金口徑。
2. **逐日盯市**：`settle_daily()` 每日以結算價結清損益、現金當天進出 `balance`、`position.price` 重設為結算價，
   累計記在 `settled_pnl`。**因此 `position.price` 不是開倉價**，開倉價另存 `entry_price`。
   平倉時的總損益 ＝ 已結算段（依平倉口數等比例攤提）＋ 最後一段；`None` 結算價必須跳過而不是當 0。
3. **PnL ＝ 價格變動 × 乘數 × 口數**，沒有股數換算、沒有證交稅。**ROI 的分母是保證金**——用契約價值會把槓桿效果抹掉。
4. **同一契約不允許雙向持倉**：已有反向未平倉部位即拒單（跨月份的價差部位不受此限）。

### 3.3 成本

| 項目 | 值 | 性質 |
|------|----|------|
| 期交稅 | 契約價值 × 十萬分之二，**買賣各課一次** | 法規值（`FuturesCost.TaxRate`） |
| 手續費 | 每口 50 元 | 市場常見值，由券商議定；逐商品以 `commission_per_lot_by_product` 指定，**不要改預設值** |
| 滑價 | 以跳動點數表達，可逐商品設定（`slippage_ticks_by_product`） | 同一個基點數在不同價位是不同檔數，沿用基點會讓同一組設定跨年份變成不同假設 |

與股票沒有一項共用：證交稅只課賣出、稅基是成交金額、手續費是費率——任何一項沿用都會算錯。
要跑「PnL 恰好等於價格公式」的驗證口徑，明確使用 `FuturesCostConfig.free()`。
費率只有一份：`FuturesPositionManager` 一律轉呼叫 CostModel。

### 3.4 保證金與追繳

- **查表是預設**（`FuturesMarginConfig.use_api=True`），API 由 `TwFuturesDataFeed.setup()` 注入；
  資料自 2020-03 起。要回測更早的期間請明確改用 `FuturesMarginConfig.ratio()`（契約價值 × 比率的近似）——
  `setup()` 會比對回測起始日與涵蓋區間，早於涵蓋起點時開跑前就警告。查表模式查不到會 raise（刻意的）。
- **設定物件只有一份**：factory 建好後回寫給策略，策略（算可開口數）、部位管理層（算應繳保證金）、
  結算模型（算追繳門檻）與 DataFeed 四者共用。兩份設定會造成「策略算得出口數、部位管理層卻開不進去」。
- **追繳判斷的是權益，不是可動用餘額**：浮動損益每日結算進帳戶，權益低於維持保證金才追繳；
  浮動獲利可以支撐加碼。**維持保證金是另一個公告值**，不可由原始保證金乘比率推得（`get_maintenance_margin()`）。
- **強制平倉砍到足額為止**：每平一筆就重算，先砍佔用保證金最多的契約。
  `WARN_ONLY` 只記 log **不計數**——`forced_cover_margin_call` 的語意是「強制平倉幾次」。

### 3.5 交易日曆

- **交易日取自行情表**（實際有行情的日子）：颱風假、補行交易日、臨時休市都是公告出來的事實，推不出來。
- **結算日規則只用來算「應該在哪天到期」**：月契約是第三個星期三，**遇休市順延到期貨自己的下一個開盤日**
  （春節連假可順延一週以上，這是不能沿用股票日曆的直接理由）。
  週契約 `YYYYMMWn` 是該月第 n 個星期三，**沒有 W3**（第三週就是月契約）。
- **夜盤屬於次一營業日**：15:00 開盤、次日 05:00 收盤，凌晨的成交屬於前一天開始的那段夜盤；
  星期五晚上那一段屬於星期一。cleaner 一律以官方歸屬日為準。
- **2017-05-15 之前沒有夜盤**，那是制度不是資料缺漏。
- 日曆涵蓋區間比回測結束日多取 45 天，否則末段契約的最後交易日會落在區間外而算不出來。

### 3.6 換月

- **換月放在結算模型**：契約會到期，部位不轉倉就會憑空消失，那是市場結構強加的；但「什麼時候轉」是政策，
  由 `FuturesRollConfig.rule` 決定——撐到最後交易日／提前 N 個交易日／未沖銷量交叉。
- **規則只有一份實作**（`core/backtest/datafeed/tw/futures_roll.py` 的 `FuturesRollPlanner`）：
  建連續合約、策略挑合約（`select_near_month()`）、結算模型轉倉都走它。兩處不一致會出現「訊號在次月、部位還在近月」。
- **轉倉 ＝ 平舊倉 ＋ 以相同口數與方向開新倉**，展期價差**如實入帳**。
- 週契約不由本規則轉倉；新契約當日無報價時不轉倉；新倉開不進去（保證金調高）只記 warning 不還原。
- 契約到期後仍拿不到報價時，以最近一次結算價強制出場並計入 `forced_cover_no_quote`（兜底，不是換月）。

### 3.7 連續合約

- 三種調整方式存在同一張表的不同 `method`，回答的問題不同：`BACKWARD`（差額）讓**點數差**連續，
  適合技術指標與點數停損；`RATIO`（比例）讓**報酬率**連續，適合波動度與報酬統計；`NONE` 是對照組。
- **「調整後的換月日變動必須等於新契約自己的日變動」**是唯一抓得到調整方向寫反的檢查——方向錯了序列一樣連續、還原檢查一樣通過。
- 展期價差與比例必須取自同一天的兩個契約。
- **換月只往前不回頭**：未沖銷量交叉後反轉時沿用昨天的契約。
- **未沖銷量缺漏不可當成 0**：夜盤本來就是 NULL，當 0 會讓近月被判定輸給次月而誤觸換月。

### 3.8 日夜盤整併

- 策略把 `session` 設為 `FuturesSession.COMBINED`，DataFeed 就把「**前一交易日**夜盤 ＋ 當日日盤」合成一根 bar
  （週一要取到週五，不是前一個曆日）。整併在報價層，不另建整併表。
- **跨盤別跳空被保留**：整併後的 `open` 取夜盤開盤、`low`／`high` 涵蓋夜盤。
- **`COMBINED` 不是資料表裡的值**：拿它查資料表一律回空表、策略整場零交易而不報錯。
  查歷史行情一律走 `BaseFuturesStrategy.price_query_session`；ETL 迭代時段一律用 `FuturesSession.data_sessions()`。

### 3.9 籌碼的前視偏差

籌碼**全部盤後公布**。`FuturesChipAPI.get_available(date)` 取的是「資料日 **< date**」的最大者，
語意是「站在這一天早上，我能知道什麼」——**不是 `<=`**，那一個等號就是前視偏差。
要看某天實際公布什麼走 `get_on_date()`，**不可用於產生訊號**。

### 3.10 股票期貨

- **乘數不是常數**：股期的契約單位標準型是 2,000 股，但除權息之後會被交易所調整。
  adapter 以 `multiplier_resolver` 掛點由 `TwFuturesDataFeed.resolve_multiplier()` 決定：
  指數期貨查常數、股期**逐日查標的池的契約單位**，兩者都查不到就 KeyError。
- **不可同時套用還原價**：台股用還原價處理除權息，股期用調整契約單位處理，兩者是同一件事的兩種做法，
  同時套用就是雙重調整。股期行情一律用原始價（`FuturesQuote.adj_close` 恆為 None）。
- 契約單位歷史由**快照差分**推得（`get_contract_size_history()`），查詢日早於第一份快照時退回最早一份——近似，非事實。
- 標的池的掛牌日 ≈ 首次出現、下市日 ≈ 最後一次出現早於最新快照，**都是觀測值不是官方日期**，
  故建議每日更新標的池（快照愈稀疏，推出來的日期誤差愈大）。

---

## 四、策略

期貨策略繼承 `core/strategies/futures/base.py` 的 `BaseFuturesStrategy`，由 `StrategyLoader` 自動收錄，
`run.py` 與 factory 以 `(market, instrument_type)` 分派，不需要任何分流設定。

| 與股票策略的差異 | 基底提供的東西 |
|------------------|----------------|
| **一天不只一個報價**（多個到期月） | `select_near_month()`；換月規則由 `roll_config` 決定，要別的政策就覆寫它 |
| **口數由保證金決定，不是契約價值** | `calculate_max_lots()`；用契約價值會低估可開口數十倍以上 |
| **日盤與夜盤是兩筆獨立行情** | `filter_session()`；不過濾會讓訊號被算兩次 |
| 需要知道離到期還有幾天 | `calendar`、`get_trading_days_to_expiry()`、`check_near_expiry()` |
| 沒有券源／借券費／平盤下限制 | `BaseStockStrategy` 的那一整組欄位不存在 |

策略層查不到保證金時**開 0 口**而非拋錯；真正 raise 的是部位管理層（已決定開倉卻算不出保證金）。

`MomentumFuturesStrategy` 的用途是**驗證介面能跑通**，不是可用的交易邏輯——門檻是隨手取的。

---

## 五、前端指標

`frontend/services/futures_metrics.py` 以**欄位**（`Contract ID`／`Multiplier`／`Margin`）判斷是不是期貨報表，
另外顯示峰值佔用保證金、峰值口數、資金使用率、平均保證金報酬率，以及保證金與口數曝險的階梯曲線。

- **看峰值不是總和**：把每筆交易的保證金加起來是同一筆錢用了幾十次，沒有意義。
- 曝險曲線由交易明細的進出場日推導，**是近似**：逐日盯市會讓保證金隨結算價變動，精確版需要引擎輸出逐日保證金。

---

## 六、已知限制

| 項目 | 影響 | 解除條件 |
|------|------|----------|
| **DolphinDB 的期貨 tick 寫入路徑未實測** | `--target futures_tick` 的爬取與清洗已驗證，入庫未驗證；無連線時保留中繼檔並記 warning | 啟動 DolphinDB server ＋ `pip install -e ".[tick]"`，跑一天確認 |
| 期貨 Tick 級別回測未實作 | `TwFuturesDataFeed.get_quotes()` 對 Tick 回空 list 並記 warning | 出現日內期貨策略需求 |
| 保證金 2020-03 之前沒有資料 | 行情自 2015 起，但更早的期間只能用 `ratio()` 近似（TX 實測跨年份誤差 +143% ~ −38%），可開口數與追繳門檻失真 | 真的要回測 2015~2019 時，人工登錄 TX 家族該期間的 16 則調整公告（掃描影像；MTX 依乘數等比例推得），並以下一則公告的「調整前」逐筆鏈式驗證、2020-03 首則的「調整前」當終點錨點。**不採 OCR**：`477000` 讀成 `47700` 不會報錯 |
| 價差部位保證金未模擬 | 同商品跨月份部位兩腿各繳全額，高估保證金、低估可開口數（保守；同契約雙向持倉已直接拒單） | 出現價差／對沖策略需求時，先確認 TAIFEX 價差保證金的收取語意（單邊全額／專屬金額／比例），再決定併入 `futures_margin_history` 或另立一張表 |
| 三大法人籌碼只有近三年 | 來源只保留約三年，更早無法回補 | 另找歷史來源 |
| 股期的調整型契約（`EE1` 等數字尾碼）與官方掛牌／下市日未入庫 | 契約單位與日期只能由快照差分近似；標的池建立之前的調整一律看不到 | 另抓 TAIFEX 契約調整與商品異動公告 |
| 跳動點只登錄已查證的台指期系列 | 其他商品需在建構時明確指定 `tick_size` | 逐商品查證後改為查表 |
| 2017-05-15 之前仍會查詢夜盤 | 回補時多打約一成的請求並記大量 `No valid futures price rows` warning（資料正確） | crawler 或 updater 在該日前跳過夜盤查詢 |
| 對標序列是近月拼接 | 報表的期貨對標曲線在換月接點有假跳空 | 改讀 `futures_continuous` |
| 保證金歷史回補每次重抓全部公告 | `FuturesMarginUpdater.update_history()` 算了已入庫的生效日（`loaded_dates`）卻沒拿來跳過，統計的「已存在跳過」恆為 0；每次回補都重新下載全部附件（約數百次請求），資料因 `INSERT OR REPLACE` 而正確 | 以 `source='announcement'` 的生效日跳過已入庫的公告（注意同一則公告可能同時有金額與比例兩張表的列） |

## 相關文件

- [多市場回測引擎架構](../backtest/multi-market-engine.md)——單一引擎與 model 掛點
- [資料覆蓋範圍](../exchanges/data_coverage.md)——各商品日盤／夜盤起始日與 NULL 語意
- [ETL 入庫約定](../pipeline/etl-ingestion.md)——各期貨 updater 的入庫時機與 resume 依據
- [命名軸線](../dev/naming-axes.md)——`tw/` 目錄與「地區 ＋ 商品」類別命名

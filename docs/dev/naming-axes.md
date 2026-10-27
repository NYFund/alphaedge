# 命名軸線：Market／InstrumentType／ListingBoard／IssuerOrigin

> 本文件描述全專案「市場」相關命名的**四條正交軸**、每條軸在目錄與程式碼中的落地位置、
> 防迴歸護欄與已定案的取捨。

---

## 為什麼要分軸

「市場／market」一詞很容易同時被拿來指三件互相正交的事——
**地區**（台股／美股）、**商品類別**（股票／期貨／選擇權）、**掛牌板別**（上市／上櫃／興櫃）。
混用的代價不只是用詞：用一維鍵（單一個 `market` 值）去選二維實作（`TwStockSpec` 這種「地區 ＋ 商品」組合），
只在全專案只有一個市場時碰巧能運作；同一個詞在兩份互相相依的文件裡指不同的軸時，讀者也無從分辨。
因此全專案把這些概念拆成四條軸，各有自己的名稱與值域。

## 四條軸

| 軸 | 名稱 | 值 | 語意 | 定義位置 |
|:--:|------|------|------|----------|
| A | `Market` | `TW`／`US` | 地區／交易市場。交易日曆、開盤時間、幣別屬於此軸 | `core/utils/constant/market.py` |
| B | `InstrumentType` | `STOCK`／`FUTURE`／`OPTION` | 商品類別。契約乘數、報價單位、結算規則屬於此軸 | `core/utils/constant/market.py` |
| C | `ListingBoard` | `sii`／`otc`／`rotc`／`pub`／`all` | 掛牌板別（上市／上櫃／興櫃／公開發行）。僅台股適用，值即公開資訊觀測站的 `TYPEK` 參數；目前只爬上市與上櫃 | `core/pipeline/utils/constant.py` |
| D | `IssuerOrigin` | `DOMESTIC = "0"`／`FOREIGN = "1"` | 發行人國別（國內／國外企業，即 F 股／KY 股）。值即月營收頁 URL 末碼 | `core/pipeline/utils/constant.py` |

**軸 A 命名為 `Market` 的依據**：對齊 Lean 的 `Market`（`usa`／`tw`）與 `tw_stock`／`tw_futures` 這類目錄與檔名；
`MarketCalendar`（台股交易日判定）也因此是正確用法——交易日曆與開盤時間本來就是地區屬性。

**軸 B 命名為 `InstrumentType` 的依據**：與專案的 `InstrumentSpec`（`core/market/instrument_spec.py`，「商品規格」）同一個詞；
亦對齊 Nautilus 的 `Instrument`。

**軸 C 與軸 D 為何要拆**：兩者若合併在同一個 Enum（例如 `SII0`／`SII1`／`OTC0`／`OTC1`），
`SII0 = "0"` 與 `OTC0 = "0"` 值相同，Python Enum 會讓後者成為前者的 **alias**——
`OTC0 is SII0` 為 `True`，任何以 `is` 判斷的程式都會踩空。上市與上櫃各自都有國內、國外兩種發行人，
兩者本來就是正交的兩條軸。

## 落地位置

| 位置 | 用法 |
|------|------|
| `core/strategies/base.py` | `self.market: Optional[Market]` ＋ `self.instrument_type: Optional[InstrumentType]`，由各商品類別的策略基底填入 |
| `core/strategies/stock/base.py`、`core/strategies/futures/base.py` | 分別填入（`Market.TW`, `InstrumentType.STOCK`）與（`Market.TW`, `InstrumentType.FUTURE`）；**個別策略不需自己設** |
| `core/backtest/factory.py` | 分派鍵為 `(strategy.market, strategy.instrument_type)`；未支援的組合拋出含兩軸值的 `ValueError` |
| `core/market/tw/`、`core/backtest/models/` | 實作類別命名即「地區 ＋ 商品」：商品規格 `TwStockSpec`／`TwFuturesSpec` 與成本模型 `TwStockCostModel`／`TwFuturesCostModel` 在 `core/market/tw/`；回測模擬零件 `TwStockFillModel`／`TwFuturesFillModel`（`fill_model.py`）、`TwStockSettlementModel`／`TwFuturesSettlementModel`（`settlement_model/`）在 `core/backtest/models/` |
| `scripts/check_layer_deps.py` 的 `_MARKET_AXIS_PACKAGES` | **市場軸目錄的權威清單**：`core/api`、`core/broker`、`core/live/datafeed`、`core/adapters`、`core/dao`、`core/backtest/datafeed`、`core/market`、`core/pipeline`。清單內的目錄只承載軸 A（`tw/`，未來 `us/`），商品類別由檔名承載（`stock_price_api.py` vs `futures_price_api.py`）、類別名仍是「地區 ＋ 商品」（`TwStockDataFeed`）；市場無關的底座（`base.py` 等）放在目錄根。新開的市場軸套件要登記進清單，跨軸混放才會被擋 |
| `core/dao/` | 資料存取層：`base.py`、`connection.py`、`timescale.py` 是市場無關底座，各資料表的 DAO 放 `tw/`（見 [資料存取層](data-access-layer.md)） |
| `core/api/` | 策略與資料源取數用的資料 API，各市場實作放 `tw/` |
| `core/adapters/` | raw 資料 → `Quote` 的純轉換層（不做 I/O），市場無關的 `quote_validation.py` 放根目錄、各市場轉換器放 `tw/` |
| `core/market/` | 市場結構與規則，ETL、回測、實盤、策略共用同一份。根目錄放市場無關的基底：`calendar.py`（`BaseTradingCalendar`）、`instrument_spec.py`（`InstrumentSpec`）、`cost_model.py`（`BaseCostModel`）、`segment.py`；台灣市場的實作放 `tw/`（交易日曆、期貨結算日與換月、保證金設定與模型、商品規格、交易成本、盤中時段） |
| `core/backtest/datafeed/` | 回測資料源（`TwStockDataFeed`／`TwFuturesDataFeed`）放 `tw/`；回測與實盤共用的契約 `BaseDataFeed` 在不分市場的 `core/datafeed/base.py` |
| `core/live/datafeed/` | 實盤資料源（歷史資料到 T−1 ＋ 當日券商報價）：`base.py`、`calendar.py`（盤前交易日判定）為市場無關底座，股票與期貨實作放 `tw/` |
| `core/broker/` | 券商閘道（送單、收回報、查帳務）：`base.py`（`BaseBroker`）等市場無關元件放根目錄，永豐金 Shioaji 的實作放 `tw/` |
| `core/pipeline/{shared,tw,utils}/` | 目錄**只承載軸 A**；商品類別由檔名承載（`stock_price_crawler.py` vs `futures_price_crawler.py`）。base 類別放 `shared/`，否則 `us/` 會反過來相依 `tw/`。**`utils/` 是層不是軸**（見下） |
| `scripts/check_layer_deps.py` 的 `_INSTRUMENT_AXIS_PACKAGES` | **商品軸目錄的權威清單**：`core/models`、`core/strategies`、`core/position`、頂層 `strategies`。子目錄承載軸 B（`stock/` ＋ `futures/`），其底下出現 `tw/`／`us/` 會被擋。商品無關的部分在 `core/models/base/`、`core/position/base/`、`core/strategies/base.py`；`core/models/` 根目錄另有兩軸皆無關的 `cost_config.py`、`fill_config.py` |
| `core/strategies/` 與頂層 `strategies/` | `core/strategies/` **只放契約**（`BaseStrategy` 與各商品類別的策略基底），具體策略放頂層 `strategies/{stock,futures}/`。`strategies/loader.py` 的 `StrategyLoader` 逐一掃描頂層 `strategies/` 的商品子套件，新增商品類別不需改程式 |
| `core/portfolio/` | **不承載任何軸**，是平的一層（`signal.py`、`sizing.py`、`construction.py`、`aggregation.py`、`order_rules.py`）。商品差異由**類別名**承載（`StockPortfolioConstructor` vs `FuturesPortfolioConstructor`）：這一層檔案少、回測與實盤共用，開子目錄只會讓少數幾個檔案各自躺在一個資料夾裡。日後某個市場需要自己的資金切分規則時，再依軸 A 開 `tw/` |
| `data/db/` | 檔名帶軸 A：`tw_stock.db`、`tw_futures.db`、`tw_trading.db`（實盤交易紀錄；常數 `TW_STOCK_DB_PATH`／`TW_FUTURES_DB_PATH`／`TW_TRADING_DB_PATH`，在 `core/config/schema.py`） |
| `core/pipeline/tw/crawlers/financial_statement_crawler.py` | `self.listing_boards`（軸 C） |
| `core/pipeline/tw/crawlers/monthly_revenue_report_crawler.py` | `self.issuer_origins`（軸 D）；TWSE／TPEX 的區分由呼叫端各自的 URL 決定，不是清單內容 |
| `core/pipeline/tw/utils/url_manager.py` | URL 樣板佔位符 `{issuer_origin}` |
| `futures_stock_universe.underlying_listing_board` 欄 | 存的是「上市／上櫃」，屬軸 C |

### 新增一個（市場, 商品）組合

回測側要動哪些掛點，見
[多市場回測引擎架構](../backtest/multi-market-engine.md) §四「新增一個（市場, 商品）組合要做什麼」。
策略側只需在該商品類別的策略基底填入兩個欄位，`factory` 即可分派。

## 防迴歸護欄

- `tests/test_naming_axes.py` 釘住四條軸的值域與 alias：
  - 軸 A 與軸 B 的值**不得有交集**——有人想把 `STOCK` 加回 `Market` 會當場失敗。
  - 軸 C 與軸 D 的值不得有交集。
  - 每條軸的「成員數 ＝ 值數」，即 alias 偵測。
- `scripts/check_layer_deps.py` 的「跨軸目錄污染」：市場軸目錄底下出現 `stock/`／`futures/`、
  商品軸目錄底下出現 `tw/`／`us/` 都會被擋；`core/strategies/` 放了契約以外的檔案也會被擋。

**軸之間的值一旦出現交集，就代表又有人把兩條軸壓回同一個名字裡**，
那是靜默的語意汙染，不會有任何執行期徵兆，只能靠測試擋。

## 已定案的取捨

### `futures_` 表名前綴維持不變

期貨表名帶 `futures_` 前綴（`futures_price_daily` 等）、台股表名不帶（`price`、`chip`），
這是**刻意決策**：股票期貨除權息需要與 `tw_stock.db` 對照（可能走 `ATTACH`），
帶前綴的表名在查詢裡不會混淆；日後若遷移到單一 PostgreSQL 資料庫，
兩個 SQLite 檔會併進同一個扁平命名空間，前綴更是必要而非多餘。

**真正的不對稱在另一側**：缺前綴的是 `tw_stock.db` 的 `price`／`chip`／`margin` 等表。
要對稱應該是補上 `stock_` 前綴，但那要改 `tw_stock.db` 全部資料表、數 GB 資料與所有 DAO，
留待資料庫遷移時一併處理。

### 每層目錄只承載一條軸

`core/pipeline/` 的目錄可以是 `pipeline/tw/` ＋ `pipeline/us/`（純市場軸），
或 `pipeline/tw_stock/` ＋ `pipeline/tw_futures/`（市場 ＋ 商品壓成單一目錄名）。
**採前者**——每層目錄只承載一條軸，商品類別由檔名承載，與本文件的整體原則一致。

### `utils/` 是層，不是軸

`core/pipeline/utils/` 與 `shared/` 同屬跨市場共用層，只放通用工具（`data_utils.py`、`exceptions.py`、`constant.py`）。
**綁定單一市場的工具一律往該市場目錄放**：`url_manager.py`（整張表都是 TWSE／TPEX／MOPS／TAIFEX 端點）、
`stock_tick_utils.py`（Shioaji 金鑰、續跑判斷的鍵是台股代號）、`mops_payload.py`（全是公開資訊觀測站的表單參數，
檔名直接承載來源）都在 `core/pipeline/tw/utils/`。

**為什麼不在 `utils/` 底下再開 `tw/`／`us/`**：那會讓同一層同時承載「層」與「軸」，正是本節要避免的事。

`constant.py` 裡的 `ListingBoard`／`IssuerOrigin` 確實只有台股適用，但它們是爬蟲參數，留在 `constant.py`；
**欄位 Enum 則是資料表 schema 的一部分**：`PriceColumn`／`ChipColumn`／`FuturesPriceColumn`
在 `core/config/schema.py`（`core/dao`、`core/api` 都要用，不能放在 pipeline）。

`shared/` 內**刻意不搬**的兩處台股痕跡：

- `BaseDataCrawler.NO_DATA_MARKERS` 是 TWSE／TPEX 的「查無資料」措辭——它是 class 屬性，
  未來 `us/` 的子類直接覆寫即可，不構成反向相依。
- `core/dao/base.py` 的 `create_symbol_date_index()` 寫死 `stock_id` 欄——
  跟著下方 `stock_id` → `symbol` 的改名一起處理。

## 已知限制

| 項目 | 現況 |
|------|------|
| `data/downloads/` | 用 `tw_stock/`／`tw_futures/` 的壓縮形式（理由見 `core/config/paths.py`：市場 × 商品只會出現實際支援的組合，多一層只是導覽成本）。與程式碼側的 `tw/` ＋ 檔名不完全同構 |
| 資料表欄位語言 | 跟著**資料來源**走，不跟市場走：交易所網頁／檔案爬來的保留中文欄，API 來源用其原始英文欄。規則與理由見 [ETL 入庫約定 §3.4](../pipeline/etl-ingestion.md) |
| `core/utils/instrument.py` 的 `StockUtils` 歸屬 | 未移出 `core/utils/`，取捨見 [多市場回測引擎架構](../backtest/multi-market-engine.md) |
| `stock_id` → `symbol` 的資料層改名 | 未做；台股表缺 `stock_` 前綴同屬這一批，留待資料庫遷移的 schema 改寫 |

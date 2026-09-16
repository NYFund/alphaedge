# 命名軸線：Market／InstrumentType／ListingBoard／IssuerOrigin

> 本文件描述全專案「市場」相關命名的**四條正交軸與其定案**，以及每條軸的落地位置、
> 防迴歸護欄與已定案的取捨。實作於 2026-08-30 至 2026-09-01 分八個步驟完成；
> 規劃文件已依
> [`manage-backlog` skill §5](../../.claude/skills/manage-backlog/SKILL.md#5-完成後的處理) 移出 `backlog/`。

---

## 概觀

在 2026-08-30 之前，全專案用「市場／market」一個詞同時指三條互相正交的軸——
**地區**（台股／美股）、**商品類別**（股票／期貨／選擇權）、**掛牌板別**（上市／上櫃／興櫃）。

這不是純粹的用詞潔癖：`docs/backtest/multi-market-engine.md` 的「市場（股票／期貨）」
與美股規劃的「多市場……台股歸位 `tw/`」指的不是同一條軸，而後者相依於前者——
**同一個詞在相依的兩份文件裡語意不同**。程式碼側的表現則是
`BaseStrategy.market` 與 `core/backtest/factory.py` 的單一分派鍵：實作類別早已是
`TwStockSpec`、`TwStockFillModel` 這種「地區 ＋ 商品」的組合，策略卻只宣告一個值，
是用一維鍵去選二維實作——能運作純粹因為當時市場只有台股一個。

## 四條軸

| 軸 | 名稱 | 值 | 語意 | 定義位置 |
|:--:|------|------|------|----------|
| A | `Market` | `TW`／`US` | 地區／交易市場。交易日曆、開盤時間、幣別屬於此軸 | `core/utils/constant.py` |
| B | `InstrumentType` | `STOCK`／`FUTURE`／`OPTION` | 商品類別。契約乘數、報價單位、結算規則屬於此軸 | `core/utils/constant.py` |
| C | `ListingBoard` | `sii`／`otc`／`rotc`／`pub`／`all` | 掛牌板別（上市／上櫃／興櫃／公開發行）。僅台股適用，值即公開資訊觀測站的 `TYPEK` 參數 | `core/pipeline/utils/constant.py` |
| D | `IssuerOrigin` | `DOMESTIC = "0"`／`FOREIGN = "1"` | 發行人國別（國內／國外企業，即 F 股／KY 股）。值即月營收頁 URL 末碼 | `core/pipeline/utils/constant.py` |

**軸 A 命名為 `Market` 的依據**：對齊 Lean 的 `Market`（`usa`／`tw`）與已定案的
`tw_stock`／`tw_futures`／`us_*` 目錄；並且讓現有的 `MarketCalendar` 與
`check_stock_market_open()` 一次變成正確用法——交易日曆與開盤時間本來就是地區屬性。

**軸 B 命名為 `InstrumentType` 的依據**：專案自己的 `InstrumentSpec`
（`core/backtest/models/instrument_spec.py`，docstring 即「商品規格」）已經用了這個詞，
只是沒推廣；亦對齊 Nautilus 的 `Instrument`。

**軸 C 與軸 D 為何要拆**：兩者原本合併在同一個 `MarketType` 裡（`SII0`／`SII1`／`OTC0`／`OTC1`）。
`SII0 = "0"` 與 `OTC0 = "0"` 值相同，Python Enum 會讓後者成為前者的 **alias**——
`MarketType.OTC0 is MarketType.SII0` 為 `True`，清單裡實際存的是 `SII0` 物件。
當時沒有行為錯誤（送進 URL 的是 `.value`，TWSE／TPEX 由呼叫端另外區分），
但任何人日後想用 `is` 判斷就會踩空。拆成兩條軸後 alias 自然消失。

## 落地位置

| 位置 | 用法 |
|------|------|
| `core/strategies/base.py` | `self.market: Optional[Market]` ＋ `self.instrument_type: Optional[InstrumentType]`，兩個欄位由各商品類別的策略基底填入 |
| `core/strategies/stock/base.py` | `Market.TW` ＋ `InstrumentType.STOCK`；**個別策略不需自己設** |
| `core/backtest/factory.py` | 分派鍵為 `(strategy.market, strategy.instrument_type)`；未支援的組合拋出含兩軸值的 `ValueError` |
| `core/backtest/models/` | 實作類別命名即「地區 ＋ 商品」：`TwStockSpec`、`TwStockFillModel`、`TwStockSettlementModel`、`TwFuturesSpec` |
| `core/api/`、`core/adapters/`、`core/backtest/datafeed/`、`core/dao/` | **目錄只承載市場一條軸**（`tw/`），商品類別由檔名承載（`stock_price_api.py` vs `futures_price_api.py`）；類別名仍是「地區 ＋ 商品」（`TwStockDataFeed`）。2026-09-02 由台期貨規劃 Phase5-3 收斂；`core/dao/` 於 2026-09-16 新增時沿用（`base.py`、`connection.py` 是市場無關底座，DAO 放 `tw/`），並登記在 `check_layer_deps.py` 的 `_MARKET_AXIS_PACKAGES` |
| `core/pipeline/{shared,tw,utils}/` | 目錄**只承載軸 A**（`tw/`，未來 `us/`）；商品類別由檔名承載（`stock_price_crawler.py` vs `futures_price_crawler.py`）。base 類別放 `shared/`，否則 `us/` 會反過來相依 `tw/`。**`utils/` 是層不是軸**，只放跨市場通用的工具；綁定單一市場的工具歸 `tw/utils/`（2026-09-13 收斂，見下）|
| `core/strategies/`、`core/models/`、`core/managers/` | 子目錄承載**軸 B**（`base/` ＋ `stock/` ＋ `futures/`，2026-09-01 起）。`strategy_loader` 逐一掃描這些子套件，新增商品類別不需改程式 |
| `data/db/` | 檔名帶軸 A：`tw_stock.db`、`tw_futures.db`（常數 `TW_STOCK_DB_PATH`／`TW_FUTURES_DB_PATH`） |
| `core/pipeline/tw/crawlers/financial_statement_crawler.py` | `self.listing_boards`（軸 C） |
| `core/pipeline/tw/crawlers/monthly_revenue_report_crawler.py` | `self.issuer_origins`（軸 D）；TWSE／TPEX 的區分由呼叫端各自的迴圈決定，不是清單內容 |
| `core/pipeline/tw/utils/url_manager.py` | URL 樣板佔位符 `{issuer_origin}` |
| `futures_stock_universe.underlying_listing_board` 欄 | 存的是「上市／上櫃」，屬軸 C |

### 新增一個（市場, 商品）組合

回測側要動哪些掛點，見
[多市場回測引擎架構](../backtest/multi-market-engine.md) §四「新增一個（市場, 商品）組合要做什麼」。
策略側只需在該商品類別的策略基底填入兩個欄位，`factory` 即可分派。

## 防迴歸護欄

`tests/test_naming_axes.py` 共 6 條，釘住四條軸的值域與 alias：

- 軸 A 與軸 B 的值**不得有交集**——有人想把 `STOCK` 加回 `Market` 會當場失敗。
- 軸 C 與軸 D 的值不得有交集。
- 每條軸的「成員數 ＝ 值數」，即 alias 偵測（就是上面 `SII0`／`OTC0` 那個坑）。

**軸之間的值一旦出現交集，就代表又有人把兩條軸壓回同一個名字裡**，
那是靜默的語意汙染，不會有任何執行期徵兆，只能靠測試擋。

## 已定案的取捨

### `futures_` 表名前綴維持不變

期貨表名帶 `futures_` 前綴（`futures_price_daily` 等）、台股表名不帶（`price`、`chip`），
這是 2026-08-22 的**刻意決策**：股票期貨除權息需要與 `tw_stock.db` 對照（可能走 `ATTACH`），
屆時帶前綴的表名在查詢裡不會混淆；PostgreSQL 遷移的目標是**單一** `alphaedge` 資料庫，
兩個 SQLite 檔會併進同一個扁平命名空間，前綴更是必要而非多餘。

**真正的不對稱在另一側**：缺前綴的是 `tw_stock.db` 的 `price`／`chip`／`margin`。
要對稱應該是補上 `stock_` 前綴，但那要改 13 張表、2.3 GB 資料與所有 API，
且 PostgreSQL 遷移本來就會重寫這一層——歸 `backlog/PostgreSQL遷移計畫.md`。

### 每層目錄只承載一條軸

`core/pipeline/` 的收斂曾有兩種提案：`pipeline/tw/` ＋ `pipeline/us/`（純市場軸），
或 `pipeline/tw_stock/` ＋ `pipeline/tw_futures/`（市場 ＋ 商品壓成單一目錄名）。
**採前者**——每層目錄只承載一條軸，商品類別由檔名承載，與本文件的整體原則一致。

### `utils/` 是層，不是軸（2026-09-13）

`core/pipeline/utils/` 與 `shared/` 同屬跨市場共用層，但當時裡面混了兩支**只有台股
用得到**的檔案：`url_manager.py`（整張表都是 TWSE／TPEX／MOPS／TAIFEX 端點）與
`stock_tick_utils.py`（Shioaji 金鑰、metadata 的鍵是台股代號）。兩支已搬到
`core/pipeline/tw/utils/`。

**為什麼不在 `utils/` 底下再開 `tw/`／`us/`**：那會讓同一層同時承載「層」與「軸」，
正是本節要避免的事。`utils/` 只留通用工具（`data_utils.py`、`exceptions.py`、`constant.py`），
綁定單一市場的一律往該市場目錄放。原本的 `sqlite_utils.py` 已於 2026-09-16 隨資料存取層收進 `core/dao/`。

**這次刻意不動 `constant.py`**：裡面的 `ListingBoard`／`IssuerOrigin` 確實只有台股適用，
但欄位 Enum 的去向另有定案——**欄位 Enum 是資料表 schema 的一部分**：`PriceColumn`／`ChipColumn`／
`FuturesPriceColumn` 已於 2026-09-16 下沉到 `core/config/schema.py`（`core/dao`、`core/api` 都要用，
不能留在 pipeline）；`ListingBoard`／`IssuerOrigin` 是爬蟲參數，仍留在 `constant.py`。

**同層的 `shared/payload.py` 也已收斂（2026-09-13）**：原本以為只有 `TYPEK` 是公開資訊
觀測站專屬、其餘欄位是通用 HTTP payload，故暫緩。**這個前提不成立**——`firstin`／`step`／
`co_id`／民國年 `year`／`season` 全是公開資訊觀測站的表單參數，整個 dataclass 沒有通用欄位，
且全專案只有 `financial_statement_crawler.py` 一支使用。已搬到
`core/pipeline/tw/utils/mops_payload.py`，檔名直接承載來源。

盤點 `shared/` 時看到、但**刻意不搬**的兩處台股痕跡：

- `BaseDataCrawler.NO_DATA_MARKERS` 是 TWSE／TPEX 的「查無資料」措辭——它是 class 屬性，
  未來 `us/` 的子類直接覆寫即可，不構成反向相依。
- `core.dao.base.create_symbol_date_index()`（原 `BaseDataLoader` 的同名方法）寫死 `stock_id` 欄——
  跟著下方遺留表的 `stock_id` → `symbol` 改名一起處理。

## 遺留與後續

| 項目 | 現況 | 歸屬 |
|------|------|------|
| `data/downloads/` | 仍是 `tw_stock/`／`tw_futures/` 的壓縮形式。純目錄名的話 `tw/stock/` 才與程式碼側同構，但那是第二次資料搬遷，不值得為一致性單獨做 | 待 PostgreSQL 遷移或下次動 `downloads/` 時順手收斂 |
| 資料表欄位語言 | **已於 2026-09-01 定案**：跟著**資料來源**走，不跟市場走——交易所網頁／檔案爬來的保留中文欄，API 來源用其原始英文欄。分界線本來就是來源（15 張表中已有 5 張全英文） | 規則與理由見 [ETL 入庫約定 §3.4](../pipeline/etl-ingestion.md) |
| `core/utils/instrument.py` 的 `StockUtils` 歸屬 | 未動 | 取捨表在 [多市場回測引擎架構](../backtest/multi-market-engine.md) |
| `stock_id` → `symbol` 的資料層改名 | 未動，本次刻意不做 | 歸 `backlog/PostgreSQL遷移計畫.md` 的 schema 批次 |

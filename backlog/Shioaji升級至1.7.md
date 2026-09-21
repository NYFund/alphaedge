# Shioaji 升級至 1.7

## Abstract

- **背景／問題**：本機 `.venv` 與鎖定檔的 shioaji 是 **1.3.3**，PyPI 最新為 **1.7.5**（2026-09-21 查詢），中間跨了 1.5、1.7 兩個系列。實盤下單（[實盤下單架構規劃.md](實盤下單架構規劃.md)）已完成的步驟中，有一批結論是**讀 1.3.3 原始碼或以 1.3.3 實測得出的**，例如 `MultiContract.__getitem__` 查不到回 `None`、`StockOrderCond` 沒有 `SBLShort`、`login()` 的 `receive_window`。版本越晚升，要重新驗證的結論越多；而借券（Phase6-1）在 1.3.3 下根本送不出去。
- **目標**：shioaji 升到 1.7.x 最新版，`core/` 所有用到 shioaji 的路徑（`core/broker/tw/`、`core/utils/` 的回呼與常數、`core/pipeline/tw/` 的 tick 爬取）都在新版下驗證過；實盤文件中依賴 1.3.3 行為的結論逐條重驗並改寫。
- **範圍界線（不做）**：
  - **不借升版之便加新功能**：新版的新能力（Contract V2 的 lazy lookup／update events、即時 KBar、市場訊號等）若值得採用，記在本文件末尾另案處理，不在升版時順手接上。
  - **不重寫實盤架構**：只修因 API 改變而失效的地方，`BaseBroker` 介面維持不變。
  - **不升級其他套件**：只以 `uv lock --upgrade-package shioaji` 升 shioaji 與它強制要求的傳遞相依，其他套件版本不動。
- **驗收標準**：
  1. `uv.lock` 的 shioaji 為 1.7.x，`pyproject.toml` 的下限同步提高。
  2. `uv run pytest`（含 `slow`）全綠，包含 `tests/live/` 與 `tests/test_order_state_parity.py`。
  3. 模擬環境實連：`manual_shioaji_login.py` 登入成功、`manual_shioaji_test_order.py` 股票委託送出並收到回報、`manual_shioaji_quote_record.py` 錄到的新行情能以 `core/broker/tw/quote_replay.py` 重放。
  4. S2 的重驗清單每一條都有新版結論，並已回寫到實盤文件。

---

## 進度追蹤表

| 編號 | 步驟名稱 | 產出檔案 | 驗證方式 | 狀態 | 備註／中斷點 |
|------|----------|----------|----------|:----:|--------------|
| S1 | 1.3.3 → 1.7.5 差異盤點 | 本文件〈差異盤點〉章節 | 差異表涵蓋 `core/` 每個用到的 shioaji 符號 | ✅ | 2026-09-21：23 項差異；試升後 24 failed、15 errors，集中在 4 個測試檔。**影響比預估大**，但都收得進 `core/broker/tw/`、`core/utils/` 與測試 |
| S2 | 依賴 1.3.3 行為的結論逐條重驗 | 本文件〈重驗清單〉章節 | 清單每一條都有 1.7.5 下的結論與佐證（原始碼位置或實測輸出） | 🔄 | 2026-09-21：12 列中 7 列已有離線結論，其餘 5 列待 S5 實連 |
| S3 | 升級鎖定版本並修正程式 | `pyproject.toml`、`uv.lock`、`core/broker/tw/*`、`core/utils/callback.py`、`core/utils/constant.py`、相關測試 | `uv run pytest -m "not slow"` 全綠 | ⬜ | 相依 S2；`tests/test_order_state_parity.py` 預期會先紅 |
| S4 | 離線驗證：測試全套與行情重放 | 無（驗證步驟） | `uv run pytest` 含 `slow` 全綠；1.3.3 時期錄的行情檔可重放，或不能重放的原因已記錄 | ⬜ | 相依 S3 |
| S5 | 模擬環境實連驗證 | 本文件實測紀錄 | 驗收標準第 3 條 | ⬜ | 相依 S4；**必須在交易日盤中**跑 |
| S6 | tick 爬取路徑驗證 | `core/pipeline/tw/crawlers/*`（如需修正） | `scripts/manual/manual_tick_crawler.py` 抓得到一檔股票與一檔期貨的 tick，欄位與舊版一致 | ⬜ | 相依 S3；可與 S5 同一天做 |
| S7 | 回寫實盤文件並解除借券阻塞 | `backlog/實盤下單架構規劃.md`、`backlog/index.md` | 實盤文件不再有「鎖定版 1.3.3」的過時敘述；Phase6-1 的借券前提更新 | ⬜ | 相依 S2、S5 |

---

## 步驟詳述

### S1. 1.3.3 → 1.7.5 差異盤點 ✅

- **目的**：升版前先知道哪些東西會變，而不是升完看測試哪裡紅。測試只覆蓋得到有寫測試的路徑，回呼、行情推播這類要實連才會跑到的地方，紅燈不會出現。
- **做法**：
  - 列出 repo 內所有 import shioaji 的檔案與用到的符號（`grep -rn "shioaji\|sj\." core scripts tests run.py`），整理成「符號 → 使用位置」表。目前涉及 `core/broker/tw/` 的六個模組、`core/utils/`（`account.py`、`callback.py`、`constant.py`、`instrument.py`、`order.py`）、`core/pipeline/tw/` 的 tick 爬取與更新、`core/live/factory.py`、`core/dao/tw/live_trade_dao.py`、`core/config/settings.py`、`run.py`。
  - 取得兩個版本的原始碼並排比對：`uv venv <scratch>/sj133 && uv pip install --python <scratch>/sj133 shioaji==1.3.3`，1.7.5 同理。只比上表用到的符號：類別與欄位（`Contract`、`StockOrder`／`FuturesOrder`、`stream_data_type`）、常數 Enum（`OrderState`、`StockOrderCond` 等）、方法簽章（`login`、`place_order`、`update_status`、`quote.subscribe`、`ticks`）。
  - 查閱官方 release notes／changelog，補上原始碼比對看不出的行為變動（例如預設值、伺服器端規則）。
  - **使用 Shioaji Claude Code plugin**（`claude plugin install shioaji`，已安裝）：它的 skill 涵蓋新版 API 用法、Contract V2 lazy lookup、migration 與 troubleshooting，適合回答「1.7.5 的正確寫法是什麼」。**它描述的是最新版，不會告訴你 1.3 與 1.7 之間改了什麼**，所以差異本身仍以原始碼比對與 changelog 為準，plugin 用來確認新寫法。
  - 已知的一項：`OrderState` 在 1.7 起改由原生模組提供、不再是 Python Enum（`tests/test_order_state_parity.py` 與 CI 註解已記錄），會影響 `core/utils/callback.py` 的 `order_cb` 比較前提。
- **產出**：本文件新增〈差異盤點〉章節（符號、1.3.3 行為、1.7.5 行為、影響位置、處置）。
- **驗證方式**：「符號 → 使用位置」表中每個符號都在差異表出現，註明「無變動」或具體變動。
- **相依**：[改用uv管理套件.md](改用uv管理套件.md) S3（以 uv 建立對照環境並用 `uv lock --upgrade-package` 升版）。

> **✅ 完成紀錄（2026-09-21）**
>
> **盤點方式**：以 `uv venv` 建立 1.3.3 與 1.7.5 兩套對照環境，用 introspection 比對 Enum 成員與值、方法簽章、model 欄位；1.7.5 核心是編譯後的原生模組（`_core.abi3.so`），原始碼無法直接比對，改讀它附帶的型別檔 `_core.pyi`（2,952 行）。另外參考官方 release notes 與 Shioaji plugin 的 `STREAMING.md`。「符號 → 使用位置」表由另一個 agent 逐檔讀完 `core/`、`scripts/manual/`、`tests/live/` 後整理而成。最後開一個獨立 worktree 把鎖定版本實際升到 1.7.5，跑完整測試，作為最直接的佐證。
>
> **試升結果**：`import` 全部成功（舊的子模組路徑有相容層），整個專案可以載入；`uv run pytest` 為 **24 failed、15 errors**，全部集中在 `tests/test_order_state_parity.py`、`tests/live/test_shioaji_order_mapper.py`、`tests/live/test_quote_replay.py`、`tests/live/test_shioaji_broker.py` 四個檔案。`uv lock --upgrade-package shioaji` 只動到 shioaji 本身，並移除 11 個它不再需要的相依（`pysolace`、`pynacl`、`sentry-sdk`、`msgpack` 等），其他套件版本不變。
>
> **整體結論**：1.5.0 起以 Rust 重寫（release notes：「refactor: rust version shioaji」），1.7.0 重構合約 API。這次升版影響的範圍**比立項時預估的大**：除了已知的 `OrderState`，還有 `login()` 參數、Enum 行為、`Trade` 結構、合約容器、行情重放、帳務欄位等。不過 `BaseBroker` 介面不需要改，所有修正都能收在 `core/broker/tw/`、`core/utils/` 與測試裡，仍在本文件的範圍內。

#### 差異盤點

依影響程度排列。「實測」＝在 1.7.5 環境實際執行；「型別檔」＝讀 `_core.pyi`；「待 S5」＝要登入或盤中才能確認。

| # | 符號 | 1.3.3 | 1.7.5 | 影響位置 | 處置 |
|---|------|-------|-------|----------|------|
| 1 | `Shioaji.login()` 參數 | 有 `fetch_contract`、`contracts_timeout`、`contracts_cb` | **三者都移除**，多了 `force_refresh`；傳 `contracts_timeout=` 會拋 `TypeError`（實測） | `core/broker/tw/shioaji_session.py:181-186`、`core/utils/account.py:29-33` | S3 必修：拿掉參數。原本用 `contracts_timeout` 等合約下載，新版合約由 shioaji 自行管理（1.7.0），等待方式在 S5 決定 |
| 2 | 套件結構 | `shioaji.constant`、`.order`、`.contracts`、`.data`、`.position`、`.account`、`.stream_data_type` 是真的模組 | 全部類別都在頂層 `sj.X`；舊子模組只剩相容層，每次取用都發 `DeprecationWarning` | 12 個檔案的 `import shioaji.constant as sj_constant`、`from shioaji.order import ...` 等（見符號表） | S3：改用頂層 `sj.X`，不依賴已棄用的相容層 |
| 3 | Enum 型別 | Python `str` Enum：可 iterate、有 `.name`、依值呼叫回傳同一個成員物件 | 原生類別：**不能 iterate**；多數成員**沒有 `.name`**（`OrderState`、`QuoteType`、`SecurityType`、`OptionRight` 例外）；`Action("Buy") is Action.Buy` 為 **False**。成員仍是 `str`，`==` 字串值成立（實測） | `core/broker/tw/shioaji_order_mapper.py:390,394`（依值呼叫、iterate）；`tests/live/test_shioaji_order_mapper.py` 以 `is` 比對與讀 `.name`（:218,270,353,357,366,387,421）；`tests/test_order_state_parity.py` 整份 | S3：mapper 的成員檢查改為讀類別屬性；測試 `is` 改 `==`；parity 測試改以成員屬性逐一比對值，保證「值對不上會紅」不變 |
| 4 | `OrderState` | str Enum | 原生類別，仍是 `str`、有 `.name`，`==` 原值成立（實測） | `core/utils/callback.py:27`、`core/broker/tw/shioaji_execution_handler.py:122,124` | **比較前提仍然成立**，不需修程式；只改 parity 測試的判定方式（它目前以「不是 Python Enum」直接判紅） |
| 5 | Enum 值變動 | — | `QuoteType.BidAsk` 值 `'bidask'` → **`'bid_ask'`**；`SecurityType.Future` 改名 **`Futures`**（並新增 `Warrant`）；`Exchange` 新增 `TIM`；`ChangeType` 新增 `Dowm`（原文拼字）；`constant.Status` 改由 `sj.OrderStatus` 提供 | `core/utils/constant.py:217-220` 的 `QuoteType` 鏡像；parity 測試 | S3：專案鏡像的 `QuoteType.BIDASK` 值改為 `bid_ask`（要查錄製檔、紀錄庫有沒有存舊值）；`Status` 改指 `sj.OrderStatus` |
| 6 | `StockOrderCond` | `Cash`／`MarginTrading`／`ShortSelling` | 新增 **`SBLShort`**、**`SBLShortPriceExempt`**、`Netting`、`Emerging`（1.7.2：「SBL short order condition」） | `core/broker/tw/shioaji_order_mapper.py:373-397`、`core/utils/constant.py:238-241`、parity 測試的 ratchet 清單（:116-122） | S3：依 ratchet 原本的設計，把 `SBLShort` 從「shioaji 缺少」清單移除；mapper 借券改為照常轉換（試升時 `test_sbl_raises_and_does_not_fall_back` 已 DID NOT RAISE） |
| 7 | 帳號類別 | `StockAccount`、`FutureAccount` 兩個類別；`account_type="S"` 字串可建構 | 兩者都對應同一個 `sj.Account`；`account_type` 必須是 `sj.AccountType`，傳字串會 `TypeError`（實測；試升時 15 個 errors 都是這個） | `tests/live/test_shioaji_broker.py:80-87` 的 fake 帳號 | S3：測試改用 `sj.Account(account_type=sj.AccountType....)`；程式本身未以 `isinstance` 區分兩種帳號 |
| 8 | 委託 model（`StockOrder`／`FuturesOrder`） | pydantic：`custom_field` 限 6 字元、pattern `^[ -~]*$`；`account=None` 會被拒 | 原生類別：**不再驗證 `custom_field`**（7 字元、中文都能建構，實測）；`account=None` 接受；欄位名稱不變 | `core/broker/tw/shioaji_order_mapper.py:40-41,246-248,333-357` | 專案自己的 `custom_field` 檢查**成為唯一的本地防線**，必須保留；更新引用 pydantic 行為的註解 |
| 9 | `Trade` 結構 | `order: Order`、`contract: Contract`、`status: OrderStatus`（model） | `order: OrderResult`、`contract: ContractIdentifier`、`status: OrderStatusInfo`；`OrderStatusInfo` 的欄位名稱與舊的 `OrderStatus` model 相同（`status`、`deal_quantity`、`deals`、`msg`、`order_datetime`…）；`OrderResult` 仍有 `seqno`、`ordno`、`custom_field`（型別檔） | `core/broker/tw/shioaji_broker.py:383-396`、`core/utils/account.py:125-142` | 欄位名稱沿用，預期不需改；`trade.contract.code` 在 `ContractIdentifier` 下是否可用待 S5 |
| 10 | `Deal` | `seq`、`price`、`quantity`、`ts` | 新增 tz-aware 的 `datetime`（Asia/Taipei，由 `ts` 計算）（型別檔） | `core/utils/account.py:125-142` | 不影響現有讀取；記入〈另案處理〉 |
| 11 | 下單回呼 `msg` | `dict` | dict-like 的 `OrderEventDict`（有 `__getitem__`、`get`、`keys`），**不是 `dict`**；keys 與 1.3.3 相同（型別檔） | `core/broker/tw/shioaji_execution_handler.py:147-189` | S3：確認 parser 沒有 `isinstance(msg, dict)` 之類的判斷；實際推播內容待 S5 |
| 12 | 行情回呼簽章 | `(exchange, data)` | 建議 `(data)` 單參數；雙參數仍會自動偵測並相容，但發 `DeprecationWarning`（`STREAMING.md`） | `core/broker/tw/shioaji_quote_stream.py:82-86` | S3：改單參數 |
| 13 | `api.quote.*` | 行情 API 的正式入口 | 已棄用的代理，改用 `api.subscribe`／`api.unsubscribe`／`api.set_on_*_callback`（`shioaji/__init__.py`） | `core/broker/tw/shioaji_quote_stream.py:82-86,135-156` | S3：改呼叫 api 本身的方法 |
| 14 | `stream_data_type` 模組 | 有 `TickSTKv1` 等 dataclass 與 `__annotations__` | 只剩相容層：`vars()` 裡沒有類別，也沒有型別標註；行情物件是**不能建構**的原生類別（實測） | `core/broker/tw/quote_replay.py:26-33,86-97`、`scripts/manual/manual_shioaji_quote_record.py:73-77` | S3 必修：試升時 `test_quote_replay.py` 6 條全紅（datetime 沒被還原，停在字串）。型別表改由 repo 自己維護，依 1.3.3 錄製檔的格式（`Decimal`／`datetime`／`List[Decimal]`）寫死，不再從套件讀 |
| 15 | 行情物件欄位型別 | 價格 `Decimal`；`datetime` 為 naive（實測 2026-09-21） | 型別檔寫價格為 `str`、`pct_chg` 為 `Decimal`，官方文件寫「Decimal-like」，兩者矛盾；新增 `date`、`time` 欄位 | `core/broker/tw/shioaji_quote_stream.py:246-263,294-296` | **待 S5**：盤中錄一段新版行情，確認價格與 `datetime` 在 runtime 的實際型別，再決定轉換層要不要改 |
| 16 | 合約容器 | `MultiContract.__getitem__` 查不到回 `None`；有 `keys()`、`_code2contract` | `ContractCategory`／`ContractGroup`：`__getitem__` 回傳型別**不是 Optional**（可能拋例外）；有 `get()`；**沒有 `keys()`**，也沒有 `_code2contract`；登入前存取 `api.Contracts` 會拋 `AuthError`（實測） | `core/broker/tw/shioaji_contract_resolver.py:10-35,65,93,129-134,170-173,216-238`、`core/utils/instrument.py:38`、`core/backtest/datafeed/tw/market_calendar.py:124`、`core/pipeline/tw/crawlers/*_tick_crawler.py`、`scripts/manual/manual_shioaji_login.py:80` | S3：查詢一律改用 `.get(code)`，列分類改為 iterate 或文件化的 API；**查不到時的實際行為待 S5** |
| 17 | 帳務欄位 | `Settlement` 有 `t_money`、`t1_money`、`t2_money`；`SettlementV1` 有 `date`、`amount`、`T` | 型別檔：`Settlement` 只有 `t_money`、`t_day`；`SettlementV1` 只有 `date`、`amount`；`StockPosition`、`Margin`、`AccountBalance` 欄位不變 | `core/broker/tw/shioaji_account_query.py:73-77,232`、`core/utils/account.py:56,69` | **待 S5**：實際呼叫 `list_settlements`／`settlements` 確認欄位；若真的少了，T+1／T+2 交割款要改用 `settlements()` 的逐日列 |
| 18 | 回傳物件的 `__dict__`／`model_dump()` | pydantic，兩者皆可 | `MappingMixin`：`__dict__` 已棄用（每次回傳新的 dict 並發警告），改用 `.dict()`；沒有 `model_dump()` | `core/utils/account.py:56,69,84,94`、`core/broker/tw/shioaji_account_query.py:319-325` | S3：改用 `.dict()` |
| 19 | 預設逾時 | `place_order`、`update_status` 等為 5 秒 | 30 秒 | `core/broker/tw/shioaji_broker.py:205,269,297,316`（皆用預設值） | S3：明確傳入 `timeout`，不讓阻塞上限隨版本改變；值沿用 5 秒 |
| 20 | `Shioaji()` 建構參數 | `simulation`、`proxies`、`currency`、`vpn` | `simulation`、`proxy`、`vpn` | 專案只用 `simulation` | 無影響 |
| 21 | `activate_ca()` | 有 `store` 參數 | 移除 `store` | 專案未傳 `store` | 無影響 |
| 22 | `Ticks` | pydantic | `MappingMixin`（有 `keys()`、`__getitem__`），欄位名稱相同 | `core/pipeline/tw/crawlers/stock_tick_crawler.py:60-62`（`{**ticks}` 展開） | 理論上可用，**待 S6** 實際抓一天確認 |
| 23 | `sj.Shioaji` 作為型別 | Python class | 原生 class，PEP 604 的 `\|` 與 `isinstance` 都可用（實測） | `core/backtest/datafeed/tw/market_calendar.py:78,102` | 無影響 |

### S2. 依賴 1.3.3 行為的結論逐條重驗 🔄

- **目的**：實盤文件中有一批設計決定建立在 1.3.3 的行為上，行為變了，決定可能跟著失效，而且多半是安靜地失效。
- **做法**：以 1.7.5 逐條重驗下表（來源皆為 [實盤下單架構規劃.md](實盤下單架構規劃.md)），能離線查的讀原始碼，要連線的併入 S5：

  | 項目 | 1.3.3 的結論 | 出處 | 重驗方式 |
  |------|--------------|------|----------|
  | `OrderState` 型別 | 是 `str` Enum，`order_cb` 以 `==` 比對字串值 | Phase1-1、`tests/test_order_state_parity.py` | 讀原始碼；已知 1.7 改變 |
  | `StockOrderCond` 成員 | 只有 `Cash`／`MarginTrading`／`ShortSelling`，沒有 `SBLShort`／`SBLShortPriceExempt` | Phase1-1、Phase2-4、Phase6-1 | 讀原始碼 |
  | 其他下單參數 Enum 值 | 與專案自訂 Enum 字串值一致 | Phase1-1 | `tests/test_order_state_parity.py` |
  | `MultiContract.__getitem__` | 查不到回 `None`、不拋例外 | Phase2-3 | 讀原始碼；**Contract V2 可能改變查詢方式** |
  | 合約查詢鍵 | slot 以 `symbol` 命名、`_code2contract` 以 `code` 索引 | Phase2-3 | 讀原始碼 |
  | `api.Contracts.Stocks[...]` | 一層涵蓋上市與上櫃 | Phase2-3 | 讀原始碼 |
  | 合約檔下載阻塞 | `_block()` 最多等 30 秒，決定 `contracts_timeout` | Phase2-3 | 讀原始碼；lazy lookup 下可能不再整批下載 |
  | `login(receive_window=)` | 伺服器把關秒級時鐘偏差 | Phase2-2 | 讀簽章＋S5 實連 |
  | `Contract.update_date` | `'2026/09/21'` 斜線格式字串 | Phase0-1、Phase2-2 | S5 實連 |
  | `Contract` 的 10 個欄位 | `reference`／`limit_up`／`limit_down`／`update_date`／`day_trade`／`margin_trading_balance`／`short_selling_balance`／`unit`／`multiplier`／`underlying_code` 皆存在 | Phase2-2 | 讀 pydantic 欄位 |
  | `custom_field` 限制 | 最多 6 字元、pattern `^[ -~]*$` | Phase2-4 | 讀原始碼 |
  | 行情推播型別 | `stream_data_type` 的 `TickSTKv1`／`BidAskSTKv1`／`TickFOPv1`／`BidAskFOPv1` | Phase2-7、`core/broker/tw/quote_replay.py` | 讀原始碼＋S4 重放 |

  實盤文件中其他標註「實測」「讀原始碼」的段落若在 S1 發現相關，一併加入本表。
- **產出**：本文件〈重驗清單〉章節（上表加兩欄：1.7.5 結論、佐證）。
- **驗證方式**：每一列都填了 1.7.5 結論與佐證；要實連的列標明「待 S5」。
- **相依**：S1。

> **🔄 進度（2026-09-21）**：能離線確認的列都已填完；需要登入或盤中的列標為「待 S5」。
>
> | 項目 | 1.3.3 的結論 | 1.7.5 結論 | 佐證 |
> |------|--------------|------------|------|
> | `OrderState` 型別 | `str` Enum，`order_cb` 以 `==` 比對字串值 | **仍成立**：不再是 Python Enum，但成員是 `str`，`==` 原值為 True，也保留 `.name` | 1.7.5 環境實測 |
> | `StockOrderCond` 成員 | 沒有 `SBLShort`／`SBLShortPriceExempt` | **不成立**：兩者都有了，另外新增 `Netting`、`Emerging` | 實測；release notes 1.7.2 |
> | 其他下單參數 Enum 值 | 與專案 Enum 字串值一致 | **仍成立**：`Action`、`StockPriceType`、`FuturesPriceType`、`OrderType`、`StockOrderLot`、`FuturesOCType` 的值全部沒變；但 Enum 不能 iterate、多數沒有 `.name`、依值呼叫的 `is` 不成立 | 實測，差異盤點 #3 |
> | `MultiContract.__getitem__` | 查不到回 `None`、不拋例外 | **可能不成立**：新容器的 `__getitem__` 回傳型別不是 Optional，另有 `get()`。待 S5 實測查不存在的代號 | 型別檔 |
> | 合約查詢鍵 | slot 以 `symbol` 命名、`_code2contract` 以 `code` 索引 | **不成立**：`_code2contract` 不存在；`ContractGroup` 以 `__getattr__`／`__getitem__`／`get` 查詢。以 code 或 symbol 查的實際行為待 S5 | 型別檔 |
> | `api.Contracts.Stocks[...]` | 一層涵蓋上市與上櫃 | 待 S5 | — |
> | 合約檔下載阻塞 | `_block()` 最多等 30 秒，決定 `contracts_timeout` | **不成立**：`login()` 已沒有 `contracts_timeout`；1.7.0 起合約由 shioaji 自行管理，可另外呼叫 `fetch_contracts(contracts_timeout=)`。實際需不需要等待，待 S5 | 實測（`TypeError`）；型別檔；release notes 1.7.0 |
> | `login(receive_window=)` | 伺服器把關秒級時鐘偏差 | 參數仍在、預設仍為 30000；伺服器行為待 S5 | 型別檔 |
> | `Contract.update_date` | `'2026/09/21'` 斜線格式字串 | 型別仍是 `str`；實際格式待 S5 | 實測（可用斜線格式建構）；型別檔 |
> | `Contract` 的 10 個欄位 | 全部存在 | **仍成立**：10 個欄位都在；`unit` 型別變成 `float`（程式以 `int()` 轉換，不受影響） | 型別檔、實測 |
> | `custom_field` 限制 | 最多 6 字元、pattern `^[ -~]*$` | **本地已不驗證**：7 字元與中文都能建構；伺服器端是否仍限制，待 S5 | 實測 |
> | 行情推播型別 | `stream_data_type` 的 `TickSTKv1` 等 | 類別仍在（頂層 `sj.TickSTKv1`），但 `stream_data_type` 只剩相容層、沒有型別標註，重放器讀不到型別；價格的 runtime 型別待 S5 | 實測（`test_quote_replay.py` 6 條紅） |

### S3. 升級鎖定版本並修正程式 ⬜

- **目的**：套用升版，並修掉 S1、S2 找出的不相容處。
- **做法**：
  - `pyproject.toml` 的 `shioaji==1.3.3` 改回下限寫法 `shioaji>=1.7`，執行 `uv lock --upgrade-package shioaji`，確認 `uv.lock` 的 diff 只動到 shioaji 與它強制要求的傳遞相依。
  - 依差異表修正 `core/broker/tw/*`、`core/utils/callback.py`（`order_cb` 在 `OrderState` 不再是 Enum 下的比較方式）、`core/utils/constant.py`（下單參數 Enum 補上新版有的值，例如 `SBLShort`）。
  - `tests/test_order_state_parity.py` 依新版型別改寫比對方式：它的用途是讓「值對不上」的安靜失效變成紅燈，改寫後這個保證必須還在。
  - 修正時查新版寫法可使用 Shioaji plugin；寫法的根據仍要回到原始碼或實測，不以 plugin 的敘述當作唯一依據。
- **產出**：`pyproject.toml`、`uv.lock`、`core/broker/tw/*`、`core/utils/callback.py`、`core/utils/constant.py`、`tests/test_order_state_parity.py` 及其他因此調整的測試。
- **驗證方式**：`uv run pytest -m "not slow" -rs` 全綠；`uv run ruff check .` 全綠。
- **相依**：S2。

### S4. 離線驗證：測試全套與行情重放 ⬜

- **目的**：在不連線的前提下盡量把問題擋在實連之前。
- **做法**：
  - `uv run pytest -rs`（含 `slow`），`tests/live/` 229 條與 `./scripts/run_regression.sh` 雙線。
  - 以 `core/broker/tw/quote_replay.py` 重放 1.3.3 時期錄製的行情檔。重放器的欄位型別取自**安裝中的** `stream_data_type`，若新版改了欄位，舊錄製檔可能無法還原：能重放 → 轉換層在新版下一致；不能重放 → 記錄是哪個欄位變了，並決定舊錄製檔要轉換還是作廢。
- **產出**：無（驗證步驟）；結果寫在本步驟章節末。
- **驗證方式**：同上兩項。
- **相依**：S3。

### S5. 模擬環境實連驗證 ⬜

- **目的**：回呼、推播、委託回報這些路徑只有實連才會跑到，也是 S2 中要連線才能重驗的那幾列。
- **做法**（交易日盤中，模擬環境）：
  1. `uv run python -m scripts.manual.manual_shioaji_login`：登入、合約檔日期檢查、印出合約 10 個欄位的值與型別，與 1.3.3 時期的實測表對照。
  2. `uv run python -m scripts.manual.manual_shioaji_test_order --confirm`：送一筆股票 ROD 限價單（跌停價買進，不會成交），確認 `order_cb` 收到回報且狀態比對成立，再撤單。期貨權限若仍未開通，期貨那筆記為「待權限」。
  3. `uv run python -m scripts.manual.manual_shioaji_quote_record`：錄一段新版行情，再以 `quote_replay.py` 重放，確認訊號與即時跑的一致。
- **產出**：本步驟章節末的實測紀錄（日期、shioaji 版本、各腳本輸出重點）。
- **驗證方式**：驗收標準第 3 條。
- **相依**：S4；Phase0-1 的帳號權限（期貨部分）。

### S6. tick 爬取路徑驗證 ⬜

- **目的**：`core/pipeline/tw/crawlers/stock_tick_crawler.py`、`futures_tick_crawler.py` 與兩個 updater 用 shioaji 抓歷史 tick，是實盤以外另一條依賴 shioaji 的路徑，容易在升版時被忽略。
- **做法**：`uv run python -m scripts.manual.manual_tick_crawler` 抓一檔股票與一檔期貨的單日 tick，比對欄位名稱與型別是否與升版前的中繼檔一致；`shioaji.data.Ticks` 若有變動，修正爬蟲與清洗器。
- **產出**：如需修正，為 `core/pipeline/tw/crawlers/*` 與對應 cleaner。
- **驗證方式**：抓得到資料，欄位與舊版一致或差異已處理；`uv run pytest tests/test_futures_tick.py` 全綠。
- **相依**：S3。

### S7. 回寫實盤文件並解除借券阻塞 ⬜

- **目的**：讓實盤文件反映新版事實，避免之後有人照著 1.3.3 的結論寫程式。
- **做法**：
  - 依 S2 重驗清單，改寫 [實盤下單架構規劃.md](實盤下單架構規劃.md) 中各項結論的版本與內容，仍成立的補註「1.7.x 重驗仍成立（日期）」，不成立的改寫並說明影響。
  - Phase6-1 的「借券要先升 shioaji」前提依 S2 結論更新；Phase0-1 記錄的 shioaji 版本改為新版。
  - `backlog/index.md` 實盤那一列的相依欄同步更新。
- **產出**：`backlog/實盤下單架構規劃.md`、`backlog/index.md`。
- **驗證方式**：`grep -n "1\.3\.3" backlog/實盤下單架構規劃.md` 的每一處都已加上新版結論，或只留作歷史脈絡並註明。
- **相依**：S2、S5。

---

## 另案處理

升版途中發現、但依範圍界線不在本文件處理的項目記在這裡：新版的新能力（Contract V2 lazy lookup、即時 KBar 等），以及舊程式在新版下仍能運作、但寫法可以改善的地方。會讓程式失效的不相容一律在 S3 當場修，不記在這裡。

本文件與 [改用uv管理套件.md](改用uv管理套件.md) 都完成後，兩份文件的這個章節合併整理成一份新的 backlog 文件，再回頭更新 [架構重構與冗餘收斂.md](架構重構與冗餘收斂.md)（重點是 Phase1-3 的 `market_calendar.py` 與 Phase7-6「未裝 shioaji 的環境」驗收）。

1. `Deal` 新增 tz-aware 的 `datetime` 欄位：成交時間可以直接讀，不必再自己從 `ts` 換算（差異盤點 #10）。
2. 新版多了原生的非同步 API（`ShioajiAsync`）與 receiver 模式（`get_tick_stk_v1_receiver()`），可以取代「回呼丟進 queue」的寫法；這屬於架構變更，不在升版時處理。
3. `shioaji[speed]` extra：import 時會提示安裝以提升效能。在 1.3.3 就已存在，是否加上留到升版後評估（來源：`改用uv管理套件.md` 的後續改善章節）。

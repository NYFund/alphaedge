# 手動腳本（`scripts/manual/`）

這裡的腳本是**人工執行的檢查與探測工具，不是測試**。

## 為什麼從 `tests/` 搬過來

它們原本放在 `tests/`，但 pytest 只收集 `test_*.py`，所以**從來沒有被執行過**。
放在那裡造成三個問題：

1. **它們是「永遠不會失敗」型態的唯一來源**。9 支裡有 19 處 `return False`、
   7 檔 `except Exception` 把錯誤吞掉。稽核 `tests/` 的測試品質時，
   每次都要先把它們排除，否則統計數字全是假的。
2. **覆蓋率與 grep 統計被污染**。
3. **新人會以為它們是測試**，看到「測試」失敗卻沒有人管而困惑。

搬過來之後，`tests/` 底下每一支都是真的會被 pytest 跑的測試。

## 怎麼執行

一律在**專案根目錄**以 `-m` 執行：

```bash
.venv/bin/python -m scripts.manual.manual_db_tables
.venv/bin/python -m scripts.manual.manual_db_tables --broker-trading --limit 10
.venv/bin/python -m scripts.manual.manual_finmind_api
```

**必須用 `-m`**：`scripts` 不在 `pyproject` 的 `packages.find` 裡
（只裝 `core*`／`tasks*`／`tests*`），直接跑檔案路徑時 `sys.path[0]` 是腳本
自己的目錄，`import core.…` 會失敗。`-m` 會把工作目錄放進 `sys.path`。

> 舊版靠每支腳本開頭的 `sys.path.insert` 硬塞，那會遮蔽「沒安裝就跑」的
> import 錯誤（已清除）。

## 有哪些

| 腳本 | 用途 | 是否碰 production DB |
|------|------|:---:|
| `manual_db_tables.py` | 檢查 `tw_stock.db` 的 FinMind 參考表是否存在，`--broker-trading` 抽樣券商分點（查詢走 DAO、唯讀連線） | 唯讀 |
| `manual_finmind_api.py` | 逐一呼叫 `FinMindAPI` 的每個方法 | 唯讀 |
| `manual_init_tick_metadata.py` | 初始化 tick metadata | 寫入 |
| `manual_tick_crawler.py` | tick 爬蟲的手動驗證（需 Shioaji 金鑰） | — |
| `manual_tick_updater.py` | tick updater 的手動驗證（需 DolphinDB） | 寫入 |

**已刪除的腳本**（2026-09-16，DAO 資料存取層收斂時）：

- `manual_broker_trading_db_query.py`：與 `manual_db_tables.py --broker-trading` 重複。
- `manual_broker_trading_updater.py`、`manual_finmind_pipeline.py`、`manual_finmind_updater.py`：以 mock 改寫
  `core.config` 後自己開臨時 SQLite 驗證，且已跟不上現行介面（例如呼叫已不存在的
  `get_actual_update_start_date()`）。同樣的行為已由真的會跑的測試守著：
  `tests/test_finmind_broker_trading_batch.py`、`tests/test_dao_finmind.py`、
  `tests/test_finmind_reference_table_loader.py`、`tests/test_finmind_loader_broker_trading.py`。

⚠️ **會寫入的那幾支請先確認沒有背景回補在跑**。同一個 SQLite 檔同時被兩個
行程寫入會互相搶鎖；MOPS 與 FinMind 另有各自的節流，同時跑兩支爬蟲會讓
兩邊都變慢甚至整段逾時。

## 想把某一支變成真的測試

把「需要 production DB」的部分換成 in-memory SQLite 的樣本，移到 `tests/`
並改名為 `test_*.py`。`tests/test_finmind_api.py` 就是這樣從
`manual_finmind_api.py` 長出來的（覆蓋率由 0% 升到 98%）。

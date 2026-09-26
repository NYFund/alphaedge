# 手動腳本（`scripts/manual/`）

這裡的腳本是**人工執行的檢查與探測工具，不是測試**。

## 為什麼從 `tests/` 搬過來

它們原本放在 `tests/`，但 pytest 只收集 `test_*.py`，所以**從來沒有被執行過**。
放在那裡造成三個問題：

1. **它們是「永遠不會失敗」型態的唯一來源**：出錯一律吞掉並印訊息，
   靠人看輸出判斷，沒有任何斷言。稽核 `tests/` 的測試品質時每次都要先把它們
   排除，否則統計數字全是假的。**這裡不記處數**——腳本增減時數字不會跟著改，
   要看實況就 `grep -c "except Exception" scripts/manual/*.py`。
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
| `manual_fix_tpex_dealer_totals.py` | 一次性修正上櫃 2014-12-01 ~ 2018-01-14 的自營商買進／賣出合計欄（清洗端漏加拆分欄，庫裡留下「買賣皆 0、買賣超非 0」）。預設只統計；`--apply` 先把受影響列的舊值匯出成 CSV 再更新，驗證不過整批回滾 | 寫入（需 `--apply`） |
| `../live_watchdog.py` | 實盤存活監控（**不在本目錄**，是排程用的獨立行程）：比對段落表與 `live_run`，發現「該跑而沒有紀錄」或「跑到一半死掉」就推播；唯讀開啟紀錄庫、不連券商 | 唯讀 |
| `manual_shioaji_login.py` | `ShioajiSession` 的模擬環境冒煙：登入、列出帳號、核對合約欄位實際值與型別、列出期貨分類代碼、登出（**不下單**，也不提供連正式環境的選項） | — |
| `manual_shioaji_test_order.py` | 永豐 API 測試用的委託：模擬環境各送一筆股票與期貨 ROD 限價單，**價格取合約的 `limit_down`**（保證在漲跌停內且不會成交）。要帶 `--confirm` 才真的送單，`--cancel` 可順手撤掉。金鑰只讀 `.env`，不接受命令列傳入 | — |
| `manual_shioaji_quote_record.py` | 盤中行情錄製：訂閱逐筆與委買賣、把原始回呼存成 JSONL 並印出**欄位名、型別與範例值**。**要在交易日盤中跑**；不下單、不連正式環境。輸出落 `data/records/`（已被 gitignore） | 寫入 |
| `manual_tick_crawler.py` | tick 爬蟲的手動驗證（需 Shioaji 金鑰） | — |
| `manual_tick_updater.py` | tick updater 的手動驗證（需 DolphinDB） | 寫入 |
| `manual_corporate_action_detect.py` | 公司行動偵測與人工補登：先列出「單日變動超過門檻、且無法由除權息或已知公司行動解釋」的候選（唯讀），逐筆判斷後補登。ETF 的受益權單位分割不在任何結構化端點裡，只能走這條路；**不帶 `--confirm` 只列計畫**，停止買賣前收盤價由腳本自己從 `price` 表取（不讓人手打） | 寫入（需 `--confirm`） |
| `manual_probe_pnl_fields.py` | 單日損益的來源欄位實際取得到什麼值？連模擬環境唯讀核對。單日虧損檢查（`RiskConfig.daily_loss_ratio`）一律以券商端為準，所以要先確認期貨 `Margin` 與股票帳戶快照的各欄位語意 | — |
| `manual_verify_contract_probe.py` | 期貨合約有沒有 `update_date`？連模擬環境唯讀核對。實盤的交易日判定在平日只剩「券商合約檔更新日」一個佐證，而**必須問期貨合約**——拿股票合約去問，兩個市場開休市不一致的那天會誤判為開市 | — |
| `manual_verify_quota_basis.py` | 資金額度的計算基準與券商實際權益對不對得上？連模擬環境唯讀核對 `check_quota_against_equity()` 與 `CAPITAL_SAFETY_RATIO` 的實際效果 | — |
| `../check_overnight_positions.py` | 模擬環境是否保留隔夜部位？（**不在本目錄**）唯讀分析 `live_position_snapshot`：模擬環境若每晚清倉，從第 2 天起本地歸屬帳與券商部位必然不一致，多日演練建立在隔夜部位上的驗收項目就達不到。不連券商、不寫任何東西 | 唯讀 |

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

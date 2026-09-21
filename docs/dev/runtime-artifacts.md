# 執行期產物與原始碼的分界

> 本文件定義「程式寫出來的東西該放哪」，以及判斷一個檔案屬於**設定**還是**產物**的準則。

---

## 分界只有一條

**`core/` 是被讀的，`data/`／`results/`／`logs/` 是被寫的。**

```
AlphaEdge/
├── core/                  # 函式庫：可被 import，不寫任何東西到自己目錄下
├── run.py  tasks/  frontend/  strategy_lab/  tests/  docs/  backlog/  scripts/
│
├── data/                  # 資料
│   ├── db/                # tw_stock.db、tw_futures.db（市場軸由檔名承載）
│   └── downloads/         # ETL 中繼檔（市場軸由目錄承載）
│       ├── tw_stock/      # price, chip, margin, dividend, financial_statement,
│       │                  # monthly_revenue_report, tick, finmind, meta
│       └── tw_futures/    # price, chip, continuous, universe, margin, tick
│
├── results/               # 回測產出：只放要給人看的東西（CSV ＋ PNG）
│   └── <StrategyName>/
│
└── logs/
    ├── api/               # core/api/ 的查詢日誌
    ├── pipeline/          # 爬取／清洗／入庫
    └── backtest/          # 回測
```

三個根皆可由環境變數覆寫（容器掛載 volume 用）：
`ALPHAEDGE_DATA_DIR`／`ALPHAEDGE_RESULTS_DIR`／`ALPHAEDGE_LOGS_DIR`。

護欄在 [`tests/test_config_paths.py`](../../tests/test_config_paths.py)。

## 為什麼要有這條線

產物一旦住進 editable 安裝的套件裡，代價會攤在每一個工具的設定上：
`pyproject.toml` 的 `packages.find`、ruff 的 `extend-exclude`、coverage 的 `omit`
都得各維護一份排除清單。

**最能說明問題的是 `.gitignore`**：產物散在原始碼裡時，它沒辦法寫「忽略產物目錄」，
只能全 repo 封鎖 `*.csv`／`*.png`／`*.log`／`*.db`，於是正當的 CSV（回歸 baseline）
得靠負向例外撈回來。分界成立之後，`.gitignore` 的產物段落只剩三行目錄，
工具的排除清單也不再需要。

### 為什麼是三個根而不是一個

因為**備份策略在三者之間不同**：`data/db/` 弄丟是災難（部分資料表的歷史回補要跑幾十小時）、
`results/` 幾分鐘可重跑但想留歷史、`logs/` 隨時可刪。收成一個目錄會讓備份規則退化成
「備份 X 但排除 X/logs 與 X/downloads」。

### 為什麼日誌分三桶

依**產生者**分：

| 桶 | 呼叫端 | 性質 |
|----|--------|------|
| `api/` | `core/api/tw/` | 每次查詢都寫，量最大、純雜訊，可整桶刪（檔案 sink 只留 WARNING 以上） |
| `pipeline/` | `core/pipeline/tw/{updaters,crawlers}` | **會回頭讀**（回補的 `N requested / N no data / N unreachable` 統計行） |
| `backtest/` | `core/backtest/`、`core/utils/` | 單次回測的執行紀錄 |

只分兩桶不成立：`pipeline` 蓋不住 `stock_price_api.log` 這些來自 `core/api/` 的日誌，
而它們恰好是檔案數最多的一群。分開之後，「可以整桶刪掉的那一批」才被隔離出來。

每個 sink 都帶 `filter`，只收自己那一桶的套件發出的記錄——少了它，一次查詢會同時寫進三個桶的每一個檔案。

---

## 判準：這個檔案是設定還是產物？

新增任何會落地的檔案時，先用這張表判斷：

| | 設定 | 產物 |
|---|---|---|
| 誰產生 | **人**維護 | **程式**寫出 |
| 重跑會怎樣 | 不變 | 被覆寫 |
| 缺檔會怎樣 | 行為改變（可能靜默降級） | 重跑就有 |
| 該放哪 | `core/` 內，隨套件發佈 | `data/`／`results/`／`logs/` |
| 進版控嗎 | **要** | **不要** |

實例：

- **設定** — `core/pipeline/tw/cleaners/schema/**/*.json`（欄位對照表）。
  被 cleaner 讀取，**缺檔只會 warning 後靜默降級清洗**，所以一定要進版控。
- **產物** — `tick_metadata.json`、`broker_trading_metadata.json`（爬蟲 resume 狀態）。

> **package data 是 `core/` 內唯一正當的非程式碼檔案**：小、唯讀、隨套件發佈、
> 以 `importlib.resources` 或路徑常數讀取。判準是「唯讀 ＋ 隨套件發佈 ＋ 小」，
> 三條都要成立。

---

## `core/config/` 的三個模組

`core/config/` 是套件，門面維持 `from core.config import X`：

| 模組 | 內容 | 什麼時候會改 |
|------|------|--------------|
| `paths.py` | 原始碼路徑、產物三根與其下所有目錄 | 目錄搬遷時 |
| `schema.py` | 分庫檔名、完整路徑、資料表名稱 | 新增資料表時 |
| `settings.py` | 爬取範圍、預設區間、DolphinDB／Shioaji 憑證 | 調整營運參數時 |

新程式碼建議直接 import 子模組（`from core.config.paths import DATA_DIR_PATH`），語意較明確。

**門面刻意用 star import**：逐一列出會埋一個陷阱——日後在 `paths.py` 新增常數卻忘了
補進 `__init__.py`，`from core.config import NEW_PATH` 會以一個指不到原因的
ImportError 收場。

---

## 搬移目錄時要注意

1. **搬移路徑常數時不要改常數名。** 改 `base_dir`、不改名稱，改動就收斂在 `core/config/` 一處；
   改名會擴散到所有取用端。**任何地方都不要自行以字串拼 `downloads/` 路徑**，一律走常數。

2. **`Path(__file__).parent` 的層數跟著檔案位置走。**
   路徑錨點所在的檔案一旦多一層目錄，同一行 `.parent` 就會指到錯的地方，
   **所有產物路徑會安靜地退回錯誤位置、不會有任何錯誤**。
   `tests/test_config_paths.py` 釘住錨點的絕對位置。

---

## 日誌保留

`LogManager.setup_logger()` 的 `retention` 預設 30 天，但 **loguru 的清理只在該 logger
再次被建立時才觸發**。一支跑完就不再執行的 crawler，它的舊檔會永遠留著。

問題出在「不會被觸發」而不是「保留太久」，故**不要調 retention**，改用與 logger 生命週期
無關的獨立進入點：

```bash
python -m tasks.clean_logs                     # 預覽（不刪）
python -m tasks.clean_logs --apply             # 實際刪除，預設保留 30 天
python -m tasks.clean_logs --apply --bucket api --days 7
```

它**只刪已輪替的檔**（檔名帶時間戳）；當前使用中的 `xxx.log` 一律保留——刪掉正在被 loguru
寫入的檔案，該 handler 會繼續寫進一個已不存在的 inode，日誌就此靜默消失。

### 日誌檔被外部刪掉之後

`clean_logs` 的自我約束保護不了手動的 `rm -rf logs`。為此 `setup_logger()` 的
`logger.add()` 帶 `watch=True`（loguru ≥ 0.7.0）：檔案被刪除或被外部程式取代時，
**下一筆記錄會重新建立它**（含缺少的父目錄）。

⚠️ **已經寫進舊 inode 的內容救不回來**，這個參數保證的是「之後不再繼續消失」。
故操作上仍然成立：**長跑的 ETL 進行中不要動 `logs/`**；要動之前，
先 `ps aux | grep -E "backfill|update_db"` 確認沒有東西在跑。

---

## 相關文件

- [命名軸線](naming-axes.md)：`core/` 內部的市場軸與商品軸分層
- [PostgreSQL 遷移計畫](../../backlog/PostgreSQL遷移計畫.md)：若日後遷移完成，
  `data/db/` 會整個消失，`data/` 只剩 `downloads/`；本結構承受得住，不需為此預留

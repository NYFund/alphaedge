# 正式環境部署（Prod Deployment）

本專案以「分角色容器化」部署：

- `core/Dockerfile`：回測／策略執行
- `frontend/Dockerfile`：前端（Streamlit，相依列在 `frontend/requirements.txt`）

容器映像**不含資料庫**，資料一律由主機掛入。

## 1) 建立映像

在專案根目錄執行：

```bash
docker build -f core/Dockerfile -t alphaedge-core .
docker build -f frontend/Dockerfile -t alphaedge-frontend .
```

### 映像裡有什麼、為什麼

| 項目 | 做法 | 為什麼 |
|------|------|--------|
| core 的 Python 相依 | 先照 `requirements.txt` 的鎖定版本裝第三方套件，再 `pip install -e .` 裝本專案 | 與本機、CI 同一種安裝方式；只改原始碼時第一層仍吃快取 |
| editable 安裝 | 必須是 `-e`，不可一般安裝 | `core/config/paths.py` 以 `__file__` 推算專案根目錄，一般安裝會把程式複製進 site-packages，`results/`、`logs/` 就不會落在 `/app` 底下的掛載點 |
| core 內含 `chromium` | apt 安裝並設 `BROWSER_PATH=/usr/bin/chromium` | 回測報表存 PNG 走 plotly → kaleido 1.x，它不內建瀏覽器，缺了會在報表最後一步失敗（CSV 已寫出、圖全沒有）。`plotly_get_chrome` 下載的 Chrome for Testing 沒有 linux arm64 版本，故用 Debian 套件 |
| frontend 相依 | 只裝 `frontend/requirements.txt` | 前端映像不安裝本專案，只 COPY `risk_metrics.py` 那條最小鏈 |
| frontend 的 `PYTHONPATH=/app` | 寫在 `frontend/Dockerfile` 的 `ENV` | 前端映像不安裝本專案，而 `streamlit run frontend/app.py` 只把 `/app/frontend` 放進 `sys.path`：少了這一行，`from core.backtest.analysis...` 與 `from frontend.config ...` 都會 `ModuleNotFoundError`。本機不出事是因為開發環境有 editable 安裝 |
| `.dockerignore` | 排除 `data/`、`results/`、`logs/`、`.env*`、`.venv/`、`.git/` | build context 是專案根目錄，不排除會把數 GB 的資料庫與金鑰送進 daemon，日後有人寫 `COPY . .` 還會打包進映像 |

映像大小約 core 2.9 GB（其中 chromium 與 shioaji 佔大宗）、frontend 0.9 GB。

**版本下限不是隨手寫的**：`frontend/app.py` 用的是 `width="stretch"`，那是 Streamlit **1.49.0** 才加進
`st.dataframe`／`st.plotly_chart`／`st.image` 的，故 `frontend/requirements.txt` 的下限是 `streamlit>=1.49`。
下限低於它的話，映像可能解析到舊版而把新參數當成未知關鍵字直接 `TypeError`——**一開頁就壞，不是降級**。
本機不安裝 streamlit，沒有測試會實際渲染元件，改版面參數時請實際開一次前端確認。

## 2) 準備資料與環境檔

建議至少準備：

- `.env`（可由 `.env.example` 複製）
- `data/db/`（`tw_stock.db`、`tw_futures.db`）
- `data/downloads/`（只有要在容器內跑 `tasks.update_db` 時才需要）
- `results/`

## 3) 執行 core 容器（回測）

```bash
docker run --rm \
  --name alphaedge-core-run \
  -v "$(pwd)/data:/app/data:ro" \
  -v "$(pwd)/results:/app/results" \
  -v "$(pwd)/logs:/app/logs" \
  --env-file .env \
  alphaedge-core --strategy MomentumStrategy1
```

只跑回測時 `data/` 以唯讀掛載：主機上的 ETL 可能正在寫同一個資料庫檔。
要在容器內更新資料時才改為可寫掛載。

## 4) 執行前端容器（選用）

```bash
docker run --rm -d \
  --name alphaedge-frontend \
  -p 8501:8501 \
  -v "$(pwd)/results:/results:ro" \
  -e ALPHAEDGE_RESULTS_DIR=/results \
  alphaedge-frontend
```

`ALPHAEDGE_RESULTS_DIR` 與後端同名：後端寫哪、前端就讀哪。

## 5) Docker Compose

根目錄的 `docker-compose.yml` 一次起 `core` 與 `frontend`：

```bash
docker compose up --build
STRATEGY=MomentumFuturesStrategy docker compose up core   # 換策略
```

| 服務 | 掛載 | 說明 |
|------|------|------|
| `core` | `./data` → `/app/data`（唯讀）、`./logs` → `/app/logs`、具名 volume `alphaedge_results` → `/app/results` | 以 `MODE`／`STRATEGY` 環境變數決定要跑什麼 |
| `frontend` | 具名 volume `alphaedge_results` → `/results` | 開 `http://localhost:8501` |

回測結果放在**具名 volume** 而非主機的 `results/`，要在主機上看 CSV 請改用第 3、4 節的 `docker run`。

## 6) 建議的正式環境切分

- **資料更新節點**：定時執行 `python -m tasks.update_db ...`
- **回測節點**：執行 `run.py --strategy ...`，唯讀掛載資料庫
- **展示節點**：掛載唯讀的 `results` 給前端

## 7) 健康檢查與維運

```bash
# 查看容器
docker ps

# 查看 core 日誌
docker logs -f alphaedge-core-run

# 停止前端
docker stop alphaedge-frontend
```

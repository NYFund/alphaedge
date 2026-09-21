# AlphaEdge Frontend

此目錄提供 AlphaEdge 回測結果的 Streamlit 唯讀檢視器，股票與期貨報表共用同一個介面。

## 功能

- 側欄選擇 `results/<StrategyName>/` 策略資料夾
- **總覽**：策略摘要、關鍵指標、多空統計、事件計數；期貨報表（以報表欄位判斷）另外顯示保證金與口數曝險
  - 指標**一律讀 `<策略>_metrics_summary.csv`**（Sharpe／Sortino／MDD／波動度／獲利因子／勝敗比／IR），前端不自行計算任何公式，因此也完全不 import `core`
  - 該檔是新版回測才有的產出；舊結果資料夾會顯示 `N/A` 並提示重跑回測
- **交易明細**：可依股票代號／契約篩選
- **圖表**：互動圖（資產曲線、每日損益）
- **圖片**：reporter 輸出的 PNG（若存在）
- **下載**：CSV 與圖片檔

## 安裝

本機：在專案根目錄以 uv 補裝 frontend extra（開發時要保留 `dev` 就一起列出：`uv sync --extra dev --extra frontend`）：

```bash
uv sync --extra frontend
```

Docker 映像**不安裝本專案**：只 COPY `frontend/`（前端完全不 import `core`，績效指標一律讀
reporter 落地的 `<策略>_metrics_summary.csv`），相依一律來自 `frontend/requirements.txt`（streamlit、pandas、plotly）。

```bash
docker build -f frontend/Dockerfile -t alphaedge-frontend .
docker run --rm -p 8501:8501 -v "$(pwd)/results:/results:ro" alphaedge-frontend
```

## 啟動

在專案根目錄執行：

```bash
streamlit run frontend/app.py
```

啟動後在瀏覽器開啟 `http://localhost:8501`。

## 結果目錄設定

預設讀取專案根目錄的 `results/`（Docker 映像內為 `/results`），可透過環境變數覆寫：

```bash
export ALPHAEDGE_RESULTS_DIR=/your/custom/path
streamlit run frontend/app.py
```

舊變數名 `ALPHAEDGE_BACKTEST_RESULTS` 已更名為 `ALPHAEDGE_RESULTS_DIR`：目前仍相容（會發出 `DeprecationWarning`），下一版將移除。

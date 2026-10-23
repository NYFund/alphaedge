"""
回測報表檢視器（Streamlit）

只讀回測落地在 `results/` 的報表與圖檔，**不 import `core`**：績效指標一律讀 reporter
寫好的 `metrics_summary.csv`，前端自己不算，映像的相依面也只剩 `frontend/` 本身。
入口是 `frontend/app.py`（`streamlit run frontend/app.py`），讀檔與整理在 `frontend/services/`。
"""

"""
前端的報表讀取與整理

- `report_loader`：找出 `results/` 下的策略目錄，讀交易明細、每日權益、指標與圖檔
- `metrics`：交易明細的輔助整理（回測區間等），不計算任何績效指標
- `futures_metrics`：期貨報表的保證金與口數曝險

頁面呈現留在 `frontend/app.py`，這裡只回傳 `DataFrame`／`Series`，方便不開 Streamlit 就能測試。
"""

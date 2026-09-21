FROM python:3.12-slim-bookworm

# **`PYTHONPATH=/app` 不可省**：`streamlit run frontend/app.py` 只把主程式所在的
# `/app/frontend` 放進 `sys.path`，工作目錄不在裡面（console script 的 `sys.path[0]`
# 是腳本自己的目錄）。少了它，`app.py` 的 `from frontend.config ...` 這條後備路徑
# 會 `ModuleNotFoundError`——而本機不出事，是因為開發環境把專案 editable 安裝了。
# `core/Dockerfile` 改用 editable 安裝時拿掉了這一行，前端映像沒有對應的替代。
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

WORKDIR /app

# NOTE:
# Build this Dockerfile with project root as context:
# docker build -f frontend/Dockerfile -t alphaedge-frontend .
COPY frontend /app/frontend

# **前端完全不 import `core`**：所有績效指標改讀 reporter 落地的
# `<策略>_metrics_summary.csv`，公式只存在於 `core/` 一處。舊版為了 Sharpe／
# Sortino 而 COPY 的那幾個 `core` 檔案（`performance_metrics.py` 與三個 `__init__.py`）
# 因此整段移除，映像的相依面只剩 `frontend/` 自己。
# `tests/test_frontend_metrics.py` 有一條測試盯住前端不再 import `core`。

RUN pip install --no-cache-dir --upgrade pip && \
    if [ -f /app/frontend/requirements.txt ]; then \
      pip install --no-cache-dir -r /app/frontend/requirements.txt; \
    else \
      pip install --no-cache-dir streamlit pandas; \
    fi

EXPOSE 8501

ENV ALPHAEDGE_RESULTS_DIR=/results

CMD ["streamlit", "run", "frontend/app.py", "--server.address=0.0.0.0", "--server.port=8501"]

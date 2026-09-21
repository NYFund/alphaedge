FROM python:3.12-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BROWSER_PATH=/usr/bin/chromium

WORKDIR /app

# chromium：回測報表存 PNG 走 plotly `write_image()` → kaleido 1.x，而 kaleido 1.x
# 不再內建瀏覽器，找不到 Chrome 就在報表最後一步拋 RuntimeError（CSV 已寫出、圖全沒有）。
# 用 Debian 的 chromium 而不是 `plotly_get_chrome`：後者下載的 Chrome for Testing
# 沒有 linux arm64 版本。`BROWSER_PATH` 讓 kaleido 不必猜瀏覽器的名稱與位置
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    chromium \
    && rm -rf /var/lib/apt/lists/*

# uv 版本釘死，與 CI 的 setup-uv 同一版：lock 檔格式跟著 uv 版本走
COPY --from=ghcr.io/astral-sh/uv:0.12.17 /uv /bin/uv

# 環境放在 /opt/venv 而不是預設的 /app/.venv：/app 底下有目錄會被 compose 掛載，
# 環境與原始碼分開，日後改成掛整個 /app 做開發時環境也不會被蓋掉。
# 放進 PATH 後 `ENTRYPOINT ["python", ...]` 用到的就是這個環境。
# UV_LINK_MODE=copy：快取與環境不在同一個檔案系統，hardlink 會失敗並逐檔警告
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

# 相依分兩層裝：先照 uv.lock 裝第三方套件，只改原始碼時這層仍吃快取。
# `--no-install-project`：此時原始碼還沒進來，本專案留到下方再裝。
# `--frozen`：直接照 lock 裝、不檢查 pyproject 是否變動——一致性由 CI 的 `--locked` 把關
COPY pyproject.toml uv.lock /app/
RUN uv sync --frozen --no-install-project --no-cache

# NOTE:
# Build this Dockerfile with project root as context:
# docker build -f core/Dockerfile -t alphaedge-core .
COPY run.py /app/run.py
COPY core /app/core
# `tasks/` 是 ETL 的入口（`python -m tasks.update_db`）。少了它，映像只跑得了
# 回測，compose 描述的資料更新流程在容器裡完全不存在
COPY tasks /app/tasks

# 裝本專案本身，與本機、CI 同一種安裝方式。
# **必須是 editable**（uv sync 對本專案預設即是）：`core/config/paths.py` 以 `__file__`
# 推算專案根目錄，一般安裝會把程式複製進 site-packages，`results/`、`logs/`
# 就不會落在掛載的 /app 底下
RUN uv sync --frozen --no-cache && uv pip check --python /opt/venv/bin/python

ENTRYPOINT ["python", "run.py"]
CMD ["--help"]

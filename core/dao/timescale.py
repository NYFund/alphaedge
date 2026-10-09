from typing import TYPE_CHECKING, Tuple, Type

from core.config.settings import require_tick_database_url

if TYPE_CHECKING:
    import psycopg

"""
台股 tick 的 TimescaleDB 連線入口

與 `core/dao/connection.py`（SQLite）並列：loader（`core/pipeline`）與 API（`core/api`）
都經這裡取得連線，連線設定只有一個來源。

**寫入與讀取用不同的 driver**：
- 寫入走 psycopg 3 的 `COPY`，百萬列級的寫入比逐列 `INSERT` 快一個數量級。
- 讀取走 ConnectorX，直接把查詢結果組成 DataFrame；`pd.read_sql` 逐列轉 Python 物件，
  一天全市場百萬列時慢上數十倍。ConnectorX 只吃連線字串、不吃 psycopg 的連線，
  所以讀取端拿的是 URI 而不是連線物件。

兩個套件都屬 `[tick]` 選用相依，**只在函式內 import**：只跑日線的機器沒有它們，
`import core.dao` 不能因此失敗。
"""

# 沒裝選用相依時的提示；放在模組層級，寫入與讀取兩個入口共用同一句
_MISSING_EXTRA_HINT: str = "請以 `uv sync --extra tick` 安裝台股 tick 的選用相依"


def get_tick_database_url() -> str:
    """取得 TimescaleDB 連線字串；`TICK_DATABASE_URL` 未設定時當場拋出"""

    return require_tick_database_url()


def connect_tick_db(autocommit: bool = False) -> "psycopg.Connection":
    """
    - Description:
        開啟寫入用的 psycopg 連線
    - Parameters:
        - autocommit: bool
            DDL 與只讀查詢可開；寫入資料一律關閉，由呼叫端以交易包住「刪除後重寫」
    - Return:
        - psycopg.Connection
            TimescaleDB 連線；呼叫端負責關閉
    - Raise:
        - ModuleNotFoundError
            未安裝 `[tick]` 選用相依
        - RuntimeError
            `TICK_DATABASE_URL` 未設定
    """

    try:
        import psycopg
    except ModuleNotFoundError as e:
        raise ModuleNotFoundError(f"找不到 psycopg：{_MISSING_EXTRA_HINT}") from e

    return psycopg.connect(get_tick_database_url(), autocommit=autocommit)


def get_connectorx_uri() -> str:
    """
    - Description:
        取得讀取用的 ConnectorX 連線字串

        與 `get_tick_database_url()` 同值，分成兩個函式是為了讓讀取端的意圖明確，
        日後若要加 ConnectorX 專用參數（例如 `?cxprotocol=`）只改這裡。
    - Return:
        - str
            `postgresql://` 開頭的連線字串
    - Raise:
        - ModuleNotFoundError
            未安裝 `[tick]` 選用相依
        - RuntimeError
            `TICK_DATABASE_URL` 未設定
    """

    try:
        import connectorx  # noqa: F401
    except ModuleNotFoundError as e:
        raise ModuleNotFoundError(f"找不到 connectorx：{_MISSING_EXTRA_HINT}") from e

    return get_tick_database_url()


def tick_db_error_types() -> Tuple[Type[BaseException], ...]:
    """
    - Description:
        TimescaleDB 寫入／查詢可能拋出的錯誤型別，給 DAO 以外的逐檔隔離 `except` 用

        `core/dao/` 以外不可 import psycopg（分層檢查會擋），又要能精準捕捉資料庫錯誤、
        而不是盲捕 `Exception`，所以由這裡回傳型別。沒裝選用相依時回傳空 tuple：
        連線那一步就已經失敗，不會走到需要捕捉資料庫錯誤的地方。
    - Return:
        - Tuple[Type[BaseException], ...]
            可直接放進 `except (...)` 的錯誤型別
    """

    try:
        import psycopg
    except ModuleNotFoundError:
        return ()

    return (psycopg.Error,)

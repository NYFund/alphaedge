import sqlite3
from pathlib import Path
from typing import Union

"""
資料庫連線的單一入口

全專案的 SQLite 連線一律從這裡開：`core/api`、`core/pipeline`、回測 DataFeed
原本各自 `sqlite3.connect()`，同一次 ETL 對同一個 DB 開兩三條連線、有些從不關閉。
收斂到一處之後，換資料庫（PostgreSQL）時只需要改這個檔案與 DAO 內部。

**`core/dao/` 以外不 `import sqlite3`**（`scripts/check_layer_deps.py` 會擋）：
型別標註用 `DBConnection`、捕捉資料庫錯誤用 `DBError`。
"""

# 資料庫連線的型別；DAO 以外的型別標註一律用它，不直接寫 `sqlite3.Connection`
DBConnection = sqlite3.Connection

# 資料庫錯誤的基底類別；DAO 以外需要捕捉資料庫錯誤時用它
DBError = sqlite3.Error


def connect_sqlite(
    db_path: Union[str, Path], read_only: bool = False
) -> sqlite3.Connection:
    """
    - Description:
        開啟 SQLite 連線
    - Parameters:
        - db_path: Union[str, Path]
            資料庫檔案路徑；`:memory:` 亦可
        - read_only: bool
            以唯讀模式開啟。只讀的分析工具應開唯讀，避免與背景 ETL 搶寫入鎖，
            也避免檔案不存在時被 `sqlite3.connect()` 默默建出一個空的 DB
    - Return:
        - sqlite3.Connection
            資料庫連線
    """

    if read_only:
        return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    return sqlite3.connect(db_path)

import sqlite3
from pathlib import Path
from typing import Union

"""
資料庫連線的單一入口

全專案的 SQLite 連線一律從這裡開。各處自行 `sqlite3.connect()` 會讓同一次 ETL
對同一個 DB 開出兩三條連線、且有些從不關閉；收斂到一處之後，換資料庫
（PostgreSQL）時只需要改這個檔案與 DAO 內部。

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


def connect_live_trading(
    db_path: Union[str, Path], read_only: bool = False
) -> sqlite3.Connection:
    """
    - Description:
        開啟**實盤交易紀錄庫**的連線

        與 `connect_sqlite()` 的差別只有兩個 PRAGMA，但兩個都不可省：

        - `journal_mode=WAL`：日報、前端與存活監控會在實盤行程寫入的**同時**讀取。
          預設的 rollback journal 會讓讀寫互相阻塞，而被擋住的那一方可能是送單路徑。
        - `synchronous=FULL`：「先寫 DB 再送單」的恢復保證建立在那一筆 commit
          **真的落地**。WAL 預設的 `NORMAL` 在主機斷電時可能丟掉最後一次 commit——
          而那剛好就是「已經寫了紀錄、還沒送出去」的那張單，重啟後會不知道它存不存在。

        **PRAGMA 下在這裡而不是讓 DAO 自己下**：每個取得連線的地方都得記得下一次的話，
        漏掉的那一次不會報錯，只會在斷電時丟掉最後一筆 commit。

        記憶體資料庫不支援 WAL（`journal_mode` 會維持 `memory`），這不是錯誤——
        測試本來就不需要這兩個保證，故不檢查回傳值。
    - Parameters:
        - db_path: Union[str, Path]
            資料庫檔案路徑
        - read_only: bool
            唯讀開啟（存活監控與報表一律用這個；實盤行程是**唯一寫入者**）
    - Return:
        - sqlite3.Connection
            已套用 WAL 與 FULL 同步的連線
    """

    conn: sqlite3.Connection = connect_sqlite(db_path, read_only=read_only)

    # 唯讀連線不可改 journal_mode（會拋 readonly database），且本來就不寫入
    if not read_only:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
    return conn

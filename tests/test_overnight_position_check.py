import importlib.util
import pathlib
import sqlite3
import sys
from typing import Any, Callable, Dict, Optional, Set

import pytest

from core.dao.connection import connect_live_trading
from core.dao.tw.live_trade_dao import LiveTradeDAO

"""
`scripts/check_overnight_positions.py` 的判讀邏輯

這支工具要回答的是「Shioaji 模擬環境會不會每晚把部位清掉」——而那個答案決定
模擬環境多日演練的四條隔夜驗收項目成不成立。判錯的代價很具體：
判成「保留」就會白跑四天，判成「清倉」則可能把一次真實的對帳缺口當成環境特性。

**釘住的是那個會漏報的判準**：`Reconciler._write_snapshots()` 是迭代券商部位
清單寫的，券商端沒有部位時**一列都不會寫**。若以「有沒有 broker 列」篩選日期，
被清倉的那天會整個消失，結論就會從「清倉」變成「資料不足」——
最該被偵測到的情況反而測不到。實作時就是先寫成那樣才被這裡抓出來。
"""


ROOT: pathlib.Path = pathlib.Path(__file__).resolve().parent.parent
SCRIPT: pathlib.Path = ROOT / "scripts" / "check_overnight_positions.py"

DAY_1: str = "2026-09-23"
DAY_2: str = "2026-09-24"


@pytest.fixture(scope="module")
def checker() -> Any:
    """以檔案路徑載入待測腳本（`scripts/` 不是套件）"""

    spec = importlib.util.spec_from_file_location("check_overnight_positions", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_overnight_positions"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def make_db(tmp_path: pathlib.Path) -> Callable[..., sqlite3.Connection]:
    """建一個有正式 schema 的空紀錄庫"""

    def _make() -> sqlite3.Connection:
        dao: LiveTradeDAO = LiveTradeDAO(conn=sqlite3.connect(tmp_path / "trading.db"))
        dao.ensure_tables()
        return dao.conn

    return _make


def add_snapshot(
    conn: sqlite3.Connection,
    date: str,
    source: str,
    symbol: str,
    volume: int,
    strategy_name: str = "MomentumStrategy1",
    direction: str = "LONG",
) -> None:
    """寫一列部位快照"""

    conn.execute(
        "INSERT INTO live_position_snapshot"
        "(date, strategy_name, symbol, source, direction, volume)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (date, strategy_name, symbol, source, direction, volume),
    )


def add_day(
    conn: sqlite3.Connection,
    date: str,
    holdings: Dict[str, int],
    with_broker: bool = True,
) -> None:
    """
    - Description:
        寫入某一天的三份快照

        `with_broker=False` 模擬「對帳有跑，但券商端一列都沒有」——
        也就是模擬環境清倉後的樣子。
    - Parameters:
        - conn: sqlite3.Connection
            紀錄庫連線
        - date: str
            日期
        - holdings: Dict[str, int]
            `{標的: 張數}`
        - with_broker: bool
            是否寫入 `source='broker'` 的列
    """

    for symbol, volume in holdings.items():
        add_snapshot(conn, date, "account", symbol, volume, strategy_name="__account__")
        add_snapshot(conn, date, "local", symbol, volume)
        if with_broker:
            add_snapshot(
                conn, date, "broker", symbol, volume, strategy_name="__broker__"
            )
    conn.commit()


def verdict(checker: Any, conn: sqlite3.Connection) -> Optional[bool]:
    """跑一次判讀，回傳結論"""

    snapshots: Dict[str, Dict[str, Any]] = checker.load_snapshots(conn)
    closed: Dict[str, Set[str]] = checker.load_closing_records(conn)
    return checker.report_overnight(snapshots, closed)


# === 核心判讀 ===
def test_positions_surviving_overnight_is_reported_as_kept(
    checker: Any, make_db: Callable[..., sqlite3.Connection]
) -> None:
    """兩天的券商部位一樣 → 判定為保留隔夜部位"""

    conn: sqlite3.Connection = make_db()
    add_day(conn, DAY_1, {"2330": 5, "2317": 3})
    add_day(conn, DAY_2, {"2330": 5, "2317": 3})

    assert verdict(checker, conn) is True


def test_broker_wiping_positions_is_detected(
    checker: Any, make_db: Callable[..., sqlite3.Connection]
) -> None:
    """
    次日券商端一列都沒有 → 判定為清倉

    **這是本檔存在的理由**：券商沒有部位時 `_write_snapshots()` 不會寫任何
    broker 列，所以不能用「有沒有 broker 列」來篩日期。第一版就是那樣寫的，
    結果這條測試回報的是「資料不足」而不是「清倉」。
    """

    conn: sqlite3.Connection = make_db()
    add_day(conn, DAY_1, {"2330": 5, "2317": 3})
    add_day(conn, DAY_2, {"2330": 5, "2317": 3}, with_broker=False)

    assert verdict(checker, conn) is False


def test_locally_closed_positions_are_not_counted_as_wiped(
    checker: Any, make_db: Callable[..., sqlite3.Connection]
) -> None:
    """
    本地有記平倉的標的不算被環境清掉

    少了這一層，**每一次正常平倉都會被誤判成環境清倉**——而演練期間策略
    每天都在平倉，那會讓這支工具天天報錯誤的結論。
    """

    conn: sqlite3.Connection = make_db()
    add_day(conn, DAY_1, {"2330": 5})
    add_day(conn, DAY_2, {}, with_broker=False)
    conn.execute(
        "INSERT INTO live_position_lot"
        "(lot_id, strategy_name, symbol, direction, volume,"
        " open_date, open_price, closed_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "lot-1",
            "MomentumStrategy1",
            "2330",
            "LONG",
            5,
            DAY_1,
            600.0,
            f"{DAY_2}T13:25:00+08:00",
        ),
    )
    conn.commit()

    assert verdict(checker, conn) is None, "全部都是正常平倉時不該下任何結論"


def test_a_single_day_is_not_enough(
    checker: Any, make_db: Callable[..., sqlite3.Connection]
) -> None:
    """只有一天的資料不下結論——演練第一天跑完時就是這個狀態"""

    conn: sqlite3.Connection = make_db()
    add_day(conn, DAY_1, {"2330": 5})

    assert verdict(checker, conn) is None


def test_empty_database_reports_nothing(
    checker: Any, make_db: Callable[..., sqlite3.Connection]
) -> None:
    """紀錄庫全空時不炸也不下結論"""

    conn: sqlite3.Connection = make_db()

    assert checker.load_snapshots(conn) == {}
    assert verdict(checker, conn) is None


# === 對帳缺口 ===
def test_mismatch_between_local_and_broker_is_listed(
    checker: Any,
    make_db: Callable[..., sqlite3.Connection],
    capsys: pytest.CaptureFixture,
) -> None:
    """
    本地有、券商沒有時要列成不一致

    這正是「模擬環境清倉」會造成的樣子，而 `Reconciler` 遇到它會送降級事件，
    接著「對帳不一致時停止開新倉」就會讓後續幾天空轉。
    """

    conn: sqlite3.Connection = make_db()
    add_day(conn, DAY_1, {"2330": 5}, with_broker=False)

    checker.report_mismatch(checker.load_snapshots(conn))
    output: str = capsys.readouterr().out

    assert "✗ 1 筆不一致" in output
    assert "本地 5 vs 券商 0" in output


def test_matching_sides_are_reported_as_consistent(
    checker: Any,
    make_db: Callable[..., sqlite3.Connection],
    capsys: pytest.CaptureFixture,
) -> None:
    """兩邊一致時要明確說一致，不可只在不一致時才出聲"""

    conn: sqlite3.Connection = make_db()
    add_day(conn, DAY_1, {"2330": 5})

    checker.report_mismatch(checker.load_snapshots(conn))

    assert "✓ 一致" in capsys.readouterr().out


# === 唯讀 ===
def test_the_script_opens_the_database_read_only() -> None:
    """
    演練進行中會執行這支工具，絕不能干擾正在寫入的行程

    經實盤紀錄庫的連線入口以唯讀開啟，寫入會直接被 SQLite 擋下。
    """

    source: str = SCRIPT.read_text()

    assert "connect_live_trading(args.db, read_only=True)" in source
    assert "sqlite3.connect" not in source
    for forbidden in ("INSERT", "UPDATE ", "DELETE", "DROP"):
        assert forbidden not in source.upper().replace("UPSERT", ""), (
            f"腳本含有 {forbidden}，它應該是唯讀的"
        )


def test_read_only_connection_actually_rejects_writes(
    tmp_path: pathlib.Path, make_db: Callable[..., sqlite3.Connection]
) -> None:
    """不只檢查字串，實際開一條唯讀連線確認寫入會被拒絕"""

    make_db().close()
    read_only: sqlite3.Connection = connect_live_trading(
        tmp_path / "trading.db", read_only=True
    )

    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        read_only.execute(
            "INSERT INTO live_position_snapshot"
            "(date, strategy_name, symbol, source, direction, volume)"
            " VALUES ('2026-09-23', 's', '2330', 'broker', 'LONG', 1)"
        )

    read_only.close()


# === 判讀不因來源順序改變 ===
@pytest.mark.parametrize(
    ("day_1", "day_2", "expected"),
    [
        ({"2330": 5}, {"2330": 5}, True),
        ({"2330": 5}, {"2330": 3}, True),  # 數量變了但標的還在＝沒有被清掉
        ({"2330": 5, "2317": 3}, {"2317": 3}, False),  # 少一檔且本地沒記平倉
    ],
)
def test_partial_changes(
    checker: Any,
    make_db: Callable[..., sqlite3.Connection],
    day_1: Dict[str, int],
    day_2: Dict[str, int],
    expected: Optional[bool],
) -> None:
    """數量變動不算清倉，標的整檔消失才算"""

    conn: sqlite3.Connection = make_db()
    add_day(conn, DAY_1, day_1)
    add_day(conn, DAY_2, day_2)

    assert verdict(checker, conn) is expected

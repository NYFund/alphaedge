import argparse
import datetime
import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import pytest

import scripts.live_watchdog as watchdog
from core.dao.tw.live_trade_dao import LiveTradeDAO
from core.dao.tw.market_holiday_dao import MarketHolidayDAO
from core.live.notify.base import BaseNotifier, NotifyLevel
from core.pipeline.tw.cleaners.market_holiday_cleaner import MarketHolidayCleaner
from core.pipeline.tw.loaders.market_holiday_loader import MarketHolidayLoader

"""
存活監控在非交易日不告警

watchdog 原本只比對段落表與 `live_run`，不判斷今天是不是交易日：週六整天每 5 分鐘
一則「open 應於 08:30 執行，但 live_run 沒有任何紀錄」CRITICAL。通知管道一設定，
每個週末與休市日都會連續推播，真的告警反而被淹沒。

規則：週末與官方休市日以 0 結束且不推播；交易日照舊；**判斷不出來時照交易日檢查並另外告警**。
"""

FIXTURE_PATH: Path = (
    Path(__file__).resolve().parents[1] / "fixtures" / "twse_holiday_schedule_2026.json"
)

TAIPEI: datetime.timezone = datetime.timezone(datetime.timedelta(hours=8))

# 2026 年度已入庫：週二交易日、週六、中秋（週五官方休市）
TRADING_DAY: datetime.date = datetime.date(2026, 9, 22)
SATURDAY: datetime.date = datetime.date(2026, 10, 17)
MID_AUTUMN: datetime.date = datetime.date(2026, 9, 25)
# 2027 年度未入庫的平日：官方日曆答不出來
UNCOVERED_WEEKDAY: datetime.date = datetime.date(2027, 3, 3)


class _RecordingNotifier(BaseNotifier):
    """記下送出的推播"""

    def __init__(self) -> None:
        self.sent: List[Tuple[NotifyLevel, str, str]] = []

    def send(self, level: NotifyLevel, title: str, body: str) -> None:
        self.sent.append((level, title, body))

    def deliver(self, level: NotifyLevel, title: str, body: str) -> None:
        pass


def build_stock_db(path: Path) -> Path:
    """灌好 2026 年官方日曆的研究庫檔案"""

    payload: Dict[str, Any] = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    raw: pd.DataFrame = pd.DataFrame(payload["data"], columns=payload["fields"])
    conn: sqlite3.Connection = sqlite3.connect(path)
    MarketHolidayLoader(dao=MarketHolidayDAO(conn=conn)).add_to_db(
        MarketHolidayCleaner().clean(raw, 2026), 2026
    )
    conn.commit()
    conn.close()
    return path


def all_phases_done(day: datetime.date) -> List[Tuple[Any, ...]]:
    """預設段落表裡每個段落都正常結束的當日紀錄（段落表依分支而異，故從程式讀）"""

    return [
        (
            phase,
            f"{day.isoformat()}T{at.strftime('%H:%M')}:00+08:00",
            f"{day.isoformat()}T{at.strftime('%H:%M')}:30+08:00",
            LiveTradeDAO.END_REASON_NORMAL,
        )
        for phase, at in watchdog.DEFAULT_EXPECTED_PHASES.items()
    ]


def run_watchdog(
    monkeypatch: pytest.MonkeyPatch,
    day: datetime.date,
    stock_db: Path,
    rows: Optional[List[Tuple[Any, ...]]],
) -> Tuple[int, _RecordingNotifier, List[str]]:
    """
    - Description:
        以指定日期 15:00 跑一次 `main()`

        `rows` 為 None 代表「不應該讀紀錄庫」：讀了就讓測試失敗。
    """

    notifier: _RecordingNotifier = _RecordingNotifier()
    fetched: List[str] = []

    def fake_fetch(db_path: str, today: datetime.date) -> List[Tuple[Any, ...]]:
        fetched.append(db_path)
        if rows is None:
            raise AssertionError("非交易日不該讀實盤紀錄庫")
        return rows

    monkeypatch.setattr(
        watchdog,
        "parse_arguments",
        lambda: argparse.Namespace(
            expect=None,
            db_path="unused-trading.db",
            stock_db_path=str(stock_db),
            grace_minutes=watchdog.GRACE_MINUTES,
        ),
    )
    monkeypatch.setattr(watchdog, "build_notifier", lambda *args, **kwargs: notifier)
    monkeypatch.setattr(
        watchdog,
        "now_live",
        lambda: datetime.datetime.combine(day, datetime.time(15, 0), tzinfo=TAIPEI),
    )
    monkeypatch.setattr(watchdog, "fetch_runs", fake_fetch)
    return watchdog.main(), notifier, fetched


def test_saturday_exits_quietly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """週六：不讀紀錄庫、不推播、結束碼 0"""

    stock_db: Path = build_stock_db(tmp_path / "tw_stock.db")

    code, notifier, fetched = run_watchdog(monkeypatch, SATURDAY, stock_db, None)

    assert code == watchdog.EXIT_OK
    assert notifier.sent == []
    assert fetched == []


def test_official_holiday_exits_quietly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """平日的官方休市日（中秋）：同樣不告警"""

    stock_db: Path = build_stock_db(tmp_path / "tw_stock.db")

    code, notifier, fetched = run_watchdog(monkeypatch, MID_AUTUMN, stock_db, None)

    assert code == watchdog.EXIT_OK
    assert notifier.sent == []
    assert fetched == []


def test_trading_day_still_alerts_on_missing_phase(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """交易日沒有任何紀錄：照舊推 CRITICAL"""

    stock_db: Path = build_stock_db(tmp_path / "tw_stock.db")

    code, notifier, fetched = run_watchdog(monkeypatch, TRADING_DAY, stock_db, [])

    assert code == watchdog.EXIT_ALERTED
    assert fetched == ["unused-trading.db"]
    assert len(notifier.sent) == 1
    assert notifier.sent[0][0] is NotifyLevel.CRITICAL
    assert "open 應於 08:30:00 執行" in notifier.sent[0][2]


def test_trading_day_with_all_phases_done_passes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """交易日段落都正常結束：不推播"""

    stock_db: Path = build_stock_db(tmp_path / "tw_stock.db")

    code, notifier, _ = run_watchdog(
        monkeypatch, TRADING_DAY, stock_db, all_phases_done(TRADING_DAY)
    )

    assert code == watchdog.EXIT_OK
    assert notifier.sent == []


def test_uncovered_year_alerts_even_when_phases_ran(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """官方日曆未入庫的平日：判斷不出來 → 照交易日檢查，並把「無法判定」列為告警"""

    stock_db: Path = build_stock_db(tmp_path / "tw_stock.db")

    code, notifier, fetched = run_watchdog(
        monkeypatch, UNCOVERED_WEEKDAY, stock_db, all_phases_done(UNCOVERED_WEEKDAY)
    )

    assert code == watchdog.EXIT_ALERTED
    assert fetched == ["unused-trading.db"]
    assert "無法判定 2027-03-03 是否為交易日" in notifier.sent[0][2]


def test_missing_stock_db_still_knows_weekends(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """研究庫開不了：週末照樣判得出休市；平日判斷不出來就告警"""

    missing: Path = tmp_path / "missing.db"

    code, notifier, _ = run_watchdog(monkeypatch, SATURDAY, missing, None)
    assert code == watchdog.EXIT_OK
    assert notifier.sent == []

    code, notifier, _ = run_watchdog(
        monkeypatch, TRADING_DAY, missing, all_phases_done(TRADING_DAY)
    )
    assert code == watchdog.EXIT_ALERTED
    assert "研究庫開不了" in notifier.sent[0][2]


def test_resolve_today_uses_the_same_rule_as_live_segments(tmp_path: Path) -> None:
    """與實盤段落同一套判定：已入庫年度答得出、未入庫平日答不出"""

    stock_db: Path = build_stock_db(tmp_path / "tw_stock.db")

    assert watchdog.resolve_today(str(stock_db), TRADING_DAY) == (True, None)
    assert watchdog.resolve_today(str(stock_db), MID_AUTUMN) == (False, None)
    assert watchdog.resolve_today(str(stock_db), SATURDAY) == (False, None)
    result, reason = watchdog.resolve_today(str(stock_db), UNCOVERED_WEEKDAY)
    assert result is None and reason

import datetime
import sqlite3
import sys
import threading
from pathlib import Path
from typing import Any, List, Optional, Tuple

import pytest

from core.dao.tw.live_trade_dao import LiveTradeDAO
from core.live.notify.base import BaseNotifier, NotifyLevel, NullNotifier, redact
from core.live.notify.factory import build_notifier
from core.live.notify.telegram_notifier import TelegramNotifier
from scripts.live_watchdog import (
    EXIT_ALERTED,
    EXIT_OK,
    EXIT_UNREACHABLE,
    PhaseStatus,
    check_phases,
    fetch_runs,
    parse_expected,
)

"""
事件通知與存活監控

實盤最常見的失效是**該跑的沒跑、沒人發現**。`live_risk_event` 與 log 都是 pull 型：
人要先去看才知道。程式在 09:05 因為登入失敗退出，到收盤都不會有人知道。

三條鐵律，每條都有測試：
1. 推播失敗**絕不可影響交易主流程**（監控拖垮被監控的東西是典型反例）。
2. 送出**不阻塞**（通知端的逾時不該讓尾盤那 4 分鐘卡住）。
3. 內容**不得包含金鑰、憑證密碼或完整帳號**（推播會被轉發、截圖、貼進群組）。
"""

NOW: datetime.datetime = datetime.datetime(2026, 9, 21, 14, 0)
TODAY: datetime.date = NOW.date()


class ExplodingNotifier(BaseNotifier):
    """送出必爆的管道"""

    def deliver(self, level: NotifyLevel, title: str, body: str) -> None:
        raise RuntimeError("通知端掛了")


class RecordingNotifier(BaseNotifier):
    """記下送出內容的管道"""

    def __init__(self, blocking: bool = True) -> None:
        super().__init__(blocking)
        self.messages: List[Tuple[NotifyLevel, str, str]] = []

    def deliver(self, level: NotifyLevel, title: str, body: str) -> None:
        self.messages.append((level, title, body))


class BlockingNotifier(BaseNotifier):
    """送出會卡住的管道，用來驗非阻塞"""

    def __init__(self) -> None:
        super().__init__(blocking=False)
        self.released: threading.Event = threading.Event()

    def deliver(self, level: NotifyLevel, title: str, body: str) -> None:
        self.released.wait(timeout=5.0)


# === 鐵律一：失敗不可影響主流程 ===
def test_delivery_failure_is_swallowed() -> None:
    """
    送出失敗只記 log，**絕不往上拋**

    監控拖垮被監控的東西是典型反例：通知端的網路逾時若讓送單路徑卡住，
    那正好發生在最需要送單的時候。
    """

    notifier: ExplodingNotifier = ExplodingNotifier(blocking=True)

    notifier.send(NotifyLevel.CRITICAL, "標題", "內容")  # 不拋出即為通過

    assert notifier.failed == 1
    assert notifier.sent == 0


def test_failure_is_counted_not_hidden() -> None:
    """
    失敗要計數

    完全吞掉的話，「推播一直送不出去」會變成一個沒有任何痕跡的狀態。
    """

    notifier: ExplodingNotifier = ExplodingNotifier(blocking=True)
    for _ in range(3):
        notifier.send(NotifyLevel.WARN, "標題", "內容")

    assert notifier.failed == 3


# === 鐵律二：不阻塞 ===
def test_send_does_not_block_the_caller() -> None:
    """
    送出走背景執行緒

    通知端逾時不該讓尾盤那 4 分鐘卡住。
    """

    notifier: BlockingNotifier = BlockingNotifier()
    started: float = datetime.datetime.now().timestamp()

    notifier.send(NotifyLevel.INFO, "標題", "內容")
    elapsed: float = datetime.datetime.now().timestamp() - started

    assert elapsed < 1.0
    notifier.released.set()


def test_background_thread_is_daemon() -> None:
    """
    背景執行緒是 daemon

    主流程結束時不等它——漏一則推播好過卡住結束。
    """

    notifier: BlockingNotifier = BlockingNotifier()
    before: set = set(threading.enumerate())

    notifier.send(NotifyLevel.INFO, "標題", "內容")
    spawned: List[threading.Thread] = [
        thread for thread in threading.enumerate() if thread not in before
    ]

    assert spawned
    assert all(thread.daemon for thread in spawned)
    notifier.released.set()


# === 鐵律三：內容遮蔽 ===
def test_account_numbers_keep_only_the_last_four_digits() -> None:
    """推播會被轉發、截圖、貼進群組，它的傳播範圍遠大於 log"""

    assert redact("帳號 1234567890 已登入") == "帳號 ****7890 已登入"


def test_long_tokens_are_masked() -> None:
    """金鑰、token、憑證指紋整段遮掉——寧可遮過頭"""

    masked: str = redact("key=abcdefghijklmnopqrstuvwxyz0123456789")

    assert "abcdefghijklmnop" not in masked


def test_redaction_happens_before_delivery() -> None:
    """
    遮蔽在**骨架**做，不是讓每個管道各寫一次

    漏寫的那一個會把金鑰送出去。
    """

    notifier: RecordingNotifier = RecordingNotifier()
    notifier.send(
        NotifyLevel.WARN, "帳號 1234567890", "key=abcdefghijklmnopqrstuvwxyz01"
    )

    _, title, body = notifier.messages[0]

    assert "1234567890" not in title
    assert "abcdefghijklmnop" not in body


def test_short_numbers_are_not_masked() -> None:
    """
    短數字不遮

    遮掉數量、價格、筆數的話，推播會變成一串看不懂的星號。
    """

    assert redact("成交 2 張 @ 1000 元") == "成交 2 張 @ 1000 元"


# === 管道組裝 ===
@pytest.mark.parametrize(
    "channel, token, target",
    [
        (None, "t", "c"),
        ("", "t", "c"),
        ("telegram", None, "c"),
        ("telegram", "t", None),
        ("slack", "t", "c"),
    ],
)
def test_incomplete_config_degrades_to_null(
    channel: Optional[str], token: Optional[str], target: Optional[str]
) -> None:
    """
    設定不全時退化為不推播，**但要留下 warning**

    靜默退化會讓人以為告警是通的——「以為有告警其實沒有」比
    「知道沒有告警」危險得多。
    """

    assert isinstance(build_notifier(channel, token, target), NullNotifier)


def test_complete_config_builds_telegram() -> None:
    """設定齊全才真的建出通道"""

    assert isinstance(build_notifier("telegram", "token", "chat"), TelegramNotifier)


def test_telegram_error_does_not_leak_the_token() -> None:
    """
    錯誤訊息只印狀態碼，**不印 URL**

    URL 帶著 bot token，而錯誤訊息會進 log。
    """

    notifier: TelegramNotifier = TelegramNotifier("secret-token", "chat", blocking=True)

    class Response:
        status_code: int = 403

    notifier_module = sys.modules["core.live.notify.telegram_notifier"]
    original = notifier_module.requests.post
    notifier_module.requests.post = lambda *args, **kwargs: Response()

    try:
        with pytest.raises(RuntimeError) as error:
            notifier.deliver(NotifyLevel.INFO, "標題", "內容")
    finally:
        notifier_module.requests.post = original

    assert "secret-token" not in str(error.value)
    assert "403" in str(error.value)


# === 存活監控 ===
def make_row(
    phase: str,
    started_at: Optional[str] = "2026-09-21 08:30:00",
    ended_at: Optional[str] = "2026-09-21 09:05:00",
    end_reason: Optional[str] = "正常結束",
) -> Tuple[Any, ...]:
    return (phase, started_at, ended_at, end_reason)


def test_missing_phase_is_detected() -> None:
    """
    該跑而沒有紀錄 → 告警

    這是 watchdog 存在的唯一理由：程式在 09:05 掛掉，沒有它到收盤都沒人知道。
    """

    statuses: List[PhaseStatus] = check_phases(
        [], {"open": datetime.time(8, 30)}, NOW, grace_minutes=15
    )

    assert len(statuses) == 1
    assert "沒有任何紀錄" in statuses[0].problem


def test_future_phase_is_not_reported() -> None:
    """
    還沒到時間的段落不告警

    對它告警會讓人每天早上收到假警報，然後學會忽略這個監控。
    """

    statuses: List[PhaseStatus] = check_phases(
        [], {"close": datetime.time(23, 0)}, NOW, grace_minutes=15
    )

    assert statuses == []


def test_grace_period_is_respected() -> None:
    """排程器本身有誤差，段落也要先做完對帳才寫紀錄"""

    just_now: datetime.datetime = datetime.datetime(2026, 9, 21, 8, 35)
    statuses: List[PhaseStatus] = check_phases(
        [], {"open": datetime.time(8, 30)}, just_now, grace_minutes=15
    )

    assert statuses == []


def test_stale_run_is_detected() -> None:
    """
    有開始紀錄但停在未結束 → 告警

    那是「跑到一半死掉」或「卡在等待回報」，兩者都要有人知道。
    """

    statuses: List[PhaseStatus] = check_phases(
        [make_row("open", ended_at=None, end_reason=None)],
        {"open": datetime.time(8, 30)},
        NOW,
        grace_minutes=15,
    )

    assert "沒有結束紀錄" in statuses[0].problem


def test_abnormal_end_reason_is_reported() -> None:
    """非正常結束（對帳不一致、kill switch）同樣要推播"""

    statuses: List[PhaseStatus] = check_phases(
        [make_row("open", end_reason="對帳不一致")],
        {"open": datetime.time(8, 30)},
        NOW,
        grace_minutes=15,
    )

    assert "非正常原因結束" in statuses[0].problem


def test_healthy_run_reports_nothing() -> None:
    """正常跑完就安靜：每天都推播的監控會被靜音"""

    statuses: List[PhaseStatus] = check_phases(
        [make_row("open")], {"open": datetime.time(8, 30)}, NOW, grace_minutes=15
    )

    assert statuses[0].is_healthy is True


def test_rerun_takes_the_latest_record() -> None:
    """同一段落重跑過時取最後一筆——那才是最終狀態"""

    statuses: List[PhaseStatus] = check_phases(
        [
            make_row("open", ended_at=None, end_reason=None),
            make_row("open"),
        ],
        {"open": datetime.time(8, 30)},
        NOW,
        grace_minutes=15,
    )

    assert statuses[0].is_healthy is True


def test_expect_argument_parsing() -> None:
    """段落表可由命令列覆寫；格式錯誤要拋出而不是默默用預設值"""

    assert parse_expected(["open=08:30"]) == {"open": datetime.time(8, 30)}
    assert parse_expected(None)  # 預設表

    with pytest.raises(ValueError):
        parse_expected(["open 08:30"])


def test_watchdog_reads_the_database_read_only(tmp_path: Path) -> None:
    """
    watchdog **只以唯讀開啟紀錄庫、不連券商**

    實盤行程是唯一寫入者；watchdog 不該佔用同帳號的連線額度或任何一類限流。
    """

    path: Path = tmp_path / "tw_trading.db"
    dao: LiveTradeDAO = LiveTradeDAO(db_path=path)
    dao.ensure_tables()
    dao.insert_run(
        {
            "run_id": "run1",
            "started_at": "2026-09-21 08:30:00",
            "phase": "open",
            "simulation": 1,
        }
    )
    dao.conn.commit()
    dao.close()

    rows: List[Tuple[Any, ...]] = fetch_runs(str(path), TODAY)

    assert len(rows) == 1
    assert rows[0][0] == "open"


def test_unreadable_database_is_critical_not_silent(tmp_path: Path) -> None:
    """
    **開不了紀錄庫本身就是 CRITICAL**

    WAL 的唯讀連線需要 `-shm`，而交易行程已死、容器以另一個 user 掛載或目錄
    不可寫時它可能不在——那剛好是最需要 watchdog 的時候。
    監控自己死掉而沒人知道，和沒有監控是同一件事。
    """

    with pytest.raises((sqlite3.Error, OSError)):
        fetch_runs(str(tmp_path / "does-not-exist.db"), TODAY)


def test_exit_codes_are_distinct() -> None:
    """排程要分得出「正常」「有告警」「連紀錄庫都開不了」"""

    assert len({EXIT_OK, EXIT_ALERTED, EXIT_UNREACHABLE}) == 3


# === 與交易主流程的整合 ===
def test_notifier_failure_does_not_stop_trading(tmp_path: Path) -> None:
    """
    **通知端拋例外時交易流程不受影響、委託照送**

    這是規劃列的驗證方式，也是整個通知模組最重要的性質：
    監控拖垮被監控的東西是典型反例。
    """

    from core.live.report.live_reporter import LiveReporter

    from .test_live_trader_day import Harness, ScriptedStrategy, make_order

    class Alpha(ScriptedStrategy):
        def __init__(self) -> None:
            super().__init__("Alpha", [make_order()])

    harness: Harness = Harness([Alpha()])
    harness.trader.notifier = ExplodingNotifier(blocking=True)
    harness.trader.reporter = LiveReporter(harness.dao, output_root=tmp_path)

    harness.trader.run(
        __import__("core.utils", fromlist=["ExecutionTiming"]).ExecutionTiming.AT_CLOSE
    )

    assert harness.broker.placed_count == 1


def test_notify_enabled_is_recorded_in_live_run() -> None:
    """
    「本次有沒有通知管道」要落地

    只記一行 warning 的話，事後回頭查「那天為什麼沒收到告警」會查不到答案。
    """

    dao: LiveTradeDAO = LiveTradeDAO(conn=sqlite3.connect(":memory:"))
    dao.ensure_tables()
    dao.insert_run(
        {
            "run_id": "run1",
            "started_at": NOW,
            "phase": "close",
            "simulation": 1,
            "notify_enabled": 0,
        }
    )
    dao.conn.commit()

    assert dao.conn.execute("SELECT notify_enabled FROM live_run").fetchone()[0] == 0

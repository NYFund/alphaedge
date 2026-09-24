import datetime
from typing import Any, List

import pytest

from core.live.intraday.session_guard import (
    SessionGuard,
    cover_action,
    is_night_session,
    resolve_accounting_date,
    resolve_live_policy,
    resolve_quote_date,
    stops_after_cover,
    uncovered_day_trade_positions,
)
from core.models import StockAccount, StockOrder, StockPosition
from core.utils import Action, DayTradeUncoveredPolicy, PositionType

"""
日終強制動作

回測的 `SettlementModel` 會在日終「替你把事情做完」，實盤不會——**沒有人會自動
幫你回補**。現股當沖先賣未回補，券商可能標借或直接違約交割，而程式這邊看起來
一切正常。

期貨夜盤有兩種日期，混用的後果只會在對帳時以「昨天的量對不起來」出現。
"""


def at(
    year: int, month: int, day: int, hour: int, minute: int = 0
) -> datetime.datetime:
    return datetime.datetime(year, month, day, hour, minute)


# === 政策對映 ===
def test_force_cover_is_used_as_is() -> None:
    """照做的政策不該產生告警——每次都告警的話沒有人會再看它"""

    policy, warning = resolve_live_policy(DayTradeUncoveredPolicy.FORCE_COVER_AT_CLOSE)

    assert policy is DayTradeUncoveredPolicy.FORCE_COVER_AT_CLOSE
    assert warning is None


def test_convert_to_margin_falls_back_to_force_cover() -> None:
    """
    `CONVERT_TO_MARGIN` 在實盤不存在

    現股當沖先賣未回補**不會自動變成融券部位**——那是券商端的處理
    （可能標借或違約），程式這邊決定不了。照回測的設定做等於什麼都沒做。
    """

    policy, warning = resolve_live_policy(DayTradeUncoveredPolicy.CONVERT_TO_MARGIN)

    assert policy is DayTradeUncoveredPolicy.FORCE_COVER_AT_CLOSE
    assert warning is not None and "不存在" in warning


def test_raise_covers_first_then_stops() -> None:
    """
    `RAISE` 要**先回補再停止**

    只拋例外會讓部位留在場上過夜，而那正是這個政策想避免的事。
    """

    policy, warning = resolve_live_policy(DayTradeUncoveredPolicy.RAISE)

    assert policy is DayTradeUncoveredPolicy.FORCE_COVER_AT_CLOSE
    assert warning is not None
    assert stops_after_cover(DayTradeUncoveredPolicy.RAISE) is True
    assert stops_after_cover(DayTradeUncoveredPolicy.FORCE_COVER_AT_CLOSE) is False


# === 期貨夜盤的兩種日期 ===
@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (at(2026, 9, 21, 9), False),  # 日盤
        (at(2026, 9, 21, 13, 40), False),  # 日盤收盤前
        (at(2026, 9, 21, 14), False),  # 空檔歸日盤
        (at(2026, 9, 21, 15), True),  # 夜盤開盤
        (at(2026, 9, 21, 23, 59), True),
        (at(2026, 9, 22, 0, 1), True),  # 跨午夜仍是夜盤
        (at(2026, 9, 22, 4, 59), True),
        (at(2026, 9, 22, 6), False),  # 夜盤收盤後的空檔歸日盤
    ],
)
def test_night_session_window(moment: datetime.datetime, expected: bool) -> None:
    """
    夜盤 15:00~次日 05:00，兩段之間的空檔歸日盤

    空檔歸夜盤的話，收盤後的動作會被記成次一交易日的帳。
    """

    assert is_night_session(moment) is expected


def test_accounting_date_does_not_change_across_midnight() -> None:
    """
    **23:59 → 00:01 帳務日不變**

    兩者是同一段夜盤。取當下日曆日再往後推，會讓 00:01 之後的成交整段記到
    後一天，而那個錯誤只會在對帳時以「昨天的量對不起來」出現。
    """

    def next_trading_day(date: datetime.date) -> datetime.date:
        return date + datetime.timedelta(days=1)

    before: datetime.date = resolve_accounting_date(
        at(2026, 9, 21, 23, 59), next_trading_day
    )
    after: datetime.date = resolve_accounting_date(
        at(2026, 9, 22, 0, 1), next_trading_day
    )

    assert before == after == datetime.date(2026, 9, 22)


def test_friday_night_belongs_to_monday() -> None:
    """
    星期五晚上的夜盤屬於星期一

    次一交易日由日曆決定，不是「加一天」——這正是要注入日曆的理由。
    """

    def next_trading_day(date: datetime.date) -> datetime.date:
        # 2026-09-25 是星期五
        return (
            datetime.date(2026, 9, 28)
            if date == datetime.date(2026, 9, 25)
            else date + datetime.timedelta(days=1)
        )

    friday_night: datetime.date = resolve_accounting_date(
        at(2026, 9, 25, 20), next_trading_day
    )
    saturday_dawn: datetime.date = resolve_accounting_date(
        at(2026, 9, 26, 0, 30), next_trading_day
    )

    assert friday_night == saturday_dawn == datetime.date(2026, 9, 28)


def test_day_session_accounting_date_is_today() -> None:
    """日盤就是當天，不經過日曆"""

    def explode(date: datetime.date) -> datetime.date:
        raise AssertionError("日盤不該查交易日曆")

    assert resolve_accounting_date(at(2026, 9, 21, 10), explode) == datetime.date(
        2026, 9, 21
    )


def test_quote_date_uses_the_session_open_day_not_the_accounting_day() -> None:
    """
    **查行情用開盤當天，不是帳務日**

    `futures_price_daily` 把夜盤存在開盤當天的日曆日（資料表忠實記錄來源）。
    拿帳務日去查會查不到東西，而那是空報價不是錯誤。
    """

    assert resolve_quote_date(at(2026, 9, 21, 20)) == datetime.date(2026, 9, 21)
    assert resolve_quote_date(at(2026, 9, 22, 0, 30)) == datetime.date(2026, 9, 21)
    assert resolve_quote_date(at(2026, 9, 21, 10)) == datetime.date(2026, 9, 21)


# === 當沖回補 ===
def make_position(
    symbol: str = "2330", is_day_trade: bool = True, closed: bool = False
) -> StockPosition:
    position: StockPosition = StockPosition(
        id=1,
        stock_id=symbol,
        is_closed=closed,
        position_type=PositionType.SHORT,
        date=datetime.date(2026, 9, 21),
        price=1000.0,
        volume=1,
    )
    position.is_day_trade = is_day_trade
    return position


def make_account(*positions: StockPosition) -> StockAccount:
    account: StockAccount = StockAccount(init_capital=10_000_000.0)
    account.positions = list(positions)
    return account


def test_only_open_day_trade_shorts_are_collected() -> None:
    """留倉空單與已平倉的不該被當成待回補"""

    account: StockAccount = make_account(
        make_position("2330"),
        make_position("2317", is_day_trade=False),
        make_position("2454", closed=True),
    )

    found: List[Any] = uncovered_day_trade_positions(account)

    assert [p.symbol for p in found] == ["2330"]


def test_cover_action_is_buy_for_a_short() -> None:
    """回補空單就是買進"""

    assert cover_action(make_position()) is Action.BUY


def test_guard_waits_until_the_cover_time() -> None:
    """13:20 之前不該動作——提早回補等於放棄當沖的後半段"""

    clock: List[datetime.datetime] = [at(2026, 9, 21, 13, 19)]
    guard: SessionGuard = SessionGuard(lambda: clock[0])

    assert guard.should_cover_now() is False

    clock[0] = at(2026, 9, 21, 13, 20)
    assert guard.should_cover_now() is True


def test_guard_covers_once_per_trading_day() -> None:
    """
    同一天只做一次，但**換日要重來**

    以日期為鍵而不是布林旗標：常駐行程跨日之後旗標不會自己歸零，
    第二天就永遠不回補了。
    """

    clock: List[datetime.datetime] = [at(2026, 9, 21, 13, 25)]
    guard: SessionGuard = SessionGuard(lambda: clock[0])

    guard.mark_covered()
    assert guard.should_cover_now() is False

    clock[0] = at(2026, 9, 22, 13, 25)
    assert guard.should_cover_now() is True


def test_build_cover_orders_reports_the_rewritten_policy() -> None:
    """政策被改寫要回報，不可以默默改掉"""

    account: StockAccount = make_account(make_position())

    def build(position: Any) -> StockOrder:
        return StockOrder(
            stock_id=position.symbol,
            date=at(2026, 9, 21, 13, 20),
            action=cover_action(position),
            position_type=PositionType.SHORT,
            price=1000.0,
            volume=position.volume,
        )

    orders, warning = SessionGuard(lambda: at(2026, 9, 21, 13, 20)).build_cover_orders(
        account, DayTradeUncoveredPolicy.CONVERT_TO_MARGIN, build
    )

    assert [o.symbol for o in orders] == ["2330"]
    assert warning is not None


def test_unpriceable_position_is_logged_not_silently_skipped() -> None:
    """
    取不到可成交價時那筆部位會留倉過夜

    這是現股當沖最不能發生的事，所以要留下 error 而不是靜靜跳過。
    """

    account: StockAccount = make_account(make_position())

    orders, _warning = SessionGuard(lambda: at(2026, 9, 21, 13, 20)).build_cover_orders(
        account,
        DayTradeUncoveredPolicy.FORCE_COVER_AT_CLOSE,
        lambda position: None,
    )

    assert orders == []


# === 接線：心跳要真的觸發回補 ===
def test_heartbeat_actually_sends_the_cover_orders() -> None:
    """
    回補時點一到，心跳要真的把回補單送出去

    **這是接線測試裡最重要的一條**：判定寫得再對，沒有人呼叫就等於沒有，
    而未回補留倉的後果見本檔開頭。
    """

    from tests.live.test_live_trader_day import Harness, ScriptedStrategy, make_order

    class Alpha(ScriptedStrategy):
        def __init__(self) -> None:
            super().__init__("Alpha", [make_order()])

    harness: Any = Harness([Alpha()])
    harness.trader.prepare()

    # 帳上留一筆未回補的當沖空單，並讓它有報價可取
    context: Any = harness.contexts[0]
    context.account.positions = [make_position("2330")]
    harness.trader._last_quotes["2330"] = harness.broker.quotes["2330"]

    before: int = harness.broker.placed_count
    harness.trader.session_guard = SessionGuard(lambda: at(2026, 9, 21, 13, 25))
    harness.trader._run_session_guard()

    assert harness.broker.placed_count == before + 1


def test_heartbeat_does_nothing_before_the_cover_time() -> None:
    """時點未到不可以動作，理由同 `test_guard_waits_until_the_cover_time`"""

    from tests.live.test_live_trader_day import Harness, ScriptedStrategy, make_order

    class Alpha(ScriptedStrategy):
        def __init__(self) -> None:
            super().__init__("Alpha", [make_order()])

    harness: Any = Harness([Alpha()])
    harness.trader.prepare()
    harness.contexts[0].account.positions = [make_position("2330")]
    harness.trader._last_quotes["2330"] = harness.broker.quotes["2330"]

    before: int = harness.broker.placed_count
    harness.trader.session_guard = SessionGuard(lambda: at(2026, 9, 21, 13, 19))
    harness.trader._run_session_guard()

    assert harness.broker.placed_count == before


# === 接線：回補失敗要重試並告警 ===
def make_cover_harness(*symbols: str) -> Any:
    """已過回補時點、帳上有未回補當沖空單的整組替身；只有 2330 有報價"""

    from tests.live.test_live_trader_day import Harness, ScriptedStrategy, make_order

    class Alpha(ScriptedStrategy):
        def __init__(self) -> None:
            super().__init__("Alpha", [make_order()])

    harness: Any = Harness([Alpha()])
    harness.trader.prepare()
    harness.contexts[0].account.positions = [
        make_position(symbol) for symbol in symbols or ("2330",)
    ]
    harness.trader._last_quotes["2330"] = harness.broker.quotes["2330"]
    harness.trader.session_guard = SessionGuard(lambda: at(2026, 9, 21, 13, 25))
    return harness


def cover_failures(harness: Any) -> List[Any]:
    """已寫入的回補失敗事件 `(severity, message)`"""

    return harness.dao.conn.execute(
        "SELECT severity, message FROM live_risk_event "
        "WHERE category = 'DAY_TRADE_COVER_FAILED' ORDER BY event_id"
    ).fetchall()


def test_failed_cover_is_retried_on_the_next_heartbeat() -> None:
    """
    送單失敗時不標記「今天已回補」，下一次心跳再送

    以前是先標記再送：那一次心跳剛好斷線，當天就不會再試，
    現股當沖空單留倉過夜，而且只留一行 log。
    """

    harness: Any = make_cover_harness()
    harness.broker.close()
    before: int = harness.broker.placed_count

    harness.trader._run_session_guard()

    assert harness.broker.placed_count == before
    assert harness.trader.session_guard.should_cover_now() is True
    assert [row[0] for row in cover_failures(harness)] == ["CRITICAL"]

    harness.broker.connect()
    harness.trader._run_session_guard()

    assert harness.broker.placed_count == before + 1
    assert harness.trader.session_guard.should_cover_now() is False


def test_heartbeat_covers_once_the_connection_is_back() -> None:
    """首次心跳連不上就先不回補，第二次心跳重連成功後送出"""

    harness: Any = make_cover_harness()
    harness.broker.close()
    harness.broker.fail_connect = True
    before: int = harness.broker.placed_count

    harness.trader._on_heartbeat()
    assert harness.broker.placed_count == before

    harness.broker.fail_connect = False
    harness.trader._on_heartbeat()
    assert harness.broker.placed_count == before + 1


def test_missing_price_is_a_critical_event_until_attempts_run_out() -> None:
    """
    取不到報價要寫 CRITICAL 事件並推播，次數用完才停

    以前只記 `logger.error`：沒有推播、沒有 risk event，要等隔天翻 log 才知道。
    """

    harness: Any = make_cover_harness("2317")

    for _ in range(harness.trader.MAX_COVER_ATTEMPTS + 2):
        harness.trader._run_session_guard()

    failures: List[Any] = cover_failures(harness)
    assert len(failures) == harness.trader.MAX_COVER_ATTEMPTS
    assert all(severity == "CRITICAL" for severity, _ in failures)
    assert "2317" in failures[0][1]
    assert "人工處理" in failures[-1][1]
    assert harness.trader.session_guard.should_cover_now() is False


def test_retry_does_not_resend_a_cover_already_on_its_way() -> None:
    """
    重試時不重送已送出、還沒成交的回補單

    回補單沒成交前，部位仍然算「未回補」。只因為另一檔失敗而整批重試的話，
    已送出的那一檔會再送一次——空單一張，買回兩張。
    """

    harness: Any = make_cover_harness("2330", "2317")
    harness.broker.fill_ratio = 0
    before: int = harness.broker.placed_count

    harness.trader._run_session_guard()
    harness.trader._run_session_guard()

    assert harness.broker.placed_count == before + 1
    assert len(cover_failures(harness)) == 2

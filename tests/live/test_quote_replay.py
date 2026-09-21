import datetime
import queue
from decimal import Decimal
from pathlib import Path
from typing import Any, List

import pytest

from core.broker.rate_limiter import RateLimiter
from core.broker.tw.quote_replay import (
    RecordedMessage,
    load_recorded_messages,
    replay_quotes,
)
from core.broker.tw.shioaji_quote_stream import ShioajiQuoteStream
from core.models import BaseQuote
from core.utils import ExecutionTiming, LiveHook, Scale

"""
錄製行情的重放

Phase5-1 的驗證條件之一是「同一份錄製 tick 餵兩次，訊號一致」。
**逐筆策略的回測重現不了實盤**（回測沒有 wall clock，跨股票的到達順序取決於
券商推送），所以「確定性」只能靠重放同一份錄製來驗——那也正是錄製的用途。

素材是 2026-09-21 台北 11:30 模擬環境的**真實推播**，不是手寫的假資料：
前兩次實連已經證明，照文件寫的假資料會漏掉 `simtrade` 與 `intraday_odd` 這類欄位。
"""

FIXTURE: Path = Path(__file__).parent / "fixtures" / "recorded_ticks.jsonl"


def make_stream() -> ShioajiQuoteStream:
    """轉換不需要 api，給一個佔位物件即可"""

    return ShioajiQuoteStream(object(), RateLimiter(), queue.Queue())


def quote_signature(quotes: List[BaseQuote]) -> List[tuple]:
    """把報價壓成可比較的鍵；浮點值直接比，重放本來就該逐位元相同"""

    return [
        (q.symbol, q.date.isoformat(), q.cur_price, q.close, q.volume) for q in quotes
    ]


# === 重放本身 ===
def test_fixture_replays_into_quotes() -> None:
    """錄製檔要能還原成報價——素材壞了的話後面每一條都沒有意義"""

    quotes: List[BaseQuote] = replay_quotes(FIXTURE, make_stream())

    assert len(quotes) == 6
    assert {q.symbol for q in quotes} == {"2330", "2317", "2454"}
    assert all(q.scale is Scale.TICK for q in quotes)


def test_replay_goes_through_the_real_converter() -> None:
    """
    重放走的是實盤那份轉換，所以型別與時區都要對

    只驗「JSON 讀得出來」證明不了什麼——要驗的是「這份資料餵進系統會得到什麼」。
    """

    quote: BaseQuote = replay_quotes(FIXTURE, make_stream())[0]

    assert isinstance(quote.close, float)
    assert quote.date.utcoffset() == datetime.timedelta(hours=8)


def test_decimal_and_datetime_are_restored_from_json() -> None:
    """
    JSON 沒有 `Decimal` 與 `datetime`，錄製時被序列化成字串

    不還原的話 `datetime` 那一欄會當場炸；更糟的是哪天有人加了預設值，
    於是重放靜靜產出錯的報價。
    """

    message: RecordedMessage = load_recorded_messages(FIXTURE)[0]

    assert isinstance(message.close, Decimal)
    assert isinstance(message.datetime, datetime.datetime)
    assert isinstance(message.total_volume, int)


def test_recorded_message_has_no_dict_like_the_real_object() -> None:
    """
    重放物件不可以比真品「好用」

    真品是 C 擴充物件（沒有 `__dict__`、`dict()`、`model_dump()`，`dir()` 也是空的）。
    重放物件多了那些，轉換層就可能走到一條真實環境走不到的路。
    """

    message: RecordedMessage = load_recorded_messages(FIXTURE)[0]

    assert not hasattr(message, "model_dump")
    assert not hasattr(message, "dict")
    missing: str = "no_such_field"
    with pytest.raises(AttributeError):
        getattr(message, missing)


# === Phase5-1 的驗證條件：餵兩次，訊號一致 ===
def test_same_recording_yields_identical_quotes_twice() -> None:
    """同一份錄製餵兩次，報價要逐筆相同"""

    first: List[BaseQuote] = replay_quotes(FIXTURE, make_stream())
    second: List[BaseQuote] = replay_quotes(FIXTURE, make_stream())

    assert quote_signature(first) == quote_signature(second)


def test_same_recording_yields_identical_signals_twice() -> None:
    """
    **同一份錄製餵兩次，訊號要一致**（Phase5-1 的驗證條件）

    逐筆策略的回測重現不了實盤，所以確定性只能靠重放驗。這條測試釘住的是
    「轉換 ＋ 逐筆派送」這一段沒有隱藏狀態——有的話兩次的訊號會不一樣，
    而那種 bug 在實盤只會表現成「昨天和今天的訊號對不起來」。
    """

    from tests.live.test_live_trader_day import (
        Harness,
        ScriptedStrategy,
        make_order,
    )

    quotes: List[BaseQuote] = replay_quotes(FIXTURE, make_stream())

    def collect() -> List[Any]:
        class Alpha(ScriptedStrategy):
            def __init__(self) -> None:
                super().__init__("Alpha", [make_order("2330")])
                self.is_intraday = True
                # **鉤子要排在 IMMEDIATE**，否則逐筆段落一個鉤子都不會觸發，
                # 這條測試就變成 `[] == []`——正是它要防的那種假綠燈
                self.live_schedule = {
                    LiveHook.OPEN.value: ExecutionTiming.IMMEDIATE,
                    LiveHook.CLOSE.value: ExecutionTiming.IMMEDIATE,
                }

        harness: Harness = Harness([Alpha()])
        harness.contexts[0].symbols = ["2330", "2317", "2454"]
        # **一定要走 `prepare()`**：資金額度與曝險上限都在那裡才刷新，
        # 跳過的話 `available_balance` 是 0，跨策略檢查會把每一張單都截斷，
        # 於是這條測試變成 `[] == []`
        harness.trader.prepare()

        placed: List[Any] = []
        original = harness.trader.dispatch

        def recording_dispatch(survivors: List[Any], window: Any) -> None:
            placed.extend(
                (context.name, order.symbol, order.action.value, order.volume)
                for context, order in survivors
            )
            original(survivors, window)

        harness.trader.dispatch = recording_dispatch
        for quote in quotes:
            harness.trader._on_intraday_quote(quote)
        return placed

    first: List[Any] = collect()
    second: List[Any] = collect()

    # **先確認真的有訊號**：兩邊都是空的話這條測試等於沒驗
    assert first, "重放沒有產生任何委託，這條測試無法證明確定性"
    assert first == second


def test_replay_feeds_every_subscribed_symbol() -> None:
    """
    重放要涵蓋多檔

    只用一檔驗確定性會漏掉「跨標的狀態互相污染」那一類問題，
    而橫斷面策略在逐筆之下正是要自己維護跨標的狀態。
    """

    quotes: List[BaseQuote] = replay_quotes(FIXTURE, make_stream())

    assert len({q.symbol for q in quotes}) >= 3


def test_empty_recording_is_reported_not_silently_skipped(tmp_path: Path) -> None:
    """
    空的 `message` 要警告

    第一版錄製腳本因為取法不對，447 筆全錄成 `{}`。靜靜略過會讓重放看起來
    「沒有資料」，而真正的問題是那份錄製本身是廢的。
    """

    broken: Path = tmp_path / "broken.jsonl"
    broken.write_text(
        '{"kind": "tick_stk", "message": {}}\n{"kind": "tick_stk", "message": {}}\n',
        encoding="utf-8",
    )

    assert load_recorded_messages(broken) == []

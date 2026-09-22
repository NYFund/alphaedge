import datetime

import pytest

from core.dao.tw.live_trade_dao import LiveTradeDAO
from core.live.risk.trading_mode import TradingMode, TradingModeState, stricter

"""
`TradingMode`：只能單向降級，恢復一律人工，而且**兩層都要落地**

擋的是實盤最常見的事故型態：程式因對帳不一致停下來，值班的人直接重啟，
於是帶著錯誤部位繼續交易。日頻下按重啟鍵的還可能是 crontab——
open／close／after_close 是三個獨立行程。
"""

NOW: datetime.datetime = datetime.datetime(2026, 9, 19, 13, 25)


@pytest.fixture
def state(dao: LiveTradeDAO) -> TradingModeState:
    return TradingModeState(dao=dao, run_id="run1", now_provider=lambda: NOW)


# === 嚴格程度 ===
@pytest.mark.parametrize(
    "first, second, expected",
    [
        (TradingMode.NORMAL, TradingMode.REDUCE_ONLY, TradingMode.REDUCE_ONLY),
        (TradingMode.REDUCE_ONLY, TradingMode.HALTED, TradingMode.HALTED),
        (TradingMode.HALTED, TradingMode.NORMAL, TradingMode.HALTED),
        (TradingMode.NORMAL, TradingMode.NORMAL, TradingMode.NORMAL),
    ],
)
def test_stricter_of_two_modes(
    first: TradingMode, second: TradingMode, expected: TradingMode
) -> None:
    """
    多策略下能否送單要同時看帳戶層與策略層，取**嚴格者**

    取寬鬆者的話，帳戶層已經 halt 了某支策略還在送單。
    """

    assert stricter(first, second) is expected


# === 單向降級 ===
def test_degrade_only_goes_one_way(state: TradingModeState) -> None:
    """
    降級之後不會被較寬鬆的目標拉回去

    自動放寬等於賭「異常已經好了」，而會自動降級的條件多半偵測不完整。
    """

    state.degrade(TradingMode.HALTED, "kill switch")
    state.degrade(TradingMode.REDUCE_ONLY, "行情中斷")

    assert state.account_mode is TradingMode.HALTED


def test_resume_is_the_only_way_back(state: TradingModeState) -> None:
    """恢復只能由人工觸發"""

    state.degrade(TradingMode.REDUCE_ONLY, "對帳不一致")
    state.resume()

    assert state.account_mode is TradingMode.NORMAL


# === 兩層 ===
def test_account_degrade_affects_every_strategy(state: TradingModeState) -> None:
    """
    帳戶層降級影響全體

    對帳差異無法歸因到單支策略，猜錯的代價是讓真正有問題的那支繼續交易。
    """

    state.degrade(TradingMode.REDUCE_ONLY, "對帳不一致")

    assert state.allows_open("A") is False
    assert state.allows_open("B") is False


def test_strategy_degrade_does_not_touch_the_others(state: TradingModeState) -> None:
    """
    策略層降級只影響那一支

    一支策略的鉤子拋例外，不該讓其他策略的平倉單也送不出去。
    """

    state.degrade(TradingMode.HALTED, "鉤子拋例外", strategy_name="A")

    assert state.allows_close("A") is False
    assert state.allows_open("B") is True


def test_reduce_only_still_allows_closing(state: TradingModeState) -> None:
    """
    `REDUCE_ONLY` 與 `HALTED` 的差別要守住

    行情中斷時部位還在場上，降到 `HALTED` 會連停損都送不出去。
    """

    state.degrade(TradingMode.REDUCE_ONLY, "報價過期", strategy_name="A")

    assert state.allows_open("A") is False
    assert state.allows_close("A") is True


def test_halted_blocks_even_closing(state: TradingModeState) -> None:
    """`HALTED` 連平倉都不送，所以只用在「連部位都不可信」的情況"""

    state.degrade(TradingMode.HALTED, "kill switch", strategy_name="A")

    assert state.allows_close("A") is False


# === 落地與讀回 ===
def test_strategy_mode_survives_a_process_restart(dao: LiveTradeDAO) -> None:
    """
    **策略層模式要跨段落延續**

    日頻的 open／close／after_close 是三個獨立行程。不落地的話，
    開盤段因當日虧損上限而降級的策略，到 13:20 尾盤段會靜默回到 NORMAL
    繼續開新倉。
    """

    morning: TradingModeState = TradingModeState(dao, "run1", lambda: NOW)
    morning.degrade(TradingMode.REDUCE_ONLY, "當日虧損上限", strategy_name="A")

    afternoon: TradingModeState = TradingModeState(dao, "run2", lambda: NOW)
    afternoon.load()

    assert afternoon.effective_mode("A") is TradingMode.REDUCE_ONLY
    assert afternoon.allows_open("A") is False


def test_account_mode_is_read_back_from_the_last_finished_run(
    dao: LiveTradeDAO,
) -> None:
    """帳戶層模式從上一次**已結束**的啟動紀錄讀回"""

    dao.insert_run(
        {"run_id": "run1", "started_at": NOW, "phase": "open", "simulation": 1}
    )
    dao.finish_run("run1", NOW, "對帳不一致", "REDUCE_ONLY")

    state: TradingModeState = TradingModeState(dao, "run2", lambda: NOW)
    state.load()

    assert state.account_mode is TradingMode.REDUCE_ONLY


def test_no_history_starts_normal(dao: LiveTradeDAO) -> None:
    """第一次啟動時兩層都是 NORMAL"""

    state: TradingModeState = TradingModeState(dao, "run1", lambda: NOW)
    state.load()

    assert state.account_mode is TradingMode.NORMAL
    assert state.effective_mode("A") is TradingMode.NORMAL


def test_state_works_without_a_dao() -> None:
    """沒有紀錄庫時仍可運作（測試與 dry-run 用），只是不落地"""

    state: TradingModeState = TradingModeState()
    state.degrade(TradingMode.HALTED, "測試")

    assert state.account_mode is TradingMode.HALTED

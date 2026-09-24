import datetime
from typing import Dict, List, Optional, Tuple

import pytest

from core.dao.tw.live_trade_dao import LiveTradeDAO
from core.live.attribution.position_ledger import (
    UNATTRIBUTED_STRATEGY,
    PositionAttributionLedger,
)
from core.models import BrokerPositionSnapshot, ExecutionReport
from core.utils import Action, PositionType

"""
部位歸屬帳：券商合併部位拆不回策略，只能本地自己記

不變式：**Σ 各策略 lot 淨額 ＝ 券商部位**。

兩件事特別要緊：
- **沖銷順序要與回測的 `PositionManager` 一致**（FIFO）。不一致的話已實現損益
  會對不上，而券商端的合計還是對的——對帳看不出來。
- **`get_holder()` 對未歸屬部位要回 `__unattributed__`，不可回 None**（回 None 等於
  放行，後果見 `test_holder_of_an_unattributed_position_is_not_none`）。
"""

NOW: datetime.datetime = datetime.datetime(2026, 9, 19, 13, 25)


@pytest.fixture
def clock() -> List[datetime.datetime]:
    """可變的「現在」：測試改 `clock[0]` 就能讓下一筆 lot 落在另一天"""

    return [NOW]


@pytest.fixture
def ledger(
    dao: LiveTradeDAO, clock: List[datetime.datetime]
) -> PositionAttributionLedger:
    """接上暫存紀錄庫與可控時鐘的歸屬帳"""

    return PositionAttributionLedger(dao, now_provider=lambda: clock[0])


def make_fill(
    symbol: str = "2330",
    volume: int = 2,
    price: float = 1000.0,
    ts: Optional[datetime.datetime] = None,
) -> ExecutionReport:
    """組一筆買進成交回報（預設 2330、2 張、1000 元）"""

    return ExecutionReport(
        broker_seqno="000001",
        broker_trade_id="T001",
        symbol=symbol,
        action=Action.BUY,
        price=price,
        volume=volume,
        ts=ts,
    )


# === 開倉與加總 ===
def test_two_strategies_hold_the_same_symbol_independently(
    ledger: PositionAttributionLedger,
) -> None:
    """
    兩支策略各自的部位互不影響，加總才是帳戶層

    帳戶層合計由各策略帳加總得出，**不另外維護一份副本**——兩份紀錄必然漂移，
    而漂移的那一刻兩邊都看起來正確。
    """

    ledger.open_lot("A", make_fill(volume=2), PositionType.LONG)
    ledger.open_lot("B", make_fill(volume=3), PositionType.LONG)

    assert ledger.get_strategy_positions("A") == {("2330", "LONG"): 2}
    assert ledger.get_strategy_positions("B") == {("2330", "LONG"): 3}
    assert ledger.get_account_positions() == {("2330", "LONG"): 5}


def test_long_and_short_are_separate_keys(ledger: PositionAttributionLedger) -> None:
    """多空分開記：合併成淨額會讓一多一空看起來像沒有部位"""

    ledger.open_lot("A", make_fill(), PositionType.LONG)
    ledger.open_lot("B", make_fill(), PositionType.SHORT)

    assert ledger.get_account_positions() == {
        ("2330", "LONG"): 2,
        ("2330", "SHORT"): 2,
    }


# === 沖銷順序 ===
def test_close_is_fifo(
    ledger: PositionAttributionLedger, clock: List[datetime.datetime]
) -> None:
    """
    最早開倉的先平（FIFO），與回測的 `BasePositionManager.close_position()` 一致

    順序不同的話，已實現損益會對不上，而券商端的合計還是對的。
    """

    first: str = ledger.open_lot(
        "A", make_fill(volume=2, price=1000.0), PositionType.LONG
    )
    clock[0] = NOW + datetime.timedelta(days=1)
    second: str = ledger.open_lot(
        "A", make_fill(volume=2, price=1100.0), PositionType.LONG
    )

    closed: List[Tuple[str, int]] = ledger.close_lots("A", "2330", 3, PositionType.LONG)

    assert closed == [(first, 2), (second, 1)]
    assert ledger.get_strategy_positions("A") == {("2330", "LONG"): 1}


def test_close_only_touches_the_owning_strategy(
    ledger: PositionAttributionLedger,
) -> None:
    """
    平倉只沖銷該策略自己的 lot

    抓錯策略的 lot 會讓兩支策略的已實現損益互相污染，而券商端的合計還是對的。
    """

    ledger.open_lot("A", make_fill(volume=2), PositionType.LONG)
    ledger.open_lot("B", make_fill(volume=2), PositionType.LONG)

    ledger.close_lots("A", "2330", 2, PositionType.LONG)

    assert ledger.get_strategy_positions("A") == {}
    assert ledger.get_strategy_positions("B") == {("2330", "LONG"): 2}


def test_partial_close_reduces_the_lot(ledger: PositionAttributionLedger) -> None:
    """部分平倉扣減數量，lot 仍留在場上"""

    ledger.open_lot("A", make_fill(volume=5), PositionType.LONG)
    ledger.close_lots("A", "2330", 2, PositionType.LONG)

    assert ledger.get_strategy_positions("A") == {("2330", "LONG"): 3}


def test_closing_more_than_held_warns_but_does_not_raise(
    ledger: PositionAttributionLedger,
) -> None:
    """
    可沖銷量不足時只沖銷已有的，**不拋出**

    走到這裡代表歸屬帳與實際成交已經對不上，但那張平倉單已經成交了；
    拋出只會讓後面的成交也處理不了。差額由對帳在下一個段落抓出來。
    """

    ledger.open_lot("A", make_fill(volume=2), PositionType.LONG)
    closed: List[Tuple[str, int]] = ledger.close_lots("A", "2330", 5, PositionType.LONG)

    assert sum(volume for _, volume in closed) == 2
    assert ledger.get_strategy_positions("A") == {}


def test_close_respects_direction(ledger: PositionAttributionLedger) -> None:
    """
    只沖銷同方向的 lot

    不分方向的話，回補空單會去平掉多單的部位——兩邊的數量剛好都變了，
    而總淨額還是對的。
    """

    ledger.open_lot("A", make_fill(volume=2), PositionType.LONG)
    ledger.open_lot("A", make_fill(volume=2), PositionType.SHORT)

    ledger.close_lots("A", "2330", 2, PositionType.SHORT)

    assert ledger.get_strategy_positions("A") == {("2330", "LONG"): 2}


def test_lot_id_ordering_is_deterministic(
    ledger: PositionAttributionLedger,
) -> None:
    """
    同一天內的 lot_id 要遞增

    FIFO 的正確性建立在「同一天內 lot_id 遞增 ＝ 開倉先後」上；
    純隨機碼會讓沖銷順序變成不可預期，兩次重跑得到不同的已實現損益。
    """

    ids: List[str] = [
        ledger.open_lot("A", make_fill(), PositionType.LONG) for _ in range(3)
    ]

    assert ids == sorted(ids)


# === 未歸屬部位 ===
def test_broker_only_positions_become_unattributed(
    ledger: PositionAttributionLedger,
) -> None:
    """
    券商有、歸屬帳沒有的部位收進 `__unattributed__`

    **刻意不平均分配給各策略**：券商給的是合併部位，分不回策略；
    猜一個分法會讓兩支策略的已實現損益都是錯的，而合計仍然正確。
    """

    ledger.adopt_broker_positions(
        [
            BrokerPositionSnapshot(
                symbol="2330", direction=PositionType.LONG, volume=3, avg_price=980.0
            )
        ]
    )

    assert ledger.get_strategy_positions(UNATTRIBUTED_STRATEGY) == {("2330", "LONG"): 3}


def test_adopt_only_covers_the_gap(ledger: PositionAttributionLedger) -> None:
    """已歸屬的部分不重複收：只補差額"""

    ledger.open_lot("A", make_fill(volume=2), PositionType.LONG)
    ledger.adopt_broker_positions(
        [BrokerPositionSnapshot(symbol="2330", direction=PositionType.LONG, volume=5)]
    )

    assert ledger.get_strategy_positions(UNATTRIBUTED_STRATEGY) == {("2330", "LONG"): 3}
    assert ledger.get_account_positions() == {("2330", "LONG"): 5}


def test_adopt_does_nothing_when_already_balanced(
    ledger: PositionAttributionLedger,
) -> None:
    """歸屬帳已經對得上時不新增任何東西"""

    ledger.open_lot("A", make_fill(volume=2), PositionType.LONG)
    created: List[str] = ledger.adopt_broker_positions(
        [BrokerPositionSnapshot(symbol="2330", direction=PositionType.LONG, volume=2)]
    )

    assert created == []


# === 持有者查詢 ===
def test_holder_of_an_unheld_symbol_is_none(ledger: PositionAttributionLedger) -> None:
    """無人持有時才是 None"""

    assert ledger.get_holder("2330") is None


def test_holder_of_an_unattributed_position_is_not_none(
    ledger: PositionAttributionLedger,
) -> None:
    """
    **未歸屬部位要回 `__unattributed__`，不可回 None**

    回 None 等於放行——策略會對一檔券商端已有部位的標的開新倉，
    踩進的正是守門要防的三個坑：券商端反向沖銷、台股同日一買一賣被判成當沖、
    以及歸屬帳一對多的拆分。
    """

    ledger.adopt_broker_positions(
        [BrokerPositionSnapshot(symbol="2330", direction=PositionType.LONG, volume=1)]
    )

    assert ledger.get_holder("2330") == UNATTRIBUTED_STRATEGY


def test_holder_disappears_after_the_position_is_closed(
    ledger: PositionAttributionLedger,
) -> None:
    """平掉之後標的就釋放出來，其他策略可以接手"""

    ledger.open_lot("A", make_fill(volume=2), PositionType.LONG)
    ledger.close_lots("A", "2330", 2, PositionType.LONG)

    assert ledger.get_holder("2330") is None


# === 對帳 ===
def test_diff_reports_both_sides(ledger: PositionAttributionLedger) -> None:
    """
    對帳只回傳差異本身

    差異**無法歸因到單支策略**，所以由呼叫端走帳戶層降級——
    猜是哪一支的代價是讓真正有問題的那支繼續交易。
    """

    ledger.open_lot("A", make_fill(symbol="2330", volume=2), PositionType.LONG)
    ledger.open_lot("B", make_fill(symbol="2317", volume=1), PositionType.LONG)

    differences: Dict[Tuple[str, str], Tuple[int, int]] = ledger.diff_against_broker(
        [
            BrokerPositionSnapshot(
                symbol="2330", direction=PositionType.LONG, volume=2
            ),
            BrokerPositionSnapshot(
                symbol="2317", direction=PositionType.LONG, volume=3
            ),
        ]
    )

    assert differences == {("2317", "LONG"): (1, 3)}


def test_diff_catches_positions_missing_on_either_side(
    ledger: PositionAttributionLedger,
) -> None:
    """兩個方向的缺漏都要抓到：本地有券商沒有，也是差異"""

    ledger.open_lot("A", make_fill(symbol="2330", volume=2), PositionType.LONG)

    assert ledger.diff_against_broker([]) == {("2330", "LONG"): (2, 0)}
    assert ledger.diff_against_broker(
        [
            BrokerPositionSnapshot(
                symbol="2330", direction=PositionType.LONG, volume=2
            ),
            BrokerPositionSnapshot(
                symbol="2454", direction=PositionType.LONG, volume=1
            ),
        ]
    ) == {("2454", "LONG"): (0, 1)}

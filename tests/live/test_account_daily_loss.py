import datetime
from typing import Any, List, Optional

import pytest

from core.live.trader import LiveTrader
from core.models import BrokerAccountSnapshot

"""
帳戶層單日虧損：取券商端，取不到就明確略過

這道守門的接線早就完成，缺的一直是**輸入**——日頻模式下三個段落是三個獨立
行程，本地帳每次由原始開倉價重建，未實現恆為 0、當日已實現也不在這個行程裡，
加總出來永遠是 0。**看起來有在跑，實際永遠不觸發。**

2026-09-24 裁示改取券商端。本檔釘住的是那條取值路徑，尤其是**取不到時的行為**：
回 0 等於宣告「沒有虧損」，那正是原本失效的樣子。
"""

TODAY: datetime.date = datetime.date(2026, 9, 24)


class FakeTrade:
    """券商的一筆已平倉交易，只保留本檔用得到的欄位"""

    def __init__(self, pnl: float) -> None:
        self.pnl: float = pnl


class FakeBroker:
    """可腳本化的最小券商替身"""

    def __init__(
        self,
        trades: Optional[List[FakeTrade]] = None,
        raises: bool = False,
    ) -> None:
        self._trades: List[FakeTrade] = trades if trades is not None else []
        self._raises: bool = raises

    def get_realized_trades(self, date: datetime.date) -> List[FakeTrade]:
        if self._raises:
            raise RuntimeError("帳務查詢逾時")
        return list(self._trades)


class BrokerWithoutRealizedQuery:
    """沒有實作已實現損益查詢的閘道（`FakeBroker` 與舊版券商都可能如此）"""


def make_trader(snapshot: Optional[BrokerAccountSnapshot], broker: Any) -> LiveTrader:
    """只組出本檔要測的那兩個方法所需的最小狀態"""

    trader: LiveTrader = object.__new__(LiveTrader)
    trader.broker = broker
    trader.account_snapshot = snapshot
    trader._now = lambda: datetime.datetime(2026, 9, 24, 13, 20)
    return trader


def snapshot_with(unrealized: float) -> BrokerAccountSnapshot:
    return BrokerAccountSnapshot(
        available_balance=100_000.0, total_equity=500_000.0, unrealized_pnl=unrealized
    )


# === 正常取值 ===
def test_loss_combines_realized_and_unrealized() -> None:
    """
    當日虧損 ＝ −（已實現 ＋ 未實現）

    兩者分屬不同來源：未實現在帳務快照上（由持倉逐檔加總），
    當日已實現要另外查券商的已平倉交易。
    """

    trader: LiveTrader = make_trader(
        snapshot_with(-8_000.0), FakeBroker([FakeTrade(-12_000.0)])
    )

    assert trader._account_loss() == pytest.approx(20_000.0)


def test_profit_is_a_negative_loss() -> None:
    """賺錢時虧損為負數——門檻比較是 `loss > cap`，負數自然不會觸發"""

    trader: LiveTrader = make_trader(
        snapshot_with(5_000.0), FakeBroker([FakeTrade(3_000.0)])
    )

    assert trader._account_loss() == pytest.approx(-8_000.0)


def test_realized_sums_every_trade() -> None:
    """券商的已平倉查詢是逐筆的，要全部加總"""

    trades: List[FakeTrade] = [
        FakeTrade(-1_000.0),
        FakeTrade(-2_500.0),
        FakeTrade(500.0),
    ]
    trader: LiveTrader = make_trader(snapshot_with(0.0), FakeBroker(trades))

    assert trader._account_loss() == pytest.approx(3_000.0)


def test_no_trades_today_is_zero_realized() -> None:
    """今天沒有平倉交易＝已實現為 0，**這與「查不到」是兩回事**"""

    trader: LiveTrader = make_trader(snapshot_with(-4_000.0), FakeBroker([]))

    assert trader._account_loss() == pytest.approx(4_000.0)


def test_realized_query_uses_today() -> None:
    """查的是今天，不是昨天或整段區間"""

    seen: List[datetime.date] = []

    class RecordingBroker(FakeBroker):
        def get_realized_trades(self, date: datetime.date) -> List[FakeTrade]:
            seen.append(date)
            return []

    make_trader(snapshot_with(0.0), RecordingBroker())._account_loss()

    assert seen == [TODAY]


# === 取不到時：必須是 None，不可以是 0 ===
def test_missing_snapshot_returns_none_not_zero() -> None:
    """
    **取不到一律回 `None`，不可回 0**

    0 的語意是「沒有虧損」，而那正是這道檢查原本失效的樣子：
    看起來有在跑、實際永遠不觸發。回 `None` 才能讓呼叫端明確略過並留下紀錄。
    """

    trader: LiveTrader = make_trader(None, FakeBroker([FakeTrade(-50_000.0)]))

    assert trader._account_loss() is None


def test_broker_without_the_query_returns_none() -> None:
    """閘道沒有實作已實現損益查詢時回 None——不可只算未實現就當成全部"""

    trader: LiveTrader = make_trader(
        snapshot_with(-30_000.0), BrokerWithoutRealizedQuery()
    )

    assert trader._account_loss() is None


def test_failed_realized_query_returns_none() -> None:
    """
    查詢拋例外時回 None，且**不可讓段落起不來**

    帳務查詢失敗是暫時性的；但也不能當成「今天沒有已實現損益」——
    那會讓當日沖銷完的虧損一毛都不算。
    """

    trader: LiveTrader = make_trader(snapshot_with(-30_000.0), FakeBroker(raises=True))

    assert trader._account_loss() is None


def test_zero_pnl_is_not_confused_with_missing_data() -> None:
    """
    真的持平（0）與取不到（None）必須分得開

    兩者若都回 0，「資料拿不到」會被靜靜當成「今天沒虧」。
    """

    real_zero: Optional[float] = make_trader(
        snapshot_with(0.0), FakeBroker([])
    )._account_loss()
    missing: Optional[float] = make_trader(None, FakeBroker([]))._account_loss()

    assert real_zero == 0.0
    assert missing is None

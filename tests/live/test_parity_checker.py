import datetime
from pathlib import Path
from typing import Any, Dict, List

from core.dao.tw.live_trade_dao import LiveTradeDAO
from core.live.after_close import AfterCloseRunner
from core.live.report.parity_checker import (
    CATEGORY_CAPITAL_EXHAUSTED,
    CATEGORY_CROSS_STRATEGY_BLOCKED,
    CATEGORY_RISK_REJECTED,
    CATEGORY_SNAPSHOT_GAP,
    CATEGORY_UNEXPLAINED,
    CATEGORY_UNFILLED,
    ParityChecker,
    ParityDiff,
    compare,
)
from core.models import BaseOrder, StockOrder
from core.utils import Action, LiveOrderStatus, PositionType

"""
訊號 parity 比對

整份實盤規劃的核心假設是「同一支策略在回測與實盤產生相同訊號」，而**它不主動比對
就看不出來**：策略少送一張單不會有任何錯誤訊息。

每一筆差異都必須歸到一個類別。`UNEXPLAINED` 是唯一推播 CRITICAL 的類別——
把已知的制度性差異（快照口徑、跨策略守門、資金排擠）混進去，
真正的未解釋差異就會被雜訊淹沒。
"""

TODAY: datetime.date = datetime.date(2026, 9, 18)
NOW: datetime.datetime = datetime.datetime(2026, 9, 18, 14, 30)


def make_live_order(
    symbol: str = "2330",
    action: str = "Buy",
    volume: int = 2,
    status: str = "FILLED",
    position_type: str = "LONG",
) -> Dict[str, Any]:
    return {
        "symbol": symbol,
        "action": action,
        "volume": volume,
        "price": 1000.0,
        "status": status,
        "position_type": position_type,
        "strategy_name": "Alpha",
    }


def make_backtest_order(
    symbol: str = "2330", action: Action = Action.BUY, volume: int = 2
) -> BaseOrder:
    return StockOrder(
        stock_id=symbol,
        date=TODAY,
        action=action,
        position_type=PositionType.LONG,
        price=1000.0,
        volume=volume,
    )


def make_event(category: str, symbol: str = "2317") -> Dict[str, Any]:
    return {"category": category, "symbol": symbol, "strategy_name": "Alpha"}


def make_resolved_action(
    symbol: str = "2454", action: str = "Sell", volume: int = 2
) -> Dict[str, Any]:
    """一筆當天被處理掉的跨日待辦（前一交易日的平倉單沒成交）"""

    return {
        "symbol": symbol,
        "action": action,
        "volume": volume,
        "status": "DONE",
        "strategy_name": "Alpha",
    }


# === 純比對 ===
def test_identical_orders_produce_no_diff() -> None:
    """兩邊一致時不該產生任何差異——否則每天都有雜訊，真的問題就看不見了"""

    assert compare([make_live_order()], [make_backtest_order()], []) == []


def test_missing_live_order_is_unexplained_by_default() -> None:
    """
    回測有、實盤沒有，而且沒有任何風控事件 → `UNEXPLAINED`

    這就是「策略少送一張單」的症狀，也是本比對存在的理由。
    """

    diffs: List[ParityDiff] = compare([], [make_backtest_order()], [])

    assert len(diffs) == 1
    assert diffs[0].category == CATEGORY_UNEXPLAINED
    assert diffs[0].is_unexplained is True


def test_cross_strategy_block_is_not_unexplained() -> None:
    """
    被跨策略同標的守門（`CrossStrategyConflictGuard`）擋下是**預期的**差異

    多策略下實盤績效本來就會低於各策略單跑的回測；混進 `UNEXPLAINED`
    會讓真正的未解釋差異被雜訊淹沒。
    """

    diffs: List[ParityDiff] = compare(
        [],
        [make_backtest_order("2317")],
        [make_event("CROSS_STRATEGY_CONFLICT", "2317")],
    )

    assert [diff.category for diff in diffs] == [CATEGORY_CROSS_STRATEGY_BLOCKED]
    assert diffs[0].is_unexplained is False


def test_capital_exhausted_has_its_own_category() -> None:
    """
    資金排擠只在多策略下存在，而且**每天都會發生**

    比對基準是各策略單獨跑的回測，那裡它獨佔 `init_capital`；
    實盤要和其他策略搶同一筆餘額。沒有這一類的話它會天天汙染 `UNEXPLAINED`。
    """

    diffs: List[ParityDiff] = compare(
        [],
        [make_backtest_order("2317")],
        [make_event("CAPITAL_RESERVE_FAILED", "2317")],
    )

    assert [diff.category for diff in diffs] == [CATEGORY_CAPITAL_EXHAUSTED]


def test_extra_live_opening_order_is_a_snapshot_gap() -> None:
    """
    實盤多送一張**開倉**單 → 歸快照口徑

    13:20 的快照不含收盤集合競價，門檻型訊號可能在實盤成立、在回測不成立。
    """

    diffs: List[ParityDiff] = compare([make_live_order("2454")], [], [])

    assert [diff.category for diff in diffs] == [CATEGORY_SNAPSHOT_GAP]


def test_extra_live_closing_order_is_not_a_snapshot_gap() -> None:
    """
    實盤多送一張**平倉**單不能歸快照口徑

    平倉的依據是持倉不是門檻——多送一張平倉單是真的有問題。
    """

    diffs: List[ParityDiff] = compare(
        [make_live_order("2454", action="Sell", position_type="LONG")], [], []
    )

    assert [diff.category for diff in diffs] == [CATEGORY_UNEXPLAINED]


def test_next_day_cover_order_is_unfilled_not_unexplained() -> None:
    """
    次日補平單要歸 `UNFILLED`，不是未解釋

    D7：平倉未成交必須補，待辦寫進 `live_pending_action`，次日開盤段第一件事送出。
    那張單在回測沒有對應——回測前一天就已經平掉了——**但那不是訊號漂移**。
    沒有這條判定，每一張補平單都會推播一次 CRITICAL。
    """

    diffs: List[ParityDiff] = compare(
        [make_live_order("2454", action="Sell", position_type="LONG")],
        [],
        [],
        [make_resolved_action("2454", action="Sell")],
    )

    assert [diff.category for diff in diffs] == [CATEGORY_UNFILLED]
    assert "D7" in diffs[0].note


def test_cover_evidence_must_match_symbol_and_side() -> None:
    """
    證據要對得上標的**與買賣別**才算數

    只比標的的話，「2454 的買單」會被「2454 的賣單待辦」解釋掉——
    那是方向相反的兩張單，歸成同一類等於把真正的問題蓋掉。
    """

    wrong_symbol: List[ParityDiff] = compare(
        [make_live_order("2454", action="Sell", position_type="LONG")],
        [],
        [],
        [make_resolved_action("2330", action="Sell")],
    )
    wrong_side: List[ParityDiff] = compare(
        [make_live_order("2454", action="Sell", position_type="LONG")],
        [],
        [],
        [make_resolved_action("2454", action="Buy")],
    )

    assert [diff.category for diff in wrong_symbol] == [CATEGORY_UNEXPLAINED]
    assert [diff.category for diff in wrong_side] == [CATEGORY_UNEXPLAINED]


def test_snapshot_gap_still_wins_over_unfilled() -> None:
    """
    開倉單仍先歸快照口徑

    兩者實際上互斥（補平是平倉單、快照口徑只認開倉單），但判定順序要照
    模組宣告的優先序，不可因為多了一條證據就把既有歸因蓋掉。
    """

    diffs: List[ParityDiff] = compare(
        [make_live_order("2454")],
        [],
        [],
        [make_resolved_action("2454", action="Buy")],
    )

    assert [diff.category for diff in diffs] == [CATEGORY_SNAPSHOT_GAP]


def test_volume_mismatch_is_reported_as_one_diff() -> None:
    """
    數量不同是**同一張單的部分差異**，不是「少一張＋多一張」

    比對鍵刻意不含數量：含了的話張數算錯會被拆成兩筆，真正的問題反而看不出來。
    """

    diffs: List[ParityDiff] = compare(
        [make_live_order(volume=1)], [make_backtest_order(volume=3)], []
    )

    assert len(diffs) == 1
    assert "實盤 1、回測 3" in diffs[0].note


def test_rejected_order_explains_the_volume_gap() -> None:
    """整批被券商退單造成的數量差異歸 `RISK_REJECTED`，不是未解釋"""

    diffs: List[ParityDiff] = compare(
        [make_live_order(volume=2, status=LiveOrderStatus.REJECTED.value)],
        [make_backtest_order(volume=5)],
        [],
    )

    assert [diff.category for diff in diffs] == [CATEGORY_RISK_REJECTED]


# === 落地 ===
def test_check_writes_the_table_and_the_csv(dao: LiveTradeDAO, tmp_path: Path) -> None:
    """差異要同時進 `live_parity_diff` 與 CSV——CSV 是給人看的，表是給查的"""

    dao.upsert_order(
        {
            "client_order_id": "run1-1",
            "run_id": "run1",
            "strategy_name": "Alpha",
            "symbol": "2330",
            "action": "Buy",
            "position_type": "LONG",
            "price": 1000.0,
            "volume": 1,
            "status": "FILLED",
            "created_at": NOW,
        }
    )
    dao.conn.commit()

    checker: ParityChecker = ParityChecker(
        dao,
        run_backtest=lambda name, date: [make_backtest_order(volume=3)],
        output_root=tmp_path,
    )
    result: Dict[str, List[ParityDiff]] = checker.check(TODAY)

    assert len(result["Alpha"]) == 1
    rows = dao.conn.execute(
        "SELECT category FROM live_parity_diff WHERE strategy_name = 'Alpha'"
    ).fetchall()
    assert len(rows) == 1
    assert (tmp_path / "Alpha" / f"{TODAY.isoformat()}_parity.csv").exists()


def test_backtest_failure_does_not_pass_silently(
    dao: LiveTradeDAO, tmp_path: Path
) -> None:
    """
    回測跑不起來時要留下 `UNEXPLAINED`，**不是靜靜跳過**

    「比對沒跑」與「比對通過」長得一樣，那是最典型的假綠燈。
    """

    dao.upsert_order(
        {
            "client_order_id": "run1-1",
            "run_id": "run1",
            "strategy_name": "Alpha",
            "symbol": "2330",
            "action": "Buy",
            "position_type": "LONG",
            "price": 1000.0,
            "volume": 1,
            "status": "FILLED",
            "created_at": NOW,
        }
    )
    dao.conn.commit()

    def exploding(name: str, date: datetime.date) -> List[BaseOrder]:
        raise RuntimeError("缺 tw_stock.db")

    checker: ParityChecker = ParityChecker(
        dao, run_backtest=exploding, output_root=tmp_path
    )
    diffs: List[ParityDiff] = checker.check(TODAY)["Alpha"]

    assert [diff.category for diff in diffs] == [CATEGORY_UNEXPLAINED]
    assert "缺 tw_stock.db" in diffs[0].note


# === 接線：寫好了就要有人呼叫 ===
class _RecordingNotifier:
    """記下推播內容；只驗「有沒有送出」與等級"""

    def __init__(self) -> None:
        self.sent: List[tuple] = []

    def send(self, level: Any, title: str, body: str) -> None:
        self.sent.append((getattr(level, "value", level), title, body))


def make_runner(dao: LiveTradeDAO, checker: Any, notifier: Any) -> AfterCloseRunner:
    """只組出跑得動 `check_signal_parity()` 的最小集合"""

    return AfterCloseRunner(
        data_feeds=[],
        broker=None,
        order_manager=None,
        account_sync=None,
        reconciler=None,
        reporter=None,
        mode_state=None,
        dao=dao,
        run_id="run1",
        notifier=notifier,
        now_provider=lambda: NOW,
        parity_checker=checker,
    )


def test_unexplained_diff_is_pushed_as_critical(
    dao: LiveTradeDAO, tmp_path: Path
) -> None:
    """
    未解釋的差異要推播 `CRITICAL`

    訊號漂移不會有任何錯誤訊息，只會讓回測績效靜靜失去參考價值——
    不推播等於沒有比對。
    """

    dao.upsert_order(
        {
            "client_order_id": "run1-1",
            "run_id": "run1",
            "strategy_name": "Alpha",
            "symbol": "2330",
            "action": "Buy",
            "position_type": "LONG",
            "price": 1000.0,
            "volume": 1,
            "status": "FILLED",
            "created_at": NOW,
        }
    )
    dao.conn.commit()

    notifier: _RecordingNotifier = _RecordingNotifier()
    checker: ParityChecker = ParityChecker(
        dao,
        run_backtest=lambda name, date: [make_backtest_order("2454")],
        output_root=tmp_path,
    )

    total: int = make_runner(dao, checker, notifier).check_signal_parity(TODAY)

    assert total >= 1
    assert notifier.sent and notifier.sent[0][0] == "CRITICAL"


def test_clean_day_does_not_push(dao: LiveTradeDAO, tmp_path: Path) -> None:
    """沒有未解釋差異就不推播——每天都收到告警的話，沒有人會再看它"""

    notifier: _RecordingNotifier = _RecordingNotifier()
    checker: ParityChecker = ParityChecker(
        dao, run_backtest=lambda name, date: [], output_root=tmp_path
    )

    assert make_runner(dao, checker, notifier).check_signal_parity(TODAY) == 0
    assert notifier.sent == []


def test_missing_checker_does_not_crash_the_after_close(dao: LiveTradeDAO) -> None:
    """未注入比對器時記 warning 並回 0，不可讓盤後作業整段掛掉"""

    assert make_runner(dao, None, None).check_signal_parity(TODAY) == 0

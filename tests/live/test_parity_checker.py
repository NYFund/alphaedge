import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import pytest

from core.dao.tw.live_trade_dao import LiveTradeDAO
from core.live.after_close import AfterCloseRunner
from core.live.attribution.position_ledger import ACCOUNT_STRATEGY
from core.live.factory import make_order_phase_lookup
from core.live.report.parity_checker import (
    CATEGORY_CAPITAL_EXHAUSTED,
    CATEGORY_CROSS_STRATEGY_BLOCKED,
    CATEGORY_EXIT_PENDING_SLOT,
    CATEGORY_GROUP,
    CATEGORY_INTRADAY_PATH,
    CATEGORY_LIMIT_UNFILLED,
    CATEGORY_LOCKED_AT_LIMIT,
    CATEGORY_QUOTE_STALE,
    CATEGORY_RISK_REJECTED,
    CATEGORY_SEGMENT_ABORTED,
    CATEGORY_SEND_FAILED,
    CATEGORY_SNAPSHOT_GAP,
    CATEGORY_UNEXPLAINED,
    CATEGORY_UNFILLED,
    GROUP_EXECUTION,
    GROUP_SIGNAL,
    GROUP_STRUCTURAL,
    ClosingLimitLookup,
    ParityChecker,
    ParityDiff,
    aborted_phases,
    compare,
    count_by_group,
    segments_ran_cleanly,
    stale_quote_scope,
)
from core.models import BaseOrder, StockOrder
from core.utils import Action, ExecutionTiming, LiveHook, LiveOrderStatus, PositionType
from strategies.stock.volume_breakout_momentum_strategy import (
    VolumeBreakoutMomentumStrategy,
)

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
    """組一筆實盤委託紀錄，預設為全部成交的 2330 買單"""

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
    """組一張回測委託"""

    return StockOrder(
        stock_id=symbol,
        date=TODAY,
        action=action,
        position_type=PositionType.LONG,
        price=1000.0,
        volume=volume,
    )


def make_event(category: str, symbol: str = "2317") -> Dict[str, Any]:
    """組一筆 Alpha 的風控事件"""

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
        [make_event("CROSS_STRATEGY_BLOCKED", "2317")],
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
        [make_event("CAPITAL_EXHAUSTED", "2317")],
    )

    assert [diff.category for diff in diffs] == [CATEGORY_CAPITAL_EXHAUSTED]
    assert "CAPITAL_EXHAUSTED" in diffs[0].note


@pytest.mark.parametrize(
    "event_category",
    [
        "MARGIN_INSUFFICIENT",
        "EXECUTION_UNAVAILABLE",
        "PRICE_LIMIT",
        "PRICE_DEVIATION",
        "ORDER_RATE",
        "KILL_SWITCH",
    ],
)
def test_every_pre_send_block_is_attributed_not_filed_as_snapshot_gap(
    event_category: str,
) -> None:
    """
    引擎在送出前擋下的每一種原因都要歸到制度性差異，並在 note 寫出是哪一條

    對照表曾只認得四種名字、其中三個引擎從未寫出：被保證金閘、執行層或風控擋下的
    開倉單，在段落正常結束時一律被歸成「快照口徑」，真正的原因被蓋掉。
    """

    diffs: List[ParityDiff] = compare(
        [],
        [make_backtest_order("2317")],
        [make_event(event_category, "2317")],
        ran_cleanly=True,
    )

    assert [diff.category for diff in diffs] == [CATEGORY_RISK_REJECTED]
    assert diffs[0].note == f"實盤在送出前被 {event_category} 擋下"


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
# === 送出但沒成交（執行差異）===
def unfilled_order(
    execution_style: Optional[str],
    order_type: str,
    status: str = "CANCELLED",
    filled_volume: int = 0,
) -> Dict[str, Any]:
    """兩邊都送了、實盤沒成交的委託"""

    row: Dict[str, Any] = make_live_order(status=status)
    row.update(
        {
            "execution_style": execution_style,
            "order_type": order_type,
            "filled_volume": filled_volume,
        }
    )
    return row


def closing_at(close: float, limit: float) -> ClosingLimitLookup:
    """假的收盤查詢：任何標的都回同一組（收盤價，漲跌停價）"""

    def lookup(symbol: str, side: str) -> Optional[Tuple[float, float]]:
        return (close, limit)

    return lookup


LOCKED: ClosingLimitLookup = closing_at(1100.0, 1100.0)


@pytest.mark.parametrize(
    "style, order_type, expected",
    [
        ("LIMIT", "ROD", CATEGORY_LIMIT_UNFILLED),
        ("MARKET", "ROD", CATEGORY_LOCKED_AT_LIMIT),
        ("MARKET", "IOC", CATEGORY_UNFILLED),
        (None, "ROD", CATEGORY_UNFILLED),
    ],
    ids=["limit", "locked-at-limit", "ioc-protection", "legacy-row"],
)
def test_unfilled_order_is_classified_by_execution_style(
    style: Optional[str], order_type: str, expected: str
) -> None:
    """
    兩邊都送了、實盤沒成交：依執行方式分類，不是未解釋

    回測一律以策略給的價成交；照價掛單沒等到價、收盤鎖漲停排不到，
    都是已知的執行差異，要分開計數才量得出回測偏樂觀多少。
    """

    diffs: List[ParityDiff] = compare(
        [unfilled_order(style, order_type)],
        [make_backtest_order()],
        [],
        closing_limit=LOCKED,
    )

    assert [diff.category for diff in diffs] == [expected]
    assert "實盤成交 0／2" in diffs[0].note


def test_auction_order_is_not_locked_when_the_close_is_below_the_limit() -> None:
    """
    集合競價掛漲停沒成交、收盤卻沒鎖漲停：歸 `UNFILLED` 並寫出兩個價

    2026-10-08 演練的 4157：漲停 9.40、收盤 9.33，14 張買單在真實市場會以 9.33 成交，
    卻因為只看委託屬性被歸成「鎖漲停」，模擬環境不撮合集合競價的跡象就被蓋掉了。
    """

    diffs: List[ParityDiff] = compare(
        [unfilled_order("MARKET", "ROD")],
        [make_backtest_order()],
        [],
        closing_limit=closing_at(9.33, 9.4),
    )

    assert [diff.category for diff in diffs] == [CATEGORY_UNFILLED]
    assert "收盤 9.33 未鎖漲停（漲停 9.4）" in diffs[0].note


def test_sell_auction_order_reports_the_limit_down() -> None:
    """賣單比的是跌停，說明也要寫跌停"""

    row: Dict[str, Any] = unfilled_order("MARKET", "ROD")
    row["action"] = "Sell"
    order: BaseOrder = make_backtest_order()
    order.action = Action.SELL

    diffs: List[ParityDiff] = compare(
        [row], [order], [], closing_limit=closing_at(91.0, 90.0)
    )

    assert [diff.category for diff in diffs] == [CATEGORY_UNFILLED]
    assert "未鎖跌停（跌停 90.0）" in diffs[0].note


def test_auction_order_without_closing_data_is_not_guessed_as_locked() -> None:
    """查不到收盤（或沒有注入查詢）時不猜鎖住，歸 `UNFILLED` 並寫明無法確認"""

    def missing(symbol: str, side: str) -> Optional[Tuple[float, float]]:
        return None

    for lookup in (missing, None):
        diffs: List[ParityDiff] = compare(
            [unfilled_order("MARKET", "ROD")],
            [make_backtest_order()],
            [],
            closing_limit=lookup,
        )

        assert [diff.category for diff in diffs] == [CATEGORY_UNFILLED]
        assert "無法確認收盤是否鎖住" in diffs[0].note


def test_checker_passes_the_trading_day_to_the_closing_lookup(
    dao: LiveTradeDAO, tmp_path: Path
) -> None:
    """比對器把交易日帶給收盤查詢；查詢判定鎖住才歸 `LOCKED_AT_LIMIT`"""

    calls: List[Tuple[str, str, datetime.date]] = []

    def closing_limit(
        symbol: str, side: str, run_date: datetime.date
    ) -> Optional[Tuple[float, float]]:
        calls.append((symbol, side, run_date))
        return (1100.0, 1100.0)

    dao.upsert_order(
        {
            "client_order_id": "run1-1",
            "run_id": "run1",
            "strategy_name": "Alpha",
            "symbol": "2330",
            "action": "Buy",
            "position_type": "LONG",
            "price": 1100.0,
            "volume": 1,
            "order_type": "ROD",
            "execution_style": "MARKET",
            "status": "CANCELLED",
            "filled_volume": 0,
            "created_at": NOW,
        }
    )
    dao.conn.commit()
    checker: ParityChecker = ParityChecker(
        dao,
        run_backtest=lambda name, day: [make_backtest_order(volume=1)],
        output_root=tmp_path,
        strategy_names=["Alpha"],
        closing_limit=closing_limit,
    )

    result: Dict[str, List[ParityDiff]] = checker.check(TODAY)

    assert [diff.category for diff in result["Alpha"]] == [CATEGORY_LOCKED_AT_LIMIT]
    assert calls == [("2330", "Buy", TODAY)]


def test_partial_fill_is_also_an_execution_diff() -> None:
    """只成交一部分也算：差的那幾張回測照樣成交"""

    diffs: List[ParityDiff] = compare(
        [unfilled_order("MARKET", "ROD", status="PARTIALLY_FILLED", filled_volume=1)],
        [make_backtest_order()],
        [],
        closing_limit=LOCKED,
    )

    assert [diff.category for diff in diffs] == [CATEGORY_LOCKED_AT_LIMIT]
    assert "實盤成交 1／2" in diffs[0].note


def test_order_stuck_in_pending_submit_is_not_reported_as_clean() -> None:
    """
    狀態停在 PENDING_SUBMIT 的委託也是沒成交，不可判成沒有差異

    2026-10-07 演練：三張單被撤掉、本地狀態卻停在 PENDING_SUBMIT；
    兩邊數量相同，若不看成交量，報表會顯示當天完全一致。
    """

    diffs: List[ParityDiff] = compare(
        [unfilled_order("MARKET", "ROD", status="PENDING_SUBMIT")],
        [make_backtest_order()],
        [],
        closing_limit=LOCKED,
    )

    assert [diff.category for diff in diffs] == [CATEGORY_LOCKED_AT_LIMIT]


def test_filled_order_is_not_a_diff() -> None:
    """成交了就沒有差異"""

    row: Dict[str, Any] = unfilled_order("MARKET", "ROD", "FILLED", filled_volume=2)

    assert compare([row], [make_backtest_order()], []) == []


@pytest.mark.parametrize("status", ["REJECTED", "FAILED"])
def test_send_failure_with_the_same_volume_is_an_execution_diff(status: str) -> None:
    """
    數量與回測相同、但送單失敗或被拒：策略決定相同，是執行沒做到

    2026-10-06 三張單在轉換層就失敗，補比卻判成「完全一致」。
    """

    row: Dict[str, Any] = unfilled_order("MARKET", "ROD", status=status)
    row["reject_reason"] = "price_type 尚未決定"

    diffs: List[ParityDiff] = compare([row], [make_backtest_order()], [])

    assert [diff.category for diff in diffs] == [CATEGORY_SEND_FAILED]
    assert diffs[0].note == "price_type 尚未決定"


def test_every_category_belongs_to_a_group() -> None:
    """
    每個類別都有組別

    新增類別卻忘了登記的話，它會被計入訊號差異（最需要人看的那一組）；
    這條讓忘記登記在測試就被抓到，而不是靠計數時的退路。
    """

    import core.live.report.parity_checker as module

    categories: List[str] = [
        value
        for name, value in vars(module).items()
        if name.startswith("CATEGORY_") and isinstance(value, str)
    ]

    assert sorted(set(categories) - set(CATEGORY_GROUP)) == []


def test_counts_are_split_into_three_groups() -> None:
    """訊號、制度性、執行三組分開計數；沒有差異的組別也列出 0"""

    def diff(category: str) -> ParityDiff:
        return ParityDiff(1, "2330", "Buy", category, "", "", "")

    counts: Dict[str, int] = count_by_group(
        [
            diff(CATEGORY_UNEXPLAINED),
            diff(CATEGORY_SNAPSHOT_GAP),
            diff(CATEGORY_SEND_FAILED),
            diff(CATEGORY_LOCKED_AT_LIMIT),
        ]
    )

    assert counts == {GROUP_SIGNAL: 1, GROUP_STRUCTURAL: 1, GROUP_EXECUTION: 2}
    assert count_by_group([]) == {
        GROUP_SIGNAL: 0,
        GROUP_STRUCTURAL: 0,
        GROUP_EXECUTION: 0,
    }


def test_loaded_strategy_without_orders_is_still_compared(
    dao: LiveTradeDAO, tmp_path: Path
) -> None:
    """
    本行程載入的策略當天一張委託都沒送，照樣比

    只比有委託的策略的話，段落中止、整天沒送單而回測會開倉的那一天完全看不到差異
    （2026-10-07 期貨尾盤段中止就是這樣）。
    """

    checker: ParityChecker = ParityChecker(
        dao,
        lambda name, day: [make_backtest_order("2330")],
        output_root=tmp_path,
        strategy_names=["Alpha"],
    )

    result: Dict[str, List[ParityDiff]] = checker.check(TODAY)

    assert [diff.category for diff in result["Alpha"]] == [CATEGORY_UNEXPLAINED]
    assert result["Alpha"][0].live_detail == "（實盤沒有送出這張單）"


# === 回測多出的開倉單（反方向的快照口徑）===
def clean_run(phase: str = "close", end_reason: str = "正常結束") -> Dict[str, Any]:
    """組一筆沒有異常的段落執行紀錄"""

    return {
        "phase": phase,
        "end_reason": end_reason,
        "strategy_params_json": '{"strategies": ["Alpha"]}',
    }


def test_backtest_only_opening_is_a_snapshot_gap_when_the_day_ran_cleanly() -> None:
    """
    段落正常、無 CRITICAL 時，回測多出的開倉單歸快照口徑

    收盤價觸發門檻、實盤決策時的快照沒觸發——與「實盤多出開倉單」同一個成因，
    只是方向相反。2026-10-05 期貨就是這樣（回測買 TX202610 ×2、實盤沒送）。
    """

    diffs: List[ParityDiff] = compare([], [make_backtest_order()], [], (), True)

    assert [diff.category for diff in diffs] == [CATEGORY_SNAPSHOT_GAP]
    assert "段落正常結束" in diffs[0].note


def test_backtest_only_opening_stays_unexplained_when_the_day_did_not_run_cleanly() -> (
    None
):
    """段落中止或出過 CRITICAL 的日子，實盤沒送單可能是程式出事，不可歸成快照口徑"""

    diffs: List[ParityDiff] = compare([], [make_backtest_order()], [], (), False)

    assert [diff.category for diff in diffs] == [CATEGORY_UNEXPLAINED]


def test_backtest_only_closing_is_never_a_snapshot_gap() -> None:
    """平倉依據的是持倉不是門檻；回測多出平倉單不是口徑問題"""

    closing: BaseOrder = make_backtest_order(action=Action.SELL)

    diffs: List[ParityDiff] = compare([], [closing], [], (), True)

    assert [diff.category for diff in diffs] == [CATEGORY_UNEXPLAINED]


@pytest.mark.parametrize(
    "runs, events, expected",
    [
        ([clean_run()], [], True),
        ([], [], False),
        ([clean_run(end_reason="例外中止：OrderStateError")], [], False),
        ([clean_run(), clean_run("open", "對帳不一致")], [], False),
        ([clean_run()], [{"severity": "CRITICAL"}], False),
        ([clean_run()], [{"severity": "WARN"}], True),
        # 盤後與補比不呼叫策略鉤子，它們正不正常與訊號無關
        ([clean_run(), clean_run("after_close", "例外中止：X")], [], True),
        # 2026-10-08 以前的紀錄沒有策略清單：判不出段落屬於誰，不算
        ([{"phase": "close", "end_reason": "正常結束"}], [], False),
    ],
    ids=[
        "clean",
        "no-run",
        "aborted",
        "one-of-two-bad",
        "critical-event",
        "warn-event",
        "after-close-ignored",
        "legacy-row",
    ],
)
def test_segments_ran_cleanly(
    runs: List[Dict[str, Any]], events: List[Dict[str, Any]], expected: bool
) -> None:
    assert segments_ran_cleanly("Alpha", runs, events) is expected


def test_recheck_replaces_the_previous_result(
    dao: LiveTradeDAO, tmp_path: Path
) -> None:
    """
    同一天重比時清掉舊結果，不殘留舊序號

    盤後段與次日補比、手動重跑都可能比同一天；以（日期、策略、序號）upsert
    只會覆寫前幾筆，差異變少時舊的那幾筆會留在表裡。
    """

    dao.upsert_order(
        {
            "client_order_id": "run1-0001",
            "run_id": "run1",
            "strategy_name": "Alpha",
            "symbol": "2330",
            "action": "Buy",
            "position_type": "LONG",
            "price": 1000.0,
            "volume": 2,
            "status": "FILLED",
            "filled_volume": 2,
            "created_at": NOW,
        }
    )
    backtest: List[List[BaseOrder]] = [
        [make_backtest_order("2317"), make_backtest_order("2454")],
        [make_backtest_order("2330")],
    ]
    checker: ParityChecker = ParityChecker(
        dao, lambda name, day: backtest.pop(0), output_root=tmp_path
    )

    checker.check(TODAY)
    checker.check(TODAY)

    rows: List[Any] = dao.conn.execute(
        "SELECT COUNT(*) FROM live_parity_diff WHERE strategy_name = 'Alpha'"
    ).fetchall()
    assert rows == [(0,)]


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


def insert_order(dao: LiveTradeDAO, client_order_id: str, strategy_name: str) -> None:
    """寫一筆當日委託"""

    dao.upsert_order(
        {
            "client_order_id": client_order_id,
            "run_id": "run1",
            "strategy_name": strategy_name,
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


def test_only_strategies_loaded_in_this_process_are_checked(
    dao: LiveTradeDAO, tmp_path: Path
) -> None:
    """
    紀錄庫裡另一個行程的策略不比對

    2026-10-06：股票與期貨的盤後段是兩個行程、共用同一個紀錄庫，期貨行程讀到股票策略的委託，
    跑不出它的回測，記了一筆假的 UNEXPLAINED 並發 CRITICAL。
    """

    insert_order(dao, "run1-1", "Alpha")
    insert_order(dao, "run2-1", "Beta")

    def backtest(name: str, date: datetime.date) -> List[BaseOrder]:
        if name != "Alpha":
            raise RuntimeError(f"本次執行沒有載入策略 {name}")
        return [make_backtest_order(volume=1)]

    checker: ParityChecker = ParityChecker(
        dao, run_backtest=backtest, output_root=tmp_path, strategy_names=["Alpha"]
    )
    result: Dict[str, List[ParityDiff]] = checker.check(TODAY)

    assert set(result) == {"Alpha"}
    assert not (tmp_path / "Beta").exists()


def test_other_process_results_are_not_overwritten(
    dao: LiveTradeDAO, tmp_path: Path
) -> None:
    """
    另一個行程寫好的差異不可被覆寫

    差異以（日期、策略、序號）覆寫；2026-10-06 期貨行程那筆假的第 1 筆蓋掉了股票行程對 1711 的結果。
    """

    insert_order(dao, "run2-1", "Beta")
    dao.upsert_parity_diff(
        {
            "date": TODAY,
            "strategy_name": "Beta",
            "seq": 1,
            "symbol": "2330",
            "side": "Buy",
            "category": "SNAPSHOT_GAP",
            "live_detail": "股票行程寫的",
            "backtest_detail": "",
            "note": "",
        }
    )
    dao.commit()

    checker: ParityChecker = ParityChecker(
        dao,
        run_backtest=lambda name, date: [],
        output_root=tmp_path,
        strategy_names=["Alpha"],
    )
    checker.check(TODAY)

    rows = dao.conn.execute(
        "SELECT category, live_detail FROM live_parity_diff WHERE strategy_name = 'Beta'"
    ).fetchall()
    assert [tuple(row) for row in rows] == [("SNAPSHOT_GAP", "股票行程寫的")]


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


# === 段落中止與帳戶層擋單 ===
def close_phase(order: BaseOrder) -> Set[str]:
    """測試用：每張回測委託都該在尾盤段送出"""

    return {"close"}


def test_order_of_an_aborted_segment_is_segment_aborted() -> None:
    """
    回測有、實盤沒有，而該送出的段落當天非正常結束：歸段落中止，不再發 CRITICAL

    2026-10-14 股票尾盤段被資料新鮮度擋下，回測的 1301、1326、1528 開倉單全落在未解釋。
    """

    diffs: List[ParityDiff] = compare(
        [],
        [make_backtest_order()],
        [],
        (),
        False,
        aborted_phases={"close"},
        order_phases=close_phase,
    )

    assert [diff.category for diff in diffs] == [CATEGORY_SEGMENT_ABORTED]
    assert "close" in diffs[0].note


def test_other_aborted_segment_does_not_explain_the_order() -> None:
    """開盤段中止不能拿來解釋尾盤段該送的單——那一段其實正常跑了，原因未知"""

    diffs: List[ParityDiff] = compare(
        [],
        [make_backtest_order()],
        [],
        (),
        False,
        aborted_phases={"open"},
        order_phases=close_phase,
    )

    assert [diff.category for diff in diffs] == [CATEGORY_UNEXPLAINED]


def test_order_spanning_a_healthy_segment_is_not_segment_aborted() -> None:
    """該送出的段落有一個正常跑完（例如停損段落沒中止）就不歸段落中止"""

    diffs: List[ParityDiff] = compare(
        [],
        [make_backtest_order(action=Action.SELL)],
        [],
        (),
        False,
        aborted_phases={"close"},
        order_phases=lambda order: {"close", "open"},
    )

    assert [diff.category for diff in diffs] == [CATEGORY_UNEXPLAINED]


def test_without_phase_lookup_nothing_is_segment_aborted() -> None:
    """判不出段落時不猜：維持原本的類別"""

    diffs: List[ParityDiff] = compare(
        [], [make_backtest_order()], [], (), False, aborted_phases={"close"}
    )

    assert [diff.category for diff in diffs] == [CATEGORY_UNEXPLAINED]


def test_risk_event_still_wins_over_segment_aborted() -> None:
    """有事件就以事件為準：被風控擋下的單即使段落後來中止，原因也是風控"""

    diffs: List[ParityDiff] = compare(
        [],
        [make_backtest_order("2317")],
        [make_event("SINGLE_ORDER_AMOUNT", "2317")],
        (),
        False,
        aborted_phases={"close"},
        order_phases=close_phase,
    )

    assert [diff.category for diff in diffs] == [CATEGORY_RISK_REJECTED]


# === 報價日期不是今天 ===
def test_dropped_stale_symbol_is_quote_stale_and_others_are_not() -> None:
    """
    部分剔除：只有被剔除的那檔歸報價過期，其他標的的差異照常判定

    2026-10-23 模擬環境的收盤段快照停在前一日；剔除之後回測有、實盤沒有的單
    原因已由 `QUOTE_STALE` 事件報過，不該再當成未解釋發一次 CRITICAL。
    """

    diffs: List[ParityDiff] = compare(
        [],
        [make_backtest_order("2317"), make_backtest_order("2330")],
        [],
        (),
        False,
        stale_symbols={"2317"},
    )

    assert [(diff.symbol, diff.category) for diff in diffs] == [
        ("2317", CATEGORY_QUOTE_STALE),
        ("2330", CATEGORY_UNEXPLAINED),
    ]


def test_skipped_segment_explains_only_its_own_orders() -> None:
    """整段跳過：只解釋該在那個段落送出的單，別的段落跳過不能拿來解釋"""

    skipped: List[ParityDiff] = compare(
        [],
        [make_backtest_order()],
        [],
        (),
        False,
        order_phases=close_phase,
        stale_phases={"close"},
    )
    other: List[ParityDiff] = compare(
        [],
        [make_backtest_order()],
        [],
        (),
        False,
        order_phases=close_phase,
        stale_phases={"open"},
    )

    assert [diff.category for diff in skipped] == [CATEGORY_QUOTE_STALE]
    assert "close" in skipped[0].note
    assert [diff.category for diff in other] == [CATEGORY_UNEXPLAINED]


def test_risk_event_still_wins_over_quote_stale() -> None:
    """有擋單事件就以事件為準"""

    diffs: List[ParityDiff] = compare(
        [],
        [make_backtest_order("2317")],
        [make_event("SINGLE_ORDER_AMOUNT", "2317")],
        (),
        False,
        stale_symbols={"2317"},
    )

    assert [diff.category for diff in diffs] == [CATEGORY_RISK_REJECTED]


def test_stale_quote_scope() -> None:
    """帶標的是部分剔除；不帶標的以 `run_id` 對到段落，對不到的不算；其他類別不看"""

    events: List[Dict[str, Any]] = [
        {"category": "QUOTE_STALE", "symbol": "2317", "run_id": "run-close"},
        {"category": "QUOTE_STALE", "symbol": None, "run_id": "run-close"},
        {"category": "QUOTE_STALE", "symbol": None, "run_id": "run-unknown"},
        {"category": "MAX_HOLDINGS", "symbol": "2454", "run_id": "run-open"},
    ]
    runs: List[Dict[str, Any]] = [
        {"run_id": "run-close", "phase": "close"},
        {"run_id": "run-open", "phase": "open"},
    ]

    assert stale_quote_scope(events, runs) == ({"2317"}, {"close"})


def test_checker_passes_the_stale_quote_scope(
    dao: LiveTradeDAO, tmp_path: Path
) -> None:
    """接線：比對器以當天的 `QUOTE_STALE` 事件與執行紀錄算出範圍並傳進比對"""

    dao.insert_run(
        {
            "run_id": "run-close",
            "started_at": NOW,
            "phase": "close",
            "simulation": 1,
            "end_reason": "正常結束",
            "strategy_params_json": '{"strategies": ["Alpha"]}',
        }
    )
    dao.insert_risk_event(
        {
            "run_id": "run-close",
            "strategy_name": "Alpha",
            "severity": "CRITICAL",
            "category": "QUOTE_STALE",
            "symbol": None,
            "message": "Alpha 有 1／1 檔報價的日期不是今天",
            "occurred_at": NOW,
        }
    )
    checker: ParityChecker = ParityChecker(
        dao,
        lambda name, day: [make_backtest_order("2330")],
        output_root=tmp_path,
        strategy_names=["Alpha"],
        order_phases=lambda name, order: {"close"},
    )

    result: Dict[str, List[ParityDiff]] = checker.check(TODAY)

    assert [diff.category for diff in result["Alpha"]] == [CATEGORY_QUOTE_STALE]


@pytest.mark.parametrize(
    "runs, expected",
    [
        ([clean_run(end_reason="例外中止：DataFreshnessError")], {"close"}),
        ([clean_run()], set()),
        # 重跑成功：那一段的決定還是做了
        ([clean_run(end_reason="例外中止：X"), clean_run()], set()),
        ([clean_run("open", "例外中止：ConnectionError"), clean_run()], {"open"}),
        # 盤後與補比不呼叫策略鉤子
        ([clean_run("after_close", "例外中止：X")], set()),
        # 認不出策略的舊紀錄不算
        ([{"phase": "close", "end_reason": "例外中止：X"}], set()),
        ([], set()),
    ],
    ids=["aborted", "clean", "rerun-ok", "open-only", "after-close", "legacy", "none"],
)
def test_aborted_phases(runs: List[Dict[str, Any]], expected: Set[str]) -> None:
    assert aborted_phases("Alpha", runs) == expected


def test_account_level_block_is_attributed(dao: LiveTradeDAO, tmp_path: Path) -> None:
    """
    帳戶層的擋下（記在 `__account__` 名下）也要拿來歸因

    2026-10-13 的 1312 被同一批委託的單一標的曝險上限擋下，事件記在 `__account__`，
    parity 只看策略自己的事件，把它記成未解釋。
    """

    dao.insert_risk_event(
        {
            "run_id": "run1",
            "strategy_name": ACCOUNT_STRATEGY,
            "severity": "WARN",
            "category": "BATCH_EXPOSURE",
            "symbol": "2330",
            "message": "2330 曝險超過單一標的上限",
            "occurred_at": NOW,
        }
    )
    checker: ParityChecker = ParityChecker(
        dao,
        lambda name, day: [make_backtest_order("2330")],
        output_root=tmp_path,
        strategy_names=["Alpha"],
    )

    result: Dict[str, List[ParityDiff]] = checker.check(TODAY)

    assert [diff.category for diff in result["Alpha"]] == [CATEGORY_RISK_REJECTED]
    assert "帳戶層 BATCH_EXPOSURE" in result["Alpha"][0].note


def test_checker_passes_aborted_segments_and_phases(
    dao: LiveTradeDAO, tmp_path: Path
) -> None:
    """接線：比對器把當天中止的段落與段落查詢傳進比對"""

    dao.insert_run(
        {
            "run_id": "run-close",
            "started_at": NOW,
            "phase": "close",
            "simulation": 1,
            "end_reason": "例外中止：DataFreshnessError",
            "strategy_params_json": '{"strategies": ["Alpha"]}',
        }
    )
    dao.conn.commit()
    checker: ParityChecker = ParityChecker(
        dao,
        lambda name, day: [make_backtest_order("2330")],
        output_root=tmp_path,
        strategy_names=["Alpha"],
        order_phases=lambda name, order: {"close"},
    )

    result: Dict[str, List[ParityDiff]] = checker.check(TODAY)

    assert [diff.category for diff in result["Alpha"]] == [CATEGORY_SEGMENT_ABORTED]


def test_order_phase_lookup_follows_the_strategy_schedule() -> None:
    """段落沿用實盤引擎的解析：開倉看 `open` 鉤子，平倉看 `close` 與 `stop_loss`"""

    strategy: VolumeBreakoutMomentumStrategy = VolumeBreakoutMomentumStrategy()
    strategy.execution_schedule = {
        LiveHook.OPEN.value: ExecutionTiming.AT_OPEN,
        LiveHook.CLOSE.value: ExecutionTiming.AT_CLOSE,
    }
    lookup = make_order_phase_lookup([strategy])
    name: str = type(strategy).__name__

    assert lookup(name, make_backtest_order()) == {"open"}
    # 沒宣告 stop_loss 時退回 close 的段落
    assert lookup(name, make_backtest_order(action=Action.SELL)) == {"close"}
    assert lookup("NotLoaded", make_backtest_order()) == set()


# === 同段平倉仍佔名額與盤中路徑 ===
def timed_live_order(
    symbol: str,
    action: str,
    timing: ExecutionTiming,
    status: str = "FILLED",
    volume: int = 2,
) -> Dict[str, Any]:
    """帶執行時點與成交量的實盤委託；成交量依狀態填（`FILLED` 全成、其餘 0）"""

    row: Dict[str, Any] = make_live_order(symbol, action, volume, status)
    row["timing"] = timing.value
    row["filled_volume"] = volume if status == "FILLED" else 0
    return row


def by_symbol(diffs: List[ParityDiff]) -> Dict[Tuple[str, str], str]:
    """差異清單 → `{(標的, 買賣別): 類別}`"""

    return {(diff.symbol, diff.side): diff.category for diff in diffs}


def close_day_of_2026_10_29() -> Tuple[List[Dict[str, Any]], List[BaseOrder]]:
    """
    2026-10-29 `VolumeBreakoutMomentumStrategy` 尾盤段：持有 2465，同段送出 2465 賣單與
    2352、2465 買單（持倉上限 3、可開檔數只剩 2）；回測先平後開，三檔都買
    """

    live: List[Dict[str, Any]] = [
        timed_live_order("2465", "Sell", ExecutionTiming.AT_CLOSE, "CANCELLED", 1),
        timed_live_order("2352", "Buy", ExecutionTiming.AT_CLOSE, "FILLED", 4),
        timed_live_order("2465", "Buy", ExecutionTiming.AT_CLOSE, "FILLED", 1),
    ]
    backtest: List[BaseOrder] = [
        make_backtest_order("2465", Action.SELL, 1),
        make_backtest_order("2352", Action.BUY, 4),
        make_backtest_order("2465", Action.BUY, 1),
        make_backtest_order("6233", Action.BUY, 4),
    ]
    return live, backtest


def test_opening_crowded_out_by_a_same_segment_exit_is_explained() -> None:
    """
    2026-10-29 的 6233：同段平倉單還沒成交、舊倉仍佔名額，回測先平後開多買了一檔

    實盤這樣做是刻意的（那張 2465 賣單真的沒成交），不該當成訊號漂移發 CRITICAL。
    """

    live, backtest = close_day_of_2026_10_29()

    diffs: List[ParityDiff] = compare(
        live, backtest, [], (), False, order_phases=close_phase
    )

    assert by_symbol(diffs)[("6233", "Buy")] == CATEGORY_EXIT_PENDING_SLOT
    assert CATEGORY_GROUP[CATEGORY_EXIT_PENDING_SLOT] == GROUP_STRUCTURAL


def test_without_a_same_segment_exit_the_missing_opening_stays_unexplained() -> None:
    """沒有平倉單就沒有被佔走的名額：少送的開倉單原因未知"""

    live, backtest = close_day_of_2026_10_29()
    live = [row for row in live if row["action"] == "Buy"]
    backtest = [order for order in backtest if order.action is Action.BUY]

    diffs: List[ParityDiff] = compare(
        live, backtest, [], (), False, order_phases=close_phase
    )

    assert by_symbol(diffs)[("6233", "Buy")] == CATEGORY_UNEXPLAINED


def test_more_missing_openings_than_exits_are_all_unexplained() -> None:
    """一檔平倉最多佔一個名額：缺兩檔卻只有一張平倉單，就有別的原因，整段不歸類"""

    live, backtest = close_day_of_2026_10_29()
    backtest.append(make_backtest_order("1201", Action.BUY, 9))

    diffs: List[ParityDiff] = compare(
        live, backtest, [], (), False, order_phases=close_phase
    )

    categories: Dict[Tuple[str, str], str] = by_symbol(diffs)
    assert categories[("6233", "Buy")] == CATEGORY_UNEXPLAINED
    assert categories[("1201", "Buy")] == CATEGORY_UNEXPLAINED


def test_exit_in_another_segment_does_not_hold_the_slot() -> None:
    """開盤段的平倉早在尾盤段之前送出，不會與尾盤段的開倉搶名額"""

    live, backtest = close_day_of_2026_10_29()
    live[0]["timing"] = ExecutionTiming.AT_OPEN.value

    diffs: List[ParityDiff] = compare(
        live, backtest, [], (), False, order_phases=lambda order: {"close"}
    )

    assert by_symbol(diffs)[("6233", "Buy")] == CATEGORY_UNEXPLAINED


def test_without_phase_lookup_the_slot_is_not_guessed() -> None:
    """判不出回測單該在哪一段送出時不猜"""

    live, backtest = close_day_of_2026_10_29()

    diffs: List[ParityDiff] = compare(live, backtest, [], ())

    assert by_symbol(diffs)[("6233", "Buy")] == CATEGORY_UNEXPLAINED


def test_risk_event_still_wins_over_exit_pending_slot() -> None:
    """
    有事件就以事件為準，被擋下的那檔也不佔「缺幾檔」的計數

    1201 被守門擋下、6233 被平倉單佔走名額：只有一張平倉單，但缺的只算 6233 一檔。
    """

    live, backtest = close_day_of_2026_10_29()
    backtest.append(make_backtest_order("1201", Action.BUY, 9))

    diffs: List[ParityDiff] = compare(
        live,
        backtest,
        [make_event("CROSS_STRATEGY_BLOCKED", "1201")],
        (),
        False,
        order_phases=close_phase,
    )

    categories: Dict[Tuple[str, str], str] = by_symbol(diffs)
    assert categories[("1201", "Buy")] == CATEGORY_CROSS_STRATEGY_BLOCKED
    assert categories[("6233", "Buy")] == CATEGORY_EXIT_PENDING_SLOT


def intraday_day_of_2026_10_29() -> Tuple[List[Dict[str, Any]], List[BaseOrder]]:
    """2026-10-29 `IntradayMomentumStrategy` 的 2352：09:21 進場、09:32 同日停損；日 K 回測只有進場"""

    live: List[Dict[str, Any]] = [
        timed_live_order("2352", "Buy", ExecutionTiming.IMMEDIATE, "FILLED", 4),
        timed_live_order("2352", "Sell", ExecutionTiming.IMMEDIATE, "FILLED", 4),
    ]
    backtest: List[BaseOrder] = [make_backtest_order("2352", Action.BUY, 4)]
    return live, backtest


def test_same_day_intraday_exit_is_the_intraday_path() -> None:
    """盤中進場又出場、回測只有進場：是日 K 看不到盤中先後，不是訊號漂移"""

    live, backtest = intraday_day_of_2026_10_29()

    diffs: List[ParityDiff] = compare(live, backtest, [])

    assert [(diff.symbol, diff.side, diff.category) for diff in diffs] == [
        ("2352", "Sell", CATEGORY_INTRADAY_PATH)
    ]
    assert CATEGORY_GROUP[CATEGORY_INTRADAY_PATH] == GROUP_STRUCTURAL


def test_extra_exit_outside_the_intraday_segment_is_unexplained() -> None:
    """盤中進場、出場卻在尾盤段：出場不在盤中，日 K 的高低收沒有看不到的先後，多一張就是真的差異"""

    live, backtest = intraday_day_of_2026_10_29()
    live[1]["timing"] = ExecutionTiming.AT_CLOSE.value

    diffs: List[ParityDiff] = compare(live, backtest, [])

    assert by_symbol(diffs)[("2352", "Sell")] == CATEGORY_UNEXPLAINED


def test_intraday_exit_without_a_filled_entry_is_unexplained() -> None:
    """當天沒有真的進場，就沒有「同日出場」可言"""

    live, backtest = intraday_day_of_2026_10_29()
    live[0]["status"] = "CANCELLED"
    live[0]["filled_volume"] = 0

    diffs: List[ParityDiff] = compare(live, backtest, [])

    assert by_symbol(diffs)[("2352", "Sell")] == CATEGORY_UNEXPLAINED


def test_intraday_exit_without_a_backtest_entry_is_unexplained() -> None:
    """回測連進場都沒有，差異在進場訊號，不是盤中路徑"""

    live, _ = intraday_day_of_2026_10_29()

    diffs: List[ParityDiff] = compare(live, [], [])

    assert by_symbol(diffs)[("2352", "Sell")] == CATEGORY_UNEXPLAINED

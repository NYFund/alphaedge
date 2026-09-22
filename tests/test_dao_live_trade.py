import datetime
import sqlite3
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import pytest

from core.config import (
    LIVE_FILL_TABLE_NAME,
    LIVE_ORDER_TABLE_NAME,
    TW_STOCK_DB_PATH,
    TW_TRADING_DB_PATH,
)
from core.dao.connection import connect_live_trading
from core.dao.tw.live_trade_dao import LiveTradeDAO

"""
實盤紀錄庫（`tw_trading.db`）：建表冪等、寫入冪等、狀態讀得回來

這一層擋的是三種**事後才會發現**的事故：

- **成交被記兩次**：實盤的回報會重複推送，重複記一筆等於帳上多了一個不存在的部位。
- **重啟偷偷解除降級**：交易模式沒落地的話，程式因對帳不一致停下來、值班的人
  直接重啟，就帶著錯誤部位繼續交易。日頻的三個段落是三個獨立行程，
  策略層的模式也一樣要落地。
- **重複送補平單**：跨日待辦沒有完成標記時，次日開盤段重跑會再送一次，
  而重複的補平單不是多買一點，是直接把部位做反。
"""

TODAY: datetime.date = datetime.date(2026, 9, 19)
NOW: datetime.datetime = datetime.datetime(2026, 9, 19, 13, 25)


def make_order(
    client_order_id: str = "run1-0001",
    strategy_name: str = "MomentumStrategy1",
    status: str = "PENDING_SUBMIT",
    broker_seqno: Optional[str] = None,
    custom_field: str = "1-0001",
) -> Dict[str, Any]:
    return {
        "client_order_id": client_order_id,
        "run_id": "run1",
        "strategy_name": strategy_name,
        "symbol": "2330",
        "action": "Buy",
        "position_type": "LONG",
        "price": 1000.0,
        "volume": 2,
        "status": status,
        "broker_seqno": broker_seqno,
        "custom_field": custom_field,
        "created_at": NOW,
    }


def make_fill(
    broker_seqno: str = "000001", broker_trade_id: str = "T001", volume: int = 2
) -> Dict[str, Any]:
    return {
        "broker_seqno": broker_seqno,
        "broker_trade_id": broker_trade_id,
        "client_order_id": "run1-0001",
        "strategy_name": "MomentumStrategy1",
        "symbol": "2330",
        "action": "Buy",
        "price": 1000.0,
        "volume": volume,
        "estimated_fee": 427.0,
        "estimated_tax": 0.0,
        "filled_at": NOW,
        "raw_json": "{}",
    }


def make_lot(
    lot_id: str = "L1",
    strategy_name: str = "MomentumStrategy1",
    symbol: str = "2330",
    volume: int = 2,
    open_date: datetime.date = TODAY,
) -> Dict[str, Any]:
    return {
        "lot_id": lot_id,
        "strategy_name": strategy_name,
        "symbol": symbol,
        "direction": "LONG",
        "volume": volume,
        "open_date": open_date,
        "open_price": 1000.0,
        "client_order_id": "run1-0001",
    }


# === 建表 ===
def test_schema_is_idempotent(dao: LiveTradeDAO) -> None:
    """建表重跑不會出錯，也不會改變既有資料"""

    dao.upsert_order(make_order())
    dao.ensure_tables()
    dao.ensure_tables()

    assert dao.count_rows() == 1


def test_every_planned_table_exists(dao: LiveTradeDAO) -> None:
    """11 張表全部要建出來：少一張會在用到它的那一步才炸"""

    names: set = {
        row[0]
        for row in dao.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }

    assert {
        "live_run",
        "live_order",
        "live_order_event",
        "live_fill",
        "live_position_lot",
        "live_position_snapshot",
        "live_account_snapshot",
        "live_risk_event",
        "live_strategy_mode",
        "live_pending_action",
        "live_parity_diff",
    } <= names


def test_trading_db_is_separate_from_research_db(production_live_db_path: Path) -> None:
    """
    交易紀錄獨立一庫

    研究資料重跑爬蟲就回來，交易紀錄不能重建。併庫之後，任何一次
    「砍掉重建研究庫」都會連委託與成交一起帶走。

    正式環境的預設值要讀替換前的原值：測試期間 `DEFAULT_DB_PATH` 已被根目錄的
    `conftest.py` 換成暫存路徑，第三條斷言確認這層隔離確實生效。
    """

    assert production_live_db_path == TW_TRADING_DB_PATH
    assert TW_TRADING_DB_PATH != TW_STOCK_DB_PATH
    assert LiveTradeDAO.DEFAULT_DB_PATH != TW_TRADING_DB_PATH


# === 連線設定 ===
def test_live_connection_enables_wal_and_full_sync(tmp_path: Path) -> None:
    """
    實盤連線要 WAL ＋ `synchronous=FULL`

    WAL 讓日報與監控能在實盤寫入的同時讀取；FULL 則是「先寫 DB 再送單」的
    恢復保證——WAL 預設的 NORMAL 在斷電時可能丟掉最後一次 commit，
    而那剛好是「已經寫了紀錄、還沒送出去」的那張單。
    """

    conn: sqlite3.Connection = connect_live_trading(tmp_path / "t.db")

    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
    conn.close()


def test_read_only_connection_does_not_attempt_pragmas(tmp_path: Path) -> None:
    """
    唯讀連線不改 journal_mode

    改了會拋 readonly database，而唯讀連線正是存活監控與報表用的——
    它們在實盤行程之外，不該因為一個 PRAGMA 就開不起來。
    """

    path: Path = tmp_path / "t.db"
    connect_live_trading(path).close()
    conn: sqlite3.Connection = connect_live_trading(path, read_only=True)

    assert conn.execute("SELECT 1").fetchone()[0] == 1
    conn.close()


# === 委託 ===
def test_order_upsert_keeps_one_row_per_client_order_id(dao: LiveTradeDAO) -> None:
    """同一張單多次更新只留一列"""

    dao.upsert_order(make_order())
    dao.upsert_order(make_order(status="SUBMITTED", broker_seqno="000001"))

    assert dao.count_rows() == 1
    row: Any = dao.conn.execute(
        f"SELECT status, broker_seqno FROM {LIVE_ORDER_TABLE_NAME}"
    ).fetchone()
    assert row == ("SUBMITTED", "000001")


def test_broker_seqno_is_unique_but_allows_many_nulls(dao: LiveTradeDAO) -> None:
    """
    `broker_seqno` 唯一，但可以有多個 NULL

    在 `place_order()` 回傳前崩潰的委託沒有 seqno。唯一索引若不允許多個 NULL，
    第二張還沒送出去的單就寫不進 DB——而「先寫 DB 再送單」正是恢復的前提。
    """

    dao.upsert_order(make_order("run1-0001", custom_field="1-0001"))
    dao.upsert_order(make_order("run1-0002", custom_field="1-0002"))

    assert dao.count_rows() == 2

    dao.upsert_order(
        make_order("run1-0003", broker_seqno="000001", custom_field="1-0003")
    )
    with pytest.raises(sqlite3.IntegrityError):
        dao.upsert_order(
            make_order("run1-0004", broker_seqno="000001", custom_field="1-0004")
        )


def test_unfinished_orders_exclude_terminal_states(dao: LiveTradeDAO) -> None:
    """
    只有非終態的委託需要接管

    把已成交的單也撈出來重新接管，會讓引擎去撤一張早就不存在的單。
    """

    for index, status in enumerate(
        ["PENDING_SUBMIT", "SUBMITTED", "PARTIALLY_FILLED", "FILLED", "CANCELLED"]
    ):
        dao.upsert_order(
            make_order(
                f"run1-{index:04d}", status=status, custom_field=f"1-{index:04d}"
            )
        )

    unfinished: List[Dict[str, Any]] = dao.get_unfinished_orders(TODAY)

    assert {row["status"] for row in unfinished} == {
        "PENDING_SUBMIT",
        "SUBMITTED",
        "PARTIALLY_FILLED",
    }


def test_lookup_by_custom_field(dao: LiveTradeDAO) -> None:
    """
    壓縮碼要反查得到本地委託

    這是重啟接管能做到精確比對的關鍵：壓縮碼可經本地紀錄反查策略，
    策略代號卻反查不到是哪一張單。
    """

    dao.upsert_order(make_order(custom_field="1-0001"))

    found: Optional[Dict[str, Any]] = dao.find_order_by_custom_field("1-0001")

    assert found is not None
    assert found["strategy_name"] == "MomentumStrategy1"
    assert dao.find_order_by_custom_field("9-9999") is None


# === 成交 ===
def test_same_fill_written_twice_stays_one_row(dao: LiveTradeDAO) -> None:
    """
    同一筆成交重放幾次都只有一列

    實盤的回報會重複推送。重複記一筆成交等於帳上多了一個不存在的部位，
    而那要到盤後對帳才會發現。
    """

    assert dao.insert_fill(make_fill()) is True
    assert dao.insert_fill(make_fill()) is False

    assert (
        dao.conn.execute(f"SELECT COUNT(*) FROM {LIVE_FILL_TABLE_NAME}").fetchone()[0]
        == 1
    )


def test_separate_fills_of_one_order_are_both_kept(dao: LiveTradeDAO) -> None:
    """
    同一張單的分批成交是兩筆

    兩筆的價格與數量可能完全相同，靠內容去重會把真實的第二筆丟掉。
    """

    dao.insert_fill(make_fill(broker_trade_id="T001"))
    dao.insert_fill(make_fill(broker_trade_id="T002"))

    assert (
        dao.conn.execute(f"SELECT COUNT(*) FROM {LIVE_FILL_TABLE_NAME}").fetchone()[0]
        == 2
    )


def test_cost_backfill_keeps_the_estimate(dao: LiveTradeDAO) -> None:
    """
    回填實際費用時估算值要留著

    併成一欄的話，回填之後就再也算不出「估得準不準」——而那正是校正
    成本設定的唯一依據。
    """

    dao.insert_fill(make_fill())
    dao.backfill_fill_costs("000001", "T001", actual_fee=421.0, actual_tax=0.0)

    row: Any = dao.conn.execute(
        f"SELECT estimated_fee, actual_fee FROM {LIVE_FILL_TABLE_NAME}"
    ).fetchone()

    assert row == (427.0, 421.0)


# === 交易模式 ===
def test_account_mode_survives_a_restart(dao: LiveTradeDAO) -> None:
    """
    帳戶層模式要讀得回來

    否則「重啟」就等於偷偷解除 halt——這是實盤最常見的事故型態。
    """

    dao.insert_run(
        {
            "run_id": "run1",
            "started_at": NOW,
            "phase": "close",
            "simulation": 1,
        }
    )
    dao.finish_run("run1", NOW, "對帳不一致", "REDUCE_ONLY")

    assert dao.get_last_account_mode() == "REDUCE_ONLY"


def test_running_session_does_not_shadow_the_last_finished_one(
    dao: LiveTradeDAO,
) -> None:
    """
    讀的是最近一筆**已結束**的紀錄

    正在跑的那一筆可能就是本次自己，讀它等於什麼都沒讀到。
    """

    dao.insert_run(
        {"run_id": "run1", "started_at": NOW, "phase": "open", "simulation": 1}
    )
    dao.finish_run("run1", NOW, "降級", "HALTED")
    dao.insert_run(
        {"run_id": "run2", "started_at": NOW, "phase": "close", "simulation": 1}
    )

    assert dao.get_last_account_mode() == "HALTED"


def test_no_history_defaults_to_normal(dao: LiveTradeDAO) -> None:
    """第一次啟動時沒有紀錄，預設 NORMAL"""

    assert dao.get_last_account_mode() == "NORMAL"


TAIPEI: datetime.timezone = datetime.timezone(datetime.timedelta(hours=8))


def at(hour: int, minute: int = 0) -> datetime.datetime:
    """`TODAY` 的台北時間（與 `now_live()` 一樣是 aware）"""

    return datetime.datetime.combine(TODAY, datetime.time(hour, minute), tzinfo=TAIPEI)


@pytest.mark.parametrize("insert_order", [("open", "close"), ("close", "open")])
def test_same_day_halt_is_read_back_by_end_time(
    dao: LiveTradeDAO, insert_order: Tuple[str, str]
) -> None:
    """
    同一天兩段執行，讀回的是**較晚結束**那段的模式

    時間欄位只存日期時，兩筆的 `ended_at` 一樣，`ORDER BY ended_at` 等於任挑一筆——
    收盤段因 kill switch 停下，下次啟動卻讀回開盤段的 NORMAL，halt 就這樣被解除了。
    兩種寫入順序都要讀回 HALTED，確認判定靠的是時間而不是寫入順序。
    """

    started_at: Dict[str, datetime.datetime] = {"open": at(8, 30), "close": at(13, 20)}
    for run_id in insert_order:
        dao.insert_run(
            {
                "run_id": run_id,
                "started_at": started_at[run_id],
                "phase": run_id,
                "simulation": 1,
            }
        )
    dao.finish_run("close", at(13, 35), "kill switch", "HALTED")
    dao.finish_run("open", at(9, 5), "正常結束", "NORMAL")

    assert dao.get_last_account_mode() == "HALTED"


def test_time_columns_keep_the_full_timestamp(dao: LiveTradeDAO) -> None:
    """時間欄位存完整時間（含時區）；日期欄位仍只存日期"""

    dao.insert_run(
        {"run_id": "run1", "started_at": at(8, 30), "phase": "open", "simulation": 1}
    )
    dao.insert_pending_action(make_action())

    started_at: str = dao.conn.execute("SELECT started_at FROM live_run").fetchone()[0]
    due_date: str = dao.conn.execute(
        "SELECT due_date FROM live_pending_action"
    ).fetchone()[0]

    assert started_at == "2026-09-19T08:30:00+08:00"
    assert due_date == "2026-09-19"


def test_early_morning_records_stay_on_their_own_day(dao: LiveTradeDAO) -> None:
    """
    台北 08:00 以前的紀錄仍歸在當天

    SQLite 的 `date()` 會先把帶時區的時間換算成 UTC，07:30+08:00 會變成前一天——
    盤前送出的委託與風控事件就會從當天的報表裡消失。
    """

    order: Dict[str, Any] = make_order()
    order["created_at"] = at(7, 30)
    dao.upsert_order(order)
    dao.insert_risk_event(
        {
            "severity": "WARNING",
            "category": "TEST",
            "message": "盤前事件",
            "occurred_at": at(7, 45),
        }
    )

    assert len(dao.get_orders_by_date(TODAY)) == 1
    assert len(dao.get_unfinished_orders(TODAY)) == 1
    assert len(dao.get_risk_events_by_date(TODAY)) == 1
    assert dao.get_orders_by_date(TODAY - datetime.timedelta(days=1)) == []


def test_legacy_date_only_rows_are_still_readable(dao: LiveTradeDAO) -> None:
    """改版前寫入的紀錄只有日期，讀取端要照樣篩得到"""

    order: Dict[str, Any] = make_order()
    order["created_at"] = TODAY.isoformat()
    dao.upsert_order(order)

    assert len(dao.get_orders_by_date(TODAY)) == 1


def test_strategy_mode_is_persisted_per_strategy(dao: LiveTradeDAO) -> None:
    """
    **策略層模式也要落地**

    日頻的 open／close／after_close 是三個獨立行程。只存帳戶層的話，
    開盤段因當日虧損上限而降級的那支策略，到 13:20 尾盤段重新啟動時
    會靜默回到 NORMAL，當天繼續開新倉。
    """

    dao.upsert_strategy_mode(
        {
            "strategy_name": "MomentumStrategy1",
            "mode": "REDUCE_ONLY",
            "reason": "當日虧損上限",
            "run_id": "run1",
            "changed_at": NOW,
        }
    )
    dao.upsert_strategy_mode(
        {
            "strategy_name": "MomentumFuturesStrategy",
            "mode": "NORMAL",
            "run_id": "run1",
            "changed_at": NOW,
        }
    )

    modes: Dict[str, str] = dao.get_strategy_modes()

    assert modes["MomentumStrategy1"] == "REDUCE_ONLY"
    assert modes["MomentumFuturesStrategy"] == "NORMAL"


# === 部位歸屬帳 ===
def test_open_lots_are_ordered_deterministically(dao: LiveTradeDAO) -> None:
    """
    未平倉 lot 的排序要固定

    平倉沖銷哪一筆由順序決定；順序不穩定的話，同一天跑兩次會得到不同的
    已實現損益，而回測那邊的沖銷順序是固定的。
    """

    dao.open_lot(make_lot("L2", open_date=datetime.date(2026, 9, 18)))
    dao.open_lot(make_lot("L1", open_date=datetime.date(2026, 9, 17)))
    dao.open_lot(make_lot("L3", open_date=datetime.date(2026, 9, 18)))

    assert [lot["lot_id"] for lot in dao.get_open_lots()] == ["L1", "L2", "L3"]


def test_closed_and_empty_lots_are_excluded(dao: LiveTradeDAO) -> None:
    """平掉的與扣到 0 的 lot 不再出現"""

    dao.open_lot(make_lot("L1", volume=2))
    dao.open_lot(make_lot("L2", volume=2))
    dao.close_lot("L1", NOW)
    dao.reduce_lot("L2", 2)

    assert dao.get_open_lots() == []


def test_lots_are_filtered_per_strategy(dao: LiveTradeDAO) -> None:
    """
    平倉只沖銷該策略自己的 lot

    抓錯策略的 lot 會讓兩支策略的已實現損益互相污染，而券商端的合計還是對的，
    對帳看不出來。
    """

    dao.open_lot(make_lot("L1", strategy_name="A"))
    dao.open_lot(make_lot("L2", strategy_name="B"))

    assert [lot["lot_id"] for lot in dao.get_open_lots(strategy_name="A")] == ["L1"]


def test_symbol_holder_for_cross_strategy_guard(dao: LiveTradeDAO) -> None:
    """同一標的只能有一個持有者（跨策略守門要用）"""

    dao.open_lot(make_lot("L1", strategy_name="A", symbol="2330"))

    assert dao.get_symbol_holder("2330") == "A"
    assert dao.get_symbol_holder("2317") is None


# === 跨日待辦 ===
def make_action(
    action_id: str = "P1", due_date: datetime.date = TODAY
) -> Dict[str, Any]:
    return {
        "action_id": action_id,
        "strategy_name": "MomentumStrategy1",
        "symbol": "2330",
        "action": "Sell",
        "position_type": "LONG",
        "volume": 2,
        "due_date": due_date,
        "status": LiveTradeDAO.ACTION_PENDING,
        "reason": "平倉單未成交",
        "source_client_order_id": "run1-0001",
        "created_at": NOW,
    }


def test_resolved_action_is_not_returned_again(dao: LiveTradeDAO) -> None:
    """
    已處理的待辦不可再送一次

    重複的補平單不是多買一點，是直接把部位做反。
    """

    dao.insert_pending_action(make_action())
    assert len(dao.get_pending_actions(TODAY)) == 1

    dao.resolve_pending_action("P1", LiveTradeDAO.ACTION_DONE, NOW)
    assert dao.get_pending_actions(TODAY) == []


def test_overdue_actions_are_included(dao: LiveTradeDAO) -> None:
    """逾期的待辦要一起撈出來，不可因為過了日期就消失"""

    dao.insert_pending_action(make_action("P1", due_date=datetime.date(2026, 9, 17)))

    assert len(dao.get_pending_actions(TODAY)) == 1


def test_future_actions_are_not_returned_yet(dao: LiveTradeDAO) -> None:
    """還沒到期的不撈"""

    dao.insert_pending_action(make_action("P1", due_date=datetime.date(2026, 9, 22)))

    assert dao.get_pending_actions(TODAY) == []


def test_postpone_keeps_it_pending(dao: LiveTradeDAO) -> None:
    """次日仍未補成時延到再次日，狀態維持 PENDING"""

    dao.insert_pending_action(make_action())
    dao.postpone_pending_action("P1", datetime.date(2026, 9, 22))

    assert dao.get_pending_actions(TODAY) == []
    assert len(dao.get_pending_actions(datetime.date(2026, 9, 22))) == 1


# === 風控事件 ===
def test_risk_event_carries_severity(dao: LiveTradeDAO) -> None:
    """
    `severity` 是欄位不是推導值

    推播分級直接讀它；在推播端再寫一份判斷的話，兩份必然漂移。
    """

    dao.insert_risk_event(
        {
            "run_id": "run1",
            "strategy_name": "MomentumStrategy1",
            "severity": "CRITICAL",
            "category": "RECONCILE_MISMATCH",
            "message": "本地與券商部位不一致",
            "occurred_at": NOW,
        }
    )

    row: Any = dao.conn.execute(
        "SELECT severity, category FROM live_risk_event"
    ).fetchone()

    assert row == ("CRITICAL", "RECONCILE_MISMATCH")


def test_dao_factory_builds_the_live_tables(
    dao_factory: Callable[..., Any],
) -> None:
    """共用的 DAO fixture 認得多表 DAO（`ensure_tables`），不必各測試自己抄 schema"""

    built: Any = dao_factory(LiveTradeDAO)

    assert built.table_exists()

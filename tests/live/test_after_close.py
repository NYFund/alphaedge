import datetime
from pathlib import Path
from typing import Dict

import pandas as pd
import pytest

from core.dao.tw.live_trade_dao import LiveTradeDAO
from core.live.report.live_reporter import (
    FILL_COLUMNS,
    ORDER_COLUMNS,
    POSITION_COLUMNS,
    LiveReporter,
)
from core.utils import StockPriceType

"""
盤後作業：把當天發生的事收攏成可稽核的結果，並把「明天要補的事」寫下來

兩件事撐起整個盤後：
- **報表筆數要和資料庫一致**。報表同時是事後追查的入口，少一筆就等於那筆交易
  在追查時不存在。
- **殘量政策開倉與出場完全相反**。開倉未成交放棄（追價等於在偏離訊號價的位置建倉，
  回測沒有這個行為）；平倉未成交必須補（那是預期外的隔夜部位）。
"""

TODAY: datetime.date = datetime.date(2026, 9, 21)
NOW: datetime.datetime = datetime.datetime(2026, 9, 21, 15, 0)


def add_order(
    dao: LiveTradeDAO,
    client_order_id: str = "run1-0001",
    strategy_name: str = "Alpha",
    action: str = "Buy",
    position_type: str = "LONG",
    price: float = 1000.0,
    volume: int = 2,
    filled_volume: int = 2,
    avg_fill_price: float = 1000.0,
    status: str = "FILLED",
) -> None:
    dao.upsert_order(
        {
            "client_order_id": client_order_id,
            "run_id": "run1",
            "strategy_name": strategy_name,
            "symbol": "2330",
            "action": action,
            "position_type": position_type,
            "price": price,
            "volume": volume,
            "price_type": StockPriceType.LMT.value,
            "status": status,
            "filled_volume": filled_volume,
            "avg_fill_price": avg_fill_price,
            "custom_field": client_order_id[-6:],
            "created_at": datetime.datetime(2026, 9, 21, 13, 25),
        }
    )
    dao.conn.commit()


def add_fill(
    dao: LiveTradeDAO,
    broker_trade_id: str = "T001",
    client_order_id: str = "run1-0001",
    price: float = 1000.0,
    volume: int = 2,
) -> None:
    dao.insert_fill(
        {
            "broker_seqno": "000001",
            "broker_trade_id": broker_trade_id,
            "client_order_id": client_order_id,
            "strategy_name": "Alpha",
            "symbol": "2330",
            "action": "Buy",
            "price": price,
            "volume": volume,
            "estimated_fee": 427.0,
            "estimated_tax": 0.0,
            "filled_at": datetime.datetime(2026, 9, 21, 13, 30),
        }
    )


# === 報表 ===
def test_reports_match_the_database_row_counts(
    dao: LiveTradeDAO, tmp_path: Path
) -> None:
    """
    三份 CSV 的筆數要和資料庫一致

    報表同時是事後追查的入口；少一筆就等於那筆交易在追查時不存在。
    """

    add_order(dao, "run1-0001")
    add_order(dao, "run1-0002", status="REJECTED", filled_volume=0)
    add_fill(dao, "T001")
    add_fill(dao, "T002")
    dao.upsert_position_snapshot(
        {
            "date": TODAY,
            "strategy_name": "Alpha",
            "symbol": "2330",
            "source": "local",
            "direction": "LONG",
            "volume": 2,
        }
    )
    dao.conn.commit()

    reporter: LiveReporter = LiveReporter(dao, output_root=tmp_path)
    written: Dict[str, Path] = reporter.write_daily_reports(TODAY)

    assert len(pd.read_csv(written["Alpha/orders"])) == 2
    assert len(pd.read_csv(written["Alpha/fills"])) == 2
    assert len(pd.read_csv(written["Alpha/positions"])) == 1


def test_rejected_orders_are_included(dao: LiveTradeDAO, tmp_path: Path) -> None:
    """
    被拒與已撤的委託也要進報表

    那些正是 parity 比對要歸因的部分；濾掉之後，「為什麼那張單沒送出去」
    就再也查不到了。
    """

    add_order(dao, "run1-0002", status="REJECTED", filled_volume=0)
    written: Dict[str, Path] = LiveReporter(dao, tmp_path).write_daily_reports(TODAY)

    frame: pd.DataFrame = pd.read_csv(written["Alpha/orders"])

    assert "REJECTED" in frame["Status"].tolist()


def test_column_names_match_the_backtest_report(
    dao: LiveTradeDAO, tmp_path: Path
) -> None:
    """
    欄位名與回測報表同一套（Title Case ＋ 空白）

    parity 比對要把兩邊的委託逐筆對起來；欄位名不同就得先寫一層對照表，
    而那層對照表會是下一個漂移的地方。
    """

    add_order(dao)
    written: Dict[str, Path] = LiveReporter(dao, tmp_path).write_daily_reports(TODAY)

    assert list(pd.read_csv(written["Alpha/orders"]).columns) == ORDER_COLUMNS
    assert list(pd.read_csv(written["Alpha/fills"]).columns) == FILL_COLUMNS
    assert list(pd.read_csv(written["Alpha/positions"]).columns) == POSITION_COLUMNS


def test_empty_day_still_writes_files(dao: LiveTradeDAO, tmp_path: Path) -> None:
    """
    沒有交易也要寫出空表

    檔案不存在與「今天沒有交易」是兩回事，而盤後檢查只看得到檔案在不在。
    """

    add_order(dao, filled_volume=0, status="CANCELLED")
    written: Dict[str, Path] = LiveReporter(dao, tmp_path).write_daily_reports(TODAY)

    assert written["Alpha/fills"].exists()
    assert len(pd.read_csv(written["Alpha/fills"])) == 0


def test_each_strategy_gets_its_own_directory(
    dao: LiveTradeDAO, tmp_path: Path
) -> None:
    """逐策略各一個目錄：多策略共用一個檔案會讓歸屬在報表這一層又糊掉"""

    add_order(dao, "run1-0001", strategy_name="Alpha")
    add_order(dao, "run1-0002", strategy_name="Beta")
    written: Dict[str, Path] = LiveReporter(dao, tmp_path).write_daily_reports(TODAY)

    assert (tmp_path / "Alpha").is_dir()
    assert (tmp_path / "Beta").is_dir()
    assert written["Alpha/orders"] != written["Beta/orders"]


def test_reports_are_written_outside_the_backtest_results_root() -> None:
    """
    實盤報表**不可寫進回測結果根目錄**

    那裡是回歸雙線的比對基準；被每日的實盤結果覆蓋掉，`run_regression.sh`
    就安靜地失去意義了。
    """

    from core.config import BACKTEST_RESULT_DIR_PATH, LIVE_RESULT_DIR_PATH

    assert LiveReporter(None).output_root == LIVE_RESULT_DIR_PATH
    assert LIVE_RESULT_DIR_PATH != BACKTEST_RESULT_DIR_PATH


# === 滑價 ===
def test_slippage_sign_is_direction_aware(dao: LiveTradeDAO) -> None:
    """
    買進成交價較高 ⇒ 滑價為正（不利）；賣出相反

    不分方向的話，一買一賣的滑價會互相抵消，統計出來永遠接近 0。
    """

    add_order(dao, "run1-0001", action="Buy", price=1000.0, avg_fill_price=1005.0)
    add_order(
        dao,
        "run1-0002",
        action="Sell",
        position_type="SHORT",
        price=1000.0,
        avg_fill_price=995.0,
    )

    summary: Dict[str, float] = LiveReporter(dao).summarize_slippage(TODAY)

    assert summary["Alpha"] == pytest.approx(5.0)


def test_market_orders_are_excluded_from_slippage(dao: LiveTradeDAO) -> None:
    """
    市價單沒有委託價可比

    送出時價格是 0，算進來會得到一個等於成交價的巨大滑價，把整個統計拉歪。
    """

    add_order(dao, "run1-0001", price=0.0, avg_fill_price=1005.0)

    assert LiveReporter(dao).summarize_slippage(TODAY) == {}


def test_unfilled_orders_are_excluded_from_slippage(dao: LiveTradeDAO) -> None:
    """沒成交就沒有滑價；算進來會讓分母變大、平均被稀釋"""

    add_order(dao, "run1-0001", filled_volume=0, avg_fill_price=0.0)

    assert LiveReporter(dao).summarize_slippage(TODAY) == {}


# === 跨日待辦（D7）===
def test_pending_action_is_idempotent(dao: LiveTradeDAO) -> None:
    """
    待辦要有完成標記才冪等

    只記「明天要補」而沒有狀態的話，次日開盤段重跑或崩潰重啟會重複送補平單——
    而重複的補平單不是多買一點，是直接把部位做反。
    """

    dao.insert_pending_action(
        {
            "action_id": "2026-09-21-run1-0001",
            "strategy_name": "Alpha",
            "symbol": "2330",
            "action": "Sell",
            "position_type": "LONG",
            "volume": 1,
            "due_date": TODAY + datetime.timedelta(days=1),
            "status": dao.ACTION_PENDING,
            "created_at": NOW,
        }
    )
    tomorrow: datetime.date = TODAY + datetime.timedelta(days=1)

    assert len(dao.get_pending_actions(tomorrow)) == 1

    dao.resolve_pending_action("2026-09-21-run1-0001", dao.ACTION_DONE, NOW)

    assert dao.get_pending_actions(tomorrow) == []


def test_failed_cover_stays_pending_and_rolls_forward(dao: LiveTradeDAO) -> None:
    """
    補不成時留在 `PENDING` 並把到期日滾到次日

    一張補不成的平倉單不會因為換了一天就變得不重要。
    """

    dao.insert_pending_action(
        {
            "action_id": "P1",
            "strategy_name": "Alpha",
            "symbol": "2330",
            "action": "Sell",
            "position_type": "LONG",
            "volume": 1,
            "due_date": TODAY,
            "status": dao.ACTION_PENDING,
            "created_at": NOW,
        }
    )
    dao.postpone_pending_action("P1", TODAY + datetime.timedelta(days=1))

    assert dao.get_pending_actions(TODAY) == []
    assert len(dao.get_pending_actions(TODAY + datetime.timedelta(days=1))) == 1


# === DAO 查詢 ===
def test_fills_are_filtered_by_fill_date_not_order_date(dao: LiveTradeDAO) -> None:
    """
    成交以 `filled_at` 篩選，不是委託的建立日

    跨段落的委託（開盤段送出、尾盤段才成交）在兩種篩法下會落在不同的日子，
    而帳務要看成交那一天。
    """

    add_order(dao)
    add_fill(dao, "T001")

    assert len(dao.get_fills_by_date(TODAY)) == 1
    assert dao.get_fills_by_date(TODAY - datetime.timedelta(days=1)) == []

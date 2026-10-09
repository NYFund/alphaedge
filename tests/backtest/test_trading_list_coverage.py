import datetime
import sqlite3
from typing import Any, Callable, Dict, List, Optional

import pytest

import apps.backtest as backtest_entry
from core.api.tw.stock_day_trade_list_api import StockDayTradeListAPI
from core.api.tw.stock_short_sale_list_api import StockShortSaleListAPI
from core.backtest.datafeed.tw.futures_datafeed import TwFuturesDataFeed
from core.backtest.datafeed.tw.stock_datafeed import TwStockDataFeed
from core.dao.tw.stock_day_trade_list_dao import StockDayTradeListDAO
from core.dao.tw.stock_short_sale_list_dao import StockShortSaleListDAO
from core.datafeed.base import TradingListCoverageError
from core.models import ShortSaleListSnapshot

"""
開啟名單檢核時的涵蓋檢查：區間早於制度起點、或名單缺日，一律拒絕執行

兩種情況都不可以退回「不檢查」：起點之前的制度是「平盤下不得融（借）券賣出、
現股當沖不能先賣」而不是不限制；缺日則讓那幾天的單全都沒被檢查，結果卻看不出來。
"""

DAY_1: datetime.date = datetime.date(2024, 1, 2)
DAY_2: datetime.date = datetime.date(2024, 1, 3)


def short_sale_row(date: datetime.date, stock_id: str, **flags: int) -> Dict[str, Any]:
    """一列平盤下名單"""

    # 以字串寫入：與 CSV 入庫後的型別一致，也避開 sqlite3 已棄用的 date adapter
    return {
        "date": str(date),
        "stock_id": stock_id,
        "證券名稱": stock_id,
        "暫停融券賣出": flags.get("margin", 0),
        "暫停借券賣出": flags.get("sbl", 0),
        "禁止平盤下融借券賣出": flags.get("below", 0),
    }


def make_feed(
    conn: sqlite3.Connection,
    start: datetime.date = DAY_1,
    end: datetime.date = DAY_2,
    trading_days: Optional[List[datetime.date]] = None,
) -> TwStockDataFeed:
    """只備妥名單 API 與交易日集合的台股資料源（不跑 `setup()`、不讀實體 DB）"""

    feed: TwStockDataFeed = TwStockDataFeed()
    feed.start_date = start
    feed.end_date = end
    feed.trading_days = set(trading_days or [DAY_1, DAY_2])
    feed.short_sale_list = StockShortSaleListAPI(conn=conn)
    feed.day_trade_list = StockDayTradeListAPI(conn=conn)
    return feed


def test_full_coverage_passes(
    memory_conn: sqlite3.Connection, dao_factory: Callable[..., Any]
) -> None:
    """區間每個交易日都有名單時放行"""

    dao_factory(
        StockShortSaleListDAO,
        records=[short_sale_row(DAY_1, "2330"), short_sale_row(DAY_2, "2330")],
    )

    make_feed(memory_conn).ensure_trading_list_coverage(True, False)


def test_missing_day_is_refused(
    memory_conn: sqlite3.Connection, dao_factory: Callable[..., Any]
) -> None:
    """缺一天就拒絕，訊息要寫出缺幾天與該跑哪個 ETL"""

    dao_factory(StockShortSaleListDAO, records=[short_sale_row(DAY_1, "2330")])

    with pytest.raises(TradingListCoverageError, match="缺 1 個交易日") as error:
        make_feed(memory_conn).ensure_trading_list_coverage(True, False)

    assert "--target short_sale_list --from 2024-01-03" in str(error.value)


def test_missing_table_is_refused(memory_conn: sqlite3.Connection) -> None:
    """表根本不存在（ETL 從沒跑過）：同樣是缺日，不可以當成不檢查"""

    with pytest.raises(TradingListCoverageError, match="缺 2 個交易日"):
        make_feed(memory_conn).ensure_trading_list_coverage(False, True)


@pytest.mark.parametrize(
    ("short_sale", "day_trade", "start"),
    [
        (True, False, datetime.date(2013, 9, 22)),
        (False, True, datetime.date(2014, 6, 27)),
    ],
    ids=["before-short-sale-list", "before-sell-first-day-trade"],
)
def test_start_before_the_rule_is_refused(
    memory_conn: sqlite3.Connection,
    short_sale: bool,
    day_trade: bool,
    start: datetime.date,
) -> None:
    """
    區間早於制度起點時拒絕

    當沖名單 2014-01-06 就有，但先賣後買 2014-06-30 才開放，起點要以後者為準。
    """

    feed: TwStockDataFeed = make_feed(memory_conn, start=start)

    with pytest.raises(TradingListCoverageError, match="早於"):
        feed.ensure_trading_list_coverage(short_sale, day_trade)


def test_disabled_checks_need_no_data(memory_conn: sqlite3.Connection) -> None:
    """沒開檢核時不查任何東西：既有回測不需要先跑名單 ETL"""

    make_feed(memory_conn).ensure_trading_list_coverage(False, False)


def test_market_without_lists_refuses_enabled_checks() -> None:
    """沒有名單制度的市場被設定成開啟檢核時拒絕，而不是讓開關靜靜失效"""

    with pytest.raises(TradingListCoverageError, match="沒有交易所名單資料"):
        TwFuturesDataFeed().ensure_trading_list_coverage(True, False)


# === API 快照 ===
def test_snapshot_maps_flags(
    memory_conn: sqlite3.Connection, dao_factory: Callable[..., Any]
) -> None:
    """三個註記各自對應到快照的集合"""

    dao_factory(
        StockShortSaleListDAO,
        records=[
            short_sale_row(DAY_1, "2330"),
            short_sale_row(DAY_1, "2317", margin=1),
            short_sale_row(DAY_1, "2454", sbl=1, below=1),
        ],
    )

    snapshot: Optional[ShortSaleListSnapshot] = StockShortSaleListAPI(
        conn=memory_conn
    ).get_snapshot(DAY_1)

    assert snapshot.listed == {"2330", "2317", "2454"}
    assert snapshot.margin_halted == {"2317"}
    assert snapshot.sbl_halted == {"2454"}
    assert snapshot.below_reference_banned == {"2454"}


def test_missing_day_snapshot_is_none_not_empty(
    memory_conn: sqlite3.Connection, dao_factory: Callable[..., Any]
) -> None:
    """
    未入庫的日子回 None，不可以回空快照

    空快照的意思是「當天沒有任何證券可放空」，會讓每一張單都被擋。
    """

    dao_factory(StockDayTradeListDAO, records=[])

    assert StockDayTradeListAPI(conn=memory_conn).get_snapshot(DAY_1) is None


# === 入口 ===
def test_backtest_entry_reports_coverage_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """入口把涵蓋錯誤印到 stderr 並回 1，不丟 traceback"""

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise TradingListCoverageError("名單缺 3 個交易日")

    monkeypatch.setattr(backtest_entry, "build_backtester", refuse)

    code: int = backtest_entry.main(["--strategy", "VolumeBreakoutMomentumStrategy"])

    assert code == 1
    assert "名單缺 3 個交易日" in capsys.readouterr().err

import datetime
import importlib
import inspect
import pkgutil
import sqlite3
from typing import List, Type

import pytest

import core.dao.tw as dao_package
from core.api.tw.futures_continuous_api import FuturesContinuousAPI
from core.api.tw.stock_day_trade_list_api import StockDayTradeListAPI
from core.api.tw.stock_margin_api import StockMarginAPI
from core.api.tw.stock_short_sale_list_api import StockShortSaleListAPI
from core.dao.base import BaseDAO
from core.dao.tw.financial_statement_dao import FinancialStatementDAO
from core.dao.tw.futures_chip_dao import FuturesChipDAO
from core.dao.tw.stock_chip_dao import StockChipDAO
from core.dao.tw.stock_price_dao import StockPriceDAO

"""
缺表語意統一：由 `BaseDAO.OPTIONAL_TABLE` 宣告，可缺的回空、不可缺的拋出

原本缺表的處理有三種：籌碼 DAO 回空表、台股籌碼直接拋錯、期貨交易日回空並警告；
API 層另有 6 處自己檢查 `table_exists()`，同一張表在不同入口有不同答案。
"""

DAY: datetime.date = datetime.date(2026, 1, 5)


def dao_classes() -> List[Type[BaseDAO]]:
    """`core/dao/tw/` 裡所有繼承 `BaseDAO` 的類別"""

    found: List[Type[BaseDAO]] = []
    for module_info in pkgutil.iter_modules(dao_package.__path__):
        module = importlib.import_module(f"{dao_package.__name__}.{module_info.name}")
        for _, cls in inspect.getmembers(module, inspect.isclass):
            if (
                issubclass(cls, BaseDAO)
                and cls is not BaseDAO
                and cls.__module__ == module.__name__
            ):
                found.append(cls)
    return sorted(found, key=lambda cls: cls.__name__)


def instantiate(cls: Type[BaseDAO], conn: sqlite3.Connection) -> BaseDAO:
    """多表 DAO 要帶表名"""

    if cls is FuturesChipDAO:
        return cls("futures_large_trader", conn=conn)
    if cls is FinancialStatementDAO:
        return cls("balance_sheet", conn=conn)
    return cls(conn=conn)


def test_every_dao_declares_the_semantic_explicitly() -> None:
    """掃描本身要有結果；行情表（`price`）與台股籌碼表是不可缺的，其餘多數可缺"""

    classes: List[Type[BaseDAO]] = dao_classes()

    assert len(classes) >= 15
    assert StockPriceDAO.OPTIONAL_TABLE is False
    assert StockChipDAO.OPTIONAL_TABLE is False
    optional: List[str] = [cls.__name__ for cls in classes if cls.OPTIONAL_TABLE]
    assert len(optional) >= 12, optional


@pytest.mark.parametrize(
    "cls",
    [cls for cls in dao_classes() if cls.OPTIONAL_TABLE],
    ids=lambda cls: cls.__name__,
)
def test_optional_table_queries_return_empty_when_missing(cls: Type[BaseDAO]) -> None:
    """可缺的表：共用查詢在表不存在時回空，不碰 SQLite"""

    dao: BaseDAO = instantiate(cls, sqlite3.connect(":memory:"))

    assert dao.missing_optional_table()
    assert dao.get_by_date(DAY).empty
    assert dao.get_distinct_dates(DAY, DAY) == []
    range_params: List[str] = list(inspect.signature(dao.get_range).parameters)
    if range_params[:2] == ["start_date", "end_date"]:
        assert dao.get_range(DAY, DAY).empty
    else:
        # 年季／年月的區間（財報、月營收）
        assert dao.get_range(2024, 1, 2024, 2).empty
    if len(dao.KEY_COLUMNS) == 1:
        assert dao.get_by_stock("x", DAY, DAY).empty


@pytest.mark.parametrize(
    "cls", [StockPriceDAO, StockChipDAO], ids=lambda cls: cls.__name__
)
def test_required_table_queries_raise_when_missing(cls: Type[BaseDAO]) -> None:
    """不可缺的表：缺了就讓 SQLite 的錯誤現形，不回空"""

    dao: BaseDAO = instantiate(cls, sqlite3.connect(":memory:"))

    assert not dao.missing_optional_table()
    with pytest.raises(sqlite3.OperationalError):
        dao.get_by_date(DAY)
    with pytest.raises(sqlite3.OperationalError):
        dao.get_distinct_dates(DAY, DAY)


def test_optional_table_present_but_broken_still_raises() -> None:
    """表存在但查詢出錯時照常拋出：可缺只豁免「表不存在」這一種情況"""

    conn: sqlite3.Connection = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE futures_large_trader (investor TEXT)")  # 故意缺 date 欄
    dao: FuturesChipDAO = FuturesChipDAO("futures_large_trader", conn=conn)

    assert not dao.missing_optional_table()
    with pytest.raises(sqlite3.OperationalError):
        dao.get_by_date(DAY)


# === API 層不再自己檢查表存不存在 ===
def test_apis_no_longer_check_table_existence_themselves() -> None:
    """原本 6 處 API 層的 `table_exists()` 檢查，可缺的那 5 處都交給 DAO"""

    for api_cls in (
        StockMarginAPI,
        StockDayTradeListAPI,
        StockShortSaleListAPI,
        FuturesContinuousAPI,
    ):
        assert "table_exists" not in inspect.getsource(api_cls), api_cls.__name__


def test_apis_behave_the_same_on_a_fresh_database() -> None:
    """全新環境：名單 API 回空／None、融券餘額對照表為空、連續合約序列為空"""

    conn: sqlite3.Connection = sqlite3.connect(":memory:")

    assert StockMarginAPI(conn=conn).get_short_balance_map(DAY) == {}
    for api_cls in (StockDayTradeListAPI, StockShortSaleListAPI):
        api = api_cls(conn=conn)
        assert api.get_covered_dates(DAY, DAY) == []
        assert api.get_snapshot(DAY) is None

    from core.utils import FuturesAdjustMethod, FuturesRollRule, FuturesSession

    series = FuturesContinuousAPI(conn=conn).get_series(
        "TX",
        DAY,
        DAY,
        session=FuturesSession.DAY,
        method=FuturesAdjustMethod.BACKWARD,
        roll_rule=FuturesRollRule.LAST_TRADING_DAY,
    )
    assert series.empty

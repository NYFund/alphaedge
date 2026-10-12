import datetime
import importlib
import inspect
import pkgutil
import re
import sqlite3
from typing import Any, Dict, List, Set, Tuple, Type

import pandas as pd
import pytest

import core.dao.tw as dao_package
from core.api.base import BaseDataAPI
from core.api.tw.futures_price_api import FuturesPriceAPI
from core.dao.base import BaseDAO
from core.dao.tw.futures_chip_dao import FuturesChipDAO
from core.dao.tw.futures_price_dao import FuturesPriceDAO
from core.dao.tw.stock_price_dao import StockPriceDAO

"""
資料層基底去掉「股票形狀」：鍵欄逐表宣告、對照表的鍵是參數

原本 `BaseDAO.SYMBOL_COLUMN = "stock_id"` 與 `get_by_stock()` 也被期貨 DAO 繼承，
期貨表沒有 `stock_id`，一呼叫就是 SQL 錯誤；`BaseDataAPI.build_column_map()` 的鍵寫死
`stock_id`，期貨只好另寫一份逐字相同的 `build_expiry_map()`。
"""

PK_PATTERN: re.Pattern = re.compile(r"PRIMARY KEY \(([^)]*)\)")


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


def literal_primary_keys(cls: Type[BaseDAO]) -> List[Set[str]]:
    """從 `create_table()` 原始碼刮出字面寫死的主鍵欄位（動態 schema 的表刮不到）"""

    source: str = inspect.getsource(cls.create_table)
    return [
        {column.strip().strip('"') for column in match.split(",")}
        for match in PK_PATTERN.findall(source)
        if "{" not in match  # f-string 佔位＝動態 schema，刮不到字面主鍵
    ]


def test_every_dao_is_scanned() -> None:
    """掃描本身要有結果，否則下面的守門會無條件通過"""

    classes: List[Type[BaseDAO]] = dao_classes()

    assert len(classes) >= 15, [cls.__name__ for cls in classes]
    assert FuturesPriceDAO in classes and StockPriceDAO in classes


@pytest.mark.parametrize("cls", dao_classes(), ids=lambda cls: cls.__name__)
def test_key_columns_match_the_literal_primary_key(cls: Type[BaseDAO]) -> None:
    """
    字面寫死主鍵的表：鍵欄必須是「主鍵扣掉日期欄」的子集，且主鍵裡真的有這些欄

    沒宣告的表沿用台股預設 `("stock_id",)`——如果主鍵裡沒有 `stock_id`，
    `get_by_stock()` 就會以 SQL 錯誤的面貌出現，而不是明確的「不支援」。
    """

    primary_keys: List[Set[str]] = literal_primary_keys(cls)
    if not primary_keys:
        pytest.skip("動態 schema，主鍵由建構子或欄位清單決定")

    key_columns: Set[str] = set(cls.KEY_COLUMNS)
    non_date: Set[str] = primary_keys[0] - {cls.DATE_COLUMN}

    assert key_columns <= primary_keys[0], (
        f"{cls.__name__}.KEY_COLUMNS={cls.KEY_COLUMNS} 不在主鍵 {primary_keys[0]} 內"
    )
    assert key_columns or not non_date, (
        f"{cls.__name__} 的主鍵有日期以外的欄 {non_date}，但 KEY_COLUMNS 是空的"
    )


def test_futures_dao_refuses_symbol_queries_explicitly() -> None:
    """期貨表的鍵是組合：`get_by_stock()` 拋 `NotImplementedError`，不是 SQL 錯誤"""

    conn: sqlite3.Connection = sqlite3.connect(":memory:")
    dao: FuturesPriceDAO = FuturesPriceDAO(conn=conn)
    dao.ensure_table()

    with pytest.raises(NotImplementedError, match="沒有單一標的欄"):
        dao.get_by_stock("TX", datetime.date(2024, 1, 1), datetime.date(2024, 1, 31))

    assert dao.KEY_COLUMNS == ("product", "expiry", "session")
    assert FuturesChipDAO.KEY_COLUMNS == ()
    assert FuturesChipDAO("futures_large_trader", conn=conn).KEY_COLUMNS == (
        "product",
        "expiry",
        "trader_type",
    )


def test_stock_dao_symbol_query_and_index_use_the_declared_key() -> None:
    """台股表的鍵欄是 `stock_id`：`get_by_stock()` 照常可用，複合索引建在它上面"""

    conn: sqlite3.Connection = sqlite3.connect(":memory:")
    dao: StockPriceDAO = StockPriceDAO(conn=conn)
    dao.ensure_table()
    dao.insert_or_ignore(
        pd.DataFrame(
            {
                "date": ["2024-01-02", "2024-01-03"],
                "stock_id": ["2330", "2317"],
                "證券名稱": ["台積電", "鴻海"],
            }
        )
    )
    dao.commit()

    rows: pd.DataFrame = dao.get_by_stock(
        "2330", datetime.date(2024, 1, 1), datetime.date(2024, 1, 31)
    )
    assert rows["stock_id"].tolist() == ["2330"]

    indexes: List[str] = [
        row[1] for row in conn.execute(f"PRAGMA index_list({dao.TABLE_NAME})")
    ]
    assert f"idx_{dao.TABLE_NAME}_stock_id_date" in indexes


# === 對照表的鍵是參數 ===
def test_column_map_key_is_a_parameter() -> None:
    """台股用 `stock_id`、期貨用 `expiry`，同一支函式；缺鍵欄回空 dict"""

    df: pd.DataFrame = pd.DataFrame(
        {
            "stock_id": ["2330", "2330", "2317"],
            "expiry": ["202401", "202402", "202401"],
            "v": [1, 2, 3],
        }
    )

    assert BaseDataAPI.build_column_map(df, "v") == {"2330": 1, "2317": 3}
    assert BaseDataAPI.build_column_map(df, "v", key="expiry") == {
        "202401": 1,
        "202402": 2,
    }
    assert BaseDataAPI.build_column_map(df, "v", key="missing") == {}


def test_futures_expiry_map_delegates_to_the_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """期貨的 `build_expiry_map()` 只是以到期月當鍵呼叫基底，到期月轉成字串"""

    seen: Dict[str, Any] = {}

    def spy(df: pd.DataFrame, column: str, key: str = "stock_id") -> Dict[Any, Any]:
        seen["key"] = key
        return {202401: 1.5}

    monkeypatch.setattr(BaseDataAPI, "build_column_map", staticmethod(spy))

    result: Dict[str, Any] = FuturesPriceAPI.build_expiry_map(pd.DataFrame(), "收盤價")

    assert seen["key"] == "expiry"
    assert result == {"202401": 1.5}
    assert "build_column_map" not in FuturesPriceAPI.__dict__


def test_symbol_date_index_follows_the_declared_key() -> None:
    """複合索引建在宣告的鍵欄上，不是寫死的 `stock_id`"""

    class _ProductDAO(BaseDAO):
        TABLE_NAME: str = "product_daily"
        KEY_COLUMNS: Tuple[str, ...] = ("product",)
        NEEDS_SYMBOL_DATE_INDEX: bool = True

        def create_table(self) -> None:
            self.conn.execute(
                'CREATE TABLE product_daily ("date" TEXT, "product" TEXT, PRIMARY KEY ("date", "product"))'
            )
            self.conn.commit()

    conn: sqlite3.Connection = sqlite3.connect(":memory:")
    _ProductDAO(conn=conn).ensure_table()

    indexes: List[str] = [
        row[1] for row in conn.execute("PRAGMA index_list(product_daily)")
    ]
    assert "idx_product_daily_product_date" in indexes

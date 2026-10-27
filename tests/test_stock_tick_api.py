import datetime

import pandas as pd
import pytest

import core.dao.tw.stock_tick_dao as stock_tick_dao_module
from core.dao.tw.stock_tick_dao import StockTickDAO

"""
台股 tick 讀取端不需要資料庫的檢查：查詢參數驗證、schema 名稱驗證、連不上時的提示

ConnectorX 不支援參數佔位符，值只能嵌進 SQL；這幾道驗證是唯一的防線，
所以獨立出來、讓沒有 TimescaleDB 的 CI 也會跑。
"""

DAY: datetime.date = datetime.date(2024, 5, 8)


def _dao() -> StockTickDAO:
    """不連線的 DAO 空殼"""

    dao: StockTickDAO = StockTickDAO.__new__(StockTickDAO)
    dao.schema = "public"
    return dao


@pytest.mark.parametrize("stock_id", ["2330'; DROP TABLE stock_tick; --", "23 30", ""])
def test_query_rejects_non_alphanumeric_stock_id(stock_id: str) -> None:
    """清單裡每個代號都只接受英數字；混在合法代號後面也在送出查詢之前就拒絕"""

    with pytest.raises(ValueError, match="stock_id"):
        _dao().query_ticks(DAY, DAY, ("time", "seq"), stock_ids=["2330", stock_id])


def test_query_rejects_single_string_as_stock_ids() -> None:
    """單一字串也是 Sequence，不擋的話 "2330" 會被當成 2、3、3、0 四檔"""

    with pytest.raises(TypeError, match="stock_ids"):
        _dao().query_ticks(DAY, DAY, ("time", "seq"), stock_ids="2330")


@pytest.mark.parametrize(
    "order_by",
    [
        (),
        ("time; DROP TABLE x",),
        ("loaded_at",),
        # 以下欄位都合法，但同一檔內不是依 (time, seq) 排序，累計量會算錯
        ("time",),
        ("seq", "time"),
        ("close", "time", "seq"),
    ],
)
def test_query_rejects_unknown_order_columns(order_by: tuple) -> None:
    """排序欄位只接受讀取欄位與 `seq`，且同一檔內必須依 `(time, seq)` 排序"""

    with pytest.raises(ValueError, match="排序"):
        _dao().query_ticks(DAY, DAY, order_by)


def test_empty_stock_ids_returns_empty_frame_without_querying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    空清單直接回空表、不送查詢

    策略當天沒有任何候選標的是正常情況；`stock_id IN ()` 在 PostgreSQL 是語法錯誤。
    """

    def _fail() -> str:
        raise AssertionError("空清單不應送出查詢")

    monkeypatch.setattr(stock_tick_dao_module, "get_connectorx_uri", _fail)

    ticks: pd.DataFrame = _dao().query_ticks(DAY, DAY, ("time", "seq"), stock_ids=[])

    assert ticks.empty
    assert list(ticks.columns) == [*StockTickDAO.READ_DTYPES, "cum_volume"]
    assert str(ticks["cum_volume"].dtype) == "int64"


def test_schema_name_is_validated_before_connecting() -> None:
    """schema 名稱會直接嵌進 SQL，不合法時在建構當下拒絕（傳入連線也不例外）"""

    with pytest.raises(ValueError, match="schema"):
        StockTickDAO(conn=object(), schema='public"; DROP SCHEMA public; --')


def test_unreachable_database_explains_how_to_start_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    連不上時錯誤訊息要說明怎麼啟動資料庫

    psycopg 的原始訊息只有 connection refused，看不出 TimescaleDB 跑在 Docker 裡、
    要先開 Docker Desktop。
    """

    pytest.importorskip("psycopg")
    import core.config.settings as settings
    from core.dao.timescale import connect_tick_db

    # port 1 不會有服務：連線當場被拒，不必等逾時
    monkeypatch.setattr(
        settings,
        "TICK_DATABASE_URL",
        "postgresql://user:pass@127.0.0.1:1/db?connect_timeout=2",
    )

    with pytest.raises(ConnectionError, match="docker compose up -d postgres"):
        connect_tick_db()

import datetime

import pytest

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
    """代號只接受英數字；在送出查詢之前就拒絕"""

    with pytest.raises(ValueError, match="stock_id"):
        _dao().query_ticks(DAY, DAY, ("time", "seq"), stock_id=stock_id)


@pytest.mark.parametrize("order_by", [(), ("time; DROP TABLE x",), ("loaded_at",)])
def test_query_rejects_unknown_order_columns(order_by: tuple) -> None:
    """排序欄位只接受讀取欄位與 `seq`"""

    with pytest.raises(ValueError, match="排序"):
        _dao().query_ticks(DAY, DAY, order_by)


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

import datetime
from typing import List

import pandas as pd
import pytest

from core.adapters.tw.futures_quote_adapter import FuturesQuoteAdapter
from core.models import FuturesQuote
from core.utils import FuturesSession, Scale
from core.utils.constant import FUTURES_MULTIPLIER

"""
行情 DataFrame → `FuturesQuote` 的轉換測試

本 adapter **只做型別轉換，不做任何選擇**：單日單商品的多個到期月一律全部轉出，
換月是政策，不屬於這一層。

真正會出事的是**空值處理**：夜盤沒有結算價與未沖銷契約量，`NaN` 若被轉成 0，
逐日盯市會把部位結算成歸零而不報錯，故單獨釘住。
"""

DATE: datetime.date = datetime.date(2026, 8, 26)
MULTIPLIER: int = FUTURES_MULTIPLIER["TX"]


def make_df(rows: List[dict]) -> pd.DataFrame:
    """組出與 `futures_price_daily` 欄位一致的 DataFrame"""

    return pd.DataFrame(rows)


DAY_ROW: dict = {
    "date": str(DATE),
    "product": "TX",
    "expiry": "202609",
    "session": "day",
    "開盤價": 46000.0,
    "最高價": 46500.0,
    "最低價": 45900.0,
    "收盤價": 46100.0,
    "成交量": 50000,
    "結算價": 46090.0,
    "未沖銷契約量": 100000,
    "最後最佳買價": 46095.0,
    "最後最佳賣價": 46105.0,
}

NIGHT_ROW: dict = {
    **DAY_ROW,
    "session": "night",
    "收盤價": 46050.0,
    "成交量": 26000,
    # 夜盤沒有這兩項
    "結算價": float("nan"),
    "未沖銷契約量": float("nan"),
}


def test_row_becomes_a_quote_with_contract_id() -> None:
    """契約代號由 product ＋ expiry 組成"""

    quotes: List[FuturesQuote] = FuturesQuoteAdapter.from_day_rows(
        make_df([DAY_ROW]), DATE
    )

    assert len(quotes) == 1
    assert quotes[0].contract_id == "TX202609"
    assert quotes[0].symbol == "TX202609"


def test_ohlc_and_current_price() -> None:
    """`cur_price` 取收盤價，與日線的股票 adapter 行為一致"""

    quote: FuturesQuote = FuturesQuoteAdapter.from_day_rows(make_df([DAY_ROW]), DATE)[0]

    assert (quote.open, quote.high, quote.low, quote.close) == (
        46000.0,
        46500.0,
        45900.0,
        46100.0,
    )
    assert quote.cur_price == 46100.0
    assert quote.scale == Scale.DAY
    assert quote.date == DATE


def test_multiplier_is_attached_from_the_registry() -> None:
    """乘數在轉換時就掛上，下游算 PnL 不必再查表"""

    quote: FuturesQuote = FuturesQuoteAdapter.from_day_rows(make_df([DAY_ROW]), DATE)[0]

    assert quote.multiplier == MULTIPLIER


def test_unregistered_product_raises() -> None:
    """乘數未登錄的商品當場 KeyError，不靜默跳過"""

    with pytest.raises(KeyError):
        FuturesQuoteAdapter.from_day_rows(
            make_df([{**DAY_ROW, "product": "XIF"}]), DATE
        )


# === 空值處理 ===
def test_night_session_nulls_stay_none() -> None:
    """
    夜盤的結算價與未沖銷契約量必須是 None，不可變成 0

    轉成 0 會讓逐日盯市把部位結算成歸零而不報錯，是災難性的靜默錯誤。
    """

    quote: FuturesQuote = FuturesQuoteAdapter.from_day_rows(make_df([NIGHT_ROW]), DATE)[
        0
    ]

    assert quote.session == FuturesSession.NIGHT
    assert quote.settlement_price is None
    assert quote.open_interest is None


def test_day_session_keeps_its_values() -> None:
    """日盤則有結算價與未沖銷契約量，用來與夜盤的 None 對照"""

    quote: FuturesQuote = FuturesQuoteAdapter.from_day_rows(make_df([DAY_ROW]), DATE)[0]

    assert quote.settlement_price == 46090.0
    assert quote.open_interest == 100000


# === 不做選擇 ===
def test_every_expiry_is_converted() -> None:
    """多個到期月全部轉出，adapter 不挑近月"""

    rows: List[dict] = [
        DAY_ROW,
        {**DAY_ROW, "expiry": "202610", "收盤價": 46000.0},
        {**DAY_ROW, "expiry": "202612", "收盤價": 45900.0},
    ]

    quotes: List[FuturesQuote] = FuturesQuoteAdapter.from_day_rows(make_df(rows), DATE)

    assert [q.expiry for q in quotes] == ["202609", "202610", "202612"]


def test_both_sessions_can_coexist() -> None:
    """
    同一契約的日盤與夜盤是兩筆報價

    這在股票側會被當成重複 symbol 而警告，期貨則是正常狀態。
    """

    quotes: List[FuturesQuote] = FuturesQuoteAdapter.from_day_rows(
        make_df([DAY_ROW, NIGHT_ROW]), DATE
    )

    assert len(quotes) == 2
    assert {q.session for q in quotes} == {FuturesSession.DAY, FuturesSession.NIGHT}
    assert {q.contract_id for q in quotes} == {"TX202609"}


def test_empty_dataframe_gives_empty_list() -> None:
    """查無資料時回傳空 list，不是 None"""

    assert FuturesQuoteAdapter.from_day_rows(pd.DataFrame(), DATE) == []


def test_no_adjusted_close_for_futures() -> None:
    """期貨沒有除權息還原，`signal_close` 一律退回原始收盤價"""

    quote: FuturesQuote = FuturesQuoteAdapter.from_day_rows(make_df([DAY_ROW]), DATE)[0]

    assert quote.adj_close is None
    assert quote.signal_close == quote.close


def test_entry_points_mirror_the_stock_adapter() -> None:
    """
    入口與台股 adapter 同構：DataFrame → `from_day_records()`（列）→ `from_day_row()`

    兩支 adapter 讀一個就要看得懂另一個；直接餵列與餵 DataFrame 的結果相同。
    """

    from core.adapters.tw.stock_quote_adapter import StockQuoteAdapter

    for name in ("from_day_rows", "from_day_records", "from_day_row"):
        assert hasattr(FuturesQuoteAdapter, name)
        assert hasattr(StockQuoteAdapter, name)

    df: pd.DataFrame = make_df([DAY_ROW])
    from_rows: List[FuturesQuote] = FuturesQuoteAdapter.from_day_records(
        list(df.itertuples(index=False)), DATE
    )
    from_df: List[FuturesQuote] = FuturesQuoteAdapter.from_day_rows(df, DATE)

    assert [vars(quote) for quote in from_rows] == [vars(quote) for quote in from_df]
    assert from_rows[0].scale == Scale.DAY

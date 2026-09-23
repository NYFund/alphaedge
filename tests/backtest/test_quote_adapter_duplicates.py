import datetime
from typing import List

from loguru import logger

from core.adapters.quote_validation import warn_duplicate_symbols
from core.models import StockQuote
from tests.conftest import build_stock_quote

"""報價轉換的重複代號防護

`price` 表把上市與上櫃資料合併存放卻沒有市場欄位，早年上櫃 ETF 的 4 碼代號
會與上市股票相撞（例如 6201 同時是 亞弘電 與 元大富櫃50）。引擎之後以
`{q.symbol: q for q in quotes}` 建對照表，重複的只會留下最後一筆——
成交價與訊號都可能取到另一檔商品，且完全不會報錯。
"""


DAY: datetime.date = datetime.date(2016, 6, 27)


def make_quote(stock_id: str, cur_price: float) -> StockQuote:
    """建立最小可用的日 K 報價"""

    return build_stock_quote(stock_id=stock_id, date=DAY, cur_price=cur_price)


def capture_warnings(quotes: List[StockQuote]) -> List[str]:
    """收集 warn_duplicate_symbols 發出的 WARNING 訊息"""

    messages: List[str] = []
    sink_id: int = logger.add(
        lambda m: messages.append(m), level="WARNING", format="{message}"
    )
    try:
        warn_duplicate_symbols(quotes, DAY, source="Stock")
    finally:
        logger.remove(sink_id)
    return messages


def test_duplicate_symbol_emits_warning() -> None:
    """同一 bar 內同代號出現兩筆時必須留痕，不可靜默"""

    quotes: List[StockQuote] = [
        make_quote("6201", 30.0),  # 亞弘電
        make_quote("6201", 12.5),  # 元大富櫃50（早年以 4 碼代號發布）
        make_quote("2330", 500.0),
    ]

    messages: List[str] = capture_warnings(quotes)

    assert len(messages) == 1
    assert "6201" in messages[0]
    assert "2330" not in messages[0]


def test_unique_symbols_emit_nothing() -> None:
    """代號皆唯一時不得產生任何警告，避免正常路徑被噪音淹沒"""

    quotes: List[StockQuote] = [
        make_quote("2330", 500.0),
        make_quote("2317", 100.0),
        make_quote("006201", 12.5),
    ]

    assert capture_warnings(quotes) == []


def test_empty_quotes_emit_nothing() -> None:
    """空報價（非交易日或資料缺口）不得誤報"""

    assert capture_warnings([]) == []

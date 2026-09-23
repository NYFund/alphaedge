import datetime
from typing import Any, List

import pandas as pd
import pytest
from loguru import logger

from core.adapters.quote_validation import (
    find_duplicate_symbols,
    has_valid_price,
    resolve_close,
    warn_duplicate_symbols,
)
from core.models import FuturesQuote, StockQuote
from core.utils import Scale

"""
報價驗證：來源無關的純函式

三條規則原本只長在 `StockQuoteAdapter` 上，而另外兩條路徑一條都沒有：
實盤的券商快照會把缺值變成 0 元報價，期貨回測完全沒有重複代號偵測。
每一條規則的存在都對應一個踩過的坑：

| 規則 | 防的是什麼 |
|------|------------|
| `has_valid_price()` | 無成交日的 OHLC 是 NULL（舊 cleaner 填成 0），不濾掉會用 0 元成交 |
| `warn_duplicate_symbols()` | 上市股與上櫃 ETF 共用 4 碼代號，建對照表時只留最後一筆，**成交價與訊號都可能取到另一檔商品** |
| `resolve_close()` | 券商快照缺值時回 0，而回測那側是直接濾掉——兩邊不一致且沒有錯誤訊息 |

本檔以**值**（不是列）測邊界，因為這一層刻意與來源的欄位名無關。
"""


DATE: datetime.date = datetime.date(2024, 1, 2)


def make_quote(symbol: str = "2330") -> StockQuote:
    """只需要 `symbol` 的最小報價"""

    return StockQuote(stock_id=symbol, scale=Scale.DAY, date=DATE, cur_price=100.0)


def capture_warnings(call: Any) -> List[str]:
    """收集一次呼叫發出的 WARNING 訊息"""

    messages: List[str] = []
    sink_id: int = logger.add(
        lambda m: messages.append(str(m)), level="WARNING", format="{message}"
    )
    try:
        call()
    finally:
        logger.remove(sink_id)
    return messages


# === has_valid_price：規格指名的四種形態 ===
@pytest.mark.parametrize(
    ("close", "expected"),
    [
        (None, False),  # 無成交日：cleaner 現在保留 NULL
        (float("nan"), False),  # pandas 讀出來的空值
        (0, False),  # 舊 cleaner 把 `--` 填成 0
        (0.0, False),
        (-1.0, False),  # 不是任何一種合法價格，出現就是上游解析錯了
        (0.01, True),
        (600.0, True),
    ],
)
def test_price_validity(close: Any, expected: bool) -> None:
    """`None`／`NaN`／`0`／負數都不可拿來成交"""

    assert has_valid_price(close) is expected


def test_pandas_na_is_invalid() -> None:
    """`pd.NA` 與 `pd.NaT` 同樣要擋掉——它們不是 float，但確實會從資料庫讀出來"""

    assert has_valid_price(pd.NA) is False
    assert has_valid_price(pd.NaT) is False


def test_non_numeric_is_invalid_not_an_exception() -> None:
    """
    非數值視為無效而不是往上拋

    讓它拋的話，一列壞資料會在轉換途中炸掉**整天**的報價；
    回 False 只損失那一檔，而且會留下可查的痕跡。
    """

    assert has_valid_price("--") is False
    assert has_valid_price(object()) is False


def test_numeric_strings_are_accepted_by_design() -> None:
    """
    **字串型的數字目前會通過**，這是抽取前就有的行為

    `float("600")` 成立，所以 `"600"` 被當成有效價。嚴格說它代表上游的型別
    已經跑掉了，理想上該擋——但**收緊判準會改變回測結果**（任何一欄若曾以
    字串形態存在，那些列會從「有效」變成「被濾掉」），而本步驟是純抽取。

    釘住現況是為了讓日後有人想收緊時，知道這不是疏漏而是刻意留著的，
    並且會看到「要一併重產回歸基準」這件事。
    """

    assert has_valid_price("600") is True
    assert has_valid_price("0") is False
    assert has_valid_price("abc") is False


# === resolve_close：實盤路徑的關鍵 ===
def test_resolve_close_returns_none_not_zero() -> None:
    """
    無效價回 `None`，**不是 0**

    這是實盤與回測分家的那一處：回測濾掉該檔，實盤原本拿 0 元去算訊號。
    """

    assert resolve_close(None) is None
    assert resolve_close(0.0) is None
    assert resolve_close(float("nan")) is None
    assert resolve_close(-5.0) is None


def test_resolve_close_passes_valid_prices_through() -> None:
    """有效價原樣轉成 float"""

    assert resolve_close(600) == 600.0
    assert resolve_close(0.01) == 0.01


# === 重複代號 ===
def test_duplicates_are_found_and_sorted() -> None:
    """回傳已排序，讓訊息與斷言都穩定"""

    quotes: List[StockQuote] = [
        make_quote("6201"),
        make_quote("2330"),
        make_quote("6201"),
        make_quote("2330"),
        make_quote("1101"),
    ]

    assert find_duplicate_symbols(quotes) == ["2330", "6201"]


def test_no_duplicates_returns_empty() -> None:
    """沒有重複時回空 list，呼叫端不必分辨 None 與空"""

    assert find_duplicate_symbols([make_quote("2330"), make_quote("2317")]) == []
    assert find_duplicate_symbols([]) == []


def test_duplicate_emits_warning_and_returns_them() -> None:
    """
    只警告不排除

    要留哪一筆屬資料修正的範疇，靜默挑一筆才是更糟的選擇——
    而「挑到另一檔商品的成交價」不會有任何錯誤訊息。
    """

    quotes: List[StockQuote] = [
        make_quote("6201"),  # 亞弘電
        make_quote("6201"),  # 元大富櫃50（早年以 4 碼代號發布）
        make_quote("2330"),
    ]

    messages: List[str] = capture_warnings(
        lambda: warn_duplicate_symbols(quotes, DATE, source="Stock")
    )

    assert len(messages) == 1
    assert "6201" in messages[0]
    assert "[Stock]" in messages[0]
    # 三筆報價原樣留著，沒有被偷偷挑掉一筆
    assert len(quotes) == 3


def test_unique_symbols_emit_nothing() -> None:
    """沒有重複就不要出聲；每天都噴的 warning 等於沒有 warning"""

    messages: List[str] = capture_warnings(
        lambda: warn_duplicate_symbols([make_quote("2330")], DATE)
    )

    assert messages == []


def test_source_label_distinguishes_paths() -> None:
    """
    來源標籤要能分辨是哪一條路徑

    股票、期貨、期貨合併三條路徑共用同一份規則，log 若不帶來源，
    看到 warning 也不知道要去查哪一張表。
    """

    quotes: List[StockQuote] = [make_quote("2330"), make_quote("2330")]

    for source in ("Stock", "Futures", "Futures合併"):
        messages: List[str] = capture_warnings(
            lambda s=source: warn_duplicate_symbols(quotes, DATE, source=s)
        )
        assert f"[{source}]" in messages[0]


# === 期貨端首次套用 ===
def test_futures_quotes_share_the_same_rule() -> None:
    """
    期貨的 `symbol` 是 `{商品}{到期月}`，同樣會重複

    日盤／夜盤同契約、合併時段失敗、週契約與月契約代號碰撞都會撞在一起，
    而引擎對期貨同樣建 `{q.symbol: q for q in quotes}`。
    """

    quotes: List[FuturesQuote] = [
        FuturesQuote(product="TX", expiry="202403", scale=Scale.DAY, date=DATE),
        FuturesQuote(product="TX", expiry="202403", scale=Scale.DAY, date=DATE),
        FuturesQuote(product="TX", expiry="202404", scale=Scale.DAY, date=DATE),
    ]

    assert find_duplicate_symbols(quotes) == ["TX202403"]


def test_the_rule_is_source_agnostic() -> None:
    """
    同一份規則吃得下股票與期貨兩種報價物件

    簽章吃 `BaseQuote` 而不是任一市場的子型別，這一層才不會又長出第二份。
    """

    mixed: List[Any] = [
        make_quote("2330"),
        FuturesQuote(product="TX", expiry="202403", scale=Scale.DAY, date=DATE),
        make_quote("2330"),
    ]

    assert find_duplicate_symbols(mixed) == ["2330"]

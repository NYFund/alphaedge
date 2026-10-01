import datetime
from typing import Optional, Tuple

import pytest

from core.market.instrument_spec import InstrumentSpec
from core.market.tw.instrument_spec import TwStockSpec
from core.utils import Action
from core.utils.instrument import StockUtils

"""InstrumentSpec 測試：台股規格的三個介面必須與既有 StockUtils 逐值相同"""


@pytest.fixture
def spec() -> TwStockSpec:
    """台股商品規格"""

    return TwStockSpec()


def test_spec_is_instrument_spec(spec: TwStockSpec) -> None:
    """TwStockSpec 必須實作抽象介面，期貨才有對稱的落點"""

    assert isinstance(spec, InstrumentSpec)


@pytest.mark.parametrize(
    "lots, expected", [(0, 0), (1, 1000), (5, 5000), (100, 100000)]
)
def test_to_units(spec: TwStockSpec, lots: int, expected: int) -> None:
    """張 → 股：1 張 ＝ 1000 股"""

    assert spec.to_units(lots) == expected
    assert spec.to_units(lots) == StockUtils.convert_lot_to_share(lots)


@pytest.mark.parametrize(
    "price, direction, expected",
    [
        (9.993, "down", 9.99),
        (9.991, "up", 10.0),
        (10.02, "down", 10.0),
        (10.02, "up", 10.05),
        (49.99, "up", 50.0),
        (50.02, "down", 50.0),
        (99.95, "up", 100.0),
        (100.2, "down", 100.0),
        (100.2, "up", 100.5),
        (499.6, "up", 500.0),
        (500.5, "down", 500.0),
        (999.5, "up", 1000.0),
        (1002.0, "down", 1000.0),
        (1002.0, "up", 1005.0),
        (100.3, "nearest", 100.5),
    ],
)
def test_round_to_tick(
    spec: TwStockSpec, price: float, direction: str, expected: float
) -> None:
    """六段檔位的邊界取整：沿用 test_cost_model 的既有 15 組測資"""

    assert spec.round_to_tick(price, direction) == expected


def test_round_to_tick_default_direction(spec: TwStockSpec) -> None:
    """未指定方向時預設就近取整，與 StockUtils 的預設一致"""

    assert spec.round_to_tick(100.3) == StockUtils.round_to_tick(100.3, "nearest")


@pytest.mark.parametrize(
    "prev_close, expected",
    [
        (100.0, (90.0, 110.0)),  # 前收 100 → 跌停 90、漲停 110
        (9.99, (9.0, 10.95)),  # 跨檔位：漲停落在 0.05 檔、跌停落在 0.01 檔
        (1000.0, (900.0, 1100.0)),
    ],
)
def test_get_price_limits(
    spec: TwStockSpec, prev_close: float, expected: Tuple[float, float]
) -> None:
    """漲跌停為前收 ±10%，並各自往內對齊檔位（漲停捨去、跌停進位）"""

    assert spec.get_price_limits(prev_close) == expected


def test_get_price_limits_rounds_inward(spec: TwStockSpec) -> None:
    """對齊方向不可對調：漲停必須 ≤ 理論值、跌停必須 ≥ 理論值"""

    prev_close: float = 33.3
    limit_down, limit_up = spec.get_price_limits(prev_close)

    assert limit_up <= prev_close * 1.1
    assert limit_down >= prev_close * 0.9


@pytest.mark.parametrize("prev_close", [0.0, None])
def test_get_price_limits_without_prev_close(
    spec: TwStockSpec, prev_close: Optional[float]
) -> None:
    """尚未取得前收時不做漲跌停判定，維持既有的「跳過該項檢查」行為"""

    assert spec.get_price_limits(prev_close) == (None, None)


# === 漲跌停幅度的年代分段 ===
def test_price_limit_ratio_before_2015_06_01() -> None:
    """
    台股於 2015-06-01 由 7% 放寬為 10%

    以 23,972 筆交易所公告值實測：放寬前中位數 6.92%、之後 9.91%。
    單用 10% 會讓 2013-01~2015-05 的區間偏寬約 43%，該期間相符率為 0.0%
    """

    spec: TwStockSpec = TwStockSpec()

    assert spec.get_price_limit_ratio(datetime.date(2014, 7, 1)) == 0.07
    assert spec.get_price_limit_ratio(datetime.date(2015, 5, 31)) == 0.07
    assert spec.get_price_limit_ratio(datetime.date(2015, 6, 1)) == 0.10
    assert spec.get_price_limit_ratio(datetime.date(2024, 1, 4)) == 0.10


def test_price_limit_ratio_defaults_to_current() -> None:
    """未提供日期時採現行幅度——呼叫端沒給日期即視為當代回測"""

    assert TwStockSpec().get_price_limit_ratio() == 0.10


def test_price_limits_use_era_specific_ratio() -> None:
    """同一個基準價在兩個年代算出不同的漲跌停區間"""

    spec: TwStockSpec = TwStockSpec()

    assert spec.get_price_limits(100.0, datetime.date(2014, 7, 1)) == (93.0, 107.0)
    assert spec.get_price_limits(100.0, datetime.date(2024, 1, 4)) == (90.0, 110.0)


def test_price_limits_match_official_announcement() -> None:
    """
    以交易所公告值反向驗證：聯發科 2024-01-04 除權息日

    開盤競價基準 928 元，官方公告漲停 1020、跌停 836
    """

    assert TwStockSpec().get_price_limits(928.0, datetime.date(2024, 1, 4)) == (
        836.0,
        1020.0,
    )


# === 普通股篩選 ===
def test_common_stock_filter_keeps_codes_above_9958() -> None:
    """
    9960、9962 這類上櫃普通股要留在股票池

    以前代號上限卡在 9958，它們被靜默排除在回測股票池與券商分點更新之外；
    ETF（00 開頭）與權證（6 碼）本來就擋得掉，上限沒有多擋到任何東西。
    """

    assert StockUtils.filter_common_stocks(
        ["2330", "9960", "9962", "0050", "00878", "030001", "1000"]
    ) == ["2330", "9960", "9962"]


# === 與交易所公告值逐筆對照（`TWT84U` 股價升降幅度，以公告的開盤競價基準為輸入） ===
@pytest.mark.parametrize(
    ("symbol", "basis", "day", "expected"),
    [
        # 浮點誤差：15.5 × 0.9 在浮點數是 13.950000000000001，往上對齊曾變成 14.0
        ("1906", 15.5, datetime.date(2024, 1, 2), (13.95, 17.05)),
        ("2101", 42.0, datetime.date(2024, 1, 2), (37.8, 46.2)),
        ("2211", 104.0, datetime.date(2024, 1, 2), (93.6, 114.0)),
        # ETF 的檔位表比普通股細：0050 套普通股表曾算成 148.5
        ("0050", 135.45, datetime.date(2024, 1, 2), (121.95, 148.95)),
        ("0055", 24.12, datetime.date(2024, 1, 2), (21.71, 26.53)),
        # 槓桿型 ETF 的幅度乘上 2 倍
        ("00631L", 151.2, datetime.date(2024, 1, 2), (121.0, 181.4)),
        ("00631L", 24.48, datetime.date(2016, 10, 3), (19.59, 29.37)),
    ],
    ids=[
        "float-1906",
        "float-2101",
        "float-2211",
        "etf-0050",
        "etf-0055",
        "lev-2024",
        "lev-2016",
    ],
)
def test_price_limits_match_announced_values(
    symbol: str, basis: float, day: datetime.date, expected: Tuple[float, float]
) -> None:
    """每一筆都是交易所當日公告的（跌停價, 漲停價）"""

    assert TwStockSpec().get_price_limits(basis, day, symbol) == expected


def test_leveraged_etf_ratio_is_doubled_but_inverse_is_not() -> None:
    """`L` 結尾（2 倍槓桿）加倍；`R` 結尾（-1 倍反向）與普通股相同"""

    spec: TwStockSpec = TwStockSpec()

    assert spec.get_price_limit_ratio(datetime.date(2024, 1, 2), "00631L") == 0.20
    assert spec.get_price_limit_ratio(datetime.date(2024, 1, 2), "00632R") == 0.10
    assert spec.get_price_limit_ratio(datetime.date(2015, 5, 4), "00631L") == 0.14


def test_tick_table_depends_on_symbol() -> None:
    """ETF 用兩段表、普通股用六段表；不給代號時用普通股表（與舊行為相同）"""

    assert StockUtils.round_to_tick(148.97, "down", "0050") == 148.95
    assert StockUtils.round_to_tick(148.97, "down", "2330") == 148.5
    assert StockUtils.round_to_tick(148.97, "down") == 148.5


def test_slippage_scaling_has_no_float_error() -> None:
    """3 元加 100 bps：3.0 × 1.01 浮點是 3.0300000000000002，往上對齊曾變成 3.04"""

    assert InstrumentSpec.scale_price(3.0, 0.01) == 3.03
    assert TwStockSpec().apply_slippage(3.0, Action.BUY, 100, "2330") == 3.03
    assert TwStockSpec().apply_slippage(3.0, Action.SELL, 100, "2330") == 2.97

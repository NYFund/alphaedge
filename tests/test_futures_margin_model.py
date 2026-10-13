import datetime
import re
from pathlib import Path
from typing import List, Optional

import pytest

from core.market.tw.cost_model import TwFuturesCostModel
from core.market.tw.futures_margin_config import FuturesMarginConfig
from core.market.tw.futures_margin_model import FuturesMarginModel
from core.models import FuturesAccount, FuturesOrder, FuturesPosition, FuturesQuote
from core.models.cost_config import FuturesCostConfig
from core.portfolio.construction import FuturesPortfolioConstructor
from core.portfolio.signal import Signal
from core.portfolio.sizing import MarginSizer
from core.position.futures.position_manager import FuturesPositionManager
from core.utils import Action, PositionType
from tests.conftest import build_futures_quote

"""
期貨保證金只有一份查表：部位管理層與部位建構層共用 `FuturesMarginModel`

原本兩份查表的判準不同：部位管理層依序查金額表、比例表（個股期貨），都查不到才拋錯；
部位建構層只查金額表、查不到就回 0——查表模式下個股期貨的 sizing 永遠 0 口，
部位管理層卻開得進去，而且不會有任何錯誤訊息。
"""

DAY: datetime.date = datetime.date(2024, 3, 1)
STOCK_FUTURES: str = "CDF"
CONTRACT_SIZE: int = 2000
UNDERLYING: float = 100.0
INITIAL_RATE: float = 0.135


class StubTwoTableAPI:
    """金額表只有 TX；股期走比例表"""

    def __init__(self, tx_per_lot: Optional[int] = 700000) -> None:
        self.tx_per_lot = tx_per_lot

    def get_initial_margin(self, product, date, fallback_to_earliest=False):
        return self.tx_per_lot if product == "TX" else None

    def get_maintenance_margin(self, product, date, fallback_to_earliest=False):
        return (
            int(self.tx_per_lot * 0.75) if product == "TX" and self.tx_per_lot else None
        )

    def calculate_stock_futures_margin(
        self, product, date, price, fallback_to_earliest=False
    ):
        return (
            price * CONTRACT_SIZE * INITIAL_RATE if product == STOCK_FUTURES else None
        )

    def calculate_stock_futures_maintenance_margin(
        self, product, date, price, fallback_to_earliest=False
    ):
        return price * CONTRACT_SIZE * 0.1 if product == STOCK_FUTURES else None

    def get_covered_date_range(self, product):
        return {"earliest": "2020-03-13", "latest": "2026-08-12"}


def stock_futures_quote() -> FuturesQuote:
    return build_futures_quote(
        product=STOCK_FUTURES,
        expiry="202403",
        date=DAY,
        close=UNDERLYING,
        multiplier=CONTRACT_SIZE,
    )


def test_ratio_mode_is_contract_value_times_ratio() -> None:
    """沒有 API：契約價值 × 比率，商品與日期可以不給"""

    model: FuturesMarginModel = FuturesMarginModel(FuturesMarginConfig.ratio(0.1))

    assert model.initial_margin_per_lot(None, None, 18000.0, 200) == 18000.0 * 200 * 0.1
    assert model.maintenance_margin_per_lot("TX", DAY) is None


def test_table_mode_requires_product_and_date() -> None:
    """查表模式沒給商品或日期就拋錯：保證金隨商品與日期變動，猜不得"""

    model: FuturesMarginModel = FuturesMarginModel(
        FuturesMarginConfig(api=StubTwoTableAPI())
    )

    with pytest.raises(ValueError, match="product 與 date"):
        model.initial_margin_per_lot(None, DAY, 18000.0, 200)


def test_stock_futures_fall_back_to_the_rate_table() -> None:
    """金額表查不到的股期走比例表：標的股價 × 契約單位 × 適用比例"""

    model: FuturesMarginModel = FuturesMarginModel(
        FuturesMarginConfig(api=StubTwoTableAPI()),
        underlying_price_resolver=lambda product, date: UNDERLYING,
    )

    assert model.initial_margin_per_lot(
        STOCK_FUTURES, DAY, UNDERLYING, CONTRACT_SIZE
    ) == (UNDERLYING * CONTRACT_SIZE * INITIAL_RATE)
    assert (
        model.maintenance_margin_per_lot(STOCK_FUTURES, DAY)
        == UNDERLYING * CONTRACT_SIZE * 0.1
    )


def test_unknown_margin_raises_instead_of_guessing() -> None:
    """兩張表都查不到就拋錯；沒有 resolver 時比例表那條路也走不通"""

    model: FuturesMarginModel = FuturesMarginModel(
        FuturesMarginConfig(api=StubTwoTableAPI(tx_per_lot=None))
    )

    with pytest.raises(ValueError, match="查無"):
        model.initial_margin_per_lot("TX", DAY, 18000.0, 200)
    with pytest.raises(ValueError, match="查無"):
        model.initial_margin_per_lot(STOCK_FUTURES, DAY, UNDERLYING, CONTRACT_SIZE)


def test_sizing_and_booking_use_the_same_number() -> None:
    """
    股期在查表模式下：部位建構層算得出口數，部位管理層也以同一個保證金入帳

    舊版建構層只查金額表、回 0 口，策略從頭到尾開不了股期；部位管理層卻查得到比例表。
    """

    model: FuturesMarginModel = FuturesMarginModel(
        FuturesMarginConfig(api=StubTwoTableAPI()),
        underlying_price_resolver=lambda product, date: UNDERLYING,
    )
    account: FuturesAccount = FuturesAccount(init_capital=1_000_000)
    manager: FuturesPositionManager = FuturesPositionManager(
        account,
        cost_model=TwFuturesCostModel(FuturesCostConfig.free()),
        multiplier_resolver=lambda product, date: CONTRACT_SIZE,
        margin_model=model,
    )
    constructor: FuturesPortfolioConstructor = FuturesPortfolioConstructor(
        MarginSizer(max_lots=100, max_capital_usage=1.0),
        margin_model=model,
        cost_model=TwFuturesCostModel(FuturesCostConfig.free()),
    )
    quote: FuturesQuote = stock_futures_quote()
    per_lot: float = UNDERLYING * CONTRACT_SIZE * INITIAL_RATE

    orders: List[FuturesOrder] = constructor.build(
        [Signal(quote, Action.BUY, PositionType.LONG, order_price=quote.close)], account
    )

    assert orders[0].volume == int(1_000_000 // per_lot) > 0
    position = manager.open_position(orders[0])
    assert position is not None
    assert position.margin == per_lot * orders[0].volume
    assert manager.margin_model is model


def test_open_lots_count_against_max_lots() -> None:
    """`max_lots` 是帳上總口數的上限：已開的口數要扣掉，不是每次都能再開滿"""

    account: FuturesAccount = FuturesAccount(init_capital=100_000_000)
    account.positions.append(
        FuturesPosition(
            product="TX", expiry="202403", position_type=PositionType.LONG, volume=3
        )
    )
    quote: FuturesQuote = build_futures_quote(expiry="202406", date=DAY)

    sized = MarginSizer(max_lots=5, max_capital_usage=1.0).size(
        account, [(quote, 100.0)], None
    )

    assert [lots for _, _, lots in sized] == [2]


def test_opening_requirement_includes_fees() -> None:
    """每口需要的資金 ＝ 保證金 ＋ 手續費 ＋ 期交稅，與開倉資金檢查同口徑"""

    cost_model: TwFuturesCostModel = TwFuturesCostModel()
    constructor: FuturesPortfolioConstructor = FuturesPortfolioConstructor(
        MarginSizer(max_lots=10),
        margin_model=FuturesMarginModel(FuturesMarginConfig.ratio(0.1)),
        cost_model=cost_model,
    )
    quote: FuturesQuote = build_futures_quote(expiry="202403", date=DAY, close=18000.0)

    requirement: float = constructor.opening_requirement_per_lot(quote)

    assert requirement == 18000.0 * 200 * 0.1 + cost_model.commission(
        volume=1, product="TX"
    ) + cost_model.tax(price=18000.0, volume=1, multiplier=200)


CORE_DIR: Path = Path(__file__).resolve().parents[1] / "core"
MODEL: Path = CORE_DIR / "market" / "tw" / "futures_margin_model.py"
API: Path = CORE_DIR / "api" / "tw" / "futures_margin_api.py"
DIRECT_LOOKUP: re.Pattern = re.compile(
    r"\.(get_initial_margin|get_maintenance_margin|calculate_stock_futures_margin"
    r"|calculate_stock_futures_maintenance_margin)\("
)


def test_margin_tables_are_only_read_through_the_model() -> None:
    """`core/` 其他地方不可再直接查保證金表：兩份查表的判準就是這樣分岔的"""

    offenders: List[str] = []
    for path in sorted(CORE_DIR.rglob("*.py")):
        if path in (MODEL, API):
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            if DIRECT_LOOKUP.search(line):
                offenders.append(f"{path.relative_to(CORE_DIR)}:{lineno}")

    assert offenders == [], f"直接查保證金表：{offenders}"

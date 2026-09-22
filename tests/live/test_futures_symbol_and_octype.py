import queue
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from core.broker.rate_limiter import RateLimiter
from core.broker.tw.shioaji_account_query import ShioajiAccountQuery
from core.broker.tw.shioaji_contract_resolver import (
    ShioajiContractResolver,
    to_futures_product,
)
from core.broker.tw.shioaji_execution_handler import ShioajiExecutionHandler
from core.broker.tw.shioaji_order_mapper import ShioajiOrderMapper
from core.broker.tw.shioaji_quote_stream import ShioajiQuoteStream
from core.models import ExecutionReport, FuturesOrder, OrderStatusEvent
from core.models.futures.execution import FuturesPositionSnapshot
from core.utils import Action, FuturesOCType, OrderState, PositionType

from .test_shioaji_account_query import FakeApi as AccountApi
from .test_shioaji_account_query import FakeFuturesPosition

"""
期貨代號與開平倉別

**代號**：券商的期貨成交回報、委託回報與部位查詢都以月份字母碼（`TXFJ6`）表示契約，
快照則拆成分類碼（`TXF`）加月份；專案的 `FuturesOrder.symbol` 是 `{商品}{YYYYMM}`
（`TX202610`）。四條路徑不統一的話，策略送出 `TX202610`、帳上卻是 `TXFJ6`：
帳戶同步拆不出月份、乘數查不到，平倉單也對不到部位（2026-09-22 模擬環境實測）。
字母碼跨年重複，月份一律取合約的 `delivery_month`。

**開平倉別**：以前一律送 `New`，平倉單會開出一口反向新倉而不是平掉原部位。
"""


class FakeFutures:
    """`api.Contracts.Futures`：以代碼查合約，並記錄查詢次數"""

    def __init__(self, contracts: Dict[str, Any]) -> None:
        self.contracts: Dict[str, Any] = contracts
        self.lookups: List[str] = []

    def get(self, code: str) -> Optional[Any]:
        self.lookups.append(code)
        return self.contracts.get(code)


def futures_contract(code: str, root: str, delivery_month: str) -> Any:
    return SimpleNamespace(code=code, root=root, delivery_month=delivery_month)


def make_resolver() -> ShioajiContractResolver:
    futures: FakeFutures = FakeFutures(
        {
            "TXFJ6": futures_contract("TXFJ6", "TXF", "202610"),
            "MXFJ6": futures_contract("MXFJ6", "MXF", "202610"),
            "CDFJ6": futures_contract("CDFJ6", "CDF", "202610"),
        }
    )
    api: Any = SimpleNamespace(Contracts=SimpleNamespace(Futures=futures))
    return ShioajiContractResolver(api)


# === 代號轉換 ===
@pytest.mark.parametrize(
    ("category", "product"),
    [("TXF", "TX"), ("MXF", "MTX"), ("EXF", "TE"), ("CDF", "CDF")],
)
def test_category_maps_to_project_product(category: str, product: str) -> None:
    """兩邊的代碼沒有規律（MXF→MTX、EXF→TE）；未登錄的分類（股期）原樣回傳"""

    assert to_futures_product(category) == product


def test_month_letter_code_becomes_product_and_delivery_month() -> None:
    resolver: ShioajiContractResolver = make_resolver()

    assert resolver.to_futures_symbol("TXFJ6") == "TX202610"
    assert resolver.to_futures_symbol("MXFJ6") == "MTX202610"


def test_unknown_code_is_kept_as_is() -> None:
    """查不到合約時原樣回傳：回報仍要入列，代號對不上由對帳抓出"""

    assert make_resolver().to_futures_symbol("NOPE1") == "NOPE1"


def test_conversion_is_cached() -> None:
    """合約屬性在一個交易日內不變；每筆回報都查一次合約檔沒有必要"""

    resolver: ShioajiContractResolver = make_resolver()
    resolver.to_futures_symbol("TXFJ6")
    resolver.to_futures_symbol("TXFJ6")

    assert resolver.api.Contracts.Futures.lookups == ["TXFJ6"]


def make_handler() -> ShioajiExecutionHandler:
    return ShioajiExecutionHandler(
        queue.Queue(), futures_symbol=make_resolver().to_futures_symbol
    )


def test_futures_deal_report_uses_the_project_symbol() -> None:
    """期貨成交回報的代碼換成 `TX202610`；欄位取自 2026-09-22 模擬環境實際回報"""

    report: Any = make_handler().parse(
        OrderState.FuturesDeal,
        {
            "trade_id": "00BE3D",
            "seqno": "00BE3D",
            "action": "Buy",
            "code": "TXFJ6",
            "price": 48271.0,
            "quantity": 1,
            "ts": 1790050319.169799,
        },
    )

    assert isinstance(report, ExecutionReport)
    assert report.symbol == "TX202610"


def test_futures_order_event_uses_the_project_symbol() -> None:
    event: Any = make_handler().parse(
        OrderState.FuturesOrder,
        {
            "operation": {"op_type": "New", "op_code": "00"},
            "order": {"seqno": "009C53"},
            "status": {"id": "009C53"},
            "contract": {"code": "TXFJ6", "security_type": "FUT"},
        },
    )

    assert isinstance(event, OrderStatusEvent)
    assert event.symbol == "TX202610"


def test_stock_deal_report_is_not_converted() -> None:
    """股票代碼本來就一致，不經過期貨的轉換"""

    report: Any = make_handler().parse(
        OrderState.StockDeal,
        {
            "trade_id": "00B7ED",
            "seqno": "00B7ED",
            "action": "Buy",
            "code": "2330",
            "price": 2480.0,
            "quantity": 1,
            "ts": 1790049639.686552,
        },
    )

    assert report.symbol == "2330"


def test_futures_positions_use_the_project_symbol() -> None:
    """部位查詢的代碼也要換：對帳比的是它與本地歸屬帳"""

    api: AccountApi = AccountApi(
        futures_positions=[FakeFuturesPosition(code="TXFJ6", direction="Buy")]
    )
    query: ShioajiAccountQuery = ShioajiAccountQuery(
        api, RateLimiter(), futures_symbol=make_resolver().to_futures_symbol
    )

    position: FuturesPositionSnapshot = query.get_futures_positions()[0]

    assert (position.symbol, position.product, position.expiry) == (
        "TX202610",
        "TX",
        "202610",
    )


def test_futures_snapshot_uses_the_project_product() -> None:
    """快照的商品要換成專案代碼，否則策略拿到 `TXF202610`、送出 `TX202610`"""

    contract: Any = futures_contract("TXFJ6", "TXF", "202610")

    assert ShioajiQuoteStream._split_code("TXFJ6", contract) == ("TX", "202610")


# === 開平倉別 ===
@pytest.mark.parametrize(
    ("position_type", "action", "expected"),
    [
        (PositionType.LONG, Action.BUY, FuturesOCType.New),
        (PositionType.LONG, Action.SELL, FuturesOCType.Cover),
        (PositionType.SHORT, Action.SELL, FuturesOCType.New),
        (PositionType.SHORT, Action.BUY, FuturesOCType.Cover),
    ],
)
def test_octype_follows_direction_and_action(
    position_type: PositionType, action: Action, expected: FuturesOCType
) -> None:
    """
    多單買／空單賣是開倉，多單賣／空單買是平倉

    平倉單送成 `New` 會開出一口反向新倉：帳上同時掛著多空兩邊、保證金照收兩份。
    """

    order: FuturesOrder = FuturesOrder(
        product="TX",
        expiry="202610",
        action=action,
        position_type=position_type,
        volume=1,
        price=48000.0,
    )

    assert ShioajiOrderMapper.derive_octype(order) is expected

from typing import Any, Optional

import pytest
import shioaji.constant as sj_constant

from core.broker.tw.shioaji_order_mapper import ShioajiOrderMapper
from core.models import FuturesOrder, StockOrder
from core.utils import (
    Action,
    FuturesOCType,
    FuturesPriceType,
    OrderType,
    PositionType,
    ShortMethod,
    StockOrderCond,
    StockOrderLot,
    StockPriceType,
)

"""
委託轉換：不合法的組合要在本地擋下，不要送到券商才被拒

券商的拒單訊息通常只有一個代碼，而尾盤段只有 13:25~13:29 可以送單——
在那裡才發現參數錯了，等於當天這張單沒了。

本檔不連網：`shioaji.order.StockOrder`／`FuturesOrder` 是可直接建構的 pydantic 模型，
欄位限制（`custom_field` 最多 6 個可列印 ASCII 字元）由它自己驗，
我們驗的是**本專案這一側的推導與檢查**。
"""


@pytest.fixture
def mapper() -> ShioajiOrderMapper:
    return ShioajiOrderMapper()


def make_stock_order(
    action: Action = Action.BUY,
    position_type: PositionType = PositionType.LONG,
    short_method: Optional[ShortMethod] = None,
    price: float = 1000.0,
    volume: int = 2,
    price_type: Optional[StockPriceType] = StockPriceType.LMT,
    order_lot: StockOrderLot = StockOrderLot.Common,
) -> StockOrder:
    return StockOrder(
        stock_id="2330",
        action=action,
        position_type=position_type,
        short_method=short_method,
        price=price,
        volume=volume,
        price_type=price_type,
        order_lot=order_lot,
    )


# === 委託條件推導矩陣 ===
@pytest.mark.parametrize(
    "position_type, short_method, action, expected_cond, expected_daytrade",
    [
        (PositionType.LONG, None, Action.BUY, StockOrderCond.Cash, False),
        (PositionType.LONG, None, Action.SELL, StockOrderCond.Cash, False),
        # 現股當沖：先賣（開倉）才是當沖賣出，回補那一腿是普通買單
        (
            PositionType.SHORT,
            ShortMethod.DAY_TRADE,
            Action.SELL,
            StockOrderCond.Cash,
            True,
        ),
        (
            PositionType.SHORT,
            ShortMethod.DAY_TRADE,
            Action.BUY,
            StockOrderCond.Cash,
            False,
        ),
        (
            PositionType.SHORT,
            ShortMethod.MARGIN,
            Action.SELL,
            StockOrderCond.ShortSelling,
            False,
        ),
        (
            PositionType.SHORT,
            ShortMethod.MARGIN,
            Action.BUY,
            StockOrderCond.ShortSelling,
            False,
        ),
        (
            PositionType.SHORT,
            ShortMethod.SBL,
            Action.SELL,
            StockOrderCond.SBLShort,
            False,
        ),
        (
            PositionType.SHORT,
            ShortMethod.SBL,
            Action.BUY,
            StockOrderCond.SBLShort,
            False,
        ),
    ],
)
def test_order_cond_matrix(
    position_type: PositionType,
    short_method: Optional[ShortMethod],
    action: Action,
    expected_cond: StockOrderCond,
    expected_daytrade: bool,
) -> None:
    """
    委託條件推導的完整矩陣

    推導錯的後果不是報錯，是**用另一種條件成交**：融券單掉成現股就會因為沒有庫存
    而退單，當沖的先賣少了 `daytrade_short` 也一樣。
    """

    cond, daytrade_short = ShioajiOrderMapper.resolve_stock_order_cond(
        position_type, short_method, action
    )

    assert cond is expected_cond
    assert daytrade_short is expected_daytrade


def test_short_without_method_raises() -> None:
    """
    SHORT 沒有放空管道要拋出

    它由委託前處理依策略設定補值。沒補到而預設成現股的話，
    那張賣單會因為帳上沒有庫存被退回，而原因完全看不出來。
    """

    with pytest.raises(ValueError, match="short_method"):
        ShioajiOrderMapper.resolve_stock_order_cond(
            PositionType.SHORT, None, Action.SELL
        )


def test_sbl_raises_and_does_not_fall_back(mapper: ShioajiOrderMapper) -> None:
    """
    借券在鎖定版 shioaji 送不出去時要拋出，**不可退回融券**

    退回 `ShortSelling` 會送出一張用融券券源與成本成交的單，而回測算的是議定費率——
    兩邊的成本從此對不上，且不會有任何錯誤訊息。
    """

    order: StockOrder = make_stock_order(
        action=Action.SELL,
        position_type=PositionType.SHORT,
        short_method=ShortMethod.SBL,
    )

    with pytest.raises(ValueError) as error:
        mapper.to_shioaji_stock_order(order)

    message: str = str(error.value)
    assert "SBLShort" in message
    assert "升級 shioaji" in message


# === 價格 ===
@pytest.mark.parametrize(
    "action, price, expected",
    [
        # 1000 以上的檔位是 5 元：買單往下、賣單往上
        (Action.BUY, 1000.3, 1000.0),
        (Action.SELL, 1000.3, 1005.0),
        # 100~500 的檔位是 0.5 元
        (Action.BUY, 123.4, 123.0),
        (Action.SELL, 123.4, 123.5),
        # 已在檔位上的價格不動
        (Action.BUY, 123.5, 123.5),
        (Action.SELL, 123.5, 123.5),
    ],
)
def test_price_alignment_is_conservative(
    action: Action, price: float, expected: float
) -> None:
    """
    對齊方向一律保守：買單往下、賣單往上

    往不利的方向取整等於每張單自己讓一個檔位，而這件事不會被任何地方記錄下來——
    它會混進滑價統計裡，看起來像市場造成的。
    """

    assert ShioajiOrderMapper.align_price(price, action) == pytest.approx(expected)


def test_aligned_price_reaches_the_broker_order(mapper: ShioajiOrderMapper) -> None:
    """對齊後的價格要真的送出去，不是算完就丟掉"""

    converted: Any = mapper.to_shioaji_stock_order(
        make_stock_order(action=Action.BUY, price=1000.3)
    )

    assert converted.price == pytest.approx(1000.0)


def test_market_order_sends_zero_price(mapper: ShioajiOrderMapper) -> None:
    """
    市價單的價格送 0

    Shioaji 的市價單不看價格欄位。送進原本的參考價雖然多半也會被忽略，
    但那會讓事後看委託紀錄的人以為那是一張限價單。
    """

    converted: Any = mapper.to_shioaji_stock_order(
        make_stock_order(price=1234.0, price_type=StockPriceType.MKT)
    )

    assert converted.price == 0.0
    assert converted.price_type is sj_constant.StockPriceType.MKT


def test_missing_price_type_raises(mapper: ShioajiOrderMapper) -> None:
    """
    價格類型未決定要拋出，**不可預設成限價**

    策略要的是市價而前處理漏了填值時，預設成限價會送出一張價格正確但語意不同的單，
    成交與否完全看運氣，而且兩邊都不會報錯。
    """

    with pytest.raises(ValueError, match="price_type"):
        mapper.to_shioaji_stock_order(make_stock_order(price_type=None))


@pytest.mark.parametrize("price, expected_error", [(1100.0, "漲停"), (900.0, "跌停")])
def test_price_outside_limits_is_rejected(
    mapper: ShioajiOrderMapper, price: float, expected_error: str
) -> None:
    """
    超出漲跌停的委託在本地就擋掉

    用合約檔的公告值而不是自行推算：除權息日的基準價是另行公告的，
    公式推出來的區間會整段偏移。
    """

    contract: Any = type("C", (), {"limit_up": 1050.0, "limit_down": 950.0})()

    with pytest.raises(ValueError, match=expected_error):
        mapper.to_shioaji_stock_order(make_stock_order(price=price), contract=contract)


def test_contract_without_limit_fields_is_not_blocked(
    mapper: ShioajiOrderMapper,
) -> None:
    """合約沒帶漲跌停欄位時略過檢查，不阻擋——那是輔助檢查，不是硬條件"""

    contract: Any = type("C", (), {})()
    converted: Any = mapper.to_shioaji_stock_order(
        make_stock_order(price=1100.0), contract=contract
    )

    assert converted.price == pytest.approx(1100.0)


# === 數量單位 ===
def test_common_lot_quantity_is_in_lots(mapper: ShioajiOrderMapper) -> None:
    """整股的數量單位是張，不換算成股"""

    converted: Any = mapper.to_shioaji_stock_order(make_stock_order(volume=3))

    assert converted.quantity == 3
    assert converted.order_lot is sj_constant.StockOrderLot.Common


def test_intraday_odd_rejects_lot_sized_quantity(mapper: ShioajiOrderMapper) -> None:
    """
    盤中零股的數量單位是股，超過 999 就是單位錯配

    錯配不會報錯，只會下成 1000 倍或千分之一的量——而 1000 倍那個方向
    會直接吃掉整個帳戶。
    """

    order: StockOrder = make_stock_order(volume=3, order_lot=StockOrderLot.IntradayOdd)
    order.volume = 3000

    with pytest.raises(ValueError, match="盤中零股"):
        mapper.to_shioaji_stock_order(order)


def test_intraday_odd_accepts_share_sized_quantity(mapper: ShioajiOrderMapper) -> None:
    """零股範圍內的股數可以送出"""

    converted: Any = mapper.to_shioaji_stock_order(
        make_stock_order(volume=500, order_lot=StockOrderLot.IntradayOdd)
    )

    assert converted.quantity == 500


@pytest.mark.parametrize("volume", [0, -1])
def test_non_positive_quantity_is_rejected(
    mapper: ShioajiOrderMapper, volume: int
) -> None:
    """數量必須是正整數"""

    with pytest.raises(ValueError, match="正整數"):
        mapper.to_shioaji_stock_order(make_stock_order(volume=volume))


# === custom_field ===
def test_custom_field_passes_through(mapper: ShioajiOrderMapper) -> None:
    """6 個可列印 ASCII 字元以內原樣送出"""

    converted: Any = mapper.to_shioaji_stock_order(
        make_stock_order(), custom_field="a1B2c3"
    )

    assert converted.custom_field == "a1B2c3"


def test_custom_field_too_long_is_rejected_locally(mapper: ShioajiOrderMapper) -> None:
    """
    長度在本地就擋，不交給 shioaji 的驗證

    它的 `ValidationError` 訊息是「String should have at most 6 characters」，
    看不出這個欄位是什麼、也看不出該填什麼。
    """

    with pytest.raises(ValueError, match="custom_field"):
        ShioajiOrderMapper.validate_custom_field("toolongfield")


def test_custom_field_non_ascii_is_rejected(mapper: ShioajiOrderMapper) -> None:
    """非 ASCII 字元同樣在本地擋下"""

    with pytest.raises(ValueError, match="ASCII"):
        ShioajiOrderMapper.validate_custom_field("中文字")


def test_custom_field_none_becomes_empty(mapper: ShioajiOrderMapper) -> None:
    """未提供時送空字串；`None` 會讓 shioaji 的欄位驗證失敗"""

    assert ShioajiOrderMapper.validate_custom_field(None) == ""


# === Enum 轉換 ===
def test_action_converts_by_value_not_by_name(mapper: ShioajiOrderMapper) -> None:
    """
    `Action` 依**值**轉換

    本專案的成員名是 `BUY`／`SELL`（另有回測用的 `OPEN`／`CLOSE`），
    Shioaji 是 `Buy`／`Sell`。依名稱查會對每一張單都拋出，而送上線路的本來就是值。
    """

    assert Action.BUY.name != sj_constant.Action.Buy.name
    assert Action.BUY.value == sj_constant.Action.Buy.value

    converted: Any = mapper.to_shioaji_stock_order(make_stock_order(action=Action.BUY))
    assert converted.action is sj_constant.Action.Buy


def test_order_type_is_carried_over(mapper: ShioajiOrderMapper) -> None:
    """委託效期要原樣帶過去；回測沒有留單概念，這個欄位只在實盤有意義"""

    order: StockOrder = make_stock_order()
    order.order_type = OrderType.IOC

    assert mapper.to_shioaji_stock_order(order).order_type is sj_constant.OrderType.IOC


# === 期貨 ===
def test_futures_order_conversion() -> None:
    """期貨委託的基本轉換"""

    mapper: ShioajiOrderMapper = ShioajiOrderMapper()
    order: FuturesOrder = FuturesOrder(
        product="TX",
        expiry="202601",
        action=Action.SELL,
        volume=2,
        price=20000.0,
        price_type=FuturesPriceType.LMT,
    )

    converted: Any = mapper.to_shioaji_futures_order(order, octype=FuturesOCType.Cover)

    assert converted.action is sj_constant.Action.Sell
    assert converted.quantity == 2
    assert converted.octype is sj_constant.FuturesOCType.Cover


def test_futures_auto_octype_is_rejected() -> None:
    """
    `Auto` 一律拒絕

    它在同時有多空部位或換月時的行為不透明，而換月正是「平舊月＋開新月」
    兩張單同時在場上的時候——猜錯的後果是曝險翻倍。
    """

    mapper: ShioajiOrderMapper = ShioajiOrderMapper()
    order: FuturesOrder = FuturesOrder(
        product="TX", expiry="202601", volume=1, price_type=FuturesPriceType.LMT
    )

    with pytest.raises(ValueError, match="Auto"):
        mapper.to_shioaji_futures_order(order, octype=FuturesOCType.Auto)


def test_futures_range_market_price_type() -> None:
    """期貨多一個 `MKP`（範圍市價），價格同樣送 0"""

    mapper: ShioajiOrderMapper = ShioajiOrderMapper()
    order: FuturesOrder = FuturesOrder(
        product="TX",
        expiry="202601",
        volume=1,
        price=20000.0,
        price_type=FuturesPriceType.MKP,
    )

    converted: Any = mapper.to_shioaji_futures_order(order, octype=FuturesOCType.New)

    assert converted.price_type is sj_constant.FuturesPriceType.MKP
    assert converted.price == 0.0


# === 帳號 ===
def test_account_is_omitted_when_absent(mapper: ShioajiOrderMapper) -> None:
    """
    帳號為 None 時整個欄位不帶

    shioaji 的 `account` 不接受 None，明確傳 None 的錯誤訊息是
    「Input should be a valid dictionary or instance of Account」——
    看不出真正的原因是「還沒登入」。
    """

    converted: Any = mapper.to_shioaji_stock_order(make_stock_order())

    assert converted.account is None  # 模型的預設值，不是我們傳進去的 None

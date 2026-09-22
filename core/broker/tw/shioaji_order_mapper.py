import inspect
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

import shioaji as sj

from core.models import FuturesOrder, StockOrder
from core.utils import (
    Action,
    FuturesOCType,
    FuturesPriceType,
    PositionType,
    ShortMethod,
    StockOrderCond,
    StockOrderLot,
    StockPriceType,
)
from core.utils.instrument import StockUtils

"""
委託轉換：本專案的領域訂單 → Shioaji 的 `StockOrder`／`FuturesOrder`

**不合法的組合在本地擋下，不要送到券商才被拒。** 券商的拒單訊息通常只有一個代碼，
而尾盤段只有 13:25~13:29 可以送單——在那裡才發現參數錯了，等於當天這張單沒了。

三件事只在這一層發生，別處不要重做：
1. **委託條件推導**：`position_type` ＋ `short_method` ＋ `action` → `order_cond`
   ＋ `daytrade_short`。策略不填、也填不了，這樣回測的成本路徑與實盤送出的委託條件
   才保證同源。
2. **價格對齊檔位**：買單往下、賣單往上，**方向一律保守**——往不利的方向對齊
   等於自己讓價，而且不會有任何地方記錄這件事。
3. **數量單位檢查**：整股的 `quantity` 是張、盤中零股是股。錯配不會報錯，
   只會下成 1000 倍或千分之一的量。
"""


class ShioajiOrderMapper:
    """領域訂單與 Shioaji 訂單之間的轉換器"""

    # `custom_field` 的券商限制：最多 6 個可列印 ASCII 字元。
    # shioaji 1.7 起建構委託物件時已不再檢查（7 個字元、中文都建得出來），
    # 本地這道 `validate_custom_field()` 是送出前唯一的防線
    CUSTOM_FIELD_MAX_LENGTH: int = 6

    # 盤中零股的數量上限（股）；達到 1000 股就該用整股下單
    INTRADAY_ODD_MAX_SHARES: int = 999

    def __init__(
        self, stock_account: Optional[Any] = None, futopt_account: Optional[Any] = None
    ) -> None:
        """
        - Description:
            建立轉換器
        - Parameters:
            - stock_account: Optional[Any]
                Shioaji 的股票帳號物件（`api.stock_account`）
            - futopt_account: Optional[Any]
                Shioaji 的期貨帳號物件（`api.futopt_account`）
        """

        self.stock_account: Optional[Any] = stock_account
        self.futopt_account: Optional[Any] = futopt_account

    # === 委託條件推導 ===
    @staticmethod
    def resolve_stock_order_cond(
        position_type: PositionType,
        short_method: Optional[ShortMethod],
        action: Action,
    ) -> Tuple[StockOrderCond, bool]:
        """
        - Description:
            推導股票的委託條件與現股當沖旗標

            | position_type | short_method | action | order_cond | daytrade_short |
            |---------------|--------------|--------|------------|:--------------:|
            | LONG | — | BUY／SELL | Cash | False |
            | SHORT | DAY_TRADE | SELL（開） | Cash | True |
            | SHORT | DAY_TRADE | BUY（平） | Cash | False |
            | SHORT | MARGIN | SELL／BUY | ShortSelling | False |
            | SHORT | SBL | SELL／BUY | SBLShort | False |

            **現股當沖的先賣要兩個欄位併用**：`order_cond=Cash` ＋ `daytrade_short=True`。
            少了後者，那張賣單會被當成賣出持股而退單——帳上根本沒有庫存。
            而回補那一腿是普通買進，`daytrade_short` 必須是 False。
        - Parameters:
            - position_type: PositionType
                持倉方向
            - short_method: Optional[ShortMethod]
                放空管道；LONG 時可為 None
            - action: Action
                買賣別
        - Return:
            - Tuple[StockOrderCond, bool]
                委託條件與現股當沖旗標
        - Raise:
            - ValueError
                SHORT 卻沒有放空管道，或管道不在支援範圍內
        """

        if position_type is PositionType.LONG:
            return (StockOrderCond.Cash, False)

        if short_method is None:
            raise ValueError(
                "SHORT 訂單沒有 short_method，無法決定委託條件；"
                "它應由委託前處理依策略設定補值，不是由策略填寫"
            )

        if short_method is ShortMethod.DAY_TRADE:
            # 先賣（開倉）才是當沖賣出；買進（回補）是普通買單
            return (StockOrderCond.Cash, action is Action.SELL)
        if short_method is ShortMethod.MARGIN:
            return (StockOrderCond.ShortSelling, False)
        if short_method is ShortMethod.SBL:
            return (StockOrderCond.SBLShort, False)

        raise ValueError(f"不支援的放空管道：{short_method}")

    # === 價格 ===
    @staticmethod
    def align_price(price: float, action: Action) -> float:
        """
        - Description:
            把價格對齊台股分段檔位，**方向一律保守**

            買單往下對齊、賣單往上對齊：往有利於自己的方向取整，寧可不成交也不讓價。
            反過來取整等於每張單自己讓一個檔位，而這件事不會被任何地方記錄下來。

            實作委派給 `StockUtils.round_to_tick()`，與回測共用同一份檔位表——
            這裡另抄一份的話，實盤送出的價格會和回測算的價格慢慢分岔。
        - Parameters:
            - price: float
                原始價格
            - action: Action
                買賣別
        - Return:
            - float
                對齊後的價格
        """

        direction: str = "down" if action is Action.BUY else "up"
        return StockUtils.round_to_tick(price, direction)

    # === 轉換 ===
    def to_shioaji_stock_order(
        self,
        order: StockOrder,
        custom_field: Optional[str] = None,
        contract: Optional[Any] = None,
    ) -> sj.StockOrder:
        """
        - Description:
            把 `StockOrder` 轉成 Shioaji 的股票委託
        - Parameters:
            - order: StockOrder
                本專案的股票訂單（`price_type` 必須已決定）
            - custom_field: Optional[str]
                隨委託往返券商的識別碼（client order id 的壓縮碼）
            - contract: Optional[Any]
                對應的 Shioaji 合約；提供時會用它的漲跌停價檢查委託價
        - Return:
            - sj.StockOrder
                Shioaji 委託物件
        - Raise:
            - ValueError
                價格類型未決定、數量單位錯配、委託價超出漲跌停，或委託條件不支援
        """

        price_type: StockPriceType = self._require_price_type(order.price_type, order)
        order_cond, daytrade_short = self.resolve_stock_order_cond(
            order.position_type, order.short_method, order.action
        )
        self._validate_stock_quantity(order)

        price: float = self._resolve_price(price_type, order)
        if price > 0:
            price = self.align_price(price, order.action)
            self._validate_price_limits(price, contract)

        fields: Dict[str, Any] = {
            "action": self._to_broker_enum(order.action, sj.Action),
            "price": price,
            "quantity": order.volume,
            "price_type": self._to_broker_enum(price_type, sj.StockPriceType),
            "order_type": self._to_broker_enum(order.order_type, sj.OrderType),
            "order_lot": self._to_broker_enum(order.order_lot, sj.StockOrderLot),
            "order_cond": self._to_broker_enum(order_cond, sj.StockOrderCond),
            "daytrade_short": daytrade_short,
            "custom_field": self.validate_custom_field(custom_field),
        }
        return sj.StockOrder(**self._with_account(fields, self.stock_account))

    @staticmethod
    def derive_octype(order: FuturesOrder) -> FuturesOCType:
        """
        - Description:
            由訂單的持倉方向與買賣別推導開平倉別

            多單買進、空單賣出是開倉（`New`）；多單賣出、空單買進是平倉（`Cover`）。
            **平倉單送成 `New` 不是小事**：它會開一口反向新倉而不是平掉原部位，
            帳上同時掛著多空兩邊、保證金照收兩份。

            **不推導 `DayTrade`**：當沖的保證金減收要先以期貨商公告核對規則，
            在那之前當日開當日平也送 `Cover`——結果正確，只是少了減收。
        - Parameters:
            - order: FuturesOrder
                本專案的期貨訂單
        - Return:
            - FuturesOCType
                `New` 或 `Cover`
        """

        opening_action: Action = (
            Action.BUY if order.position_type is PositionType.LONG else Action.SELL
        )
        return (
            FuturesOCType.New if order.action is opening_action else FuturesOCType.Cover
        )

    def to_shioaji_futures_order(
        self,
        order: FuturesOrder,
        octype: FuturesOCType,
        custom_field: Optional[str] = None,
    ) -> sj.FuturesOrder:
        """
        - Description:
            把 `FuturesOrder` 轉成 Shioaji 的期貨委託
        - Parameters:
            - order: FuturesOrder
                本專案的期貨訂單（`price_type` 必須已決定）
            - octype: FuturesOCType
                開平倉別；**不可傳 `Auto`**
            - custom_field: Optional[str]
                隨委託往返券商的識別碼
        - Return:
            - sj.FuturesOrder
                Shioaji 委託物件
        - Raise:
            - ValueError
                價格類型未決定、數量非正整數，或 `octype` 為 `Auto`
        """

        price_type: FuturesPriceType = self._require_price_type(order.price_type, order)
        if octype is FuturesOCType.Auto:
            raise ValueError(
                "octype 不可為 Auto：它在同時有多空部位或換月時的行為不透明，"
                "而換月正是「平舊月＋開新月」兩張單同時在場上的時候"
            )
        if order.volume <= 0:
            raise ValueError(f"期貨委託口數必須為正整數，收到 {order.volume}")

        fields: Dict[str, Any] = {
            "action": self._to_broker_enum(order.action, sj.Action),
            "price": self._resolve_price(price_type, order),
            "quantity": order.volume,
            "price_type": self._to_broker_enum(price_type, sj.FuturesPriceType),
            "order_type": self._to_broker_enum(order.order_type, sj.OrderType),
            "octype": self._to_broker_enum(octype, sj.FuturesOCType),
            "custom_field": self.validate_custom_field(custom_field),
        }
        return sj.FuturesOrder(**self._with_account(fields, self.futopt_account))

    # === 檢查 ===
    @staticmethod
    def _with_account(fields: Dict[str, Any], account: Optional[Any]) -> Dict[str, Any]:
        """
        帳號為 None 時**整個欄位不帶**

        不給帳號時 shioaji 會用登入後的預設帳號；明確傳 None 的語意在不同版本不一致
        （舊版直接拒絕、且錯誤訊息看不出原因是「還沒登入」），故一律不帶。
        """

        if account is None:
            return fields
        return {**fields, "account": account}

    @staticmethod
    def _require_price_type(price_type: Optional[Any], order: Any) -> Any:
        """
        價格類型必須已決定

        **不預設成限價**：策略要的是市價而前處理漏了填值時，預設成限價會送出一張
        價格正確但語意不同的單，成交與否完全看運氣，而且兩邊都不會報錯。
        """

        if price_type is None:
            raise ValueError(
                f"{order.symbol} 的 price_type 尚未決定；"
                "它應由委託前處理依策略意圖填入（限價或市價），不可預設"
            )
        return price_type

    @staticmethod
    def _resolve_price(price_type: Any, order: Any) -> float:
        """
        市價單的價格一律送 0

        Shioaji 的市價單不看價格欄位。送進原本的參考價雖然多半也會被忽略，
        但那會讓事後看委託紀錄的人以為那是一張限價單。
        """

        if price_type in (
            StockPriceType.MKT,
            FuturesPriceType.MKT,
            FuturesPriceType.MKP,
        ):
            return 0.0
        return order.price

    @classmethod
    def _validate_stock_quantity(cls, order: StockOrder) -> None:
        """
        數量單位檢查

        整股的 `quantity` 是**張**、盤中零股是**股**。錯配不會報錯，
        只會下成 1000 倍或千分之一的量——而 1000 倍那個方向會直接吃掉整個帳戶。
        """

        if order.volume <= 0:
            raise ValueError(f"委託數量必須為正整數，收到 {order.volume}")

        if order.order_lot is StockOrderLot.IntradayOdd:
            if order.volume > cls.INTRADAY_ODD_MAX_SHARES:
                raise ValueError(
                    f"盤中零股的數量單位是股，最多 {cls.INTRADAY_ODD_MAX_SHARES} 股，"
                    f"收到 {order.volume}；要下整股請改用 order_lot=Common（單位為張）"
                )

    @staticmethod
    def _validate_price_limits(price: float, contract: Optional[Any]) -> None:
        """
        委託價不得超出當日漲跌停

        用交易所公告值（合約檔的 `limit_up`／`limit_down`）而不是自行推算：
        除權息日的基準價是另行公告的，公式推出來的區間會整段偏移。
        合約沒帶這兩個欄位時略過，不阻擋。
        """

        if contract is None:
            return

        limit_up: Optional[float] = getattr(contract, "limit_up", None)
        limit_down: Optional[float] = getattr(contract, "limit_down", None)
        if limit_up and price > limit_up:
            raise ValueError(f"委託價 {price} 高於漲停價 {limit_up}")
        if limit_down and price < limit_down:
            raise ValueError(f"委託價 {price} 低於跌停價 {limit_down}")

    @classmethod
    def validate_custom_field(cls, value: Optional[str]) -> str:
        """
        - Description:
            檢查隨委託往返的識別碼

            **在這裡擋，不要交給 shioaji**：它建構委託物件時已不檢查這個欄位，
            不合規的值要送到券商才會被拒，而尾盤段沒有重送的時間。
        - Parameters:
            - value: Optional[str]
                識別碼；None 時視為空字串
        - Return:
            - str
                可送出的識別碼
        - Raise:
            - ValueError
                超過長度上限或含非 ASCII 可列印字元
        """

        if not value:
            return ""

        if len(value) > cls.CUSTOM_FIELD_MAX_LENGTH:
            raise ValueError(
                f"custom_field 最多 {cls.CUSTOM_FIELD_MAX_LENGTH} 個字元，"
                f"收到 {len(value)} 個（{value!r}）"
            )
        if not all(" " <= char <= "~" for char in value):
            raise ValueError(f"custom_field 只能是可列印的 ASCII 字元，收到 {value!r}")
        return value

    @staticmethod
    def list_broker_enum_values(broker_enum: Any) -> List[str]:
        """
        - Description:
            列出 Shioaji Enum 類別的所有成員值

            shioaji 的 Enum 是原生類別，**不能 iterate**，成員只能從類別屬性讀出；
            `value`／`name` 是實例屬性的描述器，不是成員，要排除
        - Parameters:
            - broker_enum: Any
                Shioaji 的 Enum 類別（例如 `sj.StockOrderCond`）
        - Return:
            - List[str]
                成員值，依成員名排序
        """

        return [
            str(getattr(broker_enum, name).value)
            for name in sorted(dir(broker_enum))
            if not name.startswith("_")
            and name not in ("value", "name")
            and not inspect.isroutine(getattr(broker_enum, name))
        ]

    @staticmethod
    def _to_broker_enum(member: Enum, broker_enum: Any) -> Any:
        """
        - Description:
            把本專案的 Enum 換成 Shioaji 的對應成員

            **依「值」轉換，不依成員名**。兩者的成員名並非處處一致：本專案的
            `Action` 是 `BUY`／`SELL`（另有回測用的 `OPEN`／`CLOSE`），
            Shioaji 是 `Buy`／`Sell`——依名稱查會對每一張單都拋出。
            而送上線路的本來就是值，值對了才會成交。
            Shioaji 依值建構出來的成員與類別屬性**不是同一個物件**（`is` 不成立），
            比較一律用 `==`。

            成員名與值是否同步，由 `tests/test_order_state_parity.py` 另外盯住。

            **查不到一律拋出，絕不退回別的值**。最具體的例子是借券：
            shioaji 1.7.2 之前的 `StockOrderCond` 沒有 `SBLShort`，
            若在這裡退回 `ShortSelling`，送出去的會是一張用融券券源與成本成交的單，
            而回測那邊算的是議定費率——兩邊的成本從此對不上，且不會有任何錯誤訊息。
        - Parameters:
            - member: Enum
                本專案的 Enum 成員
            - broker_enum: Any
                Shioaji 對應的 Enum 類別
        - Return:
            - Any
                Shioaji 的 Enum 成員
        - Raise:
            - ValueError
                Shioaji 的 Enum 沒有這個值
        """

        try:
            return broker_enum(member.value)
        except ValueError:
            raise ValueError(
                f"shioaji 的 {broker_enum.__name__} 沒有 {member.name}（值 {member.value!r}）；"
                f"目前支援 {ShioajiOrderMapper.list_broker_enum_values(broker_enum)}。"
                "這通常代表要先升級 shioaji（例如借券的 SBLShort），"
                "**不可退回其他值**——那會送出一張條件不同的單"
            ) from None

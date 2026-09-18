import datetime
import math
from abc import ABC, abstractmethod
from typing import Optional, Tuple

from loguru import logger

from core.utils import (
    FUTURES_TICK_SIZE,
    PRICE_LIMIT_RATIO,
    PRICE_LIMIT_RATIO_LEGACY,
    PRICE_LIMIT_WIDENED_DATE,
    Action,
)
from core.utils.instrument import StockUtils

"""InstrumentSpec: 商品規格（報價單位換算、跳動點、漲跌停規則、滑價調價）"""

# 基點換算：1 bps = 0.01% = 萬分之一
BPS_PER_UNIT: float = 10_000.0


class InstrumentSpec(ABC):
    """
    商品規格：報價單位換算、跳動點、漲跌停規則

    市場差異最集中的地方，被成交價驗證與每日權益快照兩處使用。
    對應 Lean 的 SymbolProperties。
    """

    def apply_slippage(
        self,
        price: float,
        action: Action,
        bps: float,
        product: Optional[str] = None,
    ) -> float:
        """
        - Description:
            對參考價套用滑價，回傳含滑價的成交價

            **方向寫死、不由呼叫端決定符號**：買進往上、賣出往下，
            兩者都是對下單者不利的方向。滑價的意義是「你拿不到理想價」，
            若允許呼叫端傳負值，就會出現「滑價讓績效變好」這種無意義的設定。

            調整後會對齊該商品的跳動點，避免算出不可能成交的價格；
            對齊方向同樣取**對下單者不利**的一側（買進進位、賣出捨去）。
        - Parameters:
            - price: float
                參考價（策略給的委託價）
            - action: Action
                訂單動作；買進加價、賣出減價
            - bps: float
                滑價基點（1 bps = 0.01%）；`0` 時原價回傳，不做任何對齊
            - product: Optional[str]
                商品代碼，只有跳動點逐商品不同的市場（期貨）會用到
        - Return:
            - float
                含滑價的成交價
        """

        if not bps or price <= 0:
            return price

        ratio: float = bps / BPS_PER_UNIT

        if action == Action.BUY:
            return self.round_to_tick(price * (1 + ratio), "up", product)
        return self.round_to_tick(price * (1 - ratio), "down", product)

    @abstractmethod
    def to_units(self, volume: int) -> int:
        """
        - Description:
            下單數量 → 計價單位（台股：張 → 股 ×1000；期貨：口 → 契約乘數）
        - Parameters:
            - volume: int
                下單數量（台股為張、期貨為口）
        - Return:
            - int
                計價單位數量
        """
        pass

    @abstractmethod
    def round_to_tick(
        self,
        price: float,
        direction: str = "nearest",
        product: Optional[str] = None,
    ) -> float:
        """
        - Description:
            將價格對齊該商品的跳動點，避免算出不可能成交的價格
        - Parameters:
            - price: float
                原始價格
            - direction: str
                取整方向："up"（進位）、"down"（捨去）、"nearest"（就近）
            - product: Optional[str]
                商品代碼；只有跳動點逐商品不同的市場（期貨）需要，台股用不到
        - Return:
            - float
                對齊檔位後的價格
        """
        pass

    @abstractmethod
    def get_price_limits(
        self, prev_close: float
    ) -> Tuple[Optional[float], Optional[float]]:
        """
        - Description:
            依前一交易日收盤價推算漲跌停區間
        - Parameters:
            - prev_close: float
                前一交易日收盤價
        - Return:
            - Tuple[Optional[float], Optional[float]]
                (跌停價, 漲停價)；無漲跌停制度時回傳 (None, None)
        """
        pass


class TwStockSpec(InstrumentSpec):
    """台股規格：1 張 ＝ 1000 股、六段跳動點、漲跌停 ±10%"""

    def to_units(self, volume: int) -> int:
        """張 → 股（×1000）"""

        return StockUtils.convert_lot_to_share(volume)

    def round_to_tick(
        self,
        price: float,
        direction: str = "nearest",
        product: Optional[str] = None,
    ) -> float:
        """對齊台股六段分段檔位（`product` 用不到：台股的檔位由價格級距決定）"""

        return StockUtils.round_to_tick(price, direction)

    def get_price_limit_ratio(self, date: Optional[datetime.date] = None) -> float:
        """
        - Description:
            取得該日適用的漲跌停幅度

            **台股於 2015-06-01 由 7% 放寬為 10%**。以 23,972 筆交易所公告的
            漲停／跌停價實測：放寬前中位數 6.92%、放寬後 9.91%。單用 10% 會讓
            2013-01 ~ 2015-05 的區間偏寬約 43%，該期間與官方值的相符率為 0.0%。
        - Parameters:
            - date: Optional[datetime.date]
                交易日；`None` 時採現行幅度（呼叫端未提供日期即視為當代回測）
        - Return:
            - float
                該日適用的幅度
        """

        if date is not None and date < PRICE_LIMIT_WIDENED_DATE:
            return PRICE_LIMIT_RATIO_LEGACY

        return PRICE_LIMIT_RATIO

    def get_price_limits(
        self,
        prev_close: float,
        date: Optional[datetime.date] = None,
    ) -> Tuple[Optional[float], Optional[float]]:
        """
        - Description:
            台股漲跌停為前收 ±幅度，並各自往內對齊檔位（漲停捨去、跌停進位）

            對齊方向不可對調：漲停若進位會算出高於法定漲停的價格。

            幅度依 `date` 決定（2015-06-01 前為 7%）；未提供日期時採現行幅度。

            **已知落差（2026-08-15 實測，尚未解決）**：以 23,972 筆交易所公告的
            漲停／跌停價比對，本方法的相符率為 **61.6%**。修正幅度分段前為 54.5%，
            分段解掉了 2013~2015/05 的整段偏差（該期間相符率 0.0% → 約 73%），
            剩餘落差來自**檔位對齊規則**——本方法採「±幅度後往內對齊檔位」，
            與交易所實際的升降單位取值規則不完全一致，多數不符者相差一個檔位。

            影響範圍：漲跌停只在 `FillModel.validate()` 用於拒單，多數訂單不在
            邊界上；但放空的「漲停鎖死無法回補」判定（`check_limit_up_locked`）
            直接依賴此結果，`limit_up_cover_failed` 事件計數會有偏差。
        - Parameters:
            - prev_close: float
                漲跌停基準價（一般為前一交易日收盤；除權息日為開盤競價基準）
            - date: Optional[datetime.date]
                交易日，用於選取當時適用的漲跌停幅度
        - Return:
            - Tuple[Optional[float], Optional[float]]
                （跌停價, 漲停價）；基準價為 0 時皆為 None
        """

        if not prev_close:
            return (None, None)

        ratio: float = self.get_price_limit_ratio(date)

        limit_up: float = self.round_to_tick(prev_close * (1 + ratio), "down")
        limit_down: float = self.round_to_tick(prev_close * (1 - ratio), "up")
        return (limit_down, limit_up)

    def is_locked_at_limit(
        self,
        prev_close: Optional[float],
        open_price: Optional[float],
        high: Optional[float],
        low: Optional[float],
        close: Optional[float],
        side: Action,
        date: Optional[datetime.date] = None,
    ) -> bool:
        """
        - Description:
            判定該日是否**全日鎖死**在漲停（或跌停）

            開高低收四個價都等於漲停價，代表整天沒有人願意在漲停以下賣出——
            這種標的實務上排隊也買不到，回測若照常成交會系統性偏樂觀；
            反過來鎖跌停時賣不掉，放空開倉與回補同樣不可能成交。

            **判定只有一份**：成交價驗證（買進開倉拒單）與結算的「漲停鎖死
            回補不了」共用本方法，兩邊各寫一次必然漂移。
        - Parameters:
            - prev_close: Optional[float]
                漲跌停基準價；None 或 0 時無從判定，一律回 False
            - open_price / high / low / close: Optional[float]
                當日四價
            - side: Action
                `BUY` 判漲停鎖死、`SELL` 判跌停鎖死
            - date: Optional[datetime.date]
                交易日，用於選取當時適用的漲跌停幅度
        - Return:
            - bool
                是否全日鎖死
        """

        if not prev_close:
            return False

        limit_down, limit_up = self.get_price_limits(prev_close, date)
        limit: Optional[float] = limit_up if side == Action.BUY else limit_down
        if limit is None:
            return False

        prices: Tuple[Optional[float], ...] = (open_price, high, low, close)
        if any(price is None or price <= 0 for price in prices):
            return False

        return all(price == limit for price in prices)


class TwFuturesSpec(InstrumentSpec):
    """
    台期貨規格：跳動點**逐商品查表**、**無固定漲跌停**

    與 `TwStockSpec` 的兩個根本差異，兩個都會讓沿用股票習慣的人靜默算錯：

    1. **計價單位換算逐契約不同**（TX 200、MTX 50、TE 4000），而 `to_units()`
       只拿得到數量、拿不到商品——單一 spec 無法代表整個期貨市場。故本方法一律
       回傳口數，**乘數改由 `FuturesPosition.multiplier` 提供**（該欄位在開倉時
       由 `FUTURES_MULTIPLIER` 查得）；要算損益一律走
       `FuturesPositionManager.calculate_pnl()`，不要自己乘。
    2. **沒有固定漲跌停**：期貨採動態價格穩定措施（撮合價超出參考區間即延後撮合），
       區間隨前幾分鐘的成交價變動，不是「前收 ±10%」這種可事先算出的固定區間。
       故 `get_price_limits()` 一律回傳 `(None, None)`，`TwFuturesFillModel`
       也不做漲跌停檢查。

    **跳動點逐商品查表**（`FUTURES_TICK_SIZE`，只登錄已查證的商品）：同一個
    `slippage_ticks_*` 設定在 TX 是 1 點、在 TE 是 0.05 點，寫死單一數值會讓
    非台指期系列的滑價偏掉 20 倍，而且不會有任何徵兆。故本 spec 不再自帶
    唯一的跳動點——需要對齊檔位的呼叫端**一律把 `product` 傳進來**。
    """

    # 未帶商品資訊、或商品尚未登錄時的退回值（台指期系列的跳動點）
    DEFAULT_TICK_SIZE: float = 1.0

    def __init__(self, tick_size: Optional[float] = None) -> None:
        # **明確指定時一律覆寫查表**：留給尚未登錄的商品與單一契約的測試；
        # `None`（正常回測路徑）代表逐商品查 `FUTURES_TICK_SIZE`
        self.tick_size: Optional[float] = tick_size

    def get_tick_size(self, product: Optional[str] = None) -> float:
        """
        - Description:
            取得該商品的最小跳動點

            三層順序：建構時明確指定的值 → `FUTURES_TICK_SIZE` 查表 → 退回
            `DEFAULT_TICK_SIZE`。

            **未登錄的商品退回預設值而不是中斷**：跳動點只在「對齊檔位」與
            「以檔數表達的滑價」兩處生效，滑價預設為 0 時整條路徑沒有作用，
            為此炸掉一場本來跑得起來的回測不成比例。但退回的那一次要被看見，
            故記一筆 warning——`FUTURES_TICK_SIZE[code]` 本身仍維持不給預設值，
            直接查表的呼叫端照樣會 `KeyError`。
        - Parameters:
            - product: Optional[str]
                商品代碼；`None` 表示呼叫端沒有商品資訊
        - Return:
            - float
                該商品的跳動點（點）
        """

        if self.tick_size is not None:
            return self.tick_size

        if not product:
            return self.DEFAULT_TICK_SIZE

        if product not in FUTURES_TICK_SIZE:
            logger.warning(
                f"[Instrument] {product} 的跳動點尚未登錄於 FUTURES_TICK_SIZE，"
                f"暫以 {self.DEFAULT_TICK_SIZE} 點計算；"
                f"以跳動點數設定的滑價與檔位對齊都會失真，請查證後登錄"
            )
            return self.DEFAULT_TICK_SIZE

        return FUTURES_TICK_SIZE[product]

    def to_units(self, volume: int) -> int:
        """口 → 口（**不乘契約乘數**，理由見 class docstring 第 1 點）"""

        return volume

    def round_to_tick(
        self,
        price: float,
        direction: str = "nearest",
        product: Optional[str] = None,
    ) -> float:
        """
        - Description:
            將價格對齊該商品的跳動點；跳動點 ≤ 0 時原價回傳

            **多一個 `product` 參數**（台股那份沒有）：台股的檔位由價格級距決定，
            期貨的跳動點由商品決定。不傳商品時退回 `DEFAULT_TICK_SIZE`，
            滑價算對了卻被對齊推回去的話，兩邊要一起看。
        - Parameters:
            - price: float
                原始價格
            - direction: str
                取整方向："up"（進位）、"down"（捨去）、"nearest"（就近）
            - product: Optional[str]
                商品代碼，決定採用哪一個跳動點
        - Return:
            - float
                對齊跳動點後的價格
        """

        tick_size: float = self.get_tick_size(product)

        if tick_size <= 0:
            return price

        # **先吸收除法的浮點誤差再取整**：17999.8 / 0.2 在浮點下是
        # 89998.99999999999，往下取整會整整少一檔。跳動點是整數（1 點）時看不到
        # 這件事，TF／ZFF 的 0.2 點與 TE／ZEF 的 0.05 點才會踩到。
        # 取到小數第 9 位遠小於任何有意義的價差，不會吃掉真正落在檔位之間的價格
        ticks: float = round(price / tick_size, 9)

        if direction == "up":
            aligned: float = math.ceil(ticks)
        elif direction == "down":
            aligned = math.floor(ticks)
        else:
            # 不用內建 round()：它採銀行家捨入，.5 會依奇偶倒向不同邊
            aligned = math.floor(ticks + 0.5)

        # 浮點誤差會讓 0.05 這類跳動點算出 18000.049999999999
        return round(aligned * tick_size, 10)

    def get_price_limits(
        self, prev_close: float
    ) -> Tuple[Optional[float], Optional[float]]:
        """
        期貨**沒有固定漲跌停**，一律回傳 `(None, None)`

        現行制度是動態價格穩定措施，區間由前幾分鐘的成交價即時算出，
        無法由前一交易日收盤推得。回傳 `(None, None)` 的語意是「本市場無此制度」，
        呼叫端（`FillModel`）據此跳過該項檢查，**不是「查不到資料」**。
        """

        return (None, None)

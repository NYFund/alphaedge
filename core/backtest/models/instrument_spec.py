import datetime
import math
from abc import ABC, abstractmethod
from typing import Optional, Tuple

from core.utils import (
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

    def apply_slippage(self, price: float, action: Action, bps: float) -> float:
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
        - Return:
            - float
                含滑價的成交價
        """

        if not bps or price <= 0:
            return price

        ratio: float = bps / BPS_PER_UNIT

        if action == Action.BUY:
            return self.round_to_tick(price * (1 + ratio), "up")
        return self.round_to_tick(price * (1 - ratio), "down")

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
    def round_to_tick(self, price: float, direction: str = "nearest") -> float:
        """
        - Description:
            將價格對齊該商品的跳動點，避免算出不可能成交的價格
        - Parameters:
            - price: float
                原始價格
            - direction: str
                取整方向："up"（進位）、"down"（捨去）、"nearest"（就近）
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

    def round_to_tick(self, price: float, direction: str = "nearest") -> float:
        """對齊台股六段分段檔位"""

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
    台期貨規格：跳動點 1 點（台指期系列）、**無固定漲跌停**

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

    **跳動點只登錄已查證的商品**（理由同 `FUTURES_MULTIPLIER`：猜錯不會有徵兆）：
    台指期系列（TX／MTX／TMF）為 1 點。電子期、金融期與股票期貨的跳動點不同且
    尚未查證，要回測那些商品必須在建構時明確指定 `tick_size`；
    逐商品查證後再改為依商品查表。
    """

    DEFAULT_TICK_SIZE: float = 1.0  # 台指期系列的最小跳動點（點）

    def __init__(self, tick_size: float = DEFAULT_TICK_SIZE) -> None:
        self.tick_size: float = tick_size  # 最小跳動點（點）

    def to_units(self, volume: int) -> int:
        """口 → 口（**不乘契約乘數**，理由見 class docstring 第 1 點）"""

        return volume

    def round_to_tick(self, price: float, direction: str = "nearest") -> float:
        """
        - Description:
            將價格對齊跳動點；`tick_size` 未設定（≤ 0）時原價回傳
        - Parameters:
            - price: float
                原始價格
            - direction: str
                取整方向："up"（進位）、"down"（捨去）、"nearest"（就近）
        - Return:
            - float
                對齊跳動點後的價格
        """

        if self.tick_size <= 0:
            return price

        ticks: float = price / self.tick_size

        if direction == "up":
            aligned: float = math.ceil(ticks)
        elif direction == "down":
            aligned = math.floor(ticks)
        else:
            # 不用內建 round()：它採銀行家捨入，.5 會依奇偶倒向不同邊
            aligned = math.floor(ticks + 0.5)

        # 浮點誤差會讓 0.05 這類跳動點算出 18000.049999999999
        return round(aligned * self.tick_size, 10)

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

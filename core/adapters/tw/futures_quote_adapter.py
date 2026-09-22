import datetime
import math
from typing import Any, Callable, List, Optional

import pandas as pd

from core.config.schema import FuturesPriceColumn
from core.models import FuturesQuote
from core.utils import FuturesSession, Scale
from core.utils.constant import FUTURES_MULTIPLIER

"""
FuturesQuoteAdapter: 把 `FuturesPriceAPI` 的查詢結果轉成回測吃的 `FuturesQuote`

**只做型別轉換，不做任何選擇**。單日單商品會有多個到期月，本 adapter 一律**全部**
轉出，要哪一個由呼叫端決定——換月是政策，由連續合約與 `FuturesRollPlanner` 負責，
寫進 adapter 會讓兩處各有一套換月邏輯。

與 `StockQuoteAdapter` 的差異：
- 沒有 `filter_common_stocks()` 這類過濾：期貨的商品清單由設定檔決定，不是從
  代號規則推出來的。
- 沒有還原價：期貨沒有除權息還原的概念。**股票期貨也不例外**——它的除權息由
  「調整契約單位」承接，再套台股的還原係數就是雙重調整
  （見 `FuturesStockUniverseAPI`）。
- **同一根 bar 內出現重複 symbol 是正常的**——同一契約的日盤與夜盤是兩筆；
  因此 `session` 混用時不發重複警告，而是要求呼叫端指定時段。
"""


class FuturesQuoteAdapter:
    """將 `futures_price_daily` 的查詢結果轉換為統一格式的 `FuturesQuote` 物件"""

    @staticmethod
    def to_optional_float(value: Any) -> Optional[float]:
        """
        NULL／NaN 一律轉成 None，不轉成 0

        夜盤沒有結算價與未沖銷契約量，補成 0 會讓「沒有」與「等於 0」混為一談，
        而後者在逐日盯市與保證金計算裡是完全不同的意思。
        """

        if value is None:
            return None
        if isinstance(value, float) and math.isnan(value):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def to_optional_int(value: Any) -> Optional[int]:
        """同 `to_optional_float`，但用於未沖銷契約量這類整數欄位"""

        as_float: Optional[float] = FuturesQuoteAdapter.to_optional_float(value)
        return None if as_float is None else int(as_float)

    @staticmethod
    def resolve_multiplier(product: str) -> int:
        """
        預設的乘數解析：指數期貨查 `FUTURES_MULTIPLIER`

        **未登錄的商品直接 KeyError**（理由見該常數）。股票期貨的乘數是會隨
        除權息調整的「契約單位」，查不到是正常的——那種商品要由呼叫端傳入
        `multiplier_resolver`（`TwFuturesDataFeed` 已備好，見該處）。
        """

        return FUTURES_MULTIPLIER[product]

    @staticmethod
    def from_day_rows(
        price_df: pd.DataFrame,
        date: datetime.date,
        multiplier_resolver: Optional[Callable[[str], int]] = None,
    ) -> List[FuturesQuote]:
        """
        - Description:
            把當日的行情表轉成 `FuturesQuote` 清單（日級回測用）

            **當日所有到期月都會轉出**，不挑近月。
            **查詢由呼叫端負責**：商品與時段是查詢條件，隨查詢一起留在 feed，
            本層是純轉換，不碰 `core.api`。
        - Parameters:
            - price_df: pd.DataFrame
                當日的行情表（已依商品與時段篩選）
            - date: datetime.date
                要轉換的日期
            - multiplier_resolver: Optional[Callable[[str], int]]
                商品代碼 → 契約乘數；None 時用內建對照表
        - Return:
            - List[FuturesQuote]
                轉換後的報價清單；查無資料時為空 list
        """

        return FuturesQuoteAdapter.generate_futures_quotes(
            price_df, date, Scale.DAY, multiplier_resolver=multiplier_resolver
        )

    @staticmethod
    def combine_sessions(
        day_quotes: List[FuturesQuote],
        night_quotes: List[FuturesQuote],
    ) -> List[FuturesQuote]:
        """
        - Description:
            把「當日夜盤 ＋ 當日日盤」整併成一根 bar

            **夜盤在前、日盤在後**：TAIFEX 的夜盤前一交易日 15:00 開盤、
            當日 05:00 收盤，制度上屬於當日——星期五晚上那一段屬於星期一，
            而行情表記的正是它所屬的交易日（`night` 列的 `date` 就是星期一）。
            整併時因此取同一個日期，不必往前找。

            **跨盤別的跳空被保留在 bar 內**：整併後的 `open` 是**夜盤開盤價**，
            不是日盤開盤價——前一個日盤收盤到夜盤開盤之間的跳空，
            正是隔夜風險的來源，用日盤開盤當 open 會把它整段抹掉。

            | 欄位 | 取值 | 理由 |
            |------|------|------|
            | `open` | 夜盤開盤（無夜盤時為日盤開盤） | 這段 bar 的第一筆成交 |
            | `high` / `low` | 兩盤的極值 | 夜盤的極端價常常就是當日的極值 |
            | `close` | **日盤收盤** | 這段 bar 的最後一筆成交 |
            | `volume` | 兩盤相加 | 同一根 bar 的總量 |
            | `settlement_price` / `open_interest` | **只取日盤** | 夜盤根本沒有這兩項 |

            **2017-05-15 之前沒有夜盤**，此時整併結果等於日盤本身——那是制度
            而非資料缺漏（見 `FuturesCalendar.NIGHT_SESSION_LAUNCH_DATE`）。
        - Parameters:
            - day_quotes: List[FuturesQuote]
                當日日盤的報價
            - night_quotes: List[FuturesQuote]
                同一個交易日的夜盤報價；**2017-05-15 之前給空 list**
                （那是制度而非資料缺漏，見 `FuturesCalendar.NIGHT_SESSION_LAUNCH_DATE`）
        - Return:
            - List[FuturesQuote]
                整併後的報價；日盤為空時回空 list
        """

        if not night_quotes:
            return [FuturesQuoteAdapter.mark_combined(quote) for quote in day_quotes]

        night_by_contract: dict = {quote.contract_id: quote for quote in night_quotes}

        return [
            FuturesQuoteAdapter.combine_quote(
                quote, night_by_contract.get(quote.contract_id)
            )
            for quote in day_quotes
        ]

    @staticmethod
    def mark_combined(quote: FuturesQuote) -> FuturesQuote:
        """把單一時段的報價標記為整併結果（沒有夜盤可併時使用）"""

        quote.session = FuturesSession.COMBINED
        return quote

    @staticmethod
    def combine_quote(
        day_quote: FuturesQuote, night_quote: Optional[FuturesQuote]
    ) -> FuturesQuote:
        """
        - Description:
            合併同一契約的日盤與夜盤報價

            **就地修改日盤那一筆並回傳**：日盤報價是本方法剛從 adapter 建出來的
            新物件，沒有其他持有者。

            夜盤缺該契約（多數契約夜盤不交易）時原樣回傳日盤——
            **不可補 0**，那會讓 `low` 變成 0、`open` 變成 0。
        - Parameters:
            - day_quote: FuturesQuote
                當日日盤報價
            - night_quote: Optional[FuturesQuote]
                同一交易日的夜盤報價（前一交易日 15:00 開盤的那一段）
        - Return:
            - FuturesQuote
                整併後的報價
        """

        if night_quote is None or not night_quote.close:
            return FuturesQuoteAdapter.mark_combined(day_quote)

        # 夜盤先發生，故 open 取夜盤；close 取日盤（bar 的最後一筆成交）
        day_quote.open = night_quote.open or day_quote.open
        day_quote.high = max(day_quote.high or 0, night_quote.high or 0)
        day_quote.low = min(
            [value for value in (day_quote.low, night_quote.low) if value]
            or [day_quote.low]
        )
        day_quote.volume = (day_quote.volume or 0) + (night_quote.volume or 0)

        return FuturesQuoteAdapter.mark_combined(day_quote)

    @staticmethod
    def generate_futures_quotes(
        data: pd.DataFrame,
        date: datetime.date,
        scale: Scale = Scale.DAY,
        multiplier_resolver: Optional[Callable[[str], int]] = None,
    ) -> List[FuturesQuote]:
        """
        - Description:
            由行情 DataFrame 建立 `FuturesQuote` 清單

            **乘數未登錄的商品直接跳過並不是選項**：`FUTURES_MULTIPLIER[product]`
            會 KeyError 當場中斷，理由見該常數的說明——乘數猜錯只會讓 PnL
            靜默偏掉。
        - Parameters:
            - data: pd.DataFrame
                行情資料（欄位為 `futures_price_daily` 的 schema）
            - date: datetime.date
                報價日期
            - scale: Scale
                報價級別；目前僅日線
        - Return:
            - List[FuturesQuote]
                報價清單
        """

        if data is None or data.empty:
            return []

        return [
            FuturesQuoteAdapter.generate_futures_quote(
                row, date, scale, multiplier_resolver=multiplier_resolver
            )
            for row in data.itertuples(index=False)
        ]

    @staticmethod
    def generate_futures_quote(
        row: Any,
        date: datetime.date,
        scale: Scale = Scale.DAY,
        multiplier_resolver: Optional[Callable[[str], int]] = None,
    ) -> FuturesQuote:
        """
        - Description:
            由單列行情建立一個 `FuturesQuote`

            `cur_price` 取收盤價，與 `StockQuoteAdapter` 的日線行為一致。
        - Parameters:
            - row: Any
                `itertuples()` 產出的單列
            - date: datetime.date
                報價日期
            - scale: Scale
                報價級別
        - Return:
            - FuturesQuote
                報價物件
        """

        close: float = getattr(row, FuturesPriceColumn.CLOSE.value)
        product: str = str(row.product)

        return FuturesQuote(
            product=product,
            expiry=str(row.expiry),
            scale=scale,
            date=date,
            cur_price=close,
            volume=int(getattr(row, FuturesPriceColumn.VOLUME.value) or 0),
            open=getattr(row, FuturesPriceColumn.OPEN.value),
            high=getattr(row, FuturesPriceColumn.HIGH.value),
            low=getattr(row, FuturesPriceColumn.LOW.value),
            close=close,
            session=FuturesSession(row.session),
            settlement_price=FuturesQuoteAdapter.to_optional_float(
                getattr(row, FuturesPriceColumn.SETTLEMENT.value)
            ),
            open_interest=FuturesQuoteAdapter.to_optional_int(
                getattr(row, FuturesPriceColumn.OPEN_INTEREST.value)
            ),
            multiplier=(multiplier_resolver or FuturesQuoteAdapter.resolve_multiplier)(
                product
            ),
        )

import datetime
from typing import Callable, List, Optional, Tuple

from loguru import logger

from core.models import BaseAccount, BaseOrder, BasePosition
from core.utils import Action, DayTradeUncoveredPolicy, PositionType

"""
日終強制動作：當沖回補與期貨夜盤的日期歸屬

回測的 `SettlementModel` 會在日終「替你把事情做完」，實盤不會——**沒有人會自動
幫你回補**。現股當沖先賣未回補，券商可能標借或直接違約交割，而程式這邊看起來
一切正常。

本模組的判定全部寫成**純函式**：日期歸屬與政策對映不碰帳戶、不碰時鐘，
連實例都不必建就測得動。需要狀態的只有「今天回補過了沒」。
"""

# 現股當沖的回補時點。**不等到 13:30**：收盤前最後幾分鐘流動性會變差，
# 而回補單送不出去的代價是違約交割
DAY_TRADE_COVER_TIME: datetime.time = datetime.time(13, 20)

# TAIFEX 夜盤 15:00 開盤、次日 05:00 收盤
NIGHT_SESSION_START_HOUR: int = 15
NIGHT_SESSION_END_HOUR: int = 5


def resolve_live_policy(
    policy: DayTradeUncoveredPolicy,
) -> Tuple[DayTradeUncoveredPolicy, Optional[str]]:
    """
    - Description:
        把回測的當沖未回補政策對映成**實盤真的做得到**的動作

        三個值在實盤的處境不同：

        | 回測政策 | 實盤 | 理由 |
        |---|---|---|
        | `FORCE_COVER_AT_CLOSE` | 照做（送可成交的限價單） | — |
        | `CONVERT_TO_MARGIN` | **改走強制回補** | 現股當沖先賣未回補**不會自動變成融券部位**，那是券商端的處理（可能標借或違約），程式這邊決定不了 |
        | `RAISE` | **先回補再停止** | 只拋例外會讓部位留在場上過夜，而那正是要避免的事 |

        **回傳警告理由而不是直接吞掉**：政策被改寫是要讓人知道的，
        不然設定與實際行為會永遠對不起來。
    - Parameters:
        - policy: DayTradeUncoveredPolicy
            策略宣告的政策
    - Return:
        - Tuple[DayTradeUncoveredPolicy, Optional[str]]
            `(實盤實際採用的政策, 需要告警的理由)`；照做時理由為 None
    """

    if policy is DayTradeUncoveredPolicy.CONVERT_TO_MARGIN:
        return (
            DayTradeUncoveredPolicy.FORCE_COVER_AT_CLOSE,
            "CONVERT_TO_MARGIN 在實盤不存在：現股當沖未回補不會自動轉成融券部位，"
            "改走強制回補",
        )

    if policy is DayTradeUncoveredPolicy.RAISE:
        return (
            DayTradeUncoveredPolicy.FORCE_COVER_AT_CLOSE,
            "RAISE 在實盤改為「先強制回補再停止」：只拋例外會讓部位留在場上過夜",
        )

    return (policy, None)


def stops_after_cover(policy: DayTradeUncoveredPolicy) -> bool:
    """回補完要不要停止程式；只有 `RAISE` 要"""

    return policy is DayTradeUncoveredPolicy.RAISE


def is_night_session(moment: datetime.datetime) -> bool:
    """
    這個時刻屬不屬於夜盤

    夜盤 15:00~次日 05:00；**兩段之間的空檔（05:00~08:45、13:45~15:00）歸日盤**——
    那段時間沒有行情，但歸夜盤會讓收盤後的動作被記成次一交易日的帳。
    """

    return (
        moment.hour >= NIGHT_SESSION_START_HOUR or moment.hour < NIGHT_SESSION_END_HOUR
    )


def session_open_date(moment: datetime.datetime) -> datetime.date:
    """
    這一段行情是哪一天開始的

    **跨午夜之後要往前一天**：星期五 15:00 開始的夜盤，到星期六 00:30 還是
    同一段；取當下的日曆日會把它算成新的一段。
    """

    if moment.hour < NIGHT_SESSION_END_HOUR:
        return moment.date() - datetime.timedelta(days=1)
    return moment.date()


def resolve_accounting_date(
    moment: datetime.datetime,
    next_trading_day: Callable[[datetime.date], datetime.date],
) -> datetime.date:
    """
    - Description:
        成交與損益要記在哪一個交易日

        **夜盤屬於次一交易日**：TAIFEX 的制度是 15:00 開盤、次日 05:00 收盤，
        而那一整段在制度上是次一交易日的一部分——星期五晚上的那一段屬於星期一。

        **跨午夜時帳務日不變**：23:59 與次日 00:01 是同一段夜盤，
        兩者的帳務日都是同一天。取當下日曆日再往後推會讓 00:01 之後的成交
        整段記到後一天，而那個錯誤只會在對帳時以「昨天的量對不起來」出現。
    - Parameters:
        - moment: datetime.datetime
            當下時刻（台北）
        - next_trading_day: Callable[[datetime.date], datetime.date]
            給定日期求次一交易日；由呼叫端以交易日曆注入
    - Return:
        - datetime.date
            帳務日
    """

    if not is_night_session(moment):
        return moment.date()
    return next_trading_day(session_open_date(moment))


def resolve_quote_date(
    moment: datetime.datetime,
    next_trading_day: Callable[[datetime.date], datetime.date],
) -> datetime.date:
    """
    - Description:
        查歷史行情要用的日期；**與帳務日相同**

        `futures_price_daily` 存的是夜盤**所屬的交易日**，不是它開盤的曆日——
        星期五晚上那一段記在星期一那一列。兩項實測佐證：

        1. TX 第一筆 `night` 列是 2017-05-16，而夜盤制度是 2017-05-15 **晚上**上線；
        2. 2023 年起的近月樣本中，夜盤開盤價與「前一交易日日盤收盤」的差距中位數
           12 點，與「同日日盤收盤」則是 75 點——夜盤實際發生在前一晚，
           開盤價必然貼近前一交易日的收盤。

        所以兩個日期同值。**本函式刻意保留而不是叫呼叫端去用帳務日**：
        「要查哪一天的行情」與「成交記在哪一天」是兩個問題，
        它們在這張表上碰巧同值，不代表下一張表也會。
    - Parameters:
        - moment: datetime.datetime
            當下時刻（台北）
        - next_trading_day: Callable[[datetime.date], datetime.date]
            給定日期求次一交易日；由呼叫端以交易日曆注入
    - Return:
        - datetime.date
            查詢用日期
    """

    return resolve_accounting_date(moment, next_trading_day)


def uncovered_day_trade_positions(account: BaseAccount) -> List[BasePosition]:
    """
    找出當沖放空且尚未回補的部位

    判準與回測的 `TwStockSettlementModel.cover_day_trade_shorts()` 一致：
    未平倉的空單，且 `is_day_trade` 為真。兩邊不一致的話，回測看得到的留倉
    在實盤不會發生（或反過來），而 parity 比對會把它歸成未解釋差異。
    """

    return [
        position
        for position in account.get_positions(position_type=PositionType.SHORT)
        if getattr(position, "is_day_trade", False)
    ]


class SessionGuard:
    """
    - Description:
        盤中的日終強制動作

        **只負責產生回補單，不負責送出**：送單要走跨策略守門、曝險截斷、
        資金保留與風控，那條管線在 `LiveTrader`。本層另寫一份必然漂移。
    """

    def __init__(
        self,
        now_provider: Callable[[], datetime.datetime],
        cover_time: datetime.time = DAY_TRADE_COVER_TIME,
    ) -> None:
        """
        - Description:
            建立日終守門
        - Parameters:
            - now_provider: Callable[[], datetime.datetime]
                取得目前時間（台北）
            - cover_time: datetime.time
                現股當沖的回補時點
        """

        self._now: Callable[[], datetime.datetime] = now_provider
        self.cover_time: datetime.time = cover_time

        # 已經回補過的交易日；**以日期為鍵而不是布林旗標**：
        # 常駐行程跨日之後旗標不會自己歸零，於是第二天永遠不回補
        self.covered_dates: set = set()

    def should_cover_now(self) -> bool:
        """現在該不該做當沖回補；同一個交易日只做一次"""

        moment: datetime.datetime = self._now()
        if moment.time() < self.cover_time:
            return False
        return moment.date() not in self.covered_dates

    def mark_covered(self) -> None:
        """記下今天已經回補過"""

        self.covered_dates.add(self._now().date())

    def build_cover_orders(
        self,
        account: BaseAccount,
        policy: DayTradeUncoveredPolicy,
        build_order: Callable[[BasePosition], Optional[BaseOrder]],
    ) -> Tuple[List[BaseOrder], Optional[str]]:
        """
        - Description:
            替所有未回補的當沖空單產生回補單
        - Parameters:
            - account: BaseAccount
                該策略的帳戶
            - policy: DayTradeUncoveredPolicy
                策略宣告的政策
            - build_order: Callable[[BasePosition], Optional[BaseOrder]]
                把一個部位換成回補單；取不到價格時回 None
        - Return:
            - Tuple[List[BaseOrder], Optional[str]]
                `(回補單, 政策被改寫的告警理由)`
        """

        _effective, warning = resolve_live_policy(policy)

        orders: List[BaseOrder] = []
        for position in uncovered_day_trade_positions(account):
            order: Optional[BaseOrder] = build_order(position)
            if order is None:
                # **取不到價格不可以靜靜跳過**：那筆部位會留倉過夜，
                # 而這是現股當沖最不能發生的事
                logger.error(
                    f"[Day Trade Cover] {position.symbol} 取不到可成交價，"
                    "無法產生回補單；請立即人工處理"
                )
                continue
            orders.append(order)

        return (orders, warning)


def cover_action(position: BasePosition) -> Action:
    """回補空單就是買進；與 `resolve_close_action()` 的推導一致"""

    return Action.BUY if position.position_type is PositionType.SHORT else Action.SELL

import datetime
import queue
from typing import Any, Callable, List, Optional, Sequence, Set

import shioaji.constant as sj_constant
from loguru import logger

from core.broker.rate_limiter import RateLimitCategory, RateLimiter
from core.config.settings import get_live_timezone
from core.models import FuturesQuote, StockQuote
from core.utils import FUTURES_MULTIPLIER, FuturesSession, Scale

"""
即時行情：快照與逐筆訂閱，轉成與回測同款的報價物件

**策略讀到的報價物件必須和回測一模一樣**，否則同一支策略在兩邊會拿到不同結構，
D0「策略層不分家」就不成立了。

兩個一定要說清楚的語意：

1. **`volume` 取 `total_volume`，不是 `volume`。** 快照的 `volume` 是**該筆**成交量，
   `total_volume` 才是當日累計。取錯的話，「當日成交量 ≥ 5000 張」這類門檻
   **永遠不會成立**，策略整天不開倉而且不會有任何錯誤訊息。
2. **盤中的 `close` 是暫定值。** 它是「目前最新成交價」，收盤前還會變。
   策略若用它算「今天漲幅」，得到的是此刻的漲幅，不是收盤漲幅——
   這正是 D1 要把日頻策略拆成開盤段與尾盤段的原因。
"""

# 一次快照請求帶幾檔。官方對 `snapshots` 的單次上限未在文件明載，
# 故取保守值；每批算一次 `MARKET_DATA` 額度（50 次／10 秒）
SNAPSHOT_BATCH_SIZE: int = 100


class ShioajiQuoteStream:
    """
    - Description:
        行情快照與訂閱

        回呼**只把報價丟進 queue**，理由與成交回報相同：它跑在券商的執行緒上，
        在裡面算訊號會拖住整條行情接收路徑。
    """

    # 每個連線的訂閱上限（官方值）
    MAX_SUBSCRIPTIONS: int = 200

    def __init__(
        self,
        api: Any,
        rate_limiter: RateLimiter,
        quote_queue: queue.Queue,
        now_provider: Optional[Callable[[], datetime.datetime]] = None,
    ) -> None:
        """
        - Description:
            建立行情元件
        - Parameters:
            - api: Any
                已登入的 Shioaji API 物件
            - rate_limiter: RateLimiter
                與其他元件共用的限流器
            - quote_queue: queue.Queue
                行情事件佇列
            - now_provider: Optional[Callable[[], datetime.datetime]]
                取得目前時間；未提供時由快照自帶的時戳決定日期
        """

        self.api: Any = api
        self.rate_limiter: RateLimiter = rate_limiter
        self.quote_queue: queue.Queue = quote_queue
        self._now: Optional[Callable[[], datetime.datetime]] = now_provider
        self.subscribed: Set[str] = set()

    # === 訂閱 ===
    def register_callbacks(self) -> None:
        """
        註冊逐筆與委買賣回呼

        股票與期貨各有自己的回呼（`*_stk_v1`／`*_fop_v1`），四個都要掛：
        少掛一個不會報錯，只會讓那一類行情安靜地收不到。
        """

        quote: Any = self.api.quote
        quote.set_on_tick_stk_v1_callback(self._make_callback("tick_stk"))
        quote.set_on_bidask_stk_v1_callback(self._make_callback("bidask_stk"))
        quote.set_on_tick_fop_v1_callback(self._make_callback("tick_fop"))
        quote.set_on_bidask_fop_v1_callback(self._make_callback("bidask_fop"))
        logger.info("行情回呼已註冊（tick／bidask × 股票／期貨）")

    def _make_callback(self, kind: str) -> Callable[[Any, Any], None]:
        """
        產生一個只做入列的回呼

        **整個函式包 try**：回呼跑在券商的執行緒上，例外往上拋會讓那條執行緒死掉，
        之後所有行情靜默消失——而策略還在跑，只是再也收不到報價。
        """

        def callback(exchange: Any, message: Any) -> None:
            try:
                self.quote_queue.put((kind, exchange, message))
            except Exception as exc:
                logger.opt(exception=True).error(f"行情入列失敗（{kind}）：{exc}")

        return callback

    def subscribe(self, contracts: Sequence[Any], with_bidask: bool = True) -> None:
        """
        - Description:
            訂閱逐筆行情

            **超過上限在訂閱之前就拋出**：先訂到滿再失敗的話，前面幾檔會訂閱成功、
            後面的默默訂不到，於是策略只收得到一部分標的的行情，而它不會知道。
        - Parameters:
            - contracts: Sequence[Any]
                Shioaji 合約清單
            - with_bidask: bool
                是否一併訂閱委買賣（盤中策略需要 bid/ask 才算得出可成交價）
        - Raise:
            - ValueError
                訂閱總數超過單一連線上限
        """

        codes: Set[str] = {str(getattr(contract, "code", "")) for contract in contracts}
        if len(self.subscribed | codes) > self.MAX_SUBSCRIPTIONS:
            raise ValueError(
                f"訂閱數 {len(self.subscribed | codes)} 超過單一連線上限 "
                f"{self.MAX_SUBSCRIPTIONS}；請收斂標的池或改用多個連線"
            )

        quote_types: List[Any] = [sj_constant.QuoteType.Tick]
        if with_bidask:
            quote_types.append(sj_constant.QuoteType.BidAsk)

        for contract in contracts:
            for quote_type in quote_types:
                self.api.quote.subscribe(
                    contract,
                    quote_type=quote_type,
                    version=sj_constant.QuoteVersion.v1,
                )
        self.subscribed |= codes
        logger.info(f"已訂閱 {len(codes)} 檔（累計 {len(self.subscribed)} 檔）")

    def unsubscribe(self, contracts: Sequence[Any], with_bidask: bool = True) -> None:
        """取消訂閱"""

        quote_types: List[Any] = [sj_constant.QuoteType.Tick]
        if with_bidask:
            quote_types.append(sj_constant.QuoteType.BidAsk)

        for contract in contracts:
            for quote_type in quote_types:
                self.api.quote.unsubscribe(
                    contract,
                    quote_type=quote_type,
                    version=sj_constant.QuoteVersion.v1,
                )
            self.subscribed.discard(str(getattr(contract, "code", "")))

    # === 快照 ===
    def get_stock_snapshots(self, contracts: Sequence[Any]) -> List[StockQuote]:
        """
        - Description:
            取得股票快照，轉成 `StockQuote`
        - Parameters:
            - contracts: Sequence[Any]
                Shioaji 股票合約清單
        - Return:
            - List[StockQuote]
                報價清單；查無資料的標的不會出現在結果裡
        """

        return [self.to_stock_quote(snapshot) for snapshot in self._fetch(contracts)]

    def get_futures_snapshots(self, contracts: Sequence[Any]) -> List[FuturesQuote]:
        """
        - Description:
            取得期貨快照，轉成 `FuturesQuote`
        - Parameters:
            - contracts: Sequence[Any]
                Shioaji 期貨合約清單
        - Return:
            - List[FuturesQuote]
                報價清單
        """

        return [
            self.to_futures_quote(snapshot, contract)
            for snapshot, contract in self._fetch_with_contracts(contracts)
        ]

    def _fetch(self, contracts: Sequence[Any]) -> List[Any]:
        """分批呼叫 `snapshots()`；每批算一次 `MARKET_DATA` 額度"""

        snapshots: List[Any] = []
        for start in range(0, len(contracts), SNAPSHOT_BATCH_SIZE):
            batch: Sequence[Any] = contracts[start : start + SNAPSHOT_BATCH_SIZE]
            self.rate_limiter.acquire(RateLimitCategory.MARKET_DATA)
            snapshots.extend(self.api.snapshots(list(batch)) or [])
        return snapshots

    def _fetch_with_contracts(self, contracts: Sequence[Any]) -> List[tuple]:
        """
        取快照並配回對應的合約

        **以代號配對，不用位置**：查無資料的標的不會出現在回傳裡，
        用位置索引的話，停牌一檔就會讓後面所有標的的乘數集體錯位，
        而每一筆看起來都還是合法報價。
        """

        by_code: dict = {
            str(getattr(contract, "code", "")): contract for contract in contracts
        }
        paired: List[tuple] = []
        for snapshot in self._fetch(contracts):
            code: str = str(getattr(snapshot, "code", ""))
            paired.append((snapshot, by_code.get(code)))
        return paired

    # === 轉換 ===
    def to_stock_quote(self, snapshot: Any) -> StockQuote:
        """
        - Description:
            快照 → `StockQuote`（`scale=DAY`）

            `cur_price` 與 `close` 同值，都是**目前最新成交價**。
            盤中取到的 `close` 是暫定值，收盤前還會變——策略若拿它算「今天漲幅」，
            得到的是此刻的漲幅，不是收盤漲幅。
        - Parameters:
            - snapshot: Any
                Shioaji 的 `Snapshot`
        - Return:
            - StockQuote
                與回測同款的報價物件
        """

        close: float = float(getattr(snapshot, "close", 0.0) or 0.0)
        return StockQuote(
            stock_id=str(getattr(snapshot, "code", "")),
            scale=Scale.DAY,
            date=self._resolve_date(snapshot),
            cur_price=close,
            # **當日累計成交量**；`snapshot.volume` 是該筆成交量，取錯會讓
            # 「當日成交量 ≥ N 張」這類門檻永遠不成立
            volume=int(getattr(snapshot, "total_volume", 0) or 0),
            open=float(getattr(snapshot, "open", 0.0) or 0.0),
            high=float(getattr(snapshot, "high", 0.0) or 0.0),
            low=float(getattr(snapshot, "low", 0.0) or 0.0),
            close=close,
        )

    def to_futures_quote(
        self, snapshot: Any, contract: Optional[Any] = None
    ) -> FuturesQuote:
        """
        - Description:
            快照 → `FuturesQuote`

            `settlement_price` 與 `open_interest` 一律留 `None`：快照沒有這兩項，
            **填 0 會讓它們看起來像「今天是 0」而不是「沒有資料」**，
            而回測那邊夜盤的這兩欄本來就是 None。
        - Parameters:
            - snapshot: Any
                Shioaji 的 `Snapshot`
            - contract: Optional[Any]
                對應的合約；用來取到期月份與股期乘數
        - Return:
            - FuturesQuote
                與回測同款的報價物件
        """

        code: str = str(getattr(snapshot, "code", ""))
        product, expiry = self._split_code(code, contract)
        close: float = float(getattr(snapshot, "close", 0.0) or 0.0)
        quote_date: datetime.datetime = self._resolve_date(snapshot)

        return FuturesQuote(
            product=product,
            expiry=expiry,
            scale=Scale.DAY,
            date=quote_date,
            cur_price=close,
            volume=int(getattr(snapshot, "total_volume", 0) or 0),
            open=float(getattr(snapshot, "open", 0.0) or 0.0),
            high=float(getattr(snapshot, "high", 0.0) or 0.0),
            low=float(getattr(snapshot, "low", 0.0) or 0.0),
            close=close,
            session=self.resolve_session(quote_date),
            multiplier=self.resolve_multiplier(product, contract),
        )

    @staticmethod
    def resolve_session(moment: datetime.datetime) -> FuturesSession:
        """
        - Description:
            依時刻判定日盤或夜盤

            日盤 08:45~13:45、夜盤 15:00~次日 05:00。**兩段之間的空檔歸日盤**：
            那段時間沒有行情，判成哪一邊都不影響報價，但歸夜盤會讓
            13:45 收盤後的快照被記成次一交易日的帳。
        - Parameters:
            - moment: datetime.datetime
                報價時刻
        - Return:
            - FuturesSession
                交易時段
        """

        hour: int = moment.hour
        if hour >= 15 or hour < 5:
            return FuturesSession.NIGHT
        return FuturesSession.DAY

    @staticmethod
    def resolve_multiplier(product: str, contract: Optional[Any] = None) -> int:
        """
        - Description:
            取契約乘數

            指數期貨查 `FUTURES_MULTIPLIER`；查不到時**改用合約的 `multiplier`／`unit`**
            （股票期貨的乘數會隨除權息調整，寫死必錯）。兩邊都沒有就回 0——
            呼叫端會在算 PnL 時發現，比默默用一個猜的乘數好。
        - Parameters:
            - product: str
                商品代碼
            - contract: Optional[Any]
                對應的合約
        - Return:
            - int
                契約乘數；取不到時為 0
        """

        if product in FUTURES_MULTIPLIER:
            return int(FUTURES_MULTIPLIER[product])

        for field in ("multiplier", "unit"):
            value: Any = getattr(contract, field, None)
            if value:
                return int(value)

        logger.warning(f"取不到 {product} 的契約乘數，PnL 將無法計算")
        return 0

    # === 工具 ===
    def _resolve_date(self, snapshot: Any) -> datetime.datetime:
        """
        快照時戳 → 台北時區的 aware datetime

        Shioaji 的 `Snapshot.ts` 是 **epoch 奈秒**。當成秒來解會得到 1970 年，
        而那個日期會一路寫進部位與報表。
        """

        raw: Any = getattr(snapshot, "ts", None)
        if raw:
            try:
                return datetime.datetime.fromtimestamp(
                    float(raw) / 1e9, tz=get_live_timezone()
                )
            except (TypeError, ValueError, OSError):
                logger.warning(f"無法解析快照時戳：{raw!r}")

        if self._now is not None:
            return self._now()
        return datetime.datetime.now(tz=get_live_timezone())

    @staticmethod
    def _split_code(code: str, contract: Optional[Any]) -> tuple:
        """
        把合約代號拆成商品與到期月份

        優先用合約自己的 `symbol`（格式是 `{分類}{YYYYMM}`）；快照的 `code`
        是另一組代碼（`TXFA6`），**跨年會重複**，拆不出可靠的月份。
        """

        symbol: str = str(getattr(contract, "symbol", "") or code)
        if len(symbol) > 6 and symbol[-6:].isdigit():
            return (symbol[:-6], symbol[-6:])

        delivery: Any = getattr(contract, "delivery_month", None)
        return (symbol, str(delivery) if delivery else "")

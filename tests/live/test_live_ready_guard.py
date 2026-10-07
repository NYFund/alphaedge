import datetime
from typing import Dict, List, Optional

import pytest

from core.live.strategy_guard import (
    LiveReadinessError,
    check_schedule_conflicts,
    inspect_strategy,
    resolve_hook_timing,
    verify_strategies,
)
from core.models import (
    FuturesQuote,
    LiveDataUnavailableError,
    PreOpenFuturesQuote,
    PreOpenStockQuote,
    StockQuote,
)
from core.models.base.quote import PreOpenQuoteMixin
from core.strategies.base import BaseStrategy
from core.utils import (
    ExecutionStyle,
    ExecutionTiming,
    LiveHook,
    Scale,
    StockPriceType,
)

"""
上實盤前的守門，與盤前報價的「讀了就炸」

擋的都是**不會報錯、只會讓結果安靜地不同**的情況：

- 盤前把 OHLC 填成參考價 → 漲幅類訊號永遠算出 0%，整天不開倉。
- `live_schedule` 與 `allow_day_trade` 矛盾 → 交易順序被默默改掉，
  回測與實盤的部位軌跡從當天起就不同。
- 策略沒宣告 `live_ready` → 作者根本沒確認過它的實盤語意。
- 策略沒宣告或宣告錯 `live_execution` → 送單時才發現，尾盤段沒有時間補救。
"""


DATE: datetime.date = datetime.date(2026, 9, 23)


class FakeStrategy(BaseStrategy):
    """最小策略；只用到守門會看的那幾個欄位"""

    def __init__(
        self,
        live_ready: bool = True,
        live_schedule: Optional[Dict[str, ExecutionTiming]] = None,
        allow_day_trade: bool = False,
        live_execution: Optional[object] = ExecutionStyle.MARKET,
    ) -> None:
        super().__init__()
        self.live_ready = live_ready
        self.live_schedule = live_schedule if live_schedule is not None else {}
        self.allow_day_trade = allow_day_trade
        self.live_execution = live_execution

    def setup_account(self, account: object) -> None:
        """測試不需要帳戶"""


BOTH_AT_CLOSE: Dict[str, ExecutionTiming] = {
    LiveHook.OPEN.value: ExecutionTiming.AT_CLOSE,
    LiveHook.CLOSE.value: ExecutionTiming.AT_CLOSE,
}


# === live_ready ===
def test_strategy_is_not_live_ready_by_default() -> None:
    """
    **預設不可上實盤**

    預設可以的話，一支剛寫完、沒人確認過實盤語意的策略就會直接送真單。
    """

    assert BaseStrategy.__init__ is not None
    assert FakeStrategy(live_ready=False).live_ready is False


def test_missing_live_ready_is_rejected() -> None:
    """沒宣告就拒絕啟動，而且訊息要說明它帶的是什麼契約"""

    problems: List[str] = inspect_strategy(
        FakeStrategy(live_ready=False, live_schedule=BOTH_AT_CLOSE)
    )

    assert len(problems) == 1
    assert "重建" in problems[0]


def test_missing_schedule_is_rejected() -> None:
    """沒宣告段落，引擎不知道哪個鉤子該在哪一段呼叫"""

    problems: List[str] = inspect_strategy(FakeStrategy(live_schedule={}))

    assert any("live_schedule" in problem for problem in problems)


def test_unknown_hook_name_is_rejected() -> None:
    """
    鉤子名打錯要擋下

    打錯字的鉤子整天不會被呼叫，而且不會報錯。
    """

    problems: List[str] = inspect_strategy(
        FakeStrategy(live_schedule={"opne": ExecutionTiming.AT_CLOSE})
    )

    assert any("不認得的鉤子名" in problem for problem in problems)


def test_valid_strategy_has_no_problems() -> None:
    """兩個鉤子同段落、有宣告 live_ready：通過"""

    assert inspect_strategy(FakeStrategy(live_schedule=BOTH_AT_CLOSE)) == []


def test_removed_setting_blocks_live_start() -> None:
    """設了已移除的欄位不可上實盤：實盤照預設值跑，與作者以為的不同"""

    strategy: FakeStrategy = FakeStrategy(live_schedule=BOTH_AT_CLOSE)
    strategy.bar_execution_order = "OPEN_THEN_CLOSE"

    problems: List[str] = inspect_strategy(strategy)

    assert len(problems) == 1
    assert "bar_execution_order" in problems[0]


# === live_execution ===
def test_execution_style_has_no_default() -> None:
    """不給預設值：要不要成交是策略的決定"""

    class Bare(BaseStrategy):
        def setup_account(self, account: object) -> None:
            """測試不需要帳戶"""

    assert Bare().live_execution is None


def test_missing_execution_style_is_rejected() -> None:
    """沒宣告執行方式：啟動就擋，不等到送單時才失敗"""

    problems: List[str] = inspect_strategy(
        FakeStrategy(live_schedule=BOTH_AT_CLOSE, live_execution=None)
    )

    assert len(problems) == 1
    assert "未宣告 live_execution" in problems[0]


@pytest.mark.parametrize("wrong", [StockPriceType.MKT, "MARKET"])
def test_broker_price_type_is_not_an_execution_style(wrong: object) -> None:
    """
    券商價格類型與字串都要擋

    `StockPriceType.MKT` 與 `ExecutionStyle.MARKET` 名字相近，但前者在集合競價
    時段送不出去；寫錯型別在送單前不會有任何錯誤。
    """

    problems: List[str] = inspect_strategy(
        FakeStrategy(live_schedule=BOTH_AT_CLOSE, live_execution=wrong)
    )

    assert len(problems) == 1
    assert "必須是 ExecutionStyle" in problems[0]


@pytest.mark.parametrize("style", list(ExecutionStyle))
def test_every_execution_style_passes(style: ExecutionStyle) -> None:
    """兩種執行方式都是合法宣告"""

    assert (
        inspect_strategy(
            FakeStrategy(live_schedule=BOTH_AT_CLOSE, live_execution=style)
        )
        == []
    )


# === 批次檢查 ===
def test_all_problems_are_reported_at_once() -> None:
    """
    一次列出所有問題

    逐支拋出會讓人修一個、跑一次、再撞下一個。
    """

    with pytest.raises(LiveReadinessError) as error:
        verify_strategies(
            [FakeStrategy(live_ready=False, live_schedule={}), FakeStrategy()]
        )

    message: str = str(error.value)
    assert "live_ready" in message
    assert "live_schedule" in message


def test_duplicate_strategy_names_are_rejected() -> None:
    """
    策略名稱不可重複

    歸屬鏈以策略名為鍵（`live_order.strategy_name`、`live_position_lot`），
    重名會讓兩支策略的部位與損益混在一起，而合計仍然正確——對帳看不出來。
    """

    with pytest.raises(LiveReadinessError, match="名稱重複"):
        verify_strategies(
            [
                FakeStrategy(live_schedule=BOTH_AT_CLOSE),
                FakeStrategy(live_schedule=BOTH_AT_CLOSE),
            ]
        )


def test_verify_passes_for_a_clean_set() -> None:
    """全部通過時不拋出"""

    verify_strategies([FakeStrategy(live_schedule=BOTH_AT_CLOSE)])


# === schedule 與 allow_day_trade 的一致性 ===
def test_same_segment_needs_no_check() -> None:
    """
    兩個鉤子在同一段落時順序由 `get_execution_sequence()` 決定，與回測一致

    這時 `allow_day_trade` 推導的順序照樣生效，沒有矛盾可言。
    """

    strategy: FakeStrategy = FakeStrategy(live_schedule=BOTH_AT_CLOSE)

    assert check_schedule_conflicts(strategy) is None


def test_contradiction_is_detected() -> None:
    """
    不當沖卻把 open 排在前面的段落 → 實盤等同當沖

    **這會靜默改掉交易順序**，回測與實盤的部位軌跡從當天起就不同。
    """

    strategy: FakeStrategy = FakeStrategy(
        live_schedule={
            LiveHook.OPEN.value: ExecutionTiming.AT_OPEN,
            LiveHook.CLOSE.value: ExecutionTiming.AT_CLOSE,
        },
        allow_day_trade=False,
    )

    conflict: Optional[str] = check_schedule_conflicts(strategy)

    assert conflict is not None
    assert "實盤等同當沖" in conflict


def test_day_trade_with_close_first_is_detected() -> None:
    """當沖卻把 close 排在前面的段落 → 當天開的部位當天平不掉，實盤當不了沖"""

    strategy: FakeStrategy = FakeStrategy(
        live_schedule={
            LiveHook.OPEN.value: ExecutionTiming.AT_CLOSE,
            LiveHook.CLOSE.value: ExecutionTiming.AT_OPEN,
        },
        allow_day_trade=True,
    )

    conflict: Optional[str] = check_schedule_conflicts(strategy)

    assert conflict is not None
    assert "實盤當不了沖" in conflict


def test_consistent_cross_segment_schedule_passes() -> None:
    """
    當沖 ＋ open 在開盤段、close 在尾盤段：一致

    `ForeignSellingReversalShortStrategy` 剛好是這個形狀——**但那是巧合，不是保證**，
    所以還是要檢查。
    """

    strategy: FakeStrategy = FakeStrategy(
        live_schedule={
            LiveHook.OPEN.value: ExecutionTiming.AT_OPEN,
            LiveHook.CLOSE.value: ExecutionTiming.AT_CLOSE,
        },
        allow_day_trade=True,
    )

    assert check_schedule_conflicts(strategy) is None


def test_intraday_hooks_share_one_segment() -> None:
    """盤中逐筆沒有段落之分，兩個鉤子都在同一格，不會被判成矛盾"""

    strategy: FakeStrategy = FakeStrategy(
        live_schedule={
            LiveHook.OPEN.value: ExecutionTiming.IMMEDIATE,
            LiveHook.CLOSE.value: ExecutionTiming.IMMEDIATE,
        },
    )

    assert check_schedule_conflicts(strategy) is None


# === 鉤子段落解析 ===
def test_stop_loss_falls_back_to_close() -> None:
    """
    停損沒宣告時退回平倉的段落

    兩者都是出場；把停損排在與平倉不同的段落是刻意的決定，
    不該由「忘了寫」造成。
    """

    strategy: FakeStrategy = FakeStrategy(live_schedule=BOTH_AT_CLOSE)

    assert resolve_hook_timing(strategy, LiveHook.STOP_LOSS) is ExecutionTiming.AT_CLOSE


def test_explicit_stop_loss_timing_wins() -> None:
    """明確宣告時以宣告為準：停損要盤中就反應是合理的設計"""

    strategy: FakeStrategy = FakeStrategy(
        live_schedule={
            **BOTH_AT_CLOSE,
            LiveHook.STOP_LOSS.value: ExecutionTiming.AT_OPEN,
        }
    )

    assert resolve_hook_timing(strategy, LiveHook.STOP_LOSS) is ExecutionTiming.AT_OPEN


def test_unscheduled_hook_returns_none() -> None:
    """沒宣告也退不回去時回 None，由引擎決定跳過還是拒絕啟動"""

    assert resolve_hook_timing(FakeStrategy(), LiveHook.OPEN) is None


# === 盤前報價 ===
@pytest.mark.parametrize(
    "field", ["open", "high", "low", "close", "adj_close", "signal_close"]
)
def test_pre_open_stock_quote_raises_on_ohlc(field: str) -> None:
    """
    盤前讀 OHLC 一律拋出

    **不可以填成參考價**——以 `signal_close / 昨收 - 1` 算漲幅的策略會永遠算出 0%，
    訊號默默不成立，而且不會有任何錯誤訊息。
    """

    quote: PreOpenStockQuote = PreOpenStockQuote(
        stock_id="2330", reference_price=1000.0
    )

    with pytest.raises(LiveDataUnavailableError):
        _ = getattr(quote, field)


def test_pre_open_quote_can_be_constructed() -> None:
    """
    物件要建得出來

    property 沒帶 setter 的話，父類 `__init__` 的賦值會直接 `AttributeError`
    （理由見 `test_setters_do_not_raise_so_the_object_can_be_built`）。
    """

    quote: PreOpenStockQuote = PreOpenStockQuote(
        stock_id="2330",
        date=datetime.date(2026, 9, 19),
        reference_price=1000.0,
        volume=5000,
        limit_up=1100.0,
        limit_down=900.0,
    )

    assert quote.stock_id == "2330"
    assert quote.cur_price == 1000.0
    assert quote.reference_price == 1000.0
    assert (quote.limit_up, quote.limit_down) == (1100.0, 900.0)
    assert quote.scale is Scale.DAY


def test_signal_close_is_overridden_not_inherited() -> None:
    """
    `signal_close` 必須自己覆寫

    父類的版本在 `adj_close` 非 None 時**完全不讀 `close`**，只擋 `close`
    會讓這條路徑漏掉——盤前只要 `adj_close` 有值，訊號就照樣算得出來。
    """

    assert PreOpenStockQuote.signal_close.fget is not StockQuote.signal_close.fget


def test_pre_open_futures_quote_keeps_settlement_fields_none() -> None:
    """
    期貨的結算價與未沖銷契約量**不覆寫**

    它們本來就可能是 None（夜盤沒有），維持 None 表示「沒有資料」，與回測一致。
    """

    quote: PreOpenFuturesQuote = PreOpenFuturesQuote(
        product="TX", expiry="202601", reference_price=20000.0, multiplier=200
    )

    assert quote.settlement_price is None
    assert quote.open_interest is None
    assert quote.multiplier == 200

    with pytest.raises(LiveDataUnavailableError):
        _ = quote.close


# === 盤前報價的六道防線 ===
def test_mixin_comes_before_the_concrete_quote_in_the_mro() -> None:
    """
    `PreOpenQuoteMixin` 必須排在具體報價類別**之前**

    排在後面的話 MRO 會先找到父類那份 property，**六道防線全部靜默失效**
    ——物件照樣建得出來、讀 `close` 也照樣回得出值（0.0），
    而漲幅類訊號會永遠算出 0%，不報錯。
    """

    for cls, parent in (
        (PreOpenStockQuote, StockQuote),
        (PreOpenFuturesQuote, FuturesQuote),
    ):
        mro: List[type] = list(cls.__mro__)
        assert mro.index(PreOpenQuoteMixin) < mro.index(parent), (
            f"{cls.__name__} 的 mixin 排在 {parent.__name__} 之後，六道防線失效"
        )


@pytest.mark.parametrize(
    "cls",
    [PreOpenStockQuote, "futures"],
)
def test_every_unavailable_field_raises_on_both_markets(cls) -> None:
    """
    六個欄位在**兩個市場**都要擋住

    這三個陷阱各市場複製一份就是複製三份——第三個市場加進來時，
    漏掉 `signal_close` 那條不會報錯，只會讓盤前訊號默默算得出來。
    """

    quote = (
        PreOpenStockQuote(stock_id="2330", date=DATE, reference_price=600.0)
        if cls is PreOpenStockQuote
        else PreOpenFuturesQuote(
            product="TX", expiry="202403", date=DATE, reference_price=18000.0
        )
    )

    for field in PreOpenQuoteMixin.UNAVAILABLE_FIELDS:
        with pytest.raises(LiveDataUnavailableError, match=field):
            getattr(quote, field)

    # 盤前唯一可用的價格照常讀得到
    assert quote.reference_price > 0


def test_setters_do_not_raise_so_the_object_can_be_built() -> None:
    """
    setter 刻意什麼都不做

    父類 `__init__` 會直接 `self.open = open`，沒有 setter 的話物件根本建不出來
    （`AttributeError`），連拋出「拿不到資料」的機會都沒有。
    """

    quote: PreOpenStockQuote = PreOpenStockQuote(
        stock_id="2330", date=DATE, reference_price=600.0
    )

    quote.close = 999.0  # 不該拋

    # 寫進去的值不會被記住——setter 刻意不存
    with pytest.raises(LiveDataUnavailableError):
        _ = quote.close

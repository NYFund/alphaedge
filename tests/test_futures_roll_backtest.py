import datetime
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional

import pytest

from core.backtest.models.cost_model import TwFuturesCostModel
from core.backtest.models.settlement_model import TwFuturesSettlementModel
from core.config import FUTURES_CONTINUOUS_TABLE_NAME, TW_FUTURES_DB_PATH
from core.managers.futures.position_manager import FuturesPositionManager
from core.market.tw.futures_calendar import FuturesCalendar
from core.market.tw.futures_margin_config import FuturesMarginConfig
from core.market.tw.futures_roll import FuturesRollConfig
from core.models import FuturesAccount, FuturesOrder, FuturesQuote
from core.models.cost_config import FuturesCostConfig
from core.utils import Action, FuturesRollRule, PositionType
from core.utils.constant import FUTURES_MULTIPLIER
from tests.conftest import build_futures_quote

"""
回測層的換月測試

**換月是市場結構強加的，不是策略訊號**：契約會到期，部位不轉倉就會憑空消失。
但「什麼時候轉」是政策，故做成可切換的三種規則，且**策略挑合約與結算模型轉倉
共用同一份設定物件**——兩處不一致會出現「訊號在次月、部位還在近月」這種
不會報錯的錯配。

本檔釘住四件事：

1. 三種規則可切換，且策略與結算模型看到的是同一個規則。
2. 轉倉 ＝ 平舊倉 ＋ 以**相同口數與方向**開新倉，展期價差如實入帳。
3. 新契約當日無報價時**不轉倉**（寧可留著走到期出場，也不要開一張沒有報價的單）。
4. 關掉自動轉倉時，部位不會被偷偷轉走。
"""

MULTIPLIER: int = FUTURES_MULTIPLIER["TX"]
DAY_1: datetime.date = datetime.date(2024, 3, 20)  # 202403 的最後交易日
DAY_2: datetime.date = datetime.date(2024, 3, 21)

TRADING_DAYS: List[datetime.date] = [
    datetime.date(2024, 3, 18),
    datetime.date(2024, 3, 19),
    DAY_1,
    DAY_2,
    datetime.date(2024, 3, 22),
]


def make_quote(
    expiry: str,
    close: float,
    date: datetime.date = DAY_2,
    open_interest: Optional[int] = 100,
) -> FuturesQuote:
    """組一筆 TX 報價"""

    return build_futures_quote(
        expiry=expiry,
        date=date,
        close=close,
        settlement_price=close,
        open_interest=open_interest,
        multiplier=MULTIPLIER,
    )


def make_settlement(
    roll_config: Optional[FuturesRollConfig] = None,
) -> TwFuturesSettlementModel:
    """建立零成本、比率保證金的結算模型（本檔驗換月，不驗費用）"""

    manager: FuturesPositionManager = FuturesPositionManager(
        FuturesAccount(init_capital=10_000_000),
        cost_model=TwFuturesCostModel(FuturesCostConfig.free()),
        margin_config=FuturesMarginConfig.ratio(),
    )
    config: FuturesRollConfig = roll_config or FuturesRollConfig()
    config.calendar = FuturesCalendar(TRADING_DAYS)
    return TwFuturesSettlementModel(manager, roll_config=config)


def open_position(
    settlement: TwFuturesSettlementModel,
    expiry: str = "202403",
    price: float = 20000.0,
    volume: int = 2,
    position_type: PositionType = PositionType.LONG,
) -> None:
    """在指定契約開倉"""

    settlement.position_manager.open_position(
        FuturesOrder(
            product="TX",
            expiry=expiry,
            date=DAY_1,
            action=Action.BUY if position_type == PositionType.LONG else Action.SELL,
            position_type=position_type,
            price=price,
            volume=volume,
        )
    )


# === 轉倉行為 ===
def test_position_is_rolled_to_the_next_contract() -> None:
    """
    契約到期後部位轉到次月，**口數與方向不變**

    不轉倉的話部位會因為沒有報價而卡住，最後只能走到期權宜出場——
    那等於在結算日當天把曝險憑空關掉。
    """

    settlement: TwFuturesSettlementModel = make_settlement()
    open_position(settlement)
    account: FuturesAccount = settlement.position_manager.account

    settlement.on_bar_close(DAY_2, [make_quote("202404", 20100.0)], account, {})

    positions = account.get_positions()
    assert len(positions) == 1
    assert positions[0].expiry == "202404"
    assert positions[0].volume == 2
    assert positions[0].position_type == PositionType.LONG


def test_roll_keeps_short_direction() -> None:
    """空單轉倉後仍是空單——方向反掉會讓部位在轉倉當下由避險變成加倍曝險"""

    settlement: TwFuturesSettlementModel = make_settlement()
    open_position(settlement, position_type=PositionType.SHORT)
    account: FuturesAccount = settlement.position_manager.account

    settlement.on_bar_close(DAY_2, [make_quote("202404", 20100.0)], account, {})

    assert account.get_positions()[0].position_type == PositionType.SHORT


def test_roll_spread_is_paid_for_real() -> None:
    """
    **展期價差如實入帳**

    舊契約以盯市價平倉、新契約以當日收盤價開倉，兩者的差就是轉倉成本。
    連續合約把它調整掉是為了畫圖與算指標，回測不該把這筆錢變不見。
    """

    settlement: TwFuturesSettlementModel = make_settlement()
    open_position(settlement, price=20000.0, volume=1)
    account: FuturesAccount = settlement.position_manager.account

    # 舊契約當日結算 20,050（＋50 點入帳），新契約以 20,120 開倉
    settlement.on_bar_close(
        DAY_2,
        [make_quote("202403", 20050.0), make_quote("202404", 20120.0)],
        account,
        {},
    )

    record = account.trade_records[-1]
    assert record.realized_pnl == (20050 - 20000) * MULTIPLIER
    assert account.get_positions()[0].price == 20120.0


def test_roll_is_counted_as_an_event() -> None:
    """轉倉要進事件計數，否則報表看不出「這段期間換了幾次月」"""

    settlement: TwFuturesSettlementModel = make_settlement()
    open_position(settlement)
    event_counts: Dict[str, int] = {}

    settlement.on_bar_close(
        DAY_2,
        [make_quote("202404", 20100.0)],
        settlement.position_manager.account,
        event_counts,
    )

    assert event_counts["rolled_contract"] == 1


def test_no_roll_without_a_quote_for_the_new_contract() -> None:
    """
    新契約當日無報價時不轉倉

    開一張沒有報價的單等於憑空指定成交價；留著讓它走到期出場才是誠實的。
    """

    settlement: TwFuturesSettlementModel = make_settlement()
    open_position(settlement)
    account: FuturesAccount = settlement.position_manager.account

    settlement.on_bar_close(DAY_2, [], account, {})

    assert account.get_positions()[0].expiry == "202403"


def test_roll_can_be_disabled() -> None:
    """關掉自動轉倉時部位不會被偷偷轉走（此時它會走到期權宜出場）"""

    settlement: TwFuturesSettlementModel = make_settlement(
        FuturesRollConfig(enabled=False)
    )
    open_position(settlement)
    account: FuturesAccount = settlement.position_manager.account

    settlement.on_bar_close(DAY_2, [make_quote("202404", 20100.0)], account, {})

    assert account.get_positions()[0].expiry == "202403"


# === 三種規則可切換 ===
@pytest.mark.parametrize(
    "rule, expected",
    [
        (FuturesRollRule.LAST_TRADING_DAY, "202403"),  # 最後交易日當天仍持近月
        (FuturesRollRule.DAYS_BEFORE_EXPIRY, "202404"),  # 提前 1 個交易日就換
    ],
)
def test_rules_change_the_roll_timing(rule: FuturesRollRule, expected: str) -> None:
    """同一天、同一組報價，換月規則不同就會得到不同的當家契約"""

    settlement: TwFuturesSettlementModel = make_settlement(FuturesRollConfig(rule=rule))
    open_position(settlement)
    account: FuturesAccount = settlement.position_manager.account

    settlement.on_bar_close(
        DAY_1,
        [
            make_quote("202403", 20000.0, date=DAY_1),
            make_quote("202404", 20100.0, date=DAY_1),
        ],
        account,
        {},
    )

    assert account.get_positions()[0].expiry == expected


def test_open_interest_rule_rolls_on_crossover() -> None:
    """未沖銷量交叉規則：次月的未沖銷量超過近月就換，與日期無關"""

    settlement: TwFuturesSettlementModel = make_settlement(
        FuturesRollConfig(rule=FuturesRollRule.OPEN_INTEREST)
    )
    open_position(settlement)
    account: FuturesAccount = settlement.position_manager.account

    settlement.on_bar_close(
        datetime.date(2024, 3, 18),
        [
            make_quote("202403", 20000.0, date=DAY_1, open_interest=100),
            make_quote("202404", 20100.0, date=DAY_1, open_interest=900),
        ],
        account,
        {},
    )

    assert account.get_positions()[0].expiry == "202404"


# === 策略與結算模型共用同一份規則 ===
def test_strategy_and_settlement_share_the_roll_config() -> None:
    """
    factory 建好的換月設定要同時交給策略、結算模型與 DataFeed

    三邊各拿一份的話，訊號會在次月產生、部位卻還留在近月，而且不會報錯。
    """

    from core.backtest.backtester import Backtester
    from core.backtest.factory import build_backtester
    from core.strategies.futures.momentum_futures_strategy import (
        MomentumFuturesStrategy,
    )

    strategy: MomentumFuturesStrategy = MomentumFuturesStrategy()

    original_setup = Backtester.setup
    Backtester.setup = lambda self: None
    try:
        backtester: Backtester = build_backtester(strategy)
    finally:
        Backtester.setup = original_setup

    config: FuturesRollConfig = backtester.settlement.roll_config

    assert strategy.roll_config is config
    assert backtester.data_feed.roll_config is config


def test_strategy_picks_the_contract_by_the_same_rule() -> None:
    """策略挑合約走的是同一個 `FuturesRollPlanner`，不是永遠取最近月"""

    from core.strategies.futures.momentum_futures_strategy import (
        MomentumFuturesStrategy,
    )

    strategy: MomentumFuturesStrategy = MomentumFuturesStrategy()
    strategy.roll_config = FuturesRollConfig(
        rule=FuturesRollRule.DAYS_BEFORE_EXPIRY, calendar=FuturesCalendar(TRADING_DAYS)
    )

    quotes: List[FuturesQuote] = [
        make_quote("202403", 20000.0, date=DAY_1),
        make_quote("202404", 20100.0, date=DAY_1),
    ]

    # 最後交易日前 1 個交易日就換月 → 挑次月
    assert strategy.select_near_month(quotes, "TX").expiry == "202404"


def test_without_calendar_falls_back_to_nearest_month() -> None:
    """尚未注入日曆時退回「取最近到期月」，即最單純的近月政策"""

    from core.strategies.futures.momentum_futures_strategy import (
        MomentumFuturesStrategy,
    )

    strategy: MomentumFuturesStrategy = MomentumFuturesStrategy()
    quotes: List[FuturesQuote] = [
        make_quote("202404", 20100.0),
        make_quote("202403", 20000.0),
    ]

    assert strategy.select_near_month(quotes, "TX").expiry == "202403"


# === 與連續合約表對齊 ===
@pytest.mark.slow
@pytest.mark.skipif(
    not Path(TW_FUTURES_DB_PATH).exists(), reason="需要 tw_futures.db 才能比對換月接點"
)
def test_backtest_roll_dates_match_the_continuous_table() -> None:
    """
    **回測的換月接點必須與 `futures_continuous` 的 `roll_flag` 一致**
    （本步驟的驗收條件）

    連續合約由 pipeline 建、回測由結算模型轉倉，兩者若對不上，
    用連續合約算出來的訊號就會落在部位不存在的契約上。
    以 2024 全年、`LAST_TRADING_DAY` 規則實跑一支「開倉後永不平倉」的策略比對。
    """

    from core.api.tw.futures_price_api import FuturesPriceAPI
    from core.backtest.datafeed.tw.futures_datafeed import TwFuturesDataFeed

    conn: sqlite3.Connection = sqlite3.connect(TW_FUTURES_DB_PATH)
    try:
        table_rolls: List[str] = [
            row[0]
            for row in conn.execute(
                f"SELECT date FROM {FUTURES_CONTINUOUS_TABLE_NAME} "
                f"WHERE product = 'TX' AND method = 'NONE' "
                f"AND roll_rule = 'LAST_TRADING_DAY' AND roll_flag = 1 "
                f"AND date BETWEEN '2024-01-01' AND '2024-12-31' ORDER BY date"
            )
        ]
    finally:
        conn.close()

    if not table_rolls:
        pytest.skip("連續合約表尚未建立（`--target futures_continuous`）")

    # 用回測的那一條路徑（DataFeed → 結算模型）逐日轉倉
    feed: TwFuturesDataFeed = TwFuturesDataFeed()
    api: FuturesPriceAPI = FuturesPriceAPI()
    try:
        calendar: FuturesCalendar = FuturesCalendar.from_api(
            api, datetime.date(2024, 1, 1), datetime.date(2025, 1, 31), product="TX"
        )
        settlement: TwFuturesSettlementModel = make_settlement()
        settlement.roll_config.calendar = calendar
        account: FuturesAccount = settlement.position_manager.account

        trading_days: List[datetime.date] = calendar.get_trading_days(
            datetime.date(2024, 1, 1), datetime.date(2024, 12, 31)
        )
        from core.adapters.tw.futures_quote_adapter import FuturesQuoteAdapter

        backtest_rolls: List[str] = []
        for index, date in enumerate(trading_days):
            quotes = FuturesQuoteAdapter.from_day_rows(
                api.get(date, product="TX"), date
            )
            quotes = [quote for quote in quotes if "W" not in quote.expiry]
            if not quotes:
                continue

            if index == 0:
                near = min(quotes, key=lambda quote: quote.expiry)
                settlement.position_manager.open_position(
                    FuturesOrder(
                        product="TX",
                        expiry=near.expiry,
                        date=date,
                        action=Action.BUY,
                        position_type=PositionType.LONG,
                        price=near.close,
                        volume=1,
                    )
                )
                continue

            event_counts: Dict[str, int] = {}
            settlement.on_bar_close(date, quotes, account, event_counts)
            if event_counts.get("rolled_contract"):
                backtest_rolls.append(str(date))
    finally:
        api.close()
        feed.close()

    assert backtest_rolls == table_rolls


# === 換月不可回頭===
def test_roll_never_goes_back_to_a_nearer_month() -> None:
    """
    `OPEN_INTEREST` 規則下，未沖銷量反轉不可讓部位換回近月

    未沖銷量逐日波動，換到次月之後近月可能又反超一天。每來回一次就付兩次
    手續費與一次展期價差，而且是憑空產生的——換月是單向的。
    """

    # 用 202403 最後交易日**之前**的日期：那之後近月已到期，規則本來就不會回頭
    before_expiry: datetime.date = datetime.date(2024, 3, 19)

    settlement: TwFuturesSettlementModel = make_settlement(
        FuturesRollConfig(rule=FuturesRollRule.OPEN_INTEREST)
    )
    # 前一天未沖銷量偏向次月，部位已經換到 202404
    open_position(settlement, expiry="202404")
    account: FuturesAccount = settlement.position_manager.account

    # 今天近月的未沖銷量又反超：舊版會把部位換回 202403
    settlement.on_bar_close(
        before_expiry,
        [
            make_quote("202403", 20000.0, date=before_expiry, open_interest=900),
            make_quote("202404", 20100.0, date=before_expiry, open_interest=100),
        ],
        account,
        {},
    )

    positions = account.get_positions()
    assert len(positions) == 1
    assert positions[0].expiry == "202404", "換月是單向的，不可換回更近的月份"


def test_roll_uses_the_same_price_kind_for_both_legs() -> None:
    """
    轉倉兩腿都用盯市價（結算價優先），不可一腿結算價、一腿收盤價

    兩者在期貨是不同的數字，混用會讓帳上多出一筆不存在的展期價差
    ——而展期價差正是這裡唯一該記錄的東西。
    """

    from core.backtest.models.settlement_model import TwFuturesSettlementModel

    class _Quote:
        settlement_price = 18050.0
        close = 18000.0
        cur_price = 17990.0

    assert TwFuturesSettlementModel.get_quote_mark_price(_Quote()) == 18050.0


def test_quote_mark_price_falls_back_to_close() -> None:
    """夜盤沒有結算價（來源即為 NULL），退回收盤價"""

    from core.backtest.models.settlement_model import TwFuturesSettlementModel

    class _NightQuote:
        settlement_price = None
        close = 18000.0
        cur_price = 17990.0

    assert TwFuturesSettlementModel.get_quote_mark_price(_NightQuote()) == 18000.0


def test_quote_mark_price_is_zero_when_nothing_available() -> None:
    """三種價格都沒有時回 0，由呼叫端決定退回最近一次結算價"""

    from core.backtest.models.settlement_model import TwFuturesSettlementModel

    class _EmptyQuote:
        settlement_price = None
        close = None
        cur_price = None

    assert TwFuturesSettlementModel.get_quote_mark_price(_EmptyQuote()) == 0.0


# === 近月拼接不可挑到週契約===
def test_near_month_series_excludes_weekly_contracts() -> None:
    """
    `202401W5` 在字典序上小於 `202402`，會贏過二月的月契約

    一月月契約到期之後，近月序列會黏在快到期的週契約上。
    """

    import pandas as pd

    from core.market.tw.futures_roll import FuturesRollPlanner

    expiries: pd.Series = pd.Series(["202401", "202401W5", "202402"])
    monthly: pd.Series = expiries[
        expiries.str.match(FuturesRollPlanner.MONTHLY_EXPIRY_PATTERN)
    ]

    assert monthly.tolist() == ["202401", "202402"]


# === 期貨損益只有一條公式===
def test_manager_pnl_delegates_to_the_cost_model() -> None:
    """
    `calculate_pnl()` 與 `FuturesCostModel.realized_pnl()` 必須是同一條公式

    兩邊各寫一份時，哪天有人改了乘數或方向的處理，另一邊不會跟著改，
    也不會有測試失敗。
    """

    from core.backtest.models.cost_model import TwFuturesCostModel
    from core.managers.futures.position_manager import FuturesPositionManager
    from core.models import FuturesAccount
    from core.utils import PositionType

    cost_model: TwFuturesCostModel = TwFuturesCostModel()
    manager: FuturesPositionManager = FuturesPositionManager(
        FuturesAccount(1000000.0), cost_model
    )

    kwargs = dict(
        entry_price=18000.0,
        exit_price=18100.0,
        volume=2,
        multiplier=200,
        position_type=PositionType.SHORT,
    )

    assert manager.calculate_pnl(
        position_type=kwargs["position_type"],
        entry_price=kwargs["entry_price"],
        exit_price=kwargs["exit_price"],
        volume=kwargs["volume"],
        multiplier=kwargs["multiplier"],
    ) == cost_model.realized_pnl(**kwargs, transaction_cost=0.0)

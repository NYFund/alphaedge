import datetime
from typing import Callable, Dict, List, Optional

import pandas as pd
import pytest

from core.backtest.backtester import Backtester, new_event_counts
from core.backtest.datafeed.tw.futures_datafeed import TwFuturesDataFeed
from core.backtest.factory import build_backtester
from core.backtest.models.cost_model import FuturesCostConfig, TwFuturesCostModel
from core.backtest.models.fill_model import (
    FillConfig,
    TwFuturesFillModel,
    VolumeCapPolicy,
)
from core.backtest.models.instrument_spec import TwFuturesSpec
from core.backtest.models.settlement_model import (
    BaseSettlementModel,
    TwFuturesSettlementModel,
    TwStockSettlementModel,
)
from core.backtest.report.futures_reporter import FuturesBacktestReporter
from core.managers.futures.position_manager import FuturesPositionManager
from core.models import (
    FuturesAccount,
    FuturesOrder,
    FuturesPosition,
    FuturesQuote,
    StockPosition,
)
from core.strategies.futures import BaseFuturesStrategy
from core.utils import Action, FuturesSession, PositionType, Scale

"""
台期貨回測 model 組的測試

**期貨與股票在回測層的四個根本差異**，本檔逐一釘住——每一個都不會報錯，
只會讓數字靜默偏掉：

1. **部位價值是保證金不是契約價值**：期貨只凍結保證金，沿用股票的現金帳戶口徑
   會讓權益曲線整段偏高一個數量級（TX 一口契約價值 900 萬、保證金 70 萬）。
2. **損益逐日結算**：當天就進出帳戶，不等到平倉。
3. **沒有漲跌停**：期貨採動態價格穩定措施，用前收 ±10% 會誤拒正常委託。
4. **到期契約不再有報價**：策略下不出平倉單，不處理會永久佔用保證金。

不連網路、不碰真實的 tw_futures.db。
"""


DAY_1: datetime.date = datetime.date(2024, 3, 1)
DAY_2: datetime.date = datetime.date(2024, 3, 4)
MULTIPLIER: int = 200
MARGIN_PER_LOT: float = 700000.0


class ScriptedFuturesStrategy(BaseFuturesStrategy):
    """測試用期貨策略：依「日期 → 訂單清單」的腳本回傳訊號，不依賴任何資料源"""

    def __init__(
        self,
        open_script: Optional[Dict[datetime.date, List[FuturesOrder]]] = None,
        close_script: Optional[Dict[datetime.date, List[FuturesOrder]]] = None,
    ) -> None:
        super().__init__()

        self.strategy_name: str = "ScriptedFutures"
        self.init_capital: float = 3000000.0
        self.products: List[str] = ["TX"]
        self.max_lots: int = 4
        self.scale: Scale = Scale.DAY
        self.start_date: datetime.date = DAY_1
        self.end_date: datetime.date = datetime.date(2024, 3, 31)

        # **本檔驗的是接線不是成本**：預設費率會讓每個斷言都要先扣掉
        # 手續費與期交稅，成本本身另有 `tests/test_futures_cost.py`
        self.cost_config: FuturesCostConfig = FuturesCostConfig.free()

        self.open_script: Dict[datetime.date, List[FuturesOrder]] = open_script or {}
        self.close_script: Dict[datetime.date, List[FuturesOrder]] = close_script or {}

    def setup_account(self, account: FuturesAccount) -> None:
        """設置虛擬帳戶資訊"""

        self.account: FuturesAccount = account

    def setup_apis(self, feed=None) -> None:
        """測試不使用任何資料 API"""

        pass

    def check_open_signal(self, quotes: List[FuturesQuote]) -> List[FuturesOrder]:
        """依腳本回傳當日開倉單"""

        return self.open_script.get(quotes[0].date if quotes else None, [])

    def check_close_signal(self, quotes: List[FuturesQuote]) -> List[FuturesOrder]:
        """依腳本回傳當日平倉單"""

        return self.close_script.get(quotes[0].date if quotes else None, [])

    def check_stop_loss_signal(self, quotes: List[FuturesQuote]) -> List[FuturesOrder]:
        """腳本策略不實作停損"""

        return []

    def calculate_position_size(
        self, quotes: List[FuturesQuote], action: Action
    ) -> List[FuturesOrder]:
        """下單口數由腳本直接指定，不需計算"""

        return []


@pytest.fixture
def make_quote() -> Callable[..., FuturesQuote]:
    """建立 FuturesQuote 的 factory；未指定的 OHLC 一律沿用收盤價"""

    def _make_quote(
        product: str = "TX",
        expiry: str = "202403",
        date: Optional[datetime.date] = None,
        close: float = 18000.0,
        high: Optional[float] = None,
        low: Optional[float] = None,
        volume: int = 1000,
        settlement_price: Optional[float] = None,
        session: FuturesSession = FuturesSession.DAY,
        scale: Scale = Scale.DAY,
    ) -> FuturesQuote:
        return FuturesQuote(
            product=product,
            expiry=expiry,
            scale=scale,
            date=date or DAY_1,
            cur_price=close,
            volume=volume,
            open=close,
            high=high if high is not None else close,
            low=low if low is not None else close,
            close=close,
            session=session,
            settlement_price=settlement_price,
            multiplier=MULTIPLIER,
        )

    return _make_quote


@pytest.fixture
def make_order() -> Callable[..., FuturesOrder]:
    """建立 FuturesOrder 的 factory"""

    def _make_order(
        product: str = "TX",
        expiry: str = "202403",
        date: Optional[datetime.date] = None,
        action: Action = Action.BUY,
        position_type: PositionType = PositionType.LONG,
        price: float = 18000.0,
        volume: int = 1,
    ) -> FuturesOrder:
        return FuturesOrder(
            product=product,
            expiry=expiry,
            date=date or DAY_1,
            action=action,
            position_type=position_type,
            price=price,
            volume=volume,
        )

    return _make_order


@pytest.fixture
def make_position() -> Callable[..., FuturesPosition]:
    """建立已開倉的 FuturesPosition（保證金以每口固定金額給定）"""

    def _make_position(
        price: float = 18000.0,
        volume: int = 1,
        position_type: PositionType = PositionType.LONG,
        margin: float = MARGIN_PER_LOT,
        settled_pnl: float = 0.0,
    ) -> FuturesPosition:
        return FuturesPosition(
            id=1,
            product="TX",
            expiry="202403",
            position_type=position_type,
            date=DAY_1,
            price=price,
            volume=volume,
            multiplier=MULTIPLIER,
            margin=margin * volume,
            settled_pnl=settled_pnl,
        )

    return _make_position


@pytest.fixture
def make_backtester(monkeypatch: pytest.MonkeyPatch) -> Callable[..., Backtester]:
    """建立不載入資料庫的 Backtester（setup 只負責建立目錄與載入 API）"""

    def _make_backtester(strategy: BaseFuturesStrategy) -> Backtester:
        monkeypatch.setattr(Backtester, "setup", lambda self: None)
        return build_backtester(strategy)

    return _make_backtester


# === factory 接線 ===
def test_factory_dispatches_to_the_futures_model_set(make_backtester) -> None:
    """`(TW, FUTURE)` 分派到期貨那一組 model，不可落回台股組"""

    backtester: Backtester = make_backtester(ScriptedFuturesStrategy())

    assert isinstance(backtester.account, FuturesAccount)
    assert isinstance(backtester.position_manager, FuturesPositionManager)
    assert isinstance(backtester.instrument, TwFuturesSpec)
    assert isinstance(backtester.fill_model, TwFuturesFillModel)
    assert isinstance(backtester.cost_model, TwFuturesCostModel)
    assert isinstance(backtester.settlement, TwFuturesSettlementModel)
    assert isinstance(backtester.data_feed, TwFuturesDataFeed)
    assert backtester.reporter_cls is FuturesBacktestReporter


def test_futures_never_uses_adjusted_price(make_backtester) -> None:
    """期貨沒有除權息還原，訊號一律用原始價"""

    assert make_backtester(ScriptedFuturesStrategy()).adjusted_price is False


def test_cost_config_is_shared_with_the_position_manager(make_backtester) -> None:
    """成本設定必須是**同一個物件**，兩處各填一份必然漂移"""

    backtester: Backtester = make_backtester(ScriptedFuturesStrategy())

    assert backtester.cost_model.config is backtester.position_manager.cost_config


def test_engine_does_not_reject_futures_orders_by_holdings_cap(
    make_backtester, make_order
) -> None:
    """
    期貨限制的是總口數不是持倉檔數

    `BaseStrategy.max_holdings` 的預設值是 0，而引擎只把 `None` 當成不限制——
    期貨基底若沿用預設，每一張開倉單都會被引擎剔除且只留一行 warning。
    """

    backtester: Backtester = make_backtester(ScriptedFuturesStrategy())

    assert backtester.max_holdings is None
    assert backtester.check_max_holdings(make_order()) is True


# === InstrumentSpec ===
def test_to_units_returns_lots_not_contract_units() -> None:
    """
    `to_units()` 回傳口數，**不乘契約乘數**

    乘數逐契約不同（TX 200、MTX 50），而本方法拿不到商品；乘數一律取自部位自身。
    """

    assert TwFuturesSpec().to_units(3) == 3


def test_round_to_tick_respects_direction() -> None:
    """對齊跳動點：買進進位、賣出捨去、預設就近"""

    spec: TwFuturesSpec = TwFuturesSpec()

    assert spec.round_to_tick(18000.4, "up") == 18001.0
    assert spec.round_to_tick(18000.6, "down") == 18000.0
    assert spec.round_to_tick(18000.6) == 18001.0
    assert TwFuturesSpec(tick_size=0.05).round_to_tick(18000.06, "down") == 18000.05


def test_round_to_tick_looks_the_tick_size_up_by_product() -> None:
    """
    對齊檔位用的是**該商品**的跳動點，不是寫死的 1 點

    TE 的一檔是 0.05 點：沿用 1 點會把 18000.05 對齊成 18000，
    滑價算對了、對齊又把它推回去。
    """

    spec: TwFuturesSpec = TwFuturesSpec()

    assert spec.round_to_tick(18000.06, "down", "TE") == 18000.05
    assert spec.round_to_tick(18000.06, "down", "TX") == 18000.0


def test_round_to_tick_absorbs_the_division_float_error() -> None:
    """
    非整數跳動點的浮點誤差不可以吃掉一整檔

    `17999.8 / 0.2` 在浮點下是 89998.99999999999，直接往下取整會得到 17999.6。
    跳動點是 1 點時看不到這件事，TF／ZFF 的 0.2 點才會踩到。
    """

    spec: TwFuturesSpec = TwFuturesSpec()

    assert spec.round_to_tick(17999.8, "down", "TF") == 17999.8
    assert spec.round_to_tick(18000.2, "up", "TF") == 18000.2


def test_explicit_tick_size_wins_over_the_lookup() -> None:
    """建構時明確指定的跳動點蓋過查表，留給尚未登錄的商品"""

    spec: TwFuturesSpec = TwFuturesSpec(tick_size=0.05)

    assert spec.get_tick_size("TX") == 0.05


def test_unregistered_product_falls_back_to_the_default() -> None:
    """未登錄的商品退回預設值（並在 log 留痕），不讓整場回測中斷"""

    assert TwFuturesSpec().get_tick_size("XIF") == TwFuturesSpec.DEFAULT_TICK_SIZE


def test_futures_have_no_price_limits() -> None:
    """期貨沒有固定漲跌停；`(None, None)` 的語意是「本市場無此制度」"""

    assert TwFuturesSpec().get_price_limits(18000.0) == (None, None)


# === FillModel ===
def test_fill_rejects_price_outside_bar_range(make_quote, make_order) -> None:
    """成交價超出當根 bar 的高低點即拒單（前視偏誤擋板）"""

    event_counts: Dict[str, int] = new_event_counts()
    fill_model: TwFuturesFillModel = TwFuturesFillModel(event_counts=event_counts)
    quote: FuturesQuote = make_quote(close=18000, high=18050, low=17950)

    assert fill_model.validate(make_order(price=18000), quote) is True
    assert fill_model.validate(make_order(price=18100), quote) is False
    assert event_counts["rejected_fill_price"] == 1


def test_fill_does_not_apply_price_limits(make_quote, make_order) -> None:
    """
    **期貨不做漲跌停檢查**

    同樣的情境在台股會因為超出前收 ±10% 被拒；期貨的價格穩定措施是動態的，
    無法由前收推出區間，硬套會誤拒正常委託。
    """

    fill_model: TwFuturesFillModel = TwFuturesFillModel()
    fill_model.on_bar_close([make_quote(close=10000)])  # 前收 10000

    # 較前收 +80%，但落在當根 bar 區間內
    quote: FuturesQuote = make_quote(close=18000, high=18000, low=18000)

    assert fill_model.validate(make_order(price=18000), quote) is True


def test_fill_applies_slippage_in_the_unfavourable_direction(
    make_quote, make_order
) -> None:
    """滑價一律往對下單者不利的方向，並對齊跳動點"""

    config: FillConfig = FillConfig(slippage_bps_buy=100, slippage_bps_sell=100)
    fill_model: TwFuturesFillModel = TwFuturesFillModel(config=config)
    quote: FuturesQuote = make_quote(close=18000, high=18500, low=17500)

    buy: FuturesOrder = fill_model.fill(make_order(price=18000), quote)
    sell: FuturesOrder = fill_model.fill(
        make_order(action=Action.SELL, position_type=PositionType.SHORT, price=18000),
        quote,
    )

    assert buy.price == 18180.0  # 18000 × 1.01，進位到 1 點
    assert sell.price == 17820.0  # 18000 × 0.99，捨去到 1 點


def test_fill_truncates_volume_in_lots(make_quote, make_order) -> None:
    """成交量上限的單位是**口**：當日 1,000 口 × 10% ＝ 100 口"""

    event_counts: Dict[str, int] = new_event_counts()
    fill_model: TwFuturesFillModel = TwFuturesFillModel(
        event_counts=event_counts,
        config=FillConfig(
            max_volume_share=0.1, volume_cap_policy=VolumeCapPolicy.TRUNCATE
        ),
    )

    filled: FuturesOrder = fill_model.fill(make_order(volume=500), make_quote())

    assert filled.volume == 100
    assert event_counts["truncated_by_volume"] == 1


def test_fill_returns_the_same_object_when_nothing_applies(
    make_quote, make_order
) -> None:
    """未啟用任何假設時回傳原物件，行為與導入前逐筆相同"""

    order: FuturesOrder = make_order()

    assert TwFuturesFillModel().fill(order, make_quote()) is order


# === 部位計入權益的口徑 ===
def test_base_mark_position_keeps_the_cash_account_semantics() -> None:
    """
    基底的 `mark_position()` 必須與下沉前的引擎邏輯逐筆相同

    這是台股回歸的護欄：做多部位的價值是市值、放空是保證金加未實現損益。
    """

    settlement: BaseSettlementModel = TwStockSettlementModel.__new__(
        TwStockSettlementModel
    )

    long_position: StockPosition = StockPosition(
        stock_id="2330", position_type=PositionType.LONG, price=100.0, volume=2
    )
    short_position: StockPosition = StockPosition(
        stock_id="2330", position_type=PositionType.SHORT, price=100.0, volume=2
    )
    short_position.margin = 180000.0

    long_value: float = settlement.mark_position(long_position, 110.0, 2000)
    short_value: float = settlement.mark_position(short_position, 110.0, 2000)

    assert long_value == 110.0 * 2000  # 市值
    assert long_position.unrealized_pnl == 20000.0
    assert short_value == 180000.0 - 20000.0  # 保證金 ＋ 未實現損益
    assert short_position.unrealized_pnl == -20000.0


def test_futures_position_value_is_margin_not_contract_value(make_position) -> None:
    """
    **期貨部位計入權益的是保證金，不是契約價值**

    沿用現金帳戶口徑會把 360 萬的契約價值算進權益，而實際佔用的只有 70 萬——
    權益曲線會整段偏高一個數量級。
    """

    account: FuturesAccount = FuturesAccount(3000000.0)
    settlement: TwFuturesSettlementModel = TwFuturesSettlementModel(
        FuturesPositionManager(account)
    )
    position: FuturesPosition = make_position(price=18000.0, volume=1)

    # 已逐日結算過：盯市價等於部位的最近結算價，未實現損益為 0
    assert settlement.mark_position(position, 18000.0, 1) == MARGIN_PER_LOT
    assert position.unrealized_pnl == 0.0

    # 尚未結算的一段：價差 × 乘數 × 口數，且乘數取自部位而非 `units`
    assert settlement.mark_position(position, 18100.0, 1) == MARGIN_PER_LOT + 20000.0
    assert position.unrealized_pnl == 20000.0
    # 報酬率的分母是保證金
    assert position.unrealized_roi == round(20000.0 / MARGIN_PER_LOT * 100, 2)


def test_engine_equity_equals_balance_plus_margin(
    make_backtester, make_quote, make_order
) -> None:
    """
    引擎算出的每日權益 ＝ 可動用餘額 ＋ 保證金（含未結算損益）

    這是 `snapshot_daily_equity()` 唯一與商品類別有關的地方，也是本步驟在引擎
    開的唯一掛點；接錯的話權益會多出一整個契約價值。
    """

    backtester: Backtester = make_backtester(ScriptedFuturesStrategy())
    backtester.position_manager.open_position(make_order(price=18000.0, volume=1))

    equity: float = backtester.snapshot_daily_equity(
        DAY_1, [make_quote(close=18000.0, settlement_price=18000.0)]
    )

    assert equity == backtester.account.init_capital
    assert equity == round(
        backtester.account.balance + backtester.account.margin_used, 2
    )


# === 逐日盯市 ===
def test_daily_settlement_moves_cash_on_the_same_day(
    make_backtester, make_quote, make_order
) -> None:
    """
    **期貨的損益當天就進出帳戶**，不等到平倉

    結算後部位的 `price` 重設為結算價，下一日才不會重複計算同一段。
    """

    backtester: Backtester = make_backtester(ScriptedFuturesStrategy())
    account: FuturesAccount = backtester.account
    backtester.position_manager.open_position(make_order(price=18000.0, volume=1))

    balance_before: float = account.balance
    backtester.settlement.on_bar_close(
        DAY_1,
        [make_quote(close=18120.0, settlement_price=18100.0)],
        account,
        backtester.event_counts,
    )

    position: FuturesPosition = account.get_positions()[0]

    # (18100 − 18000) × 200 × 1 ＝ 20,000，且用的是**結算價**不是收盤價
    assert account.balance - balance_before == 20000.0
    assert position.price == 18100.0
    assert position.settled_pnl == 20000.0


def test_mark_price_prefers_settlement_then_close(make_position, make_quote) -> None:
    """盯市價優先取結算價；夜盤沒有結算價時退回收盤價，都沒有時沿用最近結算價"""

    settlement: TwFuturesSettlementModel = TwFuturesSettlementModel(
        FuturesPositionManager(FuturesAccount(3000000.0))
    )
    position: FuturesPosition = make_position(price=18000.0)

    with_settlement: FuturesQuote = make_quote(close=18120.0, settlement_price=18100.0)
    without_settlement: FuturesQuote = make_quote(close=18120.0)

    assert settlement.get_mark_price(position, {"TX202403": with_settlement}) == 18100.0
    assert (
        settlement.get_mark_price(position, {"TX202403": without_settlement}) == 18120.0
    )
    assert settlement.get_mark_price(position, {}) == 18000.0


def test_expired_contract_is_closed_at_the_last_settlement_price(
    make_backtester, make_quote, make_order
) -> None:
    """
    到期契約連續無報價即強制出場

    契約到期後不再有報價，策略拿不到報價也就下不出平倉單；不處理的話部位會留到
    回測結束並持續佔用保證金（實測會凍結 79 萬）。
    """

    backtester: Backtester = make_backtester(ScriptedFuturesStrategy())
    account: FuturesAccount = backtester.account
    settlement: TwFuturesSettlementModel = backtester.settlement
    backtester.position_manager.open_position(make_order(price=18000.0, volume=1))

    # 到期前的最後一根 bar
    settlement.on_bar_close(
        DAY_1,
        [make_quote(close=18100.0, settlement_price=18100.0)],
        account,
        backtester.event_counts,
    )

    # 之後連續無報價：門檻之前不出場
    for _ in range(settlement.MAX_NO_QUOTE_DAYS - 1):
        settlement.on_bar_close(DAY_2, [], account, backtester.event_counts)
        assert account.get_positions()

    settlement.on_bar_close(DAY_2, [], account, backtester.event_counts)

    assert account.get_positions() == []
    assert account.margin_used == 0.0
    assert backtester.event_counts["forced_cover_no_quote"] == 1
    # 損益在到期前已逐日結清，出場那一段不再產生價差
    assert account.trade_records[0].realized_pnl == 20000.0


def test_no_quote_counter_resets_when_quotes_come_back(
    make_backtester, make_quote, make_order
) -> None:
    """單日資料缺漏不該被當成到期：報價回來就歸零重算"""

    backtester: Backtester = make_backtester(ScriptedFuturesStrategy())
    account: FuturesAccount = backtester.account
    settlement: TwFuturesSettlementModel = backtester.settlement
    backtester.position_manager.open_position(make_order(price=18000.0, volume=1))

    for _ in range(settlement.MAX_NO_QUOTE_DAYS - 1):
        settlement.on_bar_close(DAY_1, [], account, backtester.event_counts)

    settlement.on_bar_close(
        DAY_2,
        [make_quote(close=18000.0, settlement_price=18000.0)],
        account,
        backtester.event_counts,
    )

    assert settlement.no_quote_days["TX202403"] == 0
    assert account.get_positions()


# === DataFeed ===
class StubFuturesPriceAPI:
    """回傳固定行情的假 API；`get()` 依 product／session 過濾"""

    COLUMNS: List[str] = [
        "date",
        "product",
        "expiry",
        "session",
        "開盤價",
        "最高價",
        "最低價",
        "收盤價",
        "成交量",
        "結算價",
        "未沖銷契約量",
    ]

    def __init__(self, trading_days: Optional[List[datetime.date]] = None) -> None:
        self.trading_days: List[datetime.date] = trading_days or [DAY_1]
        self.rows: List[list] = [
            [DAY_1, "TX", "202403", "day", 18000, 18100, 17900, 18050, 100, 18050, 500],
            [
                DAY_1,
                "TX",
                "202403",
                "night",
                18050,
                18080,
                18000,
                18060,
                50,
                None,
                None,
            ],
            [DAY_1, "TX", "202404", "day", 18100, 18200, 18000, 18150, 30, 18150, 200],
            [DAY_1, "MTX", "202403", "day", 18000, 18100, 17900, 18050, 20, 18050, 80],
        ]

    def get(self, date, product=None, session=None) -> pd.DataFrame:
        rows: List[list] = [
            row
            for row in self.rows
            if row[0] == date
            and (product is None or row[1] == product)
            and (session is None or row[3] == session.value)
        ]
        return pd.DataFrame(rows, columns=self.COLUMNS)

    def get_trading_days(self, start_date, end_date, product=None) -> List:
        return list(self.trading_days)

    def close(self) -> None:
        pass


def make_feed(strategy: BaseFuturesStrategy) -> TwFuturesDataFeed:
    """建立已注入假 API 的 DataFeed（不連資料庫）"""

    feed: TwFuturesDataFeed = TwFuturesDataFeed()
    feed.futures_price = StubFuturesPriceAPI()
    feed.products = list(strategy.products)
    feed.session = strategy.session
    feed.start_date = strategy.start_date
    feed.end_date = strategy.end_date
    return feed


def test_datafeed_returns_every_expiry_of_the_declared_product() -> None:
    """當日**所有到期月**都要轉出——挑契約是策略的政策，不是資料源的"""

    quotes: List[FuturesQuote] = make_feed(ScriptedFuturesStrategy()).get_quotes(
        DAY_1, Scale.DAY
    )

    assert sorted(quote.expiry for quote in quotes) == ["202403", "202404"]
    assert {quote.product for quote in quotes} == {"TX"}


def test_datafeed_returns_only_the_declared_session() -> None:
    """
    只取策略宣告的時段

    同一契約日夜盤的 `symbol` 完全相同，兩者混在同一根 bar 會讓引擎的
    `quote_map` 互相覆蓋、訊號被算兩次。
    """

    quotes: List[FuturesQuote] = make_feed(ScriptedFuturesStrategy()).get_quotes(
        DAY_1, Scale.DAY
    )

    assert {quote.session for quote in quotes} == {FuturesSession.DAY}


def test_datafeed_has_no_tick_support_yet() -> None:
    """期貨 Tick 級別尚未實作，回空 list 而非拋錯"""

    assert make_feed(ScriptedFuturesStrategy()).get_quotes(DAY_1, Scale.TICK) == []


def test_market_open_uses_the_futures_calendar() -> None:
    """交易日判準取自期貨行情表，**不可沿用台股日曆**"""

    feed: TwFuturesDataFeed = make_feed(ScriptedFuturesStrategy())

    assert feed.is_market_open(DAY_1) is True
    assert feed.is_market_open(DAY_2) is False

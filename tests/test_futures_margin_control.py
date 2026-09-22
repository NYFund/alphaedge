import datetime
from pathlib import Path
from typing import Dict, List, Optional

import pytest

from core.backtest.models.cost_model import TwFuturesCostModel
from core.backtest.models.settlement_model import TwFuturesSettlementModel
from core.managers.futures.position_manager import FuturesPositionManager
from core.market.tw.futures_margin_config import FuturesMarginConfig
from core.models import FuturesAccount, FuturesOrder, FuturesPosition, FuturesQuote
from core.models.cost_config import FuturesCostConfig
from core.utils import Action, MarginCallPolicy, PositionType
from core.utils.constant import FUTURES_MULTIPLIER
from tests.conftest import build_futures_quote

"""
台期貨槓桿與部位控管測試

**期貨的資金約束與股票完全不同**，本檔逐一釘住：

1. **可開口數由「當時生效的保證金」決定**，不是契約價值也不是現行保證金——
   TX 的原始保證金在 2024 年就從 167,000 調到 338,000（漲一倍），
   用單一數值回測整段會讓前後兩段的槓桿都算錯。
2. **判斷充足度的是「權益」不是「可動用餘額」**：浮動損益每日結算進帳戶，
   可動用餘額歸零不等於被追繳；反之浮動獲利可以支撐加碼。
3. **追繳門檻是維持保證金**，與原始保證金是兩個獨立的公告值，不可用比率互推。

不連網路、不碰正式的 `tw_futures.db`。
"""

INIT_CAPITAL: float = 3_000_000
MULTIPLIER: int = FUTURES_MULTIPLIER["TX"]
DAY_1: datetime.date = datetime.date(2024, 3, 1)
DAY_2: datetime.date = datetime.date(2024, 3, 4)


class StubMarginAPI:
    """依生效日回傳不同保證金的假 API（模擬調整公告）"""

    def __init__(
        self,
        initial: Dict[datetime.date, int],
        maintenance: Optional[Dict[datetime.date, int]] = None,
    ) -> None:
        self.initial: Dict[datetime.date, int] = initial
        self.maintenance: Dict[datetime.date, int] = maintenance or {}

    @staticmethod
    def lookup(table: Dict[datetime.date, int], date: datetime.date) -> Optional[int]:
        """取 `生效日 <= 查詢日` 的最後一筆，與真實 API 的語意相同"""

        effective: List[datetime.date] = sorted(d for d in table if d <= date)
        return table[effective[-1]] if effective else None

    def get_initial_margin(self, product, date, fallback_to_earliest=False):
        return self.lookup(self.initial, date)

    def get_maintenance_margin(self, product, date, fallback_to_earliest=False):
        return self.lookup(self.maintenance, date)

    def get_covered_date_range(self, product):
        return {"earliest": "2020-03-13", "latest": "2026-08-12"}


def make_order(
    action: Action = Action.BUY,
    position_type: PositionType = PositionType.LONG,
    price: float = 18000.0,
    volume: int = 1,
    date: datetime.date = DAY_1,
    expiry: str = "202403",
) -> FuturesOrder:
    """組一張 TX 訂單"""

    return FuturesOrder(
        product="TX",
        expiry=expiry,
        date=date,
        action=action,
        position_type=position_type,
        price=price,
        volume=volume,
    )


def make_quote(
    close: float = 18000.0,
    date: datetime.date = DAY_1,
    expiry: str = "202403",
) -> FuturesQuote:
    """組一筆帶結算價的 TX 報價"""

    return build_futures_quote(
        expiry=expiry,
        date=date,
        close=close,
        settlement_price=close,
        multiplier=MULTIPLIER,
    )


def make_manager(
    margin_config: Optional[FuturesMarginConfig] = None,
    init_capital: float = INIT_CAPITAL,
) -> FuturesPositionManager:
    """建立零成本的部位管理器（本檔驗的是保證金，不是費用）"""

    return FuturesPositionManager(
        FuturesAccount(init_capital=init_capital),
        cost_model=TwFuturesCostModel(FuturesCostConfig.free()),
        margin_config=margin_config or FuturesMarginConfig.ratio(),
    )


# === 設定的預設值 ===
def test_lookup_is_the_default_mode() -> None:
    """
    **預設是查表**，比率近似必須明確表態

    保證金資料已備妥（2020-03 起），用比率近似回測是刻意的降級。
    """

    assert FuturesMarginConfig.default().use_api is True
    assert FuturesMarginConfig.ratio().use_api is False


def test_force_cover_is_the_default_margin_call_policy() -> None:
    """真實帳戶不會讓保證金不足的部位續留，只標記會高估留倉能力"""

    assert (
        FuturesMarginConfig.default().margin_call_policy == MarginCallPolicy.FORCE_COVER
    )


# === 可開口數依當時生效的保證金 ===
def test_margin_follows_the_effective_date() -> None:
    """
    **保證金取「生效日 <= 交易日」的最後一筆**

    調整公告只在調整那天有一列，用等號查會讓其餘每一天都查不到。
    """

    api: StubMarginAPI = StubMarginAPI(
        initial={
            datetime.date(2024, 8, 9): 265000,
            datetime.date(2024, 8, 22): 292000,
        }
    )
    manager: FuturesPositionManager = make_manager(
        FuturesMarginConfig(api=api),
        init_capital=10_000_000,
    )

    before: float = manager.calculate_margin(
        18000, 1, MULTIPLIER, product="TX", date=datetime.date(2024, 8, 20)
    )
    after: float = manager.calculate_margin(
        18000, 1, MULTIPLIER, product="TX", date=datetime.date(2024, 8, 23)
    )

    assert before == 265000
    assert after == 292000


def test_affordable_lots_change_across_an_adjustment() -> None:
    """
    調整生效日前後的**可開口數不同**——這是本步驟的驗收條件

    同一筆資金在 265,000／口 時開得起 3 口，調到 292,000／口 之後只剩 2 口。
    """

    from core.strategies.futures.momentum_futures_strategy import (
        MomentumFuturesStrategy,
    )

    api: StubMarginAPI = StubMarginAPI(
        initial={
            datetime.date(2024, 8, 9): 265000,
            datetime.date(2024, 8, 22): 292000,
        }
    )
    strategy: MomentumFuturesStrategy = MomentumFuturesStrategy()
    strategy.setup_account(FuturesAccount(init_capital=1_600_000))
    strategy.margin_config = FuturesMarginConfig(api=api)
    strategy.max_capital_usage = 0.5

    before: FuturesQuote = make_quote(date=datetime.date(2024, 8, 20))
    after: FuturesQuote = make_quote(date=datetime.date(2024, 8, 23))

    # 800,000 ÷ 265,000 = 3 口；800,000 ÷ 292,000 = 2 口
    assert strategy.calculate_max_lots(before) == 3
    assert strategy.calculate_max_lots(after) == 2


def test_floating_profit_supports_more_lots() -> None:
    """
    **浮動獲利可以支撐加碼**（本步驟明文要求）

    期貨的損益逐日結算進帳戶，賺到的錢當天就能用來開新倉。
    """

    from core.strategies.futures.momentum_futures_strategy import (
        MomentumFuturesStrategy,
    )

    api: StubMarginAPI = StubMarginAPI(initial={datetime.date(2020, 1, 1): 200000})
    manager: FuturesPositionManager = make_manager(FuturesMarginConfig(api=api))
    strategy: MomentumFuturesStrategy = MomentumFuturesStrategy()
    strategy.setup_account(manager.account)
    strategy.margin_config = manager.margin_config
    strategy.max_capital_usage = 1.0

    position: FuturesPosition = manager.open_position(make_order(volume=1))
    lots_before: int = strategy.calculate_max_lots(make_quote())

    # 一天大賺 1,000 點：(19000 − 18000) × 200 = 200,000 進帳戶
    manager.settle_daily(position, 19000.0)
    lots_after: int = strategy.calculate_max_lots(make_quote(close=19000.0))

    assert manager.account.balance == INIT_CAPITAL - 200000 + 200000
    assert lots_after == lots_before + 1


# === 維持保證金與追繳 ===
def test_maintenance_margin_is_a_separate_published_value() -> None:
    """
    維持保證金與原始保證金是**兩個獨立的公告值**，不可用比率互推
    """

    api: StubMarginAPI = StubMarginAPI(
        initial={datetime.date(2020, 1, 1): 338000},
        maintenance={datetime.date(2020, 1, 1): 259000},
    )
    manager: FuturesPositionManager = make_manager(FuturesMarginConfig(api=api))
    position: FuturesPosition = manager.open_position(make_order(volume=2))

    assert position.margin == 338000 * 2
    assert manager.calculate_maintenance_margin(position, DAY_1) == 259000 * 2


def test_maintenance_falls_back_to_initial_without_api() -> None:
    """
    沒有 API 時以**已繳原始保證金**當門檻

    那比實際的維持保證金嚴格（追繳會提早觸發），但方向上不會讓績效變好看，
    比靜默不做風控好。
    """

    manager: FuturesPositionManager = make_manager()
    position: FuturesPosition = manager.open_position(make_order())

    assert manager.calculate_maintenance_margin(position, DAY_1) == position.margin


def build_settlement(
    margin_config: FuturesMarginConfig,
) -> TwFuturesSettlementModel:
    """建立掛在同一組保證金設定上的結算模型"""

    return TwFuturesSettlementModel(make_manager(margin_config))


def test_margin_call_force_covers_until_equity_is_enough() -> None:
    """
    **砍到足額為止，不是一次清空帳戶**

    真實券商的斷頭也是砍到補足為止；一次全平會讓回測低估留倉的續航力。
    """

    api: StubMarginAPI = StubMarginAPI(
        initial={datetime.date(2020, 1, 1): 400000},
        maintenance={datetime.date(2020, 1, 1): 600000},
    )
    settlement: TwFuturesSettlementModel = build_settlement(
        FuturesMarginConfig(api=api)
    )
    manager: FuturesPositionManager = settlement.position_manager
    account: FuturesAccount = manager.account

    manager.open_position(make_order(volume=1))
    manager.open_position(make_order(volume=1))

    event_counts: Dict[str, int] = {"forced_cover_margin_call": 0}
    # 跌 5,000 點：權益 3,000,000 − 2,000,000 ＝ 1,000,000 < 維持 1,200,000
    settlement.on_bar_close(
        DAY_2, [make_quote(close=13000.0, date=DAY_2)], account, event_counts
    )

    # 平掉一口後維持保證金降為 600,000 ≤ 權益 1,000,000，故只砍一口
    assert len(account.get_positions()) == 1
    assert event_counts["forced_cover_margin_call"] == 1
    assert account.equity == 1_000_000


def test_margin_call_warn_only_keeps_the_position() -> None:
    """
    `WARN_ONLY` 只標記不平倉，且**不計數**

    `forced_cover_margin_call` 的語意是「強制平倉幾次」；只標記卻計數，
    會讓報表把「撐過去了」讀成「被斷頭了」，而且該狀態每根 bar 都成立，
    計數會隨天數膨脹。與台股的 `WARN_ONLY` 同一種處理。
    """

    api: StubMarginAPI = StubMarginAPI(
        initial={datetime.date(2020, 1, 1): 400000},
        maintenance={datetime.date(2020, 1, 1): 600000},
    )
    settlement: TwFuturesSettlementModel = build_settlement(
        FuturesMarginConfig(api=api, margin_call_policy=MarginCallPolicy.WARN_ONLY)
    )
    manager: FuturesPositionManager = settlement.position_manager
    account: FuturesAccount = manager.account

    manager.open_position(make_order(volume=2))

    event_counts: Dict[str, int] = {"forced_cover_margin_call": 0}
    settlement.on_bar_close(
        DAY_2, [make_quote(close=13000.0, date=DAY_2)], account, event_counts
    )

    assert len(account.get_positions()) == 1  # 部位還在（2 口未被平掉）
    assert account.get_positions()[0].volume == 2
    assert event_counts["forced_cover_margin_call"] == 0


def test_no_margin_call_when_equity_is_sufficient() -> None:
    """權益足夠時不可誤觸追繳——誤砍會讓策略的持有期被無故截斷"""

    api: StubMarginAPI = StubMarginAPI(
        initial={datetime.date(2020, 1, 1): 400000},
        maintenance={datetime.date(2020, 1, 1): 300000},
    )
    settlement: TwFuturesSettlementModel = build_settlement(
        FuturesMarginConfig(api=api)
    )
    manager: FuturesPositionManager = settlement.position_manager
    account: FuturesAccount = manager.account
    manager.open_position(make_order(volume=1))

    event_counts: Dict[str, int] = {"forced_cover_margin_call": 0}
    settlement.on_bar_close(
        DAY_2, [make_quote(close=18100.0, date=DAY_2)], account, event_counts
    )

    assert len(account.get_positions()) == 1
    assert event_counts["forced_cover_margin_call"] == 0


def test_margin_call_ratio_triggers_earlier() -> None:
    """`margin_call_ratio` > 1 即「比交易所更早出場」的自訂風控"""

    api: StubMarginAPI = StubMarginAPI(
        initial={datetime.date(2020, 1, 1): 400000},
        maintenance={datetime.date(2020, 1, 1): 300000},
    )
    settlement: TwFuturesSettlementModel = build_settlement(
        FuturesMarginConfig(api=api, margin_call_ratio=20.0)
    )
    manager: FuturesPositionManager = settlement.position_manager
    account: FuturesAccount = manager.account
    manager.open_position(make_order(volume=1))

    event_counts: Dict[str, int] = {"forced_cover_margin_call": 0}
    settlement.on_bar_close(
        DAY_2, [make_quote(close=18000.0, date=DAY_2)], account, event_counts
    )

    # 權益 3,000,000 < 維持 300,000 × 20，故即使沒虧損也觸發
    assert account.get_positions() == []
    assert event_counts["forced_cover_margin_call"] == 1


# === 回測接線 ===
@pytest.fixture
def isolated_futures_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """
    DataFeed 的期貨連線改開在暫存路徑

    `setup()` 會以寫入模式連 `TW_FUTURES_DB_PATH`：沒有資料庫的機器上，這會在
    `data/db/` 建出 0 byte 的 `tw_futures.db`，之後「檔案存在才跑」的測試就不再
    跳過、改以 no such table 失敗——第一次跑 1 skipped、第二次 FAILED。
    """

    import core.backtest.datafeed.tw.futures_datafeed as datafeed_module

    db_path: Path = tmp_path / "tw_futures.db"
    monkeypatch.setattr(datafeed_module, "TW_FUTURES_DB_PATH", db_path)
    return db_path


def test_datafeed_injects_the_margin_api_into_the_shared_config(
    isolated_futures_db: Path,
) -> None:
    """
    保證金 API 由 DataFeed 注入**策略與部位管理層共用的那一個設定物件**

    兩邊各拿一份設定的話，策略算得出口數、部位管理層卻開不進去，
    而且不會有任何錯誤訊息。
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

    config: FuturesMarginConfig = backtester.position_manager.margin_config

    # 策略、部位管理層、結算模型三者共用同一個物件
    assert strategy.margin_config is config
    assert backtester.settlement.margin_config is config
    assert backtester.data_feed.margin_config is config

    # 注入前沒有 API，注入後三者同時看得到
    assert config.api is None
    backtester.data_feed.setup(strategy)
    try:
        assert config.api is backtester.data_feed.margin
        assert strategy.margin_config.api is not None
    finally:
        backtester.data_feed.close()


def test_ratio_mode_is_not_injected(isolated_futures_db: Path) -> None:
    """明確宣告比率近似時不注入 API——否則使用者的降級表態會被無聲推翻"""

    from core.backtest.datafeed.tw.futures_datafeed import TwFuturesDataFeed
    from core.strategies.futures.momentum_futures_strategy import (
        MomentumFuturesStrategy,
    )

    config: FuturesMarginConfig = FuturesMarginConfig.ratio()
    strategy: MomentumFuturesStrategy = MomentumFuturesStrategy()
    strategy.margin_config = config

    feed: TwFuturesDataFeed = TwFuturesDataFeed(margin_config=config)
    feed.setup(strategy)
    try:
        assert config.api is None
    finally:
        feed.close()


# === 實際的保證金表（暫存 DB）===
def test_margin_table_matches_the_announced_adjustment(tmp_path: Path) -> None:
    """
    以保證金表驗證「調整生效日前後的可開口數不同，且與公告一致」

    TX 於 2024-08-09 調為 265,000／口、2024-08-22 再調為 292,000／口。
    表內數值取自當時的公告，寫進暫存 DB；**不連正式的 tw_futures.db**——
    舊版這條依賴正式資料，沒有 DB 的機器上永遠被 skip，等於沒有驗收。
    """

    import pandas as pd

    from core.api.tw.futures_margin_api import FuturesMarginAPI
    from core.dao.tw.futures_margin_dao import FuturesMarginDAO

    dao: FuturesMarginDAO = FuturesMarginDAO(db_path=tmp_path / "tw_futures.db")
    dao.ensure_tables()
    dao.insert_rows(
        FuturesMarginDAO.TABLE_NAME,
        pd.DataFrame(
            [
                [
                    "2024-08-09",
                    "TX",
                    "臺股期貨",
                    204000,
                    204000,
                    265000,
                    "announcement",
                ],
                [
                    "2024-08-22",
                    "TX",
                    "臺股期貨",
                    224000,
                    224000,
                    292000,
                    "announcement",
                ],
            ],
            columns=[
                "effective_date",
                "product",
                "product_name",
                "結算保證金",
                "維持保證金",
                "原始保證金",
                "source",
            ],
        ),
    )
    dao.commit()

    api: FuturesMarginAPI = FuturesMarginAPI(conn=dao.conn)
    try:
        before: Optional[int] = api.get_initial_margin("TX", datetime.date(2024, 8, 20))
        on_the_day: Optional[int] = api.get_initial_margin(
            "TX", datetime.date(2024, 8, 22)
        )
        after: Optional[int] = api.get_initial_margin("TX", datetime.date(2024, 8, 23))
        maintenance: Optional[int] = api.get_maintenance_margin(
            "TX", datetime.date(2024, 8, 23)
        )
    finally:
        dao.close()

    assert before == 265000
    # 生效日當天即適用新值（`effective_date <= 該日`）
    assert on_the_day == 292000
    assert after == 292000
    # 維持保證金是另一個公告值，不是原始的固定比例
    assert maintenance == 224000

    budget: float = 1_600_000
    assert int(budget // before) == 6
    assert int(budget // after) == 5


# === 股票期貨：查表模式走比例表 ===
STOCK_FUTURES_PRODUCT: str = "CDF"
STOCK_FUTURES_CONTRACT_SIZE: int = 2000  # 標準型股票期貨的契約單位（股）
UNDERLYING_CLOSE: float = 500.0  # 標的證券當日收盤價
INITIAL_RATE: float = 0.135  # 原始保證金適用比例
MAINTENANCE_RATE: float = 0.1035  # 維持保證金適用比例


class StubTwoTableMarginAPI:
    """
    同時帶「金額表」與「比例表」的假 API

    **分表依據是「金額 vs 比例」而不是「指數 vs 股票」**：指數期貨與 ETF 期貨在
    金額表（每口固定金額），個股期貨在比例表（適用比例 ＋ 契約單位，要自己算）。
    """

    def __init__(self, amount_products: Optional[Dict[str, int]] = None) -> None:
        # 金額表涵蓋的商品：{商品代碼: 每口原始保證金}
        self.amount_products: Dict[str, int] = amount_products or {}
        self.rate_calls: List[str] = []

    def get_initial_margin(self, product, date, fallback_to_earliest=False):
        return self.amount_products.get(product)

    def get_maintenance_margin(self, product, date, fallback_to_earliest=False):
        per_lot: Optional[int] = self.amount_products.get(product)
        return int(per_lot * 0.75) if per_lot is not None else None

    def calculate_stock_futures_margin(
        self, product, date, price, fallback_to_earliest=False
    ):
        self.rate_calls.append(product)
        if product != STOCK_FUTURES_PRODUCT:
            return None
        return price * STOCK_FUTURES_CONTRACT_SIZE * INITIAL_RATE

    def calculate_stock_futures_maintenance_margin(
        self, product, date, price, fallback_to_earliest=False
    ):
        if product != STOCK_FUTURES_PRODUCT:
            return None
        return price * STOCK_FUTURES_CONTRACT_SIZE * MAINTENANCE_RATE

    def get_covered_date_range(self, product):
        return {"earliest": "2020-03-13", "latest": "2026-08-12"}


def make_stock_futures_manager(
    api: Optional[StubTwoTableMarginAPI] = None,
    underlying_price: Optional[float] = UNDERLYING_CLOSE,
) -> FuturesPositionManager:
    """組出帶兩張表與標的取價路徑的部位管理器"""

    return FuturesPositionManager(
        FuturesAccount(init_capital=INIT_CAPITAL),
        cost_model=TwFuturesCostModel(FuturesCostConfig.free()),
        margin_config=FuturesMarginConfig(api=api or StubTwoTableMarginAPI()),
        multiplier_resolver=lambda product, date: STOCK_FUTURES_CONTRACT_SIZE,
        underlying_price_resolver=lambda product, date: underlying_price,
    )


def make_stock_futures_order(volume: int = 1) -> FuturesOrder:
    """組一張股票期貨訂單"""

    return FuturesOrder(
        product=STOCK_FUTURES_PRODUCT,
        expiry="202403",
        date=DAY_1,
        action=Action.BUY,
        position_type=PositionType.LONG,
        price=UNDERLYING_CLOSE,
        volume=volume,
    )


def test_stock_futures_margin_falls_back_to_the_rate_table() -> None:
    """
    個股期貨查不到金額表時改走比例表，不再直接中止開倉

    比例表那條路徑（`標的股價 × 契約單位 × 比例`）早就實作好也有測試，
    只是**全專案沒有任何呼叫端**——於是股期在預設的查表模式下開不了倉。
    """

    manager: FuturesPositionManager = make_stock_futures_manager()

    position: Optional[FuturesPosition] = manager.open_position(
        make_stock_futures_order(volume=2)
    )

    assert position is not None
    expected: float = UNDERLYING_CLOSE * STOCK_FUTURES_CONTRACT_SIZE * INITIAL_RATE * 2
    assert position.margin == expected


def test_amount_table_wins_and_the_rate_table_is_not_consulted() -> None:
    """
    金額表查得到就用金額表，**完全不碰比例表**

    ETF 期貨在金額表內（`NYF` 等，2020-07-22 起），指數期貨亦然；
    這條擋的是「兩張表都算一次」造成的口徑混用。
    """

    api: StubTwoTableMarginAPI = StubTwoTableMarginAPI(amount_products={"TX": 167_000})
    manager: FuturesPositionManager = FuturesPositionManager(
        FuturesAccount(init_capital=INIT_CAPITAL),
        cost_model=TwFuturesCostModel(FuturesCostConfig.free()),
        margin_config=FuturesMarginConfig(api=api),
        underlying_price_resolver=lambda product, date: UNDERLYING_CLOSE,
    )

    position: Optional[FuturesPosition] = manager.open_position(make_order())

    assert position is not None
    assert position.margin == 167_000
    assert api.rate_calls == []


def test_neither_table_covers_the_product_raises() -> None:
    """兩張表都查不到就中斷——**刻意不退回比率近似**，理由同金額表那條"""

    manager: FuturesPositionManager = make_stock_futures_manager(
        api=StubTwoTableMarginAPI()
    )
    order: FuturesOrder = make_stock_futures_order()
    order.product = "ZZZ"  # 兩張表都沒有的商品

    with pytest.raises(ValueError, match="比例表也查不到"):
        manager.open_position(order)


def test_missing_underlying_price_also_raises() -> None:
    """
    標的當日無收盤價時同樣中斷，不拿期貨價替代

    期貨價與標的股價是兩個數字，拿前者算保證金會讓整段偏掉且毫無徵兆。
    """

    manager: FuturesPositionManager = make_stock_futures_manager(underlying_price=None)

    with pytest.raises(ValueError, match="比例表也查不到"):
        manager.open_position(make_stock_futures_order())


def test_stock_futures_maintenance_margin_uses_its_own_rate() -> None:
    """
    維持保證金走**維持比例**，不是用原始比例打折

    兩者是同一列裡的兩個獨立欄位，用固定折數互推會讓追繳門檻整段偏掉。
    """

    manager: FuturesPositionManager = make_stock_futures_manager()
    position: Optional[FuturesPosition] = manager.open_position(
        make_stock_futures_order()
    )

    assert position is not None
    assert manager.calculate_maintenance_margin(position, DAY_1) == (
        UNDERLYING_CLOSE * STOCK_FUTURES_CONTRACT_SIZE * MAINTENANCE_RATE
    )

import datetime
from typing import List, Optional

import pytest

from core.backtest.models.cost_model import FuturesCostConfig, TwFuturesCostModel
from core.managers.futures.position_manager import (
    FuturesMarginConfig,
    FuturesPositionManager,
)
from core.models import (
    FuturesAccount,
    FuturesOrder,
    FuturesPosition,
    FuturesTradeRecord,
)
from core.utils import Action, PositionType
from core.utils.constant import FUTURES_MULTIPLIER

"""
期貨部位記帳測試

**驗收公式：
PnL = 價格變動 × 乘數 × 口數**。本檔把三件與股票根本不同的事釘住：

1. **開倉只凍結保證金**，不買下契約價值——`balance` 減少的是保證金而非契約價值，
   `equity`（餘額 ＋ 佔用保證金）在開倉當下不變。
2. **逐日盯市**：損益每天就進出 `balance`，不等到平倉；走完數日結算再平倉，
   總損益仍須等於「開倉價 → 平倉價」的一次算法。
3. **沒有股數換算、沒有證交稅**。

初始資金取得夠大，避免測試意外撞到保證金不足而變成在測別的東西。
"""

INIT_CAPITAL: float = 10_000_000
MULTIPLIER: int = FUTURES_MULTIPLIER["TX"]

DAY_1: datetime.date = datetime.date(2026, 1, 5)
DAY_2: datetime.date = datetime.date(2026, 1, 6)
DAY_3: datetime.date = datetime.date(2026, 1, 7)


@pytest.fixture
def manager() -> FuturesPositionManager:
    """
    成本全為 0 的部位管理器，讓 PnL 恰好等於價格公式

    **刻意用 `FuturesCostConfig.free()` 而非預設值**：預設帶有
    期交稅與手續費，本檔驗的是「記帳本身」，成本一旦不為 0，任何斷言的偏差
    都要先扣掉費用才知道是不是記帳錯了。成本本身另有 `tests/test_futures_cost.py`。
    """

    account: FuturesAccount = FuturesAccount(init_capital=INIT_CAPITAL)
    return FuturesPositionManager(
        account, cost_model=TwFuturesCostModel(FuturesCostConfig.free())
    )


def make_order(
    action: Action,
    position_type: PositionType,
    price: float,
    volume: int = 1,
    date: datetime.date = DAY_1,
    expiry: str = "202601",
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


# === 驗收公式：PnL = 價格變動 × 乘數 × 口數 ===
def test_long_pnl_is_price_change_times_multiplier_times_lots(
    manager: FuturesPositionManager,
) -> None:
    """做多獲利：(18100 − 18000) × 200 × 2 = 40,000"""

    manager.open_position(
        make_order(Action.BUY, PositionType.LONG, price=18000, volume=2)
    )
    records: List[FuturesTradeRecord] = manager.close_position(
        make_order(Action.SELL, PositionType.LONG, price=18100, volume=2, date=DAY_2)
    )

    assert records[0].realized_pnl == (18100 - 18000) * MULTIPLIER * 2


def test_short_pnl_flips_sign(manager: FuturesPositionManager) -> None:
    """做空：價格下跌才獲利，(18000 − 17900) × 200 × 1 = 20,000"""

    manager.open_position(
        make_order(Action.SELL, PositionType.SHORT, price=18000, volume=1)
    )
    records: List[FuturesTradeRecord] = manager.close_position(
        make_order(Action.BUY, PositionType.SHORT, price=17900, volume=1, date=DAY_2)
    )

    assert records[0].realized_pnl == (18000 - 17900) * MULTIPLIER * 1


def test_long_loss_is_negative(manager: FuturesPositionManager) -> None:
    """做多虧損同樣走同一條公式，不做任何截斷"""

    manager.open_position(
        make_order(Action.BUY, PositionType.LONG, price=18000, volume=1)
    )
    records: List[FuturesTradeRecord] = manager.close_position(
        make_order(Action.SELL, PositionType.LONG, price=17800, volume=1, date=DAY_2)
    )

    assert records[0].realized_pnl == (17800 - 18000) * MULTIPLIER * 1


def test_multiplier_comes_from_the_registry(manager: FuturesPositionManager) -> None:
    """乘數取自 `FUTURES_MULTIPLIER`，不是寫死在 manager 裡"""

    position: Optional[FuturesPosition] = manager.open_position(
        make_order(Action.BUY, PositionType.LONG, price=18000)
    )

    assert position.multiplier == MULTIPLIER


def test_unregistered_product_raises(manager: FuturesPositionManager) -> None:
    """
    未登錄乘數的商品當場 KeyError

    乘數猜錯不會有任何徵兆，只會讓整條 PnL 靜默偏掉——中斷比靜默錯誤好查。
    """

    order: FuturesOrder = FuturesOrder(
        product="XIF",  # 乘數曾變更過，刻意未登錄
        expiry="202601",
        date=DAY_1,
        action=Action.BUY,
        position_type=PositionType.LONG,
        price=18000,
        volume=1,
    )

    with pytest.raises(KeyError):
        manager.open_position(order)


# === 保證金：開倉只凍結，不買下契約價值 ===
def test_open_freezes_margin_not_contract_value(
    manager: FuturesPositionManager,
) -> None:
    """
    餘額減少的是保證金，不是契約價值

    契約價值 18000 × 200 = 3,600,000；保證金（比率 10%）＝ 360,000。
    若誤用股票的記帳方式，餘額會少掉 360 萬。
    """

    manager.open_position(make_order(Action.BUY, PositionType.LONG, price=18000))
    account: FuturesAccount = manager.account

    assert account.margin_used == 18000 * MULTIPLIER * 1 * 0.1
    assert account.balance == INIT_CAPITAL - account.margin_used


def test_equity_is_unchanged_at_open(manager: FuturesPositionManager) -> None:
    """開倉當下沒有損益，總權益（餘額 ＋ 佔用保證金）不變"""

    manager.open_position(make_order(Action.BUY, PositionType.LONG, price=18000))

    assert manager.account.equity == INIT_CAPITAL


def test_margin_is_released_on_close(manager: FuturesPositionManager) -> None:
    """平倉後保證金全額釋回，`margin_used` 歸零"""

    manager.open_position(make_order(Action.BUY, PositionType.LONG, price=18000))
    manager.close_position(
        make_order(Action.SELL, PositionType.LONG, price=18100, date=DAY_2)
    )

    assert manager.account.margin_used == 0
    assert manager.account.balance == INIT_CAPITAL + (18100 - 18000) * MULTIPLIER


def test_insufficient_margin_rejects_the_order() -> None:
    """可動用餘額不足以繳保證金時不開倉，且不動用帳戶"""

    account: FuturesAccount = FuturesAccount(init_capital=100_000)
    manager: FuturesPositionManager = FuturesPositionManager(account)

    position: Optional[FuturesPosition] = manager.open_position(
        make_order(Action.BUY, PositionType.LONG, price=18000)
    )

    assert position is None
    assert account.balance == 100_000
    assert account.positions == []


def test_direction_and_action_must_agree(manager: FuturesPositionManager) -> None:
    """多單只能買進開倉、空單只能賣出開倉；不一致就拒單"""

    position: Optional[FuturesPosition] = manager.open_position(
        make_order(Action.SELL, PositionType.LONG, price=18000)
    )

    assert position is None
    assert manager.account.positions == []


# === 逐日盯市 ===
def test_daily_settlement_moves_cash_every_day(
    manager: FuturesPositionManager,
) -> None:
    """
    損益每天就進出帳戶，不等到平倉

    這是期貨與股票最根本的記帳差異；股票側的 `settle_daily()` 是 no-op。
    """

    position: FuturesPosition = manager.open_position(
        make_order(Action.BUY, PositionType.LONG, price=18000)
    )
    balance_after_open: float = manager.account.balance

    manager.settle_daily(position, settle_price=18050)

    assert manager.account.balance == balance_after_open + 50 * MULTIPLIER
    assert position.settled_pnl == 50 * MULTIPLIER
    # 結算後基準價重設，下一日才不會重複計算同一段
    assert position.price == 18050


def test_settlement_then_close_totals_the_same(
    manager: FuturesPositionManager,
) -> None:
    """
    走完數日結算再平倉，總損益仍等於「開倉價 → 平倉價」的一次算法

    只算最後一段（結算價 → 平倉價）會漏掉前面所有交易日，這是逐日盯市最容易
    寫錯的地方。
    """

    position: FuturesPosition = manager.open_position(
        make_order(Action.BUY, PositionType.LONG, price=18000)
    )

    manager.settle_daily(position, settle_price=18050)  # 第 1 日
    manager.settle_daily(position, settle_price=17900)  # 第 2 日（回吐並轉虧）

    records: List[FuturesTradeRecord] = manager.close_position(
        make_order(Action.SELL, PositionType.LONG, price=18200, date=DAY_3)
    )

    assert records[0].realized_pnl == (18200 - 18000) * MULTIPLIER * 1
    assert manager.account.equity == INIT_CAPITAL + (18200 - 18000) * MULTIPLIER


def test_settlement_is_skipped_when_price_is_none(
    manager: FuturesPositionManager,
) -> None:
    """
    夜盤沒有結算價，`None` 必須被跳過

    當成 0 會讓部位在一天內被結算成歸零——那是災難性的靜默錯誤。
    """

    position: FuturesPosition = manager.open_position(
        make_order(Action.BUY, PositionType.LONG, price=18000)
    )
    balance_after_open: float = manager.account.balance

    manager.settle_daily(position, settle_price=None)

    assert manager.account.balance == balance_after_open
    assert position.settled_pnl == 0
    assert position.price == 18000


def test_entry_price_survives_settlement(manager: FuturesPositionManager) -> None:
    """`price` 會被結算重設，開倉價保存在 `entry_price`"""

    position: FuturesPosition = manager.open_position(
        make_order(Action.BUY, PositionType.LONG, price=18000)
    )
    manager.settle_daily(position, settle_price=18050)

    assert position.price == 18050
    assert position.entry_price == 18000


def test_record_keeps_the_original_entry_price(
    manager: FuturesPositionManager,
) -> None:
    """交易紀錄的買入價是**開倉價**，不是最近一次結算價"""

    position: FuturesPosition = manager.open_position(
        make_order(Action.BUY, PositionType.LONG, price=18000)
    )
    manager.settle_daily(position, settle_price=18050)
    records: List[FuturesTradeRecord] = manager.close_position(
        make_order(Action.SELL, PositionType.LONG, price=18200, date=DAY_3)
    )

    assert records[0].buy_price == 18000
    assert records[0].sell_price == 18200
    assert records[0].entry_price == 18000
    assert records[0].exit_price == 18200


def test_short_record_entry_exit_is_direction_neutral(
    manager: FuturesPositionManager,
) -> None:
    """SHORT 的 entry 是賣出開倉、exit 是買進回補（方向中立欄位）"""

    manager.open_position(make_order(Action.SELL, PositionType.SHORT, price=18000))
    records: List[FuturesTradeRecord] = manager.close_position(
        make_order(Action.BUY, PositionType.SHORT, price=17900, date=DAY_2)
    )

    assert records[0].entry_price == 18000
    assert records[0].exit_price == 17900
    assert records[0].entry_date == DAY_1
    assert records[0].exit_date == DAY_2


# === 部分平倉與 FIFO ===
def test_partial_close_prorates_settled_pnl(
    manager: FuturesPositionManager,
) -> None:
    """部分平倉時，已結算損益依平倉口數等比例攤提"""

    position: FuturesPosition = manager.open_position(
        make_order(Action.BUY, PositionType.LONG, price=18000, volume=4)
    )
    manager.settle_daily(position, settle_price=18100)  # 已結算 100 × 200 × 4

    records: List[FuturesTradeRecord] = manager.close_position(
        make_order(Action.SELL, PositionType.LONG, price=18100, volume=1, date=DAY_2)
    )

    # 平掉 1/4：已結算段攤提 1/4，最後一段為 0（結算價 = 平倉價）
    assert records[0].settled_pnl == 100 * MULTIPLIER * 4 * 0.25
    assert records[0].realized_pnl == (18100 - 18000) * MULTIPLIER * 1
    assert position.volume == 3
    assert position.settled_pnl == 100 * MULTIPLIER * 3


def test_fifo_closes_the_earliest_position_first(
    manager: FuturesPositionManager,
) -> None:
    """同一契約有多筆部位時依開倉順序平倉"""

    manager.open_position(
        make_order(Action.BUY, PositionType.LONG, price=18000, volume=1, date=DAY_1)
    )
    manager.open_position(
        make_order(Action.BUY, PositionType.LONG, price=18500, volume=1, date=DAY_2)
    )

    records: List[FuturesTradeRecord] = manager.close_position(
        make_order(Action.SELL, PositionType.LONG, price=18600, volume=1, date=DAY_3)
    )

    assert len(records) == 1
    assert records[0].buy_price == 18000  # 先開的那一筆
    assert records[0].realized_pnl == (18600 - 18000) * MULTIPLIER


def test_close_only_matches_the_same_direction(
    manager: FuturesPositionManager,
) -> None:
    """
    賣出平的是多單，不會誤平空單

    **空單直接塞進帳戶而不走 `open_position()`**：同契約雙向持倉已被開倉端擋掉
    （見 `test_open_rejects_the_opposite_direction_on_the_same_contract`），
    但 `close_position()` 的方向比對本身仍要釘住——它是最後一道防線，
    不能因為前一道擋住了就沒有測試。
    """

    manager.open_position(
        make_order(Action.BUY, PositionType.LONG, price=18000, volume=1)
    )
    manager.account.positions.append(
        FuturesPosition(
            id=manager.account.generate_trade_id(),
            product="TX",
            expiry="202601",
            is_closed=False,
            position_type=PositionType.SHORT,
            date=DAY_1,
            price=18000,
            volume=1,
            multiplier=MULTIPLIER,
            margin=0.0,
        )
    )

    records: List[FuturesTradeRecord] = manager.close_position(
        make_order(Action.SELL, PositionType.LONG, price=18100, volume=1, date=DAY_2)
    )

    assert len(records) == 1
    assert records[0].position_type == PositionType.LONG
    assert len(manager.account.get_positions(position_type=PositionType.SHORT)) == 1


# === 帳戶彙總 ===
def test_open_lots_is_gross_when_one_direction_per_contract(
    manager: FuturesPositionManager,
) -> None:
    """
    同契約只可能單一方向，淨口數的絕對值即毛口數

    `MomentumFuturesStrategy` 以 `lots != 0` 判斷「是否已有部位」、以
    `sum(abs(lots))` 對 `max_lots` 扣減，兩者都預設這條性質成立。
    """

    manager.open_position(
        make_order(Action.BUY, PositionType.LONG, price=18000, volume=3)
    )
    manager.open_position(
        make_order(Action.BUY, PositionType.LONG, price=18100, volume=1, date=DAY_2)
    )

    assert manager.account.get_open_lots() == {"TX202601": 4}


def test_open_lots_signs_short_positions_negative(
    manager: FuturesPositionManager,
) -> None:
    """空單以負數表示，不同契約各自獨立"""

    manager.open_position(
        make_order(Action.SELL, PositionType.SHORT, price=18000, volume=2)
    )
    manager.open_position(
        make_order(
            Action.BUY, PositionType.LONG, price=18050, volume=1, expiry="202602"
        )
    )

    assert manager.account.get_open_lots() == {"TX202601": -2, "TX202602": 1}


# === 同契約雙向持倉===
def test_open_rejects_the_opposite_direction_on_the_same_contract(
    manager: FuturesPositionManager,
) -> None:
    """
    同一契約已有反向部位即拒單，保證金不被重複佔用

    判準與股票端同一條（反之亦然）。舊版放行之後，
    `get_open_lots()` 相抵成淨口數 0（＝帳上看起來沒有曝險），
    `margin_used` 卻押著兩份原始保證金——而交易所對沖部位只收單邊。
    """

    long_position: Optional[FuturesPosition] = manager.open_position(
        make_order(Action.BUY, PositionType.LONG, price=18000, volume=1)
    )
    margin_after_long: float = manager.account.margin_used

    rejected: Optional[FuturesPosition] = manager.open_position(
        make_order(Action.SELL, PositionType.SHORT, price=18000, volume=1, date=DAY_2)
    )

    assert long_position is not None
    assert rejected is None
    assert manager.account.margin_used == margin_after_long
    assert len(manager.account.get_positions()) == 1
    assert manager.account.get_open_lots() == {"TX202601": 1}


def test_open_rejects_a_long_when_a_short_is_open(
    manager: FuturesPositionManager,
) -> None:
    """反向亦然：先空後多同樣拒單（兩個分支都要擋，不能只擋一半）"""

    manager.open_position(
        make_order(Action.SELL, PositionType.SHORT, price=18000, volume=1)
    )
    rejected: Optional[FuturesPosition] = manager.open_position(
        make_order(Action.BUY, PositionType.LONG, price=18000, volume=1, date=DAY_2)
    )

    assert rejected is None
    assert manager.account.get_open_lots() == {"TX202601": -1}


def test_open_allows_the_opposite_direction_on_another_expiry(
    manager: FuturesPositionManager,
) -> None:
    """
    跨月份的反向部位不受限

    這是**價差部位**（calendar spread），不是同一契約的雙向持倉；
    交易所另有價差部位保證金費率，本專案尚無該資料源，故兩腿各繳全額——
    方向保守（高估保證金），不會讓績效變好看。
    """

    near: Optional[FuturesPosition] = manager.open_position(
        make_order(Action.BUY, PositionType.LONG, price=18000, volume=1)
    )
    far: Optional[FuturesPosition] = manager.open_position(
        make_order(
            Action.SELL, PositionType.SHORT, price=18100, volume=1, expiry="202602"
        )
    )

    assert near is not None
    assert far is not None
    assert manager.account.get_open_lots() == {"TX202601": 1, "TX202602": -1}


def test_reopening_the_opposite_direction_after_a_close_is_allowed(
    manager: FuturesPositionManager,
) -> None:
    """平倉之後可以反手：`is_closed` 的部位不算佔位"""

    manager.open_position(
        make_order(Action.BUY, PositionType.LONG, price=18000, volume=1)
    )
    manager.close_position(
        make_order(Action.SELL, PositionType.LONG, price=18100, volume=1, date=DAY_2)
    )

    reversed_position: Optional[FuturesPosition] = manager.open_position(
        make_order(Action.SELL, PositionType.SHORT, price=18100, volume=1, date=DAY_2)
    )

    assert reversed_position is not None
    assert manager.account.get_open_lots() == {"TX202601": -1}


def test_roi_is_based_on_margin_not_contract_value(
    manager: FuturesPositionManager,
) -> None:
    """
    報酬率的分母是保證金

    期貨是保證金交易，用契約價值當分母會把槓桿效果抹掉。
    """

    manager.open_position(make_order(Action.BUY, PositionType.LONG, price=18000))
    records: List[FuturesTradeRecord] = manager.close_position(
        make_order(Action.SELL, PositionType.LONG, price=18100, date=DAY_2)
    )

    margin: float = 18000 * MULTIPLIER * 0.1
    assert records[0].roi == round((18100 - 18000) * MULTIPLIER / margin * 100, 2)


# === 成本掛點 ===
def test_free_config_makes_pnl_equal_the_price_formula(
    manager: FuturesPositionManager,
) -> None:
    """
    `FuturesCostConfig.free()` 之下 PnL 恰好等於價格公式

    這是驗證引擎接線用的口徑：任何偏差都必定來自記帳本身而非費用。
    """

    manager.open_position(make_order(Action.BUY, PositionType.LONG, price=18000))
    records: List[FuturesTradeRecord] = manager.close_position(
        make_order(Action.SELL, PositionType.LONG, price=18100, date=DAY_2)
    )

    assert records[0].transaction_cost == 0
    assert records[0].realized_pnl == (18100 - 18000) * MULTIPLIER


def test_default_config_charges_real_fees() -> None:
    """
    **預設設定不再是零成本**

    零成本回測會系統性高估短線期貨策略；預設帶上期交稅（法規值）與
    每口手續費（市場常見值），要跑純價格公式必須明確改用 `free()`。
    """

    account: FuturesAccount = FuturesAccount(init_capital=INIT_CAPITAL)
    manager: FuturesPositionManager = FuturesPositionManager(account)

    manager.open_position(make_order(Action.BUY, PositionType.LONG, price=18000))
    records: List[FuturesTradeRecord] = manager.close_position(
        make_order(Action.SELL, PositionType.LONG, price=18100, date=DAY_2)
    )

    # 手續費 50 × 2 ＋ 期交稅（18000 ＋ 18100）× 200 × 0.00002 取整後各 72 元
    assert records[0].commission == 100.0
    assert records[0].tax == 72 + 72
    assert records[0].realized_pnl == (18100 - 18000) * MULTIPLIER - 244.0


def test_costs_are_deducted_when_configured() -> None:
    """接上費率之後，成本從損益中扣除——掛點確實有作用"""

    account: FuturesAccount = FuturesAccount(init_capital=INIT_CAPITAL)
    manager: FuturesPositionManager = FuturesPositionManager(
        account,
        cost_model=TwFuturesCostModel(
            FuturesCostConfig(commission_per_lot=50.0, tax_rate=0.0)
        ),
        margin_config=FuturesMarginConfig(initial_margin_ratio=0.1),
    )

    manager.open_position(make_order(Action.BUY, PositionType.LONG, price=18000))
    records: List[FuturesTradeRecord] = manager.close_position(
        make_order(Action.SELL, PositionType.LONG, price=18100, date=DAY_2)
    )

    # 買賣各一次手續費
    assert records[0].transaction_cost == 100.0
    assert records[0].realized_pnl == (18100 - 18000) * MULTIPLIER - 100.0


# ============================================================
# 查表模式：接上 FuturesMarginAPI
# ============================================================


class StubMarginAPI:
    """只回傳固定值的假 API，避免測試連 tw_futures.db"""

    def __init__(self, per_lot=None, covered=None) -> None:
        self.per_lot = per_lot
        self.covered = covered
        self.calls = []

    def get_initial_margin(self, product, date, fallback_to_earliest=False):
        self.calls.append((product, date, fallback_to_earliest))
        return self.per_lot

    def get_covered_date_range(self, product):
        return self.covered


def make_manager_with_api(api) -> FuturesPositionManager:
    """建立查表模式的部位管理器"""

    account: FuturesAccount = FuturesAccount(init_capital=INIT_CAPITAL)
    return FuturesPositionManager(account, margin_config=FuturesMarginConfig(api=api))


def test_lookup_mode_uses_the_table_value_per_lot() -> None:
    """
    帶了 api 就用查表值 × 口數，**不再用契約價值 × 比率**

    真實保證金是每口固定金額，比率只是沒有資料時的權宜。
    """

    api = StubMarginAPI(per_lot=701000)
    manager = make_manager_with_api(api)

    position = manager.open_position(
        make_order(Action.BUY, PositionType.LONG, price=18000, volume=3)
    )

    assert position.margin == 701000 * 3
    # 比率模式會是 18000 × 200 × 3 × 0.1 = 1,080,000，兩者差很多
    assert position.margin != 18000 * MULTIPLIER * 3 * 0.1


def test_lookup_mode_passes_product_and_date() -> None:
    """
    保證金隨商品與日期變動，兩者都必須傳到 API

    只傳商品會取到「最新」的保證金套用到歷史，整段資金效率都會錯。
    """

    api = StubMarginAPI(per_lot=500000)
    manager = make_manager_with_api(api)

    manager.open_position(
        make_order(Action.BUY, PositionType.LONG, price=18000, date=DAY_2)
    )

    assert api.calls == [("TX", DAY_2, False)]


def test_lookup_mode_raises_when_not_covered() -> None:
    """
    查不到就 raise，**刻意不退回比率**

    理由同 `FUTURES_MULTIPLIER` 用 `[]` 而非 `.get()`：靜默套一個近似值會讓
    資金效率與可開口數整段偏掉卻毫無徵兆，中斷比靜默錯誤好查。
    """

    api = StubMarginAPI(per_lot=None, covered={"earliest": "2020-03-13"})
    manager = make_manager_with_api(api)

    with pytest.raises(ValueError, match="查無 TX"):
        manager.open_position(make_order(Action.BUY, PositionType.LONG, price=18000))


def test_error_message_names_the_covered_range() -> None:
    """錯誤訊息要說得出「表內涵蓋到哪」，否則使用者不知道該往前補還是改區間"""

    api = StubMarginAPI(
        per_lot=None, covered={"earliest": "2020-03-13", "latest": "2026-08-12"}
    )
    manager = make_manager_with_api(api)

    with pytest.raises(ValueError, match="2020-03-13"):
        manager.open_position(make_order(Action.BUY, PositionType.LONG, price=18000))


def test_lookup_mode_requires_product_and_date() -> None:
    """直接呼叫 `calculate_margin()` 而漏傳商品或日期時當場報錯，不猜"""

    manager = make_manager_with_api(StubMarginAPI(per_lot=1))

    with pytest.raises(ValueError, match="必須提供 product 與 date"):
        manager.calculate_margin(price=18000, volume=1, multiplier=MULTIPLIER)


def test_ratio_mode_is_unchanged_without_api() -> None:
    """
    沒有 api 時行為與 S5 之前完全相同

    既有的 23 條測試全部走這條路徑，不可被查表模式影響。
    """

    manager: FuturesPositionManager = FuturesPositionManager(
        FuturesAccount(init_capital=INIT_CAPITAL)
    )
    position = manager.open_position(
        make_order(Action.BUY, PositionType.LONG, price=18000)
    )

    assert position.margin == 18000 * MULTIPLIER * 0.1


def test_fallback_flag_is_forwarded_to_the_api() -> None:
    """
    `fallback_to_earliest` 由設定決定並原樣傳給 API

    它無法區分「該商品從未被調整過」與「查詢日早於資料涵蓋範圍」，
    故必須由呼叫端明確表態，不可由 manager 自己決定。
    """

    api = StubMarginAPI(per_lot=100000)
    account: FuturesAccount = FuturesAccount(init_capital=INIT_CAPITAL)
    manager = FuturesPositionManager(
        account,
        margin_config=FuturesMarginConfig(api=api, fallback_to_earliest=True),
    )

    manager.open_position(make_order(Action.BUY, PositionType.LONG, price=18000))

    assert api.calls[0][2] is True

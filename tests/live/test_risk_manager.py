import dataclasses
import datetime
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional

import pytest

from core.dao.tw.live_trade_dao import LiveTradeDAO
from core.live.risk.risk_config import RISK_LIMIT_CAPS, RiskConfig
from core.live.risk.risk_manager import (
    ExposureItem,
    PreTradeRiskManager,
    RiskDecision,
    check_daily_amount,
    check_price_deviation,
    check_single_order_amount,
    check_volume_cap,
    is_closing_order,
    truncate_batch_by_exposure,
)
from core.live.risk.trading_mode import TradingMode, TradingModeState
from core.models import FuturesOrder, StockOrder
from core.utils import Action, PositionType, StockPriceType

"""
事前風控：策略或程式出錯時，損失要有硬上限

本檔分兩段：
- **純判定**（金額、數量、價格、批次曝險）：只要「訂單 ＋ 帳戶快照 ＋ 設定」
  就算得出過或不過，故連 `PreTradeRiskManager` 實例都不必建。
  日後若要讓回測事前評估「會被風控擋掉多少」，重用的就是這幾條。
- **依賴行程狀態**（頻率、kill switch、交易模式、當日損益）：本來就只屬實盤。
"""

NOW: datetime.datetime = datetime.datetime(2026, 9, 19, 13, 25)
CAPITAL: float = 1_000_000.0


def make_order(
    symbol: str = "2330",
    action: Action = Action.BUY,
    position_type: PositionType = PositionType.LONG,
    volume: int = 2,
    price: float = 1000.0,
    price_type: Optional[StockPriceType] = StockPriceType.LMT,
) -> StockOrder:
    return StockOrder(
        stock_id=symbol,
        action=action,
        position_type=position_type,
        volume=volume,
        price=price,
        price_type=price_type,
    )


# === 設定的全域上限 ===
def test_config_caps_cover_every_field() -> None:
    """
    每個門檻都要有全域上限

    漏一個的話，那一項可以被調到任意大，而且不會有人發現——
    「調鬆一點」是逐次發生的，每一次都有當下看起來合理的理由。
    """

    from dataclasses import fields

    assert {field.name for field in fields(RiskConfig)} == set(RISK_LIMIT_CAPS)


def test_config_rejects_values_above_the_cap() -> None:
    """超過上限的設定在建立時就拋出，不等到盤中第一次拒單才發現"""

    with pytest.raises(ValueError, match="超過全域上限"):
        RiskConfig(max_stock_lots=RISK_LIMIT_CAPS["max_stock_lots"] + 1)


def test_config_rejects_negative_values() -> None:
    """負值也擋：`-1` 的門檻等於全部拒單，而錯誤訊息會指向每一張單"""

    with pytest.raises(ValueError, match="不可為負"):
        RiskConfig(daily_loss_ratio=-0.01)


def test_config_is_frozen() -> None:
    """
    設定不可在盤中被改

    那是最難查的一種事故：拒單數突然變了，而程式碼看起來完全一樣。
    """

    config: RiskConfig = RiskConfig()

    with pytest.raises(dataclasses.FrozenInstanceError):
        config.max_stock_lots = 100  # type: ignore[misc]


# === 純判定 ===
def test_single_order_amount_cap() -> None:
    """單筆委託金額上限"""

    config: RiskConfig = RiskConfig(single_order_amount_ratio=0.2)

    assert check_single_order_amount(200_000, CAPITAL, config).passed is True
    assert check_single_order_amount(200_001, CAPITAL, config).passed is False


def test_daily_amount_is_a_flow_not_a_stock() -> None:
    """
    單日累計委託金額管的是**流量**

    一天之內反覆買進賣出同一檔，曝險始終很低，累計金額卻早就爆了——
    那正是迴圈 bug 的形狀。只留曝險那條就擋不住它。
    """

    config: RiskConfig = RiskConfig(daily_amount_ratio=1.0)

    assert check_daily_amount(900_000, 100_000, CAPITAL, config).passed is True
    assert check_daily_amount(900_000, 100_001, CAPITAL, config).passed is False


def test_volume_cap_differs_between_stock_and_futures() -> None:
    """張與口是不同的單位，上限也不同"""

    config: RiskConfig = RiskConfig(max_stock_lots=50, max_futures_contracts=5)

    assert check_volume_cap(50, is_futures=False, config=config).passed is True
    assert check_volume_cap(51, is_futures=False, config=config).passed is False
    assert check_volume_cap(6, is_futures=True, config=config).passed is False


def test_price_deviation_uses_the_reference_price() -> None:
    """委託價偏離基準價超過門檻就拒"""

    config: RiskConfig = RiskConfig(price_deviation_ratio=0.03)

    assert check_price_deviation(1030.0, 1000.0, config).passed is True
    assert check_price_deviation(1031.0, 1000.0, config).passed is False
    assert check_price_deviation(969.0, 1000.0, config).passed is False


def test_price_limits_use_the_announced_values() -> None:
    """
    漲跌停用交易所公告值

    除權息日的基準價是另行公告的，公式推出來的區間會整段偏移。
    """

    config: RiskConfig = RiskConfig()
    decision: RiskDecision = check_price_deviation(
        1100.0, 1000.0, config, limit_up=1050.0, limit_down=950.0
    )

    assert decision.passed is False
    assert decision.category == "PRICE_LIMIT"


def test_market_orders_pass_the_price_check() -> None:
    """
    **市價單沒有價格可檢查**

    在這裡擋不住的東西不要假裝擋得住：市價單改以送單前的基準價做事前檢查，
    成交後由盤後流程以實際成交價回頭比對。
    """

    config: RiskConfig = RiskConfig()

    assert check_price_deviation(None, 1000.0, config).passed is True
    assert check_price_deviation(0.0, 1000.0, config).passed is True


@pytest.mark.parametrize(
    "position_type, action, expected",
    [
        (PositionType.LONG, Action.SELL, True),
        (PositionType.LONG, Action.BUY, False),
        (PositionType.SHORT, Action.BUY, True),
        (PositionType.SHORT, Action.SELL, False),
    ],
)
def test_closing_order_detection(
    position_type: PositionType, action: Action, expected: bool
) -> None:
    """
    平倉的判定只寫一份

    散在多處會漂移，而漂移的後果是平倉單被當成開倉擋掉——部位因此失去出場能力。
    """

    assert (
        is_closing_order(make_order(position_type=position_type, action=action))
        is expected
    )


# === 批次曝險 ===
def test_batch_truncates_instead_of_rejecting_everything() -> None:
    """
    超額時截斷後面的單，不整批拒絕

    整批拒會讓平倉單也被擋掉，那比超額更危險。
    """

    # 單一標的占比放到全域上限（0.5），讓這條測試只驗總曝險那一維
    config: RiskConfig = RiskConfig(
        total_exposure_ratio=1.0, single_symbol_exposure_ratio=0.5
    )
    items: List[ExposureItem] = [
        ExposureItem(make_order(symbol="2330"), 500_000),
        ExposureItem(make_order(symbol="2317"), 500_000),
        ExposureItem(make_order(symbol="2454"), 100_000),
    ]

    allowed, truncated = truncate_batch_by_exposure(items, 0.0, {}, CAPITAL, config)

    assert [item.order.symbol for item in allowed] == ["2330", "2317"]
    assert [item.order.symbol for item, _ in truncated] == ["2454"]


def test_closing_orders_are_never_truncated() -> None:
    """
    平倉單一律放行且不計入曝險

    它讓曝險變小，擋它沒有道理。
    """

    config: RiskConfig = RiskConfig(total_exposure_ratio=0.1)
    items: List[ExposureItem] = [
        ExposureItem(
            make_order(
                symbol="2330", action=Action.SELL, position_type=PositionType.LONG
            ),
            900_000,
        )
    ]

    allowed, truncated = truncate_batch_by_exposure(items, 0.0, {}, CAPITAL, config)

    assert len(allowed) == 1
    assert truncated == []


def test_single_symbol_exposure_is_a_separate_dimension() -> None:
    """
    單一標的占比與總曝險是兩個維度

    總曝險沒滿不代表可以把全部資金壓在一檔上。
    """

    config: RiskConfig = RiskConfig(
        total_exposure_ratio=1.0, single_symbol_exposure_ratio=0.25
    )
    items: List[ExposureItem] = [
        ExposureItem(make_order(symbol="2330"), 200_000),
        ExposureItem(make_order(symbol="2330"), 200_000),
    ]

    allowed, truncated = truncate_batch_by_exposure(items, 0.0, {}, CAPITAL, config)

    assert len(allowed) == 1
    assert "單一標的上限" in truncated[0][1]


def test_existing_exposure_is_counted(dao_factory: object = None) -> None:
    """既有持倉要算進去：只看本批的話，隔日續跑會把額度重新算一次"""

    config: RiskConfig = RiskConfig(total_exposure_ratio=1.0)
    items: List[ExposureItem] = [ExposureItem(make_order(), 200_000)]

    allowed, truncated = truncate_batch_by_exposure(
        items, 900_000.0, {}, CAPITAL, config
    )

    assert allowed == []
    assert len(truncated) == 1


# === 依賴行程狀態 ===
@pytest.fixture
def dao() -> LiveTradeDAO:
    instance: LiveTradeDAO = LiveTradeDAO(conn=sqlite3.connect(":memory:"))
    instance.ensure_tables()
    return instance


@pytest.fixture
def mode_state(dao: LiveTradeDAO) -> TradingModeState:
    return TradingModeState(dao=dao, run_id="run1", now_provider=lambda: NOW)


def make_manager(
    mode_state: TradingModeState,
    dao: LiveTradeDAO,
    tmp_path: Path,
    config: Optional[RiskConfig] = None,
    clock: Optional[List[datetime.datetime]] = None,
) -> PreTradeRiskManager:
    """建立風控；kill switch 指向一個不存在的暫存檔"""

    def now() -> datetime.datetime:
        return clock[0] if clock else NOW

    return PreTradeRiskManager(
        mode_state=mode_state,
        config=config,
        dao=dao,
        run_id="run1",
        kill_switch_path=tmp_path / "KILL_SWITCH",
        now_provider=now,
    )


def test_kill_switch_blocks_everything(
    mode_state: TradingModeState, dao: LiveTradeDAO, tmp_path: Path
) -> None:
    """
    kill switch 一旦生效，連平倉都不送，並把帳戶層降到 `HALTED`

    **每張單送出前都檢查**：只在啟動時檢查等於沒有 kill switch。
    """

    manager: PreTradeRiskManager = make_manager(mode_state, dao, tmp_path)
    (tmp_path / "KILL_SWITCH").touch()

    decision: RiskDecision = manager.check(
        make_order(), "A", CAPITAL, amount=100_000, reference_price=1000.0
    )

    assert decision.passed is False
    assert decision.category == "KILL_SWITCH"
    assert mode_state.account_mode is TradingMode.HALTED


def test_unreadable_kill_switch_counts_as_on(
    mode_state: TradingModeState, dao: LiveTradeDAO, tmp_path: Path
) -> None:
    """
    檔案系統查不了時**當成生效**

    這是安全開關，不確定就停。反過來會在最需要它的時候放行。
    """

    manager: PreTradeRiskManager = make_manager(mode_state, dao, tmp_path)

    class Exploding:
        def exists(self) -> bool:
            raise OSError("檔案系統無回應")

    manager.kill_switch_path = Exploding()  # type: ignore[assignment]

    assert manager.is_kill_switch_on() is True


def test_reduce_only_blocks_opens_but_not_closes(
    mode_state: TradingModeState, dao: LiveTradeDAO, tmp_path: Path
) -> None:
    """`REDUCE_ONLY` 下平倉單照過"""

    manager: PreTradeRiskManager = make_manager(mode_state, dao, tmp_path)
    mode_state.degrade(TradingMode.REDUCE_ONLY, "對帳不一致")

    opening: RiskDecision = manager.check(
        make_order(), "A", CAPITAL, amount=100_000, reference_price=1000.0
    )
    closing: RiskDecision = manager.check(
        make_order(action=Action.SELL),
        "A",
        CAPITAL,
        amount=100_000,
        reference_price=1000.0,
    )

    assert opening.passed is False
    assert closing.passed is True


def test_rejection_is_written_to_the_risk_event_table(
    mode_state: TradingModeState, dao: LiveTradeDAO, tmp_path: Path
) -> None:
    """每次拒單都要留下紀錄，包含觸發的規則"""

    manager: PreTradeRiskManager = make_manager(mode_state, dao, tmp_path)
    manager.check(
        make_order(volume=999), "A", CAPITAL, amount=100_000, reference_price=1000.0
    )

    row = dao.conn.execute(
        "SELECT category, severity, strategy_name FROM live_risk_event"
    ).fetchone()

    assert row == ("VOLUME_CAP", "WARN", "A")


def test_rejected_orders_do_not_consume_the_daily_budget(
    mode_state: TradingModeState, dao: LiveTradeDAO, tmp_path: Path
) -> None:
    """
    被拒的單沒有送出去，不該佔額度

    佔了的話，一連串被拒的單會把當日額度吃光，真正該送的反而送不出去。
    """

    manager: PreTradeRiskManager = make_manager(mode_state, dao, tmp_path)
    manager.check(
        make_order(volume=999), "A", CAPITAL, amount=500_000, reference_price=1000.0
    )

    assert manager.daily_amount.get("A", 0.0) == 0.0


def test_order_rate_degrades_the_strategy(
    mode_state: TradingModeState, dao: LiveTradeDAO, tmp_path: Path
) -> None:
    """
    頻率超標**降級**而不只是拒單

    每分鐘送出幾十張單通常是迴圈 bug，而 bug 不會因為被拒一次就停下來。
    """

    config: RiskConfig = RiskConfig(max_orders_per_minute=3)
    manager: PreTradeRiskManager = make_manager(mode_state, dao, tmp_path, config)

    decisions: List[RiskDecision] = [
        manager.check(make_order(), "A", CAPITAL, amount=1000, reference_price=1000.0)
        for _ in range(4)
    ]

    assert [d.passed for d in decisions] == [True, True, True, False]
    assert mode_state.effective_mode("A") is TradingMode.REDUCE_ONLY
    assert mode_state.effective_mode("B") is TradingMode.NORMAL


def test_order_rate_window_slides(
    mode_state: TradingModeState, dao: LiveTradeDAO, tmp_path: Path
) -> None:
    """頻率是滑動視窗：一分鐘前的單不該還佔著名額"""

    clock: List[datetime.datetime] = [NOW]
    config: RiskConfig = RiskConfig(max_orders_per_minute=2)
    manager: PreTradeRiskManager = make_manager(
        mode_state, dao, tmp_path, config, clock
    )

    manager.check(make_order(), "A", CAPITAL, amount=1000, reference_price=1000.0)
    manager.check(make_order(), "A", CAPITAL, amount=1000, reference_price=1000.0)
    clock[0] = NOW + datetime.timedelta(seconds=61)

    assert (
        manager.check(
            make_order(), "A", CAPITAL, amount=1000, reference_price=1000.0
        ).passed
        is True
    )


def test_strategy_daily_loss_only_degrades_that_strategy(
    mode_state: TradingModeState, dao: LiveTradeDAO, tmp_path: Path
) -> None:
    """策略層虧損上限只降那一支"""

    manager: PreTradeRiskManager = make_manager(mode_state, dao, tmp_path)

    assert manager.check_daily_loss("A", 31_000, CAPITAL) is True
    assert mode_state.effective_mode("A") is TradingMode.REDUCE_ONLY
    assert mode_state.effective_mode("B") is TradingMode.NORMAL


def test_account_daily_loss_degrades_everyone(
    mode_state: TradingModeState, dao: LiveTradeDAO, tmp_path: Path
) -> None:
    """帳戶層虧損上限降全體"""

    manager: PreTradeRiskManager = make_manager(mode_state, dao, tmp_path)

    assert manager.check_account_daily_loss(31_000, CAPITAL) is True
    assert mode_state.effective_mode("B") is TradingMode.REDUCE_ONLY


def test_degrade_event_from_other_components(
    mode_state: TradingModeState, dao: LiveTradeDAO, tmp_path: Path
) -> None:
    """
    其他元件只送降級事件，不自己改模式

    **對帳差異一律走帳戶層**：差異無法歸因到單支策略，猜錯的代價是讓
    真正有問題的那支繼續交易。
    """

    manager: PreTradeRiskManager = make_manager(mode_state, dao, tmp_path)
    manager.on_degrade_event("對帳不一致")

    assert mode_state.account_mode is TradingMode.REDUCE_ONLY
    assert (
        dao.conn.execute(
            "SELECT COUNT(*) FROM live_risk_event WHERE category = 'DEGRADE'"
        ).fetchone()[0]
        == 1
    )


def test_futures_volume_cap_is_applied(
    mode_state: TradingModeState, dao: LiveTradeDAO, tmp_path: Path
) -> None:
    """期貨走口數上限，不是張數上限"""

    manager: PreTradeRiskManager = make_manager(mode_state, dao, tmp_path)
    order: FuturesOrder = FuturesOrder(
        product="TX", expiry="202601", volume=6, price=20000.0
    )

    decision: RiskDecision = manager.check(
        order, "A", CAPITAL, amount=100_000, reference_price=20000.0, is_futures=True
    )

    assert decision.passed is False
    assert decision.category == "VOLUME_CAP"


def test_batch_check_records_truncations(
    mode_state: TradingModeState, dao: LiveTradeDAO, tmp_path: Path
) -> None:
    """截斷的單也要留下紀錄：盤後要回答「為什麼那張單沒送」"""

    config: RiskConfig = RiskConfig(total_exposure_ratio=0.1)
    manager: PreTradeRiskManager = make_manager(mode_state, dao, tmp_path, config)
    items: List[ExposureItem] = [ExposureItem(make_order(), 500_000)]

    allowed: List[ExposureItem] = manager.check_batch(items, "A", CAPITAL)

    assert allowed == []
    assert (
        dao.conn.execute(
            "SELECT COUNT(*) FROM live_risk_event WHERE category = 'BATCH_EXPOSURE'"
        ).fetchone()[0]
        == 1
    )


def test_pure_checks_need_no_manager_instance() -> None:
    """
    純判定不必建立 `PreTradeRiskManager`

    這是刻意的：日後若要讓回測事前評估「會被風控擋掉多少」，重用的就是這幾條，
    綁死在長駐狀態上就得再搬一次。
    """

    config: RiskConfig = RiskConfig()
    symbol_exposure: Dict[str, float] = {}

    assert check_single_order_amount(1.0, CAPITAL, config).passed is True
    assert check_volume_cap(1, False, config).passed is True
    assert truncate_batch_by_exposure([], 0.0, symbol_exposure, CAPITAL, config) == (
        [],
        [],
    )

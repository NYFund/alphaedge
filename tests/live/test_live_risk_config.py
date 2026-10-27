import datetime
import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List

import pytest

from core.config.live_risk import LiveRiskOverrides, load_live_risk_overrides
from core.dao.tw.live_trade_dao import LiveTradeDAO
from core.live.factory import build_live_trader, resolve_strategy_risk_config
from core.live.risk.risk_manager import ExposureItem, truncate_batch_by_exposure
from core.live.trader import LiveTrader
from core.models import FuturesOrder
from core.models.risk_config import (
    AccountRiskConfig,
    BaseRiskConfig,
    FuturesRiskConfig,
    StockRiskConfig,
    default_risk_config,
)
from core.utils import Action, InstrumentType, PositionType

from .conftest import FakeBroker
from .test_live_factory_and_entry import LiveFuturesStrategy, LiveStockStrategy

"""
逐策略的風控門檻：策略宣告 → 部署設定收緊 → 實際套用

三層的關係是「程式寫死的全域上限 ⊇ 策略宣告 ⊇ 部署設定」。這裡擋的是三種安靜的失效：
部署設定寫錯而被略過（以為收緊了其實沒有）、股票策略宣告了期貨的設定（欄位一條都用不到）、
期貨以股票的曝險口徑計算（一口台指期的契約價值就超過額度，所有期貨單都被擋）。
"""

NOW: datetime.datetime = datetime.datetime(2026, 9, 20, 13, 30)
CAPITAL: float = 3_000_000.0


@pytest.fixture(autouse=True)
def in_memory_history_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """資料源建構時不碰正式的歷史資料庫，一律改連 in-memory SQLite"""

    def connect_in_memory(db_path: Any, read_only: bool = False) -> sqlite3.Connection:
        return sqlite3.connect(":memory:")

    monkeypatch.setattr("core.datafeed.base.connect_sqlite", connect_in_memory)


def write_toml(tmp_path: Path, text: str) -> Path:
    """把設定內容寫成暫存的 TOML 檔"""

    path: Path = tmp_path / "live_risk.toml"
    path.write_text(text, encoding="utf-8")
    return path


def futures_order(volume: int = 1, price: float = 50000.0) -> FuturesOrder:
    """組一張 TX 期貨買單"""

    return FuturesOrder(
        product="TX",
        expiry="202611",
        action=Action.BUY,
        position_type=PositionType.LONG,
        volume=volume,
        price=price,
    )


# === 設定本身 ===
def test_overrides_can_only_tighten() -> None:
    """收緊照套；放寬當場報錯——放寬要回到策略宣告、留下 commit 與理由"""

    config: FuturesRiskConfig = FuturesRiskConfig()

    tightened: FuturesRiskConfig = config.with_overrides({"max_leverage": 3.0})
    assert tightened.max_leverage == 3.0
    assert config.max_leverage == 4.0, "原設定是 frozen，不可被改動"

    with pytest.raises(ValueError, match="只能收緊"):
        config.with_overrides({"max_leverage": 5.0})


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_levrage": 3.0},  # 打錯字
        {"total_exposure_ratio": 0.5},  # 股票才有的欄位
        {"max_leverage": "3"},  # 不是數值
        {"max_leverage": True},  # bool 是 int 的子類，不可當數值放行
    ],
)
def test_bad_overrides_are_rejected(overrides: Dict[str, Any]) -> None:
    """
    寫錯的部署設定當場報錯，不略過

    略過的話，以為收緊了其實沒有，那比沒有設定更危險。
    """

    with pytest.raises(ValueError, match="部署設定不合法"):
        FuturesRiskConfig().with_overrides(overrides)


def test_shared_fields_have_the_same_name_in_both_markets() -> None:
    """
    同性質的欄位兩個市場同名（計量不同寫在 docstring），各自只多出本質不同的那幾條

    命名不對稱的話，策略作者換市場時得重新查一次欄位名，部署設定也要兩套寫法。
    """

    from dataclasses import fields

    stock = {field.name for field in fields(StockRiskConfig)}
    futures = {field.name for field in fields(FuturesRiskConfig)}
    shared = {field.name for field in fields(BaseRiskConfig)}

    assert shared <= stock and shared <= futures
    assert stock - shared == {"total_exposure_ratio", "single_symbol_exposure_ratio"}
    assert futures - shared == {"max_leverage"}


def test_base_config_cannot_be_built_directly() -> None:
    """共用基底沒有全域上限，直接建立的話每個欄位都不受上限約束"""

    with pytest.raises(TypeError, match="StockRiskConfig"):
        BaseRiskConfig()


def test_default_config_follows_the_instrument_type() -> None:
    """沒有宣告時依商品類別取預設；沒有對應的商品類別直接報錯，不猜"""

    assert isinstance(default_risk_config(InstrumentType.STOCK), StockRiskConfig)
    assert isinstance(default_risk_config(InstrumentType.FUTURE), FuturesRiskConfig)
    with pytest.raises(ValueError, match="沒有對應的實盤風控設定"):
        default_risk_config(None)


# === 期貨曝險 ===
def test_futures_exposure_uses_the_leverage_cap() -> None:
    """
    期貨的批次曝險只有名目槓桿一條：總契約價值 ≤ 額度 × `max_leverage`

    股票那兩條（總曝險 1 倍、單一標的 35%）套在契約價值上，一口台指期就超過 300 萬額度。
    """

    config: FuturesRiskConfig = FuturesRiskConfig(max_leverage=4.0)
    one_lot: float = 50000.0 * 200  # 1,000 萬，約 3.3 倍
    items: List[ExposureItem] = [
        ExposureItem(futures_order(), one_lot),
        ExposureItem(futures_order(), one_lot),
    ]

    allowed, truncated = truncate_batch_by_exposure(items, 0.0, {}, CAPITAL, config)

    assert len(allowed) == 1
    assert "總契約價值" in truncated[0][1]


# === 部署設定檔 ===
def test_missing_file_means_no_tightening(tmp_path: Path) -> None:
    """檔案不存在不是錯誤：只用策略宣告與預設值"""

    overrides: LiveRiskOverrides = load_live_risk_overrides(tmp_path / "none.toml")

    assert overrides.account == {}
    assert overrides.strategies == {}


def test_file_is_split_into_account_and_strategy_sections(tmp_path: Path) -> None:
    path: Path = write_toml(
        tmp_path,
        """
[account]
daily_loss_ratio = 0.02

[LiveFuturesStrategy]
max_leverage = 3.0
""",
    )

    overrides: LiveRiskOverrides = load_live_risk_overrides(path)

    assert overrides.account == {"daily_loss_ratio": 0.02}
    assert overrides.for_strategy("LiveFuturesStrategy") == {"max_leverage": 3.0}
    assert overrides.for_strategy("Other") == {}


@pytest.mark.parametrize(
    "text",
    [
        "max_leverage = 3.0\n",  # 沒寫在任何段落裡
        "[LiveFuturesStrategy\nmax_leverage = 3.0\n",  # TOML 語法錯
    ],
)
def test_malformed_file_is_an_error(tmp_path: Path, text: str) -> None:
    with pytest.raises(ValueError):
        load_live_risk_overrides(write_toml(tmp_path, text))


# === 組裝層 ===
def test_strategy_declaration_and_deployment_tightening_are_combined() -> None:
    """策略宣告的值為底，部署設定再收緊；部署設定比策略宣告寬也算放寬"""

    strategy: LiveFuturesStrategy = LiveFuturesStrategy()
    strategy.live_risk_config = FuturesRiskConfig(max_leverage=3.0)

    config: BaseRiskConfig = resolve_strategy_risk_config(
        "LiveFuturesStrategy",
        strategy,
        LiveRiskOverrides({}, {"LiveFuturesStrategy": {"max_order_volume": 2}}),
    )

    assert isinstance(config, FuturesRiskConfig)
    assert (config.max_leverage, config.max_order_volume) == (3.0, 2)

    with pytest.raises(ValueError, match="LiveFuturesStrategy"):
        resolve_strategy_risk_config(
            "LiveFuturesStrategy",
            strategy,
            LiveRiskOverrides({}, {"LiveFuturesStrategy": {"max_leverage": 3.5}}),
        )


def test_declaring_the_other_markets_config_is_rejected() -> None:
    """
    股票策略宣告 `FuturesRiskConfig`：欄位在股票的送單路徑一條都用不到，等於沒宣告

    啟動時就擋下，不讓它以預設值靜靜地跑。
    """

    strategy: LiveStockStrategy = LiveStockStrategy()
    strategy.live_risk_config = FuturesRiskConfig(max_leverage=2.0)

    with pytest.raises(ValueError, match="StockRiskConfig"):
        resolve_strategy_risk_config(
            "LiveStockStrategy", strategy, LiveRiskOverrides({}, {})
        )


def build(
    dao: LiveTradeDAO, live_risk_path: Path, strategies: Any = None
) -> LiveTrader:
    """組一個期貨行程（股票與期貨的尾盤時窗沒有交集，不能同一個行程）"""

    return build_live_trader(
        strategies if strategies is not None else [LiveFuturesStrategy()],
        broker=FakeBroker(),
        dao=dao,
        run_id="20260920133000",
        live_risk_path=live_risk_path,
        now_provider=lambda: NOW,
    )


def test_each_strategy_gets_its_own_market_config(
    dao: LiveTradeDAO, tmp_path: Path
) -> None:
    """
    每支策略依自己的市場拿到門檻與單位

    期貨委託曾經被套上股票的張數上限（5 口上限變成 50）：數量上限由呼叫端另傳
    「是不是期貨」，送單路徑漏傳了。現在單位跟著每支策略的設定類別走。
    """

    path: Path = write_toml(tmp_path, "[LiveFuturesStrategy]\nmax_order_volume = 2\n")
    futures: BaseRiskConfig = build(dao, path).contexts[0].risk_limits
    stock: BaseRiskConfig = (
        build(dao, path, [LiveStockStrategy()]).contexts[0].risk_limits
    )

    assert isinstance(futures, FuturesRiskConfig)
    assert futures.max_order_volume == 2
    assert isinstance(stock, StockRiskConfig)
    assert stock.max_order_volume == 50


def test_account_section_reaches_the_risk_manager(
    dao: LiveTradeDAO, tmp_path: Path
) -> None:
    path: Path = write_toml(tmp_path, "[account]\ndaily_loss_ratio = 0.02\n")
    trader: LiveTrader = build(dao, path)

    assert trader.risk_manager.account_config == AccountRiskConfig(
        daily_loss_ratio=0.02
    )


def test_effective_limits_are_recorded_on_the_run(
    dao: LiveTradeDAO, tmp_path: Path
) -> None:
    """
    實際生效的門檻寫進 `live_run.risk_config_json`

    拒單數突然變了時要能回答「那天的上限是多少」；這一欄先前從未寫入。
    """

    path: Path = write_toml(tmp_path, "[LiveFuturesStrategy]\nmax_leverage = 3.0\n")
    build(dao, path)

    raw: str = dao.conn.execute("SELECT risk_config_json FROM live_run").fetchone()[0]
    recorded: Dict[str, Any] = json.loads(raw)

    assert (
        recorded["account"]["daily_loss_ratio"] == AccountRiskConfig().daily_loss_ratio
    )
    futures: Dict[str, Any] = recorded["strategies"]["LiveFuturesStrategy"]
    assert (futures["class"], futures["max_leverage"]) == ("FuturesRiskConfig", 3.0)


def test_invalid_deployment_file_stops_the_start(
    dao: LiveTradeDAO, tmp_path: Path
) -> None:
    """設定不合法是啟動錯誤，且不留下沒有結束時間的執行紀錄（下次啟動會當成崩潰）"""

    path: Path = write_toml(tmp_path, "[LiveFuturesStrategy]\nmax_leverage = 9.0\n")

    with pytest.raises(ValueError, match="只能收緊"):
        build(dao, path)

    assert dao.conn.execute("SELECT COUNT(*) FROM live_run").fetchone()[0] == 0

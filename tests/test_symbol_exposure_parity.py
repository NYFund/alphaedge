from typing import Dict, List, Optional, Tuple

import pytest

from core.execution.order_preprocess import exceeds_symbol_exposure
from core.live.risk.risk_config import RiskConfig
from core.live.risk.risk_manager import ExposureItem, truncate_batch_by_exposure
from core.models import StockOrder
from core.utils import Action, PositionType

"""
單一標的曝險上限：回測與實盤共用同一條公式

同一個概念「單一標的曝險不得超過本金的某個比例」原本兩邊各寫一份。
公式是同一條，但**四個面向不同**，而差在哪一直沒有寫下來：

| | 回測 | 實盤 |
|---|---|---|
| 設定 | `ShortConstraint.max_short_exposure_ratio` | `RiskConfig.single_symbol_exposure_ratio` |
| 預設 | `None`（關閉） | `0.25` |
| 適用 | **只有空單** | 多空皆適用 |
| 超限行為 | 整筆拒絕開倉 | 批次截斷 |

本檔釘住兩件事：**公式一致**，以及**那四個差異確實存在**——
把差異當成 bug 順手「修掉」會改變回測結果、讓回歸基準失效。
"""


INIT_CAPITAL: float = 1_000_000.0


def make_order(symbol: str = "2330") -> StockOrder:
    """開倉單；曝險檢查只看金額與標的"""

    return StockOrder(
        stock_id=symbol,
        action=Action.BUY,
        position_type=PositionType.LONG,
        price=100.0,
        volume=1,
    )


# === 公式一致 ===
@pytest.mark.parametrize(
    ("position_value", "ratio", "expected"),
    [
        (250_000.0, 0.25, False),  # 恰好等於上限：不算超過
        (250_001.0, 0.25, True),
        (100_000.0, 0.25, False),
        (1.0, 0.0, True),  # ratio 0 ＝ 一毛都不准
        (0.0, 0.0, False),
        (10_000_000.0, None, False),  # None ＝ 不限制
    ],
)
def test_formula_boundaries(
    position_value: float, ratio: Optional[float], expected: bool
) -> None:
    """
    邊界：**等於上限不算超過**

    兩邊原本都寫 `>` 而不是 `>=`，抽取時若改成 `>=`，恰好打滿額度的單會被擋，
    而那是完全合法的部位。
    """

    assert exceeds_symbol_exposure(position_value, INIT_CAPITAL, ratio) is expected


def test_none_means_unlimited_like_max_holdings() -> None:
    """
    `None` ＝ 不限制，與 `check_max_holdings()` 的 `max_holdings` 同一套語意

    同一份程式裡兩種「沒有設定」的語意（一個當 0、一個當無限）是很貴的錯誤：
    寫成 0 的那一邊會把每一張單都擋掉，而且不會有任何錯誤訊息。
    """

    assert exceeds_symbol_exposure(1e12, INIT_CAPITAL, None) is False


def test_live_truncation_agrees_with_the_shared_formula() -> None:
    """
    實盤的批次截斷與共用公式判定一致

    同一組 `(曝險, 本金, 比例)` 餵進兩條路徑：純函式說超過的，
    批次截斷就要把它截掉；說沒超過的就要放行。
    """

    config: RiskConfig = RiskConfig()
    ratio: float = config.single_symbol_exposure_ratio
    cap: float = INIT_CAPITAL * ratio

    cases: List[Tuple[float, bool]] = [
        (cap - 1.0, False),
        (cap, False),
        (cap + 1.0, True),
    ]

    for amount, should_exceed in cases:
        assert exceeds_symbol_exposure(amount, INIT_CAPITAL, ratio) is should_exceed

        allowed, truncated = truncate_batch_by_exposure(
            [ExposureItem(make_order(), amount)],
            existing_exposure=0.0,
            existing_symbol_exposure={},
            init_capital=INIT_CAPITAL,
            config=config,
        )
        was_truncated: bool = bool(truncated)

        assert was_truncated is should_exceed, (
            f"金額 {amount:,.0f}：純函式說 {should_exceed}、批次截斷說 {was_truncated}"
        )
        assert bool(allowed) is not should_exceed


def test_existing_symbol_exposure_accumulates() -> None:
    """
    實盤比的是**累計**曝險，不是單張金額

    只看單張的話，同一檔分五張各打 20% 就能繞過 25% 的上限。
    """

    config: RiskConfig = RiskConfig()
    cap: float = INIT_CAPITAL * config.single_symbol_exposure_ratio

    allowed, truncated = truncate_batch_by_exposure(
        [ExposureItem(make_order(), cap / 2)],
        existing_exposure=0.0,
        # 這一檔已經佔掉一半的額度
        existing_symbol_exposure={"2330": cap * 0.6},
        init_capital=INIT_CAPITAL,
        config=config,
    )

    assert allowed == []
    assert len(truncated) == 1
    assert "單一標的上限" in truncated[0][1]


# === 四個差異確實存在（不是 bug，別順手改掉）===
def test_backtest_default_is_off_and_live_default_is_on() -> None:
    """
    預設值兩邊不同，而且**刻意不統一**

    統一會改變回測結果、須重產回歸基準。
    """

    from core.models.cost_config import ShortConstraint

    assert ShortConstraint().max_short_exposure_ratio is None, "回測預設不限制"
    assert RiskConfig().single_symbol_exposure_ratio == 0.25, "實盤預設 25%"


def test_backtest_only_applies_it_to_shorts() -> None:
    """
    回測只在 SHORT 分支取用這條限制——**這是待裁示的差異，不是刻意的**

    回測的多單曝險目前完全不受本條限制，只靠 `EqualWeightSizer` 的資金切分
    間接約束。統一適用範圍同樣會改回測結果，故未在此處理。
    釘住現況是為了讓它別在無人察覺的情況下被改掉。
    """

    import ast
    import inspect

    from core.managers.stock.position_manager import StockPositionManager

    # 以 AST 數**呼叫**，不是字串出現次數——註解裡提到它是正常的
    tree: ast.Module = ast.parse(inspect.getsource(StockPositionManager))
    calls: int = sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "exceeds_symbol_exposure"
    )

    assert calls == 1, f"回測端呼叫了 {calls} 次，預期只有 SHORT 分支那一次"


def test_the_difference_table_is_written_down() -> None:
    """
    四個面向的差異要寫在共用函式的 docstring 裡

    **本步驟的主要產出是「把差異釘住」，不是省那三行。**
    兩份實作永遠不會互相報錯，只會讓「回測跑得過的規模、實盤被截掉」，
    而差在哪沒有任何一處寫下來——那才是真正的成本。
    """

    doc: str = exceeds_symbol_exposure.__doc__ or ""

    for keyword in (
        "max_short_exposure_ratio",
        "single_symbol_exposure_ratio",
        "整筆拒絕",
        "批次截斷",
        "待裁示",
    ):
        assert keyword in doc, f"差異表少了「{keyword}」"


def test_both_sides_point_at_each_other() -> None:
    """
    兩邊的設定都要指得到對方

    只有一邊寫「另一邊也有一份」的話，從另一邊改進來的人看不到。
    """

    import inspect

    from core.models import cost_config

    assert "single_symbol_exposure_ratio" in inspect.getsource(cost_config)


def collect_ratio_multiplications() -> Dict[str, int]:
    """統計還有誰自己寫 `init_capital * ratio` 這條公式"""

    import pathlib

    root: pathlib.Path = pathlib.Path(__file__).resolve().parent.parent
    counts: Dict[str, int] = {}

    for path in list((root / "core").rglob("*.py")):
        text: str = path.read_text()
        hits: int = text.count("init_capital * max_ratio")  # 舊公式的字面形式
        if hits:
            counts[str(path.relative_to(root))] = hits

    return counts


def test_nobody_reimplements_the_formula() -> None:
    """公式只有一份；`init_capital * max_ratio` 不該再出現在別處"""

    assert collect_ratio_multiplications() == {}

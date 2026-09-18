import math
from typing import List, Optional

import pytest

from core.backtest.analysis.performance_metrics import (
    TRADING_DAYS_PER_YEAR,
    compute_annualized_information_ratio,
    compute_annualized_sharpe,
    compute_annualized_sortino,
    compute_annualized_volatility,
    compute_drawdown_series,
    compute_max_drawdown,
    compute_period_returns,
    compute_profit_factor,
    compute_win_loss_ratio,
)

"""
風險調整後報酬的公式

舊版的四個問題都會讓數字看起來合理但實際錯誤：分子用百分比、分母用小數；
沒有年化；以「每筆交易」為樣本；Sortino 的下檔標準差算的是低於門檻那幾期
**彼此之間**的離散度。
"""


def test_period_returns_from_equity_curve() -> None:
    """逐期報酬率是小數，不是百分比"""

    returns: List[float] = compute_period_returns([100.0, 110.0, 99.0])

    assert returns == pytest.approx([0.1, -0.1])


def test_period_returns_skip_non_positive_base() -> None:
    """前一期權益為 0 或負數時該期沒有定義；補 0 會讓破產的帳戶看起來很平穩"""

    assert compute_period_returns([0.0, 100.0, 110.0]) == pytest.approx([0.1])


def test_sharpe_matches_hand_calculation() -> None:
    """
    手算對照：報酬率 [1%, -1%, 1%, -1%]、無風險利率 0

    平均 0、標準差 0.0115470…（ddof=1）→ Sharpe 恰為 0。
    """

    returns: List[float] = [0.01, -0.01, 0.01, -0.01]

    assert compute_annualized_sharpe(returns, risk_free_rate=0.0) == 0.0


def test_sharpe_is_annualized() -> None:
    """年化係數是 √252；未年化的數字無法與任何外部參考比較"""

    returns: List[float] = [0.01, 0.02, 0.01, 0.02]

    mean: float = sum(returns) / len(returns)
    variance: float = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    expected: float = round(
        mean / math.sqrt(variance) * math.sqrt(TRADING_DAYS_PER_YEAR), 4
    )

    assert compute_annualized_sharpe(returns, risk_free_rate=0.0) == expected


def test_risk_free_rate_is_converted_to_a_single_period() -> None:
    """
    無風險利率與報酬率同為小數，且先換算成單期再相減

    舊版分子是百分比（例如 2.31）、分母的無風險利率是 0.02，兩者差 100 倍，
    等於幾乎沒有扣無風險利率。
    """

    returns: List[float] = [0.01, 0.02, 0.01, 0.02]

    with_rf: Optional[float] = compute_annualized_sharpe(returns, risk_free_rate=0.02)
    without_rf: Optional[float] = compute_annualized_sharpe(returns, risk_free_rate=0.0)

    assert with_rf is not None and without_rf is not None
    assert with_rf < without_rf, "扣掉無風險利率之後應該變小"


def test_sharpe_returns_none_without_enough_samples() -> None:
    """
    零筆／一筆時回 None，不可回 0

    「沒有資料」與「風險調整後報酬為零」是兩件完全不同的事；
    舊版在零筆交易時是 `np.mean([])` → nan ＋ RuntimeWarning。
    """

    assert compute_annualized_sharpe([]) is None
    assert compute_annualized_sharpe([0.01]) is None


def test_sharpe_returns_none_when_there_is_no_variance() -> None:
    """報酬率完全不動時標準差為 0，Sharpe 無定義"""

    assert compute_annualized_sharpe([0.01, 0.01, 0.01]) is None


def test_sortino_only_penalises_downside() -> None:
    """上檔波動不進分母：同樣的平均報酬，正向波動大的 Sortino 應較高"""

    calm_upside: Optional[float] = compute_annualized_sortino(
        [0.01, 0.01, 0.01, -0.01], risk_free_rate=0.0
    )
    wild_upside: Optional[float] = compute_annualized_sortino(
        [0.05, -0.03, 0.01, -0.01], risk_free_rate=0.0
    )

    assert calm_upside is not None and wild_upside is not None
    assert calm_upside > wild_upside


def test_sortino_divides_by_the_full_sample() -> None:
    """
    分母是「低於門檻的偏差平方和 ÷ **全樣本**筆數」再開根號

    舊版對低於門檻的那幾期取 `np.std`，算的是它們彼此之間的離散度，與門檻無關。
    """

    returns: List[float] = [0.02, 0.02, 0.02, -0.02]

    mean: float = sum(returns) / len(returns)
    downside: float = math.sqrt(((-0.02) ** 2) / len(returns))
    expected: float = round(mean / downside * math.sqrt(TRADING_DAYS_PER_YEAR), 4)

    assert compute_annualized_sortino(returns, risk_free_rate=0.0) == expected


def test_sortino_returns_none_without_downside() -> None:
    """完全沒有下檔波動時 Sortino 無定義，回 None 而不是無限大"""

    assert compute_annualized_sortino([0.01, 0.02, 0.03], risk_free_rate=0.0) is None


# === Information Ratio===
def test_information_ratio_matches_hand_calculation() -> None:
    """
    手算對照：策略 [2%, -1%, 3%, 0%]、基準 [1%, -1%, 1%, 1%]

    主動報酬 [1%, 0%, 2%, -1%]，平均 0.5%、樣本標準差（ddof=1）
    √(((0.005)² + (0.005)² + (0.015)² + (0.015)²) / 3)，再乘 √252。
    """

    returns: List[float] = [0.02, -0.01, 0.03, 0.0]
    benchmark: List[float] = [0.01, -0.01, 0.01, 0.01]

    active: List[float] = [0.01, 0.0, 0.02, -0.01]
    mean: float = sum(active) / len(active)
    variance: float = sum((value - mean) ** 2 for value in active) / (len(active) - 1)
    expected: float = round(
        mean / math.sqrt(variance) * math.sqrt(TRADING_DAYS_PER_YEAR), 4
    )

    assert compute_annualized_information_ratio(returns, benchmark) == expected


def test_information_ratio_actually_depends_on_the_benchmark() -> None:
    """
    換一條基準就要換一個數字——這正是舊版做不到的事

    舊版的基準是寫死的常數 `0.0`，而**從每個樣本減去同一個常數不會改變分母**，
    分子則平移了同一個量，算出來恆等於 `mean(ROI) / std(ROI)`。所以只要基準的
    **波動型態**不同（不只是平均值不同），IR 就必須跟著變；若兩者仍相等，
    代表基準又被當成常數處理了。
    """

    returns: List[float] = [0.03, -0.01, 0.04, 0.02]
    flat: List[float] = [0.01, 0.01, 0.01, 0.01]  # 常數基準
    volatile: List[float] = [-0.01, 0.02, 0.0, 0.03]  # 同平均、但有波動

    assert sum(flat) == pytest.approx(sum(volatile))
    ir_flat: Optional[float] = compute_annualized_information_ratio(returns, flat)
    ir_volatile: Optional[float] = compute_annualized_information_ratio(
        returns, volatile
    )

    assert ir_flat != ir_volatile

    # 常數基準的追蹤誤差與策略自身的標準差相同（舊版恆等式的來源）
    assert ir_flat == compute_annualized_sharpe(returns, risk_free_rate=0.01 * 252)


def test_information_ratio_refuses_misaligned_series() -> None:
    """長度湊得起來不代表日期對得起來，長度不同一律拒絕而不是截頭去尾"""

    with pytest.raises(ValueError, match="長度不同"):
        compute_annualized_information_ratio([0.01, 0.02, 0.03], [0.01, 0.02])


def test_information_ratio_returns_none_without_tracking_error() -> None:
    """策略與基準完全同步時追蹤誤差為 0，IR 無定義"""

    assert compute_annualized_information_ratio([0.01, 0.02], [0.01, 0.02]) is None
    assert compute_annualized_information_ratio([0.01], [0.02]) is None


# === 最大回撤 ===
def test_max_drawdown_matches_hand_calculation() -> None:
    """自高點 120 跌到 90 ＝ −25%，取最深的那一次"""

    assert compute_max_drawdown([100.0, 120.0, 90.0, 110.0]) == pytest.approx(-25.0)


def test_max_drawdown_is_measured_from_the_running_peak() -> None:
    """
    基準是**歷史最高**權益，不是起點

    用起點當基準的話，一路上漲後的小回檔會算成 0，而那正是真實的回撤。
    """

    # 高點 200，之後跌到 150 ＝ −25%；若以起點 100 當基準會誤算成 +50%
    assert compute_max_drawdown([100.0, 200.0, 150.0]) == pytest.approx(-25.0)


def test_max_drawdown_is_zero_when_it_only_goes_up() -> None:
    """從未回撤回 0.0——與「沒有資料」（None）是兩件事"""

    assert compute_max_drawdown([100.0, 110.0, 120.0]) == 0.0
    assert compute_max_drawdown([]) is None


# === 年化波動度 ===
def test_annualized_volatility_matches_hand_calculation() -> None:
    """樣本標準差（ddof=1）× √252 × 100"""

    returns: List[float] = [0.01, -0.01, 0.01, -0.01]
    expected: float = 0.011547005383792515 * math.sqrt(TRADING_DAYS_PER_YEAR) * 100

    assert compute_annualized_volatility(returns) == pytest.approx(expected, abs=0.01)


def test_annualized_volatility_uses_the_sample_standard_deviation() -> None:
    """
    用樣本標準差（除以 n−1）不是母體標準差（除以 n）

    母體公式會系統性低估波動度，樣本數少時低估得特別明顯。
    """

    returns: List[float] = [0.02, -0.02]
    population: float = 0.02 * math.sqrt(TRADING_DAYS_PER_YEAR) * 100

    # 兩筆樣本時，樣本標準差是母體標準差的 √2 倍
    assert compute_annualized_volatility(returns) == pytest.approx(
        population * math.sqrt(2), abs=0.01
    )


def test_annualized_volatility_returns_none_without_enough_samples() -> None:
    """不足兩期沒有離散度可言"""

    assert compute_annualized_volatility([0.01]) is None
    assert compute_annualized_volatility([]) is None


# === 獲利因子與勝敗比 ===
def test_profit_factor_matches_hand_calculation() -> None:
    """總獲利 18 ÷ |總虧損| 5 ＝ 3.6"""

    assert compute_profit_factor([10.0, -5.0, 8.0]) == pytest.approx(3.6)


def test_profit_factor_returns_none_without_losses() -> None:
    """
    沒有虧損筆數回 None，**不可回 0**

    「從沒虧過」與「獲利因子為零」意思完全相反，用 0 表示會讓排序與門檻篩選
    把最好的策略當成最差的。
    """

    assert compute_profit_factor([10.0, 8.0]) is None
    assert compute_profit_factor([]) is None


def test_win_loss_ratio_matches_hand_calculation() -> None:
    """獲利 2 筆 ÷ 虧損 1 筆 ＝ 2.0"""

    assert compute_win_loss_ratio([10.0, -5.0, 8.0]) == pytest.approx(2.0)


def test_win_loss_ratio_ignores_break_even_trades() -> None:
    """損益為 0 的筆數兩邊都不算——平盤出場既不算贏也不算輸"""

    assert compute_win_loss_ratio([10.0, -5.0, 0.0]) == pytest.approx(1.0)


def test_win_loss_ratio_returns_none_without_losses() -> None:
    """零虧損筆數回 None，理由同獲利因子"""

    assert compute_win_loss_ratio([10.0, 8.0]) is None
    assert compute_win_loss_ratio([]) is None


def test_max_drawdown_is_the_minimum_of_the_drawdown_series() -> None:
    """
    純量與序列是**同一條公式**

    畫圖要整條序列、報表要最深的那一點；兩處各寫一次必然漂移——
    MDD 曾經就有 reporter 與前端兩份實作，而且沒有任何測試盯住兩者一致。
    """

    equity: List[float] = [100.0, 120.0, 90.0, 110.0, 80.0]
    series: List[float] = compute_drawdown_series(equity)

    assert len(series) == len(equity)
    assert series[0] == 0.0  # 起點必為 0（它自己就是當下的高點）
    assert compute_max_drawdown(equity) == pytest.approx(round(min(series), 2))


def test_drawdown_series_is_zero_before_any_peak_is_lost() -> None:
    """一路上漲時整條序列都是 0"""

    assert compute_drawdown_series([100.0, 110.0, 120.0]) == [0.0, 0.0, 0.0]

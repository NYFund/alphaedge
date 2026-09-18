import math
from typing import List, Optional, Sequence

"""
風險調整後報酬的共用公式

**單獨成檔、只相依 math 與 typing**：前端映像不安裝整個後端，只 COPY 這一檔來算
Sharpe 與 Sortino。日後 reporter 要輸出同樣的指標時也必須呼叫這裡——同一個指標
寫在兩個地方，最後一定會出現「報表說 1.2、前端說 0.8」而沒有人知道哪個對。

直接拿「每筆交易的 ROI」算 Sharpe／Sortino 的常見寫法有四個問題，四個都會讓
數字看起來合理但實際錯誤：

1. **單位不一致**：分子是 `record.roi`（百分比，例如 `2.31`），分母的無風險
   利率是 `0.02`（小數）。兩者差 100 倍，相減等於幾乎沒有扣無風險利率。
2. **沒有年化**：Sharpe 的慣例是年化值，未年化的數字無法與任何外部參考比較。
3. **以「每筆交易」為樣本**：交易筆數與時間無關，一年交易 5 次與 500 次算出
   的「波動度」不可比，年化更無從談起。應以**日報酬**為樣本。
4. **Sortino 的下檔標準差算錯**：對「低於門檻的那些報酬」取 `np.std`，
   那是它們**彼此之間**的離散度；正確定義是相對於門檻的偏差平方，
   除以**全樣本**筆數再開根號。
"""


# 一年的交易日數；年化係數為 √TRADING_DAYS_PER_YEAR
TRADING_DAYS_PER_YEAR: int = 252


def compute_period_returns(equity_curve: Sequence[float]) -> List[float]:
    """
    - Description:
        由權益曲線算出逐期簡單報酬率（小數，非百分比）

        前一期權益為 0 或負數時跳過該期：報酬率在那裡沒有定義，
        補 0 會讓破產的帳戶看起來「那天很平穩」。
    - Parameters:
        - equity_curve: Sequence[float]
            權益序列（第一筆通常是初始資金）
    - Return:
        - List[float]
            逐期報酬率
    """

    returns: List[float] = []
    for previous, current in zip(equity_curve, equity_curve[1:]):
        if previous > 0:
            returns.append(current / previous - 1)
    return returns


def compute_annualized_sharpe(
    returns: Sequence[float],
    risk_free_rate: float = 0.0,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> Optional[float]:
    """
    - Description:
        年化 Sharpe ratio

        `(平均超額報酬 / 超額報酬標準差) × √periods_per_year`。
        **無風險利率與報酬率同為小數**，且先換算成單期再相減。
    - Parameters:
        - returns: Sequence[float]
            逐期報酬率（小數）
        - risk_free_rate: float
            年化無風險利率（小數，例如 0.02 表示 2%）
        - periods_per_year: int
            一年幾期
    - Return:
        - Optional[float]
            年化 Sharpe；樣本不足兩期或標準差為 0 時為 None
            （零筆交易回 None 而不是 0——「沒有資料」與「風險調整後報酬為零」
            是兩件完全不同的事）
    """

    if len(returns) < 2:
        return None

    period_rf: float = risk_free_rate / periods_per_year
    excess: List[float] = [value - period_rf for value in returns]

    mean: float = sum(excess) / len(excess)
    variance: float = sum((value - mean) ** 2 for value in excess) / (len(excess) - 1)
    std: float = math.sqrt(variance)

    if std == 0:
        return None

    return round(mean / std * math.sqrt(periods_per_year), 4)


def compute_annualized_sortino(
    returns: Sequence[float],
    risk_free_rate: float = 0.0,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> Optional[float]:
    """
    - Description:
        年化 Sortino ratio

        與 Sharpe 的差別只在分母：**只罰下檔波動**，且分母是
        「低於門檻的偏差平方和 ÷ **全樣本**筆數」再開根號——不是對低於門檻的
        那幾期取標準差（那是它們彼此之間的離散度，與門檻無關）。
    - Parameters:
        - returns: Sequence[float]
            逐期報酬率（小數）
        - risk_free_rate: float
            年化無風險利率（小數），同時作為下檔門檻（MAR）
        - periods_per_year: int
            一年幾期
    - Return:
        - Optional[float]
            年化 Sortino；樣本不足兩期或完全沒有下檔波動時為 None
    """

    if len(returns) < 2:
        return None

    period_rf: float = risk_free_rate / periods_per_year
    excess: List[float] = [value - period_rf for value in returns]

    mean: float = sum(excess) / len(excess)
    downside_sum: float = sum(min(value, 0.0) ** 2 for value in excess)
    downside_deviation: float = math.sqrt(downside_sum / len(excess))

    if downside_deviation == 0:
        return None

    return round(mean / downside_deviation * math.sqrt(periods_per_year), 4)


def compute_annualized_information_ratio(
    returns: Sequence[float],
    benchmark_returns: Sequence[float],
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> Optional[float]:
    """
    - Description:
        年化 Information ratio

        `(平均主動報酬 / 追蹤誤差) × √periods_per_year`，其中主動報酬是
        **逐期相減**的 `策略報酬 - 基準報酬`。

        舊版有三個問題，第三個讓這個指標
        完全失去意義：

        1. 樣本是**每筆交易**的 `record.roi` 而非時間序列，無從年化；
        2. 沒有年化；
        3. 基準是**寫死的 0.0**。從每一個樣本減去同一個常數不會改變分母，
           所以舊值恆等於 `mean(每筆 ROI) / std(每筆 ROI)`——
           那是報酬的穩定性，與「相對基準的超額報酬」無關。

        兩條序列必須**已由呼叫端對齊到同一組日期**：長度不同時直接
        `ValueError`，不做截頭去尾。長度湊得起來不代表日期對得起來，
        自作主張對齊只會讓錯位的比較看起來很正常。
    - Parameters:
        - returns: Sequence[float]
            策略的逐期報酬率（小數）
        - benchmark_returns: Sequence[float]
            基準的逐期報酬率（小數），與 `returns` 同期同長度
        - periods_per_year: int
            一年幾期
    - Return:
        - Optional[float]
            年化 IR；樣本不足兩期或追蹤誤差為 0 時為 None
    """

    if len(returns) != len(benchmark_returns):
        raise ValueError(
            f"策略與基準的報酬序列長度不同（{len(returns)} vs "
            f"{len(benchmark_returns)}），請先由呼叫端依日期對齊"
        )

    if len(returns) < 2:
        return None

    active: List[float] = [
        value - base for value, base in zip(returns, benchmark_returns)
    ]

    mean: float = sum(active) / len(active)
    variance: float = sum((value - mean) ** 2 for value in active) / (len(active) - 1)
    tracking_error: float = math.sqrt(variance)

    if tracking_error == 0:
        return None

    return round(mean / tracking_error * math.sqrt(periods_per_year), 4)


def compute_drawdown_series(equity_curve: Sequence[float]) -> List[float]:
    """
    - Description:
        逐點回撤序列（%，負值）：`當前權益 / 歷史最高權益 − 1`

        **與 `compute_max_drawdown()` 是同一條公式**：畫圖要的是整條序列、
        報表要的是最深的那一點，兩處各寫一次必然漂移——MDD 曾經就有
        reporter 與前端兩份實作，而且沒有任何測試盯住兩者一致。

        歷史高點為 0 或負數時該點記為 0：回撤比例在那裡沒有定義
        （與 `compute_period_returns()` 同一個理由）。
    - Parameters:
        - equity_curve: Sequence[float]
            權益序列
    - Return:
        - List[float]
            與輸入等長的回撤序列（%，負值）
    """

    series: List[float] = []
    peak: float = equity_curve[0] if equity_curve else 0.0

    for value in equity_curve:
        peak = max(peak, value)
        series.append(0.0 if peak <= 0 else (value / peak - 1) * 100)

    return series


def compute_max_drawdown(equity_curve: Sequence[float]) -> Optional[float]:
    """
    - Description:
        最大回撤（%，負值）＝ `compute_drawdown_series()` 的最小值

        **回傳負值**：−12.5 表示曾自高點下跌 12.5%，正負號本身帶著方向，
        呼叫端不必再猜要不要加負號。
    - Parameters:
        - equity_curve: Sequence[float]
            權益序列
    - Return:
        - Optional[float]
            最大回撤（%，負值）；序列為空時為 None。**從未回撤時回 0.0**
            ——那與「沒有資料」是兩件事
    """

    if not equity_curve:
        return None

    return round(min(compute_drawdown_series(equity_curve)), 2)


def compute_annualized_volatility(
    returns: Sequence[float],
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> Optional[float]:
    """
    - Description:
        年化波動度（%）

        `樣本標準差（ddof=1） × √periods_per_year × 100`。
        **用樣本標準差不是母體標準差**：手上的報酬序列是樣本，母體公式
        （除以 n）會系統性低估波動度，樣本數少時低估得特別明顯。
    - Parameters:
        - returns: Sequence[float]
            逐期報酬率（小數）
        - periods_per_year: int
            一年幾期
    - Return:
        - Optional[float]
            年化波動度（%）；樣本不足兩期時為 None
    """

    if len(returns) < 2:
        return None

    mean: float = sum(returns) / len(returns)
    variance: float = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)

    return round(math.sqrt(variance) * math.sqrt(periods_per_year) * 100, 2)


def compute_profit_factor(pnls: Sequence[float]) -> Optional[float]:
    """
    - Description:
        獲利因子 ＝ 總獲利 ÷ |總虧損|

        > 1 代表整體獲利。**沒有虧損筆數時回 None 而不是 0 或無限大**：
        「從沒虧過」與「獲利因子為零」意思完全相反，用 0 表示會讓排序與
        門檻篩選把最好的策略當成最差的。
    - Parameters:
        - pnls: Sequence[float]
            逐筆已實現損益
    - Return:
        - Optional[float]
            獲利因子；沒有虧損筆數（含空序列）時為 None
    """

    gross_profit: float = sum(value for value in pnls if value > 0)
    gross_loss: float = sum(-value for value in pnls if value < 0)

    if gross_loss <= 0:
        return None

    return round(gross_profit / gross_loss, 4)


def compute_win_loss_ratio(pnls: Sequence[float]) -> Optional[float]:
    """
    - Description:
        勝敗比 ＝ 獲利筆數 ÷ 虧損筆數

        **與勝率是兩個指標**：勝率是「贏的比例」，勝敗比是「贏幾次輸一次」。
        損益為 0 的筆數不計入任何一邊（平盤出場既不算贏也不算輸）。

        零虧損筆數時回 None，理由同 `compute_profit_factor()`。
    - Parameters:
        - pnls: Sequence[float]
            逐筆已實現損益
    - Return:
        - Optional[float]
            勝敗比；沒有虧損筆數（含空序列）時為 None
    """

    wins: int = sum(1 for value in pnls if value > 0)
    losses: int = sum(1 for value in pnls if value < 0)

    if losses == 0:
        return None

    return round(wins / losses, 4)

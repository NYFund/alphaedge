from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

try:
    from config import (
        CHART_FILE_CANDIDATES,
        CSV_FILE_CANDIDATES,
        DAILY_EQUITY_FILE_CANDIDATES,
        DIRECTION_SUMMARY_FILE_CANDIDATES,
        EVENT_REPORT_FILE_CANDIDATES,
        METRICS_SUMMARY_FILE_CANDIDATES,
    )
except ModuleNotFoundError:
    from frontend.config import (
        CHART_FILE_CANDIDATES,
        CSV_FILE_CANDIDATES,
        DAILY_EQUITY_FILE_CANDIDATES,
        DIRECTION_SUMMARY_FILE_CANDIDATES,
        EVENT_REPORT_FILE_CANDIDATES,
        METRICS_SUMMARY_FILE_CANDIDATES,
    )

"""
回測報表的讀取與彙總

**前端只讀不算**：指標一律來自 reporter 已落地的 CSV，不在前端另寫一份公式。
舊版 `app.py` 自行重算，四個地方與 reporter 不同源：

1. `平均 ROI` 把已是百分比的 `ROI` 欄再乘 100（實測 0.82% 顯示成 82.1%）。
2. 資產曲線與日報酬以 `Sell Date` 排序——**SHORT 的 `Sell Date` 是開倉日**，
   曲線因此不是依平倉順序長出來的。
3. 權益用「已實現損益的累積餘額」而不是報表的盯市 `daily_equity`，
   持倉期間的逆勢被整段抹平，MDD 被低估。
4. Information Ratio 讀的 benchmark 欄位在報表裡根本不存在。

本模組只做**純資料讀取與彙總**，不含任何 Streamlit 呼叫，這樣才測得到
（`frontend/app.py` 在 import 時就會執行 Streamlit 的版面設定，無法在測試裡 import）。
"""

# 交易明細一律以**平倉日**排序：SHORT 的 `Sell Date` 是開倉日，
# 拿它排序等於把放空的曲線畫反
EXIT_DATE_COLUMN: str = "Exit Date"

# `daily_equity.csv` 的兩個欄位
EQUITY_DATE_COLUMN: str = "Date"
EQUITY_COLUMN: str = "Equity"


@dataclass
class BacktestReport:
    """一個策略資料夾裡 reporter 落地的全部產出"""

    strategy_name: str
    report_dir: Path
    csv_path: Optional[Path]
    chart_paths: Dict[str, Optional[Path]]
    daily_equity_path: Optional[Path] = None
    direction_summary_path: Optional[Path] = None
    event_report_path: Optional[Path] = None
    metrics_summary_path: Optional[Path] = None


def list_strategy_dirs(results_root: Path) -> List[Path]:
    if not results_root.exists() or not results_root.is_dir():
        return []
    excluded_dir_names = {"logs"}
    return sorted(
        [
            path
            for path in results_root.iterdir()
            if path.is_dir() and path.name.lower() not in excluded_dir_names
        ],
        key=lambda path: path.name.lower(),
    )


def _pick_first_match(base_dir: Path, patterns: List[str]) -> Optional[Path]:
    for pattern in patterns:
        matched = sorted(base_dir.glob(pattern))
        if matched:
            return matched[0]
    return None


def load_backtest_report(strategy_dir: Path) -> BacktestReport:
    csv_path = _pick_first_match(strategy_dir, CSV_FILE_CANDIDATES)
    chart_paths = {
        chart_name: _pick_first_match(strategy_dir, patterns)
        for chart_name, patterns in CHART_FILE_CANDIDATES.items()
    }
    return BacktestReport(
        strategy_name=strategy_dir.name,
        report_dir=strategy_dir,
        csv_path=csv_path,
        chart_paths=chart_paths,
        daily_equity_path=_pick_first_match(strategy_dir, DAILY_EQUITY_FILE_CANDIDATES),
        direction_summary_path=_pick_first_match(
            strategy_dir, DIRECTION_SUMMARY_FILE_CANDIDATES
        ),
        event_report_path=_pick_first_match(strategy_dir, EVENT_REPORT_FILE_CANDIDATES),
        metrics_summary_path=_pick_first_match(
            strategy_dir, METRICS_SUMMARY_FILE_CANDIDATES
        ),
    )


def _read_csv(csv_path: Optional[Path]) -> pd.DataFrame:
    """讀一份 reporter 產出的 CSV；檔案不存在時回空表（由呼叫端決定要顯示什麼）"""

    if csv_path is None or not csv_path.exists():
        return pd.DataFrame()
    # 產出檔多為 UTF-8-SIG，讀取時優先用 utf-8-sig
    return pd.read_csv(csv_path, encoding="utf-8-sig")


def read_trading_report(csv_path: Path) -> pd.DataFrame:
    # 產出檔多為 UTF-8-SIG，讀取時優先用 utf-8-sig
    return pd.read_csv(csv_path, encoding="utf-8-sig")


def read_daily_equity(csv_path: Optional[Path]) -> pd.DataFrame:
    """讀逐日盯市權益；沒有這份檔案時回空表"""

    return _read_csv(csv_path)


def read_direction_summary(csv_path: Optional[Path]) -> pd.DataFrame:
    """讀多空分開的績效統計；沒有這份檔案時回空表"""

    return _read_csv(csv_path)


def read_metrics_summary(csv_path: Optional[Path]) -> pd.DataFrame:
    """
    讀整體績效指標（`Metric`／`Value`／`Note` 長表）

    **這一份是前端所有風險指標的唯一來源**：Sharpe、Sortino、MDD、波動度、
    Profit Factor、勝敗比、IR 全部取自它，前端不再自行重算任何一條公式
    ——同一個指標算在兩個地方，最後一定會出現「報表說 1.2、前端說 0.8」
    而沒有人知道哪個對。

    改名之前產出的結果資料夾沒有這份檔案，此時回空表，由呼叫端顯示 `N/A`
    並提示重跑回測。
    """

    return _read_csv(csv_path)


def metrics_to_map(metrics: pd.DataFrame) -> Dict[str, Any]:
    """把長表轉成 `{Metric: Value}`；空表回空 dict"""

    if metrics.empty or "Metric" not in metrics.columns:
        return {}

    return dict(zip(metrics["Metric"], metrics["Value"]))


def metrics_note_map(metrics: pd.DataFrame) -> Dict[str, str]:
    """把長表轉成 `{Metric: Note}`，供留空的指標顯示原因"""

    if metrics.empty or "Note" not in metrics.columns:
        return {}

    return {
        str(metric): "" if pd.isna(note) else str(note)
        for metric, note in zip(metrics["Metric"], metrics["Note"])
    }


def read_event_report(csv_path: Optional[Path]) -> pd.DataFrame:
    """讀尾部事件計數；沒有這份檔案時回空表"""

    return _read_csv(csv_path)


def to_numeric(df: pd.DataFrame, column: str) -> pd.Series:
    """取一欄並轉成數值；欄位不存在時回空 Series"""

    if column not in df.columns:
        return pd.Series(dtype=float)
    return pd.to_numeric(df[column], errors="coerce")


def extract_starting_capital(trading_df: pd.DataFrame) -> Optional[float]:
    """
    - Description:
        由交易明細回推初始資金 ＝ **首列 `Cumulative Balance` − 首列 `Realized PnL`**

        舊版直接取首列 `Cumulative Balance` 當初始資金，那是**第一筆交易結束後**
        的餘額，已經含了第一筆的損益，於是「起始資金」永遠是錯的
        （fixture 實測差 11,269）。
    - Parameters:
        - trading_df: pd.DataFrame
            交易明細
    - Return:
        - Optional[float]
            初始資金；欄位缺漏或表為空時為 None
    """

    balance: pd.Series = to_numeric(trading_df, "Cumulative Balance").dropna()
    pnl: pd.Series = to_numeric(trading_df, "Realized PnL").dropna()
    if balance.empty or pnl.empty:
        return None

    return float(balance.iloc[0] - pnl.iloc[0])


def build_equity_series(
    daily_equity_df: pd.DataFrame,
    starting_capital: Optional[float] = None,
) -> pd.Series:
    """
    - Description:
        由 `daily_equity.csv` 建出權益序列，index 為日期

        **與 `StockBacktestReporter.get_equity_series()` 同一個口徑**：一天多筆
        時取當日最後一筆，並在最前面補上初始資金節點——否則曲線的第一個節點
        會是「第一個交易日結束後」的權益，回測首日的損益看不見，MDD 也跟著短一截。
    - Parameters:
        - daily_equity_df: pd.DataFrame
            `daily_equity.csv` 的內容
        - starting_capital: Optional[float]
            初始資金；給了才會補上起點節點
    - Return:
        - pd.Series
            index 為 `datetime.date`、值為權益；沒有逐日權益時為空 Series
    """

    if daily_equity_df.empty or EQUITY_COLUMN not in daily_equity_df.columns:
        return pd.Series(dtype=float)

    work: pd.DataFrame = daily_equity_df[[EQUITY_DATE_COLUMN, EQUITY_COLUMN]].copy()
    work[EQUITY_DATE_COLUMN] = pd.to_datetime(work[EQUITY_DATE_COLUMN], errors="coerce")
    work[EQUITY_COLUMN] = pd.to_numeric(work[EQUITY_COLUMN], errors="coerce")
    work = work.dropna(subset=[EQUITY_DATE_COLUMN, EQUITY_COLUMN])
    if work.empty:
        return pd.Series(dtype=float)

    series: pd.Series = (
        work.groupby(work[EQUITY_DATE_COLUMN].dt.date)[EQUITY_COLUMN]
        .last()
        .astype(float)
        .sort_index()
    )

    if starting_capital is not None and not series.empty:
        # 起點節點掛在第一個交易日的前一天；MDD 只看序列的相對關係，
        # 掛哪一天不影響數值，但曲線不補這個點就會少掉首日的波動
        origin_date = series.index[0] - pd.Timedelta(days=1)
        origin = pd.Series([float(starting_capital)], index=[origin_date])
        series = pd.concat([origin, series]).sort_index()

    return series


def compute_daily_pnl(equity: pd.Series) -> pd.Series:
    """由權益序列算每日損益（逐日差分）；序列不足兩點時回空 Series"""

    if len(equity) < 2:
        return pd.Series(dtype=float)

    return equity.diff().dropna()


def summarise_overview(
    trading_df: pd.DataFrame,
    direction_summary_df: pd.DataFrame,
) -> Dict[str, Optional[float]]:
    """
    - Description:
        總覽區塊的四個數字，**優先取自 reporter 的 `direction_summary.csv`**

        有多空兩列時依 `Trades` 加權合併（勝率與平均 ROI 是比率，直接相加沒有意義）。
        沒有這份檔案才退回交易明細自行彙總，此時 **`ROI` 欄已經是百分比、
        不可再乘 100**——舊版前端就是這樣把平均 ROI 放大 100 倍。兩條路徑在同一份報表上必須給
        出相同的數字，由 `tests/test_frontend_report_loader.py` 盯住。
    - Parameters:
        - trading_df: pd.DataFrame
            交易明細
        - direction_summary_df: pd.DataFrame
            多空分開的績效統計；空表代表沒有這份檔案
    - Return:
        - Dict[str, Optional[float]]
            `trade_count`／`win_count`／`loss_count`／`win_rate`／`total_pnl`／`avg_roi`
    """

    realized_pnl: pd.Series = to_numeric(trading_df, "Realized PnL")
    win_count: int = int((realized_pnl > 0).sum())
    loss_count: int = int((realized_pnl < 0).sum())

    if not direction_summary_df.empty and "Trades" in direction_summary_df.columns:
        trades: pd.Series = to_numeric(direction_summary_df, "Trades")
        total_trades: float = float(trades.sum())
        win_rate: Optional[float] = _weighted_mean(
            to_numeric(direction_summary_df, "Win Rate (%)"), trades
        )
        avg_roi: Optional[float] = _weighted_mean(
            to_numeric(direction_summary_df, "Avg ROI (%)"), trades
        )
        total_pnl: Optional[float] = float(
            to_numeric(direction_summary_df, "Total PnL").sum()
        )
        return {
            "trade_count": int(total_trades),
            "win_count": win_count,
            "loss_count": loss_count,
            "win_rate": win_rate,
            "total_pnl": total_pnl,
            "avg_roi": avg_roi,
        }

    trade_count: int = int(len(trading_df))
    roi: pd.Series = to_numeric(trading_df, "ROI").dropna()
    return {
        "trade_count": trade_count,
        "win_count": win_count,
        "loss_count": loss_count,
        "win_rate": (
            round(win_count / trade_count * 100, 2) if trade_count > 0 else None
        ),
        "total_pnl": float(realized_pnl.sum()) if not realized_pnl.empty else None,
        # **不乘 100**：`ROI` 欄本來就是百分比
        "avg_roi": round(float(roi.mean()), 2) if not roi.empty else None,
    }


def _weighted_mean(values: pd.Series, weights: pd.Series) -> Optional[float]:
    """以筆數加權平均比率欄位；權重總和為零時回 None"""

    paired: pd.DataFrame = pd.DataFrame({"value": values, "weight": weights}).dropna()
    total_weight: float = float(paired["weight"].sum())
    if paired.empty or total_weight <= 0:
        return None

    return round(float((paired["value"] * paired["weight"]).sum() / total_weight), 2)


def sort_by_exit_date(trading_df: pd.DataFrame) -> pd.DataFrame:
    """
    - Description:
        交易明細一律依**平倉日**排序

        `Sell Date` 對 SHORT 是**開倉日**（先賣後買），拿它排序會讓放空的
        資產曲線不是依平倉順序長出來的。`Exit Date` 才是與 `Cumulative Balance`
        同一個時間軸的欄位。

        ⚠️ **必須用穩定排序**（`kind="stable"`）：`Cumulative Balance` 是
        reporter 依列序累加出來的，同一天平倉的多筆之間有先後關係。pandas 的
        預設 `quicksort` 不穩定，會把同日的那幾筆打亂，累積餘額就在那一天內
        來回跳——實測本 fixture 在第 60 列就開始對不上。
    - Parameters:
        - trading_df: pd.DataFrame
            交易明細
    - Return:
        - pd.DataFrame
            依 `Exit Date` 排序後的副本；沒有該欄時原樣回傳
    """

    if EXIT_DATE_COLUMN not in trading_df.columns:
        return trading_df

    work: pd.DataFrame = trading_df.copy()
    work[EXIT_DATE_COLUMN] = pd.to_datetime(work[EXIT_DATE_COLUMN], errors="coerce")
    return work.sort_values(EXIT_DATE_COLUMN, kind="stable")

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from core.models import BaseAccount
from core.strategies.base import BaseStrategy


class BaseBacktestReporter(ABC):
    """Backtest Performance Reporter Framework (Base Template)"""

    def __init__(
        self, strategy: BaseStrategy, output_dir: Optional[Path] = None
    ) -> None:
        self.strategy: BaseStrategy = strategy  # Backtest strategy
        self.account: BaseAccount = self.strategy.account  # Account
        self.output_dir: Optional[Path] = output_dir  # Output directory
        self.trading_report: Optional[pd.DataFrame] = None  # 交易明細表

        # 逐日權益（含未實現損益）；由 Backtester.generate_backtest_report() 注入
        # 只認已實現損益的曲線會把持倉期間的逆勢整段抹平，MDD 因此被低估
        self.daily_equity: Optional[List[Dict[str, Any]]] = None

    @abstractmethod
    def setup(self, *args, **kwargs) -> None:
        """Set Up the Config of Reporter"""
        pass

    @abstractmethod
    def generate_trading_report(self) -> pd.DataFrame:
        """生成回測報告 DataFrame"""
        pass

    @abstractmethod
    def generate_direction_summary(self) -> pd.DataFrame:
        """多空分開的績效統計（放空的尾部風險不可被平均掉）"""
        pass

    @abstractmethod
    def generate_event_report(self, event_counts: dict) -> pd.DataFrame:
        """事件計數報表（強制回補、拒單等）"""
        pass

    @abstractmethod
    def generate_metrics_summary(self) -> pd.DataFrame:
        """整體績效指標（長表；公式一律走 `analysis/performance_metrics.py`）"""
        pass

    @abstractmethod
    def plot_balance_curve(self) -> None:
        """計算並繪製總資金曲線（總資金隨時間變化）"""
        pass

    @abstractmethod
    def plot_balance_and_benchmark_curve(self) -> None:
        """計算並繪製總資金 & benchmark 曲線圖"""
        pass

    @abstractmethod
    def plot_balance_mdd(self) -> None:
        """計算並繪製總資金 Max Drawdown"""
        pass

    @abstractmethod
    def plot_everyday_profit(self) -> None:
        """計算並繪製每天的利潤（已實現口徑）"""
        pass

    @abstractmethod
    def plot_everyday_equity_change(self) -> None:
        """計算並繪製每日權益變化（盯市口徑，含未實現損益）"""
        pass

    @abstractmethod
    def set_figure_config(self, *args, **kwargs) -> None:
        """設置繪圖配置"""
        pass

    @abstractmethod
    def save_report(self, *args, **kwargs) -> None:
        """儲存回測報告"""
        pass

    @abstractmethod
    def save_figure(self, *args, **kwargs) -> None:
        """儲存回測報告圖表"""
        pass

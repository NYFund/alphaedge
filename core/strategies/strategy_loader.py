import importlib
import inspect
import pkgutil
from types import ModuleType
from typing import Dict, List, Type

from loguru import logger

import core.strategies as strategies_pkg
from core.strategies.base import BaseStrategy

"""
StrategyLoader: 自動載入 core/strategies/ 下所有市場的策略類別

**單一模組壞掉不該讓所有策略都跑不了**：舊版是一路
`import_module()` 到底，任何一支策略有 import 錯誤、或在 module level 做了
會炸的事，整個 `load_strategies()` 就往外拋——`run.py --strategy` 於是連
「有哪些策略可用」都列不出來，而錯誤訊息只指向那支壞掉的模組。

**但重複的類別名稱要當場拋出**：`strategies` 以類別名為 key，同名會靜靜
覆蓋——跑的到底是哪一支要看掃描順序，這比壞掉更難查。
"""


class StrategyLoader:
    """自動載入 strategies 資料夾下所有策略類別"""

    @staticmethod
    def load_strategies() -> Dict[str, Type[BaseStrategy]]:
        """
        - Description:
            掃描 `core/strategies/` 下的所有商品類別子套件並載入其中的策略

            原本寫死只掃 `stock` 子套件；改為逐一掃描所有子套件後，
            新增一個商品類別（如 `core/strategies/futures/`）不需要修改本檔案。
        - Return:
            - Dict[str, Type[BaseStrategy]]
                類別名稱 → 策略類別
        """

        strategies: Dict[str, Type[BaseStrategy]] = {}
        broken_modules: List[str] = []

        for _, instrument_name, is_pkg in pkgutil.iter_modules(strategies_pkg.__path__):
            if not is_pkg:
                continue

            instrument_path: str = f"{strategies_pkg.__name__}.{instrument_name}"
            try:
                instrument_pkg: ModuleType = importlib.import_module(instrument_path)
            except Exception as error:
                logger.error(
                    f"[StrategyLoader] 無法載入 {instrument_path}"
                    f"（{type(error).__name__}: {error}），略過整個商品類別"
                )
                broken_modules.append(instrument_path)
                continue

            for _, module_name, _ in pkgutil.iter_modules(instrument_pkg.__path__):
                module_path: str = f"{instrument_pkg.__name__}.{module_name}"
                try:
                    module: ModuleType = importlib.import_module(module_path)
                except Exception as error:
                    # 逐模組隔離：一支策略壞掉，其餘照樣列得出來
                    logger.error(
                        f"[StrategyLoader] 無法載入 {module_path}"
                        f"（{type(error).__name__}: {error}），略過此模組"
                    )
                    broken_modules.append(module_path)
                    continue

                StrategyLoader.collect_from_module(module, strategies)

        if broken_modules:
            logger.warning(
                f"[StrategyLoader] 有 {len(broken_modules)} 個模組載入失敗，"
                f"其策略不會出現在清單中：{broken_modules}"
            )

        return strategies

    @staticmethod
    def collect_from_module(
        module: ModuleType, strategies: Dict[str, Type[BaseStrategy]]
    ) -> None:
        """
        - Description:
            把模組內定義的可實例化策略收進 `strategies`

            只收「在該模組內定義」且「可實例化」的：抽象基底
            （`BaseStrategy`／`BaseStockStrategy`）與被 import 進來的其他模組
            類別都不算策略。
        - Parameters:
            - module: ModuleType
                已載入的策略模組
            - strategies: Dict[str, Type[BaseStrategy]]
                累積結果；就地修改
        - Raise:
            - ValueError
                出現重複的策略類別名稱
        """

        for name, obj in inspect.getmembers(module, inspect.isclass):
            if not (
                issubclass(obj, BaseStrategy)
                and not inspect.isabstract(obj)
                and obj.__module__ == module.__name__
            ):
                continue

            existing: Type[BaseStrategy] = strategies.get(name)
            if existing is not None and existing is not obj:
                # **同名一定要當場拋出**：key 是類別名，靜靜覆蓋之後
                # 「跑的到底是哪一支」要看掃描順序，比模組壞掉更難查
                raise ValueError(
                    f"策略類別名稱重複：{name} 同時定義於 "
                    f"{existing.__module__} 與 {obj.__module__}；"
                    f"類別名即 `run.py --strategy` 的識別名稱，必須唯一"
                )

            strategies[name] = obj  # 用類別名稱當 key

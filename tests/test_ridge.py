from typing import Tuple

import numpy as np

from strategy_lab.strategies.tsmc_overnight_signal.ridge import (
    ridge_fit_predict,
    tune_alpha,
)

"""
Ridge 實作的行為（截距不正則化、alpha 挑選可重現）

原本是「研究端與 `core/` 共用同一份實作」的守門測試，但生產端那支策略已刪除，
實作也隨之搬回研究層，那個一致性已經沒有對象。**保留的是行為測試**：
正則化項的處理只要被動過，研究結論就會悄悄改變。
"""


def make_dataset(seed: int = 42) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """固定亂數種子的小資料集"""

    rng: np.random.Generator = np.random.default_rng(seed)
    X_train: np.ndarray = rng.normal(size=(200, 3))
    y_train: np.ndarray = X_train @ np.array([0.5, -0.2, 0.1]) + rng.normal(
        scale=0.1, size=200
    )
    X_pred: np.ndarray = rng.normal(size=(50, 3))
    return X_train, y_train, X_pred


def test_ridge_does_not_regularise_the_intercept() -> None:
    """
    截距不做正則化

    連截距一起壓會讓預測值系統性偏向 0；用一組「y 全部平移 +100」的資料
    釘住這件事——截距若被壓，預測值會明顯低於 100。
    """

    X_train, y_train, X_pred = make_dataset()

    _, shifted = ridge_fit_predict(X_train, y_train + 100.0, X_pred, alpha=1000.0)

    assert abs(float(np.mean(shifted)) - 100.0) < 1.0


def test_ridge_shrinks_slopes_as_alpha_grows() -> None:
    """alpha 越大、斜率越小（截距不算在內）"""

    X_train, y_train, X_pred = make_dataset()

    weak, _ = ridge_fit_predict(X_train, y_train, X_pred, alpha=0.01)
    strong, _ = ridge_fit_predict(X_train, y_train, X_pred, alpha=1000.0)

    assert np.abs(strong[1:]).sum() < np.abs(weak[1:]).sum()


def test_tune_alpha_is_deterministic() -> None:
    """
    同一組輸入永遠挑到同一個 alpha

    平手時取 grid 中較早出現者（嚴格小於才更新），結果因此與 grid 順序綁定
    而非浮點誤差——兩版才可能逐筆相同。
    """

    X_train, y_train, X_pred = make_dataset()
    grid: np.ndarray = np.logspace(-4, 3, 30)

    first: float = tune_alpha(
        X_train[:150], y_train[:150], X_train[150:], y_train[150:], grid
    )
    second: float = tune_alpha(
        X_train[:150], y_train[:150], X_train[150:], y_train[150:], grid
    )

    assert first == second
    assert first in grid
    assert X_pred.shape[1] == 3

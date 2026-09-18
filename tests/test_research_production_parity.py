from typing import Tuple

import numpy as np

from core.strategies.ridge import ridge_fit_predict, tune_alpha

"""
研究端與 `core/` 必須共用同一份 ridge 實作

`strategy_lab/` 的研究結論是拿來搬進 `core/` 的；兩邊各留一份實作時，
哪天有人在其中一邊調了正則化項的處理，訊號會開始分岔而沒有任何跡象——
而「研究說 Sharpe 2.1、生產跑出 1.3」這種問題查起來極貴。
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


def test_research_imports_the_same_ridge() -> None:
    """研究端指向的是 `core` 裡**同一個函式物件**，不是另一份相同的程式碼"""

    import strategy_lab.strategies.tsmc_overnight_signal.pipeline as research

    assert research.ridge_fit_predict is ridge_fit_predict
    assert research.tune_alpha is tune_alpha


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

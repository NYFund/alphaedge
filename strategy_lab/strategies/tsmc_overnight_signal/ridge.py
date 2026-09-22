from typing import Tuple

import numpy as np

"""
Ridge 迴歸的擬合與 alpha 挑選

這份實作原本放在 `core/strategies/`，為的是讓研究端與成品策略共用同一份程式、
產生一模一樣的訊號。**那支成品策略已於 2026-09-17 刪除**，生產端零消費者，
於是它留在核心層只剩一個效果：讓人以為有個生產策略正在用它。

搬回研究層之後**不要再往 `core/` 搬**，除非同時有生產策略要用；
屆時兩邊共用同一份實作這件事要重新釘住（正則化項的處理只要有一邊被動過，
訊號就會分岔，而「研究說 Sharpe 2.1、生產跑出 1.3」查起來極貴）。
"""


def ridge_fit_predict(
    X_train: np.ndarray,
    y_train: np.ndarray,
    X_pred: np.ndarray,
    alpha: float,
) -> Tuple[np.ndarray, np.ndarray]:
    """
    - Description:
        以閉式解擬合帶截距的 ridge 迴歸並預測

        **截距不做正則化**（`reg[0, 0] = 0`）：正則化的目的是壓縮斜率，
        連截距一起壓會讓預測值系統性偏向 0，而那不是任何人想要的。
    - Parameters:
        - X_train: np.ndarray
            訓練特徵，shape 為 `(n, p)`
        - y_train: np.ndarray
            訓練標的，shape 為 `(n,)`
        - X_pred: np.ndarray
            要預測的特徵
        - alpha: float
            正則化強度
    - Return:
        - Tuple[np.ndarray, np.ndarray]
            （係數含截距, 預測值）
    """

    n, p = X_train.shape
    X1 = np.c_[np.ones(n), X_train]
    reg = np.eye(p + 1)
    reg[0, 0] = 0.0
    coef = np.linalg.solve(X1.T @ X1 + alpha * reg, X1.T @ y_train)
    y_hat = np.c_[np.ones(len(X_pred)), X_pred] @ coef
    return coef, y_hat


def tune_alpha(
    X_train: np.ndarray,
    y_train: np.ndarray,
    X_val: np.ndarray,
    y_val: np.ndarray,
    grid: np.ndarray,
) -> float:
    """
    - Description:
        在驗證集上以 MSE 挑 alpha

        **平手時取 grid 中較早出現者**（嚴格小於才更新）：這讓結果與 grid 的
        順序綁定而非浮點誤差，兩版才可能逐筆相同。
    - Parameters:
        - X_train / y_train: np.ndarray
            訓練集
        - X_val / y_val: np.ndarray
            驗證集
        - grid: np.ndarray
            候選 alpha
    - Return:
        - float
            驗證集 MSE 最小的 alpha
    """

    best_alpha: float = float(grid[0])
    best_mse: float = np.inf
    for a in grid:
        _, y_hat = ridge_fit_predict(X_train, y_train, X_val, float(a))
        mse: float = float(np.mean((y_val - y_hat) ** 2))
        if mse < best_mse:
            best_mse = mse
            best_alpha = float(a)
    return best_alpha

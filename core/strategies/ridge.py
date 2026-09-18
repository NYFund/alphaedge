from typing import Tuple

import numpy as np

"""
Ridge 迴歸的擬合與 alpha 挑選

**這份實作原本散在研究端與成品策略各一份、逐字相同**。研究端與 `core/` 本來
就該產生**一模一樣的訊號**——那是「研究結果搬得進生產」的唯一保證；各留一份的話，
哪天有人在其中一邊調了正則化項的處理，兩邊的訊號會開始分岔而沒有任何跡象
（`tests/test_research_production_parity.py` 釘住這件事）。

放在 `core/strategies/` 而不是 `strategy_lab/`：相依方向是研究層依賴核心層
（見 `scripts/check_layer_deps.py`），反過來會讓生產程式碼依賴研究筆記。

**刻意是模組而不是子套件**：`StrategyLoader` 會把 `core/strategies/` 底下的
每個**子套件**當成一個商品類別去掃，多開一個 `shared/` 目錄會讓它看起來像
`stock`／`futures` 的同輩。模組層級的檔案會被 `if not is_pkg: continue` 跳過。
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

from dataclasses import dataclass, fields
from typing import Dict, List, Union

"""
RiskConfig：風控門檻

**策略可以覆寫，但不能超過全域上限。** 不設上限的話，「調鬆一點」會逐次發生，
而每一次都有當下看起來合理的理由；等到出事時，風控的門檻已經和沒有差不多。

比率一律相對於**該策略的實盤資金額度**（`live_capital`；未宣告時為 `init_capital`），
不是研究回測的 `init_capital`——兩者可以不同，拿錯基準時四道上限會跟著放寬或收緊。
帳戶層的幾條則相對於帳戶權益。
"""


# 資金額度的安全係數：Σ 各策略額度不得超過帳戶總權益 × 它。
#
# **與 `RiskConfig.account_exposure_ratio` 是同一個值**，故只定義一次——
# 定義兩次必然漂移。**不放 `core/config/schema.py`**：那個檔是資料庫結構
# （分庫檔名、表名），擺一個營運參數進去會讓「改門檻」與「改 schema」
# 混在同一個檔案的變更歷史裡。
CAPITAL_SAFETY_RATIO: float = 0.95


# 全域上限。**這一份不可由策略覆寫**，改它要有人明確決定並留下 commit。
#
# 用純字典而不是另一個 `RiskConfig` 實例：後者會陷入雞生蛋——建構它時
# `__post_init__` 要拿全域上限來比對，而那時全域上限還不存在。
#
# 數值取得比預設值寬，但仍然是「出事時虧得起」的量級：單筆四成資金、
# 單日兩倍週轉、單筆 200 張／20 口、偏離一成（剛好是台股的漲跌停幅度）。
RISK_LIMIT_CAPS: Dict[str, Union[int, float]] = {
    "single_order_amount_ratio": 0.40,
    "daily_amount_ratio": 2.00,
    "max_stock_lots": 200,
    "max_futures_contracts": 20,
    "price_deviation_ratio": 0.10,
    "max_orders_per_minute": 120,
    "total_exposure_ratio": 1.00,
    "single_symbol_exposure_ratio": 0.50,
    "daily_loss_ratio": 0.10,
    "account_daily_loss_ratio": 0.10,
    "account_exposure_ratio": 1.00,
}


@dataclass(frozen=True)
class RiskConfig:
    """
    - Description:
        單一策略的風控門檻

        **frozen**：風控設定在盤中被改掉是最難查的一種事故——拒單數突然變了，
        而程式碼看起來完全一樣。要改就重啟，並留下 `live_run.risk_config_json`。
    """

    # === 策略層：逐單 ===
    # 單筆委託金額上限（股票：價 × 股數；期貨：保證金 × 口數）。
    # **與單一標的上限同理，不可小於實盤每檔分到的比例（1 ÷ 持倉檔數）**：
    # 一檔的開倉通常就是一張單，比例一超過，每張買單都會在逐筆檢查被擋下。
    # 基準是實盤額度，現行最集中的是 3 檔（約 33%），故與單一標的上限同取 0.35
    single_order_amount_ratio: float = 0.35
    # 單日**累計委託金額**上限。這是**流量**指標，防的是迴圈 bug 或重啟造成的反覆送單；
    # 與下面的曝險（存量）是兩回事，只留其中一條都有破口
    daily_amount_ratio: float = 1.00
    max_stock_lots: int = 50  # 單筆張數上限
    max_futures_contracts: int = 5  # 單筆口數上限
    # 委託價偏離基準價的上限。**要大於送市價語意時的容許滑價**，
    # 否則自己換算出的價格會被自己的風控拒單
    price_deviation_ratio: float = 0.03
    max_orders_per_minute: int = 30  # 下單頻率

    # === 策略層：批次（存量）===
    # 送出後的預估總曝險（持倉市值 ＋ 在途委託金額）
    total_exposure_ratio: float = 1.00
    # 單一標的曝險占比。**不可小於實盤每檔分到的比例（1 ÷ 持倉檔數）**：
    # 部位建構器把資金均分給各檔，比例一超過這條上限，每張買單都會被帳戶層截斷，
    # 下單路徑整個不通卻不報錯。現行最集中的是 3 檔（約 33%），故取 0.35
    single_symbol_exposure_ratio: float = 0.35

    # === 策略層：損益 ===
    daily_loss_ratio: float = 0.03  # 單日已實現＋未實現虧損 → REDUCE_ONLY

    # === 帳戶層 ===
    account_daily_loss_ratio: float = 0.03  # 帳戶當日虧損 → 全體 REDUCE_ONLY
    # 帳戶總曝險上限；與 `CapitalAllocator` 的安全係數同一個值
    account_exposure_ratio: float = CAPITAL_SAFETY_RATIO

    def __post_init__(self) -> None:
        """建立時就驗證，不等到盤中第一次拒單才發現門檻設錯"""

        violations: List[str] = self.validate()
        if violations:
            raise ValueError("RiskConfig 超過全域上限：" + "；".join(violations))

    def validate(self) -> List[str]:
        """
        - Description:
            逐項比對全域上限，回傳違規說明

            比率類不可為負、也不可超過 `RISK_LIMIT_CAPS` 的對應值；數量類同理。
        - Return:
            - List[str]
                違規說明；全部合格時為空清單
        """

        violations: List[str] = []
        for field in fields(self):
            value: Union[int, float] = getattr(self, field.name)
            cap: Union[int, float] = RISK_LIMIT_CAPS[field.name]
            if value < 0:
                violations.append(f"{field.name}={value} 不可為負")
            elif value > cap:
                violations.append(f"{field.name}={value} 超過全域上限 {cap}")
        return violations

import dataclasses
from dataclasses import dataclass, fields
from typing import Any, ClassVar, Dict, List, Mapping, Optional, Tuple, TypeVar, Union

from core.utils import InstrumentType

"""
實盤風控門檻：策略宣告、部署設定收緊、程式寫死的全域上限三層

**與風控本體分開**（對照 `CostConfig` 之於 `TwStockCostModel`）：策略基底要宣告它，
放在 `core/live/` 的話，策略層為了拿一個 dataclass 就得 import 實盤套件。
使用它的判定邏輯在 `core/live/risk/risk_manager.py`。

三層的關係：`CAPS`（程式寫死）⊇ 策略宣告（`live_risk_config`）⊇ 部署設定（`deploy/live_risk.toml`）。
- **策略可以覆寫，但不能超過全域上限。** 不設上限的話，「調鬆一點」會逐次發生，
  而每一次都有當下看起來合理的理由；等到出事時，風控的門檻已經和沒有差不多。
- **部署設定只能收緊。** 部署端（正式環境先小額跑）要的是「比策略更保守」，
  放寬應該回到策略宣告、留下 commit 與理由，而不是在設定檔裡默默改大。

**股票與期貨分成兩個類別，同性質的欄位同名**：兩個市場的上限結構相同（單筆、單日、數量、
曝險），差別只在計量方式——股票的金額是「價 × 股數」、期貨是保證金；數量股票以張計、
期貨以口計；曝險股票看持倉金額、期貨看契約價值的名目槓桿。共用一個類別的話，股票策略也看得到、
寫得進期貨欄位，寫了卻不生效，而且不會有任何錯誤訊息。

比率一律相對於**該策略的實盤資金額度**（`live_capital`；未宣告時為 `init_capital`），
不是研究回測的 `init_capital`——兩者可以不同，拿錯基準時各道上限會跟著放寬或收緊。
"""


ConfigT = TypeVar("ConfigT", bound="_LimitConfig")


@dataclass(frozen=True)
class _LimitConfig:
    """
    - Description:
        「每個欄位都是上限」的設定共用的驗證與收緊

        **frozen**：風控設定在盤中被改掉是最難查的一種事故——拒單數突然變了，
        而程式碼看起來完全一樣。要改就重啟，並留下 `live_run.risk_config_json`。

        所有欄位都是上限，**數值越小越嚴**；收緊與全域上限的檢查都建立在這個前提上，
        新增欄位時不可加入「越大越嚴」的欄位。
    """

    # 全域上限。**不可由策略覆寫**，改它要有人明確決定並留下 commit。
    # 用純字典而不是另一個實例：後者會陷入雞生蛋——建構它時 `__post_init__`
    # 要拿全域上限來比對，而那時全域上限還不存在
    CAPS: ClassVar[Dict[str, Union[int, float]]] = {}

    def __post_init__(self) -> None:
        """建立時就驗證，不等到盤中第一次拒單才發現門檻設錯"""

        if not self.CAPS:
            raise TypeError(
                f"{type(self).__name__} 沒有全域上限，不能直接建立；"
                "請用 StockRiskConfig、FuturesRiskConfig 或 AccountRiskConfig"
            )
        violations: List[str] = self.validate()
        if violations:
            raise ValueError(
                f"{type(self).__name__} 超過全域上限：" + "；".join(violations)
            )

    def validate(self) -> List[str]:
        """
        - Description:
            逐項比對全域上限，回傳違規說明

            比率類不可為負、也不可超過 `CAPS` 的對應值；數量類同理。
        - Return:
            - List[str]
                違規說明；全部合格時為空清單
        """

        violations: List[str] = []
        for field in fields(self):
            value: Union[int, float] = getattr(self, field.name)
            cap: Union[int, float] = self.CAPS[field.name]
            if value < 0:
                violations.append(f"{field.name}={value} 不可為負")
            elif value > cap:
                violations.append(f"{field.name}={value} 超過全域上限 {cap}")
        return violations

    def with_overrides(self: ConfigT, overrides: Mapping[str, Any]) -> ConfigT:
        """
        - Description:
            套用部署設定，**只能收緊**

            寫了比目前更寬的值、或寫了不存在的欄位，一律當場報錯：
            部署設定打錯字而被忽略的話，以為收緊了其實沒有，那比沒有設定更危險。
        - Parameters:
            - overrides: Mapping[str, Any]
                欄位名 → 新的上限
        - Return:
            - ConfigT
                套用後的新設定（本身是 frozen，不會被改動）
        - Raise:
            - ValueError
                欄位不存在、型別不符或比目前更寬
        """

        known: Dict[str, Union[int, float]] = {
            field.name: getattr(self, field.name) for field in fields(self)
        }
        problems: List[str] = []
        for name, value in overrides.items():
            if name not in known:
                problems.append(f"{name} 不是 {type(self).__name__} 的欄位")
            elif isinstance(value, bool) or not isinstance(value, (int, float)):
                problems.append(f"{name}={value!r} 不是數值")
            elif value > known[name]:
                problems.append(
                    f"{name}={value} 比目前的 {known[name]} 寬，部署設定只能收緊"
                )
        if problems:
            raise ValueError("部署設定不合法：" + "；".join(problems))

        return dataclasses.replace(self, **dict(overrides))

    def to_dict(self) -> Dict[str, Union[int, float]]:
        """欄位名 → 值；寫進 `live_run.risk_config_json` 用"""

        return {field.name: getattr(self, field.name) for field in fields(self)}


@dataclass(frozen=True)
class BaseRiskConfig(_LimitConfig):
    """
    - Description:
        單一策略的風控門檻，股票與期貨共用的欄位

        兩個市場**同名的欄位計量不同**，以子類別的 docstring 為準：
        `single_order_ratio`／`daily_ratio` 股票是委託金額、期貨是保證金；
        `max_order_volume` 股票是張、期貨是口。
    """

    # === 逐單 ===
    # 單筆委託上限（÷ 額度）。**與單一標的上限同理，不可小於實盤每檔分到的比例（1 ÷ 持倉檔數）**：
    # 一檔的開倉通常就是一張單，比例一超過，每張買單都會在逐筆檢查被擋下。
    # 現行最集中的是 3 檔（約 33%），故取 0.35
    single_order_ratio: float = 0.35
    # 單日**累計委託**上限（÷ 額度）。這是**流量**指標，防的是迴圈 bug 或重啟造成的反覆送單；
    # 與曝險（存量）是兩回事，只留其中一條都有破口
    daily_ratio: float = 1.00
    max_order_volume: int = 0  # 單筆數量上限；單位與預設值由子類別決定
    # 決策價偏離基準價的上限。**要大於送市價語意時的容許滑價**，
    # 否則自己換算出的價格會被自己的風控拒單
    price_deviation_ratio: float = 0.03
    max_orders_per_minute: int = 30  # 下單頻率

    # === 損益 ===
    daily_loss_ratio: float = (
        0.03  # 單日已實現＋未實現虧損（÷ 額度）→ 該策略 REDUCE_ONLY
    )

    # 拒單訊息用的計量名稱：數量單位、逐單金額與批次曝險各是什麼
    VOLUME_UNIT: ClassVar[str] = ""
    AMOUNT_LABEL: ClassVar[str] = ""
    EXPOSURE_LABEL: ClassVar[str] = ""

    def exposure_limits(self, capital_limit: float) -> Tuple[float, Optional[float]]:
        """
        - Description:
            批次曝險的上限：`(總曝險上限金額, 單一標的上限比例)`

            曝險的計量（持倉金額或契約價值）由呼叫端換算好，這裡只給上限。
        - Parameters:
            - capital_limit: float
                該策略的實盤資金額度
        - Return:
            - Tuple[float, Optional[float]]
                總曝險上限金額；單一標的上限比例（None 表示不限制）
        """

        raise NotImplementedError


@dataclass(frozen=True)
class StockRiskConfig(BaseRiskConfig):
    """
    - Description:
        股票策略的風控門檻

        - `single_order_ratio`／`daily_ratio`：委託**金額**（價 × 股數）÷ 額度。
        - `max_order_volume`：單筆**張數**上限。
        - 曝險以持倉金額計：總曝險與單一標的各一條上限。
    """

    CAPS: ClassVar[Dict[str, Union[int, float]]] = {
        "single_order_ratio": 0.40,
        "daily_ratio": 2.00,
        "max_order_volume": 200,
        "price_deviation_ratio": 0.10,
        "max_orders_per_minute": 120,
        "daily_loss_ratio": 0.10,
        "total_exposure_ratio": 1.00,
        "single_symbol_exposure_ratio": 0.50,
    }
    VOLUME_UNIT: ClassVar[str] = "張"
    AMOUNT_LABEL: ClassVar[str] = "委託金額"
    EXPOSURE_LABEL: ClassVar[str] = "總曝險"

    max_order_volume: int = 50
    # === 批次（存量）===
    # 送出後的預估總曝險（持倉金額 ＋ 在途委託金額）
    total_exposure_ratio: float = 1.00
    # 單一標的曝險占比。**不可小於實盤每檔分到的比例（1 ÷ 持倉檔數）**：
    # 部位建構器把資金均分給各檔，比例一超過這條上限，每張買單都會被截斷，
    # 下單路徑整個不通卻不報錯。現行最集中的是 3 檔（約 33%），故取 0.35
    single_symbol_exposure_ratio: float = 0.35

    def exposure_limits(self, capital_limit: float) -> Tuple[float, Optional[float]]:
        """總曝險 ＝ 額度 × `total_exposure_ratio`；單一標的看 `single_symbol_exposure_ratio`"""

        return (
            capital_limit * self.total_exposure_ratio,
            self.single_symbol_exposure_ratio,
        )


@dataclass(frozen=True)
class FuturesRiskConfig(BaseRiskConfig):
    """
    - Description:
        期貨策略的風控門檻

        - `single_order_ratio`／`daily_ratio`：原始**保證金** ÷ 額度。以契約價值計的話，
          一口台指期就是千萬級，任何合理的額度連一口都送不出去。
        - `max_order_volume`：單筆**口數**上限。
        - 曝險以**契約價值**計，只有一條名目槓桿上限：總契約價值 ≤ 額度 × `max_leverage`。
          保證金只反映「押了多少錢」，反映不了「承擔多少風險」——同樣的保證金，
          口數放大幾倍，損益就跟著放大幾倍。股票那兩條曝險比率套在契約價值上
          會擋掉所有期貨單，故不適用；指數期貨通常只做一個商品，單一標的分散的意義也不大。
    """

    CAPS: ClassVar[Dict[str, Union[int, float]]] = {
        "single_order_ratio": 0.40,
        "daily_ratio": 2.00,
        "max_order_volume": 20,
        "price_deviation_ratio": 0.10,
        "max_orders_per_minute": 120,
        "daily_loss_ratio": 0.10,
        "max_leverage": 8.0,
    }
    VOLUME_UNIT: ClassVar[str] = "口"
    AMOUNT_LABEL: ClassVar[str] = "保證金"
    EXPOSURE_LABEL: ClassVar[str] = "總契約價值"

    max_order_volume: int = 5
    # 名目槓桿上限：期貨部位總契約價值 ÷ 額度。
    # 以台指期 47,224 點計一口約 944 萬，300 萬額度可開 1 口（約 3.15 倍），指數再漲約 27% 仍開得了
    max_leverage: float = 4.0

    def exposure_limits(self, capital_limit: float) -> Tuple[float, Optional[float]]:
        """總契約價值 ＝ 額度 × `max_leverage`；不設單一標的上限"""

        return (capital_limit * self.max_leverage, None)


@dataclass(frozen=True)
class AccountRiskConfig(_LimitConfig):
    """
    - Description:
        帳戶層的風控門檻

        **策略類別不能設**：帳戶層影響所有策略，而股票與期貨是不同行程，
        各策略各設一個值再取「最嚴者」的話，兩個行程會算出不同的帳戶門檻。
        只從部署設定的 `[account]` 段與這裡的預設值來。
    """

    CAPS: ClassVar[Dict[str, Union[int, float]]] = {"daily_loss_ratio": 0.10}

    daily_loss_ratio: float = 0.03  # 帳戶當日虧損（÷ 帳戶權益）→ 全體 REDUCE_ONLY
    # 帳戶總曝險沒有對應欄位：由額度總量與各策略的曝險上限間接保證（見 `CAPITAL_SAFETY_RATIO`）


# 各商品類別的風控設定類別；策略沒宣告時用它的預設值，宣告了則類別必須相符
RISK_CONFIG_BY_INSTRUMENT: Dict[InstrumentType, type] = {
    InstrumentType.STOCK: StockRiskConfig,
    InstrumentType.FUTURE: FuturesRiskConfig,
}


def default_risk_config(instrument_type: Optional[InstrumentType]) -> BaseRiskConfig:
    """
    - Description:
        該商品類別的預設風控設定
    - Parameters:
        - instrument_type: Optional[InstrumentType]
            策略的商品類別
    - Return:
        - BaseRiskConfig
            預設值的 `StockRiskConfig` 或 `FuturesRiskConfig`
    - Raise:
        - ValueError
            商品類別沒有對應的風控設定
    """

    config_cls: Optional[type] = RISK_CONFIG_BY_INSTRUMENT.get(instrument_type)
    if config_cls is None:
        raise ValueError(f"商品類別 {instrument_type} 沒有對應的實盤風控設定")
    return config_cls()

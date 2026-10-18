import tomllib
from pathlib import Path
from typing import Any, Dict, List, Mapping

"""
部署端的風控設定檔（`deploy/live_risk.toml`）載入

**只負責讀檔與檢查形狀**，不認識任何風控欄位：欄位是否存在、是否比策略宣告更寬，
由 `core/models/risk_config.py` 的 `with_overrides()` 判定——本層在分層上低於 `core.models`，
在這裡驗欄位等於維護第二份欄位清單。

檔案格式：

```toml
[account]                    # 帳戶層（AccountRiskConfig）
daily_loss_ratio = 0.02

[MomentumFuturesStrategy]    # 以策略類別名分段，只寫要收緊的欄位
max_leverage = 3.0
```

**檔案不存在＝不收緊**（只用策略宣告與預設值），不是錯誤；檔案存在但格式錯就當場報錯——
寫錯而被略過的話，以為收緊了其實沒有。
"""

# 帳戶層那一段的名稱；策略類別名不會是這個（類別名以大寫開頭）
ACCOUNT_SECTION: str = "account"


class LiveRiskOverrides:
    """
    - Description:
        部署設定的內容：帳戶層一段、各策略各一段，值都是「欄位名 → 上限」
    """

    def __init__(
        self,
        account: Mapping[str, Any],
        strategies: Mapping[str, Mapping[str, Any]],
    ) -> None:
        """
        - Description:
            建立部署設定
        - Parameters:
            - account: Mapping[str, Any]
                `[account]` 段
            - strategies: Mapping[str, Mapping[str, Any]]
                策略類別名 → 該段內容
        """

        self.account: Dict[str, Any] = dict(account)
        self.strategies: Dict[str, Dict[str, Any]] = {
            name: dict(section) for name, section in strategies.items()
        }

    def for_strategy(self, strategy_name: str) -> Dict[str, Any]:
        """該策略要收緊的欄位；沒有那一段時為空"""

        return dict(self.strategies.get(strategy_name, {}))

    def to_dict(self) -> Dict[str, Any]:
        """原樣輸出，寫進 `live_run.risk_config_json` 用"""

        return {
            ACCOUNT_SECTION: dict(self.account),
            "strategies": dict(self.strategies),
        }


def load_live_risk_overrides(path: Path) -> LiveRiskOverrides:
    """
    - Description:
        讀部署端的風控設定檔
    - Parameters:
        - path: Path
            設定檔路徑（正式位置見 `LIVE_RISK_CONFIG_PATH`）
    - Return:
        - LiveRiskOverrides
            檔案內容；檔案不存在時兩段皆空
    - Raise:
        - ValueError
            TOML 語法錯誤，或頂層有不是表格（`[段名]`）的鍵
    """

    if not path.exists():
        return LiveRiskOverrides({}, {})

    try:
        with path.open("rb") as file:
            data: Dict[str, Any] = tomllib.load(file)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{path} 不是合法的 TOML：{exc}") from exc

    not_tables: List[str] = [
        key for key, value in data.items() if not isinstance(value, dict)
    ]
    if not_tables:
        raise ValueError(
            f"{path} 頂層只能有 [段名] 表格，以下鍵不在任何段落內：{not_tables}"
        )

    account: Dict[str, Any] = data.pop(ACCOUNT_SECTION, {})
    return LiveRiskOverrides(account, data)

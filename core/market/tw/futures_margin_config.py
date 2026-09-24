from dataclasses import dataclass
from typing import Optional

from core.api.tw.futures_margin_api import FuturesMarginAPI
from core.utils import MarginCallPolicy

"""
期貨保證金設定：純設定物件，不含任何部位行為

**放在市場結構而不是部位管理層**：保證金級距是**交易所公告的市場結構**，
與交易日曆、結算日、換月規則同一類；策略、回測資料源、實盤資料源與部位建構層
都要建一份，但它們要的只是這個 dataclass，不是 `FuturesPositionManager` 的行為。
埋在部位管理層的話，`core/portfolio/` 為了拿一個設定就得 import 上層的管理器——
那正是 `core/portfolio/__init__.py` 明文禁止的方向。

**這裡可以合法 import `core.api`**：市場結構與資料層同層（日曆的建構子同樣收 API 物件）。
放進更低的 `core/models/` 反而會變成領域層相依資料層——實測分層檢查當場報違規。
"""


@dataclass
class FuturesMarginConfig:
    """
    保證金設定：**預設查表，查不到才退回比率近似**

    真實的 TAIFEX 原始保證金是**每口固定金額**、依波動度每日計算、達門檻就調
    （TX 在 2015~2026 調整 62 次，間隔最短 2 天、最長 372 天），
    且**調整後溯及既往**——未沖銷部位一併適用。

    | 模式 | 何時生效 | 行為 |
    |------|----------|------|
    | 查表（預設） | `api` 已注入 | 查 `futures_margin_history`；**查不到就 raise** |
    | 比率近似 | `use_api=False`，或 `api` 尚未注入 | 契約價值 × `initial_margin_ratio` |

    **`api` 由 DataFeed 注入**（`TwFuturesDataFeed.setup()`），不是在此自行建立
    ——全專案的資料 API 一律由 DataFeed 統一持有並共用同一條連線。要在回測以外
    的地方用查表模式，明確傳 `api=` 或走 `from_api()`。

    **查不到為什麼要 raise 而不是退回比率**：理由同 `FUTURES_MULTIPLIER` 用 `[]`
    而非 `.get()`——靜默套一個近似值會讓資金效率與可開口數整段偏掉卻毫無徵兆，
    中斷比靜默錯誤好查。資料涵蓋 2020-03 起（更早的公告附件是掃描影像，
    取不到數值），更早的區間會當場中止。

    比率模式的近似**跨年份會系統性偏掉**（實測 2020 年 +143% 到 2026 年 −38%，
    而且會變號），故它只適合「跑通流程」而非產出可信績效。
    """

    initial_margin_ratio: float = 0.1  # 原始保證金佔契約價值的比率（比率模式使用）
    api: Optional[FuturesMarginAPI] = None  # 查表用；回測時由 DataFeed 注入
    # 查詢日早於表內所有列時是否退回最早一列。**預設關閉**：
    # 它無法區分「該商品從未被調整過」（退回正確）與「查詢日早於資料涵蓋範圍」
    # （退回錯誤），見 `FuturesMarginAPI` 的說明
    fallback_to_earliest: bool = False
    # 是否讓 DataFeed 注入保證金 API。**預設開啟**：保證金資料已備妥
    # （2020-03 起），用比率近似回測是刻意的降級，應由使用者明確表態
    use_api: bool = True

    # === 追繳 ===
    # 權益低於維持保證金時的處理；**強制平倉是預設**——真實帳戶不會讓部位
    # 在保證金不足的情況下續留，只標記會讓回測高估留倉能力
    margin_call_policy: MarginCallPolicy = MarginCallPolicy.FORCE_COVER
    # 追繳門檻的倍數：權益 < 維持保證金總額 × 本值即觸發。
    # 1.0 為交易所口徑，調高即為「比交易所更早出場」的自訂風控
    margin_call_ratio: float = 1.0

    @staticmethod
    def default() -> "FuturesMarginConfig":
        """預設設定：查表模式（API 由 DataFeed 注入），追繳採強制平倉"""

        return FuturesMarginConfig()

    @staticmethod
    def ratio(initial_margin_ratio: float = 0.1) -> "FuturesMarginConfig":
        """
        比率近似模式：**明確表態不查表**

        只適合「跑通流程」或回測 2020-03 之前的區間（該段沒有保證金資料）。
        """

        return FuturesMarginConfig(
            initial_margin_ratio=initial_margin_ratio, use_api=False
        )

    @staticmethod
    def from_api(
        api: FuturesMarginAPI,
        fallback_to_earliest: bool = False,
    ) -> "FuturesMarginConfig":
        """
        - Description:
            以既有的 `FuturesMarginAPI` 建立查表模式的設定

            **`api` 必須由呼叫端傳入**，本函式不自行 `FuturesMarginAPI()`：那會暗中
            開一條沒有人負責關閉的連線。API 的連線應與 DataFeed 共用、由 DataFeed 關閉；
            只要預設查表模式、不自己持有 API 的話用 `default()`，由 DataFeed 注入。
        - Parameters:
            - api: FuturesMarginAPI
                查保證金用的 API（通常共用 DataFeed 的連線）
            - fallback_to_earliest: bool
                查詢日早於表內所有列時是否退回最早一列
        - Return:
            - FuturesMarginConfig
                查表模式的設定
        """

        return FuturesMarginConfig(api=api, fallback_to_earliest=fallback_to_earliest)

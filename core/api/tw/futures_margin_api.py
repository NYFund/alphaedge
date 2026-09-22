import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Type

from core.api.base import BaseDataAPI
from core.config import TW_FUTURES_DB_PATH
from core.dao.base import BaseDAO
from core.dao.tw.futures_margin_dao import FuturesMarginDAO
from core.dao.tw.futures_stock_universe_dao import FuturesStockUniverseDAO

"""
Futures Margin API: query the two margin tables in tw_futures.db

**兩張表、兩種取值方式**（分表依據是「金額 vs 比例」，不是「指數 vs 股票」——
ETF 股期給的也是每口金額，語意與指數期貨相同）：

| 表 | 涵蓋 | 每口保證金 |
|----|------|-----------|
| `futures_margin_history` | 指數期貨 ＋ ETF 股期 | **就是表裡的金額** |
| `stock_futures_margin_rate_history` | 股票股期 | 標的股價 × 契約單位 × 比例（**要算**） |

---

**查詢語意：取 `effective_date <= 該日` 的最大者，不是 `= 該日`。**

保證金是**變動序列**——只有調整那天才有列。用等號查只有剛好調整的那天查得到，
其餘每一天都會回傳 None，而「查不到」在下游多半被當成「不需要保證金」。

---

**兩種「查不到」，成因完全不同，但本層一律回傳 None 讓呼叫端決定：**

1. **查詢日早於 2020-03**（來源限制）：更早的調整公告附件是掃描影像，取不到數值。
2. **該商品在查詢日之前從未被調整過**——這是資料的語意，不是缺漏：
   **調整公告只列「有調整的商品」**，級距穩定的商品（Ex: 台積電期 CDF 一直是
   級距 1／13.5%）在整個 2020~2026 都不會出現在任何一則公告裡，
   表內因此只有現行一覽表那一列（2026-08-28）。

第 2 種可用 `fallback_to_earliest=True` 取「表內最早的一列」當近似——
對「從未調整過」的商品那就是正確答案，但**無法與第 1 種區分**，
故預設關閉、由呼叫端明確表態。
"""


class FuturesMarginAPI(BaseDataAPI):
    """Futures Margin API"""

    DEFAULT_DB_PATH: Path = Path(TW_FUTURES_DB_PATH)
    DAO_CLASS: Type[BaseDAO] = FuturesMarginDAO
    LOG_FILE_NAME: str = "futures_margin_api.log"

    def setup(self) -> None:
        """建連線與主 DAO 由基底負責；此處只補本 API 多出來的 DAO"""

        super().setup()
        self.universe_dao: FuturesStockUniverseDAO = FuturesStockUniverseDAO(
            conn=self.conn
        )

    # === 金額型（指數期貨 ＋ ETF 股期）===
    def get_margin(
        self,
        product: str,
        date: datetime.date,
        fallback_to_earliest: bool = False,
    ) -> Optional[Dict[str, int]]:
        """
        - Description:
            取得該商品在指定日期**生效中**的三種保證金金額

            取 `effective_date <= date` 的最大者——保證金不是每天都變，而調整當天的
            新值當天就適用（updater 驗證公告時問的是「調整之前」，用的是 `<`，
            見 `FuturesMarginDAO.get_margin_in_effect()`）。
        - Parameters:
            - product: str
                契約代碼（Ex: TX、NYF）
            - date: datetime.date
                查詢日
            - fallback_to_earliest: bool
                查詢日早於表內所有列時，是否退回**最早**的一列當近似。
                對「從未被調整過」的商品那就是正確答案；但它與「查詢日早於
                2020-03 的來源缺口」**無法區分**，故預設關閉，見本檔說明。
        - Return:
            - Optional[Dict[str, int]]
                `{"結算保證金", "維持保證金", "原始保證金"}`；查無資料時為 None
        """

        # 表不存在（尚未跑過保證金 ETL）：與「查無該商品」同樣回 None，
        # 由呼叫端決定要中止還是退回比率近似。**只判斷表存不存在**——
        # 舊版連 `database is locked` 一起吞，會讓回測誤用比率近似值
        row: Optional[Tuple[Any, ...]] = self.dao.get_margin_in_effect(
            product,
            date,
            inclusive=True,
            fallback_to_earliest=fallback_to_earliest,
        )

        if row is None:
            return None
        return {
            "結算保證金": row[0],
            "維持保證金": row[1],
            "原始保證金": row[2],
        }

    def get_initial_margin(
        self,
        product: str,
        date: datetime.date,
        fallback_to_earliest: bool = False,
    ) -> Optional[int]:
        """
        取得**每口原始保證金**（委託人繳交的那一種）

        回測一律用這一欄——`結算保證金` 是交易所向結算會員收的，不是交易人繳的。
        """

        margin: Optional[Dict[str, int]] = self.get_margin(
            product, date, fallback_to_earliest=fallback_to_earliest
        )
        return None if margin is None else margin["原始保證金"]

    def get_maintenance_margin(
        self,
        product: str,
        date: datetime.date,
        fallback_to_earliest: bool = False,
    ) -> Optional[int]:
        """
        取得**每口維持保證金**（追繳門檻用的那一種）

        與 `get_initial_margin()` 的分工：原始保證金是「開一口要先繳多少」，
        維持保證金是「帳戶權益低於多少就會被追繳」，後者一律較低
        （TX 2024-10-31：原始 338,000、維持 259,000）。
        兩者都是交易所公告值，**不可用比率互推**。
        """

        margin: Optional[Dict[str, int]] = self.get_margin(
            product, date, fallback_to_earliest=fallback_to_earliest
        )
        return None if margin is None else margin["維持保證金"]

    # === 比例型（股票股期）===
    def get_margin_rates(
        self,
        product_id: str,
        date: datetime.date,
        fallback_to_earliest: bool = False,
    ) -> Optional[Dict[str, float]]:
        """
        - Description:
            取得股票期貨在指定日期生效中的三種保證金**適用比例**（小數）
        - Parameters:
            - product_id: str
                股期代碼（Ex: CDF）
            - date: datetime.date
                查詢日
        - Return:
            - Optional[Dict[str, float]]
                三種比例；查無資料**或資料表尚未建立**時為 None
                （與 `get_margin()` 同一套語意）
        """

        # 表不存在時與 `get_margin()` 同樣回 None：**更舊的版本只有 `get_margin()`
        # 有這段**，於是同一個「還沒跑 ETL」的環境下，查金額回 None、查比例卻直接拋例外
        row: Optional[Tuple[Any, ...]] = self.dao.get_rates_in_effect(
            product_id, date, fallback_to_earliest=fallback_to_earliest
        )

        if row is None:
            return None
        return {
            "結算保證金適用比例": row[0],
            "維持保證金適用比例": row[1],
            "原始保證金適用比例": row[2],
        }

    def get_contract_size(self, product_id: str, date: datetime.date) -> Optional[int]:
        """
        - Description:
            取得股期的契約單位（股數）

            來源是 `futures_stock_universe` 的**快照序列**，故同樣取
            `snapshot_date <= date` 的最大者。

            ⚠️ **快照序列只回溯到本表建立之日**（2026-08-29），更早的日期會取到
            最早那份快照——契約單位在除權息後可能被調整過，那段期間的值不保證正確。
            精確的乘數歷史需另抓 TAIFEX 契約調整公告，目前未接。

            以**該商品自己**的快照序列為準（`per_product=True`），與回測乘數的查法不同，
            理由見 `FuturesStockUniverseDAO.get_contract_size()`。
        - Parameters:
            - product_id: str
                股期代碼（Ex: CDF）
            - date: datetime.date
                查詢日
        - Return:
            - Optional[int]
                契約單位（股）；查無資料時為 None
        """

        return self.universe_dao.get_contract_size(product_id, date, per_product=True)

    # 三種比例在同一列，差別只有取哪一欄；欄名寫死在此，呼叫端不必知道
    INITIAL_RATE_COLUMN: str = "原始保證金適用比例"
    MAINTENANCE_RATE_COLUMN: str = "維持保證金適用比例"

    def calculate_stock_futures_margin(
        self,
        product_id: str,
        date: datetime.date,
        price: float,
        fallback_to_earliest: bool = False,
    ) -> Optional[float]:
        """
        - Description:
            算出股票期貨的**每口原始保證金** ＝ 標的股價 × 契約單位 × 適用比例

            **這是本 API 與金額表最大的使用差異**：股期的來源只給比例，
            每口金額要自己算，而算式需要標的股價（由呼叫端提供，通常是當日收盤）。
        - Parameters:
            - product_id: str
                股期代碼（Ex: CDF）
            - date: datetime.date
                查詢日
            - price: float
                標的證券價格
        - Return:
            - Optional[float]
                每口原始保證金；比例或契約單位查不到時為 None
        """

        return self.calculate_stock_futures_margin_by_rate(
            product_id,
            date,
            price,
            self.INITIAL_RATE_COLUMN,
            fallback_to_earliest=fallback_to_earliest,
        )

    def calculate_stock_futures_maintenance_margin(
        self,
        product_id: str,
        date: datetime.date,
        price: float,
        fallback_to_earliest: bool = False,
    ) -> Optional[float]:
        """
        - Description:
            算出股票期貨的**每口維持保證金**（追繳門檻）

            **維持與原始是兩個獨立的比例**，同一列裡分成兩欄，不可用固定折數
            互推——理由同指數期貨那兩個公告金額。
        - Parameters:
            - product_id: str
                股期代碼（Ex: CDF）
            - date: datetime.date
                查詢日
            - price: float
                標的證券價格
        - Return:
            - Optional[float]
                每口維持保證金；比例或契約單位查不到時為 None
        """

        return self.calculate_stock_futures_margin_by_rate(
            product_id,
            date,
            price,
            self.MAINTENANCE_RATE_COLUMN,
            fallback_to_earliest=fallback_to_earliest,
        )

    def calculate_stock_futures_margin_by_rate(
        self,
        product_id: str,
        date: datetime.date,
        price: float,
        rate_column: str,
        fallback_to_earliest: bool = False,
    ) -> Optional[float]:
        """
        - Description:
            股期每口保證金的共用算式 ＝ 標的股價 × 契約單位 × 指定的適用比例

            原始與維持只差取哪一欄，兩處各寫一次算式必然漂移。
        - Parameters:
            - product_id / date / price: str, datetime.date, float
                股期代碼、查詢日、標的證券價格
            - rate_column: str
                要取的比例欄位（`INITIAL_RATE_COLUMN` 或 `MAINTENANCE_RATE_COLUMN`）
        - Return:
            - Optional[float]
                每口保證金；比例或契約單位查不到時為 None
        """

        rates: Optional[Dict[str, float]] = self.get_margin_rates(
            product_id, date, fallback_to_earliest=fallback_to_earliest
        )
        contract_size: Optional[int] = self.get_contract_size(product_id, date)

        if rates is None or contract_size is None:
            return None
        return price * contract_size * rates[rate_column]

    # === 涵蓋範圍 ===
    def get_covered_date_range(self, product: str) -> Optional[Dict[str, str]]:
        """
        取得該商品在金額表中的生效日範圍

        用來回答「這段回測期間有沒有保證金資料」——**早於 `earliest` 的日期一律
        查不到**，那是來源限制（2020/03 之前的公告附件是掃描影像）。
        """

        # 表還不存在＝尚未跑過 `--target futures_margin`；那是「還沒有資料」
        # 不是查詢寫錯，全新環境（CI、剛 clone）本來就會走到這裡
        covered: Optional[Tuple[str, str]] = self.dao.get_covered_date_range(product)

        if covered is None:
            return None
        return {"earliest": covered[0], "latest": covered[1]}

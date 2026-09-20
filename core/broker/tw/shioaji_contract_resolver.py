from typing import Any, Dict, Iterable, List, Optional

from loguru import logger

from core.utils import SHIOAJI_FUTURES_CATEGORY

"""
合約解析：把領域識別（`stock_id`、`product` ＋ `expiry`）換成 Shioaji 的 `Contract`

**查不到一律拋 `LookupError`，絕不回 `None`。** 這不是潔癖——Shioaji 的
`MultiContract.__getitem__` 實作是 `getattr(self, key, self._code2contract.get(key, None))`，
**查不到回 None 而且從不拋例外**。舊的 `OrderUtils` 就這樣把 `None` 往下傳給
`api.Order`，錯誤訊息完全指不到「代號打錯了」這個真正的原因。

合約的查詢鍵有兩組，不要混用：
- `symbol`：物件屬性名。股票是 `TSE2330`、期貨是 `TXF202601`（`{分類}{YYYYMM}`）。
- `code`：另一組代碼。股票是 `2330`，期貨是 `TXFI6`（月份字母 ＋ 年末碼）——
  **期貨的 `code` 不可使用**，字母碼跨年會重複。

`_code2contract` 是以 `code` 建的索引，且 `api.Contracts.Stocks` 這一層已把各交易所
（TSE／OTC／興櫃）合併，所以股票只要用代號查一次就涵蓋上市與上櫃。
"""


class ShioajiContractResolver:
    """
    - Description:
        合約解析器

        股票期貨的對照表在第一次使用時建立並快取：那是一次掃過三百多檔合約的
        操作，每次下單都重掃會拖垮尾盤那 4 分鐘。
    """

    # 保留策略名以外的識別；`__getattr__` 開頭為底線的 slot 是 Shioaji 內部用的
    _INTERNAL_PREFIX: str = "_"

    def __init__(self, api: Any) -> None:
        """
        - Description:
            建立解析器
        - Parameters:
            - api: Any
                已登入的 Shioaji API 物件（合約檔需已下載完成）
        """

        self.api: Any = api
        self._stock_futures_index: Optional[Dict[str, str]] = None

    # === 股票 ===
    def resolve_stock(self, stock_id: str) -> Any:
        """
        - Description:
            以股票代號取得合約；上市與上櫃一次查完
        - Parameters:
            - stock_id: str
                股票代號（Ex: 2330）
        - Return:
            - Any
                Shioaji 的股票合約
        - Raise:
            - LookupError
                合約檔裡沒有這個代號
        """

        contract: Optional[Any] = self.api.Contracts.Stocks[stock_id]
        if contract is None:
            raise LookupError(
                f"合約檔查無股票 {stock_id}；請確認代號正確且該檔仍在交易"
                "（下市、暫停交易或代號打錯都會走到這裡）"
            )
        return contract

    def resolve_stocks(self, stock_ids: Iterable[str]) -> Dict[str, Any]:
        """
        - Description:
            批次解析，**一次把所有查不到的都列出來**

            逐檔拋出會讓人修一個、跑一次、再撞下一個。啟動前的檢查要一次講完。
        - Parameters:
            - stock_ids: Iterable[str]
                股票代號清單
        - Return:
            - Dict[str, Any]
                `{stock_id: Contract}`
        - Raise:
            - LookupError
                任一代號查不到
        """

        resolved: Dict[str, Any] = {}
        missing: List[str] = []
        for stock_id in stock_ids:
            contract: Optional[Any] = self.api.Contracts.Stocks[stock_id]
            if contract is None:
                missing.append(stock_id)
            else:
                resolved[stock_id] = contract

        if missing:
            raise LookupError(f"合約檔查無下列股票：{sorted(missing)}")
        return resolved

    # === 指數期貨 ===
    def resolve_index_futures(self, product: str, expiry: str) -> Any:
        """
        - Description:
            以 TAIFEX 商品代碼與到期月份取得指數期貨合約
        - Parameters:
            - product: str
                TAIFEX 商品代碼（Ex: TX、MTX）
            - expiry: str
                到期月份（Ex: 202601）
        - Return:
            - Any
                Shioaji 的期貨合約
        - Raise:
            - LookupError
                商品代碼未登錄，或該月份的合約不存在
        """

        category: Optional[str] = SHIOAJI_FUTURES_CATEGORY.get(product)
        if category is None:
            raise LookupError(
                f"商品 {product} 未登錄 Shioaji 分類代碼；"
                f"目前登錄的有 {sorted(SHIOAJI_FUTURES_CATEGORY)}。"
                "兩邊的代碼沒有規律（MTX→MXF、TE→EXF），請實際登入核對後再加進對照表"
            )

        group: Optional[Any] = self.api.Contracts.Futures[category]
        if group is None:
            raise LookupError(f"合約檔查無期貨分類 {category}（商品 {product}）")

        symbol: str = f"{category}{expiry}"
        contract: Optional[Any] = group[symbol]
        if contract is None:
            raise LookupError(
                f"合約檔查無期貨合約 {symbol}；請確認該月份仍可交易"
                "（已到期、尚未掛牌都會走到這裡）"
            )
        return contract

    # === 股票期貨 ===
    def build_stock_futures_index(self, force: bool = False) -> Dict[str, str]:
        """
        - Description:
            掃描合約檔，建立「標的股票代號 → 期貨分類代碼」對照表並快取

            股期的分類代碼與標的股票代號沒有對應規則，只能從合約的
            `underlying_code` 反查。這是一次掃過三百多檔的操作，故快取；
            **每次下單都重掃會吃掉尾盤那 4 分鐘**。

            同一檔股票對到多個分類時取第一個並記 warning：那代表標準型與小型
            股期同時存在，要由呼叫端明確指定，不該由掃描順序決定。
        - Parameters:
            - force: bool
                True 時忽略快取重建（合約檔換日更新後使用）
        - Return:
            - Dict[str, str]
                `{stock_id: category}`
        """

        if self._stock_futures_index is not None and not force:
            return self._stock_futures_index

        index: Dict[str, str] = {}
        duplicated: List[str] = []
        futures: Any = self.api.Contracts.Futures

        for category in self._category_names(futures):
            group: Optional[Any] = futures[category]
            if group is None:
                continue
            for contract in group:
                underlying: Optional[str] = getattr(contract, "underlying_code", None)
                if not underlying:
                    continue
                if underlying in index and index[underlying] != category:
                    duplicated.append(underlying)
                    continue
                index[underlying] = category

        if duplicated:
            logger.warning(
                f"下列標的對到多個股期分類，已取先掃到的那個：{sorted(set(duplicated))}。"
                "標準型與小型股期同時存在時，應由呼叫端明確指定分類"
            )

        logger.info(f"股票期貨對照表建立完成，共 {len(index)} 檔標的")
        self._stock_futures_index = index
        return index

    def resolve_stock_futures(self, stock_id: str, expiry: str) -> Any:
        """
        - Description:
            以標的股票代號與到期月份取得股票期貨合約
        - Parameters:
            - stock_id: str
                標的股票代號（Ex: 2330）
            - expiry: str
                到期月份（Ex: 202601）
        - Return:
            - Any
                Shioaji 的股票期貨合約
        - Raise:
            - LookupError
                該標的沒有股期，或該月份的合約不存在
        """

        index: Dict[str, str] = self.build_stock_futures_index()
        category: Optional[str] = index.get(stock_id)
        if category is None:
            raise LookupError(
                f"標的 {stock_id} 沒有對應的股票期貨（合約檔共 {len(index)} 檔標的）"
            )

        group: Optional[Any] = self.api.Contracts.Futures[category]
        symbol: str = f"{category}{expiry}"
        contract: Optional[Any] = None if group is None else group[symbol]
        if contract is None:
            raise LookupError(
                f"合約檔查無股票期貨 {symbol}（標的 {stock_id}）；請確認該月份仍可交易"
            )
        return contract

    # === 共用 ===
    @staticmethod
    def _category_names(futures: Any) -> List[str]:
        """
        取得期貨分類代碼清單

        Shioaji 的 `BaseIterContracts.keys()` 會濾掉底線開頭的內部 slot；
        **直接迭代物件拿到的是合約群組而不是名稱**，兩者不要搞混。
        """

        return [
            name
            for name in futures.keys()
            if not name.startswith(ShioajiContractResolver._INTERNAL_PREFIX)
        ]

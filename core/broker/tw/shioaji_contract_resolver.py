from typing import Any, Dict, Iterable, List, Optional, Tuple

from loguru import logger

from core.utils import SHIOAJI_FUTURES_CATEGORY

"""
合約解析：把領域識別（`stock_id`、`product` ＋ `expiry`）換成 Shioaji 的合約

**查不到一律拋 `LookupError`，絕不回 `None`。** Shioaji 查不到合約時回的是 `None`，
舊的 `OrderUtils` 就這樣把 `None` 往下傳給下單，錯誤訊息完全指不到「代號打錯了」
這個真正的原因。

只用 shioaji 1.7 合約容器明確提供的操作：`get(code)`、屬性取分類、迭代。
舊版的 `keys()`、`_code2contract` 索引與合約的 `symbol` 屬性（`TXF202601`）在 1.7
都已不存在，期貨月份一律比對 `delivery_month`（`YYYYMM`）：
- **期貨的 `code` 不可拿來推月份**：它是 `TXFI6` 這種月份字母碼，跨年會重複。
- 連續月別名（`TXFR1`／`TXFR2`）與真實月份合約的 `delivery_month` 相同，
  要排除，否則同一個月份會對到兩張合約。

`api.Contracts.Stocks` 這一層已把各交易所（TSE／OTC／興櫃）合併，
股票只要用代號查一次就涵蓋上市與上櫃。
"""

# 期貨連續月別名的代碼結尾；它們與真實月份合約同月份，解析時要排除
CONTINUOUS_ALIAS_SUFFIXES: Tuple[str, ...] = ("R1", "R2")

# Shioaji 分類代碼 → 專案商品代碼（`SHIOAJI_FUTURES_CATEGORY` 的反向）
_CATEGORY_TO_PRODUCT: Dict[str, str] = {
    category: product for product, category in SHIOAJI_FUTURES_CATEGORY.items()
}


def to_futures_product(category: str) -> str:
    """
    - Description:
        Shioaji 的期貨分類代碼 → 專案的商品代碼（Ex: `TXF` → `TX`、`MXF` → `MTX`）

        專案的期貨代號是 `{商品}{YYYYMM}`（`FuturesOrder.symbol`），券商端全用分類代碼；
        不換的話，同一口台指期在訂單是 `TX202610`、在行情與部位是 `TXF202610`，
        策略、歸屬帳與對帳各自以為是不同的契約。未登錄的分類（例如股票期貨）原樣回傳。
    - Parameters:
        - category: str
            Shioaji 分類代碼
    - Return:
        - str
            專案商品代碼
    """

    return _CATEGORY_TO_PRODUCT.get(category, category)


class ShioajiContractResolver:
    """
    - Description:
        合約解析器

        股票期貨的對照表在第一次使用時建立並快取：那是一次掃過三百多檔合約的
        操作，每次下單都重掃會拖垮尾盤那 4 分鐘。
    """

    def __init__(self, api: Any) -> None:
        """
        - Description:
            建立解析器
        - Parameters:
            - api: Any
                已登入的 Shioaji API 物件（合約於第一次查詢時才載入）
        """

        self.api: Any = api
        self._stock_futures_index: Optional[Dict[str, str]] = None
        # 期貨月份字母碼 → 專案代號；合約屬性在一個交易日內不變，查一次就夠
        self._futures_symbols: Dict[str, str] = {}

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

        contract: Optional[Any] = self.api.Contracts.Stocks.get(stock_id)
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
            contract: Optional[Any] = self.api.Contracts.Stocks.get(stock_id)
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

        group: Optional[Any] = getattr(self.api.Contracts.Futures, category, None)
        if group is None:
            raise LookupError(f"合約檔查無期貨分類 {category}（商品 {product}）")

        contract: Optional[Any] = self._find_by_delivery_month(group, expiry)
        if contract is None:
            raise LookupError(
                f"合約檔查無期貨合約 {category} {expiry}；請確認該月份仍可交易"
                "（已到期、尚未掛牌都會走到這裡）"
            )
        return contract

    def to_futures_symbol(self, code: str) -> str:
        """
        - Description:
            券商的期貨代碼（月份字母碼，Ex: `TXFJ6`）→ 專案代號（`TX202610`）

            期貨的成交回報、委託回報與部位查詢都以月份字母碼表示契約
            （2026-09-22 模擬環境實測），而字母碼**跨年重複**，不能拿來推月份；
            一律查合約的 `root` 與 `delivery_month`。查不到時原樣回傳並記 warning：
            那筆回報仍要入列，代號對不上會由對帳抓出來。
        - Parameters:
            - code: str
                券商的期貨代碼
        - Return:
            - str
                `{商品}{YYYYMM}`；查不到合約時為原代碼
        """

        if not code or code in self._futures_symbols:
            return self._futures_symbols.get(code, code)

        contract: Optional[Any] = self.api.Contracts.Futures.get(code)
        category: Optional[str] = (
            self._category_of(contract) if contract is not None else None
        )
        delivery: str = str(getattr(contract, "delivery_month", "") or "")
        if not category or not delivery:
            logger.warning(f"期貨代碼 {code} 查無合約或月份，維持原代碼")
            return code

        symbol: str = f"{to_futures_product(str(category))}{delivery}"
        self._futures_symbols[code] = symbol
        return symbol

    # === 股票期貨 ===
    def build_stock_futures_index(self, force: bool = False) -> Dict[str, str]:
        """
        - Description:
            掃描合約檔，建立「標的股票代號 → 期貨分類代碼」對照表並快取

            股期的分類代碼與標的股票代號沒有對應規則，只能從合約的
            `underlying_code` 反查；分類代碼取合約自己的 `root`（1.7 起）或
            `category`。這是一次掃過三百多檔的操作，故快取；
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

        for group in futures:
            for contract in group:
                underlying: Optional[str] = getattr(contract, "underlying_code", None)
                category: Optional[str] = self._category_of(contract)
                if not underlying or not category:
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

        group: Optional[Any] = getattr(self.api.Contracts.Futures, category, None)
        contract: Optional[Any] = (
            None if group is None else self._find_by_delivery_month(group, expiry)
        )
        if contract is None:
            raise LookupError(
                f"合約檔查無股票期貨 {category} {expiry}（標的 {stock_id}）；"
                "請確認該月份仍可交易"
            )
        return contract

    # === 共用 ===
    @staticmethod
    def _category_of(contract: Any) -> Optional[str]:
        """期貨合約的分類代碼（Ex: TXF、CDF）；1.7 起欄位名是 `root`"""

        return getattr(contract, "root", None) or getattr(contract, "category", None)

    @staticmethod
    def _find_by_delivery_month(group: Any, expiry: str) -> Optional[Any]:
        """
        - Description:
            在同一分類裡找到期月份相符的合約

            **排除連續月別名**：`TXFR1` 與當月合約的 `delivery_month` 相同，
            不排除的話同一月份會對到兩張，取到哪張由迭代順序決定。
            排除後仍有多張時拋出，不猜。
        - Parameters:
            - group: Any
                某一期貨分類的合約群組
            - expiry: str
                到期月份（Ex: 202601）
        - Return:
            - Optional[Any]
                相符的合約；沒有時為 None
        - Raise:
            - LookupError
                同一月份有多張非別名合約
        """

        matches: List[Any] = [
            contract
            for contract in group
            if str(getattr(contract, "delivery_month", "")) == expiry
            and not str(getattr(contract, "code", "")).endswith(
                CONTINUOUS_ALIAS_SUFFIXES
            )
        ]
        if len(matches) > 1:
            raise LookupError(
                f"到期月份 {expiry} 對到多張合約："
                f"{sorted(str(getattr(c, 'code', '')) for c in matches)}"
            )
        return matches[0] if matches else None

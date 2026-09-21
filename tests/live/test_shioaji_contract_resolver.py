from typing import Any, Dict, Iterator, List, Optional

import pytest

from core.broker.tw.shioaji_contract_resolver import ShioajiContractResolver
from core.utils import SHIOAJI_FUTURES_CATEGORY

"""
合約解析：查不到一律拋 `LookupError`，絕不回 `None`

**假物件只提供 shioaji 1.7 合約容器明確定義的操作**：`get(code)`（查不到回 `None`）、
以屬性取分類、迭代。舊版的 `keys()`、`_code2contract` 與合約的 `symbol` 在 1.7
都不存在，假物件若仍提供它們，解析器誤用時這份測試也驗不出來。
"""


class FakeContract:
    """最小合約：只帶解析會用到的欄位（1.7 的 `FuturesInfo` 以 `root` 表示分類）"""

    def __init__(
        self,
        code: str,
        delivery_month: str = "",
        root: Optional[str] = None,
        underlying_code: Optional[str] = None,
    ) -> None:
        self.code: str = code
        self.delivery_month: str = delivery_month
        self.root: Optional[str] = root
        self.underlying_code: Optional[str] = underlying_code


class FakeGroup:
    """對應 1.7 的 `ContractGroup`：可迭代，以 code 查詢"""

    def __init__(self, contracts: List[FakeContract]) -> None:
        self._contracts: List[FakeContract] = contracts

    def __iter__(self) -> Iterator[FakeContract]:
        return iter(self._contracts)

    def get(self, code: str) -> Optional[FakeContract]:
        return next((c for c in self._contracts if c.code == code), None)


class FakeCategory:
    """
    對應 1.7 的 `ContractCategory`

    分類以屬性存取、迭代得到的是群組；`get()` 跨群組以 code 查，查不到回 None
    """

    def __init__(self, groups: Dict[str, FakeGroup]) -> None:
        self._groups: Dict[str, FakeGroup] = groups

    def __getattr__(self, name: str) -> FakeGroup:
        groups: Dict[str, FakeGroup] = self.__dict__.get("_groups", {})
        if name not in groups:
            raise AttributeError(name)
        return groups[name]

    def __iter__(self) -> Iterator[FakeGroup]:
        return iter(self._groups.values())

    def get(self, code: str) -> Optional[FakeContract]:
        for group in self._groups.values():
            found: Optional[FakeContract] = group.get(code)
            if found is not None:
                return found
        return None


class FakeApi:
    def __init__(self, stocks: Any, futures: Any) -> None:
        self.Contracts = type("Contracts", (), {"Stocks": stocks, "Futures": futures})()


@pytest.fixture
def api() -> FakeApi:
    """一檔上市、一檔上櫃、大台兩個月份與連續月別名、兩檔股期"""

    stocks: FakeCategory = FakeCategory(
        {
            "TSE": FakeGroup([FakeContract(code="2330")]),
            "OTC": FakeGroup([FakeContract(code="6488")]),
        }
    )
    futures: FakeCategory = FakeCategory(
        {
            "TXF": FakeGroup(
                [
                    FakeContract(code="TXFA6", delivery_month="202601", root="TXF"),
                    FakeContract(code="TXFB6", delivery_month="202602", root="TXF"),
                    # 連續月別名：與 202601 同月份，解析時要排除
                    FakeContract(code="TXFR1", delivery_month="202601", root="TXF"),
                ]
            ),
            "CDF": FakeGroup(
                [
                    FakeContract(
                        code="CDFA6",
                        delivery_month="202601",
                        root="CDF",
                        underlying_code="2330",
                    )
                ]
            ),
            "DHF": FakeGroup(
                [
                    FakeContract(
                        code="DHFA6",
                        delivery_month="202601",
                        root="DHF",
                        underlying_code="2317",
                    )
                ]
            ),
        }
    )
    return FakeApi(stocks, futures)


@pytest.fixture
def resolver(api: FakeApi) -> ShioajiContractResolver:
    return ShioajiContractResolver(api)


# === 股票 ===
def test_resolve_stock_covers_listed_and_otc(
    resolver: ShioajiContractResolver,
) -> None:
    """
    上市與上櫃一次查完

    `api.Contracts.Stocks` 這一層已把各交易所合併，以 code 查一次即可，
    不必自己依序試 TSE、OTC——依序試的寫法會在新增交易所（興櫃）時漏掉。
    """

    assert resolver.resolve_stock("2330").code == "2330"
    assert resolver.resolve_stock("6488").code == "6488"


def test_missing_stock_raises_instead_of_returning_none(
    resolver: ShioajiContractResolver,
) -> None:
    """
    查不到要拋出

    Shioaji 自己查不到是回 `None`。把 `None` 往下傳給 `api.Order` 的話，
    錯誤訊息完全指不到「代號打錯了」這個真正的原因——舊 `OrderUtils` 就是這樣。
    """

    with pytest.raises(LookupError, match="9999"):
        resolver.resolve_stock("9999")


def test_batch_resolution_reports_every_missing_symbol(
    resolver: ShioajiContractResolver,
) -> None:
    """
    批次解析要一次列出所有查不到的

    逐檔拋出會讓人修一個、跑一次、再撞下一個；啟動前的檢查要一次講完。
    """

    with pytest.raises(LookupError) as error:
        resolver.resolve_stocks(["2330", "9999", "8888"])

    message: str = str(error.value)
    assert "8888" in message and "9999" in message
    assert "2330" not in message


def test_batch_resolution_returns_a_symbol_keyed_map(
    resolver: ShioajiContractResolver,
) -> None:
    """回傳以代號為鍵，呼叫端不必依賴順序"""

    resolved: Dict[str, Any] = resolver.resolve_stocks(["2330", "6488"])

    assert set(resolved) == {"2330", "6488"}


# === 指數期貨 ===
def test_resolve_index_futures_matches_delivery_month_not_code(
    resolver: ShioajiContractResolver,
) -> None:
    """
    以 `delivery_month` 比對到期月份，不用 `code`

    期貨的 `code`（`TXFA6`＝月份字母 ＋ 年末碼）**跨年會重複**，
    拿它當鍵會在隔年查到錯的合約，而且是一個合法的合約物件，不會報錯。
    """

    contract: Any = resolver.resolve_index_futures("TX", "202601")

    assert contract.code == "TXFA6"
    assert SHIOAJI_FUTURES_CATEGORY["TX"] == "TXF"


def test_continuous_alias_is_not_resolved_as_the_month(
    resolver: ShioajiContractResolver,
) -> None:
    """
    連續月別名（`TXFR1`）不可當成該月份的合約

    它與當月合約的 `delivery_month` 相同；不排除的話同一月份對到兩張，
    取到哪張由迭代順序決定。
    """

    assert resolver.resolve_index_futures("TX", "202602").code == "TXFB6"
    assert resolver.resolve_index_futures("TX", "202601").code == "TXFA6"


def test_unregistered_product_lists_the_known_ones(
    resolver: ShioajiContractResolver,
) -> None:
    """
    未登錄的商品要在訊息裡列出已登錄的

    兩邊的代碼沒有規律（MTX→MXF、TE→EXF），看不到對照表的人只會繼續猜。
    """

    with pytest.raises(LookupError) as error:
        resolver.resolve_index_futures("XXX", "202601")

    assert "TX" in str(error.value)


def test_missing_expiry_raises(resolver: ShioajiContractResolver) -> None:
    """該月份不存在（已到期或尚未掛牌）也要拋出"""

    with pytest.raises(LookupError, match="TXF 209912"):
        resolver.resolve_index_futures("TX", "209912")


# === 股票期貨 ===
def test_stock_futures_index_is_built_from_underlying_code(
    resolver: ShioajiContractResolver,
) -> None:
    """
    股期分類要從合約的 `underlying_code` 反查

    分類代碼（CDF、DHF）與標的股票代號之間沒有任何規則，猜不出來。
    """

    index: Dict[str, str] = resolver.build_stock_futures_index()

    assert index == {"2330": "CDF", "2317": "DHF"}


def test_stock_futures_index_is_cached(
    resolver: ShioajiContractResolver, api: FakeApi
) -> None:
    """
    對照表要快取

    那是一次掃過三百多檔合約的操作，每次下單都重掃會吃掉尾盤那 4 分鐘。
    """

    first: Dict[str, str] = resolver.build_stock_futures_index()
    api.Contracts.Futures = FakeCategory({})  # 掃不到任何東西
    second: Dict[str, str] = resolver.build_stock_futures_index()

    assert second is first


def test_force_rebuild_ignores_the_cache(
    resolver: ShioajiContractResolver, api: FakeApi
) -> None:
    """合約檔換日更新後要能重建"""

    resolver.build_stock_futures_index()
    api.Contracts.Futures = FakeCategory({})

    assert resolver.build_stock_futures_index(force=True) == {}


def test_resolve_stock_futures(resolver: ShioajiContractResolver) -> None:
    """以標的代號 ＋ 月份取得股期合約"""

    assert resolver.resolve_stock_futures("2330", "202601").code == "CDFA6"


def test_stock_without_futures_raises(resolver: ShioajiContractResolver) -> None:
    """沒有股期的標的要拋出，不可靜默略過"""

    with pytest.raises(LookupError, match="6488"):
        resolver.resolve_stock_futures("6488", "202601")


def test_duplicate_underlying_keeps_the_first_and_warns(api: FakeApi) -> None:
    """
    同一標的對到多個分類時取先掃到的並記 warning

    那代表標準型與小型股期同時存在，應由呼叫端明確指定，
    不該由掃描順序決定下的是哪一種契約——兩者的乘數差 20 倍。
    """

    api.Contracts.Futures = FakeCategory(
        {
            "CDF": FakeGroup(
                [FakeContract(code="CDFA6", root="CDF", underlying_code="2330")]
            ),
            "CDS": FakeGroup(
                [FakeContract(code="CDSA6", root="CDS", underlying_code="2330")]
            ),
        }
    )
    index: Dict[str, str] = ShioajiContractResolver(api).build_stock_futures_index()

    assert index["2330"] in {"CDF", "CDS"}
    assert len(index) == 1

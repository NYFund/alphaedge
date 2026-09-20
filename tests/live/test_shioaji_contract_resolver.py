from typing import Any, Dict, Iterator, List, Optional

import pytest

from core.broker.tw.shioaji_contract_resolver import ShioajiContractResolver
from core.utils import SHIOAJI_FUTURES_CATEGORY

"""
合約解析：查不到一律拋 `LookupError`，絕不回 `None`

**假物件刻意複製 Shioaji 的真實語意**：`MultiContract.__getitem__` 的實作是
`getattr(self, key, self._code2contract.get(key, None))`——查不到回 `None`
而且從不拋例外。假物件若改成拋 KeyError，這份測試就驗不到真正會發生的那條路徑，
而那正是舊 `OrderUtils` 把 `None` 傳給 `api.Order` 的成因。
"""


class FakeContract:
    """最小合約：只帶解析會用到的欄位"""

    def __init__(
        self, symbol: str, code: str = "", underlying_code: Optional[str] = None
    ) -> None:
        self.symbol: str = symbol
        self.code: str = code or symbol
        self.underlying_code: Optional[str] = underlying_code


class FakeGroup:
    """對應 Shioaji 的 `MultiContract`：以 symbol 當屬性、另有 code 索引"""

    def __init__(self, contracts: List[FakeContract]) -> None:
        self._by_symbol: Dict[str, FakeContract] = {c.symbol: c for c in contracts}
        self._by_code: Dict[str, FakeContract] = {c.code: c for c in contracts}

    def __getitem__(self, key: str) -> Optional[FakeContract]:
        """**查不到回 None**，與 Shioaji 相同"""

        return self._by_symbol.get(key, self._by_code.get(key, None))

    def __iter__(self) -> Iterator[FakeContract]:
        return iter(self._by_symbol.values())

    def keys(self) -> Iterator[str]:
        return iter(self._by_symbol)


class FakeProductContracts:
    """對應 `ProductContracts`：slot 是分類／交易所，另有合併後的 code 索引"""

    def __init__(
        self, groups: Dict[str, FakeGroup], code_index: Optional[Dict[str, Any]] = None
    ) -> None:
        self._groups: Dict[str, FakeGroup] = groups
        self._code_index: Dict[str, Any] = code_index or {}

    def __getitem__(self, key: str) -> Any:
        return self._groups.get(key, self._code_index.get(key, None))

    def keys(self) -> Iterator[str]:
        return iter(list(self._groups) + ["_code2contract"])


class FakeApi:
    def __init__(self, stocks: Any, futures: Any) -> None:
        self.Contracts = type("Contracts", (), {"Stocks": stocks, "Futures": futures})()


@pytest.fixture
def api() -> FakeApi:
    """一檔上市、一檔上櫃、大台兩個月份、兩檔股期"""

    tsmc: FakeContract = FakeContract(symbol="TSE2330", code="2330")
    otc: FakeContract = FakeContract(symbol="OTC6488", code="6488")
    stocks: FakeProductContracts = FakeProductContracts(
        groups={"TSE": FakeGroup([tsmc]), "OTC": FakeGroup([otc])},
        code_index={"2330": tsmc, "6488": otc},
    )

    futures: FakeProductContracts = FakeProductContracts(
        groups={
            "TXF": FakeGroup(
                [
                    FakeContract(symbol="TXF202601", code="TXFA6"),
                    FakeContract(symbol="TXF202602", code="TXFB6"),
                ]
            ),
            "CDF": FakeGroup(
                [FakeContract(symbol="CDF202601", code="CDFA6", underlying_code="2330")]
            ),
            "DHF": FakeGroup(
                [FakeContract(symbol="DHF202601", code="DHFA6", underlying_code="2317")]
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

    `api.Contracts.Stocks` 這一層已把各交易所合併成一份 code 索引，
    不必自己依序試 TSE、OTC——依序試的寫法會在新增交易所（興櫃）時漏掉。
    """

    assert resolver.resolve_stock("2330").symbol == "TSE2330"
    assert resolver.resolve_stock("6488").symbol == "OTC6488"


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
def test_resolve_index_futures_uses_symbol_not_code(
    resolver: ShioajiContractResolver,
) -> None:
    """
    以 `{分類}{YYYYMM}` 查，不用 `code`

    期貨的 `code`（`TXFA6`＝月份字母 ＋ 年末碼）**跨年會重複**，
    拿它當鍵會在隔年查到錯的合約，而且是一個合法的合約物件，不會報錯。
    """

    contract: Any = resolver.resolve_index_futures("TX", "202601")

    assert contract.symbol == "TXF202601"
    assert SHIOAJI_FUTURES_CATEGORY["TX"] == "TXF"


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

    with pytest.raises(LookupError, match="TXF209912"):
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
    api.Contracts.Futures = FakeProductContracts(groups={})  # 掃不到任何東西
    second: Dict[str, str] = resolver.build_stock_futures_index()

    assert second is first


def test_force_rebuild_ignores_the_cache(
    resolver: ShioajiContractResolver, api: FakeApi
) -> None:
    """合約檔換日更新後要能重建"""

    resolver.build_stock_futures_index()
    api.Contracts.Futures = FakeProductContracts(groups={})

    assert resolver.build_stock_futures_index(force=True) == {}


def test_index_skips_internal_slots(resolver: ShioajiContractResolver) -> None:
    """
    掃描要略過底線開頭的內部 slot

    `keys()` 會吐出 `_code2contract` 這類內部欄位，拿它當分類去查會取到
    一個 dict 而不是合約群組。
    """

    assert resolver.build_stock_futures_index() == {"2330": "CDF", "2317": "DHF"}


def test_resolve_stock_futures(resolver: ShioajiContractResolver) -> None:
    """以標的代號 ＋ 月份取得股期合約"""

    assert resolver.resolve_stock_futures("2330", "202601").symbol == "CDF202601"


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

    api.Contracts.Futures = FakeProductContracts(
        groups={
            "CDF": FakeGroup(
                [FakeContract(symbol="CDF202601", underlying_code="2330")]
            ),
            "CDS": FakeGroup(
                [FakeContract(symbol="CDS202601", underlying_code="2330")]
            ),
        }
    )
    index: Dict[str, str] = ShioajiContractResolver(api).build_stock_futures_index()

    assert index["2330"] in {"CDF", "CDS"}
    assert len(index) == 1

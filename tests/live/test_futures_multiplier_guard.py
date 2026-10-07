import datetime
from typing import Any, List

import pytest

from core.live.datafeed.tw.futures_live_datafeed import TwFuturesLiveDataFeed

"""
實盤期貨取不到契約乘數時：拋出而不是回 0，盤前組裝只略過那一個契約

乘數 0 不是異常值，而是一個看起來正常的數字——部位建構算出 0 口、PnL 算成 0，
沒有任何錯誤訊息。回測那一側與 `FuturesPositionManager.get_multiplier()` 都是查不到就中斷。
"""


class Resolver:
    """契約月份字母碼 → 專案契約代號"""

    def to_futures_symbol(self, code: str) -> str:
        return {"TXFJ6": "TX202610", "ZZFJ6": "ZZ202610"}.get(code, code)


class Broker:
    resolver: Resolver = Resolver()


class Contract:
    """與 shioaji 1.7 的 `FuturesInfo` 同形狀：沒有 `symbol`"""

    def __init__(self, code: str, multiplier: int = 0) -> None:
        self.code: str = code
        self.reference: float = 24000.0
        self.limit_up: float = 26400.0
        self.limit_down: float = 21600.0
        if multiplier:
            self.multiplier: int = multiplier


def make_feed() -> TwFuturesLiveDataFeed:
    return TwFuturesLiveDataFeed(
        broker=Broker(),
        calendar_sources=[],
        now_provider=lambda: datetime.datetime(2026, 9, 24, 8, 45),
    )


def test_unknown_multiplier_raises() -> None:
    """登錄表與合約欄位都沒有乘數時拋 `KeyError`"""

    with pytest.raises(KeyError, match="ZZ"):
        TwFuturesLiveDataFeed._resolve_multiplier("ZZ", Contract("ZZFJ6"))


def test_pre_open_skips_only_the_contract_without_multiplier() -> None:
    """盤前組裝略過取不到乘數的契約，其他契約照常產出"""

    quotes: List[Any] = make_feed()._build_pre_open_quotes(
        [Contract("ZZFJ6"), Contract("TXFJ6", multiplier=200)]
    )

    assert [(quote.product, quote.multiplier) for quote in quotes] == [("TX", 200)]

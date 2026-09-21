from enum import Enum
from pathlib import Path
from typing import Dict, Set, Tuple, Type

import pytest

from core.utils.constant import (
    FuturesOCType,
    FuturesPriceType,
    LiveOrderStatus,
    OrderState,
    OrderType,
    QuoteType,
    Status,
    StockOrderCond,
    StockOrderLot,
    StockPriceType,
)

_PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]

"""
專案自訂的下單相關 Enum 必須與 Shioaji 那份一致

`core/utils/callback.py` 的 `order_cb(stat, msg)` 由 Shioaji 回呼，`stat` 是
`shioaji.constant.OrderState`；而函式裡拿來比較的是專案自己那份。兩者都是
`str` Enum，`==` 比的是字串值，**值一樣就成立、不一樣就永遠不成立**——
不成立時不會拋錯，只是實盤成交回報安靜地不發出通知。

舊版把兩個 `OrderState` 都 import（後者覆蓋前者），
於是「用的是哪一份」連讀程式碼都看不出來。已刪掉被覆蓋的那行，
兩份是否同步改由本測試盯住。

**本檔量的是 `uv.lock` 鎖定的 shioaji 版本**（本機、CI、Docker 都裝同一份）。
shioaji 1.7 起 `OrderState` 改由原生模組提供、不再是 Python Enum——升版時本檔會紅，
那是在提醒 `order_cb` 的比較前提變了，不是測試寫錯。

下單執行參數（`StockPriceType`／`FuturesPriceType`／`OrderType`／`StockOrderLot`／
`StockOrderCond`／`FuturesOCType`）同樣是兩份 `str` Enum 對打，失效方式也一樣安靜：
值對不上時委託不會拋錯，只會被券商以一個指不到原因的訊息退回，或更糟——**以另一種
委託條件成交**（例如借券單掉回融券）。故一併在此逐一比對。

`Action` 刻意不納入：本專案的 `Action` 多了 `OPEN`／`CLOSE` 兩個成員，成員名也用
大寫（`BUY` vs Shioaji 的 `Buy`），本來就不是鏡像關係，轉換一律走 mapper。
"""


def _load_shioaji_order_state() -> Type[Enum]:
    """
    - Description:
        取得 Shioaji 的 `OrderState`，並確認它仍是 Python Enum

        不先檢查的話，升版後會在迭代時丟出 `TypeError: 'type' object is not iterable`，
        完全看不出是 shioaji 換了實作。
    - Return:
        - Type[Enum]
            Shioaji 的 `OrderState`
    """

    from shioaji.constant import OrderState as ShioajiOrderState

    if not (
        isinstance(ShioajiOrderState, type) and issubclass(ShioajiOrderState, Enum)
    ):
        import shioaji

        pytest.fail(
            f"shioaji {getattr(shioaji, '__version__', '?')} 的 OrderState 已不是 "
            "Python Enum。`core/utils/callback.py` 的 `order_cb` 以 `==` 比對字串值的"
            "前提需要重新確認；不要只改這條測試讓它通過。"
        )
    return ShioajiOrderState


def test_order_state_matches_shioaji() -> None:
    """成員名稱與字串值都要與 Shioaji 那份逐一相同"""

    shioaji_order_state: Type[Enum] = _load_shioaji_order_state()

    ours: Dict[str, str] = {member.name: member.value for member in OrderState}
    theirs: Dict[str, str] = {
        member.name: member.value for member in shioaji_order_state
    }

    assert ours == theirs


def test_order_state_compares_across_both_enums() -> None:
    """
    跨兩份 Enum 的 `==` 必須成立

    這是 `order_cb` 實際做的事：拿 Shioaji 傳進來的值比對專案自己的成員。
    任何一邊改成非 `str` 的 Enum，這個比較會安靜地變成永遠 False。
    """

    shioaji_order_state: Type[Enum] = _load_shioaji_order_state()

    assert shioaji_order_state.StockDeal == OrderState.StockDeal
    assert shioaji_order_state.FuturesDeal == OrderState.FuturesDeal


# 本專案 Enum → `shioaji.constant` 的同名 Enum；成員名與值都應逐一相同
_MIRRORED_ENUMS: Dict[str, Type[Enum]] = {
    "StockPriceType": StockPriceType,
    "FuturesPriceType": FuturesPriceType,
    "OrderType": OrderType,
    "StockOrderLot": StockOrderLot,
    "StockOrderCond": StockOrderCond,
    "FuturesOCType": FuturesOCType,
    "QuoteType": QuoteType,
    "Status": Status,
}

# ratchet：我們有、但鎖定版的 shioaji 沒有的成員（成員 → 理由）。
# **升版後 shioaji 補上了就要把該條移除**，否則這份清單會慢慢變成沒人看的裝飾
_MEMBERS_MISSING_IN_SHIOAJI: Dict[Tuple[str, str], str] = {
    (
        "StockOrderCond",
        "SBLShort",
    ): "shioaji 1.3.3 的 StockOrderCond 只有 Cash／MarginTrading／ShortSelling；"
    "借券賣出要先升版才送得出去",
}


def _load_shioaji_enum(name: str) -> Type[Enum]:
    """取得 `shioaji.constant` 的同名 Enum，並確認它仍是 Python Enum"""

    import shioaji.constant

    obj: object = getattr(shioaji.constant, name, None)
    if not (isinstance(obj, type) and issubclass(obj, Enum)):
        import shioaji

        pytest.fail(
            f"shioaji {getattr(shioaji, '__version__', '?')} 的 {name} 不存在或已不是 "
            "Python Enum；委託轉換的比較前提需要重新確認，不要只改這條測試讓它通過。"
        )
    return obj


@pytest.mark.parametrize("name", sorted(_MIRRORED_ENUMS))
def test_execution_enum_values_match_shioaji(name: str) -> None:
    """
    兩邊都有的成員，字串值必須逐一相同

    值對不上不會拋錯：券商只會退單，或以另一種委託條件成交。
    """

    ours: Type[Enum] = _MIRRORED_ENUMS[name]
    theirs: Type[Enum] = _load_shioaji_enum(name)
    their_values: Dict[str, str] = {m.name: m.value for m in theirs}

    mismatched: Dict[str, Tuple[str, str]] = {
        member.name: (member.value, their_values[member.name])
        for member in ours
        if member.name in their_values and member.value != their_values[member.name]
    }

    assert mismatched == {}, (
        f"{name} 與 shioaji 的值不一致（本專案, shioaji）：{mismatched}"
    )


def test_members_missing_in_shioaji_match_the_ratchet() -> None:
    """
    「我們有、shioaji 沒有」的成員必須與 ratchet 清單完全相同

    兩個方向都要擋：**新出現**的落差代表有人加了一個送不出去的值；
    **消失**的落差代表升版後 shioaji 補上了，該把它從清單移除、並讓 mapper 真的支援。
    """

    missing: Dict[Tuple[str, str], str] = {}
    for name, ours in _MIRRORED_ENUMS.items():
        their_names: Set[str] = {m.name for m in _load_shioaji_enum(name)}
        for member in ours:
            if member.name not in their_names:
                missing[(name, member.name)] = ""

    assert set(missing) == set(_MEMBERS_MISSING_IN_SHIOAJI), (
        "本專案 Enum 與 shioaji 的成員落差和 ratchet 清單不符："
        f"新出現 {sorted(set(missing) - set(_MEMBERS_MISSING_IN_SHIOAJI))}、"
        f"已消失 {sorted(set(_MEMBERS_MISSING_IN_SHIOAJI) - set(missing))}"
    )


def test_live_order_status_does_not_collide_with_broker_status() -> None:
    """
    `LiveOrderStatus`（本地狀態機）與 `Status`（券商回報鏡像）的值域不得相交

    兩者都是 `str` Enum，值一旦撞上，`local_status == broker_status` 這種比較會
    **安靜地成立**，而重啟接管要靠的正是「本地以為送出去了」和「券商說收到了」
    這兩件事的差別。
    """

    ours: Set[str] = {member.value for member in LiveOrderStatus}
    broker: Set[str] = {member.value for member in Status}

    assert ours & broker == set()

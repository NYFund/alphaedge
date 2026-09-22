import inspect
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Set, Tuple, Type

import pytest
import shioaji

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

`ShioajiExecutionHandler.parse(stat, msg)` 處理 Shioaji 的委託與成交回呼，`stat` 是
`shioaji.OrderState`；而函式裡拿來比較的是專案自己那份。比較用的是 `==`，
**值一樣就成立、不一樣就永遠不成立**——不成立時不會拋錯，
只是實盤成交回報安靜地不發出通知。

**本檔量的是 `uv.lock` 鎖定的 shioaji 版本**（本機、CI、Docker 都裝同一份）。
shioaji 1.7 起這些 Enum 改由原生模組提供：不再是 Python Enum、**不能迭代**，
多數成員也沒有 `.name`。但成員仍是 `str`，`==` 比的仍是字串值——那才是
`order_cb` 與委託轉換真正依賴的前提，本檔直接驗它，而不是驗「是不是 Python Enum」。

下單執行參數（`StockPriceType`／`FuturesPriceType`／`OrderType`／`StockOrderLot`／
`StockOrderCond`／`FuturesOCType`）的失效方式一樣安靜：值對不上時委託不會拋錯，
只會被券商以一個指不到原因的訊息退回，或更糟——**以另一種委託條件成交**
（例如借券單掉回融券）。故一併在此逐一比對。

`Action` 刻意不納入：本專案的 `Action` 多了 `OPEN`／`CLOSE` 兩個成員，成員名也用
大寫（`BUY` vs Shioaji 的 `Buy`），本來就不是鏡像關係，轉換一律走 mapper。
"""


def _shioaji_members(name: str) -> Dict[str, Any]:
    """
    - Description:
        取得 shioaji 某個 Enum 的全部成員（成員名 → 成員）

        shioaji 1.7 的 Enum 不能迭代，成員只能從類別屬性讀出；`value`／`name`
        是實例屬性的描述器，不是成員。**讀不到任何成員時直接判紅**：
        讀法失效卻回傳空 dict 的話，下面每一條比對都會「什麼都沒比就通過」。
    - Parameters:
        - name: str
            頂層 `shioaji` 的 Enum 名稱
    - Return:
        - Dict[str, Any]
            成員名 → 成員
    """

    cls: Any = getattr(shioaji, name, None)
    if cls is None:
        pytest.fail(f"shioaji {shioaji.__version__} 沒有 {name}")

    members: Dict[str, Any] = {
        attr: getattr(cls, attr)
        for attr in dir(cls)
        if not attr.startswith("_")
        and attr not in ("value", "name")
        and not inspect.isroutine(getattr(cls, attr))
    }
    if not members:
        pytest.fail(
            f"讀不到 shioaji {shioaji.__version__} 的 {name} 成員；"
            "Enum 的實作可能又換了，請先修正本檔的讀法"
        )
    return members


def test_order_state_matches_shioaji() -> None:
    """成員名稱與字串值都要與 Shioaji 那份逐一相同"""

    ours: Dict[str, str] = {member.name: member.value for member in OrderState}
    theirs: Dict[str, str] = {
        member_name: str(member.value)
        for member_name, member in _shioaji_members("OrderState").items()
    }

    assert ours == theirs


def test_order_state_compares_across_both_enums() -> None:
    """
    跨兩份 Enum 的 `==` 必須成立

    這是 `order_cb` 實際做的事：拿 Shioaji 傳進來的值比對專案自己的成員。
    任何一邊改成非 `str` 的物件，這個比較會安靜地變成永遠 False。
    """

    for member in OrderState:
        theirs: Any = getattr(shioaji.OrderState, member.name)
        assert isinstance(theirs, str), f"shioaji 的 OrderState.{member.name} 不是 str"
        assert theirs == member


# 本專案 Enum → 頂層 `shioaji` 的對應 Enum；成員名與值都應逐一相同
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

# 名稱不同的對應：shioaji 1.7 的委託狀態 Enum 叫 `OrderStatus`（舊版叫 `Status`）
_SHIOAJI_NAMES: Dict[str, str] = {"Status": "OrderStatus"}

# ratchet：我們有、但鎖定版的 shioaji 沒有的成員（(Enum, 成員) → 理由）。
# **升版後 shioaji 補上了就要把該條移除**，否則這份清單會慢慢變成沒人看的裝飾。
# `StockOrderCond.SBLShort` 已於 shioaji 1.7.2 補上並移除，目前清單為空
_MEMBERS_MISSING_IN_SHIOAJI: Dict[Tuple[str, str], str] = {}


@pytest.mark.parametrize("name", sorted(_MIRRORED_ENUMS))
def test_execution_enum_values_match_shioaji(name: str) -> None:
    """
    兩邊都有的成員，字串值必須逐一相同，且跨兩份的 `==` 成立

    值對不上不會拋錯：券商只會退單，或以另一種委託條件成交。
    """

    theirs: Dict[str, Any] = _shioaji_members(_SHIOAJI_NAMES.get(name, name))

    mismatched: Dict[str, Tuple[str, str]] = {
        member.name: (member.value, str(theirs[member.name].value))
        for member in _MIRRORED_ENUMS[name]
        if member.name in theirs and theirs[member.name] != member.value
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

    missing: Set[Tuple[str, str]] = set()
    for name, ours in _MIRRORED_ENUMS.items():
        theirs: Dict[str, Any] = _shioaji_members(_SHIOAJI_NAMES.get(name, name))
        for member in ours:
            if member.name not in theirs:
                missing.add((name, member.name))

    assert missing == set(_MEMBERS_MISSING_IN_SHIOAJI), (
        "本專案 Enum 與 shioaji 的成員落差和 ratchet 清單不符："
        f"新出現 {sorted(missing - set(_MEMBERS_MISSING_IN_SHIOAJI))}、"
        f"已消失 {sorted(set(_MEMBERS_MISSING_IN_SHIOAJI) - missing)}"
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

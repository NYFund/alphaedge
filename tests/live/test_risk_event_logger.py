import ast
import pathlib
from typing import Dict, List, Set, Tuple

import pytest

from core.live.notify.base import NotifyLevel

"""
風控事件的寫入口徑

`live_risk_event.severity` 是推播分級的**唯一**來源——`NotifyLevel` 的 docstring
寫明「等級直接取自 `live_risk_event.severity`，不要在推播端再寫一份判斷」。
但寫入端一直是裸字串，於是出現兩種拼法：`"WARN"` 與 `"WARNING"`。

**後果是推播靜靜送不出去**：`notify_safely()` 會 `NotifyLevel(level)`，
而 `"WARNING"` 不是合法值，例外被吞掉只留一行 log
（`推播失敗（忽略）：'WARNING' is not a valid NotifyLevel`）。
事件照樣寫進資料庫，但該通知的人不會收到——而這正是實盤最不能出錯的一環。
"""


ROOT: pathlib.Path = pathlib.Path(__file__).resolve().parent.parent.parent
LIVE: pathlib.Path = ROOT / "core" / "live"

VALID_SEVERITIES: Set[str] = {level.value for level in NotifyLevel}


def collect_severity_literals() -> List[Tuple[str, int, str]]:
    """
    - Description:
        掃出 `core/live/` 內所有寫進 `severity` 欄位的字面值

        以 AST 找 `{"severity": "..."}` 這種 dict 字面值，不用字串比對——
        註解與 docstring 裡提到 `severity` 是正常的。
    - Return:
        - List[Tuple[str, int, str]]
            `(檔案, 行號, 字面值)`
    """

    found: List[Tuple[str, int, str]] = []

    for path in sorted(LIVE.rglob("*.py")):
        tree: ast.Module = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values):
                if not isinstance(key, ast.Constant) or key.value != "severity":
                    continue
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    found.append(
                        (str(path.relative_to(ROOT)), value.lineno, value.value)
                    )

    return found


# === severity 必須是合法的推播等級 ===
def test_every_written_severity_is_a_valid_notify_level() -> None:
    """
    寫進資料庫的 severity 一定要是 `NotifyLevel` 解析得出來的值

    不合法的話事件照樣寫進去，但推播會在 `NotifyLevel(level)` 拋例外、
    被 `notify_safely()` 吞掉——**該收到通知的人不會收到，而且沒有人會發現**。
    """

    offenders: List[Tuple[str, int, str]] = [
        entry
        for entry in collect_severity_literals()
        if entry[2] not in VALID_SEVERITIES
    ]

    assert offenders == [], (
        "以下 severity 不是合法的 NotifyLevel，這些事件的推播會靜靜失敗："
        + "；".join(f"{path}:{line} = {value!r}" for path, line, value in offenders)
    )


def test_warning_is_not_a_notify_level() -> None:
    """
    釘住那個具體的拼法差異

    `NotifyLevel` 用的是 `WARN`，而寫入端一度寫成 `WARNING`。
    兩者只差兩個字母，肉眼掃 log 不會察覺。
    """

    assert "WARN" in VALID_SEVERITIES
    assert "WARNING" not in VALID_SEVERITIES

    with pytest.raises(ValueError):
        NotifyLevel("WARNING")


# === 寫入點要收斂 ===
def collect_insert_sites() -> Dict[str, int]:
    """統計每個檔案有幾處直接呼叫 `dao.insert_risk_event()`"""

    counts: Dict[str, int] = {}

    for path in sorted(LIVE.rglob("*.py")):
        tree: ast.Module = ast.parse(path.read_text())
        hits: int = sum(
            1
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "insert_risk_event"
        )
        if hits:
            counts[str(path.relative_to(ROOT))] = hits

    return counts


def test_risk_events_go_through_a_single_writer() -> None:
    """
    只有 `RiskEventLogger` 可以直接碰 `insert_risk_event()`

    每個類別各抄一份的代價是：`if self.dao is None: return` 的降級守衛、
    `run_id`／`occurred_at` 的組法、以及 severity 的拼法各有一份，
    而它們會各自漂移——`"WARN"` 與 `"WARNING"` 就是這樣長出來的。
    """

    allowed: str = "core/live/risk/event_log.py"
    offenders: Dict[str, int] = {
        path: count for path, count in collect_insert_sites().items() if path != allowed
    }

    assert offenders == {}, (
        f"這些檔案仍直接寫風控事件，應改走 RiskEventLogger：{offenders}"
    )

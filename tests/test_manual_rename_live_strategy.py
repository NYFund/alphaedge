import json
from typing import Any, Dict

from scripts.manual.manual_rename_live_strategy import rename_in_params

"""
策略改名腳本的 `strategy_params_json` 改寫：`strategies` 清單與 `live_tags` 的鍵一起換

`live_tags` 以策略類別名為鍵；只換清單的話，改名後的紀錄會留著舊名的代號，
而新名字查不到它。
"""


def test_strategies_and_live_tag_keys_are_renamed_together() -> None:
    raw: str = json.dumps(
        {
            "strategies": ["OldName", "Other"],
            "live_tags": {"OldName": "動能A", "Other": "B"},
        },
        ensure_ascii=False,
    )

    params: Dict[str, Any] = json.loads(rename_in_params(raw, "OldName", "NewName"))

    assert params == {
        "strategies": ["NewName", "Other"],
        "live_tags": {"NewName": "動能A", "Other": "B"},
    }


def test_records_without_live_tags_are_left_without_them() -> None:
    """較早的紀錄沒有 `live_tags`：只改清單，不憑空補一個空的欄位"""

    raw: str = json.dumps({"strategies": ["OldName"]})

    assert json.loads(rename_in_params(raw, "OldName", "NewName")) == {
        "strategies": ["NewName"]
    }

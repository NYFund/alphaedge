from pathlib import Path
from typing import List

_PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]

"""
`docker-compose.yml` 的 `live` service：幾條設定改掉了不會有任何錯誤，只會出事

不引入 YAML 解析套件，直接比對 `live` 區塊的文字——這幾條都是單行設定，
要守的是「它們還在」，不是完整的 compose 語意（那由 `docker compose config` 負責）。
"""


def live_block() -> str:
    """取出 `live:` 到下一個同層 service 之間的文字"""

    lines: List[str] = (
        (_PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8").splitlines()
    )
    start: int = lines.index("  live:")
    end: int = next(
        index
        for index in range(start + 1, len(lines))
        if lines[index].startswith("  ")
        and not lines[index].startswith("   ")
        and lines[index].strip().endswith(":")
    )
    return "\n".join(lines[start:end])


def test_live_service_is_opt_in() -> None:
    """掛在 profile 下：`docker compose up` 不可順手把實盤帶起來"""

    assert 'profiles: ["live"]' in live_block()


def test_live_mode_is_fixed_in_the_entrypoint() -> None:
    """`run` 的參數會整個取代 command；模式寫在 command 的話漏寫一次就變成跑回測"""

    assert 'entrypoint: ["python", "run.py", "--mode", "live"]' in live_block()


def test_live_service_has_a_grace_period_for_cancelling_orders() -> None:
    """預設 10 秒就 SIGKILL，撤單會被打斷在一半"""

    assert "stop_grace_period: 90s" in live_block()


def test_live_service_never_defaults_to_production() -> None:
    """正式環境的兩個旗標只能由人寫在排程指令裡，不可寫進 compose（註解除外）"""

    settings: List[str] = [
        line for line in live_block().splitlines() if not line.strip().startswith("#")
    ]
    assert not any("production" in line for line in settings)


def test_ca_certificate_is_mounted_read_only() -> None:
    assert ":/ca:ro" in live_block()

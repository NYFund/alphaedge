import re
from pathlib import Path
from typing import Dict, List

_PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]

"""
`docker-compose.yml` 的 `live` service：幾條設定改掉了不會有任何錯誤，只會出事

**不引入 YAML 解析套件**：PyYAML 不在本專案的相依裡，為了一支測試加一個相依
不划算；完整的 compose 語意由 `docker compose config` 負責，這裡守的是
「那幾條設定還在」。

但**也不比對原始文字**。原本六條都在比對字面值（`'profiles: ["live"]'`、
`'entrypoint: ["python", "run.py", "--mode", "live"]'`），而且以
`lines.index("  live:")` 定位——把 list 改成 block style、雙引號換單引號、
或縮排變一格，語意完全相同卻會六條一起紅，其中 `lines.index()` 還是直接
`ValueError`。那是脆弱而不是嚴格：誤報一樣會侵蝕對測試的信任。

現在的做法是**正規化後比語意**：去掉引號與多餘空白，比對「有沒有這個鍵、
值裡有沒有這個詞」。這樣重排版不會紅，真的刪掉設定才會紅。
"""


def service_block(name: str) -> List[str]:
    """
    取出某個 service 的內容行

    **以縮排層級定位，不比對 `"  live:"` 的字面值**——縮排改一格就 `ValueError`
    的寫法，本身就是這檔要修掉的那種脆弱。

    **區塊一定要收在下一個同層鍵之前**：讀到檔尾的寫法只在該 service 剛好排最後
    時才正確，而那是排列順序的巧合，不是保證。
    """

    lines: List[str] = (
        (_PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8").splitlines()
    )

    start: int = -1
    indent: int = 0
    for index, line in enumerate(lines):
        matched: re.Match = re.match(rf"^(\s+){re.escape(name)}:\s*$", line)
        if matched:
            start = index
            indent = len(matched.group(1))
            break

    assert start >= 0, f"`docker-compose.yml` 找不到 `{name}` service"

    end: int = len(lines)
    for index in range(start + 1, len(lines)):
        stripped: str = lines[index].strip()
        if not stripped or stripped.startswith("#"):
            continue
        current: int = len(lines[index]) - len(lines[index].lstrip())
        if current <= indent:
            end = index
            break

    return lines[start + 1 : end]


def live_block() -> List[str]:
    """取出 `live:` service 的內容行"""

    return service_block("live")


def normalized(line: str) -> str:
    """去掉引號與多餘空白，讓重排版不影響比對"""

    return re.sub(r"\s+", " ", line.replace('"', "").replace("'", "")).strip()


def settings() -> Dict[str, str]:
    """把 `live` 區塊的單行設定收成 `{鍵: 正規化後的值}`（跳過註解）"""

    found: Dict[str, str] = {}
    for line in live_block():
        stripped: str = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        found[key.strip()] = normalized(value)
    return found


def test_the_parser_actually_finds_the_block() -> None:
    """
    先確認解析抓得到東西

    抓不到的話 `settings()` 會是空 dict，而「某個鍵不在空 dict 裡」對所有
    負向斷言都成立——這一檔會整組變成假綠燈。
    """

    assert len(live_block()) >= 5, "`live` 區塊的內容行太少，定位方式可能已失效"
    assert "entrypoint" in settings()


def test_live_service_is_opt_in() -> None:
    """掛在 profile 下：`docker compose up` 不可順手把實盤帶起來"""

    assert "live" in settings().get("profiles", ""), (
        "`live` service 沒有掛在 `profiles` 下，`docker compose up` 會把它帶起來"
    )


def test_live_mode_is_fixed_not_templated() -> None:
    """
    模式寫死在 `entrypoint`，不可由變數決定

    `run` 的參數會整個取代 `command`，模式寫在 `command` 的話漏寫一次就變成跑回測。
    **斷言的是「寫死且指向實盤」而不是某一串字面值**：入口日後可能換成
    `python -m apps.live` 之類的形式，那時要守的仍然是同一件事——
    模式不可在執行期被換掉。比對字面值的話，換一次入口就得改一次測試。
    """

    entrypoint: str = settings().get("entrypoint", "")

    assert entrypoint, "`live` service 沒有 `entrypoint`"
    assert "${" not in entrypoint, (
        f"`entrypoint` 用了變數（{entrypoint}）——模式就可能在執行期被換掉"
    )
    assert "live" in entrypoint, f"`entrypoint` 沒有指向實盤：{entrypoint}"


def test_live_service_has_a_grace_period_for_cancelling_orders() -> None:
    """預設 10 秒就 SIGKILL，撤單會被打斷在一半"""

    grace: str = settings().get("stop_grace_period", "")
    seconds: re.Match = re.fullmatch(r"(\d+)s", grace)

    assert seconds is not None, f"`stop_grace_period` 不是秒數：{grace!r}"
    assert int(seconds.group(1)) >= 60, (
        f"`stop_grace_period` 只有 {grace}，撤單來不及做完"
    )


def test_live_service_never_defaults_to_production() -> None:
    """正式環境的兩個旗標只能由人寫在排程指令裡，不可寫進 compose（註解除外）"""

    live: List[str] = [
        line for line in live_block() if not line.strip().startswith("#")
    ]

    assert not any("production" in line for line in live), (
        "compose 裡出現 production 旗標——那兩個只能由人寫在排程指令裡"
    )


def test_ca_certificate_is_mounted_read_only() -> None:
    """CA 憑證掛在 `/ca` 且唯讀：容器內的程式不該有覆寫或刪掉憑證的可能"""

    mounts: str = "\n".join(normalized(line) for line in live_block())

    assert ":/ca:ro" in mounts, "CA 憑證沒有以唯讀掛載到 `/ca`"


def test_core_service_mounts_data_read_only() -> None:
    """
    `core` 也要守：資料目錄唯讀

    原本只守 `live`，而 `core` 的 `./data:/app/data:ro` 那個 `:ro` 被拿掉
    不會有任何東西紅——背景 ETL 可能正在寫同一個檔。
    """

    text: str = (_PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    core_mounts: List[str] = [
        normalized(line)
        for line in text.splitlines()
        if "/app/data" in line and not line.strip().startswith("#")
    ]

    assert core_mounts, "找不到任何 `/app/data` 掛載"
    assert any(line.endswith("/app/data:ro") for line in core_mounts), (
        f"沒有任何 `/app/data` 是唯讀掛載：{core_mounts}"
    )


def test_frontend_does_not_depend_on_the_batch_container() -> None:
    """
    `frontend` 不可宣告 `depends_on: core`

    `core` 是跑完就結束的一次性批次容器，`frontend` 是常駐 Web，只讀 volume 裡
    已落地的 CSV——兩者沒有執行期相依。宣告了的實際效果是
    `docker compose up frontend` 順手跑一整場回測，而那不會有任何錯誤訊息。
    """

    block: List[str] = [
        normalized(line)
        for line in service_block("frontend")
        if not line.strip().startswith("#")
    ]

    assert block, "`docker-compose.yml` 的 `frontend` 區塊是空的"
    assert not any(line.startswith("depends_on") for line in block), (
        "`frontend` 宣告了 `depends_on`——`docker compose up frontend` 會順手跑回測"
    )

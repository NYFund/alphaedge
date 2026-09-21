import sqlite3
from pathlib import Path
from typing import Callable, List, Set

import pytest

from core.dao.base import BaseDAO
from core.dao.tw.stock_price_dao import StockPriceDAO

"""
入口與日誌的四個坑，共通點是**平常不會有人發現**

1. 預設 `no_tick` 仍包含 `futures_tick`，沒有 Shioaji 金鑰的機器每晚紅燈；
   `all` 也不可帶到它（重跑會重複寫入）。
2. `delete_price_data` 沒有預覽也沒有確認，打錯日期就少一整天行情。
3. `.env` 缺 `DDB_PATH` 時路徑被拼成 `"NonetickDB"`。
4. `logs/api/` 每天長約 100 MB。
"""


# === target 捷徑的展開 ===
def test_no_tick_excludes_futures_tick() -> None:
    """
    預設的 `python -m tasks.update_db` 不可去跑期貨 tick

    那需要 Shioaji 金鑰與 `[tick]` 選用相依，沒有的機器每晚都以結束碼 1 收場，
    久了就沒人在看那個紅燈了。
    """

    from core.pipeline.utils import DataType
    from tasks.update_db import expand_targets

    expanded: Set[str] = expand_targets({"no_tick"})

    assert DataType.FUTURES_TICK.name.lower() not in expanded
    assert DataType.TICK.name.lower() not in expanded
    # 其餘 target 一個都不能少
    assert DataType.PRICE.name.lower() in expanded
    assert DataType.FUTURES_PRICE.name.lower() in expanded


def test_all_excludes_futures_tick_until_it_can_resume() -> None:
    """
    `--target all` 不含期貨 tick：它每跑一次就重複寫入一份

    沒有續跑依據、DolphinDB 表允許重複、loader 每次重放整個目錄，
    成交量會隨執行次數被放大。現貨 tick 仍在 `all` 內。
    """

    from core.pipeline.utils import DataType
    from tasks.update_db import expand_targets

    expanded: Set[str] = expand_targets({"all"})

    assert DataType.FUTURES_TICK.name.lower() not in expanded
    assert DataType.TICK.name.lower() in expanded


def test_explicit_futures_tick_is_still_honoured() -> None:
    """明確點名的 target 一定保留：暫停的是捷徑，不是這個功能"""

    from tasks.update_db import expand_targets

    assert "futures_tick" in expand_targets({"futures_tick"})


# === delete_price_data 預設不刪 ===
def make_price_db(tmp_path: Path, dao_factory: Callable[..., BaseDAO]) -> Path:
    """以正式 schema 建一個只有 price 表的暫存 DB"""

    db_path: Path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    dao_factory(
        StockPriceDAO,
        records=[
            {"date": date, "stock_id": stock_id}
            for date, stock_id in (
                ("2025-07-13", "2330"),
                ("2025-07-13", "2317"),
                ("2025-07-14", "2330"),
            )
        ],
        conn=conn,
    )
    conn.close()
    return db_path


def test_delete_price_data_defaults_to_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dao_factory: Callable[..., BaseDAO]
) -> None:
    """沒有 --apply 時一列都不能刪"""

    import tasks.delete_price_data as module

    db_path: Path = make_price_db(tmp_path, dao_factory)
    monkeypatch.setattr(module, "TW_STOCK_DB_PATH", str(db_path))

    module.delete_price_data_by_date("2025-07-13")

    conn = sqlite3.connect(db_path)
    assert conn.execute("select count(*) from price").fetchone()[0] == 3
    conn.close()


def test_delete_price_data_requires_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dao_factory: Callable[..., BaseDAO]
) -> None:
    """--apply 但沒有 --yes 且無法互動時不可刪除"""

    import tasks.delete_price_data as module

    db_path: Path = make_price_db(tmp_path, dao_factory)
    monkeypatch.setattr(module, "TW_STOCK_DB_PATH", str(db_path))
    monkeypatch.setattr(module.sys.stdin, "isatty", lambda: False)

    module.delete_price_data_by_date("2025-07-13", apply=True)

    conn = sqlite3.connect(db_path)
    assert conn.execute("select count(*) from price").fetchone()[0] == 3
    conn.close()


def test_delete_price_data_applies_with_yes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dao_factory: Callable[..., BaseDAO]
) -> None:
    """--apply --yes 才真的刪，且只刪指定那天"""

    import tasks.delete_price_data as module

    db_path: Path = make_price_db(tmp_path, dao_factory)
    monkeypatch.setattr(module, "TW_STOCK_DB_PATH", str(db_path))

    module.delete_price_data_by_date("2025-07-13", apply=True, assume_yes=True)

    conn = sqlite3.connect(db_path)
    remaining: List[tuple] = conn.execute("select date from price").fetchall()
    assert remaining == [("2025-07-14",)]
    conn.close()


def test_delete_price_data_parser_has_the_two_flags() -> None:
    """`--apply` 與 `--yes` 必須存在，否則使用說明會與行為不符"""

    import tasks.delete_price_data as module

    source: str = Path(module.__file__).read_text(encoding="utf-8")

    assert '"--apply"' in source
    assert '"--yes"' in source


# === DDB_PATH 缺值要當場拋出 ===
def test_require_tick_db_path_raises_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    缺 `DDB_PATH` 時不可拼出 `"NonetickDB"`

    那是一個看起來像路徑的字串，DolphinDB 會拿它去查一個永遠不存在的位置，
    錯誤訊息完全指不到真正的原因。
    """

    import core.config.schema as schema

    monkeypatch.setattr(schema, "TICK_DB_PATH", None)

    with pytest.raises(RuntimeError, match="DDB_PATH"):
        schema.require_tick_db_path()


def test_tick_db_path_is_none_rather_than_none_string() -> None:
    """設定值缺漏時是 None，不是字面上的 'None...' 字串"""

    import core.config.schema as schema

    assert schema.TICK_DB_PATH is None or not str(schema.TICK_DB_PATH).startswith(
        "None"
    )


# === api 桶的檔案 sink 只留 WARNING ===
def test_api_log_file_level_is_warning() -> None:
    """
    `core/api/` 每次查詢都寫一行，回測一跑就是數十萬次

    console 不受影響，開發時照樣看得到 INFO。
    """

    from core.config import API_LOG_FILE_LEVEL

    assert API_LOG_FILE_LEVEL == "WARNING"


def test_every_api_module_uses_the_shared_level() -> None:
    """12 支 API 都要用同一份常數，不可各自寫死"""

    api_dir: Path = Path("core/api/tw")
    modules: List[Path] = [
        path
        for path in sorted(api_dir.glob("*.py"))
        if "API_LOGS_DIR_PATH" in path.read_text(encoding="utf-8")
    ]

    assert modules, "找不到任何使用 API_LOGS_DIR_PATH 的模組"
    for path in modules:
        source: str = path.read_text(encoding="utf-8")
        assert "API_LOG_FILE_LEVEL" in source, f"{path.name} 沒有指定 api 桶日誌等級"


# === sink 要帶 filter，否則每個檔案都收下整個行程的每一行 ===
def make_record(name: str) -> dict:
    """最小的 loguru record 替身；filter 只看 `name`"""

    return {"name": name}


def test_api_bucket_only_accepts_api_records() -> None:
    """
    `logs/api/` 每天長約 100 MB，大部分不是 api 自己的日誌

    loguru 的 sink 預設收下整個行程的每一行；本專案有 33 個 `setup_logger()`
    呼叫端，少了 `filter=` 的話，一次查詢會同時寫進三個桶底下的每一個檔案。
    """

    from core.utils.log_manager import LogManager

    accept = LogManager.build_bucket_filter(Path("logs/api"))

    assert accept(make_record("core.api.tw.stock_price_api"))
    assert not accept(make_record("core.pipeline.shared.date_planner"))
    assert not accept(make_record("core.backtest.backtester"))


def test_backtest_bucket_only_accepts_backtest_records() -> None:
    """回測桶收回測相關套件，不收 ETL"""

    from core.utils.log_manager import LogManager

    accept = LogManager.build_bucket_filter(Path("logs/backtest"))

    assert accept(make_record("core.backtest.backtester"))
    assert accept(make_record("core.strategies.stock.momentum_strategy_1"))
    assert not accept(make_record("core.pipeline.tw.updaters.stock_price_updater"))


def test_pipeline_bucket_is_the_complement() -> None:
    """
    其餘桶收「沒有被其他桶認領」的記錄

    **刻意是排除法而不是白名單**：新增一個套件時它會自動落進 pipeline 桶，
    而不是整批消失——日誌設定的預設值該偏向多收，少收是查不出來的。
    """

    from core.utils.log_manager import LogManager

    accept = LogManager.build_bucket_filter(Path("logs/pipeline"))

    assert accept(make_record("core.pipeline.tw.updaters.stock_price_updater"))
    assert accept(make_record("tasks.update_db"))
    assert accept(make_record("some.brand.new.package")), "沒被認領的要落進 pipeline"
    assert not accept(make_record("core.api.tw.stock_price_api"))


# === S7：日誌檔被外部刪除後要重建（2026-09-03 事故）===
def test_log_file_is_recreated_after_external_deletion(tmp_path: Path) -> None:
    """
    檔案被刪掉之後，下一筆記錄要重新建立它

    loguru 的 file sink 只在達到 `rotation` 條件時才重開檔案。少了 `watch=True`，
    目錄被 `rm -rf` 之後 handler 會繼續寫進一個**已 unlink 的 inode**
    ——程序照跑、資料照寫、沒有任何錯誤，但日誌從此不可見。

    2026-09-03 19:12 實際發生：驗證「pytest 不再產生 logs/」時執行了
    `rm -rf logs`，而台期貨回補已跑了 1 小時 32 分；`lsof` 顯示該程序的 fd
    仍指向已不存在的 `update_futures_price.log`、已寫入 4.3 MB。
    """

    import time

    from loguru import logger

    from core.utils.log_manager import LogManager

    log_dir: Path = tmp_path / "pipeline"
    target: Path = log_dir / "watch_test.log"

    # `conftest.py` 把 `setup_logger` 換成 no-op（避免測試寫 `logs/`），
    # 但這條測試要驗的正是 sink 本身，故取用它保留下來的原函式
    LogManager._configured_logs.discard(str(target))
    handler_ids_before = set(logger._core.handlers)
    LogManager.real_setup_logger("watch_test.log", log_dir=log_dir, level="INFO")
    new_handlers = set(logger._core.handlers) - handler_ids_before

    try:
        logger.info("BEFORE_DELETE")
        logger.complete()
        time.sleep(0.2)
        assert target.exists()

        target.unlink()
        assert not target.exists()

        logger.info("AFTER_DELETE")
        logger.complete()
        time.sleep(0.3)

        assert target.exists(), "檔案被刪除後，下一筆記錄必須重建它"
        assert "AFTER_DELETE" in target.read_text(encoding="utf-8")
    finally:
        for handler_id in new_handlers:
            logger.remove(handler_id)
        LogManager._configured_logs.discard(str(target))


# === loguru 的 traceback 要真的印得出來 ===
PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]


def iter_project_sources() -> List[Path]:
    """列出 `core/` 與 `tasks/` 底下所有 .py（護欄的掃描範圍）"""

    paths: List[Path] = []
    for package in ("core", "tasks"):
        paths.extend(sorted((PROJECT_ROOT / package).rglob("*.py")))
    return paths


def test_no_stdlib_exc_info_kwarg() -> None:
    """
    `exc_info=` 是標準庫 `logging` 的參數，loguru 會默默丟掉

    loguru 的 `logger.error(message, *args, **kwargs)` 把多餘的 kwargs 當成
    `str.format()` 的參數，訊息裡沒有 `{exc_info}` 佔位符時就整個忽略——
    **traceback 從來沒有被印出來過**，而這 15 處全在 ETL 與 tick 的失敗路徑上，
    半夜跑掛時只留下一行訊息字串。正確寫法是 `logger.opt(exception=True).error(...)`。

    這條測試存在的理由是 `exc_info=True` 是標準庫肌肉記憶，ruff 沒有規則能抓它
    （它不是語法錯誤），沒有護欄就會再長回來。
    """

    import ast

    offenders: List[str] = []
    for path in iter_project_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg == "exc_info":
                    rel: str = str(path.relative_to(PROJECT_ROOT))
                    offenders.append(f"{rel}:{node.lineno}")

    assert not offenders, (
        "loguru 不吃 exc_info=，請改用 logger.opt(exception=True).error(...)："
        + "、".join(offenders)
    )


def test_loguru_opt_exception_emits_traceback(tmp_path: Path) -> None:
    """
    實證正確寫法會帶出堆疊、錯誤寫法不會

    只驗「有沒有護欄」不夠——要有一條測試釘住「為什麼要換寫法」，
    否則下次有人把護欄改掉時，沒有東西說明代價是什麼。
    """

    from loguru import logger

    sink: Path = tmp_path / "traceback_probe.log"
    handler_id: int = logger.add(sink, level="ERROR", backtrace=True, diagnose=False)

    try:
        try:
            raise ValueError("刻意拋錯")
        except ValueError as e:
            logger.error(f"A: 標準庫寫法 -> {e}", exc_info=True)
            logger.opt(exception=True).error(f"B: loguru 正確寫法 -> {e}")
        logger.complete()
    finally:
        logger.remove(handler_id)

    content: str = sink.read_text(encoding="utf-8")
    head, _, tail = content.partition("B: loguru 正確寫法")

    assert "A: 標準庫寫法" in head
    assert "Traceback" not in head, "exc_info= 竟然印出了堆疊，本護欄的前提要重新檢查"
    assert "Traceback" in tail, "logger.opt(exception=True) 必須附上完整堆疊"
    assert "刻意拋錯" in tail


# === delete_price_data 的結束碼 ===
def test_delete_price_data_reports_failure_on_bad_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dao_factory: Callable[..., BaseDAO]
) -> None:
    """
    日期解析失敗要回報失敗

    `--yes` 的用途是排程，而排程只看得到結束碼。舊版任何失敗都只記一行 log
    就 return，行程照樣以 0 結束，排程端完全看不出來。
    """

    import tasks.delete_price_data as module

    monkeypatch.setattr(
        module, "TW_STOCK_DB_PATH", str(make_price_db(tmp_path, dao_factory))
    )

    assert module.delete_price_data_by_date("2025-13-45") is False


def test_delete_price_data_reports_failure_when_db_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    資料庫不存在時回報失敗，且**不可建出空的 DB**

    `sqlite3.connect()` 會直接建一個 0 byte 的 `tw_stock.db`，
    之後所有查詢都回空表，看起來像「資料還沒更新」。
    """

    import tasks.delete_price_data as module

    missing: Path = tmp_path / "nope" / "tw_stock.db"
    monkeypatch.setattr(module, "TW_STOCK_DB_PATH", str(missing))

    assert module.delete_price_data_by_date("2025-07-13") is False
    assert not missing.exists()


def test_delete_price_data_reports_success_on_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dao_factory: Callable[..., BaseDAO]
) -> None:
    """預覽模式與「本來就沒有這天」都算成功，排程不該因此變紅"""

    import tasks.delete_price_data as module

    monkeypatch.setattr(
        module, "TW_STOCK_DB_PATH", str(make_price_db(tmp_path, dao_factory))
    )

    assert module.delete_price_data_by_date("2025-07-13") is True
    assert module.delete_price_data_by_date("2025-07-15") is True

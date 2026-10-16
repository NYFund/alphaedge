import inspect
from typing import List, Set

import apps.update_db as update_db
from apps.update_db import TARGETS, TargetSpec, expand_targets
from core.pipeline.shared.base_updater import BaseDataUpdater
from core.pipeline.utils import DataType, FinMindDataType

"""
`update_db` 改為表格驅動、updater 的生命週期一致

原本 `main()` 有 23 個幾乎一樣的區塊、收尾方法有三種名字、`LogManager.setup_logger` 在
13 處各寫一次；`BaseDataUpdater` 沒有定義生命週期，入口只能逐支手寫 `try/finally`。
"""


def test_registry_covers_every_target_exactly_once() -> None:
    """每個 `DataType` 與 FinMind 子集都在註冊表裡，名稱不重複，與 `--target` 的選項一致"""

    names: List[str] = [spec.name for spec in TARGETS]

    assert len(names) == len(set(names))
    expected: Set[str] = {dt.name.lower() for dt in DataType} | {
        dt.value.lower() for dt in FinMindDataType
    }
    assert set(names) == expected


def test_shortcuts_follow_the_declared_flags() -> None:
    """`all` 不含只能點名的 target；`no_tick` 再扣掉需要 tick 相依的；點名的一律保留"""

    all_targets: Set[str] = expand_targets({"all"})
    no_tick: Set[str] = expand_targets({"no_tick"})

    assert all_targets == {"all"} | {
        spec.name for spec in TARGETS if not spec.explicit_only
    }
    assert no_tick == {"no_tick"} | {
        spec.name for spec in TARGETS if not spec.explicit_only and not spec.needs_tick
    }
    assert "tick" in all_targets and "tick" not in no_tick
    for name in ("futures_stock_price", "finmind", "stock_info", "broker_trading"):
        assert name not in all_targets
        assert expand_targets({name}) == {name}


def test_main_has_a_single_loop_not_a_block_per_target() -> None:
    """`main()` 不再逐 target 手寫：原始碼裡沒有任何 `if DataType.X ... in targets` 區塊"""

    source: str = inspect.getsource(update_db.main) + inspect.getsource(
        update_db.run_targets
    )

    assert "if DataType." not in source
    assert "for spec in TARGETS" in source


def test_every_target_builds_a_fresh_updater_lazily() -> None:
    """`factory` 每次呼叫才建 updater：測試以 monkeypatch 換掉模組層的類別時才換得到"""

    for spec in TARGETS:
        assert isinstance(spec, TargetSpec)
        assert callable(spec.factory) and callable(spec.run)
        source: str = inspect.getsource(spec.factory)
        assert "lambda" in source, spec.name


# === updater 生命週期 ===
class _Updater(BaseDataUpdater):
    LOG_FILE_NAME: str = "test_updater.log"

    def __init__(self) -> None:
        super().__init__()
        self.closed: int = 0

    def update(self, *args, **kwargs) -> None:
        pass

    def close(self) -> None:
        self.closed += 1


def test_base_provides_setup_close_and_context_manager(monkeypatch) -> None:
    """基底開日誌、預設 `close()`、with 區塊結束即 `close()`"""

    opened: List[str] = []
    monkeypatch.setattr(
        "core.pipeline.shared.base_updater.LogManager.setup_logger",
        lambda name, *a, **k: opened.append(name),
    )

    with _Updater() as updater:
        updater.setup()

    assert opened == ["test_updater.log"]
    assert updater.closed == 1

    class _Quiet(BaseDataUpdater):
        def update(self, *args, **kwargs) -> None:
            pass

    quiet: _Quiet = _Quiet()
    quiet.setup()
    quiet.close()
    assert opened == ["test_updater.log"]  # 沒宣告檔名就不開


def test_no_updater_sets_up_its_logger_by_hand() -> None:
    """日誌檔名一律以 `LOG_FILE_NAME` 宣告，`setup()` 不再各自呼叫 `LogManager.setup_logger`"""

    import importlib
    import pkgutil

    import core.pipeline.tw.updaters as package

    offenders: List[str] = []
    for module_info in pkgutil.iter_modules(package.__path__):
        module = importlib.import_module(f"{package.__name__}.{module_info.name}")
        source: str = inspect.getsource(module)
        if "LogManager.setup_logger(" in source:
            offenders.append(module_info.name)

    assert offenders == []


def test_every_concrete_updater_closes_with_close() -> None:
    """收尾只有一個名字：沒有 `cleanup()`／`logout()` 這類別名"""

    import importlib
    import pkgutil

    import core.pipeline.tw.updaters as package

    for module_info in pkgutil.iter_modules(package.__path__):
        module = importlib.import_module(f"{package.__name__}.{module_info.name}")
        for _, cls in inspect.getmembers(module, inspect.isclass):
            if issubclass(cls, BaseDataUpdater) and cls.__module__ == module.__name__:
                assert not hasattr(cls, "cleanup"), cls.__name__
                assert not hasattr(cls, "logout"), cls.__name__
                assert callable(cls.close)


def test_main_runs_each_target_with_its_range_and_closes(monkeypatch) -> None:
    """迴圈對每個點名的 target：取（或不取）區間 → 跑 → `close()`；tick 也會被收尾"""

    import argparse
    import datetime

    calls: List[tuple] = []

    def make_fake(name: str) -> type:
        class Fake:
            def __init__(self) -> None:
                calls.append((name, "init"))

            def update(self, **kwargs) -> None:
                calls.append((name, "update", tuple(sorted(kwargs))))

            def close(self) -> None:
                calls.append((name, "close"))

        return Fake

    monkeypatch.setattr(update_db, "StockPriceUpdater", make_fake("price"))
    monkeypatch.setattr(update_db, "StockTickUpdater", make_fake("tick"))
    monkeypatch.setattr(update_db, "FuturesChipUpdater", make_fake("futures_chip"))
    monkeypatch.setattr(update_db, "cleanup_api_logs", lambda: None)
    monkeypatch.setattr(
        update_db,
        "parse_arguments",
        lambda: argparse.Namespace(
            target=["price", "tick", "futures_chip"],
            from_date=datetime.date(2024, 1, 1),
            retry_failed=0,
            retry_wait=0,
        ),
    )

    update_db.main()

    assert calls == [
        ("price", "init"),
        ("price", "update", ("end_date", "start_date")),
        ("price", "close"),
        ("tick", "init"),
        ("tick", "update", ("end_date", "start_date")),
        ("tick", "close"),
        ("futures_chip", "init"),
        ("futures_chip", "update", ()),
        ("futures_chip", "close"),
    ]

import re
from pathlib import Path
from typing import Dict, List, Tuple

import pytest

from core.config.schema import ChipColumn, PriceColumn

_PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]

"""防護測試：策略層不得出現資料庫欄位字面值

現況的失效模式是**靜默**的：欄位名一旦更名，部分策略會走進既有的 `continue`
分支而安靜地不開倉，回測報表上只表現為「訊號變少」，極難察覺。本測試讓這件事
在 CI 就變紅，而不是等到換資料源才爆。
"""


STRATEGY_DIR: Path = _PROJECT_ROOT / "core" / "strategies"

# 例外清單刻意留空。若日後真有必要保留，須在此明列檔案並註明理由
ALLOWED_FILES: List[str] = []


def db_column_names() -> List[str]:
    """所有資料庫欄位名（策略層一律不得直接引用）"""

    return [column.value for column in PriceColumn] + [
        column.value for column in ChipColumn
    ]


def strategy_source_files() -> List[Path]:
    """`core/strategies/` 下的所有策略原始碼"""

    return sorted(
        path
        for path in STRATEGY_DIR.rglob("*.py")
        if "__pycache__" not in path.parts and path.name not in ALLOWED_FILES
    )


def test_strategy_dir_has_source_files() -> None:
    """掃描範圍不得為空，否則本測試會永遠通過而失去意義"""

    assert len(strategy_source_files()) >= 6


@pytest.mark.parametrize("column", db_column_names())
def test_no_db_column_literal_in_strategies(column: str) -> None:
    """策略層出現任一資料庫欄位字面值即失敗"""

    offenders: List[str] = []

    for path in strategy_source_files():
        source: str = path.read_text(encoding="utf-8")
        for line_no, line in enumerate(source.splitlines(), start=1):
            # 只擋字面值取值（帶引號），中文註解與 docstring 說明不在此限
            if f'"{column}"' in line or f"'{column}'" in line:
                offenders.append(f"{path.relative_to(_PROJECT_ROOT)}:{line_no}")

    assert not offenders, (
        f"策略層不得直接引用資料庫欄位 {column!r}，請改用 core/api/ 的具名查詢方法："
        f"{offenders}"
    )


# === 策略不得自建資料連線===
# 樣式與說明成對，測試失敗時直接把「該怎麼做」印出來，不必再去翻 README
FORBIDDEN_DATA_ACCESS: List[Tuple[str, str, str]] = [
    (
        "sqlite3-connect",
        r"sqlite3\s*\.\s*connect\s*\(",
        "策略不得自行連資料庫；資料一律由 DataFeed 經 quotes 傳入",
    ),
    (
        "api-instantiation",
        r"\b[A-Z]\w*API\s*\(",
        "策略不得自行建立 core/api/ 的物件（一次回測會多開好幾條連線）；"
        "需要額外資料請在 DataFeed 取好後放進 quotes",
    ),
    (
        "dolphindb",
        r"\b(?:dolphindb|ddb)\b",
        "策略不得直接連 tick 資料庫；tick 由 DataFeed 以 Scale.TICK 供應",
    ),
]


@pytest.mark.parametrize(
    ("pattern_id", "pattern", "reason"),
    FORBIDDEN_DATA_ACCESS,
    ids=[item[0] for item in FORBIDDEN_DATA_ACCESS],
)
def test_no_self_built_data_access_in_strategies(
    pattern_id: str, pattern: str, reason: str
) -> None:
    """
    - Description:
        策略層不得自建資料連線或 API 物件

        「策略不得自行建立 API／連線」原本**只寫在 `core/strategies/README.md`**，
        沒有任何測試釘住它。目前 grep 無違規，所以這條是**預防性**護欄——
        下一支策略寫 `StockPriceAPI()` 時不會有任何東西變紅，而症狀是
        一次回測多開好幾條互不相干的連線，不會報錯、只會慢慢累積。

        **只擋實例化，不擋型別標註**：`price: StockPriceAPI` 是合法的
        （策略可以持有 DataFeed 給的物件），`StockPriceAPI()` 才是自建。
        兩者的差別就是那個左括號。
    - Parameters:
        - pattern_id: str
            樣式代號（供 pytest 的 test id 顯示）
        - pattern: str
            要擋的正規表達式
        - reason: str
            違規時要告訴作者的替代做法
    """

    offenders: List[str] = []
    compiled: re.Pattern = re.compile(pattern)

    for path in strategy_source_files():
        for line_no, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            # 註解與 docstring 裡提到這些名字是說明，不是呼叫
            code: str = line.split("#", 1)[0]
            if compiled.search(code):
                offenders.append(f"{path.relative_to(_PROJECT_ROOT)}:{line_no}")

    assert not offenders, f"{reason}。違規處：{offenders}"


def test_forbidden_pattern_actually_matches() -> None:
    """
    護欄本身要抓得到東西

    三個樣式若因為寫錯而永遠不命中，上面那條會永遠通過而毫無作用——
    這正是本專案已經踩過的坑（回歸腳本把 skip 當通過的假綠燈、`test_broker_trading_updater.py`
    整段包在 try/except）。這裡以**合成的違規程式碼**驗證樣式真的有效。
    """

    samples: Dict[str, str] = {
        "sqlite3-connect": "conn = sqlite3.connect(TW_STOCK_DB_PATH)",
        "api-instantiation": "self.price = StockPriceAPI()",
        "dolphindb": "import dolphindb as ddb",
    }

    for pattern_id, pattern, _ in FORBIDDEN_DATA_ACCESS:
        assert re.search(pattern, samples[pattern_id]), pattern_id

    # 型別標註不得被誤判為自建
    assert not re.search(FORBIDDEN_DATA_ACCESS[1][1], "price: StockPriceAPI")
    assert not re.search(
        FORBIDDEN_DATA_ACCESS[1][1], "def f(api: StockChipAPI) -> None:"
    )


# === 策略層的三道防線===
def test_momentum_strategy_rejects_tick_scale() -> None:
    """
    TICK 級別要當場擋下，不是等到第一根 bar 才崩

    本策略的訊號建立在「前一交易日收盤」上，TICK 路徑只會掛 `self.tick`、
    `self.price` 維持 None，第一根 bar 就會撞 `ValueError("Invalid API type")`。
    """

    import pytest

    from core.strategies.stock.momentum_strategy_1 import MomentumStrategy1
    from core.utils import Scale

    strategy: MomentumStrategy1 = MomentumStrategy1()
    strategy.scale = Scale.TICK

    class _Feed:
        chip = mrr = fs = price = tick = None

    with pytest.raises(NotImplementedError, match="只支援日線"):
        strategy.setup_apis(_Feed())


def test_max_holdings_defaults_to_unlimited() -> None:
    """
    `BaseStrategy.max_holdings` 預設 None ＝ 不限制

    舊版預設 0，而 `Backtester.check_max_holdings()` 只把 None 當成不限制
    ——忘記設定的新策略，每一張開倉單都被引擎剔除，回測跑完是零筆交易、
    零錯誤訊息。
    """

    from core.strategies.base import BaseStrategy

    class _Bare(BaseStrategy):
        def setup_account(self, account) -> None: ...

        def setup_apis(self, feed) -> None: ...

        def check_open_signal(self, quotes) -> None: ...

        def check_close_signal(self, quotes) -> None: ...

        def check_stop_loss_signal(self, positions) -> None: ...

        def calculate_position_size(self, *args, **kwargs) -> None: ...

    assert _Bare().max_holdings is None


def test_strategy_loader_isolates_a_broken_module(monkeypatch) -> None:
    """
    單一模組壞掉不該讓所有策略都列不出來

    舊版一路 `import_module()` 到底，任何一支策略有 import 錯誤，
    `run.py --strategy` 連「有哪些策略可用」都印不出來。
    """

    import importlib

    from core.strategies.strategy_loader import StrategyLoader

    original = importlib.import_module

    def explode_on_one(name: str, *args, **kwargs):
        if name.endswith("momentum_strategy_1"):
            raise ImportError("boom")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", explode_on_one)

    strategies = StrategyLoader.load_strategies()

    assert "MomentumStrategy1" not in strategies
    assert "ForeignSellShortDayTradeStrategy" in strategies, "其餘策略仍要載得到"


def test_strategy_loader_rejects_duplicate_class_names() -> None:
    """
    同名類別要當場拋出，不可靜默覆蓋

    key 是類別名，覆蓋之後「跑的到底是哪一支」要看掃描順序，比模組壞掉更難查。
    """

    import types

    import pytest

    from core.strategies.base import BaseStrategy
    from core.strategies.strategy_loader import StrategyLoader

    def make_module(module_name: str):
        module = types.ModuleType(module_name)

        class Duplicated(BaseStrategy):
            def setup_account(self, account) -> None: ...

            def setup_apis(self, feed) -> None: ...

            def check_open_signal(self, quotes) -> None: ...

            def check_close_signal(self, quotes) -> None: ...

            def check_stop_loss_signal(self, positions) -> None: ...

            def calculate_position_size(self, *args, **kwargs) -> None: ...

        Duplicated.__module__ = module_name
        module.Duplicated = Duplicated
        return module

    collected = {}
    StrategyLoader.collect_from_module(make_module("pkg.first"), collected)

    with pytest.raises(ValueError, match="策略類別名稱重複"):
        StrategyLoader.collect_from_module(make_module("pkg.second"), collected)


def test_momentum_skips_stocks_without_a_valid_previous_close() -> None:
    """
    昨收為 `NaN` 的股票不可變成買進候選

    無成交日的收盤價在資料庫是 `NULL`（無成交價改存 NULL 之後），讀進來是 `NaN`，
    而 `price_chg < 門檻` 對 `NaN` 恆為 `False`——**不會 continue，反而一路
    走成候選**，log 裡只留一行「漲幅 nan%」。

    修 `price` 表那 104,046 列時實測到：少了這道防線，LONG 回歸多出 10 筆、
    少掉 3 筆交易（同一天的名額被 NaN 標的擠掉）。
    """

    import datetime

    import pandas as pd

    from core.models import StockQuote
    from core.strategies.stock.momentum_strategy_1 import MomentumStrategy1
    from core.utils import Scale

    date: datetime.date = datetime.date(2024, 6, 6)
    strategy: MomentumStrategy1 = MomentumStrategy1()
    strategy.max_holdings = 10
    strategy.trading_days = [datetime.date(2024, 6, 5), date]

    # 昨收為 NaN，但今日有價、量也夠——舊寫法會讓它通過所有檢查
    quote: StockQuote = StockQuote(
        stock_id="1102",
        scale=Scale.DAY,
        date=date,
        cur_price=50.0,
        volume=999_999,
        open=50.0,
        high=50.0,
        low=50.0,
        close=50.0,
    )

    strategy.get_signal_close_map = lambda quotes, day: {"1102": float("nan")}
    strategy.get_previous_trading_date = lambda day: datetime.date(2024, 6, 5)

    class _Account:
        balance: float = 1_000_000.0

        def get_position_count(self) -> int:
            return 0

    strategy.account = _Account()

    assert pd.isna(float("nan"))
    assert strategy.check_open_signal([quote]) == [], "昨收為 NaN 的股票不可產生開倉單"


# === sys.path 注入不得再出現===
def test_no_sys_path_injection_anywhere() -> None:
    """
    `sys.path.insert` 全專案應為 0 處

    專案已 `pip install -e .`（CI 亦然），注入全部多餘——更麻煩的是它會
    **遮蔽「沒安裝就跑」的 import 錯誤**：測試在沒裝套件的環境照樣綠，
    直到有人在別的目錄執行才發現。

    `strategy_lab` 不在 `pyproject` 的 `packages.find` 裡，所以那幾支研究腳本
    改成一律用 `python -m strategy_lab.…` 執行（直接跑檔案路徑會
    ModuleNotFoundError，那是**刻意的**——它比靠路徑硬塞而安靜地成功要好）。

    判定沿用 `scripts/check_layer_deps.py` 的 AST 掃描，不另寫一份；
    它只認真的呼叫節點，不會把說明這件事的 docstring 算成一處。
    """

    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "check_layer_deps", _PROJECT_ROOT / "scripts" / "check_layer_deps.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    hits: List[str] = module.check_sys_path(module.collect_files())

    assert hits == [], f"sys.path 注入應為 0 處，實際：{hits}"

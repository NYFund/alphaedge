import argparse
import datetime
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterator, List, Optional, Set, Tuple, Union

from loguru import logger

from apps.clean_logs import clean_logs
from core.config import (
    DAY_TRADE_LIST_START_DATE,
    DEFAULT_CHIP_START_DATE,
    DEFAULT_CORPORATE_ACTION_START_DATE,
    DEFAULT_DIVIDEND_START_DATE,
    DEFAULT_END_MONTH,
    DEFAULT_FUTURES_START_DATE,
    DEFAULT_MARGIN_START_DATE,
    DEFAULT_PRICE_START_DATE,
    DEFAULT_START_YEAR,
    FINMIND_BROKER_TRADING_START_DATE,
    SHORT_SALE_LIST_START_DATE,
    STOCK_FUTURES_TOP_N,
    TICK_UPDATE_START_DATE,
)
from core.pipeline.tw.updaters.corporate_action_updater import (
    CorporateActionUpdater,
)
from core.pipeline.tw.updaters.financial_statement_updater import (
    FinancialStatementUpdater,
)
from core.pipeline.tw.updaters.finmind_updater import FinMindUpdater
from core.pipeline.tw.updaters.futures_chip_updater import FuturesChipUpdater
from core.pipeline.tw.updaters.futures_continuous_updater import (
    FuturesContinuousUpdater,
)
from core.pipeline.tw.updaters.futures_margin_updater import FuturesMarginUpdater
from core.pipeline.tw.updaters.futures_price_updater import FuturesPriceUpdater
from core.pipeline.tw.updaters.futures_stock_universe_updater import (
    FuturesStockUniverseUpdater,
)
from core.pipeline.tw.updaters.market_holiday_updater import MarketHolidayUpdater
from core.pipeline.tw.updaters.monthly_revenue_report_updater import (
    MonthlyRevenueReportUpdater,
)
from core.pipeline.tw.updaters.stock_chip_updater import StockChipUpdater
from core.pipeline.tw.updaters.stock_day_trade_list_updater import (
    StockDayTradeListUpdater,
)
from core.pipeline.tw.updaters.stock_dividend_updater import StockDividendUpdater
from core.pipeline.tw.updaters.stock_margin_updater import StockMarginUpdater
from core.pipeline.tw.updaters.stock_price_updater import StockPriceUpdater
from core.pipeline.tw.updaters.stock_short_sale_list_updater import (
    StockShortSaleListUpdater,
)
from core.pipeline.tw.updaters.stock_tick_updater import StockTickUpdater
from core.pipeline.utils import DataLoadError, DataType, FinMindDataType

# api 桶日誌的保留天數（`update_db` 收尾會自動清理）
API_LOG_RETENTION_DAYS: int = 7

"""
資料更新任務主程式 (update_db)

本模組為資料更新系統的入口，依 --target 參數選擇要更新的資料類型，
可單一更新或一次指定多個目標。預設為 no_tick（更新所有資料但不含 tick）。

================================================================================
參考：財報申報期限（一般行業）
================================================================================
  Q1    5月15日
  Q2    8月14日
  Q3    11月14日
  年報  3月31日

================================================================================
參考：Shioaji 台股 ticks 資料時間
================================================================================
  可取得區間  2020/03/02 ~ 今日（DB 內的實際區間依維護狀況而定）

================================================================================
Target 對照表
================================================================================

  選項                        說明
  -------------------------  -----------------------------------------------
  tick                        逐筆成交 (Shioaji ticks)
  chip                        三大法人籌碼
  margin                      信用交易（融資融券餘額）
  short_sale_list             平盤下得融（借）券賣出名單（2013-09-23 起；回測擋單用）
  day_trade_list              現股當沖標的名單（2014-01-06 起；回測擋單用）
  dividend                    除權除息計算結果表（含還原係數、現金股利）
  corporate_action            非除權息的公司行動（減資、面額變更；含調整倍率）
  price                       收盤價
  futures_price               台期貨每日行情（寫入 tw_futures.db）
  futures_stock_universe      股票期貨標的池（寫入 tw_futures.db）
  futures_margin              台期貨保證金（變動序列，寫入 tw_futures.db）
  market_holiday              市場開休市日期（TWSE 公告；去年／今年／明年，實盤交易日判定用）
  futures_continuous          台期貨連續合約（由 futures_price_daily 建出，不連網路）
  futures_chip                台期貨籌碼（三大法人、大額交易人、選擇權 PCR）
  futures_stock_price         股票期貨行情（商品清單取自標的池，預設只爬流動性前 N 檔；
                              **不含在 all／no_tick 內**，只在此處點名才會跑）
  fs                          財報 (Financial Statement)
  mrr                         月營收報表 (Monthly Revenue Report)
  finmind                     全部 FinMind（台股總覽 + 證券商 + 券商分點；
                              **不含在 all／no_tick 內**：帳號等級拿不到券商分點，只在點名時跑）
  stock_info                  FinMind 台股總覽（不含權證）
  stock_info_with_warrant     FinMind 台股總覽（含權證）
  broker_info                 FinMind 證券商資訊
  broker_trading              FinMind 券商分點統計
  all                         全部資料（含 tick；不含 futures_stock_price 與 finmind）
  no_tick                     全部資料（不含 tick、futures_stock_price 與 finmind，預設）

================================================================================
使用範例
================================================================================

  python -m apps.update_db                                  # 等同 --target no_tick
  python -m apps.update_db --target price
  python -m apps.update_db --target chip price tick         # 多個 target
  python -m apps.update_db --target futures_stock_price     # 只能這樣點名才會跑
  python -m apps.update_db --target all --from 2024-01-01   # 覆寫起日
"""


@dataclass(frozen=True)
class TargetSpec:
    """
    `update_db` 的一個 target：怎麼建 updater、要不要取回補區間、怎麼跑、能不能被捷徑選到

    **各 target 的 `update()` 簽名刻意不統一**（請求的單位本來就不同：日期、年季、年月、
    或根本沒有區間），差異由 `run` 吸收；`main()` 只剩一個迴圈。
    """

    name: str
    # 建 updater；**每次呼叫才建**，測試以 monkeypatch 換掉模組層的類別時才換得到
    factory: Callable[[], Any]
    # 跑這個 target：收 updater 與（有的話）回補區間設定
    run: Callable[[Any, Optional[Dict[str, Any]]], None]
    # 要不要取回補區間（`get_update_time_config()` 的 key）；None 表示這個 target 沒有區間
    time_config: Optional[Union[DataType, str]] = None
    # 只在 `--target` 明確點名時才跑；`all` 與 `no_tick` 兩個捷徑一律排除
    explicit_only: bool = False
    # 需要 Shioaji 金鑰、`[tick]` 選用相依與執行中的 TimescaleDB；`no_tick` 排除
    needs_tick: bool = False


def run_date_range(method_name: str) -> Callable[[Any, Optional[Dict[str, Any]]], None]:
    """以 `start_date`／`end_date` 呼叫 updater 的某個方法"""

    def run(updater: Any, time_config: Optional[Dict[str, Any]]) -> None:
        getattr(updater, method_name)(
            start_date=time_config["start_date"], end_date=time_config["end_date"]
        )

    return run


def run_without_range(updater: Any, time_config: Optional[Dict[str, Any]]) -> None:
    """沒有回補區間的 target：直接 `update()`"""

    updater.update()


def run_stock_futures(updater: Any, time_config: Optional[Dict[str, Any]]) -> None:
    """股期：商品清單取自標的池，預設只爬流動性前 N 檔"""

    updater.update_stock_futures(
        start_date=time_config["start_date"],
        end_date=time_config["end_date"],
        top_n=STOCK_FUTURES_TOP_N,
    )


def run_year_season(updater: Any, time_config: Optional[Dict[str, Any]]) -> None:
    """以年季為單位的 target（財報）"""

    updater.update(
        start_year=time_config["start_year"],
        end_year=time_config["end_year"],
        start_season=time_config["start_season"],
        end_season=time_config["end_season"],
    )


def run_year_month(updater: Any, time_config: Optional[Dict[str, Any]]) -> None:
    """以年月為單位的 target（月營收）"""

    updater.update(
        start_year=time_config["start_year"],
        end_year=time_config["end_year"],
        start_month=time_config["start_month"],
        end_month=time_config["end_month"],
    )


def run_finmind_subset(
    data_type: FinMindDataType,
) -> Callable[[Any, Optional[Dict[str, Any]]], None]:
    """FinMind 的單一資料集"""

    def run(updater: Any, time_config: Optional[Dict[str, Any]]) -> None:
        updater.update(data_type=data_type)

    return run


# **順序就是執行順序**，與 `--target` 的點名順序無關：
#
# - PRICE 必須排在 CHIP／MARGIN 之前：那兩個以 `price` 表的交易日為日曆，price 還沒更新到
#   今天的話，它們今天就不會被請求——結果是永遠落後一天。`DatePlanner.extend_calendar_tail()`
#   是第二層保險，但正確的執行順序仍應在這裡表達。
# - TICK 必須排在 PRICE 之後：tick 入庫以 `price` 表判斷「這天這檔是不是上市櫃」，
#   `price` 表整天沒資料的日子會被當成日 K 還沒更新而計為失敗。
# - FUTURES_STOCK_PRICE 只在點名時跑：股期有 320 檔，表內還沒有股期行情時排不出流動性會
#   無聲退回整份標的池，320 檔是 100 天以上的回補，不能被預設值一腳踩進去。
# - FINMIND 與它的四個子集只在點名時跑：帳號等級拿不到券商分點。
# - 籌碼、標的池、保證金、休市日沒有回補區間：籌碼各自從表內最新日續跑（盤後公布，盤中跑只會
#   拿到無資料，那是正常狀態）；標的池是當下快照；保證金是現行一覽表（沒有調整時不會新增列）；
#   休市日固定抓去年／今年／明年（明年的公告 12 月前多半還沒出來）。
# - 連續合約是衍生表，來源是同一個 DB 的 futures_price_daily，不連網路，一律整段重建。
TARGETS: Tuple[TargetSpec, ...] = (
    TargetSpec(
        "price", lambda: StockPriceUpdater(), run_date_range("update"), DataType.PRICE
    ),
    TargetSpec(
        "tick",
        lambda: StockTickUpdater(),
        run_date_range("update"),
        DataType.TICK,
        needs_tick=True,
    ),
    TargetSpec(
        "chip", lambda: StockChipUpdater(), run_date_range("update"), DataType.CHIP
    ),
    TargetSpec(
        "margin",
        lambda: StockMarginUpdater(),
        run_date_range("update"),
        DataType.MARGIN,
    ),
    TargetSpec(
        "short_sale_list",
        lambda: StockShortSaleListUpdater(),
        run_date_range("update"),
        DataType.SHORT_SALE_LIST,
    ),
    TargetSpec(
        "day_trade_list",
        lambda: StockDayTradeListUpdater(),
        run_date_range("update"),
        DataType.DAY_TRADE_LIST,
    ),
    TargetSpec(
        "dividend",
        lambda: StockDividendUpdater(),
        run_date_range("update"),
        DataType.DIVIDEND,
    ),
    TargetSpec(
        "corporate_action",
        lambda: CorporateActionUpdater(),
        run_date_range("update"),
        DataType.CORPORATE_ACTION,
    ),
    TargetSpec(
        "futures_price",
        lambda: FuturesPriceUpdater(),
        run_date_range("update"),
        DataType.FUTURES_PRICE,
    ),
    TargetSpec(
        "futures_stock_price",
        lambda: FuturesPriceUpdater(),
        run_stock_futures,
        DataType.FUTURES_PRICE,
        explicit_only=True,
    ),
    TargetSpec("futures_chip", lambda: FuturesChipUpdater(), run_without_range),
    TargetSpec(
        "futures_continuous", lambda: FuturesContinuousUpdater(), run_without_range
    ),
    TargetSpec(
        "futures_stock_universe",
        lambda: FuturesStockUniverseUpdater(),
        run_without_range,
    ),
    TargetSpec("futures_margin", lambda: FuturesMarginUpdater(), run_without_range),
    TargetSpec("market_holiday", lambda: MarketHolidayUpdater(), run_without_range),
    TargetSpec("fs", lambda: FinancialStatementUpdater(), run_year_season, DataType.FS),
    TargetSpec(
        "mrr", lambda: MonthlyRevenueReportUpdater(), run_year_month, DataType.MRR
    ),
    TargetSpec(
        "finmind",
        lambda: FinMindUpdater(),
        run_date_range("update_all"),
        DataType.FINMIND,
        explicit_only=True,
    ),
    TargetSpec(
        "stock_info",
        lambda: FinMindUpdater(),
        run_finmind_subset(FinMindDataType.STOCK_INFO),
        explicit_only=True,
    ),
    TargetSpec(
        "stock_info_with_warrant",
        lambda: FinMindUpdater(),
        run_finmind_subset(FinMindDataType.STOCK_INFO_WITH_WARRANT),
        explicit_only=True,
    ),
    TargetSpec(
        "broker_info",
        lambda: FinMindUpdater(),
        run_finmind_subset(FinMindDataType.BROKER_INFO),
        explicit_only=True,
    ),
    TargetSpec(
        "broker_trading",
        lambda: FinMindUpdater(),
        run_date_range("update_broker_trading_daily_report"),
        FinMindDataType.BROKER_TRADING.value.lower(),
        explicit_only=True,
    ),
)


def expand_targets(targets: Set[str]) -> Set[str]:
    """
    - Description:
        把 `all`／`no_tick` 兩個集合捷徑展開成實際的 target

        `explicit_only` 的 target 兩個捷徑都不含，只有明確點名才會跑；
        `no_tick` 另外排除所有需要 Shioaji 金鑰與 tick 選用相依的 target。
    - Parameters:
        - targets: Set[str]
            命令列給的 target（可含捷徑）
    - Return:
        - Set[str]
            展開後的 target（原本點名的一律保留）
    """

    expanded: Set[str] = set(targets)

    # all = 所有 target（包含 tick），但不含只能點名的那幾個
    if "all" in expanded:
        expanded.update(spec.name for spec in TARGETS if not spec.explicit_only)

    # no_tick = all − tick（預設值）：tick 需要 Shioaji 金鑰、`[tick]` 選用相依
    # 與執行中的 TimescaleDB，沒有的機器每晚都以結束碼 1 收場，久了就沒人在看那個紅燈了
    if "no_tick" in expanded:
        expanded.update(
            spec.name
            for spec in TARGETS
            if not spec.explicit_only and not spec.needs_tick
        )

    return expanded


def parse_arguments() -> argparse.Namespace:
    """解析命令列參數"""

    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="Update stock-related databases"
    )

    parser.add_argument(
        "--target",
        nargs="+",
        choices=[dt.name.lower() for dt in DataType]
        + [
            "all",
            "no_tick",
        ]
        + [dt.value.lower() for dt in FinMindDataType],
        default=["no_tick"],
        help="Targets to update (default: no_tick)",
    )
    parser.add_argument(
        "--from",
        dest="from_date",
        type=datetime.date.fromisoformat,
        default=None,
        metavar="YYYY-MM-DD",
        help=(
            "覆寫所有以日期為單位的 target 的起日。"
            "平常不需要——updater 會自動補齊表內的缺口；"
            "要把起點往前拉到比預設更早時才用得到"
        ),
    )
    return parser.parse_args()


def get_update_time_config(
    data_type: Union[DataType, str, None] = None,
    from_date: Optional[datetime.date] = None,
) -> Dict[str, datetime.date | int]:
    """
    - Description:
        依資料類型回傳對應的時間區間設定

        結束日（end_date／end_year／end_month／end_season）一律取當日，
        確保每次執行都同步到今天。
    - Parameters:
        - data_type: Union[DataType, str, None]
            資料類型；None 時回傳通用設定
        - from_date: Optional[datetime.date]
            `--from` 的覆寫起日，**只影響以日期為單位的 target**；
            年／季／月為單位的 target（fs、mrr）不受影響
    - Return:
        - Dict[str, datetime.date | int]
            時間區間設定
    """

    config: Dict[str, datetime.date | int] = _build_time_config(data_type)
    if from_date is not None and "start_date" in config:
        config["start_date"] = from_date
    return config


def _build_time_config(
    data_type: Union[DataType, str, None] = None,
) -> Dict[str, datetime.date | int]:
    """各 target 的預設時間區間（`get_update_time_config()` 的內部實作）"""

    if data_type == DataType.TICK:
        return {
            "start_date": TICK_UPDATE_START_DATE,
            "end_date": datetime.date.today(),
        }
    elif data_type == DataType.CHIP:
        return {
            "start_date": DEFAULT_CHIP_START_DATE,
            "end_date": datetime.date.today(),
        }
    elif data_type == DataType.MARGIN:
        return {
            "start_date": DEFAULT_MARGIN_START_DATE,
            "end_date": datetime.date.today(),
        }
    elif data_type == DataType.SHORT_SALE_LIST:
        return {
            "start_date": SHORT_SALE_LIST_START_DATE,
            "end_date": datetime.date.today(),
        }
    elif data_type == DataType.DAY_TRADE_LIST:
        return {
            "start_date": DAY_TRADE_LIST_START_DATE,
            "end_date": datetime.date.today(),
        }
    elif data_type == DataType.DIVIDEND:
        return {
            "start_date": DEFAULT_DIVIDEND_START_DATE,
            "end_date": datetime.date.today(),
        }
    elif data_type == DataType.CORPORATE_ACTION:
        return {
            "start_date": DEFAULT_CORPORATE_ACTION_START_DATE,
            "end_date": datetime.date.today(),
        }
    elif data_type == DataType.PRICE:
        return {
            "start_date": DEFAULT_PRICE_START_DATE,
            "end_date": datetime.date.today(),
        }
    elif data_type == DataType.FUTURES_PRICE:
        return {
            "start_date": DEFAULT_FUTURES_START_DATE,
            "end_date": datetime.date.today(),
        }
    elif data_type == DataType.FS:
        return {
            "start_year": DEFAULT_START_YEAR,
            "end_year": datetime.date.today().year,
            "start_season": 1,
            "end_season": 4,
        }
    elif data_type == DataType.MRR:
        return {
            "start_year": DEFAULT_START_YEAR,
            "end_year": datetime.date.today().year,
            "start_month": 1,
            "end_month": DEFAULT_END_MONTH,
        }
    elif data_type == DataType.FINMIND:
        return {
            "start_date": FINMIND_BROKER_TRADING_START_DATE,
            "end_date": datetime.date.today(),
        }
    elif data_type == FinMindDataType.BROKER_TRADING or (
        isinstance(data_type, str)
        and data_type.lower() == FinMindDataType.BROKER_TRADING.value.lower()
    ):
        return {
            "start_date": FINMIND_BROKER_TRADING_START_DATE,
            "end_date": datetime.date.today(),
        }
    else:
        return {
            "start_date": DEFAULT_PRICE_START_DATE,
            "end_date": datetime.date.today(),
            "start_year": DEFAULT_START_YEAR,
            "end_year": datetime.date.today().year,
            "start_month": 1,
            "end_month": DEFAULT_END_MONTH,
            "start_season": 1,
            "end_season": 4,
        }


@contextmanager
def target_guard(name: str, failed_targets: List[str]) -> Iterator[None]:
    """
    - Description:
        隔離單一 target 的失敗：記錄下來但不中斷其餘 target

        一次 `--target no_tick` 會跑十來個 updater、耗時數小時。若其中一個因為
        少數檔案入庫失敗就中止整批，當天其餘資料全部不會更新——那是拿可用性換
        可見度。此處讓每個 target 各自成敗，最後由 `main()` 統一以非零狀態結束。
    - Parameters:
        - name: str
            target 名稱，用於訊息與失敗清單
        - failed_targets: List[str]
            失敗的 target 會被附加進這個清單
    """

    try:
        yield
    except DataLoadError as exc:
        logger.error(f"[{name}] 更新失敗：{exc}；失敗檔案：{exc.failed_files[:10]}")
        failed_targets.append(name)
    except Exception as exc:
        logger.error(f"[{name}] 更新失敗：{type(exc).__name__}: {exc}")
        failed_targets.append(name)


def cleanup_api_logs() -> None:
    """
    - Description:
        收尾清掉過舊的 api 桶日誌

        `logs/api/` 每天長約 100 MB——每次查詢都寫一行，
        而回測一跑就是數十萬次查詢。保留 7 天足夠追查昨晚的問題，
        再久就只是佔硬碟。清理失敗不影響更新結果，故只記 warning。
    """

    try:
        clean_logs(days=API_LOG_RETENTION_DAYS, buckets=["api"], apply=True)
    except Exception as error:
        logger.warning(f"清理 api 日誌失敗（不影響更新結果）：{error}")


def main() -> None:
    """依 `--target` 逐一更新；任一 target 失敗仍跑完其餘，最後以非零結束碼收尾"""

    args: argparse.Namespace = parse_arguments()
    targets: Set[str] = set(args.target)
    from_date: Optional[datetime.date] = args.from_date
    failed_targets: List[str] = []

    if from_date is not None:
        logger.info(f"--from {from_date}：以日期為單位的 target 一律由此日起算")

    targets = expand_targets(targets)

    for spec in TARGETS:
        if spec.name not in targets:
            continue
        with target_guard(spec.name, failed_targets):
            time_config: Optional[Dict[str, Any]] = (
                get_update_time_config(spec.time_config, from_date=from_date)
                if spec.time_config is not None
                else None
            )
            updater: Any = spec.factory()
            try:
                spec.run(updater, time_config)
            finally:
                # 每一支 updater 都以 `close()` 收尾（`BaseDataUpdater` 提供預設）；
                # 測試可能以沒有 `close()` 的替身換掉類別，故容忍沒有
                close: Optional[Callable[[], None]] = getattr(updater, "close", None)
                if close is not None:
                    close()

    # **一定要在結束碼判斷之前**：失敗的那幾天正是 `logs/api/` 長最多的時候，
    # 放在 `sys.exit(1)` 之後等於永遠跑不到
    cleanup_api_logs()

    if failed_targets:
        logger.error(
            f"❌ Database Update Failed. 失敗的 target："
            f"{', '.join(sorted(failed_targets))}"
            f"（成功：{', '.join(sorted(targets - set(failed_targets))) or '無'}）"
        )
        sys.exit(1)

    logger.info(f"✅ Database Update Completed. Updated: {', '.join(sorted(targets))}")


if __name__ == "__main__":
    main()

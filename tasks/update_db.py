import argparse
import datetime
import sys
from contextlib import contextmanager
from typing import Dict, Iterator, List, Optional, Set, Union

from loguru import logger

from core.config import (
    DEFAULT_CHIP_START_DATE,
    DEFAULT_CORPORATE_ACTION_START_DATE,
    DEFAULT_DIVIDEND_START_DATE,
    DEFAULT_END_MONTH,
    DEFAULT_FUTURES_START_DATE,
    DEFAULT_MARGIN_START_DATE,
    DEFAULT_PRICE_START_DATE,
    DEFAULT_START_YEAR,
    FINMIND_BROKER_TRADING_START_DATE,
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
from core.pipeline.tw.updaters.futures_tick_updater import FuturesTickUpdater
from core.pipeline.tw.updaters.monthly_revenue_report_updater import (
    MonthlyRevenueReportUpdater,
)
from core.pipeline.tw.updaters.stock_chip_updater import StockChipUpdater
from core.pipeline.tw.updaters.stock_dividend_updater import StockDividendUpdater
from core.pipeline.tw.updaters.stock_margin_updater import StockMarginUpdater
from core.pipeline.tw.updaters.stock_price_updater import StockPriceUpdater
from core.pipeline.tw.updaters.stock_tick_updater import StockTickUpdater
from core.pipeline.utils import DataLoadError, DataType, FinMindDataType
from tasks.clean_logs import clean_logs

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
  可取得區間  2020/03/02 ~ 今日
  目前 DB    2020/04/01 ~ 2024/05/10（依實際維護為準）

================================================================================
參數說明
================================================================================

  --target  <target> [<target> ...]
      欲更新的資料類型，可多選。未指定時預設為 no_tick。
      選項見下方「Target 對照表」。

================================================================================
Target 對照表
================================================================================

  選項                        說明
  -------------------------  -----------------------------------------------
  tick                        逐筆成交 (Shioaji ticks)
  chip                        三大法人籌碼
  margin                      信用交易（融資融券餘額）
  dividend                    除權除息計算結果表（含還原係數、現金股利）
  corporate_action            非除權息的公司行動（減資、面額變更；含調整倍率）
  price                       收盤價
  futures_price               台期貨每日行情（寫入 tw_futures.db）
  futures_stock_universe      股票期貨標的池（寫入 tw_futures.db）
  futures_margin              台期貨保證金（變動序列，寫入 tw_futures.db）
  futures_continuous          台期貨連續合約（由 futures_price_daily 建出，不連網路）
  futures_chip                台期貨籌碼（三大法人、大額交易人、選擇權 PCR）
  futures_stock_price         股票期貨行情（商品清單取自標的池，預設只爬流動性前 N 檔）
  futures_tick                台期貨逐筆成交（Shioaji → DolphinDB；需 [tick] 相依與金鑰）
  fs                          財報 (Financial Statement)
  mrr                         月營收報表 (Monthly Revenue Report)
  finmind                     全部 FinMind（台股總覽 + 證券商 + 券商分點）
  stock_info                  FinMind 台股總覽（不含權證）
  stock_info_with_warrant     FinMind 台股總覽（含權證）
  broker_info                 FinMind 證券商資訊
  broker_trading              FinMind 券商分點統計
  all                         全部資料（含 tick 與 futures_tick）
  no_tick                     全部資料（不含 tick 與 futures_tick，預設）

================================================================================
各資料更新指令（單一 target）
================================================================================

  # 逐筆成交
  python -m tasks.update_db --target tick

  # 三大法人籌碼
  python -m tasks.update_db --target chip

  # 信用交易（融資融券餘額）
  python -m tasks.update_db --target margin

  # 除權除息計算結果表（上市走證交所、上櫃走櫃買中心，皆為全歷史）
  python -m tasks.update_db --target dividend
  python -m tasks.update_db --target corporate_action

  # 收盤價
  python -m tasks.update_db --target price

  # 更新台期貨每日行情（寫入 tw_futures.db，商品見 FUTURES_TARGET_PRODUCTS）
  python -m tasks.update_db --target futures_price

  # 由各月份契約重建連續合約（三種調整方式；不連網路，整段重建）
  python -m tasks.update_db --target futures_continuous

  # 更新台期貨籌碼（三個資料集，一天三次請求即涵蓋全市場）
  python -m tasks.update_db --target futures_chip

  # 更新股票期貨行情（預設流動性前 20 檔；320 檔全爬要好幾個月）
  python -m tasks.update_db --target futures_stock_price

  # 更新股票期貨標的池（寫入 tw_futures.db；每次執行留下一份當日快照）
  python -m tasks.update_db --target futures_stock_universe

  # 更新台期貨保證金（寫入 tw_futures.db；保證金沒調整時不會新增列）
  python -m tasks.update_db --target futures_margin

  # 財報
  python -m tasks.update_db --target fs

  # 月營收報表
  python -m tasks.update_db --target mrr

  # 全部 FinMind（台股總覽 + 證券商 + 券商分點）
  python -m tasks.update_db --target finmind

  # FinMind 台股總覽（不含權證）
  python -m tasks.update_db --target stock_info

  # FinMind 台股總覽（含權證）
  python -m tasks.update_db --target stock_info_with_warrant

  # FinMind 證券商資訊
  python -m tasks.update_db --target broker_info

  # FinMind 券商分點統計
  python -m tasks.update_db --target broker_trading

  # 全部資料（含 tick）
  python -m tasks.update_db --target all

  # 全部資料（不含 tick 與 futures_tick，等同預設）
  python -m tasks.update_db --target no_tick
  或
  python -m tasks.update_db

================================================================================
組合更新範例（多個 target）
================================================================================

  python -m tasks.update_db --target chip price
  python -m tasks.update_db --target chip price tick
  python -m tasks.update_db --target stock_info broker_trading
"""


# 需要 Shioaji 金鑰與 `[tick]` 選用相依的 target；`no_tick` 一律排除這些
TICK_DATA_TYPES: Set[DataType] = {DataType.TICK, DataType.FUTURES_TICK}


def parse_arguments() -> argparse.Namespace:
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
    根據不同的資料類型返回對應的時間區間設定

    Args:
        data_type: 資料類型，如果為 None 則返回通用設定

    Returns:
        包含時間區間設定的字典

    Note:
        預設結束日（end_date/end_year/end_month/end_season）皆更新到最新日（當日），
        以確保資料持續同步至今日。

        `from_date` 為 `--from` 的覆寫值，只影響以日期為單位的 target；
        年／季／月為單位的 target（fs、mrr）不受影響。
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
    args: argparse.Namespace = parse_arguments()
    targets: Set[str] = set(args.target)
    from_date: Optional[datetime.date] = args.from_date
    failed_targets: List[str] = []

    if from_date is not None:
        logger.info(f"--from {from_date}：以日期為單位的 target 一律由此日起算")

    # all = 所有資料類型（包含 tick 和 finmind）
    if "all" in targets:
        targets.update(dt.name.lower() for dt in DataType)

    # no_tick = 所有資料類型 − **所有** tick（包含 finmind）
    #
    # **`futures_tick` 也要排除**：舊版只排除 `DataType.TICK`，
    # 於是預設的 `python -m tasks.update_db` 會去跑期貨 tick——那需要 Shioaji
    # 金鑰與 `[tick]` 選用相依，沒有的機器每晚都以結束碼 1 收場，
    # 久了就沒人在看那個紅燈了。
    if "no_tick" in targets:
        targets.update(dt.name.lower() for dt in DataType if dt not in TICK_DATA_TYPES)

    if DataType.TICK.name.lower() in targets:
        with target_guard("tick", failed_targets):
            time_config: Dict[str, datetime.date | int] = get_update_time_config(
                data_type=DataType.TICK,
                from_date=from_date,
            )
            stock_tick_updater: StockTickUpdater = StockTickUpdater()
            stock_tick_updater.update(
                start_date=time_config["start_date"], end_date=time_config["end_date"]
            )

    # **PRICE 必須排在 CHIP／MARGIN 之前**：那兩個以 `price` 表的交易日為
    # 日曆，price 還沒更新到今天的話，它們今天就不會被請求——結果是永遠
    # 落後一天。`DatePlanner.extend_calendar_tail()` 是第二層保險，
    # 但正確的執行順序仍應在這裡表達
    if DataType.PRICE.name.lower() in targets:
        with target_guard("price", failed_targets):
            time_config: Dict[str, datetime.date | int] = get_update_time_config(
                data_type=DataType.PRICE,
                from_date=from_date,
            )
            stock_price_updater: StockPriceUpdater = StockPriceUpdater()
            try:
                stock_price_updater.update(
                    start_date=time_config["start_date"],
                    end_date=time_config["end_date"],
                )
            finally:
                stock_price_updater.close()

    if DataType.CHIP.name.lower() in targets:
        with target_guard("chip", failed_targets):
            time_config: Dict[str, datetime.date | int] = get_update_time_config(
                data_type=DataType.CHIP,
                from_date=from_date,
            )
            stock_chip_updater: StockChipUpdater = StockChipUpdater()
            try:
                stock_chip_updater.update(
                    start_date=time_config["start_date"],
                    end_date=time_config["end_date"],
                )
            finally:
                stock_chip_updater.close()

    if DataType.MARGIN.name.lower() in targets:
        with target_guard("margin", failed_targets):
            time_config: Dict[str, datetime.date | int] = get_update_time_config(
                data_type=DataType.MARGIN,
                from_date=from_date,
            )
            stock_margin_updater: StockMarginUpdater = StockMarginUpdater()
            try:
                stock_margin_updater.update(
                    start_date=time_config["start_date"],
                    end_date=time_config["end_date"],
                )
            finally:
                stock_margin_updater.close()

    if DataType.DIVIDEND.name.lower() in targets:
        with target_guard("dividend", failed_targets):
            time_config: Dict[str, datetime.date | int] = get_update_time_config(
                data_type=DataType.DIVIDEND,
                from_date=from_date,
            )
            stock_dividend_updater: StockDividendUpdater = StockDividendUpdater()
            try:
                stock_dividend_updater.update(
                    start_date=time_config["start_date"],
                    end_date=time_config["end_date"],
                )
            finally:
                stock_dividend_updater.close()

    if DataType.CORPORATE_ACTION.name.lower() in targets:
        with target_guard("corporate_action", failed_targets):
            time_config: Dict[str, datetime.date | int] = get_update_time_config(
                data_type=DataType.CORPORATE_ACTION,
                from_date=from_date,
            )
            corporate_action_updater: CorporateActionUpdater = CorporateActionUpdater()
            try:
                corporate_action_updater.update(
                    start_date=time_config["start_date"],
                    end_date=time_config["end_date"],
                )
            finally:
                corporate_action_updater.close()

    if DataType.FUTURES_PRICE.name.lower() in targets:
        with target_guard("futures_price", failed_targets):
            time_config: Dict[str, datetime.date | int] = get_update_time_config(
                data_type=DataType.FUTURES_PRICE,
                from_date=from_date,
            )
            futures_price_updater: FuturesPriceUpdater = FuturesPriceUpdater()
            try:
                futures_price_updater.update(
                    start_date=time_config["start_date"],
                    end_date=time_config["end_date"],
                )
            finally:
                futures_price_updater.close()

    if DataType.FUTURES_STOCK_PRICE.name.lower() in targets:
        with target_guard("futures_stock_price", failed_targets):
            # **股期不走 FUTURES_TARGET_PRODUCTS**：320 檔且會隨掛牌／下市異動，
            # 清單改由 futures_stock_universe 提供。預設只爬流動性前 N 檔——
            # 全爬是每天 640 次請求，而尾端商品一天只成交個位數口
            time_config: Dict[str, datetime.date | int] = get_update_time_config(
                data_type=DataType.FUTURES_PRICE,
                from_date=from_date,
            )
            stock_futures_updater: FuturesPriceUpdater = FuturesPriceUpdater()
            try:
                stock_futures_updater.update_stock_futures(
                    start_date=time_config["start_date"],
                    end_date=time_config["end_date"],
                    top_n=STOCK_FUTURES_TOP_N,
                )
            finally:
                stock_futures_updater.close()

    if DataType.FUTURES_TICK.name.lower() in targets:
        with target_guard("futures_tick", failed_targets):
            # **要爬哪些契約由日線行情表決定**，不是自己推近月＋次月；
            # 預設只爬近月（期貨的量集中在近月，遠月同樣佔配額卻沒幾筆）
            time_config: Dict[str, datetime.date | int] = get_update_time_config(
                data_type=DataType.FUTURES_PRICE,
                from_date=from_date,
            )
            futures_tick_updater: FuturesTickUpdater = FuturesTickUpdater()
            try:
                futures_tick_updater.update(
                    start_date=time_config["start_date"],
                    end_date=time_config["end_date"],
                )
            finally:
                futures_tick_updater.logout()

    if DataType.FUTURES_CHIP.name.lower() in targets:
        with target_guard("futures_chip", failed_targets):
            # 三個資料集各自從自己表內的最新日續跑（見 FuturesChipUpdater）；
            # **籌碼是盤後公布**，當日盤中跑只會拿到「無資料」，那是正常狀態
            futures_chip_updater: FuturesChipUpdater = FuturesChipUpdater()
            try:
                futures_chip_updater.update()
            finally:
                futures_chip_updater.close()

    if DataType.FUTURES_CONTINUOUS.name.lower() in targets:
        with target_guard("futures_continuous", failed_targets):
            # 連續合約是**衍生表**：來源是同一個 DB 的 futures_price_daily，
            # 不連網路。逆向調整的調整量會隨「之後又換了幾次月」而改變，
            # 故一律整段重建而非增量（見 FuturesContinuousUpdater.update()）
            futures_continuous_updater: FuturesContinuousUpdater = (
                FuturesContinuousUpdater()
            )
            try:
                futures_continuous_updater.update()
            finally:
                futures_continuous_updater.close()

    if DataType.FUTURES_STOCK_UNIVERSE.name.lower() in targets:
        with target_guard("futures_stock_universe", failed_targets):
            # 標的池是「當下快照」，沒有回補區間，故不取 time_config
            futures_stock_universe_updater: FuturesStockUniverseUpdater = (
                FuturesStockUniverseUpdater()
            )
            try:
                futures_stock_universe_updater.update()
            finally:
                futures_stock_universe_updater.close()

    if DataType.FUTURES_MARGIN.name.lower() in targets:
        with target_guard("futures_margin", failed_targets):
            # 保證金是「現行一覽表」，一次請求就結束，沒有回補區間，故不取 time_config；
            # 沒有調整時不會新增列（主鍵相同被 INSERT OR IGNORE 擋掉），那是正常狀態
            futures_margin_updater: FuturesMarginUpdater = FuturesMarginUpdater()
            try:
                futures_margin_updater.update()
            finally:
                futures_margin_updater.close()

    if DataType.FS.name.lower() in targets:
        with target_guard("fs", failed_targets):
            time_config: Dict[str, datetime.date | int] = get_update_time_config(
                data_type=DataType.FS,
                from_date=from_date,
            )
            fs_updater: FinancialStatementUpdater = FinancialStatementUpdater()
            try:
                fs_updater.update(
                    start_year=time_config["start_year"],
                    end_year=time_config["end_year"],
                    start_season=time_config["start_season"],
                    end_season=time_config["end_season"],
                )
            finally:
                fs_updater.close()

    if DataType.MRR.name.lower() in targets:
        with target_guard("mrr", failed_targets):
            time_config: Dict[str, datetime.date | int] = get_update_time_config(
                data_type=DataType.MRR,
                from_date=from_date,
            )
            mrr_updater: MonthlyRevenueReportUpdater = MonthlyRevenueReportUpdater()
            try:
                mrr_updater.update(
                    start_year=time_config["start_year"],
                    end_year=time_config["end_year"],
                    start_month=time_config["start_month"],
                    end_month=time_config["end_month"],
                )
            finally:
                mrr_updater.close()

    # FinMind 資料更新
    if DataType.FINMIND.name.lower() in targets:
        with target_guard("finmind", failed_targets):
            time_config: Dict[str, datetime.date | int] = get_update_time_config(
                data_type=DataType.FINMIND,
                from_date=from_date,
            )
            finmind_updater: FinMindUpdater = FinMindUpdater()
            try:
                finmind_updater.update_all(
                    start_date=time_config["start_date"],
                    end_date=time_config["end_date"],
                )
            finally:
                finmind_updater.close()

    # FinMind 子類型更新
    if FinMindDataType.STOCK_INFO.value.lower() in targets:
        with target_guard("stock_info", failed_targets):
            finmind_updater: FinMindUpdater = FinMindUpdater()
            try:
                finmind_updater.update(data_type=FinMindDataType.STOCK_INFO)
            finally:
                finmind_updater.close()

    if FinMindDataType.STOCK_INFO_WITH_WARRANT.value.lower() in targets:
        with target_guard("stock_info_with_warrant", failed_targets):
            finmind_updater: FinMindUpdater = FinMindUpdater()
            try:
                finmind_updater.update(
                    data_type=FinMindDataType.STOCK_INFO_WITH_WARRANT
                )
            finally:
                finmind_updater.close()

    if FinMindDataType.BROKER_INFO.value.lower() in targets:
        with target_guard("broker_info", failed_targets):
            finmind_updater: FinMindUpdater = FinMindUpdater()
            try:
                finmind_updater.update(data_type=FinMindDataType.BROKER_INFO)
            finally:
                finmind_updater.close()

    if FinMindDataType.BROKER_TRADING.value.lower() in targets:
        with target_guard("broker_trading", failed_targets):
            time_config: Dict[str, datetime.date | int] = get_update_time_config(
                FinMindDataType.BROKER_TRADING.value.lower(),
                from_date=from_date,
            )
            finmind_updater: FinMindUpdater = FinMindUpdater()
            try:
                finmind_updater.update_broker_trading_daily_report(
                    start_date=time_config["start_date"],
                    end_date=time_config["end_date"],
                )
            finally:
                finmind_updater.close()

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

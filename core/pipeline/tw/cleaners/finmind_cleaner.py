from pathlib import Path
from typing import List, Optional

import pandas as pd
from loguru import logger

from core.config import FINMIND_DOWNLOADS_PATH
from core.pipeline.shared.base_cleaner import BaseDataCleaner
from core.pipeline.utils import FinMindDataType
from core.utils import FileEncoding


class FinMindCleaner(BaseDataCleaner):
    """FinMind Cleaner (Transform): validate data, write CSV, return DataFrame"""

    def __init__(self) -> None:
        super().__init__()
        # Downloads directory Path
        self.finmind_dir: Path = FINMIND_DOWNLOADS_PATH
        self.setup()

    def setup(self, *args, **kwargs) -> None:
        """Set Up the Config of Cleaner"""
        # Generate downloads directory
        self.finmind_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def keep_latest_per_stock(df: pd.DataFrame) -> pd.DataFrame:
        """
        - Description:
            同一個 `stock_id` 只留日期最新的一列

            FinMind 的台股總覽**每個身分各給一列**：興櫃轉上市櫃的公司同時有
            「emerging（轉板前一天）」與「twse／tpex（今天）」兩列，而且舊的那列
            排在前面。直接 `keep="first"` 會留下過期的興櫃身分，以 `type` 取上市櫃
            清單的地方（財報權益變動表）就把它排除。同一天多列（產業別不同）時
            保留原本排在前面的那列，與舊行為一致。沒有 `date` 欄時退回原順序。
        - Parameters:
            - df: pd.DataFrame
                原始總覽資料
        - Return:
            - pd.DataFrame
                每檔一列
        """

        if "date" in df.columns:
            df = df.sort_values("date", ascending=False, kind="stable")
        return df.drop_duplicates(subset=["stock_id"], keep="first").sort_index()

    def clean_stock_info(self, df: pd.DataFrame) -> Optional[pd.DataFrame]:
        """
        清洗台股總覽資料 (TaiwanStockInfo)

        參數:
            df: pd.DataFrame - 從 crawler 取得的原始資料

        回傳值:
            pd.DataFrame 或 None（如果資料為空或驗證失敗）
        """
        if df is None or df.empty:
            logger.warning("Stock info data is empty")
            return None

        # 基本驗證：檢查必要欄位
        required_columns: List[str] = ["stock_id", "stock_name"]
        missing_columns: List[str] = [
            col for col in required_columns if col not in df.columns
        ]
        if missing_columns:
            logger.error(
                f"Missing required columns in stock info data: {missing_columns}"
            )
            return None

        # 移除重複資料：同一檔保留**日期最新**的那一列
        df = self.keep_latest_per_stock(df)

        # 存入 CSV 檔案到各自的資料夾
        data_type_dir: Path = (
            self.finmind_dir / FinMindDataType.STOCK_INFO.value.lower()
        )
        data_type_dir.mkdir(parents=True, exist_ok=True)
        csv_path: Path = data_type_dir / "taiwan_stock_info.csv"
        df.to_csv(csv_path, index=False, encoding=FileEncoding.UTF8_SIG.value)
        logger.info(f"Saved stock info data to {csv_path} ({len(df)} rows)")

        return df

    def clean_stock_info_with_warrant(self, df: pd.DataFrame) -> Optional[pd.DataFrame]:
        """
        清洗台股總覽(含權證)資料 (TaiwanStockInfoWithWarrant)

        參數:
            df: pd.DataFrame - 從 crawler 取得的原始資料

        回傳值:
            pd.DataFrame 或 None（如果資料為空或驗證失敗）
        """
        if df is None or df.empty:
            logger.warning("Stock info with warrant data is empty")
            return None

        # 基本驗證：檢查必要欄位
        required_columns: List[str] = ["stock_id", "stock_name"]
        missing_columns: List[str] = [
            col for col in required_columns if col not in df.columns
        ]
        if missing_columns:
            logger.error(
                f"Missing required columns in stock info data: {missing_columns}"
            )
            return None

        # 移除重複資料：同一檔保留**日期最新**的那一列
        df = self.keep_latest_per_stock(df)

        # 存入 CSV 檔案到各自的資料夾
        data_type_dir: Path = (
            self.finmind_dir / FinMindDataType.STOCK_INFO_WITH_WARRANT.value.lower()
        )
        data_type_dir.mkdir(parents=True, exist_ok=True)
        csv_path: Path = data_type_dir / "taiwan_stock_info_with_warrant.csv"
        df.to_csv(csv_path, index=False, encoding=FileEncoding.UTF8_SIG.value)
        logger.info(
            f"Saved stock info with warrant data to {csv_path} ({len(df)} rows)"
        )

        return df

    def clean_broker_info(self, df: pd.DataFrame) -> Optional[pd.DataFrame]:
        """
        清洗證券商資訊表資料 (TaiwanSecuritiesTraderInfo)

        參數:
            df: pd.DataFrame - 從 crawler 取得的原始資料

        回傳值:
            pd.DataFrame 或 None（如果資料為空或驗證失敗）
        """
        if df is None or df.empty:
            logger.warning("Broker info data is empty")
            return None

        # 基本驗證：檢查必要欄位
        required_columns: List[str] = ["securities_trader_id", "securities_trader"]
        missing_columns: List[str] = [
            col for col in required_columns if col not in df.columns
        ]
        if missing_columns:
            logger.error(
                f"Missing required columns in broker info data: {missing_columns}"
            )
            return None

        # 移除重複資料
        df = df.drop_duplicates(subset=["securities_trader_id"], keep="first")

        # 存入 CSV 檔案到各自的資料夾
        data_type_dir: Path = (
            self.finmind_dir / FinMindDataType.BROKER_INFO.value.lower()
        )
        data_type_dir.mkdir(parents=True, exist_ok=True)
        csv_path: Path = data_type_dir / "taiwan_securities_trader_info.csv"
        df.to_csv(csv_path, index=False, encoding=FileEncoding.UTF8_SIG.value)
        logger.info(f"Saved broker info data to {csv_path} ({len(df)} rows)")

        return df

    def clean_broker_trading_daily_report(
        self, df: pd.DataFrame, write_csv: bool = True
    ) -> Optional[pd.DataFrame]:
        """
        清洗當日券商分點統計表資料 (TaiwanStockTradingDailyReportSecIdAgg)

        參數:
            df: pd.DataFrame - 從 crawler 取得的原始資料
            write_csv: bool - 是否寫出 `broker_trading/{broker_id}/{stock_id}.csv`；
                呼叫端會立刻把回傳的 DataFrame 入庫時傳 False，只做欄位檢查與去重

        回傳值:
            pd.DataFrame 或 None（如果資料為空或驗證失敗）
        """
        if df is None or df.empty:
            logger.warning("Broker trading daily report data is empty")
            return None

        # 基本驗證：檢查必要欄位
        required_columns: List[str] = [
            "stock_id",
            "date",
            "securities_trader_id",
            "buy_volume",
            "sell_volume",
        ]
        missing_columns: List[str] = [
            col for col in required_columns if col not in df.columns
        ]
        if missing_columns:
            logger.error(
                f"Missing required columns in broker trading daily report data: {missing_columns}"
            )
            return None

        # 移除重複資料（以 stock_id, date, securities_trader_id 為唯一鍵）
        df = df.drop_duplicates(
            subset=["stock_id", "date", "securities_trader_id"], keep="first"
        )

        # 寫 CSV 要先把同組合的舊檔整份讀進來合併再寫回，每個組合都付一次檔案 I/O；
        # 直接入庫的路徑用不到這份檔案（resume 看的是 DB ＋ metadata），故可略過
        if not write_csv:
            return df

        # 存入 CSV 檔案到各自的資料夾
        # 結構：broker_trading/{broker_id}/{stock_id}.csv
        data_type_dir: Path = (
            self.finmind_dir / FinMindDataType.BROKER_TRADING.value.lower()
        )
        data_type_dir.mkdir(parents=True, exist_ok=True)

        # 按照 securities_trader_id 和 stock_id 分組，為每個組合創建一個 CSV 檔案
        saved_files: List[str] = []
        for (securities_trader_id, stock_id), group_df in df.groupby(
            ["securities_trader_id", "stock_id"]
        ):
            # 創建 broker_id 資料夾
            broker_dir: Path = data_type_dir / str(securities_trader_id)
            broker_dir.mkdir(parents=True, exist_ok=True)

            # CSV 檔案路徑：{broker_id}/{stock_id}.csv
            csv_path: Path = broker_dir / f"{stock_id}.csv"

            # 如果檔案已存在，讀取現有資料並合併（使用追加模式避免覆蓋）
            if csv_path.exists():
                try:
                    existing_df: pd.DataFrame = pd.read_csv(
                        csv_path, encoding=FileEncoding.UTF8_SIG.value
                    )
                    # 合併資料
                    combined_df: pd.DataFrame = pd.concat(
                        [existing_df, group_df], ignore_index=True
                    )
                    # 再次去重（以 stock_id, date, securities_trader_id 為唯一鍵）
                    combined_df: pd.DataFrame = combined_df.drop_duplicates(
                        subset=["stock_id", "date", "securities_trader_id"],
                        keep="first",
                    )
                    group_df: pd.DataFrame = combined_df
                    logger.debug(
                        f"Merged existing data for broker_id={securities_trader_id}, "
                        f"stock_id={stock_id}. Total rows: {len(group_df)} "
                        f"(added {len(group_df) - len(existing_df)} new rows)"
                    )
                except Exception as e:
                    logger.warning(
                        f"Failed to read existing CSV file {csv_path}: {e}. "
                        f"Will overwrite file."
                    )

            # 寫入 CSV 檔案
            group_df.to_csv(csv_path, index=False, encoding=FileEncoding.UTF8_SIG.value)
            saved_files.append(f"{securities_trader_id}/{stock_id}.csv")
            logger.info(
                f"Saved broker trading daily report data to {csv_path} "
                f"(broker_id={securities_trader_id}, stock_id={stock_id}, {len(group_df)} rows)"
            )

        logger.info(f"Saved {len(saved_files)} broker trading daily report files")

        return df

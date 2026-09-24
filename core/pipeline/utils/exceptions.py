"""
Pipeline 專用例外類別

依 ETL 層級（Crawler／Cleaner／Loader／Updater）與資料來源分組，
**全部繼承 `PipelineError`**：呼叫端才能一次收下整條 pipeline 拋出的錯誤，
而不會連帶吞掉其他模組的 Exception。

這些例外存在的共同理由是「**失敗不可以長得像沒有資料**」：
靜默回 None 或回空表會讓行程以結束碼 0 結束，缺漏要事後對帳才會被發現。

使用方式：

    from core.pipeline.utils import FinMindError, FinMindQuotaExhaustedError

    try:
        df = crawler.crawl_broker_trading_daily_report(...)
    except FinMindQuotaExhaustedError:
        # 配額用盡：等待重置或稍後重試
        ...
    except FinMindError as e:
        # 其他 FinMind 錯誤
        ...
"""

from typing import Any, Dict, List, Optional, Set, Tuple

# -----------------------------------------------------------------------------
# Pipeline 通用基底
# -----------------------------------------------------------------------------


class PipelineError(Exception):
    """Pipeline 相關錯誤的共通基底，方便與其他模組的 Exception 區隔"""

    pass


# -----------------------------------------------------------------------------
# FinMind 例外階層（Base -> 具體錯誤類型）
# -----------------------------------------------------------------------------


class FinMindError(PipelineError):
    """FinMind 相關錯誤的基底類別"""

    @classmethod
    def is_quota_error(cls, exc: BaseException) -> bool:
        """
        - Description:
            判斷例外是否為 FinMind API 配額用盡

            **配額用盡沒有單一明確的訊號**，故三種跡象依序比對，
            並沿著 `__cause__` 鏈一路往下找：
            1. `KeyError('data')`：配額用盡時回傳的 JSON 沒有 `data` 鍵，
               FinMind 套件在 `pd.DataFrame(response["data"])` 就先炸了
            2. HTTP 402（Payment Required／用量超出上限）
            3. 訊息含 `402`、`quota`、`rate limit`、`exceeded`、`配額`
        - Parameters:
            - exc: BaseException
                要檢查的例外（可為鏈狀 `__cause__` 的根）
        - Return:
            - bool
                判定為配額相關錯誤為 True
        """

        err: Optional[BaseException] = exc
        seen: Set[int] = set()

        while err is not None and id(err) not in seen:
            seen.add(id(err))

            # FinMind 配額用盡時常回傳無 "data" 的 JSON，套件內 pd.DataFrame(response["data"]) 會拋 KeyError
            if (
                isinstance(err, KeyError)
                and len(err.args) > 0
                and err.args[0] == "data"
            ):
                return True

            # HTTP 402 (FinMind 配額用盡／用量超出上限)
            if hasattr(err, "response") and getattr(err, "response", None) is not None:
                status = getattr(err.response, "status_code", None)
                if status == 402:
                    return True

            # 訊息或內容含配額相關關鍵字
            msg: str = ""
            if getattr(err, "args", ()):
                msg = str(err.args[0]) if err.args else ""
            if not msg:
                msg = str(err)
            msg_lower: str = msg.lower()
            if any(
                k in msg_lower
                for k in (
                    "402",
                    "quota",
                    "rate limit",
                    "rate_limit",
                    "exceeded",
                    "配額",
                )
            ):
                return True

            err = getattr(err, "__cause__", None)

        return False


class FinMindQuotaExhaustedError(FinMindError):
    """
    FinMind API 配額用盡

    判定跡象見 `FinMindError.is_quota_error()`：HTTP 402、回應 JSON 無 `data` 鍵、
    或訊息含 quota／rate limit／exceeded。
    """

    pass


class FinMindRequestError(FinMindError):
    """
    FinMind API 呼叫失敗（配額用盡以外的原因：連線錯誤、非預期回應）

    **存在的理由是「失敗不可被當成沒有資料」**：crawler 若一律回 None，
    updater 就分不出「API 回空表」與「請求根本沒成功」，失敗會被記成 `NO_DATA`，
    行程以結束碼 0 成功結束。
    """

    pass


class FinMindPermissionError(FinMindRequestError):
    """
    FinMind 帳號等級不足，沒有該資料集的權限

    FinMind 套件把這種回應包成一般 `Exception`，訊息形如
    `FinMind API unexpected response: Your level is register. Please update your user level.`。
    **與其他請求錯誤分開**，是因為它對每一次請求都會失敗：批次更新遇到它應該立即中止，
    而不是讓數十萬個組合各失敗一次。
    """

    PERMISSION_KEYWORDS: Tuple[str, ...] = ("your level is", "update your user level")

    @classmethod
    def is_permission_error(cls, exc: BaseException) -> bool:
        """判斷例外（含 `__cause__` 鏈）是否為帳號等級不足"""

        err: Optional[BaseException] = exc
        seen: Set[int] = set()
        while err is not None and id(err) not in seen:
            seen.add(id(err))
            message: str = str(err).lower()
            if any(keyword in message for keyword in cls.PERMISSION_KEYWORDS):
                return True
            err = err.__cause__
        return False


# -----------------------------------------------------------------------------
# Crawler 例外
# -----------------------------------------------------------------------------


class IPBlockedError(PipelineError):
    """
    連續多次建立 Session 失敗，本機 IP 多半已被交易所封鎖

    **存在的理由是「被擋」不能長得像「沒資料」**：`find_best_session()` 若在
    連續失敗後回 `None`，呼叫端會把 `None` 當成休市，整段回補就安靜地跳過每一天，
    事後才發現資料整片缺失。

    這是需要人介入（換 IP、重開數據機）才能解除的狀態，故用例外表達。
    """

    def __init__(
        self, url: str, attempts: int, last_error: Optional[str] = None
    ) -> None:
        """記下被擋的 URL、嘗試次數與最後一次的錯誤訊息"""

        self.url: str = url
        self.attempts: int = attempts
        self.last_error: Optional[str] = last_error
        super().__init__(
            f"連續 {attempts} 次無法建立 Session（{url}），IP 可能已被封鎖"
            + (f"；最後一次錯誤：{last_error}" if last_error else "")
        )


class UnbuildableSeriesError(PipelineError):
    """
    有來源資料卻建不出衍生序列（例如連續合約排不出換月表）

    與「來源根本沒資料」刻意分開：後者在回補未完成時是正常狀態，
    前者代表到期月代碼異常或交易日曆有問題，是真的出錯。
    """

    pass


# -----------------------------------------------------------------------------
# Cleaner 例外
# -----------------------------------------------------------------------------


class ColumnLayoutError(PipelineError):
    """
    來源表格的欄位數與預期不符

    針對**依位置命名欄位**的來源（上櫃的多張表都不給欄名）：版面一改，
    位置命名會把每一欄都對到錯的名字——最低價變成成交量、成交金額變成收盤價
    ——而且完全不會報錯，資料照樣入庫。與其事後從數字裡看出不對勁，
    不如在命名之前就停下來。
    """

    def __init__(
        self,
        label: str,
        expected: int,
        actual: int,
        columns: Optional[List[Any]] = None,
    ) -> None:
        """記下來源名稱、預期與實際欄位數，以及實際的欄位清單"""

        self.label: str = label
        self.expected: int = expected
        self.actual: int = actual
        self.columns: Optional[List[Any]] = columns
        super().__init__(
            f"{label} 欄位數不符：預期 {expected} 欄、實際 {actual} 欄"
            + (f"；實際欄位：{columns}" if columns else "")
        )


class AnnouncementParseError(PipelineError):
    """
    公告附件解析不了：空白、下載到錯誤頁，或保證金附件的版面改了

    與「附件正常、只是沒有期貨列」（選擇權、部位限制公告）必須分開：後者寫進
    處理紀錄、以後不再下載；前者若也寫進去，這則公告就永遠不會再被重抓，
    只能整批強制重下。
    """

    def __init__(self, announcement_date: Any, reason: str) -> None:
        """記下公告日期與解析失敗的原因"""

        self.announcement_date: Any = announcement_date
        self.reason: str = reason
        super().__init__(f"{announcement_date} 公告附件解析失敗：{reason}")


class CleanFailureError(PipelineError):
    """
    部分來源的清洗失敗（版面改制），整批更新不算成功

    版面改制是逐來源、逐期間隔離的——其餘期間仍該清洗入庫，否則一個異常年份
    會讓整段回補作廢。但整批跑完後若有任何清洗失敗，就必須讓行程非零結束：
    除權息與減資是還原價的輸入，靜靜停止更新不會有任何錯誤訊息，
    只會讓還原價從某一天起停在舊值。
    """

    def __init__(self, source: str, failures: List[str]) -> None:
        """記下來源名稱與失敗的來源／期間清單"""

        self.source: str = source
        self.failures: List[str] = failures
        super().__init__(
            f"{source} 有 {len(failures)} 個來源／期間清洗失敗：{failures[:10]}"
        )


# -----------------------------------------------------------------------------
# Loader 例外
# -----------------------------------------------------------------------------


class DataLoadError(PipelineError):
    """
    部分或全部檔案入庫失敗

    **存在的理由是「不讓失敗變成靜默」**：loader 逐檔入庫時，單一檔案失敗
    （撞主鍵、欄位不符、檔案損毀）不應中止整批——其餘檔案仍該入庫。
    但整批跑完後若有任何失敗，就必須讓呼叫端知道，否則行程會以成功狀態結束，
    缺漏要靠事後逐檔對帳才會被發現。

    `failed_files` 保留失敗清單，供呼叫端記錄或重試。
    """

    def __init__(
        self, source: str, failed_files: List[str], succeeded: int = 0
    ) -> None:
        """記下來源名稱、失敗檔案清單與成功檔數"""

        self.source: str = source
        self.failed_files: List[str] = failed_files
        self.succeeded: int = succeeded
        super().__init__(
            f"{source} 入庫未完全成功：成功 {succeeded} 檔、失敗 {len(failed_files)} 檔"
        )


class SymbolNameConflictError(PipelineError):
    """
    同一批資料裡，一個證券代號對到兩個以上的證券名稱

    **這是「前導 0 被吃掉」在入庫當下唯一驗得出來的跡象**：`pd.read_csv()`／
    `read_html()` 只要看到某份檔案的代號全是數字就整欄推斷成整數，`006201`
    （元大富櫃50）少掉兩個 0 之後剛好是合法的上市代號 `6201`（亞弘電）。
    代號長度是 4、代號也真的存在，比對 `taiwan_stock_info` 同樣驗不出來。

    **寫進資料庫之後才查就來不及了**：`margin` 的主鍵是 `(date, stock_id)`，
    冒名的那一列會被 `INSERT OR IGNORE` 吞掉，表裡不留任何痕跡——後果不是缺資料，
    而是留下來的那一列可能是另一檔的數字。

    來源當天的表裡，同一個代號本來就只會有一個名稱（改名是跨時間的），
    出現兩個就一定是錯的，故整批視為失敗、一列都不寫，下次執行重試。
    """

    def __init__(self, label: str, conflicts: Dict[str, List[str]]) -> None:
        """記下來源名稱與「證券代號 → 衝突的名稱清單」；訊息只列前 10 筆"""

        self.label: str = label
        self.conflicts: Dict[str, List[str]] = conflicts
        detail: str = "；".join(
            f"{stock_id} → {names}" for stock_id, names in list(conflicts.items())[:10]
        )
        super().__init__(
            f"{label} 有 {len(conflicts)} 個證券代號對到多個證券名稱："
            f"{detail}" + ("…（僅列前 10 筆）" if len(conflicts) > 10 else "")
        )


# -----------------------------------------------------------------------------
# Updater 例外
# -----------------------------------------------------------------------------


class ProductUpdateError(PipelineError):
    """
    部分商品更新失敗

    **存在的理由與 `DataLoadError` 相同**：逐商品更新時，一個商品失敗（例如上市日
    晚於回補起點而觸發空產出保險絲）不應擋住其餘商品；但全部跑完後若有任何失敗，
    就必須讓呼叫端知道，否則 target 會以成功狀態結束。

    `failures` 保留「商品代碼 → 失敗原因」，供呼叫端記錄或重試。
    """

    def __init__(self, failures: Dict[str, str], succeeded: int = 0) -> None:
        """記下「商品代碼 → 失敗原因」與成功商品數"""

        self.failures: Dict[str, str] = failures
        self.succeeded: int = succeeded
        super().__init__(
            f"商品更新未完全成功：成功 {succeeded} 個、失敗 {len(failures)} 個；"
            + "；".join(f"{product}: {reason}" for product, reason in failures.items())
        )

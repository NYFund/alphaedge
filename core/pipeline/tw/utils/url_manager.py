from typing import Any, Dict

"""
台股資料來源的 URL 總表（TWSE／TPEX／MOPS／TAIFEX）

**整張表都是台灣的端點，故歸在 `tw/` 底下**：`core/pipeline/{shared,utils}/` 是
跨市場共用層，把只有台股用得到的端點放在那裡，等 `us/` 進來就會變成美股流程
相依台股模組。
"""


class URLManager:
    """URL Manager"""

    # fmt: off
    # 本表刻意以空行分隔每一條 URL，維護時一眼就看得出邊界；
    # ruff-format 會移除 dict 內的空行，故以 fmt: off／on 圈住這一段。
    # **例外僅限這個 dict**，其餘程式碼一律交給 formatter（見 CLAUDE.md §2.10）
    URLS: Dict[str, str] = {
        # 台灣證券交易所首頁 URL
        "TWSE_URL": "https://www.twse.com.tw/zh/",

        # 上市公司代號 URL
        "TWSE_CODE_URL": "https://isin.twse.com.tw/isin/C_public.jsp?strMode=2",

        # 上櫃公司代號 URL
        "TPEX_CODE_URL": "https://isin.twse.com.tw/isin/C_public.jsp?strMode=4",

        # 上市公司籌碼 URL
        "TWSE_CHIP_URL": "https://www.twse.com.tw/rwd/zh/fund/T86?date={date}&selectType=ALLBUT0999&response=html",

        # 上櫃公司籌碼 URL (URL1: 2007/4/21 ~ 2014/11/30, URL2: 2014/12/1 ~ present)
        "TPEX_CHIP_URL_1": "https://www.tpex.org.tw/www/zh-tw/insti/dailyTradeHis?type=Daily&cate=EW&date={date}&id=&response=html",
        "TPEX_CHIP_URL_2": "https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade?type=Daily&sect=EW&date={date}&id=&response=html",

        # 發行量加權股價報酬指數
        "TAIEX_RETURN_INDEX": "https://www.twse.com.tw/rwd/zh/TAIEX/MFI94U?date={date}01&response=html",

        # 上市信用交易統計（Summary）
        "TWSE_MARGIN_SUMMARY_URL": "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?date={date}&selectType=MS&response=html",

        # 上市信用交易統計（封閉式基金 Fund）
        "TWSE_MARGIN_FUND_URL": "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?date={date}&selectType=0049&response=html",

        # 上市信用交易統計（ETF）
        "TWSE_MARGIN_ETF_URL": "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?date={date}&selectType=0099P&response=html",

        # 上市信用交易統計（存託憑證 TDR）
        "TWSE_MARGIN_TDR_URL": "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?date={date}&selectType=9299&response=html",

        # 上市信用交易統計（股票）
        "TWSE_MARGIN_STOCK_URL": "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?date={date}&selectType=STOCK&response=html",

        # 上市信用交易統計（全部，含股票／ETF／TDR／受益證券，Ex: date = 20260731）
        "TWSE_MARGIN_ALL_URL": "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?date={date}&selectType=ALL&response=html",

        # 上櫃信用交易統計（全部，Ex: date = 2026/07/31）
        "TPEX_MARGIN_ALL_URL": "https://www.tpex.org.tw/www/zh-tw/margin/balance?date={date}&id=&response=html",

        # 上市除權除息計算結果表（支援日期區間，一次可取整年，Ex: 20240101 / 20241231）
        "TWSE_EX_RIGHT_URL": "https://www.twse.com.tw/rwd/zh/exRight/TWT49U?startDate={start_date}&endDate={end_date}&response=html",

        # 上櫃除權除息計算結果表（支援日期區間，一次可取整年，Ex: 2024/01/01 ~ 2024/12/31）
        # 日期**必須**用斜線格式：傳 20240101 不會報錯，會靜默退回「近三日」的預設區間
        "TPEX_EX_RIGHT_URL": "https://www.tpex.org.tw/www/zh-tw/bulletin/exDailyQ?startDate={start_date}&endDate={end_date}&response=json",

        # 上市減資（含面額變更）恢復買賣參考價（支援日期區間，一次可取整年）
        # 直接給「停止買賣前收盤價格」與「恢復買賣參考價」，相除即調整倍率，不需推算
        "TWSE_CAPITAL_REDUCTION_URL": "https://www.twse.com.tw/rwd/zh/reducation/TWTAUU?startDate={start_date}&endDate={end_date}&response=json",

        # 上櫃減資恢復買賣參考價（Ex: 2024/01/01 ~ 2024/12/31）
        # 日期格式與 TPEX_EX_RIGHT_URL 同樣**必須**用斜線；`詳細資料` 欄是一段 HTML，
        # 內含「每壹仟股換發新股票」，是調整倍率的獨立佐證
        "TPEX_CAPITAL_REDUCTION_URL": "https://www.tpex.org.tw/www/zh-tw/bulletin/revivt?startDate={start_date}&endDate={end_date}&response=json",

        # 上市收盤行情（Ex: date = 20250801）
        "TWSE_CLOSING_QUOTE_URL": "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={date}&type=ALLBUT0999&response=html",

        # 上櫃收盤行情（Ex: date = 2025/08/01）
        "TPEX_CLOSING_QUOTE_URL": "https://www.tpex.org.tw/www/zh-tw/afterTrading/otc?date={date}&type=EW&id=&response=html&order=0&sort=asc",

        # 市場開休市日期（Ex: date = 20260101；只看年份，一次回傳整年）
        # 列表混有「開始交易日／最後交易日」這類提醒列，那幾天其實有開市，分類在 cleaner
        "TWSE_HOLIDAY_SCHEDULE_URL": "https://www.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?response=json&date={date}",

        # 上市月營收財報（month: m, issuer_origin: {0: 國內發行, 1: 國外發行}）
        "TWSE_MONTHLY_REVENUE_REPORT_URL": "https://mopsov.twse.com.tw/nas/t21/sii/t21sc03_{roc_year}_{month}_{issuer_origin}.html",

        # 上櫃月營收財報（month: m, issuer_origin: {0: 國內發行, 1: 國外發行}）
        "TPEX_MONTHLY_REVENUE_REPORT_URL": "https://mopsov.twse.com.tw/nas/t21/otc/t21sc03_{roc_year}_{month}_{issuer_origin}.html",

        # === 上市上櫃財報（四大報表）爬蟲網站（新方式）===
        # 資產負債表
        "BALANCE_SHEET_URL": "https://mopsov.twse.com.tw/mops/web/ajax_t163sb05",

        # 綜合損益表
        "INCOME_STATEMENT_URL": "https://mopsov.twse.com.tw/mops/web/ajax_t163sb04",

        # 現金流量表
        "CASH_FLOW_STATEMENT_URL": "https://mopsov.twse.com.tw/mops/web/ajax_t163sb20",

        # 權益變動表
        "EQUITY_CHANGE_STATEMENT_URL": "https://mopsov.twse.com.tw/mops/web/ajax_t164sb06",

        # 台期貨每日交易行情（TAIFEX）
        # **與本表其他 URL 不同：這是 POST 端點，參數走 form data 而非 query string**，
        # 因此沒有 {} 佔位符——對它呼叫 get_url(..., date=...) 既不會報錯也不會生效
        # 必要的 form 欄位：
        #   queryDate: 查詢日（yyyy/MM/dd）
        #   commodity_id / commodity_idt: 商品代碼，兩者須帶相同值
        #     （Ex: TX 臺股期貨、MTX 小型臺指、TMF 微型臺指；股票期貨如台積電期為 CDF）
        #   MarketCode / marketCode: 0 一般交易時段（日盤）、1 盤後交易時段（夜盤）
        # 兩個時段的**欄位結構不同**（日盤 17 欄、夜盤 15 欄且無結算價與未沖銷契約量），
        # 且各自是獨立行情，須分別查詢後以 session 區分入庫
        # 一次僅能查一個商品；非交易日回傳的頁面無表格（`pd.read_html` 會拋 ValueError）
        "TAIFEX_FUTURES_PRICE_URL": "https://www.taifex.com.tw/cht/3/futDailyMarketReport",

        # 股票期貨、選擇權標的證券一覽表（TAIFEX）
        # 純 GET 頁面，整份清單一次回傳，不帶查詢參數
        # 表格為「當下快照」：**沒有掛牌日／下市日欄位**，兩者須由快照序列差分推得
        # 頁面給的是 2 碼商品代碼（Ex: CD 台積電期），行情頁的 commodity_id 要加尾碼 F
        # 解析時 `keep_default_na=False` 不可省：穩懋的代碼就是 `NA`，會被當成 NaN
        "TAIFEX_STOCK_FUTURES_LIST_URL": "https://www.taifex.com.tw/cht/2/stockLists",

        # === 台期貨籌碼（TAIFEX）===
        #
        # 三個端點都是 **POST ＋ 日期區間**，回的是 big5 CSV（`*Down` 結尾者為
        # 下載版，`*Qry` 是網頁版）。**一次請求就涵蓋所有商品**，
        # 不必逐商品打——大額交易人一天的回應約 80KB、含全部契約。
        #
        # ⚠️ **三者都是盤後公布**：當日資料要收盤後才有，回測當日一律只能用
        # 前一交易日的（見 `FuturesChipAPI` 的前視偏差說明）。
        # 三大法人：期貨各商品的多空交易口數與未平倉（自營商／投信／外資）
        "TAIFEX_FUTURES_INSTITUTIONAL_URL": "https://www.taifex.com.tw/cht/3/futContractsDateDown",
        # 大額交易人：前五大／前十大交易人的買賣方部位，含「特定法人」拆分
        "TAIFEX_FUTURES_LARGE_TRADER_URL": "https://www.taifex.com.tw/cht/3/largeTraderFutDown",
        # 選擇權 Put/Call Ratio：一天一列（成交量比與未平倉量比）
        "TAIFEX_FUTURES_PCR_URL": "https://www.taifex.com.tw/cht/3/pcRatioDown",

        # === 台期貨保證金（TAIFEX）===
        #
        # 現行保證金一覽表（股價指數類），純 GET，直接回 **CSV**（不是 HTML）
        # 編碼為 **big5**（`FileEncoding.BIG5`），非 UTF-8
        # 第一行是 `更新日期:YYYY/MM/DD`，那是這組保證金的生效日，資料表頭在第二行
        # 欄位：商品別, 結算保證金, 維持保證金, 原始保證金（皆為**每口固定金額**，單位：元）
        # **回測要用的是「原始保證金」**（委託人繳交），不是結算保證金（交易所向結算會員收）
        # 金額與契約乘數等比例：TX(200) 701,000 → MTX(50) 175,250 → TMF(10) 35,050
        # 本表**只有現值、沒有歷史**，歷史須走下方的公告查詢
        "TAIFEX_INDEX_MARGIN_URL": "https://www.taifex.com.tw/cht/5/indexMargingDown",

        # 現行保證金一覽表（股票類），同樣是 GET ＋ big5 CSV
        # **一份檔案裡有四個段落，欄位語意與更新日期都不同**（2026-09-01 逐段實查）：
        #   一(一) 股票期貨（標的為股票）  → **適用比例** ＋ 級距，296 檔
        #   一(二) 股票期貨（標的為 ETF）  → **每口固定金額**，24 檔
        #   二(一) 股票選擇權（股票）      → 比例，且是 a%／b% 雙欄
        #   二(二) 股票選擇權（ETF）       → A值／B值固定金額，一個商品佔兩列
        # 一(一) ＋ 一(二) ＝ 320 檔，正好等於 `futures_stock_universe` 的商品數
        # 股期的每口保證金 = 標的股價 × 契約單位 × 比例，
        # 契約單位取自 `futures_stock_universe.contract_size`（2000 股／100 股）
        # ⚠️ 解析時四個坑：
        #   1. **選擇權混在同一檔**，代碼與股期只差一個字母（DFF vs DFO、NYF vs NYA），
        #      混進來完全不會報錯——必須先切掉「二、」之後的全部內容
        #   2. **每段各有自己的「更新日期」**（實查 08/28 與 08/12），
        #      用全檔第一個日期套用到全部會讓 ETF 段的生效日錯 16 天
        #   3. **級距欄可以是空的**：296 檔中有 15 檔是處置／注意股票，沒有級距但
        #      比例更高（21.60%／22.95%／30.38%）；正常三級距是 13.50%／16.20%／20.25%
        #   4. 公司名稱含逗號（`"...Co., Ltd."`），**必須用 csv 模組解析**，不可 `split(",")`
        "TAIFEX_STOCK_MARGIN_URL": "https://www.taifex.com.tw/cht/5/stockMarginingDown",

        # 歷史公告查詢（保證金調整的歷史來源）
        # **與本表其他 URL 不同：這是 POST 端點**，參數走 form data，故無 {} 佔位符
        # 必要的 form 欄位：
        #   isQuery: **必須是 "1"**（頁面上的 hidden input 預設為空字串；
        #            傳 "true"／"Y"／"yes" 都只會回沒有結果的空表單，且不會報錯）
        #   queryStartDate / queryEndDate: 查詢區間（yyyy/MM/dd）
        #   queryKeyWord: 關鍵字；保證金調整用「保證金金額」
        #   newsType: **值是中文字**（`公告`／`新聞稿`／`契約調整`），不是代碼
        # 回應可直接 `pd.read_html` 解成「日期, 標題」兩欄；標題本身帶生效日與商品清單
        # 列的連結有兩種：直接指向 .pdf，或指向下方的 newsDetail 頁
        "TAIFEX_HISTORY_NEWS_URL": "https://www.taifex.com.tw/cht/11/hisNews",

        # 公告明細頁（取得附件連結用），GET
        # 附件為絕對 URL，直接出現在頁面上，不需另外組合
        # ⚠️ **附件型態隨年份而異**（2026-09-01 逐筆盤點 TX 的 62 筆公告）：
        #   - 2020/03 起（44 筆）：附 **CSV**，欄位含契約代碼與**調整前／調整後**的
        #     原始／維持／結算保證金——這是唯一可直接入庫的歷史來源
        #   - 2015~2019（16 筆）：只有 PDF，且**全部是掃描影像**
        #     （`/Image` + DCTDecode/CCITTFaxDecode、無 `/Font`、可抽文字 0 字），
        #     沒有 OCR 就取不到數值。目前不處理
        #   - 另有 2 筆（2020/01/17、2020/01/30）無附件
        "TAIFEX_NEWS_DETAIL_URL": "https://www.taifex.com.tw/cht/11/newsDetail?newsType={news_type}&idx={idx}",
    }
    # fmt: on

    @classmethod
    def get_url(cls, url_name: str, **kwargs: Any) -> str:
        """取得指定 URL，若提供 kwargs 則進行格式化"""

        if url_name not in cls.URLS:
            raise ValueError(f"URL key '{url_name}' not found in URLManager")

        url: str = cls.URLS[url_name]
        return url.format(**kwargs) if kwargs else url

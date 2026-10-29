# 指令教學

本文件整理 `apps/` 底下的五個執行入口：資料更新（`apps.update_db`）、資料維護（`apps.delete_price_data`、`apps.clean_logs`）、回測（`apps.backtest`）與實盤（`apps.live`）。
各旗標的完整說明以 `python -m apps.<入口> --help` 為準。

## 資料更新：`python -m apps.update_db`

### 功能說明

透過 `--target` 指定要更新的資料類型，可單一或多選；未指定時預設為 `no_tick`。

- **執行順序固定**，依 `apps/update_db.py` 的 `TARGETS` 註冊表，與 `--target` 的點名順序無關：`price` 一定排在 `chip`／`margin`／`tick` 之前（它們以 `price` 表的交易日為日曆）。
- **任一 target 失敗不中斷其餘 target**；全部跑完（含重試）仍有失敗時以結束碼 1 收場，訊息會列出失敗與成功的 target。
- 收尾一律清掉 7 天以前的 `logs/api/` 輪替檔（在判斷結束碼之前執行，失敗的那天也會清）。

### 參數

| 參數 | 說明 |
|------|------|
| `--target <target> [<target> ...]` | 欲更新的資料類型，可指定一個或多個；預設 `no_tick` |
| `--from YYYY-MM-DD` | 覆寫以**日期**為單位的 target 的起日（見下方〈`--from`〉） |
| `--retry-failed ROUNDS` | **只重跑失敗的 target**，最多再跑幾輪（預設 `0`，不重試）；全部補上就提前結束，結束碼以最後一輪仍失敗的 target 為準 |
| `--retry-wait MINUTES` | 每輪重跑前等幾分鐘（預設 `60`；交易所封鎖來源 IP 通常要數十分鐘以上才解除） |
| `--defer-failed` | 只跑一輪：失敗的 target 記在 `logs/update_db_deferred.json`，交給之後的 `--retry-deferred`，本次以 0 結束（寫不進清單時以 1 結束）。不可與 `--retry-failed`（大於 0）或 `--retry-deferred` 同時使用 |
| `--retry-deferred` | 不跑 `--target`，只重跑 `--defer-failed` 記下的 target（沿用那一輪的 `--from`），輪數與等待依 `--retry-failed`／`--retry-wait`；清單讀完就刪，不是當天的清單不用 |

`--retry-failed` 與 `--retry-wait` 不可為負數，否則以用法錯誤（結束碼 2）結束。

排程把重試拆成兩段：06:00 先 `--defer-failed` 跑一輪，接著補比，最後才 `--retry-deferred --retry-failed 4 --retry-wait 60`。
補比因此不必等最多 4 小時的重試；補比要用的資料第一輪就失敗時，那條補比會因資料太舊拒絕（結束碼 3），重試之後再跑一次。

### Target 對照表

| 選項 | 說明 |
|------|------|
| `price` | 收盤價 |
| `tick` | 台股逐筆成交（Shioaji ticks，寫入 TimescaleDB） |
| `chip` | 三大法人籌碼 |
| `margin` | 信用交易（融資融券餘額） |
| `short_sale_list` | 平盤下得融（借）券賣出名單與當日暫停註記（2013-09-23 起；回測放空檢核用） |
| `day_trade_list` | 現股當沖標的名單與暫停先賣後買註記（2014-01-06 起） |
| `dividend` | 除權除息計算結果表（含還原係數、現金股利） |
| `corporate_action` | 非除權息的公司行動（減資、分割、面額變更） |
| `fs` | 財報（Financial Statement，含逐檔查詢的權益變動表） |
| `mrr` | 月營收報表（Monthly Revenue Report） |
| `market_holiday` | 市場開休市日期（TWSE 公告；每次重抓去年／今年／明年，寫入 `tw_stock.db`）。實盤盤前判定交易日的主來源 |
| `futures_price` | 台期貨指數期貨日行情（寫入 `tw_futures.db`；商品見 `FUTURES_TARGET_PRODUCTS`） |
| `futures_stock_universe` | 股票期貨標的池（寫入 `tw_futures.db`；每次執行留下一份當日快照） |
| `futures_stock_price` | 股票期貨行情（商品清單取自標的池，只爬流動性前 `STOCK_FUTURES_TOP_N` 檔）。**只能點名** |
| `futures_continuous` | 台期貨連續合約（由 `futures_price_daily` 整段重建，不連網路） |
| `futures_margin` | 台期貨保證金現行一覽表（變動序列，寫入 `tw_futures.db`） |
| `futures_chip` | 台期貨籌碼（三大法人、大額交易人、選擇權 PCR） |
| `finmind` | 全部 FinMind（台股總覽 ＋ 證券商 ＋ 券商分點）。**只能點名**：現行帳號等級拿不到券商分點，放進預設會每晚失敗 |
| `stock_info` | FinMind 台股總覽（不含權證）。**只能點名** |
| `stock_info_with_warrant` | FinMind 台股總覽（含權證）。**只能點名** |
| `broker_info` | FinMind 證券商資訊。**只能點名** |
| `broker_trading` | FinMind 券商分點統計。**只能點名** |
| `all` | 上列全部，**不含**只能點名的 target |
| `no_tick` | `all` 再扣掉 `tick`（**預設**）。tick 需要 Shioaji 金鑰、`[tick]` 選用相依與執行中的 TimescaleDB，沒有這些的機器不排除的話每晚都會以結束碼 1 收場 |

「只能點名」的 target 不會被 `all`／`no_tick` 選到，必須在 `--target` 明確寫出。

### 範例

```bash
python -m apps.update_db                                  # 等同 --target no_tick
python -m apps.update_db --target price                   # 單一 target
python -m apps.update_db --target chip price tick         # 多個 target（仍依註冊表順序執行）
python -m apps.update_db --target futures_stock_price     # 只能點名的 target
python -m apps.update_db --retry-failed 4 --retry-wait 60 # 失敗的 target 每小時重跑，最多 4 輪
```

### 各 target 的注意事項

- **`fs`**：權益變動表（`equity_change`）是逐檔查詢，一個年季約 2,000 次請求；重跑只補差集（已入庫與已確認沒資料的公司都不會重打）。資料形狀與已知限制見 [權益變動表](../pipeline/equity-change.md)。
- **`dividend`**：上市走證交所、上櫃走櫃買中心，皆為全歷史。
- **`corporate_action`**：每次掃整個區間——這類事件是事後公告。
- **`short_sale_list`／`day_trade_list`**：日常更新已含在 `all`／`no_tick` 內；從起點回補約 3,200 個交易日 × 每日兩個請求，以現行節流要數小時。
- **`market_holiday`**：一年一次請求，每次重抓去年、今年、明年並整年替換。明年的公告通常 12 月才出來，那之前明年會被記為「尚未公告」跳過，屬正常；實盤查到未入庫年度的日期時，若其他日曆來源也判定不了就拒絕啟動。
- **`futures_price`**：一次只能查一個商品、日盤與夜盤分開查，請求數 ＝ 商品數 × 2 × 交易日數；自 `DEFAULT_FUTURES_START_DATE`（2015-01-01）起，單檔 TX 首次回補約 6,100 次請求。
- **`futures_stock_universe`**：整份清單一次 GET，同一天重跑不會產生第二份快照。來源沒有掛牌日／下市日欄位，兩者由快照序列差分推得，故建議每日更新（快照愈稀疏，推出來的日期誤差愈大）。下游取商品清單一律用 `FuturesStockUniverseAPI.get_products()`，不要另外手寫清單。

期貨各 target 的回補細節（新商品上市日、股期流動性排序、保證金歷史公告）見 [台期貨平台](../futures/tw-futures-platform.md)〈2.3 指令〉。

### `--from`：把起日往前拉

```bash
python -m apps.update_db --target price --from 2013-01-01
```

**平常不需要用**：以日期為單位的 updater 候選日期是「日曆 − 表內已有 − 已確認沒有資料」的差集，中間缺的日子會自動補回。`--from` 只在要把起點拉到比預設更早時才用得到。

- 只影響有日期區間的 target；`fs`／`mrr`（以年季／年月為單位）與沒有區間的 target（`futures_chip`、`futures_stock_universe`、`futures_margin`、`futures_continuous`、`market_holiday`、FinMind 的 `stock_info`／`stock_info_with_warrant`／`broker_info`）不受影響。
- **對 `futures_price`／`futures_stock_price` 拉不到更早**：期貨續跑的差集從表內該商品的第一筆起算，`--from` 早於那一天也不會往前補。往前擴張要直接呼叫 `FuturesPriceUpdater.update(start_date=..., end_date=..., resume=False)`。

## 刪除單日行情：`python -m apps.delete_price_data`

刪除 `tw_stock.db` 的 `price` 表中指定日期的所有資料。**預設只預覽不刪除**——打錯一個日期就少掉一整天、上千檔的收盤行情，而且要重跑 ETL 才補得回來。

```bash
# 只報告會刪幾筆，不寫入
python -m apps.delete_price_data --date 2025-07-13

# 實際刪除；會要求輸入完整日期做確認
python -m apps.delete_price_data --date 2025-07-13 --apply

# 排程用：跳過互動確認
python -m apps.delete_price_data --date 2025-07-13 --apply --yes
```

- `--date` 接受 `YYYY-MM-DD` 或 `YYYY/MM/DD`（例如 `2025/7/13`）。
- 非互動環境（無 tty）沒有 `--yes` 時一律視為未確認、取消刪除。
- 日期解析失敗、找不到資料庫、未確認、刪除後仍有殘留時以結束碼 1 結束；當天本來就沒有資料算成功。

## 清理已輪替的日誌：`python -m apps.clean_logs`

```bash
python -m apps.clean_logs                              # 預覽（不刪）
python -m apps.clean_logs --apply                      # 實際刪除，預設保留 30 天
python -m apps.clean_logs --apply --bucket api --days 7
```

| 參數 | 說明 |
|------|------|
| `--days DAYS` | 保留天數（預設 30） |
| `--bucket {api,pipeline,backtest}` | 只清理指定的桶，可重複指定；未指定時三個桶全清 |
| `--apply` | 實際刪除；未指定時只列出會被刪的檔案 |

只刪檔名帶時間戳的已輪替檔（`<name>.<時間戳>.log`），使用中的 `xxx.log` 一律保留——刪掉正被寫入的檔案，日誌會就此靜默消失。`apps.update_db` 收尾會自動以 7 天清理 `api` 桶。

## 回測：`python -m apps.backtest --strategy <StrategyClassName>`

`--strategy` 收**策略的類別名稱**，一次只接一支；可用的策略是 `strategies/stock/`、`strategies/futures/` 底下的非抽象子類（例如 `VolumeBreakoutMomentumStrategy`、`MomentumFuturesStrategy`）。

```bash
python -m apps.backtest --strategy <StrategyClassName>
python -m apps.backtest --strategy <StrategyClassName> --show      # 畫完圖在瀏覽器開起來
python -m apps.backtest --strategy <StrategyClassName> --no-show   # 不開圖（覆寫 ALPHAEDGE_SHOW_FIGURES）
# 覆寫策略宣告的回測區間與初始資金（皆選填，不帶時沿用策略預設）
python -m apps.backtest --strategy <StrategyClassName> --start 2024-01-01 --end 2024-12-31 --capital 500000
# 改用 TICK 級別、讀全市場逐筆
python -m apps.backtest --strategy <StrategyClassName> --scale TICK --tick-universe all
```

| 參數 | 說明 |
|------|------|
| `--strategy` | 策略類別名稱（必填，只接一支） |
| `--start`／`--end YYYY-MM-DD` | 覆寫回測起迄日 |
| `--capital` | 覆寫初始資金 |
| `--scale {TICK,DAY}` | 改用的回測級別；只能選策略 `supported_scales` 宣告支援的 |
| `--tick-universe {strategy,all}` | TICK 回測每天讀哪些標的：`strategy`＝策略 `get_live_symbols()` 的清單加持倉（預設，與實盤一致），`all`＝全市場（一天上百萬筆，較慢）。DAY 回測不可帶 |
| `--show`／`--no-show` | 回測結束後是否在瀏覽器開啟圖表；都沒給時依環境變數 `ALPHAEDGE_SHOW_FIGURES`，預設不開（圖一律存成檔案） |

本入口只認得回測的旗標：帶了實盤旗標（例如 `--production`）會被 argparse 以用法錯誤拒絕。

| 結束碼 | 意義 |
|:---:|---|
| 0 | 正常結束 |
| 1 | 策略設定有誤（用了已移除的設定欄位，或策略自己的 `scale` 與 `supported_scales` 矛盾）；策略開啟交易所名單檢核（`ShortConstraint.check_short_sale_list`／`check_day_trade_list`）而區間早於名單起點或名單有缺日；回測區間內沒有任何交易日。原因印在 stderr，先跑對應的 `update_db` target 或改區間。其他未預期的例外也是 1 |
| 2 | 用法錯誤：策略名找不到（會列出可用策略）、未知旗標、起日晚於迄日、起日早於歷史資料起點（台股 2013-01-01、期貨 2015-01-01）、資金不為正數、`--scale` 不在策略支援範圍、DAY 回測帶 `--tick-universe`、TICK 回測選 `strategy` 但策略沒覆寫 `get_live_symbols()` |

結果輸出在 `results/<strategy_name>/`（可用環境變數 `ALPHAEDGE_RESULTS_DIR` 改根目錄）。目錄名取策略的 `self.strategy_name`，**不是** `--strategy` 吃的類別名（`VolumeBreakoutMomentumStrategy` → `results/Volume-Breakout-Momentum/`）。

## 實盤：`python -m apps.live --strategy <StrategyClassName[,…]> --phase <段落>`

```bash
# 模擬環境（預設）跑開盤段
python -m apps.live --strategy VolumeBreakoutMomentumStrategy --phase open
# 走完整流程但不送出委託
python -m apps.live --strategy VolumeBreakoutMomentumStrategy --phase close --dry-run
# 正式環境：兩個旗標缺一不可
python -m apps.live --strategy VolumeBreakoutMomentumStrategy --phase open --production --confirm-production
# 次日資料更新後補比前一交易日的訊號 parity（不連券商、不送單）
python -m apps.live --strategy VolumeBreakoutMomentumStrategy --phase parity --date 2026-10-15
# 人工恢復交易模式（不給策略名時恢復帳戶層）；不要放進排程
python -m apps.live --strategy VolumeBreakoutMomentumStrategy --phase open --resume-trading
# 以券商部位重建歸屬帳：先只列計畫，確認後加 --confirm-resync 再跑一次；不可帶 --phase
python -m apps.live --strategy VolumeBreakoutMomentumStrategy --resync-from-broker
```

| 旗標 | 說明 |
|------|------|
| `--strategy` | 策略類別名稱，可用逗號分隔多支（共用一個帳戶）。台股與期貨的段落時窗不重疊，要分成兩個行程各自排程，混在同一行會在組裝時被拒絕 |
| `--phase` | `open`／`close`／`after_close`／`intraday`／`parity`；除 `--resync-from-broker` 外必填。`after_close` 不送新倉單（刷新委託、對帳、回填成本、殘量處理、輸出報表）；`parity` 為次日資料更新後補比前一交易日，不連券商、不送單 |
| `--date YYYY-MM-DD` | 只與 `--phase parity` 併用：要補比的交易日，預設為歷史資料最新日 |
| `--simulation`／`--production` | 模擬（預設）／正式環境；`--production` 必須同時帶 `--confirm-production`。正式環境刻意沒有對應的環境變數 |
| `--broker` | `shioaji`（預設）或 `fake`。`fake` 只供測試直接注入，從命令列使用一律以結束碼 2 拒絕 |
| `--dry-run` | 走完整流程但不真的送出委託 |
| `--resume-trading [策略名 ...]` | 人工恢復交易模式：不給策略名時恢復帳戶層與所有策略；給策略名時只恢復那幾支（須在 `--strategy` 之中），帳戶層不動 |
| `--resync-from-broker`／`--confirm-resync` | 以券商部位重建歸屬帳；不帶 `--confirm-resync` 只列計畫 |

所有防呆都在建立任何券商連線之前完成。實盤**只有未預期的例外才回傳 1**，其餘結果各有自己的結束碼，排程要依碼處理：

| 結束碼 | 意義 |
|:---:|---|
| 0 | 正常結束 |
| 1 | 未預期的例外 |
| 2 | 用法錯誤：策略名找不到或重複、缺 `--phase`、`--date` 沒搭 `--phase parity`、`--production` 沒帶 `--confirm-production`、使用 `fake` 券商、`--resume-trading` 指名的策略不在 `--strategy` 之中、重建相關旗標的組合不合法、多支策略的段落時窗沒有交集、策略設定實盤不支援（例如期貨的 `OPEN_INTEREST` 換月規則） |
| 3 | 資料未更新到前一個交易日，或判定不出今天是否為交易日 |
| 4 | 對帳不一致，或以券商部位重建被拒絕 |
| 5 | kill switch 生效 |
| 6 | 上次結束時帳戶層交易模式非 NORMAL，本次未帶 `--resume-trading` |
| 7 | `--resync-from-broker` 只列出重建計畫、沒有寫入（未帶 `--confirm-resync`） |
| 143 | 收到 SIGTERM：已撤未成交單、寫完結束紀錄才離開 |

**`6` 要和 `4`、`5` 分開**：排程看到 4／5 是「今天剛出事」，看到 6 是「昨天出的事還沒有人處理」。**`7` 不是 0**：只列計畫代表歸屬帳仍與券商不一致。
排程時刻表與完整操作見[實盤部署與排程](../deployment/live-deployment.md)。

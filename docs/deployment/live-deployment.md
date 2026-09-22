# 實盤部署與排程（Live Deployment）

實盤以 **Shioaji** 為券商閘道，程式入口是 `run.py --mode live`。本文件說明怎麼在容器裡跑、
怎麼排程，以及停止、失敗時會發生什麼事。映像本身的建置見 [正式環境部署](prod-deployment.md)。

## 1) 執行模型：逐段落啟動，不常駐

實盤**不是一個一直跑著的服務**，而是一天內由排程觸發幾次、每次跑完一個段落就結束的行程：

| 段落 | `--phase` | 做什麼 |
|------|-----------|--------|
| 開盤 | `open` | 補平前一天沒成交的平倉單 → 收開盤訊號 → 送單 |
| 尾盤 | `close` | 期貨先換月 → 收收盤訊號 → 送單 |
| 盤後 | `after_close` | 刷新委託、對帳、回填費用、殘量處理、輸出報表 |
| 盤中 | `intraday` | 逐筆行情驅動，跑到收盤（只有 `is_intraday` 的策略需要） |

每個段落各自連線、各自結束，狀態全靠 `data/db/tw_trading.db`（委託、成交、歸屬帳、交易模式）
在段落之間延續。某一段崩潰不會拖到下一段：下一段啟動時會先把沒有結束紀錄的舊執行標成崩潰、
讀回交易模式、接管未終結的委託，再對帳。

## 2) 事前準備

1. `.env`（專案根目錄，不進 git、不進映像）至少要有：
   - `API_KEY`、`API_SECRET_KEY`：Shioaji 金鑰。
   - `SHIOAJI_CA_PASSWORD`：下單憑證密碼（`SHIOAJI_CA_PATH` 在容器內由 compose 覆寫成 `/ca/...`）。
   - （建議）`ALPHAEDGE_LIVE_NOTIFY_CHANNEL`／`_TOKEN`／`_TARGET`：推播管道。沒設的話異常只會寫進 log 與 `live_risk_event`，不會主動通知任何人。
2. CA 憑證（`.pfx`）放在**專案目錄以外**，預設 `~/.alphaedge/ca/Sinopac.pfx`。
   目錄與檔名可用環境變數 `ALPHAEDGE_CA_DIR`、`SHIOAJI_CA_FILE` 改（compose 讀，程式不讀）。
3. 策略要標 `live_ready = True` 並宣告 `live_schedule`，否則啟動檢查會拒絕。
4. 資料庫要在開盤前更新到前一個交易日：實盤啟動時會檢查，沒更新就以退出碼 3 拒絕啟動。

## 3) 用容器執行

`live` service 掛在 compose 的 `live` profile 下，`docker compose up` **不會**把它帶起來：

```bash
docker compose --profile live build live
docker compose --profile live run --rm live --strategy MomentumStrategy1 --phase open
```

| 設定 | 為什麼 |
|------|--------|
| `entrypoint` 寫死 `--mode live` | `run` 後面的參數會整個取代 `command`；放在 command 的話漏寫一次就變成跑回測 |
| `./data` 可寫掛載 | 要寫 `tw_trading.db`；研究庫（`tw_stock.db` 等）一律唯讀開啟，不會被寫到 |
| CA 目錄唯讀掛在 `/ca` | 憑證不進映像、不進 git |
| `TZ=Asia/Taipei` | 段落時窗、交易日判定都以台北時間為準；主機排程也要是台北時間 |
| `stop_grace_period: 90s` | 停止時要先撤單、等券商回覆、寫結束紀錄；預設 10 秒到了就被 SIGKILL |
| 預設連**模擬環境** | 正式環境要另外帶 `--production --confirm-production`，**刻意不寫進 compose**：兩個旗標只能出現在排程指令裡、由人明確寫下 |

不用容器時，在專案根目錄以 `uv run python run.py --mode live ...` 執行，效果相同。

## 4) 排程

所有時間都是**台北時間、交易日**。台股與期貨的段落時窗不同（尾盤：台股 13:25～13:29、
期貨 13:30～13:44，兩者不重疊），**每個段落都要依市場分開排程**：組裝引擎時會把所有策略的
時窗合併取交集，同一行指令混合兩個市場的策略，不論哪個段落都會在啟動時被拒絕。

| 時間 | 指令 | 說明 |
|------|------|------|
| 08:00 | `uv run python -m tasks.update_db` | 預設 `--target no_tick`，更新到前一個交易日；實盤啟動時的資料新鮮度檢查靠它 |
| 08:30 | `... run --rm live --strategy <股票策略> --phase open` | 台股開盤段（送單時窗 08:30～08:59） |
| 08:40 | `... run --rm live --strategy <期貨策略> --phase open` | 期貨開盤段（送單時窗 08:45～08:59） |
| 13:20 | `... run --rm live --strategy <股票策略> --phase close` | 台股尾盤段；程式會等到 13:25 才送單 |
| 13:28 | `... run --rm live --strategy <期貨策略> --phase close` | 期貨尾盤段；**換月在這一段的開頭執行** |
| 14:30 | `... run --rm live --strategy <股票策略> --phase after_close` | 台股盤後作業 |
| 14:35 | `... run --rm live --strategy <期貨策略> --phase after_close` | 期貨盤後作業 |
| 08:00～15:00 每 5 分鐘 | `uv run python -m scripts.live_watchdog` | 存活監控，**在主機上跑、和 `live` 分開** |

`crontab` 範例（`...` 為 `docker compose -f /path/to/AlphaEdge/docker-compose.yml --profile live`）：

```cron
CRON_TZ=Asia/Taipei
0  8 * * 1-5  cd /path/to/AlphaEdge && uv run python -m tasks.update_db
30 8 * * 1-5  ... run --rm live --strategy MomentumStrategy1 --phase open
40 8 * * 1-5  ... run --rm live --strategy MomentumFuturesStrategy --phase open
20 13 * * 1-5 ... run --rm live --strategy MomentumStrategy1 --phase close
28 13 * * 1-5 ... run --rm live --strategy MomentumFuturesStrategy --phase close
30 14 * * 1-5 ... run --rm live --strategy MomentumStrategy1 --phase after_close
35 14 * * 1-5 ... run --rm live --strategy MomentumFuturesStrategy --phase after_close
*/5 8-14 * * 1-5 cd /path/to/AlphaEdge && uv run python -m scripts.live_watchdog
```

- **休市日照排也沒關係**：程式會判定當天休市而不進送單路徑、正常結束。判斷不出來（交易日來源都失效）時以退出碼 3 拒絕啟動。
- **存活監控要和實盤分開排程**：和實盤同一個行程或同一個容器的心跳，會跟著被監控的東西一起死。它唯讀開 `tw_trading.db`、不連券商；期貨的尾盤段也要監控時加上 `--expect open=08:30 close=13:28 after_close=14:30`。
- macOS 用 launchd 時，每一行寫成一個 `StartCalendarInterval` 的 plist；launchd 以系統時區觸發，主機時區要設成台北。
- **不要放進排程**的指令：`--resume-trading`（寫進排程就等於自動解除降級）、`--resync-from-broker --confirm-resync`（重建要人看過計畫才寫入）。

## 5) 停止與退出碼

- **SIGTERM**（`docker stop`、排程逾時、`kill`）：先撤未成交單、續收回報、把結束原因寫進 `live_run`，再以退出碼 **143** 結束。收尾期間再收到的 SIGTERM 會被忽略；要強制結束用 SIGKILL。
- 退出碼（排程可據此決定要不要告警）：

| 碼 | 意義 |
|----|------|
| 0 | 正常結束（含休市日、只跑對帳的段落） |
| 1 | 未預期的例外 |
| 2 | 用法錯誤（策略名、旗標組合） |
| 3 | 資料未更新到前一個交易日，或判定不出是否為交易日 |
| 4 | 對帳不一致（或以券商部位重建被拒絕） |
| 5 | kill switch 生效 |
| 6 | 帳戶層交易模式非 NORMAL（前一天的降級還沒有人處理） |
| 7 | `--resync-from-broker` 只列出計畫、沒有寫入 |
| 143 | 收到 SIGTERM，已撤單並寫入結束紀錄 |

## 6) 已知限制

- **期貨換月比回測早一天**：回測可以撐過最後交易日、隔天以結算價平掉舊月；實盤在最後交易日的尾盤段舊月已收盤，故一律在最後交易日的前一個交易日換月。
- **未來的國定假日不在交易日曆裡**：換月判定以平日近似未來的交易日，距到期日之間夾著假日時可能晚一天換月。
- **模擬環境的期貨保證金查詢一律回 0**：送單前的帳戶層保證金檢查在模擬環境會略過（只做策略層），正式環境查不到就擋單。

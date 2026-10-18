# 正式環境小額上線檢查表（股票線）

股票線第一次連正式環境前，照本表**逐項勾選**。標「共通」的項目期貨線同樣適用（期貨線目前沒有自己的檢查表）；
日後另立期貨線檢查表時，共通項目要逐字複製過去而不是互相引用——上線時是照著單子一項一項勾，跳到另一份會漏勾。

> **使用前提**：模擬環境演練的股票線驗收（穩定性與下單路徑）已通過。
> 本表列的是「模擬環境驗不到、或正式環境才會不同」的事；模擬環境已驗過的行為不在這裡重複。

相關文件：[實盤部署與排程](../deployment/live-deployment.md)、[實盤執行層](execution-layer.md)、
[策略撰寫指南](../../strategies/README.md)〈實盤設定〉。

---

## 一、環境與憑證（共通）

- [ ] **CA 憑證**：直接執行時 `.env` 的 `SHIOAJI_CA_PATH` 指到正式憑證；用容器時改設 `ALPHAEDGE_CA_DIR`／`SHIOAJI_CA_FILE`
      （compose 會把 `SHIOAJI_CA_PATH` 覆寫成 `/ca/...`）。檔案權限只有執行帳號可讀（`chmod 600`），不在專案目錄、不進版控。
- [ ] **正式金鑰與模擬金鑰分開**：`.env` 換成正式那組，模擬那組另存；兩組不要同時出現在同一份 `.env`。
- [ ] **VPN 關閉**：開著 VPN 時 Shioaji 登入會逾時，整個段落直接失敗。
- [ ] **登入驗證**：在段落空檔以 `--production --confirm-production --resync-from-broker`（只列計畫、不帶 `--confirm-resync`）
      連一次正式環境，確認登入、憑證與帳號都正常，並看一眼計畫裡券商端的部位。
- [ ] **正式環境的排程另外寫**：`scripts/launchd/rehearsal_schedule.py` 只產生模擬環境排程，沒有正式環境選項。
      正式排程的每個交易段落都要帶 `--production --confirm-production`，且不與模擬段落混在同一組排程；
      `--resume-trading` 與 `--resync-from-broker --confirm-resync` 不可出現在排程裡。
- [ ] **排程重裝**：排程內容（策略名、入口模組、時刻）改過之後要重裝 launchd 排程，再用 `--status` 對照實際參數——
      已安裝的 plist 參數不會跟著程式更新。launchd 依安裝當下的時差換算台北時間，**本機夏令時間切換後也要重裝**，否則全部偏一小時。
- [ ] **跑的是哪一版**：launchd 跑的是主目錄磁碟上的檔案；上線期間開發一律在 worktree，主目錄只在段落空檔更新，
      更新前後以 `git log -1` 確認實際跑的版本。

## 二、風控與資金（共通）

- [ ] **風控門檻收緊到小額**：在 `deploy/live_risk.toml` 以策略類別名分段寫要收緊的欄位（只能收緊，寫寬了啟動時報錯），
      帳戶層寫 `[account]` 段。單筆比例（`single_order_ratio`）、單日比例（`daily_ratio`）、單筆張數上限（`max_order_volume`）、
      單一標的曝險比例都要對照首日預計的額度重新決定；單筆與單一標的比例不可小於 1 ÷ `live_max_holdings`，否則每張買單都會被擋。
      啟動後查 `live_run.risk_config_json` 確認實際生效的值。
- [ ] **跨行程的資金總量**：股票的盤中段落與開盤／尾盤段落是**不同行程**，啟動時各自只檢查自己載入的策略額度。
      手動確認 **Σ 全部股票策略的 `live_capital`（跨行程加總）≤ 帳戶總權益 × 0.95**（`CAPITAL_SAFETY_RATIO`）。
- [ ] **kill switch 演練**：建立 `ALPHAEDGE_LIVE_KILL_SWITCH_PATH`（未設定時為 `data/live/KILL_SWITCH`），
      確認下一個段落拒絕送單、交易模式轉 `HALTED`、退出碼 5；刪掉檔案後以 `--resume-trading` 解除。
- [ ] **降級後重啟仍維持降級**：讓某支策略進入 `REDUCE_ONLY`，重啟行程後確認仍是 `REDUCE_ONLY`，
      沒有被啟動流程靜默解除。

## 三、通知與監控（共通）

- [ ] **通知管道實際送達**：`.env` 設好 `ALPHAEDGE_LIVE_NOTIFY_CHANNEL`／`_TOKEN`／`_TARGET`，
      觸發一次 CRITICAL（例如 kill switch 演練）並確認手機或頻道真的收到；
      啟動 log 不得出現「本次啟動沒有可用的通知管道」。
- [ ] **watchdog 已排程且與交易行程分開**：`--status` 看得到 `com.alphaedge.live.watchdog`；
      `--expect` 逐市場列出實際排程的每個段落（含 `intraday:stock`），故意讓一個段落不跑，確認 15 分鐘內收到告警。
- [ ] **監看方式**：每天收盤後看三樣——`logs/launchd/*.err.log` 的 ERROR／CRITICAL、
      `live_risk_event` 當日事件、隔天 06:00 資料更新後的 parity 補比結果（`UNEXPLAINED` 必須為 0）。

## 四、首日節奏（共通）

- [ ] **首日只跑 `--dry-run`**：委託只寫進紀錄庫、不送券商；收盤後把當日 dry-run 委託與策略預期逐筆比對。
- [ ] **次日才送真單**，且首週只開一支策略。

## 五、股票線專屬

- [ ] **融資融券與現股當沖的資格在正式帳戶生效**：模擬環境只驗過送單前的資格判斷；
      正式帳戶要確認已簽當沖與信用交易契約，盤中動能策略的當天停損（現股當沖）實際賣得出去。
- [ ] **先賣後買的現股當沖策略不上線**：盤中段落 13:18 收線，早於當沖回補時點 13:20，現行時窗下回補不會觸發
      （見[實盤執行層](execution-layer.md)〈已知限制〉）。
- [ ] **`__unattributed__`**：正式帳戶若有本系統以外開的部位，第一次啟動會被收進 `__unattributed__`，
      **只允許平倉、而且不算進策略可用的資金**（股票帳戶總權益會扣掉它們）。上線前先決定這些部位要不要清掉。
- [ ] **資金夠用**：帳戶總權益（扣掉 `__unattributed__` 後）× 0.95 ≥ Σ 本行程各策略 `live_capital`，否則啟動即拒絕（退出碼 1）；
      並確認 `live_capital` 與 `live_max_holdings` 成對訂好（每檔資金 ＝ 額度 ÷ 檔數，決定能買的股價上限）。
- [ ] **盤中零股不支援**：下游（金額、成本、部位、歸屬帳、對帳）全以張計，策略不可宣告零股。
- [ ] **盤中段落的標的池**：`IntradayMomentumStrategy` 盤前篩出的標的數 ≤ 190（`LIVE_UNIVERSE_SIZE`；逐筆訂閱上限 200，
      留 10 檔給持有標的）；啟動 log 有「盤前篩選出 N 檔標的」，且盤中段落沒有因訂閱超過上限而中止。
- [ ] **策略名稱與紀錄庫一致**：策略改過名的話，紀錄庫已用 `scripts/manual/manual_rename_live_strategy.py --apply`
      遷移（各表筆數不變、無舊名殘留），否則舊名的部位會在對帳時變成無人認領。

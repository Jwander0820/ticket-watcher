# 規格依據與第一版實作

本專案依引用對話「命名 GitHub 儲存庫」中的完整 v0.1 規格，以及 v0.1.1、v0.1.2 的對話補充實作。對話內 `sandbox:/mnt/data/` 下載附件未能從此工作區取回，因此本文件是實作整理，**不是下載附件的逐字副本**。

核心需求：低資源、後期蹲釋票、一般隨機 300～900 秒、釋票後 60～180 秒、單一穩定出口；Python 核心供 CLI、VPS、外部排程與 AI 共用，MCP 可日後另做薄包裝。

| 模組 | 第一版實作 |
| --- | --- |
| 資料來源 | 已驗證公開 HTTP API，支援場次粒度，公開與內部 ID 轉換 |
| 單次操作 | capabilities、query、status、events、check、tick、health、resume |
| 常駐 | run；共用 HTTP client；本機每 5 秒檢查排程，未到期不查網站 |
| 狀態 | UNKNOWN／UPCOMING／AVAILABLE／SOLD_OUT／PAUSED／ENDED；本次觀測與最後有效值分開 |
| 釋票 | 同一穩定項目的最後有效 SOLD_OUT → AVAILABLE；首次有票不報；同輪合併通知 |
| 快速模式 | 30 分鐘；再釋票延長；持續有票不延長；兩次完整無票退出 |
| SQLite | 票況、排程、事件、outbox、平台節流與租約；UTC 時間；重啟恢復 |
| 異常 | 網路 15／30／60 分鐘退避，429 平台共用等待，403 暫停，解析三次暫停 |
| Discord | 環境變數 webhook、wait=true、訊息 ID、禁用 mentions、獨立重試、TTL 10 分鐘 |
| 保留與健康 | 30 天事件歷史，保留未完成工作；heartbeat 與觀測能力分開 |
| 部署 | Docker Compose、named volume、非 root、唯讀設定、無入站 port、日誌輪替 |
| AI 使用 | JSON 摘要／分頁、query 無比較、CACHE 原始時間、PENDING 與 SENT 分開 |

`query` 不建立事件、不發通知、不覆蓋監控基準，但會保存共用平台限流與元資料快取。查詢取得有票只能稱當下可購票，不能宣稱剛釋票。status／events 不查外部網站。

目前明確限制：票區與逐席資料、價格篩選、MCP、Discord Bot、多平台、Web UI 尚未實作；不加瀏覽器 fallback 或自動 IP 輪換。Docker 部署檔案已提供，不代表已在 VPS 部署或驗證實際 Discord 送達。

資料來源和設定基準改變時重新建立初始狀態。單次 query 與常駐程序必須使用同一 DB 才能共用限流。跨網路主機不自動同步 SQLite。

通知更新與事件入列使用同一 SQLite transaction。跨系統發送只能盡量去重：Discord 收到後若成功回應遺失，重試仍可能重複；訊息中保留穩定事件 ID。後續已確認不可購買的待送項目會取消或從訊息中移除，過期通知不補發。

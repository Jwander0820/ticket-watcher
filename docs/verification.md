# 第一版驗證紀錄

日期：2026-10-05，Asia/Taipei。程式版本：0.1.0。

| 檢查 | 結果 |
| --- | --- |
| Windows Python 3.12.14 測試 | 79 passed；固定樣本／模擬 HTTP |
| Ruff 靜態檢查與格式 | 通過 |
| CLI 入口 | `ticket-watcher capabilities --json` 成功 |
| 套件建置 | wheel 與 source distribution 成功，產物在 `dist/` |
| Compose 設定 | `docker compose config --quiet` 通過 |
| Linux Docker 建置 | `ticket-watcher:local` 成功 |
| 容器執行 | 禁網路、唯讀、非 root 的臨時容器成功執行 capabilities 與 tick |
| 容器 SQLite | `/app/data` 可寫 tmpfs 下，tick 寫入本機 heartbeat 並回報健康 |
| 真實公開票況查詢 | 完成三次唯讀 GET，取得一個場次的明確 `SOLD_OUT` |

真實查詢觀測時間：2026-10-05 23:03:19（台北）。[公開活動](https://ticketplus.com.tw/activity/190c8cf3965a985d9151d912313e2fe1)，回傳場次 `s000001778`，`complete: true`、`granularity: SESSION`、`release_detected: null`、`notification.status: NOT_APPLICABLE`。這是測試當下快照，不代表現在票況。

測試重點包括：首次有票不誤報、最後有效售完經 UNKNOWN 後釋票、持續有票去重、再次釋票、快速模式退出、完整樣本後分頁、共享節流與租約、429 期限重啟保留、解析暫停、狀態／事件／outbox 同交易回滾、待送項目取消、取消項目不在舊事件復活、六次送信上限、失去成功回應時保留同事件 ID、DB 遷移與本機健康資訊。

Windows 受限沙箱阻擋 pytest 預設暫存目錄與 asyncio 初始化。驗證改在正常本機環境執行，pytest 資料固定留在專案內，完成後移除；這項環境限制未透過修改程式行為解決。

仍未驗證：VPS 出口的實際存取、真實釋票事件、實際 Discord webhook 送達。未啟動常駐監控、部署到 VPS 或發布套件。來源目前僅支援場次層級，詳見 [ticketplus-source.md](ticketplus-source.md)。

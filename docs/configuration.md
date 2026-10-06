# 監控設定

[文件索引](README.md) · [CLI 使用](cli.md) · [控制台操作](ui.md)

## 建立設定檔

使用 UI 時由控制台管理設定。使用純 CLI 時，從 [config.example.yaml](../config.example.yaml) 複製一份私有設定：

```powershell
Copy-Item config.example.yaml config.yaml
```

Linux 使用 `cp config.example.yaml config.yaml`。填入真實 TicketPlus 網址，再將需要的目標改為 `enabled: true`；`run` 至少需要一個啟用目標。範例刻意停用，但手寫目標若省略 `enabled`，程式預設為 `true`。

CLI 預設讀取目前目錄的 `config.yaml`，或以 `--config <檔案>` 指定。`app.database_path` 相對於 **YAML 所在目錄** 解析，不是命令執行目錄。例如根目錄 `config.yaml` 的 `data/watcher.db` 指向專案的資料目錄；`data/ui-config.yaml` 則應使用 `watcher.db` 才會指向相同位置。

CLI 常駐程序需重啟才會套用外部設定變更。UI 儲存會自動重載，但會重寫 YAML 並移除註解；外部手動編輯 UI 設定仍需重啟 UI。

## 目標與篩選

| 欄位 | 用途與預設 |
| --- | --- |
| `id` | 唯一目標 ID，1～80 個英數字、底線或連字號 |
| `name` | 顯示名稱，省略時使用 ID |
| `url` | 公開 HTTPS `activity/<活動ID>` 或 `order/<活動ID>/<場次ID>` 網址，不含 query／fragment |
| `platform` / `source` | 目前僅 `ticketplus`；來源可用 `auto` 或 `api` |
| `enabled` | 是否啟用；設定範本為 `false`，省略時為 `true` |
| `channel_id` | 通知目的地，預設 `default`，或填已建立的命名頻道 ID |
| `session_ids` | 指定場次；空陣列代表該 URL 範圍內所有公開場次 |
| `item_ids` | `order` URL 的票區／票種 ID；空陣列代表全部公開項目 |
| `auto_stop` | 預設 `true`，依演出開始時間停止 |
| `stop_at` | 額外停止期限，預設未設定，必須包含時區 |

`activity` URL 監控場次外頁；`order` URL 已限定一場，自動選擇該場的票區（AREA）或票種（PRODUCT）來源。`session_ids` 可使用 API 的 `s000001778` 或公開場次 ID。

只有外頁網址時，先以 `query --detail full` 取得每場的 `order_url`。公開 API 可以取得售完場次的內頁資料，不需先點入網站或登入。

票區篩選填 `a000...` ID，票種填 `p000...` ID，實際值從完整查詢結果取得。`activity` 不接受 `item_ids`。程式會先以完整靜態清單驗證 ID，再查所有選定項目的動態票況；未指定時查全部公開項目。輸出的 `limit`／`offset` 只影響顯示，不縮減評估範圍，缺失資料仍視為未知或解析失敗。

完整案例見 [範例設定](../examples/ticketplus-cases.yaml)；這些目標預設全部關閉，票況紀錄見 [歷史實測](ticketplus-cases.md)。

## 輪詢與 HTTP

| YAML 欄位 | 預設 | 規則 |
| --- | --- | --- |
| `polling.normal_interval_seconds` | `[300, 900]` | 隨機一般間隔，下限至少 300 秒 |
| `polling.active_interval_seconds` | `[60, 180]` | 隨機快速間隔，下限至少 60 秒 |
| `polling.active_window_seconds` | `1800` | 快速模式維持 30 分鐘 |
| `polling.exit_active_after_no_available_checks` | `2` | 連續完整確認無票的退出次數 |
| `http.platform_concurrency` | `1` | 目前必須為 1 |
| `http.min_request_gap_seconds` | `5` | 同平台每個 HTTP 請求至少間隔 5 秒 |
| `http.timeout_seconds` | `20` | 請求逾時秒數 |
| `http.backoff_seconds` | `[900, 1800, 3600]` | 失敗退避，另有 0～30 秒隨機延遲；伺服器期限較長時優先遵守 |

釋票或外頁線索會進入快速模式；再次釋票可延長，持續相同有票狀態不延長。查詢失敗不等於售完，也不計入完整無票次數。超過 45 分鐘無有效觀測時，後續釋票通知會標示監控空窗。

AI、CLI 與 VPS 只有共用同一 SQLite 才能共用平台租約與節流；不同主機的資料庫不會自動同步。`check --now` 也不會略過請求間隔、暫停或錯誤退避，詳見 [CLI 指南](cli.md)。

## 演出開始後自動停止

首次監控取得 TicketPlus 場次日期與時間後，以台灣時區解析演出開始時間，並將停止期限存入 SQLite。重啟後仍遵守期限，不需要修改 YAML 的 `enabled`。

- `order` URL：依該場次開始時間停止。
- `activity` URL：依選定場次判定，已開始的場次不再產生釋票／線索通知，其餘場次繼續監測；全部開始後停止查詢，外頁仍使用整批票況 API。
- 若任一選定場次缺少可靠時間，繼續監測時間不明的場次，不推算演出長度或擅自停止整個目標。

已開始場次的未知或缺失票況不會累加其他場次的解析失敗；仍在監控的場次資料不完整時，維持原有退避與暫停保護。單次 `query` 不受監控停止期限限制，仍回報來源整體完整性。

手動「查詢一次」也遵守停止期限；尚未送出的通知會取消或移除已開始場次。`stop_at` 與自動期限取較早者，例如：

```yaml
auto_stop: true
stop_at: '2026-11-29T18:00:00+08:00'
```

`stop_at` 省略、`null` 或空白字串代表未設定；不接受布林、數字、陣列、物件或沒有時區的時間。

關閉自動停止不會清除票況基準。活動改期且監控已停止時，可先關閉自動停止，再查詢一次更新來源時間，確認後重新啟用自動停止；若另有已到期的 `stop_at`，也需調整。來源名稱／場次資料最多快取一小時。

## 通知與環境變數

| 設定 | 預設與用途 |
| --- | --- |
| `notifications.webhook_url_env` | `DISCORD_WEBHOOK_URL`，預設頻道的環境變數名稱 |
| `notifications.system_alerts_enabled` | `true`，查詢異常與恢復通知 |
| `notifications.worker_alerts_enabled` | `true`，UI 監控工作中止／穩定恢復通知 |
| `notifications.worker_alert_channel_id` | `default`，UI 服務告警目的地 |
| `notifications.delivery_ttl_seconds` | `600`，通知有效期限 10 分鐘 |
| `notifications.retry_delays_seconds` | `[10, 30, 60, 120, 300]`，失敗重試間隔 |
| `app.retention_days` | `30`，事件歷史保留天數；保留未完成工作 |

`channels` 只存頻道的 `id` 與 `name`；Webhook 由 [控制台](ui.md#設定-discord-頻道) 存入資料庫旁的私有 JSON，不填進 YAML。未設定 Webhook 時，事件留在待送佇列，超過 TTL 就過期。

直接執行 CLI 時，設定程序環境：

```powershell
$env:DISCORD_WEBHOOK_URL = '<指定頻道的 Webhook URL>'
```

Linux 對應為 `export DISCORD_WEBHOOK_URL='<指定頻道的 Webhook URL>'`。CLI 不自動載入 `.env`；Docker Compose 使用 [.env.example](../.env.example) 複製出的 `.env` 注入環境。UI 儲存的預設 Webhook 優先於環境值，清除 UI 覆寫才恢復環境值。

Compose 的 `TICKET_WATCHER_PUBLIC_ORIGIN` 與容器資源變數見 [部署指南](vps-migration.md)。完整 YAML 以 [設定範本](../config.example.yaml) 為起點；不支援的欄位會在載入時報錯。

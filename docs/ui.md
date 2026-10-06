# 控制台指南

[文件索引](README.md) · [監控設定](configuration.md) · [疑難排解](operations.md)

## 啟動與停止

從專案根目錄啟動 Docker UI：

```sh
docker compose -f compose.ui.yaml up -d --build
```

開啟 [http://localhost:8787](http://localhost:8787)。不需要先建立 `config.yaml` 或 `.env`；新資料 volume 會建立空白設定，新增目標並啟用後才開始監控。

不使用 Docker 時，先完成 [Python 安裝](../README.md#使用-cli-單次查詢)，再執行：

```powershell
.\.venv\Scripts\ticket-watcher.exe ui
# 使用既有 YAML：
.\.venv\Scripts\ticket-watcher.exe --config config.yaml ui
```

Python UI 預設建立 `data/ui-config.yaml`，資料庫為同目錄的 `watcher.db`。UI 程序包含常駐監控，不需另啟動 `run`，也不要讓另一個程序同時管理同一份設定／資料庫。關閉網頁不會停止監控；Python 前景程序可用 Ctrl+C 停止，Docker 則使用：

```sh
docker compose -f compose.ui.yaml down
```

這會保留資料 volume。遠端使用與搬移請見 [部署指南](vps-migration.md)。

## 暫停運作，保留 UI

頂端「暫停運作」會停止目前這個 UI 服務的自動查票、通知發送／重試，以及手動查票和測試通知，並取消進行中的工作。頁面、設定編輯、歷史紀錄及本機健康心跳仍可使用；不會連動其他主機上的服務。

暫停狀態寫入 `app.ui_paused`，儲存其他設定或重啟容器後仍保持暫停。各目標的啟用狀態、票況基準、平台限流期限與待送通知都保留。按「恢復運作」後，已到期的查詢及仍有效的待送通知可能立即執行；過期通知仍依原本 TTL 處理。暫停前已送到平台或 Discord 的請求無法撤回。

建議讓 VPS 執行正式監控，本機保持暫停以開發 UI。離線測試不需要開啟容器。這個按鈕只控制目前的 UI 程序；另外啟動的 CLI 程序不受影響，請勿讓它們同時使用這份資料庫。

## 設定 Discord 頻道

1. 在 Discord「伺服器設定 → 整合 → Webhook」建立 Webhook，選擇接收通知的文字頻道並複製網址。帳號需有管理 Webhook 權限，詳見 [官方說明](https://support.discord.com/hc/en-us/articles/228383668-Intro-to-Webhooks)。
2. 到控制台「Discord 頻道 → 預設頻道 → 編輯」貼上網址；也可「新增頻道」設定其他目的地。
3. 編輯監控，在「通知到哪個頻道」選擇目的地。儲存頻道本身不傳送訊息；按「傳送測試」才會嘗試送出測試通知。

預設頻道優先使用 UI 儲存的 Webhook，未設定時沿用程序環境的 `DISCORD_WEBHOOK_URL`。編輯時留白保留原值；「清除 UI 設定」移除覆寫，恢復使用環境變數。CLI 不自動讀 `.env`，Docker Compose 會將其注入容器。

Webhook 存在資料庫旁的 `discord-webhooks.json`，是本機明文憑證檔；頁面不回傳完整網址。它不會寫入 YAML、SQLite 或查詢日誌，已排除 Git 與 Docker 建置內容，備份時仍需保護。

## 新增與調整監控

填入活動外頁 `activity` 或購票內頁 `order` URL，依需要選擇場次、票區／票種、通知頻道與停止時間。新監控預設停用，勾選「儲存後啟用監控」才開始查票。篩選規則及演出自動停止見 [設定指南](configuration.md)。

UI 儲存會重新載入設定，保留排程與平台等待期限。不同變更的影響如下：

| 變更 | 影響 |
| --- | --- |
| URL、來源或篩選 | 建立新票況基準，不跨基準判定釋票 |
| 通知頻道 | 保留票況基準，取消尚未送出的舊頻道通知 |
| 自動停止開關 | 保留票況基準 |
| 在外部直接編輯 YAML | 重啟 UI 後生效 |

切回原頻道不會恢復已取消通知；已送出的 HTTP 請求無法撤回。UI 儲存會重寫 YAML，原有註解不保留。

## 「查詢一次」與即時顯示

「查詢一次」立即嘗試檢測，略過目標例行排程，但仍遵守平台限流、正在進行的查詢、暫停、錯誤退避、停用及停止期限。它會更新監控基準，符合釋票條件時會通知。只想查票且不通知，請用 CLI [`query`](cli.md)。

手動查詢完成後先返回票況，通知由背景佇列送出，因此初始結果可能顯示待送。查看事件中的送達狀態後，才能確認 Discord 是否收到。

控制台每 30 秒更新目前頁面，內容未變更時不重畫清單或表單；分頁隱藏時停止定時更新，切回時立即更新。事件與查詢日誌只在對應頁面載入，切頁與操作後會刷新。查票排程獨立運作，不受瀏覽器是否開啟影響。

## 監控服務異常通知

在「輪詢設定 → 監控服務異常通知」可開關通知並指定接收頻道；需要專用 Webhook 時，先在「Discord 頻道」新增頻道。預設啟用並使用預設頻道，未設定 Webhook 就無法送達。

監控工作意外中止時，UI 會自動重建工作；持續故障只建立一則異常事件，恢復且持續運作 60 秒後才建立恢復事件。完整重試、設定回復與故障處理見 [維運指南](operations.md)。

## 資料位置

Docker 使用獨立的 `watcher-ui-data` volume，掛載於 `/app/data`，包含設定、SQLite、私有 Webhook 與查詢日誌；純 CLI Compose 使用另一個 `watcher-data` volume，兩者不自動同步。

備份與還原應搬移完整資料目錄，操作步驟見 [本機 UI 搬移到 VPS](vps-migration.md)。日誌檔案與保留期限見 [查詢紀錄與保留](operations.md#查詢紀錄與保留)。

# Ticket Watcher

低資源的 TicketPlus 查票與釋票通知工具。Python 核心可供 CLI、VPS 常駐程序與外部排程共用；查詢和通知流程不需要 AI 或瀏覽器。

支援公開活動外頁的場次票況，以及購票內頁的票區／票種資料。外頁 `售完 → 暫無票券` 建立獨立的釋票線索通知；無票轉有票才建立確認有票事件。首次觀測、持續相同狀態不通知。程式不登入、不購票、不占位。

## 安裝與單次查詢

需要 Python 3.12 以上：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\ticket-watcher.exe capabilities --json
.\.venv\Scripts\ticket-watcher.exe query --url "https://ticketplus.com.tw/activity/<活動ID>" --detail full --json
.\.venv\Scripts\ticket-watcher.exe query --url "https://ticketplus.com.tw/order/<活動ID>/<場次ID>" --detail full --json
```

Linux 可使用 `.venv/bin/python` 與 `.venv/bin/ticket-watcher`。也可安裝後執行 `python -m ticket_watcher`。

`query` 不建立監控、事件或通知，也不覆蓋既有監控基準；共用 SQLite 僅保存平台節流與一小時的活動／場次名稱快取。票況每次查 API，不使用靜態資訊當即時庫存。

## 設定監控

### 本機 UI

使用 Docker 啟動控制台：

```powershell
docker compose -f compose.ui.yaml up -d --build
```

開啟 [http://localhost:8787](http://localhost:8787)，即可新增／編輯監控、啟用／停用目標、選擇每場的 Discord 頻道、調整輪詢間隔並查看最近事件。新監控預設停用，勾選「儲存後啟用監控」才開始查票。介面提供場次／票區篩選與停止時間；「查詢一次」立即嘗試檢測，略過該目標的例行排程，但保留平台限流、正在進行的查詢、暫停與錯誤退避。手動查詢同樣更新票況基準，符合釋票條件時會依既有設定通知。

Discord 設定步驟：

1. 在 Discord「伺服器設定 → 整合 → Webhook」建立 Webhook，選擇接收通知的文字頻道，再複製網址。需有管理 Webhook 權限，詳見 [Discord 官方說明](https://support.discord.com/hc/en-us/articles/228383668-Intro-to-Webhooks)。
2. 到控制台「Discord 頻道 → 新增頻道」，填入自訂名稱與 Webhook 網址。每個目的頻道分別新增。
3. 編輯監控，在「通知到哪個頻道」選擇目的地。可按頻道的「傳送測試」確認送達；儲存頻道本身不傳送訊息。

UI 儲存會自動重新載入監控設定，保留既有票況、排程與平台等待期限。修改網址或篩選會建立新基準；單純更換通知頻道保留票況基準，尚未送出的舊頻道通知會取消。輪詢最低值維持一般 300 秒、快速 60 秒、每個 HTTP 請求間隔 5 秒。

此 Compose 僅發布本機 `127.0.0.1:8787`，以獨立 `watcher-ui-data` volume 保存設定、SQLite 與私有 Webhook 檔案。`discord-webhooks.json` 位於資料庫旁，是本機明文憑證檔，已排除 Git 與 Docker 建置內容；頁面不回傳儲存的完整網址。備份 volume 時需一併保護這個檔案。預設頻道仍可沿用 `.env` 的 `DISCORD_WEBHOOK_URL`。

不使用 Docker 時，安裝後執行：

```powershell
.\.venv\Scripts\ticket-watcher.exe ui
# 或使用既有設定檔（UI 儲存時會重寫 YAML，註解不保留）：
.\.venv\Scripts\ticket-watcher.exe --config config.yaml ui
```

預設建立 `data/ui-config.yaml` 與同目錄資料庫。UI 程序已包含常駐監控，不需另外執行 `run`，也不要讓 UI 與其他 `run` 程序同時管理同一份設定／資料庫。手動在外部修改設定檔後需重啟 UI。停止 Docker UI 可執行 `docker compose -f compose.ui.yaml down`，資料 volume 會保留。

### 查詢紀錄

UI 的「查詢紀錄」顯示最近 100 筆檢測：時間、目標、手動／自動、成功／不完整／失敗、票況摘要、請求數、耗時及下次排程。從此功能啟用後開始記錄，不會補造過去查詢。手動操作未能執行時會記錄原因；每 5 秒檢查排程但沒有查票的等待不會洗版。

完整紀錄是資料庫旁 `<資料庫名稱>-logs/` 下的 `queries.log` 與 `queries.previous.log`，每行一筆 JSON。以第一次建立紀錄週期起每 7 天輪替，僅保留目前與上一個週期，最長 14 天。輪替時間寫入 SQLite，重啟不重算；常駐服務即使沒有新查詢也會清理舊週期，停機期間則於下次啟動／讀取時清理。UI Docker 的預設路徑為 `/app/data/watcher-logs/`。

查詢紀錄只保存操作摘要與目標 ID，不保存 Webhook、完整 URL、頻道名稱、HTTP 回應或例外原文。檔案寫入失敗會在 UI 顯示原因，監控本身繼續運作。這份 14 天紀錄與既有 30 天票況事件／通知紀錄分開；Docker 的程序日誌仍受 `5m × 3` 大小上限管理。

### CLI 設定

複製 `config.example.yaml` 為 `config.yaml`，填入 URL，將目標改成 `enabled: true`。`activity` URL 監控場次外頁；`order` URL 自動使用該場的票區或票種來源。`session_ids` 可填 API 的 `s000001778` 或公開場次 ID；空陣列代表所有公開場次，購票 URL 已限定一場。

`order` URL 可透過 `item_ids` 篩選 `a000...` 票區或 `p000...` 票種；空陣列代表該場全部公開項目。`activity` URL 不接受 `item_ids`，避免暗中忽略票區篩選。只有外頁網址時，先用 `query --detail full` 取得每場的 `order_url`。公開 API 可以取得售完場次的內頁資料，無須先點入網站或登入。

YUURI 外頁與 10/9、10/10 內頁的設定已放在 [案例設定](examples/ticketplus-cases.yaml)，預設全部關閉。查詢結果與操作範例見 [案例實測](docs/ticketplus-cases.md)。內頁只回報票區／票種名稱，不取得逐席位置。

```powershell
$env:DISCORD_WEBHOOK_URL = '<指定頻道的 webhook URL>'
.\.venv\Scripts\ticket-watcher.exe --config config.yaml check --target my-event --json
.\.venv\Scripts\ticket-watcher.exe --config config.yaml status --target my-event --detail full --json
.\.venv\Scripts\ticket-watcher.exe --config config.yaml events --target my-event --json
.\.venv\Scripts\ticket-watcher.exe --config config.yaml tick --json
.\.venv\Scripts\ticket-watcher.exe --config config.yaml run
```

CLI 直接讀程序環境，不自動載入 `.env`；Compose 會讀取 `.env`。Webhook 不放 YAML、資料庫或日誌。未設定 webhook 時，事件留在待送佇列，超過 10 分鐘便過期。

一般間隔為隨機 300～900 秒；偵測到釋票或外頁釋票線索後，該目標改成 60～180 秒，最多維持 30 分鐘。連續兩次完整確認無票也會退出快速模式。查詢失敗不代表售完；中間超過 45 分鐘無有效觀測，通知會標示監控空窗。這些間隔沿用原規格，未採用手動刷頁的 1～3 秒頻率，因此仍可能錯過短暫有票。

同一資料庫的 AI、CLI 與 VPS 共用平台租約與節流，每個請求至少隔 5 秒。`check` 預設遵守排程，尚未到期回傳 `DEFERRED`；明確加上 `check --target <ID> --now` 才會像 UI 的「查詢一次」略過例行排程。`--now` 不略過限流、平台／目標暫停或失敗後的退避，也不啟用已停用或到期的目標。

429 會保存平台共用等待期限；401／403／驗證頁會暫停平台；連續三次解析異常會暫停目標。確認存取問題已處理後，手動執行：

```text
ticket-watcher --config config.yaml resume --platform ticketplus --json
ticket-watcher --config config.yaml resume --target my-event --json
```

`resume` 不清除伺服器等待期限與原有排程。修改來源、URL 或篩選會建立新基準，不跨基準判定釋票。CLI 常駐程序的設定變更需重啟生效；UI 儲存會自動套用。

`run` 的查票、通知重試與 heartbeat 分開執行，Discord 等待不會阻塞下一輪查票。`tick` 同樣讓通知與本輪查票並行，結束前等待通知批次完成；`check` 保留單次查票後立即嘗試通知的行為。完整成功的 `query` 會重設平台失敗累計，但不清除伺服器等待期限。

新通知保存建立時的目標設定簽章，每次送信嘗試都有獨立領取識別；已取消或由其他程序接手的工作不會被舊請求回應覆寫。已送往 Discord 的 HTTP 請求無法撤回，成功回應遺失仍可能造成重複通知。

## VPS / Docker

已透過 UI 設定的頻道、活動與基準可隨資料 volume 搬移，不必逐項重設。Git 只帶程式與範例，私有資料需另行傳送。操作步驟見 [本機 UI 搬移到 VPS](docs/vps-migration.md)。

```sh
cp config.example.yaml config.yaml
cp .env.example .env
# 編輯 config.yaml 與 .env 後：
docker compose up -d --build
docker compose logs --tail 100 -f
docker compose exec ticket-watcher ticket-watcher --config /app/config.yaml status --json
```

Compose 使用 named volume 保存 SQLite，不開入站 port，設定唯讀掛載，日誌自動輪替。範例的 `data/watcher.db` 會相對於容器內 `/app/config.yaml` 解析成 `/app/data/watcher.db`，也可直接設定該絕對路徑。`docker compose exec` 與常駐程序會讀取同一份資料庫。宿主機 CLI 必須透過容器執行才能共用該狀態；另一台電腦的獨立 SQLite 不會自動同步。

`health` 只讀本機資料，回報程序 heartbeat 與各目標最近成功查詢的時間；`run`／`tick` 執行期間每 30 秒獨立更新 heartbeat，長查詢或通知等待不會讓它停止更新。平台被限制時不因此將 heartbeat 判為失敗。初始化活動查詢需三個 HTTP 請求，內頁通常需四個；超過 100 個內頁項目時分批完整查詢。健康檢查有 60 秒啟動緩衝。

## JSON 與 Python 使用

所有 CLI 操作預設輸出 JSON，日誌送 stderr。摘要是預設，`--detail full --limit 50 --offset 0` 可分頁取得項目；程式先解析和比較完整樣本，最後才縮減輸出。`events` 預設只提供事件摘要，加入 `--detail full` 才回傳完整變更內容。

`status` 的摘要由 SQLite 統計全部項目，明細只讀指定分頁。30 天歷史資料清理每小時最多執行一次，由共用資料庫記錄執行時間；通知過期仍在通知佇列處理時即時檢查，不會延後一小時。

退出碼：`0` 成功、`2` 失敗、`3` 延後執行、`4` 不支援、`130` 人工中止。`tick` 的退出碼代表該輪完成，個別目標結果列在 `checks`；未送達通知不能解讀為 `SENT`。

```python
import asyncio
from ticket_watcher.config import load_config
from ticket_watcher.service import Watcher


async def main():
    async with Watcher(load_config("config.yaml")) as watcher:
        print(watcher.status("my-event").to_dict())
        result = await watcher.query("https://ticketplus.com.tw/activity/<活動ID>")
        print(result.to_dict())


asyncio.run(main())
```

詳見 [AI 操作指引](docs/ai-usage.md)、[工具契約](skills/ticket-watcher/references/tool-contract.md)、[資料來源驗證](docs/ticketplus-source.md)、[規格與實作範圍](docs/spec.md) 與 [第一版驗證紀錄](docs/verification.md)。

## 開發驗證

```sh
python -m pip install -e '.[dev]'
pytest -q
ruff check .
ruff format --check .
python -m build
```

測試使用固定樣本及模擬 HTTP，不對 TicketPlus 反覆輪詢或傳送真實 Discord 訊息。Docker 及 VPS 上的實際存取條件須在部署環境再確認。MCP、Discord 指令機器人、其他售票平台與逐席來源留待後續擴充。

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

同一資料庫的 AI、CLI 與 VPS 共用平台租約與節流，每個請求至少隔 5 秒。`check` 也遵守排程，尚未到期會回傳 `DEFERRED`；沒有強制略過限流的選項。

429 會保存平台共用等待期限；401／403／驗證頁會暫停平台；連續三次解析異常會暫停目標。確認存取問題已處理後，手動執行：

```text
ticket-watcher --config config.yaml resume --platform ticketplus --json
ticket-watcher --config config.yaml resume --target my-event --json
```

`resume` 不清除伺服器等待期限與原有排程。修改來源、URL 或篩選會建立新基準，不跨基準判定釋票。設定變更重啟生效。

## VPS / Docker

```sh
cp config.example.yaml config.yaml
cp .env.example .env
# 編輯 config.yaml 與 .env 後：
docker compose up -d --build
docker compose logs --tail 100 -f
docker compose exec ticket-watcher ticket-watcher --config /app/config.yaml status --json
```

Compose 使用 named volume 保存 SQLite，不開入站 port，設定唯讀掛載，日誌自動輪替。範例的 `data/watcher.db` 會相對於容器內 `/app/config.yaml` 解析成 `/app/data/watcher.db`，也可直接設定該絕對路徑。`docker compose exec` 與常駐程序會讀取同一份資料庫。宿主機 CLI 必須透過容器執行才能共用該狀態；另一台電腦的獨立 SQLite 不會自動同步。

`health` 只讀本機資料，回報程序 heartbeat 與各目標最近成功查詢的時間；平台被限制時不因此將 heartbeat 判為失敗。初始化活動查詢需三個 HTTP 請求，內頁通常需四個；超過 100 個內頁項目時分批完整查詢。健康檢查有 60 秒啟動緩衝。

## JSON 與 Python 使用

所有 CLI 操作預設輸出 JSON，日誌送 stderr。摘要是預設，`--detail full --limit 50 --offset 0` 可分頁取得項目；程式先解析和比較完整樣本，最後才縮減輸出。`events` 預設只提供事件摘要，加入 `--detail full` 才回傳完整變更內容。

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

# Ticket Watcher

低資源的 TicketPlus 查票與釋票通知工具，提供本機控制台、CLI 與 Python 共用核心，可在 VPS 常駐執行。查票與通知不需要 AI 或瀏覽器，程式不登入、不排隊、不占位、不購票。

- **公開票況**：活動外頁查場次，購票內頁查票區／票種；支援指定場次與項目。
- **Discord 通知**：區分確認有票的 `RELEASE` 與外頁釋票線索 `RELEASE_HINT`，可為每個目標指定頻道。
- **持續監控**：SQLite 保存票況基準、排程與待送通知，支援演出開始後自動停止及 UI 監控工作恢復。

首次觀測只建立基準，不報釋票。一般輪詢預設 300～900 秒，偵測到釋票或線索後改為 60～180 秒；短暫有票仍可能在兩次觀測之間消失。

## 快速開始

以下命令從專案根目錄執行。選擇控制台或 CLI 其中一種方式即可。

### 使用控制台

需要 Docker 與 Docker Compose：

```sh
docker compose -f compose.ui.yaml up -d --build
```

開啟 [http://localhost:8787](http://localhost:8787)：

1. 在「Discord 頻道」設定 Webhook，選擇接收通知的頻道。
2. 新增監控，填入 TicketPlus 的 `activity` 或 `order` 網址。
3. 確認篩選與停止條件後，勾選「儲存後啟用監控」。新監控預設停用。

控制台已包含常駐監控，關閉瀏覽器不會停止查票。頂端「暫停運作」可停止此服務的查票與通知，保留 UI 編輯設定；暫停狀態在儲存設定及重啟後仍保留，按「恢復運作」才繼續。不需要 UI 時，停止服務可執行下列命令，資料 volume 會保留：

```sh
docker compose -f compose.ui.yaml down
```

詳細操作與不用 Docker 的啟動方式見 [控制台指南](docs/ui.md)。已有本機資料要搬到 VPS，請看 [部署與搬移](docs/vps-migration.md)。

### 使用 CLI 單次查詢

需要 Python 3.12 以上。Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\ticket-watcher.exe capabilities --json
.\.venv\Scripts\ticket-watcher.exe query --url "https://ticketplus.com.tw/activity/<活動ID>" --detail full --json
```

將範例網址換成實際活動網址；也接受 `https://ticketplus.com.tw/order/<活動ID>/<場次ID>`。Linux 對應使用 `.venv/bin/python` 與 `.venv/bin/ticket-watcher`；安裝後也能以該環境的 `python -m ticket_watcher` 呼叫。

`query` 取得當下票況，不建立監控、釋票事件或通知。要持續監控，先依 [設定指南](docs/configuration.md) 建立 `config.yaml`，再使用 [CLI 指南](docs/cli.md) 的 `run`；CLI `run` 與 UI 不應同時管理同一份設定／資料庫。

## 先分清楚的結果

| 結果 | 意義 |
| --- | --- |
| `AVAILABLE` | 本次來源回報可購票，不保證成功下單 |
| `RELEASE` | 監控比較到同一項目由無票轉為 `AVAILABLE` |
| `RELEASE_HINT` | 外頁由售完轉為「暫無票券」，尚未確認有票 |
| `UNKNOWN` | 本次資料無法確認；保留的上次有效值不是即時票況 |
| `PENDING` / `SENT` | 通知待送／Discord 已確認送達，兩者不可混用 |

內頁最多提供票區／票種資訊，不提供逐席位置。「熱賣中」不換算成確切庫存。MCP、Discord 指令機器人、其他售票平台與價格篩選尚未實作。

## 文件導覽

| 想做的事 | 文件 |
| --- | --- |
| 新增監控、設定 Discord、手動檢測 | [控制台指南](docs/ui.md) |
| 編輯 YAML、選擇票區、設定自動停止 | [設定指南](docs/configuration.md) |
| 使用 CLI、JSON 或 Python | [CLI 與程式整合](docs/cli.md) |
| 排查沒查票、沒通知、查日誌與健康狀態 | [維運與疑難排解](docs/operations.md) |
| 使用 Docker、搬移資料、遠端存取 | [部署與 VPS 搬移](docs/vps-migration.md) |
| 安裝開發依賴、執行測試、了解程式結構 | [開發指南](docs/development.md) |
| 查閱 AI 操作、來源對照與歷史驗證 | [完整文件索引](docs/README.md) |

設定範本：[config.example.yaml](config.example.yaml)、[.env.example](.env.example)。公開案例：[範例設定](examples/ticketplus-cases.yaml) 與 [歷史實測](docs/ticketplus-cases.md)，案例預設全部停用。

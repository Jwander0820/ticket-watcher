# Docker 部署與 VPS 搬移

[文件索引](README.md) · [控制台操作](ui.md) · [維運與疑難排解](operations.md)

正式部署前先參考 [VPS 上線準備與 DNS 設計](vps-launch-plan.md)，確認入口名稱、Tunnel 拓撲、Access 與切換驗收。

VPS 查票需要指定 Cloudflare WARP 出口時，依 [WARP 查票出口](warp-proxy.md) 設定；只有 TicketPlus API 使用代理，控制台入口與 Discord 維持原本連線。

## 選擇執行方式

兩份 Compose 使用同一套程式，但管理方式與資料 volume 不同。只啟動需要的服務，避免重複監控相同目標。

| 項目 | UI 控制台 | 純 CLI 常駐 |
| --- | --- | --- |
| Compose | `compose.ui.yaml` | `compose.yaml`（預設） |
| 服務 | `ticket-watcher-ui` | `ticket-watcher` |
| 設定 | 容器 `/app/data/ui-config.yaml`，由 UI 管理 | 宿主機 `config.yaml` 唯讀掛至 `/app/config.yaml` |
| 資料 volume | `watcher-ui-data` | `watcher-data` |
| 網路入口 | 宿主機 `127.0.0.1:8787` | 不發布入站 port |
| 健康檢查啟動緩衝 | 30 秒 | 60 秒 |

Compose 預設以非 root、唯讀容器檔案系統執行，可寫資料保存於 volume，程序日誌自動輪替。兩種 volume 不會自動同步；Git 也不包含私有設定或執行資料。

## 首次部署

UI 從專案根目錄執行 `docker compose -f compose.ui.yaml up -d --build`，完整新增監控步驟見 [控制台指南](ui.md)。已有本機 UI 資料時，直接依下節搬移，不需逐項重新建立。

純 CLI 部署先建立設定（以下為 Linux shell）：

```sh
cp config.example.yaml config.yaml
cp .env.example .env
```

編輯 `config.yaml` 的網址，至少啟用一個目標，並在 `.env` 設定 Webhook；欄位說明見 [設定指南](configuration.md)。確認後啟動：

```sh
docker compose up -d --build
docker compose logs --tail 100 -f
docker compose exec ticket-watcher ticket-watcher --config /app/config.yaml health --json
docker compose exec ticket-watcher ticket-watcher --config /app/config.yaml status --json
```

`logs -f` 持續顯示日誌，按 Ctrl+C 離開後再執行後續命令，容器會繼續運作。設定範例的 `data/watcher.db` 相對於容器 `/app/config.yaml` 解析成 `/app/data/watcher.db`，因此容器內 CLI 與常駐程序讀取同一 DB。宿主機的獨立 CLI 不會直接取得 named volume 狀態。

修改掛載的 CLI `config.yaml` 後，以 `docker compose restart ticket-watcher` 重新載入；修改 `.env` 後以 `docker compose up -d` 重新建立需要更新的容器。停止服務使用 `docker compose down`，不加 `--volumes` 會保留資料。

## 本機 UI 資料搬移

UI 新增的 Discord 頻道、監控目標、票況基準和日誌都在 `watcher-ui-data` volume；只 clone Git 不會取得它們。搬移整份資料便不必重新填寫。Git 的排除規則包含執行設定、Webhook、SQLite、查詢日誌與 `backups/`；請勿強制加入 Git 或上傳備份到公開位置。

以下是手動搬移步驟，並未自動執行備份或傳送。需先確定 VPS 的 Docker 與 Compose 已可使用。

### Windows 匯出

從專案根目錄執行，先停止寫入 SQLite，再複製完整資料目錄（包含可能存在的 WAL／SHM）：

```powershell
docker compose -f compose.ui.yaml stop ticket-watcher-ui
New-Item -ItemType Directory -Force -Path backups/ui-data | Out-Null
docker compose -f compose.ui.yaml cp ticket-watcher-ui:/app/data/. backups/ui-data/
```

確認備份包含 `ui-config.yaml`、`watcher.db`，以及已建立的 `discord-webhooks.json` 與 `watcher-logs/`。尚未透過 UI 儲存 Webhook 或尚無查詢紀錄時，對應檔案可能不存在。Webhook 是明文憑證，備份需私下保管；此命令不會顯示其內容。資料目錄備份不包含宿主機 `.env`，若使用環境變數預設頻道，需另行安全搬移 `.env`；UI 儲存的預設頻道與命名頻道均已在 JSON 檔內。

以 SSH/SCP 私下將 `backups/ui-data/` 傳到 VPS 專案的 `backups/ui-data/`。正式切換時保持本機服務停止，避免兩端各自查票與通知。若暫時只做備份，可用 `docker compose -f compose.ui.yaml start ticket-watcher-ui` 恢復本機監控，正式搬移時重新備份。

### VPS 還原

先取得相同或較新的程式版本。在尚未使用、可放入這份資料的新 UI volume 上操作；不要覆蓋已有其他監控資料的 volume。

```sh
docker compose -f compose.ui.yaml build ticket-watcher-ui
docker compose -f compose.ui.yaml create ticket-watcher-ui
docker compose -f compose.ui.yaml cp backups/ui-data/. ticket-watcher-ui:/app/data/
docker compose -f compose.ui.yaml run --rm --no-deps --user 0 --cap-add CHOWN --cap-add DAC_OVERRIDE --entrypoint chown ticket-watcher-ui -R 10001:10001 /app/data
docker compose -f compose.ui.yaml up -d ticket-watcher-ui
docker compose -f compose.ui.yaml exec -T ticket-watcher-ui ticket-watcher --config /app/data/ui-config.yaml health --json
```

`chown` 讓還原後的設定與資料庫仍可由容器既有的非 root 使用者寫入。這裡的 `/app/data` 是明確掛載的資料 volume，不是宿主機根目錄。預設 UI 設定使用相對資料庫路徑；若自行改成其他宿主機路徑，需在啟動前調整。

確認 heartbeat 健康、目標和頻道數量相符，再觀察下一次查詢紀錄。搬移後保留基準、原排程與限流等待；未送達事件也保留，超過 TTL 的通知不補發。更換網路出口後的 TicketPlus 存取是否正常需在 VPS 上實測。

UI 會自動重新建立意外中止的監控工作，程序完全退出時仍依 Compose 的重啟政策處理。搬移後可在「輪詢設定 → 監控服務異常通知」確認告警目的地；重試流程、健康資訊與日誌見 [維運指南](operations.md)。平台保護性暫停不會自動解除。

## 多頻道通知資料庫升級

2026-10-07 新增的多頻道通知將 SQLite 從第 2 版升為第 3 版。舊 VPS 部署入口拒絕所有資料表變更，因此重跑同一個 deploy 仍會失敗；舊容器在預檢失敗時繼續運作。

更新 `scripts/deploy-vps.sh` 與 `scripts/deploy_state.py` 並重新執行部署 installer 後，第 2 → 3 版可自動處理：先核對第 2 版的完整結構，在唯讀來源的 SQLite 副本上演練升級，確認完整性、外鍵與所有票況／通知資料保留；通過才停止服務，保存完整私有備份，執行離線升級並以暫停狀態驗收。成功後恢復原本的暫停狀態。未知結構與其他版本升級仍需人工處理。

驗收尚未恢復監控時，可用升級前 DB 與設定回復舊映像。恢復監控後的失敗不自動還原 DB，以免遺失新送達紀錄而重送 Discord；服務保持停止，需以新結構人工修復。自動升級與副本演練都不會建立查票或通知程序。[SQLite 副本備份](https://www.sqlite.org/backup.html) 會包含已提交的 WAL 資料。

若維持舊入口或遇到不支援的結構，先在維護窗口停用自動部署、停止 UI，依本頁備份流程保存完整 volume、設定及原映像版本，再採以下人工流程。

確認新映像已下載、`TICKET_WATCHER_IMAGE` 指向含多頻道功能的新映像後，可從 VPS 專案根目錄執行一次離線遷移：

```sh
docker compose -f compose.vps.yaml stop ticket-watcher-ui
docker compose -f compose.vps.yaml run --rm --no-deps --entrypoint python ticket-watcher-ui -c "from ticket_watcher.config import load_config; from ticket_watcher.storage import Store; Store(load_config('/app/data/ui-config.yaml').database_path).close()"
```

這個命令只載入設定及升級 SQLite，不建立查票或通知程序。須先完成停止與備份，並確認沒有其他 CLI／容器共用該資料庫；遷移保留票況基準、送達紀錄、重試與租約。之後以新映像重新部署驗收，核對原暫停狀態、目標及頻道選擇。若需回復舊映像，必須先停止服務並還原升級前的完整資料，不能讓舊程式直接讀第 3 版資料庫。

## 遠端查看控制台

### Cloudflare Tunnel + Access

Compose 保持只發布 VPS 的 `127.0.0.1:8787`。以下假設 `cloudflared` 跑在同一台 VPS 宿主機；若它在另一個容器，該容器的 `127.0.0.1` 不是宿主機，需另外安排私有網路連線與 origin 隔離。

1. 為完整網域（例如 `tickets.example.com`，含所有路徑）建立 Access self-hosted application，Allow policy 僅允許自己的帳號，不設 Bypass。
2. Tunnel 的公開網域指向 `http://127.0.0.1:8787`，保留原本的公開 Host；啟用 **Protect with Access**，讓 cloudflared 驗證 Access JWT，並填入此 application 的 team name 與 AUD tag。
3. VPS 專案 `.env` 加入 `TICKET_WATCHER_PUBLIC_ORIGIN=https://tickets.example.com`，再重建／啟動 UI。這是完整 HTTPS origin，可含非預設連接埠，不含子路徑或參數。直接執行 Python 時用 `ui --public-origin https://tickets.example.com` 或同名程序環境變數。

若使用本機管理的 Tunnel，將以下 ingress 加到既有 cloudflared 設定，替換範例值；保留原本的 tunnel ID 與 credentials-file：

```yaml
ingress:
  - hostname: tickets.example.com
    service: http://127.0.0.1:8787
    originRequest:
      access:
        required: true
        teamName: your-team-name
        audTag:
          - your-access-application-aud
  - service: http_status:404
```

`TICKET_WATCHER_PUBLIC_ORIGIN` 是應用程式的 Host／Origin 白名單，不會建立 Access policy 或在 Python 內驗證 JWT；身分驗證由 Access 與 cloudflared 負責。`X-Forwarded-Host`、`X-Forwarded-Proto` 不會自動加入信任範圍。所有設定修改仍需要 CSRF token；本機 loopback 存取保持可用，因此 VPS 本機程序屬於信任範圍。

正式啟用前，確認未登入和非允許帳號都無法讀取 `/api/state`，自己的帳號登入後可以載入及儲存設定，VPS 公網 IP 的 8787 埠不可直接連線。這些檢查需在自己的 Cloudflare 與 VPS 環境完成。

依據：[Cloudflare origin Access 驗證參數](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/origin-parameters/#access)。

### SSH tunnel

不使用公開網域時，保留空白的 `TICKET_WATCHER_PUBLIC_ORIGIN`，在自己的電腦建立 SSH tunnel：

```sh
ssh -N -L 8788:127.0.0.1:8787 user@your-vps
```

再開啟本機 `http://localhost:8788`。不需把無登入功能的 UI port 公開到網際網路。Git 同步、資料搬移與遠端部署是三個獨立步驟。

## 共用 2 核心／2 GB VPS 的資源設定

兩份 Compose 都提供以下環境變數（只啟動需要的 UI 或 CLI 服務）：

| `.env` 變數 | 預設值 | 用途 |
| --- | --- | --- |
| `TICKET_WATCHER_CPUS` | `0.50` | 單一容器 CPU 上限 |
| `TICKET_WATCHER_MEMORY` | `256m` | 單一容器記憶體上限 |
| `TICKET_WATCHER_PIDS` | `64` | 程序／執行緒數量上限 |

這些值是起始限制，不代表已在你的 VPS 驗證容量。部署後使用 `docker stats --no-stream` 觀察，再按目標數與其他服務用量調整；超出記憶體限制可能被終止並由重啟政策恢復。健康檢查已改為唯讀 SQLite，不建立完整監控程序或 HTTP client。UI 每 30 秒更新目前頁面，隱藏分頁不做定時更新。

依據：[Docker Compose 資源限制](https://docs.docker.com/reference/compose-file/services/#cpus)。

依據：[Docker 容器複製](https://docs.docker.com/reference/cli/docker/container/cp/)、[Docker volume 備份與搬移](https://docs.docker.com/engine/storage/volumes/#back-up-restore-or-migrate-data-volumes)。

# 本機 UI 搬移到 VPS

UI 新增的 Discord 頻道、監控目標、票況基準和日誌都在 `watcher-ui-data` volume；只 clone Git 不會取得它們。搬移整份資料便不必重新填寫。Git 的排除規則包含執行設定、Webhook、SQLite、查詢日誌與 `backups/`；請勿強制加入 Git 或上傳備份到公開位置。

以下是手動搬移步驟，並未自動執行備份或傳送。需先確定 VPS 的 Docker 與 Compose 已可使用。

## Windows 匯出

從專案根目錄執行，先停止寫入 SQLite，再複製完整資料目錄（包含可能存在的 WAL／SHM）：

```powershell
docker compose -f compose.ui.yaml stop ticket-watcher-ui
New-Item -ItemType Directory -Force -Path backups/ui-data | Out-Null
docker compose -f compose.ui.yaml cp ticket-watcher-ui:/app/data/. backups/ui-data/
```

確認備份包含 `ui-config.yaml`、`discord-webhooks.json`、`watcher.db`，若已有新日誌則會有 `watcher-logs/`。Webhook 是明文憑證，備份需私下保管；此命令不會顯示其內容。資料目錄備份不包含宿主機 `.env`，若使用環境變數預設頻道，需另行安全搬移 `.env`；UI 新增的命名頻道則已在 JSON 檔內。

以 SSH/SCP 私下將 `backups/ui-data/` 傳到 VPS 專案的 `backups/ui-data/`。正式切換時保持本機服務停止，避免兩端各自查票與通知。若暫時只做備份，可用 `docker compose -f compose.ui.yaml start ticket-watcher-ui` 恢復本機監控，正式搬移時重新備份。

## VPS 還原

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

## 遠端查看控制台

Compose 仍只發布 VPS 的 `127.0.0.1:8787`。在自己的電腦建立 SSH tunnel：

```sh
ssh -N -L 8788:127.0.0.1:8787 user@your-vps
```

再開啟本機 `http://localhost:8788`。不需把無登入功能的 UI port 公開到網際網路。Git 同步、資料搬移與遠端部署是三個獨立步驟。

依據：[Docker 容器複製](https://docs.docker.com/reference/cli/docker/container/cp/)、[Docker volume 備份與搬移](https://docs.docker.com/engine/storage/volumes/#back-up-restore-or-migrate-data-volumes)。

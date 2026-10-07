# VPS 初次設定與 GitHub 自動更新

[文件索引](README.md) · [架構與上線計畫](vps-launch-plan.md)

本流程使用空白控制台、`tickets.jwander.net`，VPS 專案為 `/opt/ticket-watcher`，與既有 Discord Bot 分開。命令標示執行位置；部署檔必須先 commit／push 至 GitHub，才可在 VPS clone 取得。首次推送會先測試並發布映像，未開啟 `VPS_DEPLOY_ENABLED` 時不會部署。

## 1. 取得部署檔案（VPS SSH 視窗）

```sh
git clone https://github.com/Jwander0820/ticket-watcher.git ~/ticket-watcher-source
cd ~/ticket-watcher-source
```

若資料夾已存在，改為 `cd ~/ticket-watcher-source`、`git pull --ff-only`。這個 checkout 是安裝檔來源，執行資料不放在裡面。

## 2. 建立專用部署金鑰（VPS SSH 視窗）

先確認以下兩個檔名尚未使用；若存在，不覆寫，先確認用途：

```sh
ls -l ~/.ssh/github_ticket_watcher_deploy*
```

尚未存在時建立無密碼 Ed25519 部署金鑰：

```sh
ssh-keygen -t ed25519 -N '' -f ~/.ssh/github_ticket_watcher_deploy -C github-actions-ticket-watcher
sudo bash scripts/install-vps-deploy.sh
sudo nano /opt/ticket-watcher/.env
```

installer 會安裝 root 管理的固定部署入口，追加受限公鑰至 ubuntu 的 authorized_keys，不改原本登入金鑰；不啟動任何容器。保留 Webhook 空白，在 `.env` 設定：

```dotenv
DISCORD_WEBHOOK_URL=
TICKET_WATCHER_PUBLIC_ORIGIN=https://tickets.jwander.net
TICKET_WATCHER_CPUS=0.50
TICKET_WATCHER_MEMORY=256m
TICKET_WATCHER_PIDS=64
```

nano 儲存：Ctrl+O、Enter，再 Ctrl+X。

## 3. 設定 GitHub Environment Secrets（自己的瀏覽器）

開啟 [repo Settings → Environments](https://github.com/Jwander0820/ticket-watcher/settings/environments)，建立 `production`，允許 main 部署，若要全自動就不設每次人工審核。這是 Ticket Watcher 自己的 Environment，另一個 repo 的 Bot Secrets 不會自動共用。

| Secret 名稱 | 填入內容 |
| --- | --- |
| `VPS_HOST` | `43.153.134.44` |
| `VPS_PORT` | `22` |
| `VPS_USER` | `ubuntu` |
| `VPS_SSH_KEY` | 此次新金鑰完整私鑰（含 BEGIN／END） |
| `VPS_KNOWN_HOSTS` | 已核對的 VPS Ed25519 host key 記錄 |

SSH 視窗取得部署私鑰，直接複製到 `VPS_SSH_KEY`，不要貼回聊天或存入 Git：

```sh
cat ~/.ssh/github_ticket_watcher_deploy
```

取得 host key 指紋與 known_hosts 記錄（host 公鑰不是私鑰）：

```sh
sudo ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
sudo awk '{print "43.153.134.44 " $1 " " $2}' /etc/ssh/ssh_host_ed25519_key.pub
```

核對指紋與 Windows 已信任的 VPS 記錄後，將第二個命令的整行輸出放入 `VPS_KNOWN_HOSTS`。不要在 workflow 臨時 ssh-keyscan 後不經核對直接信任。

## 4. 確認 GHCR 映像可下載

首次推送後，在 [Actions](https://github.com/Jwander0820/ticket-watcher/actions) 確認 Tests 的 `publish` 成功，對應映像為 `ghcr.io/jwander0820/ticket-watcher:sha-<commit>`。

新 GHCR package 預設可能是 private；若保留私有，在 VPS 使用專用、有 `read:packages` 的 GitHub PAT 登入。Token 留在 VPS root 的 Docker credential 設定，不放 repo 的 `.env`，也不用 Actions 的短效 `GITHUB_TOKEN` 當成 VPS 長效憑證：

```sh
sudo docker login ghcr.io -u Jwander0820
```

Password 提示時貼上 PAT。若你選擇把這個純程式映像設為 public，VPS 可匿名下載；調整 package visibility 是另外的發布決定，無需為部署公開憑證或執行資料。[GHCR 官方認證說明](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry)

## 5. 設定 Cloudflare Access 與 Tunnel

1. 在 Zero Trust 建立 self-hosted Access app，hostname `tickets.jwander.net`，涵蓋所有路徑；Allow 僅包含自己的帳號。
2. 找到 VPS 已在使用的 Tunnel，新增 published application route：`tickets.jwander.net` → `http://127.0.0.1:8787`。
3. 啟用 Protect with Access，填入此 app 的 team name／AUD。保留公開 Host，不改寫成 localhost。
4. 確認 DNS `tickets` 指向該 Tunnel 的 UUID，代理啟用。VPS 不需對外開 8787。

VPS 已有 `cloudflared-admin.service`，不要另裝一個衝突的預設 cloudflared service；新路由沿用現有管理方式。如果是本機管理 Tunnel，依 [ingress 範例](vps-migration.md#cloudflare-tunnel--access) 在既有 catch-all 前加入規則，保留其他管理服務。正式 route 是否遠端管理，需在帳戶中確認。[Access 官方設定與 origin 驗證](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/self-hosted-public-app/)

## 6. 開啟自動更新與首次部署

在 [repo Actions Variables](https://github.com/Jwander0820/ticket-watcher/settings/variables/actions) 新增 **repository variable**：

```text
VPS_DEPLOY_ENABLED = true
```

開關使用 repository variable，不放在 Environment variable，因為 job 是否執行要先能讀到值。

回到先前 main 推送的成功 Actions run，選擇 **Re-run all jobs**；也可在下一次正式 main 推送觸發。流程會測試、建置、發布固定 digest，再經受限 SSH 首次啟動空白 UI。只有 main push 可發布；PR 或其他分支不部署。映像依該 run 的 commit SHA 驗證，不以浮動 latest tag 部署。[GitHub 映像發布說明](https://docs.github.com/en/actions/tutorials/publish-packages/publish-docker-images)

首次啟動的設定為 `ui_paused: true`、沒有監控目標，也沒有 Discord Webhook。它可以顯示控制台與更新心跳，尚不查票／發通知。

## 7. 驗收與新增監控

VPS SSH 視窗確認容器與 Tunnel：

```sh
sudo docker ps --filter name=ticket-watcher
systemctl is-active cloudflared-admin
curl --fail --silent --output /dev/null http://127.0.0.1:8787/
sudo docker exec ticket-watcher-ticket-watcher-ui-1 ticket-watcher --config /app/data/ui-config.yaml health --json
```

自己的瀏覽器開啟 `https://tickets.jwander.net`：確認 Access 登入後可讀寫，未登入與非允許帳號讀不到 `/api/state`。在控制台設定 Discord、新增監控，確認目標後按「恢復運作」。保留全域暫停時不能送測試通知，需恢復後才測試 Discord。

未來推送 main 會自動更新；UI 資料保存於固定 `ticket-watcher-ui-data` volume，不會被 checkout 或映像更新覆蓋。部署先在 SQLite 副本演練已知升級，再停止 UI 並備份全部資料，離線更新後先暫停驗收，再恢復原本的 paused 狀態。每次部署會有短暫查票中斷。

## 常用維運

```sh
# 程序日誌，不包含控制台私有設定檔內容
sudo docker logs --tail 80 ticket-watcher-ticket-watcher-ui-1
# 部署結果（實際 systemd unit 名稱含 run number）
sudo journalctl --no-pager -n 100 -u 'ticket-watcher-deploy-*'
# 僅確認部署入口，不更新容器
sudo /usr/local/sbin/ticket-watcher-deploy check
```

部署工作由 VPS systemd 管理，SSH 中斷不會取消它；GitHub 工作失敗需檢查 VPS 的最終狀態再重跑。同一 workflow 的過時 run 會略過，VPS 鎖防止與人工操作同時部署。

更新失敗時，先確認候選容器已停止；一般更新保留目前 DB，只在結構相容時恢復前一映像。已知的第 2 → 3 版升級先比對完整結構、在副本檢查完整性及所有資料，再備份並離線升級。只有尚未恢復監控的失敗可還原升級前 DB 及設定；一旦可能恢復監控，就拒絕倒退通知紀錄，保持停止並要求人工處理。未知結構與其他版本變更在停機前拒絕。首次部署失敗若沒有舊版則保持停止，不假裝成功。

每次更新的私有備份位於 `/opt/ticket-watcher/backups`，包含 Webhook。此版本不會自動刪除備份；需安排加密異機備份並按保留政策清理，避免磁碟持續增長。`.env` 另行備份。installer、部署腳本或 Compose 本身有改動時，要重新執行 `sudo bash scripts/install-vps-deploy.sh` 安裝 root 管理的入口；此操作不會停止服務或改寫既有 `.env`。本次自動升級流程也需要先完成這一次入口更新；平常自動更新只更新應用映像，不給 CI 修改 root 部署入口的權限。

Actions 固定使用 `ubuntu-24.04`，避免 `ubuntu-latest` 遷移改變部署環境。映像發布前會在無網路、唯讀檔案系統及正式資源限制的容器中驗證升級、檔案擁有者與回復流程。日誌會區分 `SCHEMA_MIGRATION_REQUIRED`、`SCHEMA_MIGRATION_UNSUPPORTED`、`MIGRATION_READY` 與 `MIGRATION_ROLLBACK_BLOCKED`，不輸出私人設定或資料列。

2026-10-06 本機驗證：全專案 294 項測試、Ruff、Bash 語法與 Compose 設定通過；17 項部署測試涵蓋命令拒絕、鎖、過時版本、預檢、失敗回復與資料暫停／恢復。測試映像建置成功，無網路臨時容器驗證 UID 10001 的空白 UI、首頁、暫停狀態與 heartbeat。沒有掛載正式資料或發送通知。

這些本機驗證不代表 GitHub publish、VPS 部署、Cloudflare Access 或真實通知已完成；這些要依上述步驟逐一讀回驗收。

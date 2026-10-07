# TicketPlus 專用 WARP 出口

[文件索引](README.md) · [VPS 部署](vps-migration.md) · [設定指南](configuration.md)

`TICKET_WATCHER_TICKETPLUS_PROXY` 設定後，所有 TicketPlus 查票操作（UI 自動監控、手動查詢、CLI `query`／`check`／`tick`）會自動將 `https://apis.ticketplus.com.tw` 的請求送至指定 HTTP 代理。Discord 與其他網域仍直連。留空時保持原本的本機開發行為。

代理不可用時依現有 `NETWORK` 錯誤與退避處理，不回退直連，也不增加輪詢或重試。HTTP 代理位址只接受 `http://`／`https://` 端點；全域 `HTTP_PROXY`／`HTTPS_PROXY` 不會取代此設定。

## 本機測試

原生 Python 程序若有本機 WARP HTTP proxy，可設定：

```powershell
$env:TICKET_WATCHER_TICKETPLUS_PROXY = 'http://127.0.0.1:40000'
```

未設定時不需 WARP。Docker 容器的 `127.0.0.1` 是容器自身，必須使用容器能連到的代理地址；不要將宿主機 WARP 的 localhost 位址直接填入容器。

## VPS 接入

前提是 Ubuntu 宿主機已安裝官方 WARP，設定 consumer local proxy 模式、MASQUE、僅監聽 `127.0.0.1:40000`，並且 Ticket Watcher 的 `ticket-watcher_default` bridge 已存在。此腳本不安裝或切換 WARP 模式，也不改宿主機預設路由。

在包含本次程式及部署檔的專案根目錄執行：

```sh
sudo bash scripts/install-vps-warp.sh
```

安裝腳本會安裝小型 `socat` relay、設定開機啟動、備份 VPS Compose／私有 `.env`，並更新 `/opt/ticket-watcher/compose.vps.yaml` 與以下兩個變數：

```dotenv
TICKET_WATCHER_TICKETPLUS_PROXY=http://host.docker.internal:40001
TICKET_WATCHER_WARP_HOST=<Docker bridge 實際 gateway>
```

relay 只監聽 Docker 內部 gateway 的 TCP 40001，限定 Ticket Watcher bridge 來源網段，轉接到宿主機 WARP 的 localhost 40000。啟用 UFW 時，只加入該 bridge、來源網段與目的地址／port 的 INPUT 放行規則。相同 Ticket Watcher 網路中未來新增的容器也屬於這個允許範圍；不要將不受信任的容器加入此網路。

安裝腳本不重啟應用程式。以已測試、包含代理功能的映像更新 `ticket-watcher-ui` 才會生效；現有固定部署入口會讀取 VPS 的 Compose／`.env`，後續部署相同功能的映像會保留代理設定。不要再部署尚未包含此功能的舊版映像。若執行 `compose down` 或重新建立 Docker 網路，重跑安裝腳本更新 relay 的網段與防火牆設定。

## 驗證與回復

```sh
sudo systemctl status ticket-watcher-warp-relay --no-pager
warp-cli --accept-tos status
sudo ss -lnt '( sport = :40000 or sport = :40001 )'
```

驗證容器的 `host.docker.internal` 解析到 relay gateway、TicketPlus 回傳有效 JSON，並觀察自動監控新增成功紀錄。只看 `warp-cli` Connected 或容器 healthy，不能證明查票成功。WARP 出口是否被網站接受仍以每次實際 API 結果為準。

要停用應用程式代理，清空 VPS `.env` 的 `TICKET_WATCHER_TICKETPLUS_PROXY`，再重建同一個 UI 容器；也可回復安裝腳本列出的 Compose／`.env` 私有備份和先前映像。保留原資料 volume，不執行 `down --volumes`。如需停用 relay：

```sh
sudo systemctl disable --now ticket-watcher-warp-relay
```

停止 relay 但未清空代理設定時，查票會依原有退避規則失敗。WARP／relay 的服務重啟不會切換其他程式的出口。UFW 規則可由 `sudo ufw status numbered` 找到 `Ticket Watcher WARP relay` 項目後刪除。

依據：[Cloudflare local proxy](https://developers.cloudflare.com/warp-client/warp-modes/#local-proxy)、[HTTPX transports](https://www.python-httpx.org/advanced/transports/)、[Docker host gateway](https://docs.docker.com/reference/cli/dockerd/#configure-host-gateway-ip)。

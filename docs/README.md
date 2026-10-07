# 文件索引

[回專案首頁](../README.md)

第一次使用可從首頁的快速開始啟動控制台；以下依操作目的安排。所有命令預設從專案根目錄執行，文件中的相對連結以所在文件為起點。

## 使用與維運

| 文件 | 內容 |
| --- | --- |
| [控制台指南](ui.md) | 啟動、Discord 頻道、監控編輯、手動查詢與資料位置 |
| [設定指南](configuration.md) | YAML 欄位、URL 與 ID 篩選、輪詢、自動停止、環境變數 |
| [CLI 與程式整合](cli.md) | 指令選擇、JSON、退出碼、Python 呼叫 |
| [維運與疑難排解](operations.md) | 健康狀態、通知送達、保護性暫停、監控恢復與日誌 |
| [部署與 VPS 搬移](vps-migration.md) | UI／CLI Compose、備份還原、Tunnel／SSH、資源上限 |
| [VPS 上線準備](vps-launch-plan.md) | DNS、PORT、Access／Tunnel、GitHub 自動更新、驗收與回復 |
| [VPS 初次設定](vps-setup.md) | 空白控制台、部署金鑰、GitHub Secrets、GHCR 與上線操作 |
| [WARP 查票出口](warp-proxy.md) | TicketPlus 專用代理、本機測試、VPS 內部轉接與回復 |

## 開發與介面參考

| 文件 | 內容 |
| --- | --- |
| [開發指南](development.md) | 開發環境、驗證命令、程式碼位置、文件維護分工 |
| [設計與行為規格](spec.md) | 目前功能範圍、狀態判定、排程、持久化與可靠性約束 |
| [AI 操作指引](ai-usage.md) | 如何選擇工具、解讀票況與送達結果，以及可用提示詞 |
| [Ticket Watcher skill](../skills/ticket-watcher/SKILL.md) | 提供已安裝 CLI 的 AI 操作指引；不會安裝 MCP |
| [工具契約](../skills/ticket-watcher/references/tool-contract.md) | JSON 欄位、操作語意與錯誤狀態 |

## 來源與歷史紀錄

| 文件 | 用途與時間範圍 |
| --- | --- |
| [TicketPlus 資料來源](ticketplus-source.md) | 2026-10-05 公開端點驗證、欄位與票況對照 |
| [TicketPlus 案例實測](ticketplus-cases.md) | 2026-10-05 活動／票區／票種快照及重查範例 |
| [第一版驗證紀錄](verification.md) | v0.1.0 的測試、容器與公開查詢證據 |

歷史票況、測試數與部署紀錄只代表標示日期的結果。使用能力以目前程式的 `capabilities` 為準，重跑驗證的方法見 [開發指南](development.md)。

## 設定與部署檔案

| 檔案 | 用途 |
| --- | --- |
| [config.example.yaml](../config.example.yaml) | 完整監控設定範本，目標預設停用 |
| [.env.example](../.env.example) | Compose 注入的 Webhook、公開 origin 與資源限制 |
| [examples/ticketplus-cases.yaml](../examples/ticketplus-cases.yaml) | 公開案例設定，目標預設全部停用 |
| [compose.ui.yaml](../compose.ui.yaml) | 控制台與內建監控，使用獨立 UI volume |
| [compose.vps.yaml](../compose.vps.yaml) | VPS 固定 volume、已驗證映像與本地入口 |
| [compose.yaml](../compose.yaml) | 純 CLI 常駐監控，唯讀掛載設定 |

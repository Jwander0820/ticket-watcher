# 開發指南

[文件索引](README.md) · [設計規格](spec.md) · [工具契約](../skills/ticket-watcher/references/tool-contract.md)

## 環境與驗證

需要 Python 3.12 以上。以下從專案根目錄執行，命令不需要真實 TicketPlus 查詢或 Discord Webhook。

Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest -q --basetemp .pytest-tmp-local
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\ruff.exe format --check .
.\.venv\Scripts\python.exe -m build
```

Linux：

```sh
python -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest -q --basetemp .pytest-tmp-local
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/python -m build
```

pytest 會重建指定的 `--basetemp`，請只使用專用暫存目錄。測試使用固定樣本及模擬 HTTP，涵蓋票況比較、租約、通知重試、排程、停止期限、UI 與恢復流程；不輪詢真實票網，也不傳送真實 Discord 訊息。

[CI](../.github/workflows/tests.yml) 在 Python 3.12、3.13、3.14 執行 Ruff 與 pytest。套件建置產物位於 `dist/`。Windows 受限環境若阻擋暫存目錄或 asyncio 初始化，先辨識環境限制，不要為通過測試修改產品行為。

只修改文件時，檢查相對連結、標題錨點、範例參數與 `git diff --check`；CLI 參數可透過 `ticket-watcher <指令> --help` 核對，`capabilities --json` 可離線確認能力。文件驗證不需啟動監控或執行真實 `query`／`check`。

## 程式碼位置

| 路徑 | 職責 |
| --- | --- |
| [cli.py](../src/ticket_watcher/cli.py) | 命令參數、JSON 與退出碼 |
| [config.py](../src/ticket_watcher/config.py) | YAML 驗證、預設值與目標設定簽章 |
| [service.py](../src/ticket_watcher/service.py) | 共用 Watcher、票況比較、排程與事件協調 |
| [platforms/ticketplus.py](../src/ticket_watcher/platforms/ticketplus.py) | 公開來源、ID 轉換與票況正規化 |
| [transport.py](../src/ticket_watcher/transport.py)、[http_body.py](../src/ticket_watcher/http_body.py) | 租約、限流、逾時與回應大小限制 |
| [storage.py](../src/ticket_watcher/storage.py) | SQLite 狀態、交易、outbox 與持久化 |
| [notifications.py](../src/ticket_watcher/notifications.py) | Discord 送達確認、重試與通知取消 |
| [schedule.py](../src/ticket_watcher/schedule.py) | 演出時間與監控停止條件 |
| [health.py](../src/ticket_watcher/health.py)、[query_log.py](../src/ticket_watcher/query_log.py) | 唯讀健康檢查與輪替日誌 |
| [web.py](../src/ticket_watcher/web.py)、[static/](../src/ticket_watcher/static/) | UI、設定重載、工作恢復與網頁資產 |
| [private_io.py](../src/ticket_watcher/private_io.py) | 私有設定檔讀寫與原子寫入 |
| [tests/](../tests/) | 離線測試與來源固定樣本 |

核心狀態語意與可靠性約束集中於 [設計規格](spec.md)，公開端點與資料對照集中於 [來源說明](ticketplus-source.md)。

## 文件維護方式

- README 保留用途、快速開始與入口；操作細節放在對應指南。
- 設定欄位或預設值改動時，同步 `config.example.yaml`、[設定指南](configuration.md) 及相關 UI 說明。
- CLI 或輸出語意改動時，同步 [CLI 指南](cli.md)、[AI 指引](ai-usage.md) 與 [工具契約](../skills/ticket-watcher/references/tool-contract.md)。
- 部署、備份與存取條件集中在 [部署指南](vps-migration.md)，排程／恢復的實作約束放在 [設計規格](spec.md)。
- 歷史驗證保留原日期、版本與測試數，不改成當前結果；新驗證另記日期、範圍與未驗證事項。
- 文件與範例使用專案相對路徑，不納入私有 YAML、Webhook、SQLite 或備份。

[MANIFEST.in](../MANIFEST.in) 已納入 `docs/*.md`、skill 文件及範例；新增同層 Markdown 文件會包含於 source distribution。Docker／VPS 存取、真實釋票與 Discord 送達需在部署環境另外驗證，離線測試通過不代表這些項目已完成。

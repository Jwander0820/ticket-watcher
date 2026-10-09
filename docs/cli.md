# CLI 與程式整合

[文件索引](README.md) · [設定指南](configuration.md) · [AI 操作指引](ai-usage.md)

以下以 `ticket-watcher` 表示已安裝環境的 CLI。Windows 未啟用虛擬環境時用 `.\.venv\Scripts\ticket-watcher.exe`；Linux 用 `.venv/bin/ticket-watcher`。也可用該環境的 `python -m ticket_watcher`。安裝步驟見 [首頁](../README.md#使用-cli-單次查詢)。

## 選擇指令

| 指令 | 用途 | 對監控與通知的影響 |
| --- | --- | --- |
| `capabilities` | 查看版本、支援來源與操作 | 不查票 |
| `query --url <URL>` | 單次取得公開票況 | 不改監控基準、不建立釋票事件、不通知 |
| `status [--target <ID>]` | 讀取已保存票況 | 不查外部網站 |
| `events [--target <ID>]` | 讀取事件與通知送達紀錄 | 不查外部網站 |
| `check --target <ID>` | 依排程檢查既有監控 | 更新基準、可能建立事件並嘗試通知 |
| `check --target <ID> --now` | 立即嘗試檢查既有監控 | 只略過例行排程，保留其他限制 |
| `tick` | 執行一輪到期目標與通知工作 | 可更新基準及通知，完成後退出 |
| `run` | 常駐監控 | 持續查票與處理通知，至少需一個啟用目標 |
| `health` | 唯讀查看本機 heartbeat 與最近查詢資訊 | 不查票、不建立或遷移資料庫 |
| `resume --target <ID>` / `--platform ticketplus` | 人工解除目標／平台暫停 | 不清除等待期限與原有排程 |
| `ui` | 啟動控制台與監控工作 | 詳見 [控制台指南](ui.md) |

監控操作需 `--config <檔案>` 或目前目錄的 `config.yaml`。`query` 未指定設定且目前目錄沒有 `config.yaml` 時，使用目前目錄的 `data/watcher.db`。它仍會保存共享平台節流、元資料快取及查詢紀錄，並非完全不寫磁碟。

## 單次查票

```sh
ticket-watcher capabilities --json
ticket-watcher --config config.yaml query --url "https://ticketplus.com.tw/activity/<活動ID>" --detail full --json
ticket-watcher --config config.yaml query --url "https://ticketplus.com.tw/order/<活動ID>/<場次ID>" --item-id "<票區或票種ID>" --detail full --json
```

替換尖括號佔位值後執行。`--session-id` 與 `--item-id` 可重複指定；`activity` 不接受 `--item-id`。完整活動結果包含各場 `order_url`，內頁結果只回報票區／票種，不推論逐席位置。

`query` 每次取得動態票況，名稱／場次等靜態資料最多快取一小時。它沒有監控比較，`evaluation.release_detected` 固定為 `null`，查到有票不代表「剛釋票」。

## 檢查與常駐

完成 [設定與 Webhook](configuration.md) 後，依目的選擇命令：

```sh
ticket-watcher --config config.yaml status --target my-event --detail full --json
ticket-watcher --config config.yaml events --target my-event --json
ticket-watcher --config config.yaml check --target my-event --json
ticket-watcher --config config.yaml check --target my-event --now --json
ticket-watcher --config config.yaml tick --json
ticket-watcher --config config.yaml run
```

`check` 尚未到期會回傳 `DEFERRED`。`--now` 不略過平台租約、HTTP 間隔、平台／目標暫停、錯誤退避，也不啟用已停用或已到期目標。它與 UI「查詢一次」都會更新基準，可能通知；只想查看票況時使用 `query`。

`run` 將查票、通知與 heartbeat 分開執行；`tick` 讓通知與本輪查票並行，退出前等待最後通知批次；CLI `check` 查票後會等待一次通知嘗試。UI 手動查詢則先返回票況，再由背景佇列送出通知。

外部排程必須實際重複觸發 `tick`；每小時執行一次不能實現分鐘級輪詢。需要內建排程時使用 `run` 或 UI。

Docker 使用容器內 CLI 才能讀取服務的同一份資料庫：

```sh
# 純 CLI Compose：
docker compose exec ticket-watcher ticket-watcher --config /app/config.yaml status --json
# UI Compose：
docker compose -f compose.ui.yaml exec ticket-watcher-ui ticket-watcher --config /app/data/ui-config.yaml status --json
```

宿主機或另一台電腦的獨立 SQLite 不等於容器狀態，兩種 Compose 的 volume 也不同。

## JSON 與退出碼

CLI 操作結果預設輸出 JSON，日誌送 stderr；`--json` 可明確標記但不改變預設格式。`query`、`status`、`events`、`check` 支援 `--detail full --limit 50 --offset 0`。`limit` 為 1～500，`offset` 從 0 開始；分頁只縮減輸出，不縮減查票評估範圍。`events` 預設為摘要，`full` 才含完整變更內容。

同一輪的通知按場次合併，過長時分則。`check` 的 `event_ids`、`hint_event_ids` 列出各類觸發涉及的全部通知事件；舊欄位 `event_id`、`hint_event_id` 保留為各清單第一筆，沒有對應觸發時為 `null`。同場次混合有票與暫無票券的觸發共用通知事件，因此兩個清單可能包含相同 ID。`notification`、`hint_notification` 彙總對應清單的送達狀態；`deliveries` 中以 `event_id` 與 `channel_id` 識別每則、每個頻道的結果，多筆送達時需查看各筆的 `message_id`。

| 退出碼 | 意義 |
| --- | --- |
| `0` | 操作完成；仍需檢查 `complete`、各目標結果與通知狀態 |
| `1` | `health` 未確認程序健康，含 heartbeat 缺失／過期、資料庫不存在或版本不支援 |
| `2` | 操作失敗或參數／設定錯誤 |
| `3` | `DEFERRED`，本次未完成有效查詢／評估 |
| `4` | `UNSUPPORTED`，來源或所需粒度不支援 |
| `130` | 人工中止 |

`DEFERRED` 須連同 `reason` 與 `next_allowed_at` 解讀：可能是排程或平台等待，也可能已發出部分請求後遇到限制，或因租約失效回傳 `QUERY_SUPERSEDED`。不能一概解讀為「未查網站」。

`tick` 的外層完成只代表該輪結束，個別結果在 `checks` 與 `delivery`。`PENDING` 不代表送達，只有 `SENT` 並有 `message_id` 才能稱 Discord 已確認送出。CACHE 結果需保留原始觀測時間，UNKNOWN 的上次有效值不能當成即時有票。

## Python 呼叫

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

使用監控程序相同設定與資料庫即可共用狀態與節流。Python 的立即檢測為 `await watcher.check("my-event", immediate=True)`，遵守相同限制。完整欄位與語意見 [工具契約](../skills/ticket-watcher/references/tool-contract.md)。

# AI 操作指引與提示詞

優先用已安裝的 Ticket Watcher 工具，避免重寫 crawler 或以瀏覽器畫面取代已驗證 API。先執行 `ticket-watcher capabilities --json` 確認版本和粒度。

| 使用者需求 | 操作 |
| --- | --- |
| 看已監控結果 | `status --target <id> --json` |
| 看釋票與送達紀錄 | `events --target <id> --json` |
| 單次查某活動 | `query --url <URL> --json` |
| 授權檢查既有監控 | `check --target <id> --json` |
| 授權外部排程執行一輪 | `tick --json` |

status、events、check、tick 以 `--config` 指向監控程序相同設定。VPS 使用 Docker 時，用 `docker compose exec ticket-watcher ticket-watcher --config /app/config.yaml <操作>`。本機另一份 DB 不等於 VPS 狀態。

`query.evaluation.release_detected` 固定為 null；只有監控比較並建立 RELEASE 事件，才可稱偵測到釋票。CACHE 結果報出原始觀測時間與資料年齡；UNKNOWN 保留的最後有效值不能當作目前已確認有票。

DEFERRED 表示未查網站，遵守 next_allowed_at；平台暫停必須先人工確認問題再用 resume，不能自動解除或以別的出口繼續打。UNSUPPORTED 表示所需粒度不支援，不能忽略 item_ids 改成查整場。

通知入列 PENDING 不等於送達。僅 SENT 並有 message_id 可說 Discord 已確認送出。工具自行通知時 AI 不另外發 webhook。CLI token 成本來自 AI 讀參數與 JSON，監控迴圈本身不用模型。

可貼給 AI 的提示詞：

> 使用已安裝或已連接的 Ticket Watcher 查票工具。先查 capabilities；查看既有監控優先使用 status/events，臨時查票使用 query。只有我授權監控操作時才執行 check/tick。遵守共享 SQLite 與等待期限，不修改監控基準，不用對話記憶推論釋票。只報來源支援的場次粒度，未知和過期資料明確標示原始時間。通知結果以 PENDING/SENT 和 message_id 為準，工具送通知時不重複發送。輸出預設摘要，必要時才分頁讀完整項目。

外部 runner 必須實際定時觸發 tick；提示詞本身不是排程器。每小時 runner 無法實現 1～3 分鐘輪詢。一般建議 VPS 的 run 負責確定性排程，AI 按需要取用結果。

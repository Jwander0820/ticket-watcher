# TicketPlus 公開資料來源驗證

[文件索引](README.md) · [設計規格](spec.md) · [案例實測](ticketplus-cases.md)

驗證日期：2026-10-05（Asia/Taipei）。僅發出公開唯讀 HTTP GET，未登入、未排隊、未呼叫占位或訂單端點。

## 已確認

首頁與活動頁原始 HTML 為 SPA 外殼，沒有可靠票況，因此不使用 BeautifulSoup 猜測庫存。網站當日公開前端：

- `https://ticketplus.com.tw/static/js/app.84a6ed63.js`
- `https://ticketplus.com.tw/static/js/chunk-3cb21956.541d50d3.js`
- `https://ticketplus.com.tw/static/js/chunk-ca2b7fb4.029ce4ef.js`（內頁）
- `https://ticketplus.com.tw/assets/lang/tw.json`（狀態標籤）

前端的 `getInfos` 與靜態資料函式使用以下公開端點，未帶登入憑證可讀取：

| 用途 | GET URL 與參數 |
| --- | --- |
| 活動名稱 | `https://apis.ticketplus.com.tw/config/api/v1/getS3?path=event/<PUBLIC_EVENT_ID>/event.json` |
| 公開場次、日期與場館 | `https://apis.ticketplus.com.tw/config/api/v1/getS3?path=event/<PUBLIC_EVENT_ID>/sessions.json` |
| 當下場次票況 | `https://apis.ticketplus.com.tw/config/api/v1/get?eventId=<INTERNAL_EVENT_ID>&sessionId=<逗號分隔的 INTERNAL_SESSION_ID>` |
| 公開票區／票種名稱與價格 | `https://apis.ticketplus.com.tw/config/api/v1/getS3?path=event/<PUBLIC_EVENT_ID>/ticketAreas.json` 或 `products.json` |
| 當下票區／票種票況 | `https://apis.ticketplus.com.tw/config/api/v1/get?ticketAreaId=<逗號分隔的 AREA_ID>` 或 `?productId=<逗號分隔的 PRODUCT_ID>` |

驗證樣本：[公開活動頁](https://ticketplus.com.tw/activity/190c8cf3965a985d9151d912313e2fe1)。對應內部活動 `e000001189`、場次 `s000001778`。動態回應為 `errCode: "00"`，`result.session` 中有該場次，來源狀態 `soldout`。這只是當日來源快照，不是現在票況，也不是購買保證。

公開舊版 URL ID 是網站以 AES-128-CBC、PKCS7 表示的內部 ID。程式採用公開前端 module 9263 的相同轉換常數，在本機完成轉換；不使用此轉換進入受保護流程。無法可靠轉換的 ID 會回報不支援。**直接將公開十六進位 ID 丟進動態 API，可能得到空陣列，不能判成售完。**

## 狀態對照

活動頁 session table 根據動態 `status` 顯示以下 UI。只使用明確對照，未辨識狀態一律為 `UNKNOWN`。

| 來源 | 標準化 | 前端用途 |
| --- | --- | --- |
| `onsale` | `AVAILABLE` | 顯示購票入口 |
| `soldout` | `SOLD_OUT` | 顯示售完 |
| `pending` | `UPCOMING` | 等待開賣 |
| `over` | `ENDED` | 販售結束 |
| `unavailable` | `TEMPORARILY_UNAVAILABLE` | 暫無票券；外頁釋票線索，不是正數餘票 |
| `lock` | `PAUSED` | 販售鎖定 |

此資料只證明場次層級的入口狀態。`onsale` 可能仍有特定票區售完，程式不推論席位、價格、數量或能成功下單。來源的 `updatedAt` 是設定記錄更新時間，不當成精確釋票時間。

外頁由 `soldout` 轉為 `unavailable` 依使用者的手動刷票經驗建立 `RELEASE_HINT`，不建立確認有票的 `RELEASE`。當日實測尚未觀測到此轉換；「曾釋票」的推論不能只靠一個 `unavailable` 快照證實。`lock` 與 `unavailable` 分開處理。

內頁使用場次的 `ticketArea` 布林欄位選來源：有票區讀 `ticketArea`，無票區讀 `product`。不能只讀 YUURI 的 product：該活動的 productLimit=false 且沒有 count，真正的票區狀態在 ticketArea。內頁 `soldout`／`unavailable` 都標準化為 `SOLD_OUT`；`onsale` 且限制數量時，count=0 為售完，正數為有票，缺失或無效數量為 UNKNOWN。

內頁 UI 只在限定數量且 count≤20 時顯示「剩餘 N」；超過 20 或不限定數量顯示「熱賣中」。因此 `remaining_count` 僅提供 0～20 的頁面對應數量，其餘為 null。販售中案例的全票 count=999999 顯示「熱賣中」，不解讀為實際有 999999 張票；身障票 count=1 則保留「身障票」名稱，不能解讀成一般票剩餘 1 張。票區及票種不是逐席位置。

## 請求與限制

- activity 固定使用 `ticketplus-public-v2/session`；order 根據場次設定固定使用 `ticketplus-public-v1/area` 或 `/product`，不在失敗後切換來源或出口 IP。
- 第一次活動查詢三次 GET，內頁通常四次（活動、場次、票區或票種靜態清單、動態票況）。靜態資料快取一小時；後續每輪通常一次動態查詢，資料不超過 100 個項目時共用回應。
- 元資料快取僅供名稱和穩定 ID。新增場次最多延遲一小時納入；缺少已知場次或不認識狀態時不視為無票，且不計入快速模式的無票退出次數。
- 最多 100 個公開場次。更多場次明確回報不支援，不只看前 100 個。
- 內頁最多 1000 個公開項目，每 100 個批次查詢；不只看第一批。activity 不接受 item_ids；order 支援票區或票種 ID 篩選，仍先讀完整場次再篩選、分頁。
- 所有觀測依賴公開 API 的當下回應；沒有點入受保護的購票、座位選取或訂單流程。
- 沒有官方公開 API 穩定性承諾。前端欄位或 ID 格式變更可能使來源失效；三次解析異常後保存暫停狀態。
- 本機的公開存取於上述日期驗證；該次驗證未包含 VPS 出口存取、真實釋票轉換與 Discord 實際送達，部署環境需另行確認。

HTTP client 的連線重用與 webhook 確認方式參考 [HTTPX 官方文件](https://www.python-httpx.org/async/) 與 [Discord Execute Webhook](https://docs.discord.com/developers/resources/webhook#execute-webhook)。

# 使用者提供的 TicketPlus 案例實測

新版 CLI 查詢時間：2026-10-05 23:46:05～23:46:30（Asia/Taipei）。所有結果都是當時快照，非即時持續監控。未登入、未建立訂單，未發送真實 Discord。

| 案例 | 本次完整查詢結果 |
| --- | --- |
| [YUURI 活動外頁](https://ticketplus.com.tw/activity/7acedf4b414903ac17104384cb416849) | 10/9、10/10 兩場均為 SOLD_OUT；公開資料提供兩場購票網址 |
| [YUURI 10/9 內頁](https://ticketplus.com.tw/order/7acedf4b414903ac17104384cb416849/c88be5c1c01bf684e9a56b6defcd6e8d) | 完整取得 71 個公開票區，全部 SOLD_OUT |
| [YUURI 10/10 內頁](https://ticketplus.com.tw/order/7acedf4b414903ac17104384cb416849/2606691b5fcc73c52940bfb047a3d622) | 完整取得 71 個公開票區，全部 SOLD_OUT |
| [販售中活動](https://ticketplus.com.tw/activity/6def673ab73ab7d7a4d0597ae84809ca)／[內頁](https://ticketplus.com.tw/order/6def673ab73ab7d7a4d0597ae84809ca/c2e30fcb68cdcd8cee2b3576dd4fe2f5) | 尚霖 X 葉澈＋KITA X 周穆；全票 $500「熱賣中」，身障票 $250「剩餘 1」 |

YUURI 的日期以本次公開場次資料為準，是 10/9、10/10；使用者提供的手動技巧文字提及 10/11、10/12，沒有用來代替本活動日期。

## 兩種訊號

- 外頁：`soldout → unavailable` 對應「銷售一空 → 暫無票券」，依手動經驗發送 `RELEASE_HINT`。訊息明確標示釋票線索、尚未確認正數餘票；相同狀態持續存在不重複通知。首次就看到暫無票券只建立基準。
- 內頁：有票區的活動查票區，無票區的活動查票種。限定數量下，`count=0 → 正數` 可辨識有票；`剩餘 1`～`剩餘 20` 保留可見數量，更大的數字與不限數量顯示熱賣中。未知或缺失資料不猜售完。
- 正數餘票的確認通知與外頁線索通知分開。外頁下一輪變成可購票，可另建立確認事件。只有 query 沒有基準比較，不報偵測到釋票，也不發通知。

「暫無票券」的文字及資料欄位已由網站公開前端、語言檔核對，真實 `soldout → unavailable` 釋票轉換尚未在本次快照觀測到；相關通知流程以離線模擬驗證。全票原始數量 999999 在網站顯示為熱賣中，不解讀成實際庫存；身障票 1 張也不是一般票餘量。

## 直接重查

在專案根目錄執行：

```powershell
# 只提供外頁時，完整結果包含每場 order_url：
.\.venv\Scripts\ticket-watcher.exe query --url 'https://ticketplus.com.tw/activity/7acedf4b414903ac17104384cb416849' --detail full

# 不需要先從網站點入，直接讀取該場的公開票區來源：
.\.venv\Scripts\ticket-watcher.exe query --url 'https://ticketplus.com.tw/order/7acedf4b414903ac17104384cb416849/c88be5c1c01bf684e9a56b6defcd6e8d' --detail full --limit 100

# 販售中的票種案例：
.\.venv\Scripts\ticket-watcher.exe query --url 'https://ticketplus.com.tw/order/6def673ab73ab7d7a4d0597ae84809ca/c2e30fcb68cdcd8cee2b3576dd4fe2f5' --detail full
```

每次先查完整選定範圍再做輸出分頁，`--limit 3` 不會只評估前三個票區。票區／票種可指定 `--item-id a000...` 或 `--item-id p000...`，先以完整靜態清單驗證後，只查選定項目的動態票況；不合此場的項目會明確報錯。未指定項目仍查全部公開項目。

## 監控設定與驗證

[examples/ticketplus-cases.yaml](../examples/ticketplus-cases.yaml) 提供外頁與兩場內頁，可混用；目前全部 `enabled: false`，沒有啟動常駐監控。需要使用時，選擇目標改為 true，設定 Discord webhook 環境變數，再以這份設定執行 check／run。所有目標共用專案 data/watcher.db 與請求節流。

保持原一般 300～900 秒、快速 60～180 秒、HTTP 請求至少隔 5 秒。沒有改成 1～3 秒；因此短暫釋票仍可能在兩次觀測之間被買走。這次驗證的是來源與判斷流程，沒有證明實際抓到短暫釋票或 Discord 實際送達。

109 項離線測試通過；公開樣本只保留測試所需名稱、ID、價格、狀態與數量在 tests/fixtures/ticketplus-cases.json。測試涵蓋兩場完整票區、內頁數量、混合訊號、批次超過 100 項、缺失資料、去重與待送取消。

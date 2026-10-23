# 2026-10-23 CA 事件與 cryptography 版本評估

本筆記記錄官方來源與來源碼判讀；實際映像、測試與部署狀態應另依本次驗證結果判定。研究過程未向 TicketPlus 發送請求。

## eMudhra／emSign 憑證事件

Python 安全公告要求更新 `pip>=26.2.2`、`certifi>=2026.10.19` 並重新建置容器，取得最新系統憑證。公告推薦 2026.10.19，而非最初修補版 2026.10.18，以保留未受影響的 emSign 根憑證相容性。[Python 公告](https://discuss.python.org/t/information-for-python-users-regarding-emudhra-emsign-ca-certificate-security-incident/109576)

更新應用 certifi 與系統 CA 並不足以修補舊 pip：pip 另外使用內嵌 certifi，更新 pip 才會替換這份信任資料。[Python 安全團隊說明](https://discuss.python.org/t/information-for-python-users-regarding-emudhra-emsign-ca-certificate-security-incident/109576/3)；[pip 26.2.2 changelog](https://pip.pypa.io/en/stable/news/#v26-2-2) 確認內嵌 certifi 升為 2026.10.19。

Python 官方提供下列 wheel 及 SHA256 作為安全引導來源；安裝前／安裝時必須驗證 hash，而不能只依賴原本可能過期的 CA 信任。[Python 官方安全更新與引導命令](https://blog.python.org/2026/10/python-3151-3149-31317/)

```text
URL: https://files.pythonhosted.org/packages/3f/06/3e4420302363fbb71be3c7f35ce042f54bfca330e3e66b3a2e3c4cca579f/pip-26.2.2-py3-none-any.whl
SHA256: fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70
```

應直接解析實際映像中的 PEM 憑證，確認系統 CA bundle 與 CA 目錄、應用 certifi、pip 內嵌 certifi 都沒有 `emSign ECC Root CA - G3`。版本下限或文字註解搜尋不能代替憑證內容核對。[certifi 維護者公告](https://github.com/certifi/python-certifi/security/advisories/GHSA-6q48-6c7w-9gw2)

## cryptography：獨立的套件漏洞議題

截至本次查證，PyPI 最新穩定系列為 50.0，最新修補版為 **50.0.2**（2026-09-30）。其官方 wheel 使用 OpenSSL 4.0.3；50.0.1 使用 4.0.2。[PyPI](https://pypi.org/project/cryptography/)、[官方 changelog](https://cryptography.io/en/latest/changelog/)

| 公告 | 官方修補版本 | 實際風險範圍 |
| --- | --- | --- |
| [GHSA-g6cj-pr64-35w5 / CVE-2026-69247](https://github.com/pyca/cryptography/security/advisories/GHSA-g6cj-pr64-35w5) | 50.0.0 | 44.0.0 引入的 PKCS#7 EnvelopedData RSA 金鑰解封裝錯誤／時序 oracle；涉及 `pkcs7_decrypt_der`、`pkcs7_decrypt_pem`、`pkcs7_decrypt_smime`。需要服務解密攻擊者提供的訊息並可觀察結果。 |
| [GHSA-m2h6-j472-rp4c / CVE-2026-69248](https://github.com/pyca/cryptography/security/advisories/GHSA-m2h6-j472-rp4c) | >=49.0.0 | X.509 verifier wildcard DNS name constraints 檢查。 |
| [GHSA-jwv3-5hgf-82ww / CVE-2026-69249](https://github.com/pyca/cryptography/security/advisories/GHSA-jwv3-5hgf-82ww) | >=49.0.0 | 含重複自簽憑證的無效鏈造成指數型路徑建構。 |
| [GHSA-537c-gmf6-5ccf / CVE-2026-34180](https://github.com/pyca/cryptography/security/advisories/GHSA-537c-gmf6-5ccf) | 48.0.1 | 官方 wheel 內含的 OpenSSL；sdist 使用者須自行維護連結的 OpenSSL。 |

原本 `cryptography>=44,<49` 允許上述漏洞版本，且上限阻擋 49／50 系列修補。建議在本專案相容性測試通過後採 `cryptography>=50.0.2,<51`，以目前修補系列為下限並保留下一個 major 的檢查機會。

本專案 `src/ticket_watcher/platforms/ticketplus.py` 唯一直接使用路徑為 `Cipher(algorithms.AES(...), modes.CBC(...))` 與 `padding.PKCS7(128)`，用於公開 URL ID 的轉換。這是對稱加密區塊填補，沒有 RSA RecipientInfo，也沒有呼叫 `serialization.pkcs7.pkcs7_decrypt_*`。因此不能因名稱同為 PKCS7 就宣稱 TicketPlus ID 路徑可觸發 EnvelopedData 公告漏洞；也不能因此保留阻擋修補的套件上限。這項結論來自本次來源碼搜尋，並不代表所有未來相依套件或新功能皆無觸發路徑。

49／50 的相容性注意事項包含移除舊型別別名、調整 ChaCha20 counter 與部分 X.509 解析限制，以及 FFDH 棄用；來源碼搜尋未發現本專案直接使用這些 API。仍須用實際 50.0.2 環境驗證 ID 已知值、往返轉換、錯誤輸入與既有 UI／部署測試。[官方 changelog](https://cryptography.io/en/latest/changelog/)

## 驗證與建置邊界

重新建置應同時拉取 base image 並停用舊建置 layer cache；更新系統 CA 後仍須對最終映像執行內容檢查。此處版本與來源查證不等於已完成映像或正式環境驗收。本次工作不得發送 TicketPlus 驗收請求、變更監控、發通知或部署；主分支 push 的既有部署觸發須另經授權。

## 2026-10-23 本機驗證結果

- 已實作 `scripts/bootstrap_pip.py`：以標準函式庫 HTTPS 下載上述 wheel，先核對固定 SHA256，再從已核對的 wheel 執行 pip，使用 `--no-index --no-deps` 安裝。沒有啟動舊 pip 下載修補。Docker 與 CI 的兩處 Python 環境安裝皆先執行此引導。
- 應用依賴已改為 `certifi>=2026.10.19`、`cryptography>=50.0.2,<51`。本機 Windows Python 3.12.14 與 Linux 映像均實際使用 cryptography 50.0.2；原本 Windows 環境為 48.0.1。CI 保留 3.12／3.13／3.14 矩陣，後兩者尚未在本機執行。
- `docker build --pull --no-cache --platform linux/amd64` 成功；本次 Python base manifest 為 `sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704`。本機標籤為 `ticket-watcher:ca-security-local`，最終 image ID 為 `sha256:aa2cf349e14030f4477f15214f55e9f76827b6f5eefdfd8f75d96bc30a92bc9a`。未推送 registry。
- `scripts/verify_ca.py` 對實際 PEM 解析 subject，並要求 TLS 可載入；不是只搜尋 PEM 註解。最終映像的系統 `/etc/ssl/certs/ca-certificates.crt` 有 148 張、`/etc/ssl/certs/*.pem` 合計 148 張、應用 certifi 有 120 張、pip 內嵌 certifi 有 120 張，皆無 `emSign ECC Root CA - G3`。pip 實際版本為 26.2.2，certifi 為 2026.10.19。
- 上述映像檢查在 `--network none --read-only --cap-drop ALL --security-opt no-new-privileges:true --memory 256m --cpus 0.5 --pids-limit 64` 下通過。使用映像內安裝的程式驗證兩筆已知 TicketPlus ID 解密與一筆已知加密值，也通過既有受限能力的遷移／權限／備份／回復 smoke。全程只使用暫存資料，未掛載正式 volume。
- 最終完整測試命令為設定 `TICKET_WATCHER_DEPLOY_TEST_IMAGE=ticket-watcher:ca-security-local` 後執行 `python -m pytest -q --basetemp .pytest-tmp-ca-final`：**555 passed**。涵蓋 ID 格式／轉換、UI Host／Origin 安全、暫停與存取限制、部署模擬及實際映像。新增測試確認 wheel 被竄改時不執行安裝，以及 CA 檢查以憑證 subject 而非註解判定。
- Ruff check／format、`pip check`、`git diff --check` 通過；`python -m build --no-isolation` 成功產出 sdist／wheel。`MANIFEST.in` 納入建置需要的 scripts，避免從 sdist 建置映像時缺少引導／驗證程式。
- 系統既有根憑證中有非正序號，cryptography 50.0.2 解析時發出未來相容性警告；本次解析及 TLS 載入成功，未隱藏該警告。這不是目標根憑證仍存在的證據。

未修改 Compose 的 CPU／記憶體／PID 限制、localhost 綁定或 Cloudflare Access 邊界。未向 TicketPlus 發驗收請求、發送通知、啟動或變更正式監控、commit、push、發布映像、觸發 Actions 或部署 VPS。上述結果是本機證據，正式環境仍待另行授權更新。

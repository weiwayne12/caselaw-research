# caselaw-research — 司法院判決大量抓取與分析工具

針對某個**案由／法律爭點**（例如「返還寄託物」），一次把司法院上的**幾百件類似判決**抓到自己電腦，
再請 AI 分批讀完、整理成三份辦案參考報告（爭點光譜、辦案檢核清單、常引法條與按語）。

> 只處理**公開判決**，沒有個資問題。
> 本說明假設你**完全沒用過**，照著做就能上手。

---

## 它幫你做兩件事

| 階段 | 工具 | 做什麼 | 你會得到 |
|---|---|---|---|
| 一、抓取 | `harvester/harvest.py` | 用關鍵字大量抓判決存到本機 | `corpus/` 裡一堆判決檔 |
| 二、分析 | 請 AI（Claude）讀 `corpus/` | 整理成辦案報告 | `reports/` 裡三份 .md 報告 |

---

## 🧰 開始前你需要這些（重要）

這個工具**借用**了一支已經寫好的爬蟲引擎（最難的「突破司法院防爬」部分），所以要先有它：

1. **那支爬蟲引擎**：位於 `C:\LLMWIKI\.mcp\taiwan-legal-db`
   （已內含 Playwright 等套件，無須另外安裝）。
2. **用它的 Python 來執行本工具**，路徑是：
   ```
   C:\LLMWIKI\.mcp\taiwan-legal-db\Scripts\python.exe
   ```
   下面所有指令的開頭 `python` 都要換成這一長串路徑。

> ⚠️ 如果你的電腦**沒有** `C:\LLMWIKI` 這支引擎，這個工具會跑不起來
> （會出現「無法 import mcp_server」）。請先向專案提供者索取該引擎。

---

## 🚀 5 分鐘快速上手

### 步驟 1：打開終端機，切到專案資料夾
在 Windows 開「PowerShell」或「終端機」，輸入（路徑換成你放專案的地方）：
```powershell
cd C:\Users\你的帳號\Documents\caselaw-research
```

### 步驟 2：先「試水溫」，看有多少判決（不會真的抓，很快）
複製這行，把 `返還寄託物` 換成你要研究的案由關鍵字：
```powershell
C:\LLMWIKI\.mcp\taiwan-legal-db\Scripts\python.exe harvester\harvest.py --keyword "返還寄託物" --case-type 民事 --slug 返還寄託物 --year-from 110 --year-to 114 --dry-run
```
它會印出「母體共 N 件」和各法院／年度分布。**`--dry-run` = 只看數量、不下載。**

### 步驟 3：正式抓下來
把上面那行**最後的 `--dry-run` 拿掉**就會真的開始抓。第一次建議先加 `--target 30` 限制數量試跑：
```powershell
C:\LLMWIKI\.mcp\taiwan-legal-db\Scripts\python.exe harvester\harvest.py --keyword "返還寄託物" --case-type 民事 --slug 返還寄託物 --year-from 110 --year-to 114 --target 30
```
跑完後判決就存在 `corpus\返還寄託物\` 裡。

### 步驟 4：看抓到什麼
打開 `corpus\返還寄託物\` 資料夾，裡面每個 `.json` 就是一篇判決（含全文）。

### 步驟 5：請 AI 幫你分析（產出報告）
這一步是「**請 Claude 讀 `corpus/` 裡的判決，整理成報告**」。
打開 Claude Code（或你的 AI 助理），切到本專案，對它說：
> 「請讀 `corpus/返還寄託物/` 裡的判決，產出爭點光譜、辦案檢核清單、常引法條三份報告，放到 `reports/`。」

AI 會用 `analysis/` 裡的輔助腳本分批讀完，最後在 `reports/` 產出三份 `.md`。
（可參考已附的範例：`reports/返還寄託物-01~03`。）

---

## ⚙️ harvester 常用參數

| 參數 | 意思 | 範例 |
|---|---|---|
| `--keyword` | 案由／主題關鍵字（**必填**之一） | `--keyword "分管協議"` |
| `--case-type` | 案件類型：民事／刑事／行政 | `--case-type 民事` |
| `--slug` | 存檔資料夾名（通常同案由） | `--slug 返還寄託物` |
| `--year-from` / `--year-to` | 起訖**民國**年（如 110=民國110年） | `--year-from 110 --year-to 114` |
| `--target` | 最多抓幾件（達標即停） | `--target 300` |
| `--courts` | 指定法院（不填＝最高→高院→北院） | `--courts "臺灣高雄地方法院"` |
| `--dry-run` | 只估數量、不下載 | （加在最後面） |
| `--reuse-manifest` | 中斷後**續抓**（已抓的自動跳過） | （加在最後面） |
| `--include-rulings` | 連「裁定」也收（**預設只收判決**） | （加在最後面） |

看完整說明：
```powershell
C:\LLMWIKI\.mcp\taiwan-legal-db\Scripts\python.exe harvester\harvest.py --help
```

---

## 📁 資料夾這樣分（看名字就懂）

```
caselaw-research/
├─ harvester/   工具一：抓取程式
├─ analysis/    工具二：分析輔助腳本
├─ corpus/      抓下來的判決（JSON，每篇一檔）   ← 不上傳 GitHub
├─ state/       抓取進度與快取（可重建）          ← 不上傳 GitHub
├─ notes/       分析中間筆記                      ← 不上傳 GitHub
└─ reports/     最終報告（.md，給人讀）           ← 會上傳 GitHub
```
**心法**：`state→corpus→notes→reports` 就是資料的旅程（原料 → 成品）。
判決原始檔故意存成 JSON，是為了讓程式能精準統計（例如自動算法條出現次數）。

---

## ❓ 常見狀況

- **「無法 import mcp_server」**：沒有 `C:\LLMWIKI` 那支引擎，或沒用它的 python 執行。請見上方「開始前你需要這些」。
- **畫面中文變亂碼**：本工具已自動處理；若仍亂碼，是終端機編碼問題，可改用 Windows Terminal。
- **抓一抓全部失敗、說連線失敗**：被司法院**暫時擋住**了（打太密集）。這不是壞掉，等幾分鐘到約一小時，
  用**同一條指令加 `--reuse-manifest`** 續抓即可，已抓的不會重抓。
- **想換研究主題**：只要改 `--keyword` 和 `--slug` 重跑就好。

---

## 📌 注意事項

- 抓取依賴本機 `C:\LLMWIKI\.mcp\taiwan-legal-db` 引擎（已知耦合點；之後可改為本專案自有相依以便他人使用）。
- `corpus/`、`state/`、`notes/` 已設定**不上傳**（判決原始檔與快取留在本機）。
- 協作規範與完整技術細節見 **[`KB.md`](KB.md)**（本專案規範的唯一真實來源）。
- 分析報告一律**中立呈現、不下結論、不替個案給建議**，僅供研究參考。

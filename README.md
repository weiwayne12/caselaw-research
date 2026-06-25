# caselaw-research — 司法院判決大量抓取與分析工具

針對單一**案由 / 爭點**，從司法院裁判書系統大量抓取「類似案件」存成本機語料庫，
再由 Claude 分批閱讀語料，產出三種辦案 SOP 產出（爭點光譜、辦案檢核清單、常引按語與法條清單）。

> 本專案僅處理**公開判決**。與 `judgment-workspace-repo` 工作區完全無關，互不干涉。

## 兩階段、兩支工具

### 工具一：harvester（抓取）— 獨立 Python CLI
- 直接 import 既有爬蟲套件 `mcp_server`（位於 `C:\LLMWIKI\.mcp\taiwan-legal-db`），
  重用其 Playwright + F5 WAF bypass 引擎，**不重寫爬蟲**。
- 自跑迴圈：多輪 `search_judgments` 翻頁去重 → 對每個 JID 呼叫 `get_judgment`
  → 逐篇寫入 `corpus/<案由-slug>/<JID>.json`。
- 維護 `manifest.jsonl`，中斷可續抓、可重跑。
- **預設只收「判決」、濾掉「裁定」**（實體法研究取向；依 case_id 後綴在搜尋階段判定，
  不浪費抓取）。要連裁定一起收用 `--include-rulings`。
- 被司法院 WAF 節流時，fetch 斷路器會在連續失敗達上限時停手；稍後加
  `--reuse-manifest` 即可續抓（已抓的自動跳過）。

### 工具二：分析（Claude 讀語料）
抓完後由 Claude 分批讀本機 `corpus/` 純文字檔（不再經 MCP），產出：
1. **爭點光譜**：常見爭點與各爭點實務見解分布（主流／少數／事實分型）。中立呈現，不下結論、不給建議。
2. **辦案檢核清單／流程**：從反覆出現的調查事項、程序節點歸納成可勾選 SOP。
3. **常引按語與法條清單**：引用法條＋引用判決，依頻率排序，附代表案。

## 目錄結構
```
caselaw-research/
├─ harvester/          # 工具一 CLI（實作待確認後撰寫）
├─ corpus/             # 語料庫：<案由-slug>/<JID>.json（不入庫）
├─ state/              # manifest、快取 DB、WAF cookies（不入庫）
├─ notes/             # 分析中間筆記（不入庫）
└─ reports/            # 最終 SOP 產出（入庫）
```

## 已知耦合點
- 依賴 `C:\LLMWIKI\.mcp\taiwan-legal-db` 的 venv（`Scripts\python.exe`，Python 3.12，
  已裝 playwright 等相依）。harvester 需用該 venv 的 python 執行。
- 後續可評估改為本專案自有 venv 安裝同套件以解耦。

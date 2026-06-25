# 專案知識庫（核心規範）

> 本檔為本專案 AI 協作規範的**唯一真實來源（single source of truth）**。
> `CLAUDE.md` 與 `AGENTS.md` 皆指向本檔，請勿在那兩份重複撰寫內容，一律只改本檔。

---

## 專案用途

一套「司法院判決**大量抓取 ＋ 分析**」工具，分兩階段、兩支工具，針對單一**案由／爭點**建立本機判決語料庫並產出辦案 SOP。

- **工具一 harvester（抓取）**：用案由關鍵字大量抓「類似案件」存成本機語料庫。
- **工具二 analysis（分析）**：由 Claude 分批讀本機語料，產出三種 SOP 產出（爭點光譜、辦案檢核清單、常引按語與法條清單）。

僅處理**公開判決**，無個資包袱，正常 git 管理。與另一工作區 `judgment-workspace-repo` **完全無關，不得更動它**。

---

## 協作規範（通則）

- **語言**：繁體中文（台灣法律用語）。
- **不得臆測**：判決內容、法條、見解一律以實際語料／官方來源為準；查不到就說查不到，不杜撰字號或按語。
- **官方來源優先**：判決出自司法院裁判書系統（FJUD）、法規出自全國法規資料庫。
- **編碼**：所有檔案 UTF-8（無 BOM）。Windows 主控台預設 Big5，Python 程式須 `sys.stdout.reconfigure(encoding="utf-8")` 避免中文亂碼。
- **分析中立原則（最重要）**：工具二的報告一律**中立呈現、不下結論、不評價、不給個案建議**。只整理見解光譜與事實分型，引用字號供回溯。

---

## 架構與耦合

- **爬蟲引擎重用，不重寫**：Playwright + F5 WAF bypass 的爬蟲已存在於
  `C:\LLMWIKI\.mcp\taiwan-legal-db`（核心套件 `mcp_server`）。harvester 直接 import 其 client 類別
  （`JudicialSearchClient` / `JudgmentDocClient` / `CacheDB` / `JudicialWAFBypass`）。
- **執行用該 venv 的 python**：
  `C:\LLMWIKI\.mcp\taiwan-legal-db\Scripts\python.exe`（Python 3.12，已裝 playwright 等相依）。
- 該套件視為**唯讀重用**，不得修改。WAF cookies 沿用其共用檔（重用已暖機成果）；
  但快取 DB 導向本專案 `state/`，不污染共用安裝。

---

## 資料夾與檔案慣例

| 路徑 | 內容 | 入庫？ |
|---|---|---|
| `harvester/harvest.py` | 工具一 CLI | ✅ |
| `analysis/build_skim.py` | 工具二：全語料精簡略讀表（省 token 鳥瞰） | ✅ |
| `analysis/dump_batch.py` | 工具二：成批吐出判決關鍵欄位供精讀 | ✅ |
| `corpus/<案由-slug>/<JID>.json` | 語料：每篇判決一個結構化 JSON | ❌ 忽略 |
| `state/<slug>.manifest.jsonl` | 母體清單（可續抓） | ❌ 忽略 |
| `state/legal_cache.db` | 抓取快取（可重建） | ❌ 忽略 |
| `notes/<slug>-skim.md` | 分析中間略讀表 | ❌ 忽略 |
| `reports/<slug>-NN-*.md` | 最終 SOP 報告（Markdown，給人讀） | ✅ |

**分層心法**：`corpus/*.json` 是**機器用的結構化原始語料**；`reports/*.md` 是**人用的分析成品**。
語料用 JSON 是為了讓欄位（主文、事實、引用法條…）可被程式精準抽取與彙總（例如免讀就統計法條頻率）。

---

## 工具一 harvester 用法與行為

```
<venv python> harvester/harvest.py --keyword "案由關鍵字" --case-type 民事 \
    --slug 案由 --year-from 110 --year-to 114 --target 300
```

- **預設只收「判決」、濾掉「裁定」**（實體法研究取向）；依搜尋結果 `case_id` 後綴在搜尋階段判定，
  不浪費抓取。要連裁定一起收用 `--include-rulings`。**不可靠字別推定判決/裁定**
  （台上字別也可能是裁定、再審之訴卻是判決），一律看官方後綴。
- **預設法院**（審級權威序）：最高法院 → 臺灣高等法院 → 臺灣臺北地方法院。`--courts` 可覆寫（須用全名）。
- **搜尋切片**：(年度 × 法院) 笛卡兒積，每格上限 200 筆，跨格以 JID 去重。單格滿 200 會警告可能截斷。
- **不空轉**：達 `--target` 即停；搜尋單格失敗有限次重試；fetch 連續失敗達斷路器上限即停手。
- **可續抓**：`corpus/<slug>/<JID>.json` 存在即跳過；中斷後相同指令加 `--reuse-manifest` 續抓。
- **先估母體**：`--dry-run` 只建 manifest、印分布，不抓全文。
- **被司法院 WAF 節流時**：屬外部反爬機制，非程式錯誤；斷路器會停手並提示「等一段時間後 `--reuse-manifest` 續抓」。

### corpus JSON 欄位
`jid, case_id, doc_type, court, case_type, date, cause, judges, parties,
main_text, facts, reasoning, cited_statutes, cited_cases, full_text, source_url, harvested_at`

> ⚠️ **重要 gotcha**：民事判決多用「事實及理由」合併標題，上游 parser 會把理由併入 `facts`，
> 故 `reasoning` 欄位**多為空**。分析見解時要讀 `facts`（或 `full_text`），不能只看 `reasoning`。

---

## 工具二 analysis 流程（兩段式，省 token）

1. **第一段・全語料略讀**：`build_skim.py <slug>` → 產 `notes/<slug>-skim.md`
   （每篇只留主文、案由、引用法條、事實理由首段）。一次讀完建立「請求類型 × 勝敗 × 法條」骨架，
   並標出論理最豐富、最具代表性的幾篇。
2. **第二段・關鍵精讀**：`dump_batch.py <slug> <start> <count>` 深讀代表案完整理由，擷取精確按語原文。
3. **彙整三份報告**（Markdown，中立呈現）：
   - `reports/<slug>-01-爭點光譜.md`：事實分型 + 各爭點實務見解分布（主流／少數／事實分型）。
   - `reports/<slug>-02-辦案檢核清單.md`：從反覆出現的調查事項、程序節點歸納之可勾選 SOP。
   - `reports/<slug>-03-常引按語與法條.md`：引用法條 + 引用判決依頻率排序，附最高法院按語原文與代表案。

引用頻率類統計（如法條出現篇數）以 Python 直接彙總 `cited_statutes` / `cited_cases` 欄位，不需 Claude 逐篇讀。

---

## 修改與驗證

- 改 `harvester/harvest.py` 後：先 `<venv python> -m py_compile harvester/harvest.py`，
  再以小範圍 `--dry-run`（如 1 法院 × 2 年度）煙霧測試。
- 改分析腳本後：以既有 `corpus/` 跑一次確認輸出。
- **不得更動** `C:\LLMWIKI\.mcp\taiwan-legal-db`（唯讀重用）與 `judgment-workspace-repo`。
- git：`corpus/`、`state/`、`notes/` 已於 `.gitignore` 忽略；提交前確認語料與快取未誤入庫。
  未經明確要求不擅自 commit／push。

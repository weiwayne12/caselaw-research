#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""harvester — 司法院判決大量抓取 CLI（工具一）

重用既有爬蟲套件 mcp_server（C:\\LLMWIKI\\.mcp\\taiwan-legal-db）的
搜尋 / 取全文 client，自跑迴圈把某案由的「類似案件」抓成本機語料庫。

設計原則：
  - 不空轉：搜尋是 (年度 × 法院) 的有限笛卡兒積，格數可預估、有上限。
  - 可續抓：corpus/<slug>/<JID>.json 存在 = 該篇已完成；重跑只補缺。
  - 逐筆容錯：單篇失敗記 errors.log 不中斷全局。

用法（以共用 venv 的 python 執行）：
  C:\\LLMWIKI\\.mcp\\taiwan-legal-db\\Scripts\\python.exe harvester\\harvest.py \\
      --keyword "分管協議" --case-type 民事 --slug 分管協議 \\
      --year-from 110 --year-to 114 --target 300

  先估母體不抓全文：加 --dry-run
"""

import argparse
import asyncio
import json
import logging
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path

# Windows 主控台預設 Big5(cp950)，中文 log 會變亂碼 → 強制 UTF-8 輸出。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

# ── 定位 Taiwan Legal DB MCP 引擎（mcp_server 套件）──
# 本工具重用「Taiwan Legal DB MCP」背後的 Python 套件 mcp_server 作為爬蟲引擎，
# 並以該 MCP 的 venv python 執行（內含 playwright 等相依）。
# 解析順序：環境變數 TAIWAN_LEGAL_DB_HOME → 預設候選路徑。
# TAIWAN_LEGAL_DB_HOME 應指向 MCP 安裝根目錄（其下有 Lib/site-packages 與 Scripts/python.exe）。
_DEFAULT_HOMES = [
    r"C:\LLMWIKI\.mcp\taiwan-legal-db",  # 本機既有安裝
]


def _bootstrap_mcp_engine() -> None:
    """把 Taiwan Legal DB MCP 的 site-packages 加進 sys.path（正常用其 venv python 跑時非必要，此為防呆）。"""
    import os
    candidates = []
    env_home = os.environ.get("TAIWAN_LEGAL_DB_HOME", "").strip()
    if env_home:
        candidates.append(env_home)
    candidates.extend(_DEFAULT_HOMES)
    for home in candidates:
        sp = Path(home) / "Lib" / "site-packages"
        if sp.is_dir() and str(sp) not in sys.path:
            sys.path.insert(0, str(sp))


_bootstrap_mcp_engine()

try:
    from mcp_server.cache.db import CacheDB
    from mcp_server.tools.waf_bypass import (
        JudicialWAFBypass,
        WAFPermanentBlockError,
    )
    from mcp_server.tools.judicial_search import JudicialSearchClient
    from mcp_server.tools.judicial_doc import JudgmentDocClient
    from mcp_server.config import COURT_CODES
except ImportError as e:  # pragma: no cover
    sys.stderr.write(
        f"無法 import mcp_server（Taiwan Legal DB MCP 引擎）：{e}\n\n"
        f"本工具依賴「Taiwan Legal DB MCP」作為爬蟲引擎。請先：\n"
        f"  1. 安裝 taiwan-legal-db MCP（含 playwright 等相依）。\n"
        f"  2. 用『該 MCP 的 venv python』執行本工具，例如：\n"
        f"       <安裝路徑>\\Scripts\\python.exe harvester\\harvest.py ...\n"
        f"  3. 若安裝路徑非預設，請設定環境變數 TAIWAN_LEGAL_DB_HOME 指向其安裝根目錄。\n"
    )
    sys.exit(2)


# ── 路徑（相對本專案根目錄，不硬編碼絕對路徑）──
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CORPUS_DIR = PROJECT_ROOT / "corpus"
STATE_DIR = PROJECT_ROOT / "state"

# 預設法院（依審級權威序）。可用 --courts 覆寫。
DEFAULT_COURTS = ["最高法院", "臺灣高等法院", "臺灣臺北地方法院"]

# get_by_jid 無內建限速 → 自家禮貌延遲（秒），避免打太兇被 WAF 盯上。
FETCH_DELAY_MIN = 0.6
FETCH_DELAY_MAX = 1.6

# 斷路器：連續失敗達此數，判定司法院正在擋我們，停手不空轉。
# （失敗多為被節流，繼續狂打只會延長封鎖、浪費時間。已抓的會保留，
#   稍後加 --reuse-manifest 重跑即可續抓。）
FETCH_FAIL_CIRCUIT = 8
FETCH_FAIL_DELAY = 1.5  # 每次失敗後的小退避秒數，避免持續猛打

# 單格搜尋硬上限（套件本身也 cap 在 200）。
MAX_PER_CELL = 200

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
# 壓掉套件內部過吵的輸出（含連線瞬斷時的整串 traceback），只留我們自己的進度。
# 連線/WAF 失敗我們的 harvester 會自行重試並用一行清楚訊息回報，不需要套件的 traceback。
logging.getLogger("mcp_server").setLevel(logging.CRITICAL)
for _n in ("httpx", "httpcore", "asyncio"):
    logging.getLogger(_n).setLevel(logging.WARNING)
log = logging.getLogger("harvest")

# 單格搜尋失敗（多為連線瞬斷）時的重試設定。
SEARCH_RETRIES = 3          # 總嘗試次數
SEARCH_RETRY_BACKOFF = 4.0  # 每次重試前等待秒數（線性遞增）


def slugify(s: str) -> str:
    """案由 → 安全資料夾名（保留中文，去掉路徑危險字元）"""
    s = s.strip()
    s = re.sub(r'[\\/:*?"<>|]+', "", s)
    s = re.sub(r"\s+", "-", s)
    return s or "untitled"


def roc_year_now() -> int:
    return datetime.now().year - 1911


# 搜尋結果的 case_id 末尾帶裁判種類，如「…第1990號民事裁定」「…第2214號民事判決」。
# 實體法研究只要「判決」，不要程序性「裁定」。種類無法靠字別推定（台上字別也可能是
# 裁定、再審之訴卻是判決），但 case_id 後綴是司法院官方標示，權威可靠。
_DOC_TYPE_RE = re.compile(r"(判決|裁定)\s*$")


def doc_type_from_case_id(case_id: str) -> str:
    """從 case_id 後綴判定裁判種類：判決 / 裁定 / unknown。"""
    m = _DOC_TYPE_RE.search(case_id or "")
    return m.group(1) if m else "unknown"


# ============================================================
# 搜尋階段：建母體 → manifest.jsonl
# ============================================================

async def _search_cell_with_retry(
    search: JudicialSearchClient, label: str, *,
    keyword: str, main_text: str, case_type: str, court: str, year: int,
) -> dict:
    """單格搜尋 + 有限次重試。

    司法院連打太快時偶發連線瞬斷（ReadError），套件會收斂成 success=False。
    這多半是暫時性的，退避幾秒重試即可救回，避免母體出現缺格。
    重試有上限（SEARCH_RETRIES），不會無限迴圈。
    """
    last = {"success": False}
    for attempt in range(1, SEARCH_RETRIES + 1):
        res = await search.search(
            keyword=keyword, main_text=main_text, case_type=case_type,
            court=court, year_from=year, year_to=year,
            max_results=MAX_PER_CELL,
        )
        if res.get("success"):
            return res
        last = res
        if attempt < SEARCH_RETRIES:
            wait = SEARCH_RETRY_BACKOFF * attempt
            log.info("    %s 第 %d 次失敗，%.0f 秒後重試…", label, attempt, wait)
            await asyncio.sleep(wait)
    return last


async def build_manifest(
    search: JudicialSearchClient,
    *,
    keyword: str,
    main_text: str,
    case_type: str,
    courts: list[str],
    year_from: int,
    year_to: int,
    target: int,
    manifest_path: Path,
    judgments_only: bool = True,
) -> list[dict]:
    """對 (年度 × 法院) 逐格搜尋，跨格以 JID 去重，回傳並寫出 manifest。

    優先序：courts 依傳入順序（審級權威序）、年度由新到舊 → 截到 target 時保留最優先者。
    judgments_only=True 時，依 case_id 後綴在此階段直接濾掉「裁定」（不進清單、不抓全文）。
    """
    years = list(range(year_to, year_from - 1, -1))
    total_cells = len(years) * len(courts)
    log.info(
        "搜尋母體：%d 法院 × %d 年度 = %d 格（每格上限 %d 筆）",
        len(courts), len(years), total_cells, MAX_PER_CELL,
    )

    seen: dict[str, dict] = {}
    truncated_cells: list[str] = []
    ruling_skipped = 0  # 被濾掉的裁定數
    cell_i = 0

    for court in courts:
        for year in years:
            cell_i += 1
            label = f"{court} {year}年"
            res = await _search_cell_with_retry(
                search, label,
                keyword=keyword, main_text=main_text, case_type=case_type,
                court=court, year=year,
            )
            if not res.get("success"):
                log.warning("[%d/%d] %s 搜尋失敗（已重試 %d 次仍不過）：%s",
                            cell_i, total_cells, label, SEARCH_RETRIES,
                            res.get("error", res.get("message", "未知")))
                continue

            rows = res.get("results", [])
            new = cell_rulings = 0
            for r in rows:
                jid = r.get("jid", "")
                if not jid or jid in seen:
                    continue
                dtype = doc_type_from_case_id(r.get("case_id", ""))
                if judgments_only and dtype == "裁定":
                    ruling_skipped += 1
                    cell_rulings += 1
                    continue
                seen[jid] = {
                    "jid": jid,
                    "case_id": r.get("case_id", ""),
                    "doc_type": dtype,
                    "court": r.get("court", ""),
                    "case_type": r.get("case_type", ""),
                    "court_level": r.get("court_level", 0),
                    "date": r.get("date", ""),
                    "cause": r.get("cause", ""),
                    "url": r.get("url", ""),
                    "search_cell": label,
                }
                new += 1

            flag = ""
            if len(rows) >= MAX_PER_CELL:
                truncated_cells.append(label)
                flag = " ⚠破200上限(可能截斷,建議縮小)"
            ruling_note = f"，濾掉裁定 {cell_rulings}" if cell_rulings else ""
            log.info("[%d/%d] %s：%d 筆，新增判決 %d%s（累計 %d）%s",
                     cell_i, total_cells, label, len(rows), new, ruling_note,
                     len(seen), flag)

            # 已達目標就停（節省時間，呼應「搜尋別跑太久」）
            if target and len(seen) >= target:
                log.info("累計 %d ≥ 目標 %d，停止搜尋。", len(seen), target)
                break
        else:
            continue
        break

    manifest = list(seen.values())
    if target and len(manifest) > target:
        manifest = manifest[:target]  # 已按 court 優先序、年度新→舊累積
        log.info("截到目標件數 %d。", target)

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_path.open("w", encoding="utf-8") as f:
        for row in manifest:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    only_note = f"（只收判決，已濾掉裁定 {ruling_skipped} 件）" if judgments_only else "（含判決與裁定）"
    log.info("母體共 %d 件%s，已寫入 %s", len(manifest), only_note, manifest_path)
    if truncated_cells:
        log.warning("以下格達 200 上限、可能未抓全（建議加 --main-text 或縮年度細分）：%s",
                    "、".join(truncated_cells))
    return manifest


def load_manifest(manifest_path: Path) -> list[dict]:
    rows = []
    with manifest_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


# ============================================================
# 抓取階段：逐篇 get_by_jid → corpus/<slug>/<JID>.json
# ============================================================

def _safe_filename(jid: str) -> str:
    """JID 含逗號等字元，轉成安全檔名。"""
    return re.sub(r'[\\/:*?"<>|,]+', "_", jid) + ".json"


async def fetch_corpus(
    doc: JudgmentDocClient,
    manifest: list[dict],
    *,
    slug: str,
) -> dict:
    """讀 manifest，對尚未存在的 JID 取全文寫檔。可續抓：已存在即跳過。"""
    out_dir = CORPUS_DIR / slug
    out_dir.mkdir(parents=True, exist_ok=True)
    errors_path = out_dir / "_errors.log"

    done = skip = error = 0
    consecutive_fail = 0
    aborted = False
    total = len(manifest)
    err_f = errors_path.open("a", encoding="utf-8")
    try:
        for i, row in enumerate(manifest, 1):
            jid = row["jid"]
            out_path = out_dir / _safe_filename(jid)
            if out_path.exists():
                skip += 1
                continue

            failed_reason = None
            res = None
            try:
                res = await doc.get_by_jid(jid)
            except WAFPermanentBlockError:
                failed_reason = "WAF硬擋"
            except Exception as e:  # noqa: BLE001 — 逐筆容錯，不讓單篇炸掉全局
                failed_reason = f"{type(e).__name__}: {e}"
            else:
                if not res.get("success") or not res.get("full_text"):
                    failed_reason = "取全文失敗"

            if failed_reason is not None:
                error += 1
                consecutive_fail += 1
                err_f.write(f"{jid}\t{failed_reason}\n"); err_f.flush()
                log.warning("[%d/%d] %s %s（連續失敗 %d）",
                            i, total, jid, failed_reason, consecutive_fail)
                if consecutive_fail >= FETCH_FAIL_CIRCUIT:
                    aborted = True
                    log.error(
                        "連續失敗 %d 次，研判司法院正在節流／封鎖此 IP，停止抓取。\n"
                        "    已抓的語料皆保留。請等一段時間（數分鐘~約一小時）後，\n"
                        "    用相同指令加 --reuse-manifest 續抓未完成的部分。",
                        consecutive_fail,
                    )
                    break
                await asyncio.sleep(FETCH_FAIL_DELAY)
                continue

            consecutive_fail = 0

            record = {
                "jid": jid,
                "case_id": res.get("case_id") or row.get("case_id", ""),
                "doc_type": row.get("doc_type")
                or doc_type_from_case_id(res.get("case_id", "")),
                "court": res.get("court") or row.get("court", ""),
                "case_type": row.get("case_type", ""),
                "date": res.get("date") or row.get("date", ""),
                "cause": res.get("cause") or row.get("cause", ""),
                "judges": res.get("judges", []),
                "parties": res.get("parties", {}),
                "main_text": res.get("main_text", ""),
                "facts": res.get("facts", ""),
                "reasoning": res.get("reasoning", ""),
                "cited_statutes": res.get("cited_statutes", []),
                "cited_cases": res.get("cited_cases", []),
                "full_text": res.get("full_text", ""),
                "source_url": res.get("source_url", ""),
                "harvested_at": datetime.now().isoformat(timespec="seconds"),
            }
            tmp = out_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            tmp.replace(out_path)  # atomic：中斷不會留半截檔
            done += 1
            cached = " (快取)" if res.get("cached") else ""
            log.info("[%d/%d] ✓ %s %s%s", i, total, record["court"],
                     record["case_id"] or jid, cached)

            if not res.get("cached"):
                await asyncio.sleep(random.uniform(FETCH_DELAY_MIN, FETCH_DELAY_MAX))
    finally:
        err_f.close()

    return {"done": done, "skip": skip, "error": error,
            "total": total, "aborted": aborted}


# ============================================================
# 主流程
# ============================================================

async def run(args) -> int:
    keyword = args.keyword or ""
    main_text = args.main_text or ""
    if not keyword and not main_text:
        log.error("至少要給 --keyword 或 --main-text 其一。")
        return 2

    courts = args.courts or DEFAULT_COURTS
    unknown = [c for c in courts if c not in COURT_CODES]
    if unknown:
        log.error("未知法院名稱（需用全名）：%s\n可用：%s",
                  "、".join(unknown), "、".join(COURT_CODES.keys()))
        return 2

    year_to = args.year_to or roc_year_now()
    year_from = args.year_from or (year_to - 4)  # 預設近 5 年，避免一抓無底洞
    if year_from > year_to:
        log.error("year_from(%d) 不可大於 year_to(%d)", year_from, year_to)
        return 2

    judgments_only = not args.include_rulings

    slug = slugify(args.slug or keyword or main_text)
    manifest_path = STATE_DIR / f"{slug}.manifest.jsonl"

    log.info("案由 slug：%s", slug)
    log.info("關鍵字=%r 主文=%r 案類=%r 年度=%d~%d 法院=%s 目標=%s 裁判種類=%s",
             keyword, main_text, args.case_type or "(全部)",
             year_from, year_to, "、".join(courts), args.target or "(不限)",
             "只收判決" if judgments_only else "判決+裁定")

    # ── 初始化：快取 DB 導去本專案 state/，不污染共用安裝 ──
    cache = CacheDB(db_path=STATE_DIR / "legal_cache.db")
    await cache.initialize()
    waf = JudicialWAFBypass()
    log.info("WAF 暖機中（首次或 cookie 過期會跑 Playwright，約數秒~數十秒）…")
    await waf.ensure_ready()
    search = JudicialSearchClient(cache, waf)
    doc = JudgmentDocClient(cache, waf)

    try:
        # 搜尋階段（除非 --reuse-manifest 且檔案已存在）
        if args.reuse_manifest and manifest_path.exists():
            manifest = load_manifest(manifest_path)
            log.info("沿用既有 manifest（%d 件）：%s", len(manifest), manifest_path)
        else:
            t0 = time.time()
            manifest = await build_manifest(
                search,
                keyword=keyword, main_text=main_text, case_type=args.case_type or "",
                courts=courts, year_from=year_from, year_to=year_to,
                target=args.target, manifest_path=manifest_path,
                judgments_only=judgments_only,
            )
            log.info("搜尋階段耗時 %.1fs", time.time() - t0)

        if args.dry_run:
            log.info("--dry-run：只建母體，不抓全文。母體 %d 件。", len(manifest))
            _print_population_summary(manifest)
            return 0

        if not manifest:
            log.warning("母體為 0，無可抓取。")
            return 0

        # 抓取階段
        t1 = time.time()
        stats = await fetch_corpus(doc, manifest, slug=slug)
        log.info("抓取階段耗時 %.1fs", time.time() - t1)
        verb = "中止（斷路器觸發）" if stats.get("aborted") else "完成"
        log.info("%s：新增 %d、跳過(已存在) %d、失敗 %d / 共 %d 件 → %s",
                 verb, stats["done"], stats["skip"], stats["error"], stats["total"],
                 CORPUS_DIR / slug)
        if stats["error"]:
            log.info("失敗清單見：%s", CORPUS_DIR / slug / "_errors.log")
        if stats.get("aborted"):
            log.info("續抓指令：相同指令加 --reuse-manifest")
    finally:
        await search.close()
        await doc.close()
        await cache.close()

    return 0


def _print_population_summary(manifest: list[dict]) -> None:
    by_court: dict[str, int] = {}
    by_year: dict[str, int] = {}
    by_type: dict[str, int] = {}
    for r in manifest:
        by_court[r.get("court", "?")] = by_court.get(r.get("court", "?"), 0) + 1
        y = (r.get("date", "") or "?")[:3]
        by_year[y] = by_year.get(y, 0) + 1
        t = r.get("doc_type", "unknown")
        by_type[t] = by_type.get(t, 0) + 1
    log.info("母體分布（種類）：%s",
             "、".join(f"{k}:{v}" for k, v in sorted(by_type.items(), key=lambda x: -x[1])))
    log.info("母體分布（法院）：%s",
             "、".join(f"{k}:{v}" for k, v in sorted(by_court.items(), key=lambda x: -x[1])))
    log.info("母體分布（年度）：%s",
             "、".join(f"{k}:{v}" for k, v in sorted(by_year.items(), reverse=True)))


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="司法院判決大量抓取 CLI（工具一）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--keyword", help="全文檢索關鍵字（主題式），如「分管協議」")
    p.add_argument("--main-text", dest="main_text",
                   help="裁判主文關鍵字（jud_jmain），結構化篩選輸贏方")
    p.add_argument("--case-type", dest="case_type", default="",
                   help="案件類型：民事/刑事/行政/懲戒（預設全部）")
    p.add_argument("--courts", nargs="+",
                   help=f"法院全名（可多個）。預設：{ '、'.join(DEFAULT_COURTS) }")
    p.add_argument("--year-from", dest="year_from", type=int, default=0,
                   help="起始民國年（預設 year-to 往前 5 年）")
    p.add_argument("--year-to", dest="year_to", type=int, default=0,
                   help="截止民國年（預設今年）")
    p.add_argument("--target", type=int, default=0,
                   help="目標件數上限（達到即停搜尋；預設不限）")
    p.add_argument("--include-rulings", dest="include_rulings", action="store_true",
                   help="連裁定一起收（預設只收判決，適合實體法研究）")
    p.add_argument("--slug", help="語料夾名稱（預設取自 keyword）")
    p.add_argument("--dry-run", dest="dry_run", action="store_true",
                   help="只搜尋估母體、不抓全文")
    p.add_argument("--reuse-manifest", dest="reuse_manifest", action="store_true",
                   help="若已有 manifest 則跳過搜尋，直接續抓全文")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        log.warning("使用者中斷。已寫入的語料與 manifest 保留，可加 --reuse-manifest 續抓。")
        return 130


if __name__ == "__main__":
    sys.exit(main())

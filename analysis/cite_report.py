#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""cite_report — 引用判解報告 CLI。

輸入判決字號或見解文句，查詢引用該字號／包含該文句的裁判，
取全文後摘出命中段落，輸出 Markdown 與 HTML 報告。
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import logging
import os
import random
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


if sys.platform == "win32" and not sys.flags.utf8_mode:
    os.environ["PYTHONUTF8"] = "1"
    os.execv(sys.executable, [sys.executable, *sys.argv])

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = PROJECT_ROOT / "state"
REPORTS_DIR = PROJECT_ROOT / "reports"
HTML_DIR = PROJECT_ROOT / "reports_html"

DEFAULT_COURTS = ["最高法院", "臺灣高等法院"]
DEFAULT_MCP_HOMES = [r"C:\Users\user\mcp-taiwan-legal-db\.venv"]
DOC_DELAY_MIN = 0.5
DOC_DELAY_MAX = 1.2
FETCH_FAIL_CIRCUIT = 5


def _bootstrap_mcp_engine() -> None:
    candidates: list[str] = []
    env_home = os.environ.get("TAIWAN_LEGAL_DB_HOME", "").strip()
    if env_home:
        candidates.append(env_home)
    candidates.extend(DEFAULT_MCP_HOMES)
    for home in candidates:
        source_root = Path(home).parent
        if (source_root / "mcp_server").is_dir() and str(source_root) not in sys.path:
            sys.path.insert(0, str(source_root))
        site_packages = Path(home) / "Lib" / "site-packages"
        if site_packages.is_dir() and str(site_packages) not in sys.path:
            sys.path.insert(0, str(site_packages))


_bootstrap_mcp_engine()

try:
    from mcp_server.cache.db import CacheDB
    from mcp_server.config import COURT_CODES
    from mcp_server.tools.judicial_doc import JudgmentDocClient
    from mcp_server.tools.judicial_search import JudicialSearchClient
    from mcp_server.tools.waf_bypass import JudicialWAFBypass
except ImportError as exc:  # pragma: no cover
    sys.stderr.write(
        f"無法 import mcp_server（Taiwan Legal DB MCP 引擎）：{exc}\n\n"
        "請用該 MCP 的 venv python 執行，例如：\n"
        r"  C:\Users\user\mcp-taiwan-legal-db\.venv\Scripts\python.exe analysis\cite_report.py ..."
        "\n"
    )
    sys.exit(2)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("mcp_server").setLevel(logging.CRITICAL)
for _name in ("httpx", "httpcore", "asyncio"):
    logging.getLogger(_name).setLevel(logging.WARNING)
log = logging.getLogger("cite_report")


@dataclass(frozen=True)
class Citation:
    year: str
    case_word: str
    number: str


@dataclass
class HitDoc:
    row: dict
    source_url: str
    passages: list[str]


_DOC_TYPE_RE = re.compile(r"(判決|裁定)\s*$")


def slugify(value: str) -> str:
    value = value.strip()
    value = re.sub(r'[\\/:*?"<>|]+', "", value)
    value = re.sub(r"\s+", "-", value)
    return value[:80] or "引用判解"


def doc_type_from_case_id(case_id: str) -> str:
    match = _DOC_TYPE_RE.search(case_id or "")
    return match.group(1) if match else "unknown"


def parse_citation(text: str) -> Citation | None:
    compact = re.sub(r"\s+", "", text)
    compact = re.sub(r"(民事|刑事|行政)?(判決|裁定)$", "", compact)
    compact = compact.replace("臺", "台")
    match = re.fullmatch(
        r"(?P<year>\d{2,3})(?:年度?|年)?(?P<word>[\u4e00-\u9fff]+?)(?:字)?"
        r"(?:第)?(?P<number>[0-9A-Za-z\-]+)(?:號)?",
        compact,
    )
    if not match:
        return None
    return Citation(
        year=match.group("year"),
        case_word=match.group("word"),
        number=match.group("number"),
    )


def citation_variants(citation: Citation) -> list[str]:
    variants: list[str] = []
    words = {citation.case_word, citation.case_word.replace("台", "臺")}
    for word in words:
        variants.extend(
            [
                f"{citation.year}年度{word}字第{citation.number}號",
                f"{citation.year}年{word}字第{citation.number}號",
            ]
        )
    return dedupe(variants)


def citation_search_variants(citation: Citation) -> list[str]:
    variants = citation_variants(citation)
    words = {citation.case_word, citation.case_word.replace("台", "臺")}
    variants.extend(f"{citation.year}{word}{citation.number}" for word in words)
    return dedupe(variants)


def dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def build_keyword(query: str, extra_terms: list[str]) -> tuple[str, list[str], Citation | None]:
    citation = parse_citation(query)
    if citation:
        terms = citation_variants(citation)
        return "+".join(citation_search_variants(citation)), dedupe([*terms, *extra_terms]), citation

    terms = text_terms(query)
    if extra_terms:
        terms = dedupe([*terms, *extra_terms])
    return query, terms, None


def text_terms(text: str) -> list[str]:
    parts = [
        part.strip()
        for part in re.split(r"[\s，,。；;：:、（）()\[\]「」『』]+", text)
        if len(part.strip()) >= 2
    ]
    return dedupe(parts or [text.strip()])


def match_citation_row(row: dict, citation: Citation | None) -> bool:
    if not citation:
        return True
    case_id = (row.get("case_id") or "").replace("臺", "台")
    return (
        citation.year in case_id
        and citation.case_word in case_id
        and citation.number in case_id
    )


def normalize_paragraph(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def split_paragraphs(text: str) -> list[str]:
    raw = re.split(r"\n\s*\n|(?<=。)\s*\n+", text or "")
    return [normalize_paragraph(p) for p in raw if normalize_paragraph(p)]


def compact_text(text: str) -> str:
    """比對用文字：去除全部空白，並統一臺／台。"""
    return re.sub(r"\s+", "", text or "").replace("臺", "台")


def compact_with_map(text: str) -> tuple[str, list[int]]:
    compact: list[str] = []
    index_map: list[int] = []
    for index, char in enumerate(text or ""):
        if char.isspace():
            continue
        compact.append("台" if char == "臺" else char)
        index_map.append(index)
    return "".join(compact), index_map


def is_ascii_alnum(char: str) -> bool:
    return bool(re.fullmatch(r"[0-9A-Za-z]", char or ""))


def compact_positions(paragraph: str, terms: list[str]) -> list[int]:
    compact, index_map = compact_with_map(paragraph)
    positions: list[int] = []
    for term in terms:
        needle = compact_text(term)
        if not needle:
            continue
        start = 0
        while True:
            pos = compact.find(needle, start)
            if pos < 0:
                break
            before = compact[pos - 1] if pos > 0 else ""
            after_pos = pos + len(needle)
            after = compact[after_pos] if after_pos < len(compact) else ""
            if not (
                is_ascii_alnum(needle[-1:])
                and is_ascii_alnum(after)
                or is_ascii_alnum(needle[:1])
                and is_ascii_alnum(before)
            ):
                positions.append(index_map[pos])
            start = pos + 1
    return positions


def find_passages(
    full_text: str,
    terms: list[str],
    *,
    context_chars: int,
    passages_per_doc: int,
) -> list[str]:
    hits: list[str] = []
    for paragraph in split_paragraphs(full_text):
        positions = compact_positions(paragraph, terms)
        if not positions:
            continue
        start_at = min(positions)
        if len(paragraph) > context_chars * 2:
            left = max(0, start_at - context_chars)
            right = min(len(paragraph), start_at + context_chars)
            excerpt = paragraph[left:right]
            if left:
                excerpt = "…" + excerpt
            if right < len(paragraph):
                excerpt += "…"
        else:
            excerpt = paragraph
        hits.append(excerpt)
        if len(hits) >= passages_per_doc:
            break
    return hits


def term_regex(term: str) -> re.Pattern[str] | None:
    compact = compact_text(term)
    if not compact:
        return None
    body = r"\s*".join("[台臺]" if char == "台" else re.escape(char) for char in compact)
    prefix = r"(?<![0-9A-Za-z])" if is_ascii_alnum(compact[:1]) else ""
    suffix = r"(?![0-9A-Za-z])" if is_ascii_alnum(compact[-1:]) else ""
    return re.compile(prefix + body + suffix)


def highlight_text(text: str, terms: list[str]) -> str:
    spans: list[tuple[int, int]] = []
    for term in sorted(terms, key=lambda item: len(compact_text(item)), reverse=True):
        pattern = term_regex(term)
        if not pattern:
            continue
        for match in pattern.finditer(text):
            spans.append(match.span())

    selected: list[tuple[int, int]] = []
    for start, end in sorted(spans, key=lambda span: (span[0], -(span[1] - span[0]))):
        if start == end:
            continue
        if selected and start < selected[-1][1]:
            continue
        selected.append((start, end))

    out: list[str] = []
    cursor = 0
    for start, end in selected:
        out.append(html.escape(text[cursor:start]))
        out.append(f'<mark class="hit">{html.escape(text[start:end])}</mark>')
        cursor = end
    out.append(html.escape(text[cursor:]))
    return "".join(out)


def selected_courts(args) -> list[str]:
    return [] if args.all_courts else args.courts


async def search_rows(search: JudicialSearchClient, args, keyword: str) -> tuple[list[dict], list[str]]:
    courts = selected_courts(args) or [""]
    seen: dict[str, dict] = {}
    warnings: list[str] = []
    max_results = max(1, min(args.max_results, 500))

    for court in courts:
        label = court or "全部法院"
        log.info("搜尋：%s", label)
        result = await search.search(
            keyword=keyword,
            court=court,
            case_type=args.case_type or "",
            year_from=args.year_from,
            year_to=args.year_to,
            max_results=max_results,
            search_system=args.search_system,
        )
        if not result.get("success"):
            warnings.append(f"{label} 搜尋失敗：{result.get('error') or result.get('message') or '未知錯誤'}")
            continue

        rows = result.get("results", [])
        total_count = result.get("total_count") or len(rows)
        if total_count > len(rows):
            warnings.append(f"{label} 僅取得 {len(rows)}/{total_count} 筆；可能需縮小年份或法院。")

        for row in rows:
            jid = row.get("jid", "")
            if not jid or jid in seen:
                continue
            dtype = doc_type_from_case_id(row.get("case_id", ""))
            if args.exclude_rulings and dtype == "裁定":
                continue
            row["doc_type"] = dtype
            seen[jid] = row

    return list(seen.values()), warnings


async def search_judgment_itself(search: JudicialSearchClient, args, citation: Citation) -> tuple[list[dict], list[str]]:
    seen: dict[str, dict] = {}
    warnings: list[str] = []
    max_results = max(1, min(args.max_results, 500))
    for court in selected_courts(args) or [""]:
        result = await search.search(
            court=court,
            case_type=args.case_type or "",
            year_from=args.year_from or int(citation.year),
            year_to=args.year_to or int(citation.year),
            case_word=citation.case_word,
            case_number=citation.number,
            max_results=max_results,
            search_system=args.search_system,
        )
        if not result.get("success"):
            label = court or "全部法院"
            warnings.append(f"{label} 精確查詢失敗：{result.get('error') or result.get('message') or '未知錯誤'}")
            continue
        for row in result.get("results", []):
            jid = row.get("jid", "")
            if jid and jid not in seen and match_citation_row(row, citation):
                row["doc_type"] = doc_type_from_case_id(row.get("case_id", ""))
                seen[jid] = row
    return list(seen.values()), warnings


async def fetch_hits(doc: JudgmentDocClient, rows: list[dict], args, terms: list[str]) -> tuple[list[HitDoc], list[str]]:
    hits: list[HitDoc] = []
    warnings: list[str] = []
    consecutive_fail = 0
    total = len(rows)
    for i, row in enumerate(rows, 1):
        jid = row["jid"]
        log.info("[%d/%d] 取全文：%s", i, total, row.get("case_id") or jid)
        try:
            result = await doc.get_by_jid(jid)
        except Exception as exc:  # noqa: BLE001 - 逐筆容錯，避免單件毀掉整份報告
            consecutive_fail += 1
            warnings.append(f"{row.get('case_id') or jid} 取全文例外：{type(exc).__name__}: {exc}")
            if consecutive_fail >= FETCH_FAIL_CIRCUIT:
                warnings.append(f"連續取全文失敗 {FETCH_FAIL_CIRCUIT} 件，已停止後續抓取。")
                break
            await asyncio.sleep(DOC_DELAY_MAX)
            continue
        if not result.get("success") or not result.get("full_text"):
            consecutive_fail += 1
            warnings.append(f"{row.get('case_id') or jid} 取全文失敗")
            if consecutive_fail >= FETCH_FAIL_CIRCUIT:
                warnings.append(f"連續取全文失敗 {FETCH_FAIL_CIRCUIT} 件，已停止後續抓取。")
                break
            continue

        consecutive_fail = 0
        full_text = result.get("full_text", "")
        passages = find_passages(
            full_text,
            terms,
            context_chars=args.context_chars,
            passages_per_doc=args.passages_per_doc,
        )
        if args.judgment and not passages:
            passages = [normalize_paragraph(full_text)[: args.context_chars * 2]]
        if not passages:
            warnings.append(f"{row.get('case_id') or jid} 已取全文，但未能定位命中段落；請開來源連結檢視。")

        hits.append(HitDoc(row=row, source_url=result.get("source_url", ""), passages=passages))
        if not result.get("cached"):
            await asyncio.sleep(random.uniform(DOC_DELAY_MIN, DOC_DELAY_MAX))
    return hits, warnings


def markdown_report(args, keyword: str, terms: list[str], hits: list[HitDoc], warnings: list[str]) -> str:
    generated_at = datetime.now().isoformat(timespec="seconds")
    lines = [
        f"# {args.query} — 引用判解報告",
        "",
        f"- 產生時間：{generated_at}",
        f"- 查詢模式：{'查判決本身' if args.judgment else '查引用或包含該文字的裁判'}",
        f"- 搜尋關鍵字：`{keyword}`",
        f"- 法院：{'全部法院' if args.all_courts else '、'.join(args.courts)}",
        f"- 命中裁判：{len(hits)} 件",
        "",
    ]
    if warnings:
        lines.extend(["## 注意事項", ""])
        lines.extend(f"- {warning}" for warning in warnings)
        lines.append("")

    lines.extend(["## 命中段落", ""])
    if not hits:
        lines.append("查無命中段落。")
    for index, hit in enumerate(hits, 1):
        row = hit.row
        lines.extend(
            [
                f"### {index}. {row.get('court','')} {row.get('case_id','')}",
                "",
                f"- 日期：{row.get('date','')}",
                f"- 案由：{row.get('cause','')}",
                f"- JID：`{row.get('jid','')}`",
                f"- 來源：{hit.source_url or row.get('url','')}",
                "",
            ]
        )
        for passage in hit.passages:
            lines.extend([f"> {passage}", ""])
        if not hit.passages:
            lines.extend(["> 未能定位命中段落，請開來源連結檢視。", ""])
    lines.extend(["## 高亮詞", "", "、".join(terms) or "（無）", ""])
    return "\n".join(lines)


def html_report(args, keyword: str, terms: list[str], hits: list[HitDoc], warnings: list[str]) -> str:
    warning_html = ""
    if warnings:
        warning_html = "<section><h2>注意事項</h2><ul>" + "".join(
            f"<li>{html.escape(w)}</li>" for w in warnings
        ) + "</ul></section>"
    hit_html = []
    for index, hit in enumerate(hits, 1):
        row = hit.row
        if hit.passages:
            passages = "\n".join(
                f"<blockquote>{highlight_text(p, terms)}</blockquote>" for p in hit.passages
            )
        else:
            passages = "<blockquote>未能定位命中段落，請開來源連結檢視。</blockquote>"
        source = html.escape(hit.source_url or row.get("url", ""))
        link = f'<a href="{source}">{source}</a>' if source else ""
        hit_html.append(
            f"""
<article>
  <h2>{index}. {html.escape(row.get('court',''))} {html.escape(row.get('case_id',''))}</h2>
  <div class="meta">日期：{html.escape(row.get('date',''))}　案由：{html.escape(row.get('cause',''))}<br>
  JID：<code>{html.escape(row.get('jid',''))}</code><br>{link}</div>
  {passages}
</article>"""
        )
    if not hit_html:
        hit_html.append("<p>查無命中段落。</p>")

    return f"""<!doctype html>
<html lang="zh-Hant-TW">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(args.query)} — 引用判解報告</title>
  <style>
    body {{ margin: 0; color: #202124; background: #fafafa; font-family: "Noto Sans TC", "Microsoft JhengHei", Arial, sans-serif; line-height: 1.75; }}
    main {{ max-width: 980px; margin: 0 auto; padding: 36px 28px 64px; background: white; min-height: 100vh; }}
    h1 {{ font-size: 30px; margin: 0 0 18px; border-bottom: 3px solid #345f72; padding-bottom: 12px; }}
    h2 {{ margin-top: 34px; font-size: 21px; color: #263238; }}
    .meta {{ color: #5f6368; font-size: 14px; margin: 8px 0 14px; }}
    .summary {{ border: 1px solid #d7dde2; background: #f6f8fa; padding: 14px 16px; margin: 18px 0 24px; }}
    blockquote {{ margin: 14px 0; padding: 13px 17px; border-left: 5px solid #345f72; background: #f7fbfc; }}
    .hit {{ background: #fff176; color: #b00020; padding: 0 2px; }}
    code {{ background: #f1f3f4; padding: 1px 5px; border-radius: 4px; }}
    a {{ color: #0b57d0; overflow-wrap: anywhere; }}
    @media print {{ main {{ max-width: none; padding: 0; }} a {{ color: inherit; text-decoration: none; }} }}
  </style>
</head>
<body>
<main>
  <h1>{html.escape(args.query)} — 引用判解報告</h1>
  <section class="summary">
    產生時間：{datetime.now().isoformat(timespec="seconds")}<br>
    查詢模式：{html.escape('查判決本身' if args.judgment else '查引用或包含該文字的裁判')}<br>
    搜尋關鍵字：<code>{html.escape(keyword)}</code><br>
    法院：{html.escape('全部法院' if args.all_courts else '、'.join(args.courts))}<br>
    命中裁判：{len(hits)} 件
  </section>
  {warning_html}
  {''.join(hit_html)}
</main>
</body>
</html>
"""


def write_reports(args, keyword: str, terms: list[str], hits: list[HitDoc], warnings: list[str]) -> tuple[Path, Path]:
    slug = slugify(args.slug or args.query)
    REPORTS_DIR.mkdir(exist_ok=True)
    HTML_DIR.mkdir(exist_ok=True)
    md_path = REPORTS_DIR / f"{slug}-引用判解報告.md"
    html_path = HTML_DIR / f"{slug}-引用判解報告.html"
    md_path.write_text(markdown_report(args, keyword, terms, hits, warnings), encoding="utf-8", newline="\n")
    html_path.write_text(html_report(args, keyword, terms, hits, warnings), encoding="utf-8", newline="\n")
    return md_path, html_path


async def run(args) -> int:
    unknown = [court for court in selected_courts(args) if court and court not in COURT_CODES]
    if unknown:
        log.error("未知法院名稱（需用全名）：%s", "、".join(unknown))
        return 2

    keyword, terms, citation = build_keyword(args.query, args.term)
    if args.judgment and not citation:
        log.error("--judgment 僅支援可解析字號，例如 99台上222。")
        return 2

    STATE_DIR.mkdir(exist_ok=True)
    cache = CacheDB(db_path=STATE_DIR / "legal_cache.db")
    await cache.initialize()
    waf = JudicialWAFBypass()
    log.info("WAF 暖機中…")
    await waf.ensure_ready()
    search = JudicialSearchClient(cache, waf)
    doc = JudgmentDocClient(cache, waf)

    try:
        if args.judgment:
            rows, warnings = await search_judgment_itself(search, args, citation)  # type: ignore[arg-type]
        else:
            rows, warnings = await search_rows(search, args, keyword)
        log.info("搜尋結果：%d 件，開始取全文", len(rows))
        hits, fetch_warnings = await fetch_hits(doc, rows, args, terms)
        warnings.extend(fetch_warnings)
        md_path, html_path = write_reports(args, keyword, terms, hits, warnings)
    finally:
        await search.close()
        await doc.close()
        await cache.close()

    log.info("已寫出 Markdown：%s", md_path)
    log.info("已寫出 HTML：%s", html_path)
    print(json.dumps({"markdown": str(md_path), "html": str(html_path), "hits": len(hits)}, ensure_ascii=False))
    return 0


def parse_args(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description="產生引用判解 Markdown + HTML 報告")
    parser.add_argument("query", help="判決字號（如 99台上222）或見解文句")
    parser.add_argument("--judgment", action="store_true", help="查判決本身；預設為查引用或包含該文字的裁判")
    parser.add_argument("--term", action="append", default=[], help="額外命中／高亮詞，可重複指定")
    parser.add_argument("--courts", nargs="+", default=DEFAULT_COURTS, help="法院全名；預設最高法院、臺灣高等法院")
    parser.add_argument("--all-courts", action="store_true", help="不指定法院，查全部法院")
    parser.add_argument("--case-type", default="", help="案件類型：民事/刑事/行政/懲戒（預設全部）")
    parser.add_argument("--year-from", type=int, default=0, help="起始民國年（預設不限）")
    parser.add_argument("--year-to", type=int, default=0, help="截止民國年（預設不限）")
    parser.add_argument("--max-results", type=int, default=50, help="每法院最多搜尋件數（預設 50）")
    parser.add_argument("--exclude-rulings", action="store_true", help="排除裁定（預設納入裁定）")
    parser.add_argument("--search-system", choices=["auto", "regular", "easy", "both"], default="auto", help="引擎查詢系統")
    parser.add_argument("--context-chars", type=int, default=260, help="每段命中前後擷取字數")
    parser.add_argument("--passages-per-doc", type=int, default=3, help="每件裁判最多摘錄段落數")
    parser.add_argument("--slug", default="", help="輸出檔名 slug，預設取 query")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        log.warning("使用者中斷。")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

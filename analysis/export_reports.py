"""export_reports — 將 reports/*.md 匯出成適合閱讀與列印的 HTML。

本工具保留 Markdown 作為可追蹤原始報告，同時產出瀏覽器可開啟的單一閱讀版：
reports_html/<slug>-閱讀版.html
"""

from __future__ import annotations

import argparse
import html
import re
import sys
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPORTS_DIR = PROJECT_ROOT / "reports"
OUT_DIR = PROJECT_ROOT / "reports_html"


@dataclass
class TocItem:
    level: int
    title: str
    anchor: str


def slugify_anchor(text: str, used: set[str]) -> str:
    base = re.sub(r"\s+", "-", text.strip())
    base = re.sub(r"[^\w\u4e00-\u9fff\-]+", "", base, flags=re.UNICODE).strip("-")
    if not base:
        base = "section"
    anchor = base
    counter = 2
    while anchor in used:
        anchor = f"{base}-{counter}"
        counter += 1
    used.add(anchor)
    return anchor


def inline_md(text: str) -> str:
    escaped = html.escape(text)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", escaped)
    escaped = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', escaped)
    return escaped


def split_table_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def is_table_separator(line: str) -> bool:
    cells = split_table_row(line)
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells)


def paragraph_to_html(lines: list[str]) -> str:
    text = " ".join(line.strip() for line in lines if line.strip())
    return f"<p>{inline_md(text)}</p>"


def markdown_to_html(markdown: str, toc: list[TocItem], used_anchors: set[str]) -> str:
    lines = markdown.splitlines()
    out: list[str] = []
    paragraph: list[str] = []
    in_ul = False
    in_ol = False
    in_blockquote = False

    def flush_paragraph() -> None:
        nonlocal paragraph
        if paragraph:
            out.append(paragraph_to_html(paragraph))
            paragraph = []

    def close_lists() -> None:
        nonlocal in_ul, in_ol
        if in_ul:
            out.append("</ul>")
            in_ul = False
        if in_ol:
            out.append("</ol>")
            in_ol = False

    def close_blockquote() -> None:
        nonlocal in_blockquote
        if in_blockquote:
            out.append("</blockquote>")
            in_blockquote = False

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            flush_paragraph()
            close_lists()
            close_blockquote()
            i += 1
            continue

        if stripped == "---":
            flush_paragraph()
            close_lists()
            close_blockquote()
            out.append("<hr>")
            i += 1
            continue

        heading = re.match(r"^(#{1,6})\s+(.+)$", stripped)
        if heading:
            flush_paragraph()
            close_lists()
            close_blockquote()
            level = len(heading.group(1))
            title = heading.group(2).strip()
            anchor = slugify_anchor(title, used_anchors)
            toc.append(TocItem(level=level, title=title, anchor=anchor))
            out.append(f'<h{level} id="{html.escape(anchor)}">{inline_md(title)}</h{level}>')
            i += 1
            continue

        if stripped.startswith(">"):
            flush_paragraph()
            close_lists()
            if not in_blockquote:
                out.append("<blockquote>")
                in_blockquote = True
            out.append(f"<p>{inline_md(stripped.lstrip('>').strip())}</p>")
            i += 1
            continue

        if "|" in stripped and i + 1 < len(lines) and is_table_separator(lines[i + 1].strip()):
            flush_paragraph()
            close_lists()
            close_blockquote()
            headers = split_table_row(stripped)
            out.append("<div class=\"table-wrap\"><table><thead><tr>")
            for cell in headers:
                out.append(f"<th>{inline_md(cell)}</th>")
            out.append("</tr></thead><tbody>")
            i += 2
            while i < len(lines) and "|" in lines[i].strip() and lines[i].strip():
                out.append("<tr>")
                for cell in split_table_row(lines[i]):
                    out.append(f"<td>{inline_md(cell)}</td>")
                out.append("</tr>")
                i += 1
            out.append("</tbody></table></div>")
            continue

        unordered = re.match(r"^-\s+(.*)$", stripped)
        ordered = re.match(r"^(\d+)\.\s+(.*)$", stripped)
        if unordered:
            flush_paragraph()
            close_blockquote()
            if in_ol:
                out.append("</ol>")
                in_ol = False
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            item = unordered.group(1)
            checkbox = re.match(r"^\[( |x|X)\]\s+(.*)$", item)
            if checkbox:
                checked = " checked" if checkbox.group(1).lower() == "x" else ""
                out.append(
                    f'<li class="check-item"><input type="checkbox" disabled{checked}>'
                    f"<span>{inline_md(checkbox.group(2))}</span></li>"
                )
            else:
                out.append(f"<li>{inline_md(item)}</li>")
            i += 1
            continue
        if ordered:
            flush_paragraph()
            close_blockquote()
            if in_ul:
                out.append("</ul>")
                in_ul = False
            if not in_ol:
                out.append("<ol>")
                in_ol = True
            out.append(f"<li>{inline_md(ordered.group(2))}</li>")
            i += 1
            continue

        close_lists()
        close_blockquote()
        paragraph.append(stripped)
        i += 1

    flush_paragraph()
    close_lists()
    close_blockquote()
    return "\n".join(out)


def css() -> str:
    return """
:root {
  color-scheme: light;
  --paper: #fbfaf7;
  --ink: #1f2428;
  --muted: #667085;
  --line: #d8d3c7;
  --accent: #245c7a;
  --accent-soft: #e5f0f4;
  --warn-soft: #fff4d6;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--paper);
  color: var(--ink);
  font-family: "Noto Sans TC", "Microsoft JhengHei", "PingFang TC", Arial, sans-serif;
  line-height: 1.75;
}
.layout {
  display: grid;
  grid-template-columns: minmax(220px, 280px) minmax(0, 1fr);
  min-height: 100vh;
}
nav {
  position: sticky;
  top: 0;
  height: 100vh;
  overflow: auto;
  padding: 28px 20px;
  border-right: 1px solid var(--line);
  background: #f3f1ea;
}
nav h2 {
  margin: 0 0 16px;
  font-size: 18px;
}
nav a {
  display: block;
  margin: 7px 0;
  color: var(--accent);
  text-decoration: none;
  font-size: 14px;
}
nav a:hover { text-decoration: underline; }
nav .level-1 { font-weight: 700; margin-top: 14px; }
nav .level-2 { padding-left: 10px; }
nav .level-3 { padding-left: 22px; color: var(--muted); }
main {
  max-width: 1080px;
  padding: 42px 56px 72px;
}
.meta {
  margin-bottom: 28px;
  padding: 14px 18px;
  border: 1px solid var(--line);
  background: #fffdf8;
}
h1, h2, h3, h4 {
  line-height: 1.35;
  letter-spacing: 0;
}
h1 {
  margin: 0 0 18px;
  font-size: 32px;
  border-bottom: 3px solid var(--accent);
  padding-bottom: 12px;
}
h2 {
  margin-top: 42px;
  padding-top: 12px;
  font-size: 24px;
  border-top: 1px solid var(--line);
}
h3 { margin-top: 30px; font-size: 20px; color: #333; }
p { margin: 12px 0; }
blockquote {
  margin: 18px 0;
  padding: 12px 18px;
  border-left: 5px solid var(--accent);
  background: var(--accent-soft);
}
blockquote p { margin: 4px 0; }
hr {
  border: 0;
  border-top: 1px solid var(--line);
  margin: 28px 0;
}
.table-wrap {
  overflow-x: auto;
  margin: 18px 0 24px;
  border: 1px solid var(--line);
  background: white;
}
table {
  width: 100%;
  border-collapse: collapse;
  min-width: 680px;
}
th, td {
  padding: 10px 12px;
  border-bottom: 1px solid var(--line);
  border-right: 1px solid var(--line);
  vertical-align: top;
}
th {
  background: #ece8dd;
  text-align: left;
  white-space: nowrap;
}
tr:nth-child(even) td { background: #fffdf8; }
ul, ol { padding-left: 1.45rem; }
li { margin: 6px 0; }
.check-item {
  list-style: none;
  display: flex;
  gap: 10px;
  margin-left: -1.2rem;
}
.check-item input {
  width: 18px;
  height: 18px;
  margin-top: 5px;
  accent-color: var(--accent);
}
code {
  padding: 1px 5px;
  border-radius: 4px;
  background: var(--warn-soft);
  font-family: Consolas, "Courier New", monospace;
}
@media (max-width: 900px) {
  .layout { display: block; }
  nav {
    position: relative;
    height: auto;
    border-right: 0;
    border-bottom: 1px solid var(--line);
  }
  main { padding: 28px 18px 48px; }
  h1 { font-size: 26px; }
}
@media print {
  body { background: white; }
  .layout { display: block; }
  nav { display: none; }
  main { max-width: none; padding: 0; }
  a { color: inherit; text-decoration: none; }
  h1, h2 { break-after: avoid; }
  .table-wrap { overflow: visible; }
  table { min-width: 0; font-size: 10.5pt; }
}
"""


def find_report_files(slug: str) -> list[Path]:
    files = sorted(REPORTS_DIR.glob(f"{slug}-*.md"))
    if not files:
        raise FileNotFoundError(f"找不到 reports/{slug}-*.md")
    return files


def build_html(slug: str) -> Path:
    toc: list[TocItem] = []
    used_anchors: set[str] = set()
    body_parts: list[str] = []

    for path in find_report_files(slug):
        markdown = path.read_text(encoding="utf-8")
        body_parts.append(markdown_to_html(markdown, toc, used_anchors))

    toc_html = "\n".join(
        f'<a class="level-{min(item.level, 3)}" href="#{html.escape(item.anchor)}">'
        f"{html.escape(item.title)}</a>"
        for item in toc
        if item.level <= 3
    )
    document = f"""<!doctype html>
<html lang="zh-Hant-TW">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(slug)} — 閱讀版報告</title>
  <style>{css()}</style>
</head>
<body>
  <div class="layout">
    <nav>
      <h2>目錄</h2>
      {toc_html}
    </nav>
    <main>
      <div class="meta">
        <strong>{html.escape(slug)} — 閱讀版報告</strong><br>
        由 reports 內 Markdown 報告合併產生；法律內容請以原報告及原裁判全文回查核對。
      </div>
      {'<hr>'.join(body_parts)}
    </main>
  </div>
</body>
</html>
"""
    OUT_DIR.mkdir(exist_ok=True)
    out_path = OUT_DIR / f"{slug}-閱讀版.html"
    out_path.write_text(document, encoding="utf-8", newline="\n")
    return out_path


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="將 reports/<slug>-*.md 合併匯出成 HTML 閱讀版")
    parser.add_argument("slug", help="報告 slug，例如：返還寄託物")
    args = parser.parse_args(argv)

    try:
        out_path = build_html(args.slug)
    except Exception as exc:
        print(f"匯出失敗：{exc}", file=sys.stderr)
        return 1

    print(out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

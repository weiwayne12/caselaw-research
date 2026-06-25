#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""build_skim — 工具二第一段：產全語料精簡略讀表（省 token 的鳥瞰）。

每篇只留：審級、字號、案由、主文（勝敗結果）、引用法條、引用判決、
事實理由極短摘錄（首段，常含請求權基礎與核心爭執）。
輸出到 notes/<slug>-skim.md，供 Claude 一次讀完建立分析骨架。

用法：python analysis/build_skim.py <slug> [snippet_chars]
"""
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def short_main(mt: str, cap: int = 220) -> str:
    mt = re.sub(r"\s+", " ", mt or "").strip()
    return mt if len(mt) <= cap else mt[:cap] + "…"


def claim_snippet(facts: str, cap: int) -> str:
    """取事實理由首段（去開頭裁判字號/日期 metadata 雜訊）。"""
    f = facts or ""
    # 砍掉開頭的「裁判字號…裁判案由…」metadata 區塊，找到判決正文起點
    m = re.search(r"(上訴人|原告|聲請人|再審原告|兩造|本件)", f)
    if m:
        f = f[m.start():]
    f = re.sub(r"\s+", " ", f).strip()
    return f[:cap] + ("…" if len(f) > cap else "")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    slug = sys.argv[1] if len(sys.argv) > 1 else ""
    snippet = int(sys.argv[2]) if len(sys.argv) > 2 else 320

    corpus = PROJECT_ROOT / "corpus" / slug
    recs = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(corpus.glob("*.json"))]
    recs.sort(key=lambda d: (
        {"最高法院": 1}.get(d.get("court", ""), 2 if "高等" in d.get("court", "") else 3),
        d.get("date", ""),
    ))

    out = PROJECT_ROOT / "notes" / f"{slug}-skim.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# {slug} — 略讀表（{len(recs)} 篇）\n"]
    for i, d in enumerate(recs):
        facts = d.get("facts", "") or d.get("full_text", "")
        lines.append(
            f"## [{i}] {d.get('court','')} {d.get('case_id','')}（{d.get('date','')}）\n"
            f"- 案由：{d.get('cause','')}\n"
            f"- 主文：{short_main(d.get('main_text',''))}\n"
            f"- 引用法條：{'、'.join(d.get('cited_statutes',[])) or '（無）'}\n"
            f"- 引用判決：{'、'.join(d.get('cited_cases',[])) or '（無）'}\n"
            f"- 起手：{claim_snippet(facts, snippet)}\n"
            f"- 全文字數：{len(d.get('full_text',''))}\n"
        )
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"已寫出略讀表：{out}（{len(recs)} 篇）")
    print(f"檔案大小：{out.stat().st_size/1024:.1f} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())

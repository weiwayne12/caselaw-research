#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""dump_batch — 工具二分析輔助：成批吐出 corpus 判決的關鍵欄位供 Claude 閱讀。

把每篇判決壓成「分析就緒摘要」（主文＋案由＋事實理由＋引用法條），
分批列印，讓 Claude 一次 tool call 讀一批、寫筆記，控管 token。

注意：民事判決 reasoning 欄位多為空，理由併入 facts，故以 facts 為主體。

用法：
  python analysis/dump_batch.py <slug> [start] [count] [facts_cap]
  例：python analysis/dump_batch.py 返還寄託物 0 12
"""
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    slug = sys.argv[1] if len(sys.argv) > 1 else ""
    start = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    count = int(sys.argv[3]) if len(sys.argv) > 3 else 12
    facts_cap = int(sys.argv[4]) if len(sys.argv) > 4 else 4500

    corpus = PROJECT_ROOT / "corpus" / slug
    files = sorted(p for p in corpus.glob("*.json"))
    if not files:
        print(f"找不到語料：{corpus}")
        return 1

    # 依審級權威序、日期排序，閱讀順序較自然
    recs = []
    for p in files:
        d = json.loads(p.read_text(encoding="utf-8"))
        recs.append(d)
    recs.sort(key=lambda d: (
        {"最高法院": 1}.get(d.get("court", ""), 2 if "高等" in d.get("court", "") else 3),
        d.get("date", ""),
    ))

    total = len(recs)
    batch = recs[start:start + count]
    print(f"=== 語料 {slug}：共 {total} 篇，本批 [{start}:{start + len(batch)}] ===\n")

    for i, d in enumerate(batch, start):
        facts = d.get("facts", "") or d.get("full_text", "")
        truncated = ""
        if len(facts) > facts_cap:
            facts = facts[:facts_cap]
            truncated = f"\n…（事實理由過長已截斷至 {facts_cap} 字，完整見 corpus 檔）"
        print(f"───────── [{i}] {d.get('court','')} {d.get('case_id','')} ─────────")
        print(f"案由：{d.get('cause','')}　日期：{d.get('date','')}　JID：{d.get('jid','')}")
        print(f"主文：{d.get('main_text','').strip()}")
        print(f"引用法條：{'、'.join(d.get('cited_statutes',[])) or '（無）'}")
        print(f"引用判決：{'、'.join(d.get('cited_cases',[])) or '（無）'}")
        print(f"事實及理由：\n{facts.strip()}{truncated}")
        print()

    nxt = start + len(batch)
    if nxt < total:
        print(f"=== 本批結束。下一批起點：{nxt} / 共 {total} ===")
    else:
        print(f"=== 已到最後一批（{total} 篇讀畢）===")
    return 0


if __name__ == "__main__":
    sys.exit(main())

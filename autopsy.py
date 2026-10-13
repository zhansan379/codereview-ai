"""Autopsy: quantify whether codereview-ai misses are "content wrong" or "line wrong".

For every reference comment, find the best-similarity codereview finding in the SAME
file (difflib on text + keyword overlap). Classify each matched pair's line gap:
  - within k(=1): true hit
  - off by 2..: content likely right, line drifted
  - None/missing line: content right, line never anchored
Count refs with no similar finding => content-level miss (understanding/recall gap).
"""
import json
import re
import difflib
from pathlib import Path

K = 1
SIM_THRESH = 0.28  # conservative: only call it a match when plausibly same concern

RESULTS = Path("D:/Project/pythonProject/aacr-bench/evaluation/results/aacr_bench_s5/codereview")
DATA = "D:/Project/pythonProject/aacr-bench/evaluation/data/aacr_bench_s5.jsonl"


def words(t):
    return set(re.findall(r"[a-zA-Z_]{3,}", (t or "").lower()))


def similarity(a, b):
    a, b = (a or "").lower(), (b or "").lower()
    seq = difflib.SequenceMatcher(None, a, b).ratio()
    wa, wb = words(a), words(b)
    jac = (len(wa & wb) / len(wa | wb)) if (wa and wb) else 0.0
    return max(seq, jac)


def gap(ref_start, ref_end, gen_line):
    if gen_line is None:
        return None
    # distance to nearest of [start,end]
    d = min(abs(gen_line - ref_start), abs(gen_line - ref_end)) if ref_start is not None else None
    return d


def load_instances():
    inst = {}
    for line in open(DATA, encoding="utf-8"):
        d = json.loads(line)
        inst[d["instance_id"]] = d["reference_comments"]
    return inst


def main():
    inst = load_instances()
    totals = {}
    for mdir in sorted(RESULTS.iterdir()):
        rec = {"refs": 0, "content_hit": 0, "line_hit": 0, "line_none_but_content": 0,
               "line_drift_but_content": 0, "content_miss": 0, "gaps": []}
        for fp in mdir.glob("*.json"):
            if fp.name.startswith("summary"):
                continue
            d = json.loads(fp.read_text(encoding="utf-8"))
            r = d.get("review")
            findings = (r.get("findings") or []) if isinstance(r, dict) else []
            refs = inst.get(d.get("instance_id"), [])
            for ref in refs:
                if not (ref.get("path") and ref.get("text")):
                    continue
                rec["refs"] += 1
                rp = ref["path"].replace("\\", "/")
                # best finding in same file
                cand = [f for f in findings if (f.get("file") or "").replace("\\", "/") == rp]
                best, best_sim = None, 0.0
                for f in cand:
                    s = similarity(ref["text"], f.get("content"))
                    if s > best_sim:
                        best, best_sim = f, s
                if best is None or best_sim < SIM_THRESH:
                    # no same-file candidate that plausibly matches => try global fallback
                    gbest, gsim = None, 0.0
                    for f in findings:
                        s = similarity(ref["text"], f.get("content"))
                        if s > gsim:
                            gbest, gsim = f, s
                    if gbest is not None and gsim >= SIM_THRESH:
                        best, best_sim = gbest, gsim
                        rec["content_hit"] += 1  # roughly content found (maybe wrong file)
                        g = gap(ref.get("start_line"), ref.get("end_line"), best.get("line"))
                        rec["gaps"].append(g)
                        if g is not None and g <= K:
                            rec["line_hit"] += 1
                        elif g is None:
                            rec["line_none_but_content"] += 1
                        else:
                            rec["line_drift_but_content"] += 1
                        continue
                    rec["content_miss"] += 1
                    rec["gaps"].append(None)
                    continue
                rec["content_hit"] += 1
                g = gap(ref.get("start_line"), ref.get("end_line"), best.get("line"))
                rec["gaps"].append(g)
                if g is not None and g <= K:
                    rec["line_hit"] += 1
                elif g is None:
                    rec["line_none_but_content"] += 1
                else:
                    rec["line_drift_but_content"] += 1
        totals[mdir.name] = rec

    print(f"Inspecting codereview results vs {K} ground-truth references (content-thresh={SIM_THRESH})\n")
    hdr = f"{'model':<16}{'refs':>5}{'内容命中':>8}{'行号中也':>9}{'内容中·行None':>14}{'内容中·行偏':>11}{'内容未中':>9}"
    print(hdr)
    print("-" * len(hdr))
    for m, rec in totals.items():
        if rec["refs"] == 0:
            continue
        print(f"{m:<16}{rec['refs']:>5}{rec['content_hit']:>8}{rec['line_hit']:>9}"
              f"{rec['line_none_but_content']:>14}{rec['line_drift_but_content']:>11}{rec['content_miss']:>9}")
    # line-drift distribution for the best model that had hits
    print("\n-- line-gap distribution (all content-hits across models, gap=None=list) --")
    from collections import Counter
    dist = Counter()
    for m, rec in totals.items():
        for g in rec["gaps"]:
            if g is None:
                dist["no_line"] += 1
            elif g <= K:
                dist["<=1"] += 1
            elif g <= 5:
                dist["2-5"] += 1
            elif g <= 20:
                dist["6-20"] += 1
            else:
                dist[">20"] += 1
    for k, v in dist.most_common():
        print(f"  gap {k:>6}: {v}")


if __name__ == "__main__":
    main()
"""keywords.json 에 설정한 키워드(각 카테고리의 terms)만 집계 대상으로 삼는 도우미.
- 라벨: 정규식 대안 중 가장 짧은 표기 (예: '식품의약품안전처|식약처' → '식약처'). keyword_labels 로 직접 지정 가능.
- tracked_extra: 추가로 집계할 키워드, tracked_exclude: 집계에서 뺄 라벨 목록 (keywords.json 에 선택적으로 추가)
"""
import json, re
from pathlib import Path


def _split_top(p):
    out, depth, cur = [], 0, ""
    for ch in p:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "|" and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    return out + [cur]


def _clean(a):
    s = re.sub(r"\(\?-i:([^)]*)\)", r"\1", a)
    s = re.sub(r"\(\?<[!=][^)]*\)", "", s)
    s = re.sub(r"\\s\??", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return None if re.search(r"[\\()\[\]?*+{}]", s) else s


def label_of(pat, overrides):
    if pat in overrides:
        return overrides[pat]
    alts = [c for c in (_clean(a) for a in _split_top(pat)) if c]
    return min(alts, key=len) if alts else None


def load(config_path="keywords.json"):
    cfg = json.loads(Path(config_path).read_text(encoding="utf-8"))
    ov = cfg.get("keyword_labels", {})
    skip = set(cfg.get("tracked_exclude", []))
    out, seen = [], set()
    pats = [p for c in cfg["categories"].values() for p in c.get("terms", [])] + list(cfg.get("tracked_extra", []))
    for p in pats:
        lab = label_of(p, ov)
        if not lab or lab in skip or lab in seen:
            continue
        seen.add(lab)
        out.append((lab, re.compile(p, re.I)))
    return out


def hits(title, summary, tracked):
    text = f"{title} {summary or ''}"
    return [lab for lab, rx in tracked if rx.search(text)]

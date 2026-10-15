#!/usr/bin/env python3
"""주간 누적 파일(data/weeks/*.json)에서 월간 집계 파일을 만든다.
출력: data/months/YYYY-MM.json (집계), YYYY-MM.md (읽기용 요약), YYYY-MM.csv (해당 월 전체 기사)
사용: python monthly.py            → 이번 달 + 지난달 갱신
      python monthly.py 2026-11    → 지정한 달만
월 구분은 기사가 대시보드에 처음 수집된 날짜(first_seen) 기준.
"""
import csv, json, re, sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
DATA = Path("data")
OUT = DATA / "months"

STOP = set("""AI 인공지능 의료 병원 헬스케어 기반 활용 개발 도입 추진 지원 강화 구축 협력 협약 체결 확대 통해 위한 대한 관련 이후 올해 내년
지난 오늘 최근 시대 분야 기술 서비스 사업 기업 국내 해외 세계 글로벌 신규 새로운 첫 최초 개최 발표 공개 출시 선정 진행 운영 제공 나선다 나서
열려 연다 밝혀 전망 가능 필요 중심 방안 계획 정부 대표 한국 바이오 맞손 넘어 논의 참여 지역 시스템 통합 혁신 전문 산업 플랫폼 환자 시작
함께 나란히 손잡고 손잡아 도약 변화 미래 현장 교수 연구 연구팀 결과 효과 솔루션 프로그램 센터 병원장 이사장
the for and with from that this new how why into its are has will can using use health medical care healthcare hospital hospitals system
systems clinical patient patients says said could over more than after about""".split())
JOSA = re.compile(r"(으로|에서|에게|까지|부터|이라|라는|에는|으로는|에도|와의|과의|이다|한다|하는|하며|했다|된다|한|은|는|이|가|을|를|의|에|와|과|로|도|만)$")


def tokens(title):
    out = set()
    for t in re.findall(r"[가-힣]{2,}|[A-Za-z][A-Za-z0-9\-]{2,}", title or ""):
        w = t
        if re.search(r"[가-힣]", w):
            s = JOSA.sub("", w)
            if len(s) >= 2:
                w = s
        else:
            w = w.lower()
        if w not in STOP and w.upper() not in STOP and len(w) >= 2:
            out.add(w)
    return out


def load_items():
    items = []
    for p in sorted((DATA / "weeks").glob("[0-9][0-9][0-9][0-9]-W[0-9][0-9].json")):
        w = json.loads(p.read_text(encoding="utf-8"))
        for it in w.get("items", []):
            it["_week"] = w["week"]
            items.append(it)
    return items


def month_of(it):
    return (it.get("first_seen") or it.get("pubDate", ""))[:7]


def prev_month(ym):
    y, m = map(int, ym.split("-"))
    return f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"


def kw_counts(items):
    c = Counter()
    for it in items:
        for w in tokens(it["title"]):
            c[w] += 1
    return c


def importance(it):
    base = it.get("score", 0) + 2 * it.get("similar_count", 0)
    if it.get("source_type") in ("major", "medical", "global_pref"):
        base += 3
    return base


def brief(it):
    return {"title": it["title"], "link": it.get("originallink") or it.get("link"), "date": (it.get("pubDate") or "")[:10],
            "category": it.get("category"), "region": it.get("region"), "score": it.get("score", 0),
            "similar_count": it.get("similar_count", 0), "source": it.get("agency") or ", ".join(it.get("sources") or []) or
            (re.sub(r"^https?://(www\.)?", "", (it.get("originallink") or it.get("link") or "")).split("/")[0]),
            "importance": importance(it)}


def build(ym, all_items):
    cur = [i for i in all_items if month_of(i) == ym]
    pm = prev_month(ym)
    prv = [i for i in all_items if month_of(i) == pm]
    if not cur:
        return None
    by_region = Counter(i["region"] for i in cur)
    cat_cur, cat_prv = Counter(f"{i['region']} · {i['category']}" for i in cur), Counter(f"{i['region']} · {i['category']}" for i in prv)
    categories = [{"name": k, "count": n, "prev": cat_prv.get(k, 0), "delta": n - cat_prv.get(k, 0)} for k, n in cat_cur.most_common()]
    weekly = defaultdict(Counter)
    for i in cur:
        weekly[i["_week"]][i["region"]] += 1

    kc, kp = kw_counts(cur), kw_counts(prv)
    kws = []
    for w, n in kc.most_common(60):
        if n < 3:
            break
        p = kp.get(w, 0)
        status = "신규" if prv and p == 0 else ("급증" if prv and p and n >= 2 * p and n - p >= 3 else "")
        kws.append({"word": w, "count": n, "prev": p, "status": status})
    kws = kws[:20]

    kr = [i for i in cur if i["region"] == "국내"]
    gl = [i for i in cur if i["region"] == "해외"]
    gv = [i for i in cur if i["region"] == "정부"]
    issues_kr = sorted([i for i in kr if i.get("similar_count", 0) > 0], key=lambda i: (-i["similar_count"], -i.get("score", 0)))[:10]
    issues_gl = sorted([i for i in gl if i.get("similar_count", 0) > 0 or len(i.get("sources") or []) > 1],
                       key=lambda i: (-(len(i.get("sources") or [])), -i.get("score", 0)))[:10]
    policy = sorted([i for i in gv if i.get("kind") in ("법령·입법", "가이드라인·기준")], key=lambda i: i.get("pubDate", ""), reverse=True)
    support = sorted([i for i in gv if i.get("kind") not in ("법령·입법", "가이드라인·기준")], key=lambda i: i.get("pubDate", ""), reverse=True)[:30]
    top_kr = sorted(kr, key=importance, reverse=True)[:20]
    top_gl = sorted(gl, key=importance, reverse=True)[:15]
    per_cat = {}
    for i in sorted(cur, key=importance, reverse=True):
        if i["region"] == "정부":
            continue
        lst = per_cat.setdefault(f"{i['region']} · {i['category']}", [])
        if len(lst) < 3:
            lst.append(brief(i))

    days = sorted({i.get("first_seen") for i in cur if i.get("first_seen")})
    summary = {
        "month": ym, "generated_at": datetime.now(KST).isoformat(), "prev_month": pm if prv else None,
        "collected_days": len(days), "first_day": days[0] if days else None, "last_day": days[-1] if days else None,
        "total": len(cur), "prev_total": len(prv) or None, "by_region": dict(by_region),
        "weekly": {w: dict(c) for w, c in sorted(weekly.items())},
        "categories": categories, "keywords": kws,
        "issues_domestic": [brief(i) for i in issues_kr], "issues_global": [brief(i) for i in issues_gl],
        "policy_announcements": [brief(i) for i in policy], "government_other": [brief(i) for i in support],
        "important_domestic": [brief(i) for i in top_kr], "important_global": [brief(i) for i in top_gl],
        "top_by_category": per_cat,
        "notes": ["importance = 관련도 점수 + 2×유사기사 수 + 3(주요 일간지·의료 전문지·주요 해외 매체). 후보 선정용이며 최종 판단은 검수 필요.",
                  "월 구분은 대시보드 최초 수집일 기준. 수집일 수가 적은 달은 비교에 주의.",
                  "기사 본문은 저장하지 않고 제목·요약·링크만 보관."],
    }
    return summary, cur


def to_md(s):
    L = [f"# {s['month']} 월간 집계", "",
         f"- 수집 기간: {s['first_day']} ~ {s['last_day']} ({s['collected_days']}일 수집)",
         f"- 총 {s['total']:,}건 (" + ", ".join(f"{k} {v:,}" for k, v in s['by_region'].items()) + ")"
         + (f" · 전월 {s['prev_total']:,}건" if s['prev_total'] else ""), ""]
    L += ["## 주별 건수", "| 주 | 국내 | 해외 | 정부 |", "|---|---|---|---|"]
    for w, c in s["weekly"].items():
        L.append(f"| {w} | {c.get('국내', 0)} | {c.get('해외', 0)} | {c.get('정부', 0)} |")
    L += ["", "## 카테고리별 건수 (전월 대비)", "| 카테고리 | 건수 | 전월 | 증감 |", "|---|---|---|---|"]
    for c in s["categories"]:
        L.append(f"| {c['name']} | {c['count']} | {c['prev']} | {c['delta']:+d} |")
    L += ["", "## 키워드 Top 20 (제목 기준)", "| 키워드 | 건수 | 전월 | 비고 |", "|---|---|---|---|"]
    for k in s["keywords"]:
        L.append(f"| {k['word']} | {k['count']} | {k['prev']} | {k['status']} |")

    def lst(title, rows, extra=None):
        out = ["", f"## {title}"]
        if not rows:
            return out + ["(해당 없음)"]
        for r in rows:
            tail = f" · {extra(r)}" if extra else ""
            out.append(f"- [{r['title']}]({r['link']}) — {r['source']}, {r['date']}{tail}")
        return out
    L += lst("많이 다뤄진 국내 이슈 (유사 기사 수 기준)", s["issues_domestic"], lambda r: f"유사 {r['similar_count']}건")
    L += lst("국내 중요 기사 후보", s["important_domestic"], lambda r: f"중요도 {r['importance']} · {r['category']}")
    L += lst("해외 중요 기사 후보", s["important_global"], lambda r: f"중요도 {r['importance']}")
    L += lst("정부 발표: 법령·입법·가이드라인", s["policy_announcements"])
    L += lst("정부 발표: 그 외 (최근 30건)", s["government_other"])
    L += ["", "## 비고"] + [f"- {n}" for n in s["notes"]]
    return "\n".join(L) + "\n"


def main():
    now = datetime.now(KST)
    cur = now.strftime("%Y-%m")
    targets = sys.argv[1:] or [prev_month(cur), cur]
    items = load_items()
    OUT.mkdir(parents=True, exist_ok=True)
    for ym in targets:
        res = build(ym, items)
        if not res:
            print(f"{ym}: 데이터 없음")
            continue
        s, rows = res
        (OUT / f"{ym}.json").write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")
        (OUT / f"{ym}.md").write_text(to_md(s), encoding="utf-8")
        with open(OUT / f"{ym}.csv", "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["수집일", "발행일", "구분", "카테고리", "제목", "출처", "링크", "관련도", "유사기사수", "요약"])
            for i in sorted(rows, key=lambda i: (i.get("first_seen", ""), -i.get("score", 0))):
                b = brief(i)
                w.writerow([i.get("first_seen"), b["date"], i["region"], i["category"], i["title"], b["source"], b["link"],
                            i.get("score", 0), i.get("similar_count", 0), i.get("summary", "")])
        print(f"{ym}: {s['total']}건 → data/months/{ym}.json/.md/.csv")


if __name__ == "__main__":
    main()

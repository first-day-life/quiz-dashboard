#!/usr/bin/env python3
"""
pull.py: pull Octane AI quiz analytics, reshape them, and write data/*.json for the dashboard.

Standard library only. Python 3.9+.

Environment:
    OCTANE_API_KEY   required. A READ-ONLY key (Read quizzes + Read analytics). Never commit it.
    QUIZ_NAME        quiz to find by exact name     (default "v2 | Branch Logic")
    QUIZ_ID          fallback id if the name isn't found (default quiz_1a69eb0c9b684adb9c4716ad98428013)
    DAYS             days in the window, ending today (default 14)
    FROM / TO        YYYY-MM-DD; override DAYS when set
    TZ_NAME          IANA timezone (default America/Chicago)
    OUT_DIR          plaintext output folder (default build/, gitignored; run scripts/encrypt.mjs next)

Locally:  OCTANE_API_KEY=... python3 scripts/pull.py && DASHBOARD_KEY=... node scripts/encrypt.mjs
(or put OCTANE_API_KEY=... in a .env file at the repo root; .env is gitignored)
"""

import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

API = "https://api.octaneai.com/v1"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BREAKDOWN_DIMS = ["channel", "device", "new_returning", "country"]


# --------------------------------------------------------------------------- config

def load_dotenv():
    path = os.path.join(ROOT, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_dotenv()
KEY = os.environ.get("OCTANE_API_KEY", "").strip()
QUIZ_NAME = os.environ.get("QUIZ_NAME") or "v2 | Branch Logic"
QUIZ_ID = os.environ.get("QUIZ_ID") or "quiz_1a69eb0c9b684adb9c4716ad98428013"
TZ = os.environ.get("TZ_NAME") or "America/Chicago"
DAYS = int(os.environ.get("DAYS") or 14)
OUT = os.path.join(ROOT, os.environ.get("OUT_DIR") or "build")   # plaintext; gitignored. scripts/encrypt.mjs publishes it


# --------------------------------------------------------------------------- http

def get(path, params=None, retries=5):
    params = {k: v for k, v in (params or {}).items() if v not in (None, "")}
    url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {KEY}",
        "Accept": "application/json",
        "User-Agent": "octane-quiz-dashboard/1.0",
    })
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            if e.code in (429, 500, 502, 503) and attempt < retries - 1:
                wait = int(e.headers.get("Retry-After") or 2 * (attempt + 1))
                print(f"  {e.code} on {path}; retrying in {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            sys.exit(f"{e.code} on {path}: {body[:400]}")
        except urllib.error.URLError as e:
            if attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
                continue
            sys.exit(f"network error on {path}: {e.reason}")


def walk(path, **params):
    params = {**params, "limit": 100}
    while True:
        page = get(path, params)
        yield from page.get("data", [])
        if not page.get("has_more"):
            return
        params["cursor"] = page["next_cursor"]


# --------------------------------------------------------------------------- reshaping helpers

TITLE_RE = re.compile(r"^\s*(\d+)\s*-\s*(.+?)\s*$")


def fix(s):
    return (s or "").replace("Youger", "Younger")


def parse_title(title):
    """'2 - Problems / Younger Kid Only' -> (2, 'Problems', 'Younger Kid Only'). No number -> (None, title, None)."""
    title = fix(title)
    m = TITLE_RE.match(title)
    if not m:
        return None, title.strip(), None
    rest = m.group(2)
    name, _, audience = rest.partition(" / ")
    return int(m.group(1)), name.strip(), (audience.strip() or None)


def generalize(q):
    """Swap the audience-specific subject for [demo]: 'your kid/teen/child/family', and 'you' as an object."""
    q = re.sub(r"\byour (kid|teen|child|family)\b", "[demo]", q, flags=re.I)
    q = re.sub(r"\byou(?=\s*[?.!]|\s*$)", "[demo]", q, flags=re.I)
    return q


def rate(n, d):
    return None if not d else n / d


# --------------------------------------------------------------------------- reshape

def shape_overview(ov):
    t = ov.get("totals", {})
    series = []
    for d in ov.get("series", []):
        uv = d.get("unique_viewers_approx") or 0
        series.append({
            "day": d["day"],
            "views": d.get("views", 0),
            "unique_viewers": uv,
            "starts": d.get("starts", 0),
            "completions": d.get("completions", 0),
            "start_rate": rate(d.get("starts", 0), uv),
        })
    rev = t.get("revenue") or {}
    med = t.get("median_time_to_complete_ms")
    totals = {
        "views": t.get("views", 0),
        "starts": t.get("starts", 0),
        "completions": t.get("completions", 0),
        "unique_viewers": t.get("unique_viewers_approx", 0),
        "email_opt_ins": t.get("email_opt_ins", 0),
        "phone_opt_ins": t.get("phone_opt_ins", 0),
        "orders": t.get("orders", 0),
        "revenue": {"amount": rev.get("amount"), "currency": rev.get("currency")},
        "median_time_to_complete_s": None if med is None else med / 1000,
    }
    totals["start_rate"] = rate(totals["starts"], totals["unique_viewers"])
    totals["view_start_rate"] = rate(totals["starts"], totals["views"])
    totals["completion_rate"] = rate(totals["completions"], totals["starts"])
    return {"totals": totals, "series": series}


def shape_funnel(fn, ov_totals):
    pages = fn.get("pages") or []
    steps, by_num = [], {}
    for p in pages:
        n, name, _ = parse_title(p.get("title"))
        if n is None:
            row = {"step": None, "label": name, "reached": 0, "dropped": 0, "versions": 0}
            steps.append(row)
        else:
            row = by_num.get(n)
            if row is None:
                row = {"step": n, "label": f"{n} · {name}", "reached": 0, "dropped": 0, "versions": 0}
                by_num[n] = row
                steps.append(row)
        row["reached"] += p.get("sessions_reached") or 0
        row["dropped"] += p.get("sessions_dropped") or 0
        row["versions"] += 1
    # numbered steps in order, unnumbered pages (e.g. results) after them in original order
    steps = sorted([s for s in steps if s["step"] is not None], key=lambda s: s["step"]) + \
            [s for s in steps if s["step"] is None]

    starts = fn.get("starts", ov_totals.get("starts", 0))
    in_quiz = starts
    out = [{"step": 0, "label": "Viewed quiz", "reached": ov_totals.get("views", fn.get("views", 0)),
            "dropped": fn.get("not_started", 0), "versions": 1, "in_quiz": in_quiz}]
    for s in steps:
        in_quiz -= s["dropped"]
        out.append({**s, "in_quiz": in_quiz})
    quiz_steps = [s for s in out[1:]]
    if quiz_steps:
        biggest = max(quiz_steps, key=lambda s: s["dropped"])
        for s in out:
            s["biggest_drop"] = s is biggest and biggest["dropped"] > 0

    left = [{
        "page": fix(p.get("title")),
        "page_key": p.get("page_key"),
        "reached": p.get("sessions_reached") or 0,
        "dropped": p.get("sessions_dropped") or 0,
        "avg_s": None if p.get("avg_time_on_page_ms") is None else p["avg_time_on_page_ms"] / 1000,
    } for p in pages if (p.get("sessions_dropped") or 0) > 0]
    left.sort(key=lambda r: -r["dropped"])

    return {
        "views": fn.get("views", 0), "starts": starts, "completions": fn.get("completions", 0),
        "not_started": fn.get("not_started", 0), "in_progress": fn.get("in_progress", 0),
        "steps": out, "pages_left": left,
    }


def shape_branch_check(paths, fn):
    titles = {p["page_key"]: fix(p.get("title")) for p in (fn.get("pages") or [])}
    for nd in paths.get("nodes") or []:
        k = nd.get("page_key") or nd.get("key")
        if k and k not in titles:
            titles[k] = fix(nd.get("title") or nd.get("page_title") or k)
    rows = []
    for e in paths.get("edges") or []:
        if e.get("cause") != "sequential":
            continue
        a, b = e.get("from_page_key"), e.get("to_page_key")
        if not a or not b or a == b:
            continue
        na, _, _ = parse_title(titles.get(a, ""))
        nb, _, _ = parse_title(titles.get(b, ""))
        if na is not None and na == nb:
            rows.append({"from_page": titles.get(a, a), "to_page": titles.get(b, b),
                         "sessions": e.get("sessions_moved") or 0})
    rows.sort(key=lambda r: -r["sessions"])
    return {"rows": rows}


def shape_breakdown(bd):
    rows = []
    for dim in BREAKDOWN_DIMS:
        for r in (bd.get(dim) or {}).get("rows", []):
            rows.append({"dimension": dim, "value": r.get("value"), "views": r.get("views", 0),
                         "starts": r.get("starts", 0), "completions": r.get("completions", 0),
                         "unique_viewers": r.get("unique_viewers_approx", 0)})
    return {"rows": rows}


def opt_row(o):
    return {"label": o.get("label"), "chose": o.get("chose", 0), "completed": o.get("completed", 0),
            "opted_in": o.get("opted_in", 0)}


def assign_slots(versions, n_slots=8):
    """Stable color slot per option label within one question (same label = same color in every view).
    Greedy coloring, busiest labels first: labels shown together in one version never share a slot, and
    neither do the top 7 of the combined view (the rest of that view folds into "Other"). Slot 8+ = gray."""
    totals, together = {}, []
    for v in versions:
        together.append([o["label"] for o in v["options"]])
        for o in v["options"]:
            totals[o["label"]] = totals.get(o["label"], 0) + o["chose"]
    ranked = sorted(totals, key=lambda l: -totals[l])
    if len(versions) > 1:
        together.append(ranked if len(ranked) <= n_slots else ranked[:n_slots - 1])
    slots = {}
    for label in ranked:
        taken = {slots[o] for g in together if label in g for o in g if o in slots}
        slots[label] = next((i for i in range(n_slots) if i not in taken), n_slots)
    return slots


def shape_answers(questions, details):
    groups, order = {}, []
    for q in questions:
        if q.get("kind") != "choice":
            continue
        n, name, audience = parse_title(q.get("page_title"))
        key = n if n is not None else q["component_key"]
        if key not in groups:
            groups[key] = {"step": n, "name": name, "versions": []}
            order.append(key)
        d = details.get(q["component_key"]) or {}
        opts = [o for o in (d.get("options") or []) if not (o.get("removed") and not o.get("chose"))]
        groups[key]["versions"].append({
            "audience": audience or "All",
            "question": fix(q.get("question")),
            "answered": d.get("answered", 0),
            "options": [opt_row(o) for o in opts],
        })

    out = []
    for key in sorted(order, key=lambda k: (groups[k]["step"] is None, groups[k]["step"] or 0)):
        g = groups[key]
        vs = g["versions"]
        slots = assign_slots(vs)
        if len(vs) == 1:
            series = [{**vs[0], "audience": "All"}]
        else:
            combined = {}
            for v in vs:
                for o in v["options"]:
                    c = combined.setdefault(o["label"], {"label": o["label"], "chose": 0, "completed": 0, "opted_in": 0})
                    for f in ("chose", "completed", "opted_in"):
                        c[f] += o[f]
            # pick the wording with a recognizable subject to generalize
            src = next((v["question"] for v in vs if re.search(r"\byour (kid|teen|child|family)\b", v["question"], re.I)),
                       vs[0]["question"])
            series = [{"audience": "All", "question": generalize(src),
                       "answered": sum(v["answered"] for v in vs),
                       "options": sorted(combined.values(), key=lambda o: -o["chose"])}] + vs
        for s in series:
            for o in s["options"]:
                o["slot"] = slots[o["label"]]
        out.append({"step": g["step"], "name": g["name"],
                    "label": f"{g['step']} · {g['name']}" if g["step"] is not None else g["name"],
                    "series": series})
    audiences = []
    for g in out:
        for s in g["series"][1:] if len(g["series"]) > 1 else []:
            if s["audience"] not in audiences:
                audiences.append(s["audience"])
    return {"questions": out, "audiences": audiences}


# --------------------------------------------------------------------------- main

def find_quiz():
    quizzes = list(walk("/quizzes"))
    for q in quizzes:
        if q["name"].strip().lower() == QUIZ_NAME.strip().lower():
            return q
    for q in quizzes:
        if q["id"] == QUIZ_ID:
            print(f"note: no quiz named {QUIZ_NAME!r}; using id {QUIZ_ID} ({q['name']})", file=sys.stderr)
            return q
    names = ", ".join(repr(q["name"]) for q in quizzes) or "none"
    sys.exit(f"quiz {QUIZ_NAME!r} / {QUIZ_ID} not found. Quizzes this key can see: {names}")


def write(name, data):
    with open(os.path.join(OUT, name), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
        f.write("\n")


def main():
    if not KEY:
        sys.exit("OCTANE_API_KEY is not set.")
    today = dt.datetime.now(ZoneInfo(TZ)).date()
    to = dt.date.fromisoformat(os.environ["TO"]) if os.environ.get("TO") else today
    frm = dt.date.fromisoformat(os.environ["FROM"]) if os.environ.get("FROM") else to - dt.timedelta(days=DAYS - 1)
    if frm > to or (to - frm).days + 1 > 92:
        sys.exit("FROM must be on/before TO and the range must be 92 days or fewer (funnel/answers/paths cap).")

    quiz = find_quiz()
    qid = quiz["id"]
    P = f"/quizzes/{qid}/analytics"
    base = {"from": frm.isoformat(), "to": to.isoformat(), "tz": TZ}
    print(f"{quiz['name']} ({qid})  {frm} → {to}  {TZ}")

    ov = get(f"{P}/overview", base); print("  overview ok")
    fn = get(f"{P}/funnel", base); print("  funnel ok")
    try:
        paths = get(f"{P}/paths", {**base, "cohort": "all"}); print("  paths ok")
    except SystemExit as e:  # paths can be refused on very busy quizzes; keep the rest
        print(f"  paths skipped: {e}", file=sys.stderr)
        paths = {"edges": [], "error": str(e)}
    bd = {by: get(f"{P}/breakdown", {**base, "by": by}) for by in BREAKDOWN_DIMS}; print("  breakdown ok")
    first = get(f"{P}/answers", base)
    questions = first.get("questions", [])
    details = {}
    for q in questions:
        if q.get("kind") != "choice":
            continue
        ck = q["component_key"]
        details[ck] = first if (first.get("question") or {}).get("component_key") == ck \
            else get(f"{P}/answers", {**base, "component_key": ck})
    print(f"  answers ok ({len(details)} questions)")

    overview = shape_overview(ov)
    os.makedirs(OUT, exist_ok=True)
    write("overview.json", overview)
    write("funnel.json", shape_funnel(fn, overview["totals"]))
    write("branch_check.json", {**shape_branch_check(paths, fn), "paths_error": paths.get("error")})
    write("breakdown.json", shape_breakdown(bd))
    write("answers.json", shape_answers(questions, details))
    write("meta.json", {
        "pulled_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "quiz": {"id": qid, "name": quiz["name"]},
        "from": frm.isoformat(), "to": to.isoformat(), "tz": TZ,
        "octane_as_of": (ov.get("freshness") or {}).get("as_of"),
        "repo": os.environ.get("GITHUB_REPOSITORY") or os.environ.get("DASHBOARD_REPO") or "first-day-life/quiz-dashboard",
    })
    print(f"wrote {OUT}/")


if __name__ == "__main__":
    main()

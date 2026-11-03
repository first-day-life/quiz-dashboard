#!/usr/bin/env python3
"""
octane_quiz_reports.py — pull Octane AI analytics reports for a quiz.

Standard library only (no pip install). Python 3.8+.

SETUP
    Put your key in a .env file next to this script:
        OCTANE_API_KEY=oct_live_...
    (or export OCTANE_API_KEY in the shell; .env wins if both are set)

USAGE
    # see which quizzes the key can reach
    python octane_quiz_reports.py quizzes

    # pull the last 30 days (today in America/Chicago) for one quiz
    python octane_quiz_reports.py report --quiz "Skin routine finder"
    python octane_quiz_reports.py report --quiz quiz_2ccb8aebb27c463fa783cafb3bf858e0

    # custom range, timezone, and only some reports
    python octane_quiz_reports.py report --quiz "..." --from 2026-10-01 --to 2026-10-27 \
        --tz America/Chicago --reports overview,funnel,answers

    # narrow every report to one segment (channel/device/new_returning/country)
    python octane_quiz_reports.py report --quiz "..." --device mobile --country US

    # rebuild report.html from a folder already pulled (no API calls)
    python octane_quiz_reports.py render octane_reports/<quiz>_<from>_<to>

    # store-wide numbers instead of one quiz
    python octane_quiz_reports.py store --from 2026-10-01 --to 2026-10-27

OUTPUT
    octane_reports/<quiz>_<from>_<to>/
        overview.json / overview_series.csv / overview_totals.csv / overview_markers.csv
        funnel.json / funnel_pages.csv
        answers_<component_key>.json / .csv   (one per choice/number question)
        breakdown_channel.csv, breakdown_device.csv, breakdown_new_returning.csv, breakdown_country.csv
        traffic_referrer_host.csv, traffic_utm_source.csv, traffic_utm_medium.csv, traffic_utm_campaign.csv
        products.json / products.csv
        versions.json / versions.csv
        paths.json
        report.html   <- open this first

REPORTS (per quiz; all need the analytics:read scope)
    overview    views/starts/completions/opt-ins/orders/revenue per day + totals, with publish markers
    funnel      page-by-page drop-off                                  (max 92-day range)
    answers     what shoppers who picked each option did next, per question (max 92-day range)
    breakdown   the counts by channel, device, new/returning, country
    traffic     referrers and UTM values                               (max 92-day range)
    products    revenue attributed per product
    versions    views/starts/completions/orders/revenue per published version
    paths       page-to-page journeys (may be refused on very busy quizzes)

API reference: https://developers.octaneai.com/guides/analytics/
"""

import argparse
import csv
import datetime as dt
import difflib
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

API = "https://api.octaneai.com/v1"
DEFAULT_TZ = "America/Chicago"
RAW_CAPPED = {"funnel", "answers", "traffic", "paths"}   # 92-day cap per the docs
BREAKDOWN_DIMS = ["channel", "device", "new_returning", "country"]
TRAFFIC_DIMS = ["referrer_host", "utm_source", "utm_medium", "utm_campaign"]
ALL_REPORTS = ["overview", "funnel", "answers", "breakdown", "traffic", "products", "versions", "paths"]


# ----------------------------------------------------------------------------- HTTP

class OctaneError(Exception):
    pass


def load_dotenv():
    """Read OCTANE_API_KEY from a .env file next to this script; it overrides any env var."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() == "OCTANE_API_KEY":
                os.environ["OCTANE_API_KEY"] = v.strip().strip('"').strip("'")


def api_key():
    load_dotenv()
    key = os.environ.get("OCTANE_API_KEY", "").strip()
    if not key:
        sys.exit("OCTANE_API_KEY is not set. Put OCTANE_API_KEY=oct_live_... in the .env file next to this script.")
    return key


def get(path, params=None, _retries=5):
    """GET an API route. Retries on 429/503 using Retry-After. Returns parsed JSON."""
    params = {k: v for k, v in (params or {}).items() if v is not None and v != ""}
    url = f"{API}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {api_key()}",
        "Accept": "application/json",
        "User-Agent": "octane-quiz-reports/1.0",
    })
    for attempt in range(_retries):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            try:
                problem = json.loads(body)
            except ValueError:
                problem = {"title": "http_error", "detail": body[:300]}
            if e.code in (429, 503) and attempt < _retries - 1:
                wait = int(e.headers.get("Retry-After") or problem.get("retry_after") or 2)
                print(f"  {e.code} {problem.get('title')}: waiting {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            msg = f"{e.code} {problem.get('title')}: {problem.get('detail')}"
            if problem.get("required"):
                msg += f"  (key needs scope: {', '.join(problem['required'])})"
            if problem.get("errors"):
                msg += "  " + "; ".join(f"{x.get('field')}: {x.get('message')}" for x in problem["errors"])
            if problem.get("request_id"):
                msg += f"  [request_id {problem['request_id']}]"
            raise OctaneError(msg)
        except urllib.error.URLError as e:
            raise OctaneError(f"network error: {e.reason}")
    raise OctaneError("gave up after retries")


def walk(path, **params):
    """Cursor pagination: {"data": [...], "has_more": bool, "next_cursor": ...}."""
    params = {**params, "limit": 100}
    while True:
        page = get(path, params)
        yield from page["data"]
        if not page.get("has_more"):
            return
        params["cursor"] = page["next_cursor"]


# ----------------------------------------------------------------------------- helpers

def today_in(tz_name):
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo(tz_name)).date()
    except Exception:
        return dt.date.today()


def resolve_range(args):
    to = dt.date.fromisoformat(args.to) if args.to else today_in(args.tz)
    frm = dt.date.fromisoformat(getattr(args, "from")) if getattr(args, "from") else to - dt.timedelta(days=29)
    if frm > to:
        sys.exit("--from must be on or before --to")
    days = (to - frm).days + 1
    if days > 366:
        sys.exit("range is over 366 days; the API refuses that")
    return frm.isoformat(), to.isoformat(), days


def money(m):
    if not m:
        return ""
    return m.get("amount", "")


def flatten(row, prefix=""):
    """Flatten nested dicts; Money -> amount; lists -> JSON text."""
    out = {}
    for k, v in row.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            if set(v.keys()) == {"amount", "currency"}:
                out[key] = v["amount"]
                out.setdefault("currency", v["currency"])
            else:
                out.update(flatten(v, key + "."))
        elif isinstance(v, list):
            out[key] = json.dumps(v)
        else:
            out[key] = v
    return out


def write_csv(path, rows):
    rows = [flatten(r) for r in rows if isinstance(r, dict)]
    if not rows:
        return False
    cols = []
    for r in rows:
        for c in r:
            if c not in cols:
                cols.append(c)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    return True


def write_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:60] or "quiz"


def pct(x):
    return "" if x is None else f"{x:.1f}%"


def ms(x):
    if x is None:
        return ""
    s = x / 1000
    return f"{s:.0f}s" if s < 90 else f"{s/60:.1f}m"


# ----------------------------------------------------------------------------- commands

def cmd_quizzes(args):
    print(f"{'id':44}  {'status':10}  {'pages':>5}  name")
    for q in walk("/quizzes", status=args.status, archived=args.archived):
        print(f"{q['id']:44}  {q['status']:10}  {q['page_count']:>5}  {q['name']}")


def find_quiz(ref):
    if re.fullmatch(r"quiz_[0-9a-f]{32}", ref):
        return get(f"/quizzes/{ref}")
    matches = [q for q in walk("/quizzes") if ref.lower() in q["name"].lower()]
    exact = [q for q in matches if q["name"].lower() == ref.lower()]
    if exact:
        return exact[0]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        sys.exit(f"no quiz named like {ref!r}. Run `quizzes` to list them.")
    sys.exit("ambiguous name; matches:\n" + "\n".join(f"  {q['id']}  {q['name']}" for q in matches))


def cmd_report(args):
    quiz = find_quiz(args.quiz)
    qid = quiz["id"]
    frm, to, days = resolve_range(args)
    reports = [r.strip() for r in args.reports.split(",")] if args.reports else ALL_REPORTS
    bad = [r for r in reports if r not in ALL_REPORTS]
    if bad:
        sys.exit(f"unknown report(s): {', '.join(bad)}. Choose from {', '.join(ALL_REPORTS)}")

    filters = {"channel": args.channel, "device": args.device,
               "new_returning": args.new_returning, "country": args.country}
    base = {"from": frm, "to": to, "tz": args.tz, **filters}

    out = os.path.join(args.out, f"{slug(quiz['name'])}_{frm}_{to}")
    os.makedirs(out, exist_ok=True)
    print(f"quiz:   {quiz['name']}  ({qid}, {quiz['status']})")
    print(f"range:  {frm} → {to}  ({days} days, {args.tz})")
    print(f"output: {out}/")

    if days > 92:
        capped = [r for r in reports if r in RAW_CAPPED]
        if capped:
            print(f"note: {', '.join(capped)} are capped at 92 days by the API — skipping them. "
                  f"Re-run with a shorter range to get them.")
            reports = [r for r in reports if r not in RAW_CAPPED]

    results = {}
    P = f"/quizzes/{qid}/analytics"

    def run(name, fn):
        try:
            print(f"  pulling {name} ...", end="", flush=True)
            results[name] = fn()
            print(" ok")
        except OctaneError as e:
            print(f" FAILED\n    {e}")
            results[name] = {"error": str(e)}

    if "overview" in reports:
        def f():
            d = get(f"{P}/overview", base)
            write_json(f"{out}/overview.json", d)
            write_csv(f"{out}/overview_series.csv", d.get("series", []))
            write_csv(f"{out}/overview_totals.csv", [d.get("totals", {})])
            write_csv(f"{out}/overview_markers.csv", d.get("markers", []))
            return d
        run("overview", f)

    if "funnel" in reports:
        def f():
            d = get(f"{P}/funnel", base)
            write_json(f"{out}/funnel.json", d)
            write_csv(f"{out}/funnel_pages.csv", d.get("pages") or [])
            return d
        run("funnel", f)

    if "answers" in reports:
        def f():
            first = get(f"{P}/answers", base)
            questions = first.get("questions", [])
            all_q = {}
            for q in questions:
                ck = q["component_key"]
                d = first if first.get("question", {}).get("component_key") == ck \
                    else get(f"{P}/answers", {**base, "component_key": ck})
                all_q[ck] = d
                safe = slug(ck)
                write_json(f"{out}/answers_{safe}.json", d)
                rows = d.get("options") or d.get("buckets") or []
                write_csv(f"{out}/answers_{safe}.csv", rows)
            return all_q
        run("answers", f)

    if "breakdown" in reports:
        def f():
            res = {}
            for by in BREAKDOWN_DIMS:
                d = get(f"{P}/breakdown", {**base, "by": by})
                res[by] = d
                write_csv(f"{out}/breakdown_{by}.csv", d.get("rows", []))
            write_json(f"{out}/breakdown.json", res)
            return res
        run("breakdown", f)

    if "traffic" in reports:
        def f():
            res = {}
            for by in TRAFFIC_DIMS:
                d = get(f"{P}/traffic", {**base, "by": by, "limit": 50})
                res[by] = d
                rows = list(d.get("rows", []))
                if d.get("other"):
                    rows.append(d["other"])
                write_csv(f"{out}/traffic_{by}.csv", rows)
            write_json(f"{out}/traffic.json", res)
            return res
        run("traffic", f)

    if "products" in reports:
        def f():
            d = get(f"{P}/products", {"from": frm, "to": to, "tz": args.tz, "sort": "revenue"})
            write_json(f"{out}/products.json", d)
            rows = d.get("rows") or d.get("products") or d.get("data") or []
            write_csv(f"{out}/products.csv", rows)
            return d
        run("products", f)

    if "versions" in reports:
        def f():
            d = get(f"{P}/versions", base)
            write_json(f"{out}/versions.json", d)
            rows = d.get("versions") or d.get("rows") or d.get("data") or []
            write_csv(f"{out}/versions.csv", rows)
            return d
        run("versions", f)

    if "paths" in reports:
        def f():
            d = get(f"{P}/paths", base)
            write_json(f"{out}/paths.json", d)
            return d
        run("paths", f)

    write_html(f"{out}/report.html", quiz, frm, to, args.tz, filters, results)
    print(f"\ndone → open {out}/report.html")
    if not args.no_open:
        open_in_browser(f"{out}/report.html")


def cmd_render(args):
    """Rebuild report.html from the JSON a `report` run saved, without calling the API."""
    d = args.dir.rstrip("/")
    load = lambda name: json.load(open(os.path.join(d, name))) if os.path.exists(os.path.join(d, name)) else None
    R = {k: load(f"{k}.json") for k in ("overview", "funnel", "breakdown", "traffic", "products", "versions", "paths")}
    answers = {}
    for name in sorted(os.listdir(d)):
        if name.startswith("answers_") and name.endswith(".json"):
            a = load(name)
            answers[(a.get("question") or {}).get("component_key") or name] = a
    R["answers"] = answers or None
    R = {k: v for k, v in R.items() if v is not None}
    some = next((v for v in R.values() if isinstance(v, dict) and v.get("range")), {}) or next(iter(answers.values()), {})
    rng = some.get("range") or {}
    m = re.match(r"(.+)_(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})$", os.path.basename(d))
    quiz = {"name": m.group(1) if m else os.path.basename(d), "id": some.get("quiz_id", "")}
    write_html(f"{d}/report.html", quiz, rng.get("start", ""), rng.get("end", ""), rng.get("timezone", DEFAULT_TZ),
               some.get("filters") or {}, R)
    print(f"rebuilt {d}/report.html")
    if not args.no_open:
        open_in_browser(f"{d}/report.html")


def open_in_browser(path):
    url = "file://" + os.path.abspath(path)
    if sys.platform == "darwin" and subprocess.run(
            ["open", "-a", "Google Chrome", url], capture_output=True).returncode == 0:
        return
    webbrowser.open(url)


def cmd_store(args):
    frm, to, days = resolve_range(args)
    out = os.path.join(args.out, f"store_{frm}_{to}")
    os.makedirs(out, exist_ok=True)
    base = {"from": frm, "to": to, "tz": args.tz}
    print(f"store-wide  {frm} → {to}  ({args.tz})  → {out}/")
    for name, path, extra in [
        ("overview", "/analytics/overview", {}),
        ("quizzes", "/analytics/quizzes", {"limit": 1000}),
        ("products", "/analytics/products", {"sort": "revenue"}),
    ]:
        try:
            print(f"  pulling {name} ...", end="", flush=True)
            d = get(path, {**base, **extra})
            write_json(f"{out}/{name}.json", d)
            rows = d.get("series") or d.get("rows") or d.get("data") or d.get("quizzes") or []
            write_csv(f"{out}/{name}.csv", rows)
            if d.get("totals"):
                write_csv(f"{out}/{name}_totals.csv", [d["totals"]])
            print(" ok")
        except OctaneError as e:
            print(f" FAILED\n    {e}")
    for by in BREAKDOWN_DIMS:
        try:
            print(f"  pulling breakdown by {by} ...", end="", flush=True)
            d = get("/analytics/breakdown", {**base, "by": by})
            write_csv(f"{out}/breakdown_{by}.csv", d.get("rows", []))
            print(" ok")
        except OctaneError as e:
            print(f" FAILED\n    {e}")
    print("\ndone")


# ----------------------------------------------------------------------------- HTML summary

def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def table(rows, cols, fmt=None):
    fmt = fmt or {}
    if not rows:
        return "<p class='muted'>no rows</p>"
    h = "<table><thead><tr>" + "".join(f"<th>{esc(c[1])}</th>" for c in cols) + "</tr></thead><tbody>"
    for r in rows:
        h += "<tr>"
        for key, _ in cols:
            v = r.get(key)
            if key in fmt:
                v = fmt[key](v)
            elif isinstance(v, dict) and "amount" in v:
                v = f"{v['amount']} {v['currency']}"
            elif v is None:
                v = ""
            h += f"<td>{esc(v)}</td>"
        h += "</tr>"
    return h + "</tbody></table>"


STEP_RE = re.compile(r"\s*(\d+)\s*-\s*([^/]+?)\s*(?:/\s*(.+?))?\s*$")


def funnel_steps(pages):
    """Group branch variants into one row per step, using page titles like '4 - Eating Habit / Teen Only'."""
    steps = []
    for p in pages:
        m = STEP_RE.match(p.get("title") or "")
        num, name, variant = (m.group(1), m.group(2), m.group(3)) if m else (None, p.get("title") or p["page_key"], None)
        if not steps or num is None or steps[-1]["num"] != num:
            steps.append({"num": num, "name": name, "pages": []})
        steps[-1]["pages"].append({**p, "variant": variant or p.get("title")})
    for st in steps:
        ps = st["pages"]
        st["reached"] = sum(p["sessions_reached"] for p in ps)
        st["dropped"] = sum(p["sessions_dropped"] for p in ps)
        timed = [(p["avg_time_on_page_ms"], p["sessions_reached"]) for p in ps if p.get("avg_time_on_page_ms") is not None]
        n = sum(w for _, w in timed)
        st["avg_ms"] = sum(t * w for t, w in timed) / n if n else None
    return steps


def bar(share):
    return (f"<div class='bar'><span style='width:{max(0, min(100, share)):.0f}%'></span></div>"
            f"<span class='barv'>{share:.0f}%</span>")


def funnel_html(fn, paths):
    starts, views = fn["starts"], fn.get("views") or 0
    steps = funnel_steps(fn["pages"] or [])
    h = ["<table class='funnel'><thead><tr><th>Step</th><th>Reached</th><th>% of starts</th>"
         "<th>Dropped here</th><th>Avg time</th></tr></thead><tbody>"]
    # the API's page rows only count sessions that started; the first row adds the view → start step
    # so the funnel's first drop matches the overview start rate
    if views:
        h.append(f"<tr><td>Viewed quiz</td><td>{views}</td><td></td>"
                 f"<td class='drop'>{fn['not_started']} didn't start ({100 * fn['not_started'] / views:.0f}% of views)</td><td></td></tr>")
    in_quiz = starts
    for st in steps:
        label = esc(f"{st['num']} · {st['name']}" if st["num"] is not None else st["name"])
        note = ""
        if st["reached"] < in_quiz:
            note = f"<div class='muted small'>branch page — shown to {st['reached']} of {in_quiz} still in the quiz</div>"
        elif st["reached"] > in_quiz:
            note = f"<div class='muted small'>includes {st['reached'] - in_quiz} repeat page visit(s)</div>"
        drop = f"<td class='drop'>{st['dropped']}</td>" if st["dropped"] else "<td class='muted'>–</td>"
        h.append(f"<tr><td>{label}{note}</td><td>{st['reached']}</td>"
                 f"<td>{bar(100 * st['reached'] / starts) if starts else ''}</td>{drop}<td>{esc(ms(st['avg_ms']))}</td></tr>")
        in_quiz -= st["dropped"]
    h.append("</tbody></table>")
    h.append("<p class='muted'>Branch variants are summed into one row per step. "
             "Rows after “Viewed quiz” count only sessions that started.</p>")

    # where people actually left, with the exact variant page
    leaks = [(st, p) for st in steps for p in st["pages"] if p["sessions_dropped"]]
    if leaks:
        h.append("<h3>Where people left</h3><ul>")
        for st, p in sorted(leaks, key=lambda x: -x[1]["sessions_dropped"]):
            h.append(f"<li><b>{esc(p['title'])}</b> — {p['sessions_dropped']} of {p['sessions_reached']} who reached it "
                     f"(avg {esc(ms(p.get('avg_time_on_page_ms')) or 'n/a')} on page)</li>")
        h.append("</ul>")

    # a sequential move between two variants of the same step usually means an answer has no jump rule
    if isinstance(paths, dict) and paths.get("edges"):
        step_of = {p["page_key"]: st["num"] for st in steps for p in st["pages"] if st["num"] is not None}
        title = {p["page_key"]: p["title"] for st in steps for p in st["pages"]}
        gaps = [e for e in paths["edges"] if e["cause"] == "sequential" and e["from_page_key"] != e["to_page_key"]
                and step_of.get(e["from_page_key"]) is not None
                and step_of.get(e["from_page_key"]) == step_of.get(e["to_page_key"])]
        if gaps:
            h.append("<h3>Branch logic check</h3><ul>")
            for e in gaps:
                h.append(f"<li class='warn'>{e['sessions_moved']} session(s) fell through from "
                         f"<b>{esc(title.get(e['from_page_key']))}</b> to <b>{esc(title.get(e['to_page_key']))}</b> "
                         "with no jump rule — check that every answer on that page has a jump.</li>")
            h.append("</ul>")

    # per-variant detail, collapsed, without the zero-traffic pages
    h.append("<details><summary>Page variants (pages that got traffic)</summary>")
    h.append(table([p for st in steps for p in st["pages"] if p["sessions_reached"]], [
        ("title", "Page"), ("sessions_reached", "Reached"), ("sessions_continued", "Continued"),
        ("sessions_dropped", "Dropped"), ("sessions_went_back", "Went back"), ("avg_time_on_page_ms", "Avg time")],
        fmt={"avg_time_on_page_ms": ms}))
    unseen = [p["title"] for st in steps for p in st["pages"] if not p["sessions_reached"]]
    if unseen:
        h.append(f"<p class='muted'>No traffic in range ({len(unseen)} pages): {esc(', '.join(unseen))}</p>")
    h.append("</details>")
    return "".join(h)


def _outcome(rows):
    """Completed / opted in / buyers / revenue summed over options, or None if the options overlap."""
    if not rows:
        return None
    return {**{k: sum(r.get(k) or 0 for r in rows) for k in ("chose", "completed", "opted_in", "buyers", "answer_orders")},
            "revenue": sum(float((r.get("answer_revenue") or {}).get("amount") or 0) for r in rows)}


def _outcome_line(base, completed, opted, buyers, revenue=0.0):
    rate = lambda n: f"{100 * n / base:.0f}%" if base else "–"
    return (f"<span class='o'>completed <b>{rate(completed)}</b> ({completed} of {base})</span>"
            f"<span class='o'>opted in <b>{rate(opted)}</b> ({opted})</span>"
            f"<span class='o{' good' if buyers else ''}'>bought <b>{rate(buyers)}</b> ({buyers})"
            f"{f' · ${revenue:,.0f}' if revenue else ''}</span>")


def branch_tree_html(fn, paths, answers):
    """
    The funnel as a tree: who they picked on the first question decides the branch, then
    the branch's own first question splits it again, each node with what those people did.

    Built from what the API gives. A branch is the page the first question's jump rules send
    people to (from paths); its pages are the ones sharing that page's suffix
    (problems-kid → eating-kid, sleep-kid ...). Outcomes per answer come from the answers
    report. Questions are multi-select, so answers overlap and are never summed; a branch's
    totals come from one of its single-select questions, where every person picked exactly one.
    """
    if not (isinstance(paths, dict) and paths.get("edges") and isinstance(answers, dict) and answers):
        return ""
    by_page = {d["question"]["page_key"]: d for d in answers.values() if isinstance(d, dict) and d.get("question")}
    questions = (next(iter(answers.values())) or {}).get("questions") or []
    first = next((q for q in questions if (by_page.get(q["page_key"]) or {}).get("answered")), None)
    if not first:
        return ""
    q1 = by_page[first["page_key"]]
    pages = {p["page_key"]: p for p in fn.get("pages") or []}
    roots = sorted([e for e in paths["edges"] if e["from_page_key"] == first["page_key"] and e["cause"] == "jump"
                    and e["to_page_key"] in pages], key=lambda e: -e["sessions_moved"])
    if len(roots) < 2:
        return ""  # the first question doesn't branch; the step table says it all

    step_no = lambda key: int(m.group(1)) if (m := STEP_RE.match(pages[key].get("title") or "")) and m.group(1) else 999
    variant = lambda key: (STEP_RE.match(pages[key].get("title") or "") or [None] * 4)[3] or pages[key].get("title")
    suffix = lambda key: key.split("-", 1)[1] if "-" in key else key
    single = lambda d: d.get("options") and sum(o["chose"] for o in d["options"]) == d.get("answered")
    starts = fn.get("starts") or 0

    h = [f"<div class='tree'><div class='node root'><div class='nh'><b>{starts}</b> started the quiz</div>"]
    chips = "".join(f"<span class='chip'>{esc(o['label'].strip())} <b>{o['chose']}</b></span>"
                    for o in q1.get("options") or [] if o["chose"])
    h.append(f"<div class='q'>Q1 · {esc(first['question'])} <span class='muted'>({q1.get('answered')} answered · pick more than one)</span>"
             f"<div class='chips'>{chips}</div></div>")
    h.append("<div class='kids'>")

    last_branch_step = 0
    for e in roots:
        root = e["to_page_key"]
        sfx = suffix(root)
        own = sorted([k for k in pages if suffix(k) == sfx and pages[k]["sessions_reached"]], key=step_no)
        last_branch_step = max([last_branch_step] + [step_no(k) for k in own if step_no(k) != 999])
        reached = pages[root]["sessions_reached"]
        # one line of the branch's own pages: reached at each, and who left where
        trail = " → ".join(
            f"{esc((STEP_RE.match(pages[k]['title']) or [None, None, pages[k]['title']])[2])} {pages[k]['sessions_reached']}"
            + (f" <span class='drop'>−{pages[k]['sessions_dropped']}</span>" if pages[k]["sessions_dropped"] else "")
            for k in own)
        totals = next((_outcome(by_page[k]["options"]) for k in own if k in by_page and single(by_page[k])), None)
        out = _outcome_line(reached, totals["completed"], totals["opted_in"], totals["buyers"], totals["revenue"]) if totals \
            else "<span class='muted'>no single-choice question here to total the branch</span>"
        share = f"{100 * reached / starts:.0f}%" if starts else ""
        h.append(f"<details class='node' open><summary class='nh'><b>{esc(variant(root))}</b> "
                 f"<span class='muted'>{reached} people · {share} of starts</span><div class='outs'>{out}</div></summary>")
        h.append(f"<p class='trail muted small'>{trail}</p>")

        q2 = by_page.get(root)
        if q2 and q2.get("options"):
            h.append(f"<div class='q'>Q2 · {esc(q2['question']['question'])} <span class='muted'>({q2.get('answered')} answered"
                     f"{'' if single(q2) else ' · pick more than one, so these overlap'})</span></div><div class='kids'>")
            for o in sorted([o for o in q2["options"] if o["chose"]], key=lambda o: -o["chose"]):
                rev = float((o.get("answer_revenue") or {}).get("amount") or 0)
                h.append(f"<div class='node leaf'><div class='nh'>{esc(o['label'].strip())} <span class='muted'>{o['chose']}</span>"
                         f"<div class='outs'>{_outcome_line(o['chose'], o['completed'], o['opted_in'], o['buyers'], rev)}</div></div></div>")
            h.append("</div>")
            later = [k for k in own if k != root and k in by_page and by_page[k].get("options")]
            if later:
                h.append("<details class='later'><summary class='small'>The rest of this branch's questions</summary>")
                for k in later:
                    d = by_page[k]
                    h.append(f"<div class='q small'>{esc(d['question']['question'])} <span class='muted'>({d.get('answered')} answered)</span><div class='chips'>"
                             + "".join(f"<span class='chip'>{esc(o['label'].strip())} <b>{o['chose']}</b></span>"
                                       for o in sorted(d["options"], key=lambda o: -o["chose"]) if o["chose"])
                             + "</div></div>")
                h.append("</details>")
        h.append("</details>")
    h.append("</div>")

    # the steps every branch shares at the end; a step with variants (education by answer) is summed
    tail = [st for st in funnel_steps(fn.get("pages") or []) if st["reached"]
            and (st["num"] is None or int(st["num"]) > last_branch_step)]
    if tail:
        h.append("<div class='node root'><div class='nh'>Then everyone, together</div><p class='trail muted small'>"
                 + " → ".join(f"{esc(st['name'])}{' (varies)' if len(st['pages']) > 1 else ''} {st['reached']}"
                              + (f" <span class='drop'>−{st['dropped']}</span>" if st["dropped"] else "")
                              for st in tail)
                 + f"</p><div class='outs'><span class='o'>completed <b>{100 * (fn.get('completions') or 0) / starts:.0f}%</b> "
                   f"({fn.get('completions') or 0} of {starts} who started)</span></div></div>")
    h.append("</div>")
    if starts < 100:
        h.append(f"<p class='warn small'>Only {starts} starts in range: read these as early signs, not rates.</p>")
    return "".join(h)


OPTION_COLS = [("label", "Option"), ("chose", "Chose"), ("chose_share", "Share"), ("completed", "Completed"),
               ("completion_rate", "Completion"), ("opted_in", "Opted in"), ("buyers", "Buyers"),
               ("buyer_rate", "Buyer rate"), ("answer_orders", "Orders"), ("answer_revenue", "Revenue")]
OPTION_FMT = {"chose_share": pct, "completion_rate": pct, "buyer_rate": pct}

# words a quiz uses for who a question is about; swapped for "[demo]" when audience versions are merged
AUDIENCE_RE = re.compile(r"\b(?:your\s+)?(?:kids?|teens?|child(?:ren)?|family)\b", re.I)


def merge_options(variants):
    """Sum option rows with the same label across audience variants of one question."""
    rows, answered = {}, sum(d.get("answered") or 0 for d in variants)
    for d in variants:
        for o in d.get("options") or []:
            r = rows.setdefault(o["label"], {"label": o["label"], "chose": 0, "completed": 0, "opted_in": 0,
                                             "buyers": 0, "answer_orders": 0, "rev": 0.0, "cur": ""})
            for k in ("chose", "completed", "opted_in", "buyers", "answer_orders"):
                r[k] += o.get(k) or 0
            rev = o.get("answer_revenue") or {}
            r["rev"] += float(rev.get("amount") or 0)
            r["cur"] = rev.get("currency") or r["cur"]
    for r in rows.values():
        r["chose_share"] = 100 * r["chose"] / answered if answered else None
        r["completion_rate"] = 100 * r["completed"] / r["chose"] if r["chose"] else None
        r["buyer_rate"] = 100 * r["buyers"] / r["chose"] if r["chose"] else None
        r["answer_revenue"] = f"{r['rev']:g}"
    return list(rows.values()), answered


# categorical slots in fixed order (validated: CVD-safe on a light surface); past 7 options fold into "Other"
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6"]
OTHER_COLOR = "#9a9993"


def donut(rows, answered, colors):
    """Donut of every pick. Questions are multi-select, so slices are shares of picks; the legend gives % of people."""
    picked = [r for r in rows if r["chose"]]
    if not picked:
        return ""
    slices = [(r["label"], r["chose"], colors.get(r["label"], OTHER_COLOR)) for r in picked if colors.get(r["label"]) != OTHER_COLOR]
    other = sum(r["chose"] for r in picked if colors.get(r["label"]) == OTHER_COLOR)
    if other:
        slices.append(("Other", other, OTHER_COLOR))
    total = sum(n for _, n, _ in slices)
    cx = cy = 60
    ro, ri = 56, 34
    svg = [f"<svg viewBox='0 0 120 120' width='150' height='150' role='img' aria-label='answer split'>"]
    if len(slices) == 1:
        label, n, c = slices[0]
        svg.append(f"<circle cx='{cx}' cy='{cy}' r='{(ro + ri) / 2}' fill='none' stroke='{c}' stroke-width='{ro - ri}'>"
                   f"<title>{esc(label)}: {n} pick{'s' * (n != 1)} (100%)</title></circle>")
    else:
        a = -math.pi / 2
        for label, n, c in slices:
            b = a + 2 * math.pi * n / total
            big = 1 if b - a > math.pi else 0
            p = lambda r, t: f"{cx + r * math.cos(t):.2f} {cy + r * math.sin(t):.2f}"
            svg.append(f"<path d='M {p(ro, a)} A {ro} {ro} 0 {big} 1 {p(ro, b)} L {p(ri, b)} A {ri} {ri} 0 {big} 0 {p(ri, a)} Z' "
                       f"fill='{c}' stroke='#fff' stroke-width='2'><title>{esc(label)}: {n} pick{'s' * (n != 1)} · "
                       f"{100 * n / total:.0f}% of picks · {100 * n / answered:.0f}% of people</title></path>")
            a = b
    svg.append(f"<text x='60' y='58' text-anchor='middle' class='dc1'>{total}</text>"
               f"<text x='60' y='72' text-anchor='middle' class='dc2'>pick{'s' * (total != 1)}</text></svg>")
    legend = "".join(f"<li><span class='sw' style='background:{c}'></span>{esc(label)}"
                     f"<span class='lv'>{n} · {100 * n / answered:.0f}% of people</span></li>" for label, n, c in slices)
    note = f"{total} pick{'s' * (total != 1)} from {answered} {'person' if answered == 1 else 'people'}" + (" — people could pick more than one" if total > answered else "")
    return (f"<div class='donut'>{''.join(svg)}<div><ul class='legend'>{legend}</ul>"
            f"<p class='muted small'>{note}</p></div></div>")


def answers_html(an, page_order):
    """One block per quiz step: audience variants of a question are merged, with a filter to split them back out."""
    order = {k: i for i, k in enumerate(page_order)}
    groups = {}
    for ck, d in an.items():
        q = d.get("question") or {}
        m = STEP_RE.match(q.get("page_title") or "")
        key = m.group(1) if m else q.get("page_key") or ck
        variant = m.group(3) if m and m.group(3) and m.group(3).lower() != "all" else None
        g = groups.setdefault(key, {"name": m.group(2) if m else q.get("page_title") or ck, "num": m.group(1) if m else None,
                                    "pos": order.get(q.get("page_key"), 10 ** 6), "items": []})
        g["pos"] = min(g["pos"], order.get(q.get("page_key"), 10 ** 6))
        g["items"].append((variant, d))
    # fold typo'd audience names ("Youger Kid + Teen") into the most-used close spelling
    counts = {}
    for g in groups.values():
        for v, _ in g["items"]:
            if v:
                counts[v] = counts.get(v, 0) + 1
    canon = {}
    for v in sorted(counts, key=lambda v: -counts[v]):
        canon[v] = next((c for c in canon.values() if difflib.SequenceMatcher(None, v.lower(), c.lower()).ratio() > 0.9), v)
    for g in groups.values():
        g["items"] = [(canon.get(v, v), d) for v, d in g["items"]]
    audiences = sorted({v for g in groups.values() for v, _ in g["items"] if v},
                       key=lambda v: -sum(d.get("answered") or 0 for g in groups.values() for vv, d in g["items"] if vv == v))

    h = ["<div class='filter'><label>Audience: <select id='aud' onchange='setAud(this.value)'>"
         "<option value=''>All audiences (combined)</option>"
         + "".join(f"<option value='{esc(v)}'>{esc(v)}</option>" for v in audiences) + "</select></label>"
         "<span class='muted small'> Questions asked to every audience aren't split, so they show everyone.</span></div>"]
    for g in sorted(groups.values(), key=lambda g: g["pos"]):
        items = g["items"]
        split = any(v for v, _ in items)
        lead = max(items, key=lambda x: x[1].get("answered") or 0)[1]
        title = (lead.get("question") or {}).get("question") or g["name"]
        if split:
            title = AUDIENCE_RE.sub("[demo]", title)
        label = f"{g['num']} · {g['name']}" if g["num"] is not None else g["name"]
        h.append(f"<div class='qgroup'><h3>{esc(title)}</h3><p class='muted small'>{esc(label)}"
                 + (f" · asked as {len(items)} audience versions" if split else " · same question for everyone") + "</p>")
        if any(d.get("buckets") for _, d in items):
            # number questions: no merge, show each version's bands
            for v, d in items:
                h.append(f"<div class='aud' data-aud='{esc(v or '')}'>" + table(d.get("buckets") or [], [
                    ("label", "Band"), ("answered", "Answered"), ("answered_share", "Share"), ("completed", "Completed"),
                    ("completion_rate", "Completion"), ("opted_in", "Opted in")],
                    fmt={"answered_share": pct, "completion_rate": pct}) + "</div>")
            h.append("</div>")
            continue
        rows, answered = merge_options([d for _, d in items])
        # color follows the option, so a slice keeps its color when the audience filter changes
        picked = [r["label"] for r in rows if r["chose"]]
        colors = {lab: SERIES[i] if i < len(SERIES) else OTHER_COLOR for i, lab in enumerate(picked)}
        h.append(f"<div class='aud' data-aud=''><p class='small'>{answered} answered"
                 + (" · " + ", ".join(f"{esc(v)}: {d.get('answered') or 0}" for v, d in items if d.get("answered")) if split else "")
                 + "</p>" + (donut(rows, answered, colors) + table([r for r in rows if r["chose"] or not split], OPTION_COLS, OPTION_FMT)
                    + (f"<p class='muted small'>{sum(1 for r in rows if not r['chose'])} options across the audience versions "
                       "weren't picked — choose an audience above to see its full list</p>" if split and any(not r["chose"] for r in rows) else "")
                    if answered else "<p class='muted'>nobody answered in range</p>") + "</div>")
        if split:
            for v, d in items:
                vr, va = merge_options([d])
                h.append(f"<div class='aud' data-aud='{esc(v)}' hidden><p class='small'>{va} answered · {esc(v)}"
                         + (f" · asked as “{esc((d.get('question') or {}).get('question'))}”" if va else "") + "</p>"
                         + (donut(vr, va, colors) + table(vr, OPTION_COLS, OPTION_FMT) if va else "<p class='muted'>nobody in this audience answered</p>")
                         + "</div>")
            missing = [a for a in audiences if a not in {v for v, _ in items}]
            for a in missing:
                h.append(f"<div class='aud' data-aud='{esc(a)}' hidden><p class='muted'>not asked to {esc(a)}</p></div>")
        h.append("</div>")
    h.append("""<script>
function setAud(a){document.querySelectorAll('.qgroup').forEach(function(g){
  var blocks=g.querySelectorAll('.aud'), hit=false;
  blocks.forEach(function(b){if(b.dataset.aud===a)hit=true;});
  blocks.forEach(function(b){b.hidden=hit?b.dataset.aud!==a:b.dataset.aud!=='';});});}
</script>""")
    return "".join(h)


def write_html(path, quiz, frm, to, tz, filters, R):
    parts = []
    active = {k: v for k, v in filters.items() if v}
    parts.append(f"<h1>{esc(quiz['name'])}</h1>"
                 f"<p class='muted'>{esc(quiz['id'])} · {frm} → {to} · {esc(tz)}"
                 + (f" · filters: {esc(json.dumps(active))}" if active else "") + "</p>")

    ov = R.get("overview")
    if ov and "totals" in ov:
        t = ov["totals"]
        fr = ov.get("freshness", {})
        cards = [
            ("Views", t["views"]), ("Starts", t["starts"]), ("Completions", t["completions"]),
            ("Start rate (starts ÷ views)", pct(t.get("start_rate"))),
            ("Start rate (starts ÷ unique viewers, approx)",
             pct(100 * t["starts"] / t["unique_viewers_approx"]) if t.get("unique_viewers_approx") else ""),
            ("Completion rate (completions ÷ starts)", pct(t.get("completion_rate"))),
            ("Email opt-ins", t["email_opt_ins"]), ("Phone opt-ins", t["phone_opt_ins"]),
            ("Orders", t["orders"]), ("Revenue", money(t.get("revenue"))), ("AOV", money(t.get("aov"))),
            ("Median time to complete", ms(t.get("median_time_to_complete_ms"))),
            ("Unique viewers (approx)", t.get("unique_viewers_approx")),
        ]
        parts.append("<h2>Overview</h2><div class='cards'>" +
                     "".join(f"<div class='card'><div class='k'>{esc(k)}</div><div class='v'>{esc(v)}</div></div>"
                             for k, v in cards) + "</div>")
        rb = t.get("revenue_by_bucket")
        if rb:
            parts.append("<p class='muted'>Revenue split — added from quiz: "
                         f"{money(rb.get('added_from_quiz'))} · shown, bought later: {money(rb.get('shown_bought_later'))}"
                         f" · bought, not shown: {money(rb.get('bought_not_shown'))}</p>")
        parts.append(f"<p class='muted'>Data settled through {esc(fr.get('closed_days_through'))}"
                     f"{' · today is live' if fr.get('today_live') else ''}"
                     f"{' · STALE rollup' if fr.get('stale') else ''}{' · PENDING first rollup' if fr.get('pending') else ''}</p>")
        if ov.get("markers"):
            parts.append("<p class='muted'>Publishes in range: " + ", ".join(
                f"v{m.get('version_number')} on {m['day']}" for m in ov["markers"]) + "</p>")
        parts.append("<details><summary>Daily series</summary>" + table(
            ov["series"], [("day", "Day"), ("views", "Views"), ("starts", "Starts"), ("completions", "Completions"),
                           ("email_opt_ins", "Email"), ("phone_opt_ins", "Phone"), ("orders", "Orders"),
                           ("revenue", "Revenue")]) + "</details>")
    elif ov and "error" in ov:
        parts.append(f"<h2>Overview</h2><p class='err'>{esc(ov['error'])}</p>")

    fn = R.get("funnel")
    if fn and "pages" in fn:
        parts.append(f"<h2>Funnel</h2><p class='muted'>starts {fn['starts']} → completions {fn['completions']} · "
                     f"not started {fn['not_started']} · still in progress {fn['in_progress']}</p>")
        tree = branch_tree_html(fn, R.get("paths"), R.get("answers"))
        if tree:
            parts.append("<h3>By branch</h3>" + tree + "<h3>Step by step</h3>")
        parts.append(funnel_html(fn, R.get("paths")))
    elif fn and "error" in fn:
        parts.append(f"<h2>Funnel</h2><p class='err'>{esc(fn['error'])}</p>")

    an = R.get("answers")
    if isinstance(an, dict) and "error" not in an:
        parts.append("<h2>Answers</h2>")
        if not an:
            parts.append("<p class='muted'>no choice or number questions on the published version</p>")
        else:
            fn_pages = (R.get("funnel") or {}).get("pages") or []
            parts.append(answers_html(an, [p["page_key"] for p in fn_pages]))
    elif isinstance(an, dict) and "error" in an:
        parts.append(f"<h2>Answers</h2><p class='err'>{esc(an['error'])}</p>")

    bd = R.get("breakdown")
    if isinstance(bd, dict) and "error" not in bd:
        parts.append("<h2>Breakdown</h2>")
        for by, d in bd.items():
            parts.append(f"<h3>by {esc(by)}</h3>" + table(d.get("rows", []), [
                ("value", by), ("views", "Views"), ("starts", "Starts"), ("completions", "Completions"),
                ("email_opt_ins", "Email"), ("phone_opt_ins", "Phone"), ("unique_viewers_approx", "Unique (approx)")]))
    elif isinstance(bd, dict):
        parts.append(f"<h2>Breakdown</h2><p class='err'>{esc(bd['error'])}</p>")

    tr = R.get("traffic")
    if isinstance(tr, dict) and "error" not in tr:
        parts.append("<h2>Traffic</h2>")
        for by, d in tr.items():
            rows = list(d.get("rows", [])) + ([d["other"]] if d.get("other") else [])
            parts.append(f"<h3>by {esc(by)}</h3>" + table(rows, [
                ("value", by), ("views", "Views"), ("starts", "Starts"), ("completions", "Completions"),
                ("email_opt_ins", "Email"), ("phone_opt_ins", "Phone"), ("unique_viewers", "Unique")]))
    elif isinstance(tr, dict):
        parts.append(f"<h2>Traffic</h2><p class='err'>{esc(tr['error'])}</p>")

    for name in ("products", "versions"):
        d = R.get(name)
        if not d:
            continue
        if "error" in d:
            parts.append(f"<h2>{name.title()}</h2><p class='err'>{esc(d['error'])}</p>")
            continue
        rows = d.get("rows") or d.get(name) or d.get("data") or []
        if rows and isinstance(rows[0], dict):
            cols = [(k, k) for k in flatten(rows[0]).keys() if k != "currency"][:12]
            parts.append(f"<h2>{name.title()}</h2>" + table([flatten(r) for r in rows], cols))
        else:
            parts.append(f"<h2>{name.title()}</h2><p class='muted'>see {name}.json</p>")

    if R.get("paths"):
        d = R["paths"]
        parts.append("<h2>Paths</h2>" + (f"<p class='err'>{esc(d['error'])}</p>" if "error" in d
                                         else "<p class='muted'>see paths.json for the page-to-page edges</p>"))

    # starred metrics stay open at the top; everything else folds into "All metrics"
    header, body = parts[0], parts[1:]
    star = ""
    t = (ov or {}).get("totals") if isinstance(ov, dict) else None
    if t:
        uv = t.get("unique_viewers_approx")
        rate = pct(100 * t["starts"] / uv) if uv else "n/a"
        star = ("<details open class='starred'><summary><h2>★ Starred metrics</h2></summary>"
                "<div class='hero'><div class='k'>Start rate</div>"
                f"<div class='v'>{esc(rate)}</div>"
                f"<div class='muted'>{t['starts']} starts ÷ {esc(uv)} unique viewers (approx). "
                "The API reports starts as sessions, not unique people, so one person who starts twice counts twice.</div>"
                "</div></details>")
    parts = [header, star, "<details class='all'><summary><h2>All metrics</h2></summary>"] + body + ["</details>"]

    html = f"""<!doctype html><html><head><meta charset="utf-8"><title>{esc(quiz['name'])} — Octane AI report</title>
<style>
body{{font:14px/1.5 -apple-system,Segoe UI,Helvetica,Arial,sans-serif;max-width:1100px;margin:32px auto;padding:0 16px;color:#1a1a1a}}
h1{{margin-bottom:0}} h2{{margin-top:36px;border-bottom:1px solid #ddd;padding-bottom:4px}} h3{{margin:20px 0 6px}}
.muted{{color:#666}} .err{{color:#b00020;background:#fdecec;padding:8px 12px;border-radius:6px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px;margin:12px 0}}
.card{{border:1px solid #e3e3e3;border-radius:8px;padding:10px 12px}} .card .k{{font-size:12px;color:#666}} .card .v{{font-size:20px;font-weight:600}}
table{{border-collapse:collapse;width:100%;margin:8px 0;font-size:13px}} th,td{{border-bottom:1px solid #eee;padding:6px 8px;text-align:left;vertical-align:top}}
th{{background:#fafafa;position:sticky;top:0}} td:not(:first-child),th:not(:first-child){{text-align:right}}
details summary{{cursor:pointer;color:#444;margin:8px 0}}
details.starred>summary,details.all>summary{{list-style:none;margin:0}} details.starred>summary h2,details.all>summary h2{{display:flex;gap:8px}}
details.starred>summary h2::before,details.all>summary h2::before{{content:"▸";color:#999}}
details.starred[open]>summary h2::before,details.all[open]>summary h2::before{{content:"▾"}}
.hero{{border:1px solid #e3e3e3;border-radius:10px;padding:18px 22px;margin:12px 0;max-width:420px}}
.bar{{display:inline-block;width:120px;height:8px;background:#eee;border-radius:4px;vertical-align:middle;margin-right:8px}}
.bar span{{display:block;height:100%;background:#4a7bd0;border-radius:4px}} .barv{{display:inline-block;width:36px}}
table.funnel td:nth-child(3){{white-space:nowrap}} .drop{{color:#b00020;font-weight:600}} .small{{font-size:12px}}
.warn{{color:#8a5a00}}
.filter{{position:sticky;top:0;background:#fff;padding:8px 0;z-index:2;border-bottom:1px solid #eee}} .filter select{{font:inherit;padding:4px 6px}}
.donut{{display:flex;gap:20px;align-items:center;margin:8px 0}} .dc1{{font-size:18px;font-weight:700;fill:#1a1a1a}} .dc2{{font-size:9px;fill:#666}}
.legend{{list-style:none;padding:0;margin:0}} .legend li{{display:flex;align-items:center;gap:8px;margin:3px 0}}
.sw{{width:10px;height:10px;border-radius:3px;flex:none}} .lv{{color:#666;margin-left:6px}}
.qgroup{{margin:18px 0 28px}} .qgroup h3{{margin-bottom:0}} .qgroup>p.small{{margin:2px 0 6px}}
.tree{{margin:8px 0 4px}} .tree .kids{{margin-left:14px;padding-left:14px;border-left:2px solid #e3e3e3}}
.tree .node{{border:1px solid #e3e3e3;border-radius:8px;padding:8px 12px;margin:8px 0;background:#fff}}
.tree .node.root{{background:#f7f9fc}} .tree .node.leaf{{padding:6px 10px;border-color:#eee}}
.tree details.node>summary{{margin:0;list-style:none}} .tree details.node>summary::-webkit-details-marker{{display:none}}
.tree details.node>summary>b::before{{content:"▸ ";color:#999}} .tree details.node[open]>summary>b::before{{content:"▾ "}}
.tree .nh{{color:#1a1a1a}} .tree .q{{margin:8px 0 2px;font-weight:600;font-size:13px}} .tree .q .muted{{font-weight:400}}
.tree .outs{{display:flex;flex-wrap:wrap;gap:4px 14px;font-size:12px;color:#444;margin-top:2px}} .tree .o b{{color:#1a1a1a}}
.tree .o.good b{{color:#1a7f37}} .tree .trail{{margin:4px 0}} .tree .later>summary{{margin:6px 0 0}}
.chips{{display:flex;flex-wrap:wrap;gap:6px;margin-top:4px;font-weight:400}}
.chip{{font-size:12px;border:1px solid #e3e3e3;border-radius:10px;padding:1px 9px;background:#fafafa}}
.hero .k{{font-size:13px;color:#666}} .hero .v{{font-size:44px;font-weight:700;line-height:1.1;margin:4px 0 8px}}
</style></head><body>{''.join(parts)}
<p class='muted' style='margin-top:40px'>Generated {dt.datetime.now().strftime('%Y-%m-%d %H:%M')} by octane_quiz_reports.py · raw JSON and CSVs sit next to this file.</p>
</body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)


# ----------------------------------------------------------------------------- CLI

def main():
    ap = argparse.ArgumentParser(description="Pull Octane AI quiz analytics reports.",
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog="Run with -h on a subcommand for its options. Full notes at the top of this file.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    q = sub.add_parser("quizzes", help="list quizzes the key can see")
    q.add_argument("--status", choices=["draft", "published"])
    q.add_argument("--archived", choices=["true", "false"])
    q.set_defaults(fn=cmd_quizzes)

    def range_args(p):
        p.add_argument("--from", dest="from", metavar="YYYY-MM-DD", help="first day (default: 29 days before --to)")
        p.add_argument("--to", metavar="YYYY-MM-DD", help="last day (default: today in --tz)")
        p.add_argument("--tz", default=DEFAULT_TZ, help=f"IANA timezone (default {DEFAULT_TZ})")
        p.add_argument("--out", default="octane_reports", help="output folder (default octane_reports/)")

    r = sub.add_parser("report", help="pull every report for one quiz")
    r.add_argument("--quiz", required=True, help="quiz id (quiz_...) or part of its name")
    range_args(r)
    r.add_argument("--reports", help="comma list, default all: " + ",".join(ALL_REPORTS))
    r.add_argument("--channel", help="direct, organic_search, paid_search, organic_social, paid_social, email, sms, affiliate, display, paid_other, referral, ai_assistant, other, shared_results")
    r.add_argument("--device", choices=["mobile", "tablet", "desktop"])
    r.add_argument("--new-returning", dest="new_returning", choices=["new", "returning"])
    r.add_argument("--country", help="ISO alpha-2, e.g. US")
    r.add_argument("--no-open", action="store_true", help="don't open report.html in Chrome when done")
    r.set_defaults(fn=cmd_report)

    rr = sub.add_parser("render", help="rebuild report.html from a folder a report run saved (no API calls)")
    rr.add_argument("dir", help="e.g. octane_reports/v2-branch-logic_2026-10-01_2026-10-29")
    rr.add_argument("--no-open", action="store_true")
    rr.set_defaults(fn=cmd_render)

    s = sub.add_parser("store", help="store-wide overview, per-quiz comparison, products, breakdowns")
    range_args(s)
    s.set_defaults(fn=cmd_store)

    args = ap.parse_args()
    try:
        args.fn(args)
    except OctaneError as e:
        sys.exit(f"error: {e}")
    except KeyboardInterrupt:
        sys.exit(1)


if __name__ == "__main__":
    main()

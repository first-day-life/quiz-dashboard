# Octane quiz dashboard (v2 Branch Logic)

A static dashboard for First Day's Octane AI quiz, hosted on GitHub Pages. A GitHub Action re-pulls the data every hour. It replaces the earlier Claude dashboard.

**This repo and site are public, but the data is not readable.** The Action encrypts it (AES-256-GCM) before committing, and the page only decrypts it with the key in the private link:

```
https://first-day-life.github.io/quiz-dashboard/#k=<DASHBOARD_KEY>
```

```
GitHub Action (hourly + manual)                                GitHub Pages (static, public)
  OCTANE_API_KEY, DASHBOARD_KEY (repo secrets)                   index.html + assets/
  scripts/pull.py → build/*.json (plaintext, never committed)    fetch data/bundle.enc?t=…
  scripts/encrypt.mjs → data/bundle.enc + data/meta.json ─commit─▶ decrypt in browser with #k= → render
```

- Only `data/bundle.enc` (ciphertext) and `data/meta.json` (`{pulled_at}`, so the locked page can say when data was last pulled) are ever committed. Readable numbers never appear in the repo or its history.
- The key sits after `#` in the link. Browsers never send that part to the server, so it doesn't show up in GitHub's logs or in referrers. Without it, the page shows a lock screen where someone can paste a key.
- **Anyone who has the link can see the dashboard.** Share it like a password. To revoke access, rotate the key (see below) and send the new link to the people who should still have it.

- **The Action** (`.github/workflows/refresh.yml`) runs at :07 every hour and whenever you start it by hand. It pulls the last 14 days (America/Chicago), reshapes the reports, encrypts them, commits `data/bundle.enc` and `data/meta.json` (`pulled_at`), and redeploys Pages.
- **The Refresh button** reloads the most recently committed JSON and updates "last pulled". A static page can't call Octane itself: the browser would block the request (CORS) and the key would be exposed. So the Action is the only thing that calls Octane. **Pull from Octane now ↗** opens the workflow page, where you can click *Run workflow*. Press Refresh about a minute later.
- The Octane key lives only in the repo secret. It is never in client JS and never committed (`.env` is gitignored).

## Setup

1. **Create a READ-ONLY Octane key.** In Octane, go to *Developer → API keys*. Give it only *Read quizzes* and *Read analytics*. Don't use a key with write scopes; this dashboard only reads.
2. **Add the secret.** In the repo, go to *Settings → Secrets and variables → Actions → New repository secret*. Name it `OCTANE_API_KEY` and paste the key as the value.
   Or from a terminal: `gh secret set OCTANE_API_KEY -R first-day-life/octane-quiz-dashboard`, then paste the key.
3. **Add the dashboard key.** Generate one with `node scripts/encrypt.mjs --new-key` and save it as the secret `DASHBOARD_KEY`. That value is the `#k=` part of the private link.
4. **Enable Pages.** Go to *Settings → Pages → Build and deployment → Source: **GitHub Actions***.
5. **Run it once.** Go to *Actions → Refresh quiz data → Run workflow*. Then open `https://first-day-life.github.io/quiz-dashboard/#k=<DASHBOARD_KEY>`.

### Rotating the key (revoking access)

1. Run `node scripts/encrypt.mjs --new-key` and run `gh secret set DASHBOARD_KEY -R first-day-life/quiz-dashboard`, pasting in the new key.
2. Run the workflow. The next bundle can only be opened with the new key, and old links show the lock screen.
3. Send the new link to the people who should keep access.

## Pointing at a different quiz or date range

Set these under *Settings → Secrets and variables → Actions → **Variables***. Nothing needs to change in the code.

| Variable | Default | Meaning |
|---|---|---|
| `QUIZ_NAME` | `v2 \| Branch Logic` | The quiz is looked up by exact name with `GET /v1/quizzes`. |
| `QUIZ_ID` | `quiz_1a69eb0c9b684adb9c4716ad98428013` | Used if no quiz has that name. |
| `DAYS` | `14` | Window length, ending today. 92 days at most (Octane's cap for funnel, answers and paths). |
| `TZ_NAME` | `America/Chicago` | Timezone for the day boundaries. |

For a one-off custom range, *Run workflow* also takes `from` and `to` dates (YYYY-MM-DD). The next hourly run goes back to the default window.

The page title is in `index.html` (`<title>` and `<h1>`).

## Running locally

```sh
# .env (gitignored):  OCTANE_API_KEY=oct_live_...   DASHBOARD_KEY=<same as the repo secret>
python3 scripts/pull.py           # plaintext → build/ (Python 3.9+, stdlib only)
node scripts/encrypt.mjs          # → data/bundle.enc + data/meta.json (Node 18+, no packages)
python3 -m http.server 8000       # open http://localhost:8000/#k=<DASHBOARD_KEY>
```

Don't commit a locally generated `data/` unless you want to overwrite the published data. The Action replaces it within the hour anyway.

`octane_quiz_reports.py` is the older CLI that pulls every report into CSV and HTML. It still works for ad-hoc digging.

## How the data is reshaped (`scripts/pull.py`)

Octane returns one funnel page and one answer question for each **audience version** of a step. Their titles look like `2 - Problems / Younger Kid Only`. The typo "Youger" is corrected to "Younger".

- **Funnel:** pages are grouped by their leading step number N. `sessions_reached` and `sessions_dropped` are summed across the audience versions, and each row is labelled `N · <name>`. A "Viewed quiz" row comes first, with reached = overview views and dropped = `not_started`. "Still in quiz" starts at `starts` and goes down by each step's drops. The step with the most drops is highlighted. Pages without a number (the results page) come last.
- **Pages people left on:** every funnel page with `sessions_dropped > 0`, with its reached and dropped counts and average time on page.
- **Branch logic check:** `paths?cohort=all` edges where `cause == "sequential"` and both pages are *different* audience versions of the *same* step N. These are sessions that fell through to the next version without a jump rule catching them. The table should be empty.
- **Breakdown:** the four `breakdown?by=` calls (channel, device, new_returning, country) flattened into one set of rows.
- **Answers:** questions are grouped by step N. A step with a single version gets one "All" series. A step with several versions gets an "All" series that sums each option's chose, completed and opted_in by label (answered is summed too), plus one series per audience version with its own wording. In the "All" wording, "your kid/teen/child/family" becomes `[demo]`, and so does a "you" that ends the sentence ("affect you?"). The "you" in "How would you describe" is left alone. Each option keeps the same color in every audience view.

Metrics: start rate = starts ÷ unique_viewers_approx · starts ÷ views · completion rate = completions ÷ starts · median time = median_time_to_complete_ms ÷ 1000. Daily start rate is left empty (a gap in the chart) on days with 0 viewers.

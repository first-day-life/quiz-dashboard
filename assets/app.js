/* v2 Branch Logic Quiz dashboard. Reads ./data/*.json committed by the GitHub Action. No keys here. */
(() => {
  "use strict";
  const FILES = ["meta", "overview", "funnel", "branch_check", "breakdown", "answers"];
  const $ = (id) => document.getElementById(id);
  const state = { data: null, audience: "All", dim: "device", charts: [] };

  // ---------- formatting
  const nf = new Intl.NumberFormat("en-US");
  const num = (n) => (n == null ? "—" : nf.format(Math.round(n)));
  const pct = (r, d = 1) => (r == null || !isFinite(r) ? "—" : (r * 100).toFixed(d) + "%");
  const secs = (s) => (s == null ? "—" : s < 90 ? `${Math.round(s)}s` : `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmtDay = (iso) => new Date(iso + "T12:00:00").toLocaleDateString("en-US", { month: "short", day: "numeric" });
  const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
  const slotColor = (slot) => (slot < 8 ? css(`--series-${slot + 1}`) : css("--series-other"));

  function table(cols, rows) {
    const head = cols.map((c) => `<th class="${c.n ? "n" : ""}" scope="col">${esc(c.h)}</th>`).join("");
    const body = rows.map((r) => "<tr>" + cols.map((c) => `<td class="${c.n ? "n" : ""}">${c.f ? c.f(r) : esc(r[c.k])}</td>`).join("") + "</tr>").join("");
    return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
  }

  // ---------- loading (data is AES-256-GCM encrypted; key comes from the URL fragment #k=..., never sent to the server)
  const hashParams = () => new URLSearchParams(location.hash.replace(/^#/, ""));
  const fromB64u = (s) => Uint8Array.from(atob(s.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((s.length + 3) % 4)), (c) => c.charCodeAt(0));
  class LockedError extends Error {}

  async function load() {
    const k = hashParams().get("k");
    if (!k) throw new LockedError("no key");
    const bust = "?t=" + Date.now();
    const r = await fetch(`data/bundle.enc${bust}`, { cache: "no-store" });
    if (!r.ok) throw new Error(`data/bundle.enc: HTTP ${r.status}`);
    const env = await r.json();
    let plain;
    try {
      const raw = fromB64u(k);
      if (raw.length !== 32) throw new Error("bad key length");
      const key = await crypto.subtle.importKey("raw", raw, "AES-GCM", false, ["decrypt"]);
      plain = await crypto.subtle.decrypt({ name: "AES-GCM", iv: fromB64u(env.iv) }, key, fromB64u(env.ct));
    } catch (e) {
      throw new LockedError("wrong key");
    }
    const out = JSON.parse(new TextDecoder().decode(plain));
    for (const f of FILES) if (!out[f]) throw new Error(`bundle is missing ${f}`);
    return out;
  }

  function showLocked(reason) {
    $("content").hidden = true;
    $("range").textContent = "Private";
    $("lock").hidden = false;
    $("lock-msg").textContent = reason === "wrong key"
      ? "That key didn't unlock the data. Check you copied the whole link."
      : "This dashboard is private. Open it with the link you were given, or paste the key below.";
    fetch("data/meta.json?t=" + Date.now(), { cache: "no-store" }).then((r) => r.json())
      .then((m) => {
        if (!m.pulled_at) return;
        $("lock-pulled").textContent = `Data last pulled ${ago(m.pulled_at)}.`;
        $("pulled").textContent = new Date(m.pulled_at).toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZoneName: "short" });
      })
      .catch(() => {});
  }

  async function refresh() {
    const btn = $("refresh");
    const prev = state.data && state.data.meta.pulled_at;
    btn.disabled = true;
    setStatus("Loading latest data…");
    try {
      state.data = await load();
      $("lock").hidden = true;
      $("content").hidden = false;
      render();
      const now = state.data.meta.pulled_at;
      setStatus(prev && prev === now
        ? `Already showing the latest pull (${ago(now)}). The Action re-pulls hourly; use "Pull from Octane now" to force one.`
        : `Updated. Data pulled ${ago(now)}.`);
      return true;
    } catch (e) {
      if (e instanceof LockedError) { showLocked(e.message); setStatus(""); return false; }
      setStatus("Couldn't load data: " + e.message, true);
      return false;
    } finally {
      btn.disabled = false;
    }
  }

  function setStatus(msg, err) {
    const s = $("status");
    s.textContent = msg;
    s.classList.toggle("err", !!err);
  }
  function ago(iso) {
    const m = Math.round((Date.now() - new Date(iso)) / 60000);
    return m < 1 ? "just now" : m < 60 ? `${m} min ago` : `${Math.round(m / 60)} h ago`;
  }

  // ---------- render
  function render() {
    state.charts.forEach((c) => c.destroy());
    state.charts = [];
    const { meta, overview, funnel, branch_check, breakdown, answers } = state.data;
    const t = overview.totals;

    // header
    document.title = $("title").textContent = meta.quiz && meta.quiz.name ? `${meta.quiz.name.replace(/\s*\|\s*/g, " ")} Quiz` : "Quiz dashboard";
    $("range").textContent = `${fmtDay(meta.from)} – ${fmtDay(meta.to)}, ${new Date(meta.to + "T12:00:00").getFullYear()} (${meta.tz})`;
    const pulled = $("pulled");
    pulled.dateTime = meta.pulled_at;
    pulled.textContent = new Date(meta.pulled_at).toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZoneName: "short" });
    if (meta.repo) { $("pull-now").href = `https://github.com/${meta.repo}/actions/workflows/refresh.yml`; $("pull-now").hidden = false; }
    else $("pull-now").hidden = true;

    // lede + hero
    $("lede").textContent = `${pct(t.start_rate)} of unique viewers started the quiz, and ${pct(t.completion_rate)} of those who started finished it.`;
    $("hero-num").textContent = pct(t.start_rate);
    $("hero-sub").textContent = `${num(t.starts)} starts ÷ ${num(t.unique_viewers)} unique viewers`;

    renderDaily(overview.series);
    renderTiles(t);
    renderFunnel(funnel);
    renderLeft(funnel.pages_left);
    renderBranch(branch_check);
    renderAnswers(answers);
    renderBreakdown(breakdown.rows);
  }

  function renderDaily(series) {
    const labels = series.map((d) => fmtDay(d.day));
    const vals = series.map((d) => (d.start_rate == null ? null : d.start_rate * 100));
    const valid = series.filter((d) => d.start_rate != null);
    const canvas = $("daily");
    canvas.setAttribute("aria-label", valid.length
      ? `Daily start rate. ${valid.length} days with viewers; range ${pct(Math.min(...valid.map((d) => d.start_rate)))} to ${pct(Math.max(...valid.map((d) => d.start_rate)))}.`
      : "Daily start rate: no days with viewers in this range.");
    $("daily-table").innerHTML = table([
      { h: "Day", f: (r) => esc(fmtDay(r.day)) },
      { h: "Unique viewers", n: 1, f: (r) => num(r.unique_viewers) },
      { h: "Starts", n: 1, f: (r) => num(r.starts) },
      { h: "Start rate", n: 1, f: (r) => pct(r.start_rate) },
    ], series);
    if (!window.Chart) return;
    const accent = css("--accent"), grid = css("--grid"), ink = css("--text-secondary"), surf = css("--surface-1");
    state.charts.push(new Chart(canvas, {
      type: "line",
      data: { labels, datasets: [{ label: "Start rate", data: vals, borderColor: accent, backgroundColor: accent, borderWidth: 2,
        pointRadius: 4, pointHoverRadius: 6, pointBorderColor: surf, pointBorderWidth: 2, spanGaps: false, tension: 0 }] },
      options: {
        responsive: true, maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: { callbacks: {
            label: (c) => {
              const d = series[c.dataIndex];
              return d.start_rate == null ? "No viewers" : `${pct(d.start_rate)}  (${num(d.starts)} of ${num(d.unique_viewers)})`;
            } } },
        },
        scales: {
          x: { grid: { display: false }, ticks: { color: ink, maxRotation: 0, autoSkip: true } },
          y: { beginAtZero: true, grid: { color: grid }, border: { display: false },
               ticks: { color: ink, callback: (v) => v + "%" } },
        },
      },
    }));
  }

  function renderTiles(t) {
    const tiles = [
      ["Views", num(t.views)], ["Starts", num(t.starts)], ["Completions", num(t.completions)],
      ["Email opt-ins", num(t.email_opt_ins)], ["Starts ÷ views", pct(t.view_start_rate)],
      ["Median time to complete", secs(t.median_time_to_complete_s)],
    ];
    $("tiles").innerHTML = tiles.map(([k, v]) => `<div class="tile"><div class="v">${esc(v)}</div><div class="k">${esc(k)}</div></div>`).join("");
  }

  function renderFunnel(f) {
    const max = Math.max(...f.steps.map((s) => s.reached), 1);
    const rows = f.steps.map((s) => {
      const share = s.reached ? s.dropped / s.reached : 0;
      return `<div class="frow${s.biggest_drop ? " big" : ""}" role="listitem">
        <span class="lbl" title="${esc(s.label)}${s.versions > 1 ? ` (${s.versions} audience versions)` : ""}">${esc(s.label)}${s.biggest_drop ? '<span class="badge">Biggest drop</span>' : ""}</span>
        <span class="track" aria-hidden="true"><span class="fill" style="width:${(100 * s.reached / max).toFixed(1)}%;display:block"></span></span>
        <span class="nums">${num(s.reached)} reached · −${num(s.dropped)} (${pct(share, 0)})</span>
      </div>`;
    }).join("");
    $("funnel").innerHTML = `<div class="funnel" role="list" aria-label="Funnel steps">${rows}</div>
      <details class="table-view"><summary>Show as table</summary><div class="scroll">${table([
        { h: "Step", k: "label" }, { h: "Reached", n: 1, f: (r) => num(r.reached) },
        { h: "Dropped", n: 1, f: (r) => num(r.dropped) + (r.biggest_drop ? " ⚠" : "") },
        { h: "Drop %", n: 1, f: (r) => pct(r.reached ? r.dropped / r.reached : null) },
        { h: "Still in quiz", n: 1, f: (r) => num(r.in_quiz) },
      ], f.steps)}</div></details>`;
  }

  function renderLeft(rows) {
    $("left").innerHTML = rows.length ? table([
      { h: "Page", k: "page" }, { h: "Reached", n: 1, f: (r) => num(r.reached) },
      { h: "Left here", n: 1, f: (r) => num(r.dropped) }, { h: "Left %", n: 1, f: (r) => pct(r.reached ? r.dropped / r.reached : null) },
      { h: "Avg time on page", n: 1, f: (r) => secs(r.avg_s) },
    ], rows) : '<p class="muted">Nobody left mid-quiz in this range.</p>';
  }

  function renderBranch(b) {
    const el = $("branch");
    if (b.paths_error) { el.innerHTML = `<p class="warn">Octane refused the paths report: ${esc(b.paths_error)}</p>`; return; }
    el.innerHTML = b.rows.length
      ? `<p class="warn">⚠ ${b.rows.length} fall-through${b.rows.length > 1 ? "s" : ""} found. Check the jump rules on these pages.</p>` + table([
          { h: "From", k: "from_page" }, { h: "To", k: "to_page" }, { h: "Sessions", n: 1, f: (r) => num(r.sessions) }], b.rows)
      : '<p class="empty">✓ No fall-throughs. Every audience version jumped where it should.</p>';
  }

  function chips(el, items, current, onPick) {
    el.innerHTML = items.map(([v, l]) => `<button type="button" class="chip" role="radio" aria-checked="${v === current}" data-v="${esc(v)}">${esc(l)}</button>`).join("");
    el.onclick = (e) => { const b = e.target.closest(".chip"); if (b) onPick(b.dataset.v); };
    el.onkeydown = (e) => {
      if (!["ArrowRight", "ArrowLeft"].includes(e.key)) return;
      const bs = [...el.querySelectorAll(".chip")], i = bs.indexOf(document.activeElement);
      if (i < 0) return;
      const n = bs[(i + (e.key === "ArrowRight" ? 1 : bs.length - 1)) % bs.length];
      n.focus(); onPick(n.dataset.v);
    };
  }

  function renderAnswers(a) {
    if (!a.audiences.includes(state.audience)) state.audience = "All";
    chips($("aud-filter"), [["All", "All audiences"], ...a.audiences.map((x) => [x, x])], state.audience, (v) => {
      state.audience = v; renderAnswers(a);
      const b = $("aud-filter").querySelector(`[data-v="${CSS.escape(v)}"]`); b && b.focus();
    });
    // destroy only donut charts
    state.charts = state.charts.filter((c) => (c.config.type === "doughnut" ? (c.destroy(), false) : true));
    const box = $("answers");
    box.innerHTML = "";
    const surf = css("--surface-1");
    a.questions.forEach((q, qi) => {
      const s = q.series.find((x) => x.audience === state.audience) || q.series[0];
      // more than 8 answers (combined view of differently-worded versions): top 7 + "Other"; the table keeps every row
      const byChose = [...s.options].sort((x, y) => y.chose - x.chose);
      let opts = byChose.length > 8 ? byChose.slice(0, 7) : s.options;
      if (byChose.length > 8) {
        const rest = byChose.slice(7);
        opts = [...opts, { label: `Other (${rest.length} answers)`, chose: rest.reduce((n, o) => n + o.chose, 0), slot: 99 }];
      }
      const total = s.options.reduce((n, o) => n + o.chose, 0);
      const card = document.createElement("article");
      card.className = "q";
      const shownAud = q.series.length === 1 ? "Everyone sees this" : s.audience === "All" ? "All audiences combined" : s.audience;
      card.innerHTML = `<h3>${esc(q.label)}</h3><p class="qt">${esc(s.question)}</p>
        <div class="donut-row"><div class="donut-box"><canvas id="d${qi}" role="img"></canvas></div>
        <ul class="legend">${opts.map((o) => `<li><span class="sw" style="background:${slotColor(o.slot)}"></span><span>${esc(o.label)}</span><span class="pct">${pct(total ? o.chose / total : null, 0)}</span></li>`).join("")}</ul></div>
        <p class="aud">${esc(shownAud)} · ${num(s.answered)} answered${q.series.length > 1 && state.audience !== "All" && s.audience === "All" ? " (no version for this audience)" : ""}</p>
        <details class="table-view"><summary>Show as table</summary><div class="scroll">${table([
          { h: "Answer", k: "label" }, { h: "Chose", n: 1, f: (r) => num(r.chose) },
          { h: "Share", n: 1, f: (r) => pct(total ? r.chose / total : null) },
          { h: "Completed", n: 1, f: (r) => num(r.completed) }, { h: "Opted in", n: 1, f: (r) => num(r.opted_in) },
        ], s.options)}</div></details>`;
      box.appendChild(card);
      const cv = card.querySelector("canvas");
      cv.setAttribute("aria-label", `${q.label}: ` + opts.map((o) => `${o.label} ${pct(total ? o.chose / total : null, 0)}`).join(", "));
      if (!window.Chart || !total) return;
      state.charts.push(new Chart(cv, {
        type: "doughnut",
        data: { labels: opts.map((o) => o.label), datasets: [{ data: opts.map((o) => o.chose), backgroundColor: opts.map((o) => slotColor(o.slot)),
          borderColor: surf, borderWidth: 2, hoverOffset: 4 }] },
        options: { responsive: true, maintainAspectRatio: false, cutout: "62%", animation: { duration: 200 },
          plugins: { legend: { display: false }, tooltip: { callbacks: {
            label: (c) => `${c.label}: ${num(c.raw)} (${pct(c.raw / total)})` } } } },
      }));
    });
  }

  const DIMS = [["device", "Device"], ["channel", "Channel"], ["new_returning", "New vs returning"], ["country", "Country"]];
  function renderBreakdown(rows) {
    chips($("bd-filter"), DIMS, state.dim, (v) => {
      state.dim = v; renderBreakdown(rows);
      const b = $("bd-filter").querySelector(`[data-v="${CSS.escape(v)}"]`); b && b.focus();
    });
    const r = rows.filter((x) => x.dimension === state.dim).sort((a, b) => b.views - a.views);
    $("breakdown").innerHTML = r.length ? table([
      { h: DIMS.find((d) => d[0] === state.dim)[1], f: (x) => esc(String(x.value ?? "(unknown)").replace(/_/g, " ")) },
      { h: "Views", n: 1, f: (x) => num(x.views) }, { h: "Unique viewers", n: 1, f: (x) => num(x.unique_viewers) },
      { h: "Starts", n: 1, f: (x) => num(x.starts) }, { h: "Start rate", n: 1, f: (x) => pct(x.unique_viewers ? x.starts / x.unique_viewers : null) },
      { h: "Completions", n: 1, f: (x) => num(x.completions) }, { h: "Completion rate", n: 1, f: (x) => pct(x.starts ? x.completions / x.starts : null) },
    ], r) : '<p class="muted">No rows.</p>';
  }

  // ---------- boot
  document.addEventListener("DOMContentLoaded", () => {
    $("refresh").addEventListener("click", refresh);
    if (hashParams().has("all")) $("all-metrics").open = true;   // deep link: #k=...&all opens "All metrics"
    $("lock-form").addEventListener("submit", (e) => {
      e.preventDefault();
      let v = $("lock-key").value.trim();
      const m = v.match(/[#&]k=([^&\s]+)/);                     // accept a pasted full link or a bare key
      if (m) v = m[1];
      if (!v) return;
      const p = hashParams(); p.set("k", v);
      history.replaceState(null, "", "#" + p.toString());
      refresh().then((ok) => ok && setStatus(""));
    });
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => state.data && render());
    refresh().then((ok) => ok && setStatus(""));
  });
})();

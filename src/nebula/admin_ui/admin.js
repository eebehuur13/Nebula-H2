/* Nebula admin page. Plain JavaScript, no build step.
   Data comes from /admin/api/state; every value from the server is inserted as text, never as HTML. */
(() => {
  "use strict";

  // ------------------------------------------------------------------ small helpers
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));
  const SVGNS = "http://www.w3.org/2000/svg";

  function h(tag, props, ...kids) {
    const el = document.createElement(tag);
    if (props) {
      for (const [k, v] of Object.entries(props)) {
        if (v == null || v === false) continue;
        if (k === "class") el.className = v;
        else if (k === "text") el.textContent = v;
        else if (k === "style") Object.assign(el.style, v);
        else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
        else el.setAttribute(k, v === true ? "" : v);
      }
    }
    for (const kid of kids.flat()) {
      if (kid == null || kid === false) continue;
      el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    }
    return el;
  }
  function icon(id, cls) {
    const s = document.createElementNS(SVGNS, "svg");
    if (cls) s.setAttribute("class", cls);
    s.setAttribute("aria-hidden", "true");
    const u = document.createElementNS(SVGNS, "use");
    u.setAttribute("href", "#i-" + id);
    s.append(u);
    return s;
  }
  function svg(tag, attrs) {
    const el = document.createElementNS(SVGNS, tag);
    for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, v);
    return el;
  }

  const TZ = "Asia/Kolkata";
  const fTime = new Intl.DateTimeFormat("en-GB", { timeZone: TZ, hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
  const fHM = new Intl.DateTimeFormat("en-GB", { timeZone: TZ, hour: "2-digit", minute: "2-digit", hour12: false });
  const fDay = new Intl.DateTimeFormat("en-GB", { timeZone: TZ, weekday: "short", day: "numeric", month: "short" });
  const fDate = new Intl.DateTimeFormat("en-GB", { timeZone: TZ, day: "numeric", month: "short" });
  const fWeekday = new Intl.DateTimeFormat("en-GB", { timeZone: TZ, weekday: "short" });
  const fDayKey = new Intl.DateTimeFormat("en-CA", { timeZone: TZ, year: "numeric", month: "2-digit", day: "2-digit" });
  const nf = new Intl.NumberFormat("en-IN");

  const now = () => Date.now() + S.offset;
  const t = (iso) => (iso ? Date.parse(iso) : NaN);
  const isToday = (ms) => fDayKey.format(ms) === fDayKey.format(now());
  function when(iso) {
    const ms = t(iso);
    if (Number.isNaN(ms)) return "—";
    return isToday(ms) ? fHM.format(ms) : `${fDate.format(ms)}, ${fHM.format(ms)}`;
  }
  function ago(iso) {
    const ms = t(iso);
    if (Number.isNaN(ms)) return "—";
    let d = Math.round((now() - ms) / 1000);
    const fut = d < 0;
    d = Math.abs(d);
    let s;
    if (d < 5) return fut ? "in a moment" : "just now";
    if (d < 60) s = `${d} s`;
    else if (d < 3600) s = `${Math.floor(d / 60)} min`;
    else if (d < 86400) s = `${Math.floor(d / 3600)} h`;
    else s = `${Math.floor(d / 86400)} day${d >= 172800 ? "s" : ""}`;
    return fut ? `in ${s}` : `${s} ago`;
  }
  function clockLeft(sec) {
    sec = Math.max(0, Math.round(sec));
    if (sec >= 3600) return `${Math.floor(sec / 3600)} h ${Math.floor((sec % 3600) / 60)} min`;
    return `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, "0")}`;
  }
  function dur(ms) {
    if (ms == null) return "—";
    if (ms < 1000) return `${Math.round(ms)} ms`;
    if (ms < 60000) return `${(ms / 1000).toFixed(ms < 10000 ? 1 : 0)} s`;
    const m = Math.floor(ms / 60000);
    return `${m} min ${Math.round((ms % 60000) / 1000)} s`;
  }
  const num = (n) => (n == null ? "—" : nf.format(Math.round(n)));
  function compact(n) {
    if (n == null) return "—";
    if (Math.abs(n) < 10000) return nf.format(n);
    if (Math.abs(n) < 1e6) return `${(n / 1000).toFixed(n < 1e5 ? 1 : 0)}K`;
    return `${(n / 1e6).toFixed(1)}M`;
  }
  const plural = (n, one, many) => `${num(n)} ${n === 1 ? one : (many || one + "s")}`;
  function niceMax(v) {
    if (!v || v <= 0) return 1;
    const p = Math.pow(10, Math.floor(Math.log10(v)));
    for (const m of [1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10]) if (m * p >= v) return m * p;
    return 10 * p;
  }
  function initials(p) {
    const base = (p && (p.name || p.email)) || "?";
    const parts = base.replace(/@.*/, "").split(/[\s._-]+/).filter(Boolean);
    return ((parts[0] || "?")[0] + (parts[1] ? parts[1][0] : "")).toUpperCase();
  }
  const AV = [["#e8f1fc", "#1c5cab"], ["#fdeee6", "#b84a1d"], ["#e3f6ee", "#0f7a54"], ["#fdf3dc", "#8a5a00"],
              ["#fcedf3", "#b0306b"], ["#e3f2e3", "#006300"], ["#ecebf7", "#4a3aa7"], ["#fbe9e9", "#b42323"]];
  function avatar(p, size) {
    if (!p) return h("span", { class: `avatar sys ${size || ""}`, text: "N", title: "Nebula" });
    let x = 0;
    for (const c of p.email || "") x = (x * 31 + c.charCodeAt(0)) >>> 0;
    const [bg, fg] = AV[x % AV.length];
    return h("span", { class: `avatar ${size || ""}`, style: { background: bg, color: fg }, text: initials(p) });
  }
  function who(p, size) {
    return h("div", { class: "who" }, avatar(p, size),
      h("div", { class: "meta" },
        h("div", { class: "cell-main", text: p ? (p.name || p.email.split("@")[0]) : "Nebula" }),
        h("div", { class: "cell-sub", text: p ? p.email : "Background work" })));
  }
  function tag(text, kind, opts = {}) {
    return h("span", { class: `tag ${kind || ""}`, title: opts.title },
      opts.spin ? h("span", { class: "spin" }) : (opts.dot === false ? null : h("span", { class: "dot" })), text);
  }
  function empty(iconId, title, text) {
    return h("div", { class: "empty" }, icon(iconId), h("b", { text: title }), text ? h("span", { text }) : null);
  }
  function card(title, sub, body, actions) {
    return h("section", { class: "card" },
      h("div", { class: "card-head" },
        h("div", null, h("h2", { text: title }), sub ? (sub instanceof Node ? sub : h("p", { text: sub })) : null),
        actions ? h("div", { class: "actions" }, actions) : null),
      body);
  }
  function tween(el, to, format) {
    if (to == null || Number.isNaN(to)) { el.textContent = "—"; el._v = null; return; }
    const from = el._v == null ? 0 : el._v;
    el._v = to;
    if (from === to || REDUCED) { el.textContent = format(to); return; }
    const start = performance.now();
    const step = (ts) => {
      const k = Math.min(1, (ts - start) / 650);
      const e = 1 - Math.pow(1 - k, 3);
      el.textContent = format(from + (to - from) * e);
      if (k < 1 && el._v === to) requestAnimationFrame(step);
      else if (el._v === to) el.textContent = format(to);
    };
    requestAnimationFrame(step);
  }
  const REDUCED = matchMedia("(prefers-reduced-motion: reduce)").matches;

  // ------------------------------------------------------------------ app state
  const TABS = {
    overview: { title: "Overview", sub: "Who's using Nebula, how often, and what it's doing right now.", range: true, every: 5000 },
    people: { title: "People", sub: "Everyone who has signed in, connected Claude, or shared something from Drive with Nebula.", range: true, every: 6000 },
    activity: { title: "Activity", sub: "Every request, sign-in, share and file read, as it happens.", range: true, every: 3000 },
    indexing: { title: "Indexing", sub: "Every file Nebula holds, and where each one is in being read and made searchable.", range: false, every: 3000 },
    sources: { title: "Sources", sub: "What's shared with Nebula, and how often Nebula checks it.", range: false, every: 4000 },
    jobs: { title: "Jobs", sub: "Background work: Drive checks, file reads and search updates.", range: true, every: 3000 },
  };
  const RANGE_WORDS = { "1h": "the last hour", today: "today", "24h": "the last 24 hours", "7d": "the last 7 days", all: "all time" };
  const PREV_WORDS = { "1h": "previous hour", today: "yesterday", "24h": "previous day", "7d": "previous week" };
  const store = {
    get(k, d) { try { return localStorage.getItem("nebula-admin:" + k) || d; } catch { return d; } },
    set(k, v) { try { localStorage.setItem("nebula-admin:" + k, v); } catch { /* private mode */ } },
  };
  const S = {
    tab: (location.hash || "").slice(1) in TABS ? location.hash.slice(1) : "overview",
    range: store.get("range", "24h"),
    offset: 0, timer: null, busy: false, lastOk: 0, failing: false, me: null,
    views: {},
    act: { rows: [], keys: new Set(), after: null, person: "", category: "", status: "", q: "", primed: false, sig: "" },
    people: { sort: "last_request", dir: -1, q: "", only: "all" },
    idx: { state: "", q: "" },
    jobs: { status: "" },
    show: {},
  };

  // ------------------------------------------------------------------ data
  async function api(view, extra) {
    const p = new URLSearchParams({ view, range: S.range, ...(extra || {}) });
    const res = await fetch(`/admin/api/state?${p}`, { credentials: "same-origin", headers: { Accept: "application/json" } });
    if (res.status === 403) { location.href = "/login?next=/admin"; throw new Error("signed out"); }
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.json();
  }
  async function act(action, body, btn) {
    const label = btn ? btn.innerHTML : "";
    if (btn) { btn.classList.add("busy"); btn.replaceChildren(h("span", { class: "spin" }), btn.textContent.trim() || "Working"); }
    try {
      const res = await fetch("/admin/api/action", {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ action, ...(body || {}) }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
      toast(data.message || "Done", null, false);
      refresh(true);
      return data;
    } catch (e) {
      toast("That didn't work", e.message, true);
      return null;
    } finally {
      if (btn) { btn.classList.remove("busy"); btn.innerHTML = label; }
    }
  }
  function toast(title, text, bad) {
    const el = h("div", { class: `toast ${bad ? "bad" : ""}` },
      h("span", { class: "ic" }, icon(bad ? "x" : "check")),
      h("div", null, h("b", { text: title }), text ? h("span", { text }) : null));
    $("#toasts").append(el);
    setTimeout(() => { el.classList.add("out"); setTimeout(() => el.remove(), 320); }, bad ? 6000 : 3200);
  }

  async function refresh(now_) {
    clearTimeout(S.timer);
    if (document.hidden) { setLive(); return; }
    if (S.busy && !now_) { S.timer = setTimeout(refresh, 800); return; }
    S.busy = true;
    const tab = S.tab;
    try {
      let extra = {};
      if (tab === "activity") {
        const sig = `${S.range}|${S.act.person}`;
        if (sig !== S.act.sig) { S.act.rows = []; S.act.keys = new Set(); S.act.after = null; S.act.primed = false; S.act.sig = sig; }
        extra = { person: S.act.person, ...(S.act.after ? { after: S.act.after } : {}) };
      } else if (tab === "jobs" && S.jobs.status) extra = { status: S.jobs.status };
      const data = await api(tab, extra);
      if (tab !== S.tab) return;
      S.offset = Date.parse(data.now) - Date.now();
      S.lastOk = Date.now();
      S.failing = false;
      S.me = data.me;
      pulse(data.pulse);
      RENDER[tab](data);
      $("#foot-me").textContent = `Signed in as ${data.me.email}`;
      $("#foot-updated").textContent = `Updated ${fTime.format(now())} IST`;
    } catch (e) {
      if (e.message !== "signed out") S.failing = true;
    } finally {
      S.busy = false;
      setLive();
      S.timer = setTimeout(refresh, TABS[S.tab].every);
    }
  }

  // ------------------------------------------------------------------ chrome: tabs, range, clock, pills
  function placeInk(container, btn, ink) {
    if (!btn) return;
    const c = container.getBoundingClientRect();
    const b = btn.getBoundingClientRect();
    const pad = ink.classList.contains("tab-ink") ? 10 : 0;
    ink.style.width = `${b.width - pad * 2}px`;
    ink.style.transform = `translateX(${b.left - c.left + container.scrollLeft + pad}px)`;
  }
  function setTab(tab, push) {
    if (!(tab in TABS)) tab = "overview";
    S.tab = tab;
    if (push) history.replaceState(null, "", `#${tab}`);
    $$(".tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === tab)));
    placeInk($(".tabs"), $(`.tabs button[data-tab="${tab}"]`), $(".tab-ink"));
    $$(".view").forEach((v) => v.classList.toggle("on", v.dataset.view === tab));
    $("#page-title").textContent = TABS[tab].title;
    $("#page-sub").textContent = TABS[tab].sub;
    $("#range").classList.toggle("off", !TABS[tab].range);
    document.title = `${TABS[tab].title} · Nebula Admin`;
    refresh(true);
  }
  function setRange(r) {
    S.range = r;
    store.set("range", r);
    $$("#range button").forEach((b) => b.setAttribute("aria-checked", String(b.dataset.range === r)));
    placeInk($("#range"), $(`#range button[data-range="${r}"]`), $(".range-ink"));
    refresh(true);
  }
  function setLive() {
    const pill = $("#live-pill");
    const label = pill.querySelector("span");
    if (document.hidden) { pill.className = "pill warn"; label.textContent = "Paused"; return; }
    if (S.failing) { pill.className = "pill bad"; label.textContent = "Reconnecting"; return; }
    pill.className = S.lastOk ? "pill ok" : "pill"; label.textContent = "Live";
  }
  function pulse(p) {
    const pill = $("#worker-pill");
    const label = pill.querySelector("span");
    const age = p.worker_seen ? (now() - t(p.worker_seen)) / 1000 : Infinity;
    pill.dataset.seen = p.worker_seen || "";
    const job = p.worker_job;
    if (age < 45 && job) { pill.className = "pill ok"; label.textContent = `Worker busy · ${job.label.toLowerCase()}`; }
    else if (age < 45) { pill.className = "pill ok"; label.textContent = "Worker idle"; }
    else if (age < 180) { pill.className = "pill warn"; label.textContent = "Worker slow to respond"; }
    else { pill.className = "pill bad"; label.textContent = p.worker_seen ? "Worker stopped" : "Worker not seen"; }
    pill.title = p.worker_seen
      ? `Last heartbeat ${ago(p.worker_seen)}.${job ? ` Working on: ${job.label}${job.since ? `, for ${ago(job.since).replace(" ago", "")}` : ""}.` : ""} ${p.jobs_running} running, ${p.jobs_queued} waiting.`
      : "The worker hasn't checked in yet.";
    const jb = $("#badge-jobs");
    const busy = p.jobs_running + p.jobs_queued;
    jb.hidden = !busy; jb.textContent = compact(busy); jb.classList.toggle("hot", p.jobs_running > 0);
    const ib = $("#badge-indexing");
    ib.hidden = !p.pieces_waiting; ib.textContent = compact(p.pieces_waiting); ib.classList.toggle("hot", p.pieces_waiting > 0);
    ib.title = `${num(p.pieces_waiting)} pieces of text waiting to be made searchable`;
  }
  function tick() {
    const n = now();
    $("#clock-time").textContent = fTime.format(n);
    $("#clock-date").textContent = `${fDay.format(n)} · IST`;
    for (const el of $$("[data-ago]")) el.textContent = ago(el.dataset.ago);
    for (const el of $$("[data-ring]")) updateRing(el);
    if (S.lastOk && Date.now() - S.lastOk > 20000 && !document.hidden) { S.failing = true; setLive(); }
  }

  // ------------------------------------------------------------------ tooltip
  const tip = $("#tooltip");
  function showTip(x, y, title, rows) {
    tip.replaceChildren(h("div", { class: "tt-title", text: title }),
      ...rows.map((r) => h("div", { class: "tt-row" },
        h("span", null, r.color ? h("i", { style: { background: r.color } }) : null, r.label), h("b", { text: r.value }))));
    tip.hidden = false;
    const w = tip.offsetWidth, hgt = tip.offsetHeight;
    let left = x + 14, top = y - hgt - 12;
    if (left + w > innerWidth - 8) left = x - w - 14;
    if (top < 8) top = y + 16;
    tip.style.left = `${left}px`; tip.style.top = `${top}px`;
  }
  const hideTip = () => { tip.hidden = true; };

  // ------------------------------------------------------------------ charts
  function bucketLabel(iso, step) {
    const ms = t(iso);
    if (step >= 86400) return fDate.format(ms);
    if (step >= 21600) return `${fWeekday.format(ms)} ${fHM.format(ms)}`;
    return fHM.format(ms);
  }
  function bucketTitle(iso, step) {
    const a = t(iso), b = a + step * 1000;
    if (step >= 86400) return fDay.format(a);
    return `${isToday(a) ? "" : fDate.format(a) + ", "}${fHM.format(a)}–${fHM.format(b)}`;
  }
  function axes(chart, max, labels) {
    const y = $(".yaxis", chart), plot = $(".plot", chart), x = $(".xaxis", chart);
    y.replaceChildren(); $$(".gridline", plot).forEach((g) => g.remove());
    for (const f of [0, 0.5, 1]) {
      const v = max * f;
      y.append(h("span", { style: { top: `${(1 - f) * 100}%` }, text: v >= 1000 ? compact(v) : (v % 1 ? v.toFixed(1) : num(v)) }));
      if (f > 0) plot.prepend(h("div", { class: "gridline", style: { top: `${(1 - f) * 100}%` } }));
    }
    x.replaceChildren(...labels.map((l) => h("span", { style: { left: `${l.at}%` }, text: l.text })));
  }
  function xLabels(series, step, maxLabels) {
    const n = series.length;
    const every = Math.max(1, Math.ceil(n / maxLabels));
    const out = [];
    for (let i = 0; i < n; i += every) out.push({ at: ((i + 0.5) / n) * 100, text: bucketLabel(series[i].t, step) });
    return out;
  }
  function chartShell() {
    return h("div", { class: "chart" }, h("div", { class: "yaxis" }), h("div", { class: "plot" }), h("div"), h("div", { class: "xaxis" }));
  }
  const CAT = {
    search: ["Searches", "search"], open: ["Opens", "open"], numbers: ["Number questions", "numbers"],
    admin: ["Admin", "admin"], sources: ["Sources", "sources"], "sign-in": ["Sign-ins", "signin"], indexing: ["Indexing", "indexing"],
    other: ["Other", "other"],
  };
  function columns(el, series, step) {
    let chart = $(".chart", el);
    const total = series.reduce((a, b) => a + b.total, 0);
    if (!total) { el.replaceChildren(empty("chart", "No requests yet", `Nothing in ${RANGE_WORDS[S.range]}.`)); return; }
    if (!chart || chart._n !== series.length) {
      chart = chartShell(); chart._n = series.length;
      const cols = h("div", { class: "cols" });
      series.forEach(() => cols.append(h("div", { class: "col", tabindex: "0" }, h("i", null, h("b")))));
      $(".plot", chart).append(cols);
      el.replaceChildren(chart);
      cols.addEventListener("pointerleave", hideTip);
    }
    const max = niceMax(Math.max(...series.map((s) => s.total)));
    axes(chart, max, xLabels(series, step, 6));
    $$(".col", chart).forEach((c, i) => {
      const s = series[i];
      const bar = c.firstChild;
      requestAnimationFrame(() => { bar.style.height = `${(s.total / max) * 100}%`; });
      bar.firstChild.style.height = s.total ? `${(s.errors / s.total) * 100}%` : "0";
      const rows = [{ label: "Requests", value: num(s.total), color: "var(--accent)" },
        ...Object.entries(s.by).sort((a, b) => b[1] - a[1]).map(([k, v]) => ({ label: (CAT[k] || [k])[0], value: num(v) }))];
      if (s.errors) rows.push({ label: "Failed", value: num(s.errors), color: "var(--bad)" });
      const show = (ev) => { const r = c.getBoundingClientRect(); showTip(ev.clientX ?? r.left + r.width / 2, ev.clientY ?? r.top, bucketTitle(s.t, step), rows); };
      c.onpointermove = show; c.onfocus = show; c.onblur = hideTip;
    });
  }
  function lines(el, series, step) {
    const pts = series.map((s) => [s.p50, s.p90]);
    const any = pts.some(([a]) => a != null);
    if (!any) { el.replaceChildren(empty("clock", "No searches yet", `Search times show up here once people search, in ${RANGE_WORDS[S.range]}.`)); return; }
    const max = niceMax(Math.max(...pts.map(([a, b]) => Math.max(a || 0, b || 0))));
    const first = !$(".chart", el);
    const chart = first ? chartShell() : $(".chart", el);
    if (first) el.replaceChildren(chart);
    const plot = $(".plot", chart);
    axes(chart, max / 1000, xLabels(series, step, 6));
    $$(".yaxis span", chart).forEach((s) => { s.textContent = `${s.textContent} s`; });
    const n = series.length;
    const X = (i) => ((i + 0.5) / n) * 1000;
    const Y = (v) => 100 - (v / max) * 100;
    const path = (k) => {
      let d = "", pen = false;
      pts.forEach((p, i) => {
        if (p[k] == null) { pen = false; return; }
        d += `${pen ? "L" : "M"}${X(i).toFixed(1)},${Y(p[k]).toFixed(2)}`;
        pen = true;
      });
      return d;
    };
    let s = $("svg.lines", plot);
    if (!s) {
      s = svg("svg", { class: "lines", viewBox: "0 0 1000 100", preserveAspectRatio: "none" });
      const a = svg("path"), b = svg("path");
      a.style.stroke = "var(--series-2)"; b.style.stroke = "var(--series-1)";
      s.append(a, b);
      plot.append(s, h("div", { class: "crosshair" }), h("div", { class: "dotmark", style: { background: "var(--series-2)" } }),
        h("div", { class: "dotmark", style: { background: "var(--series-1)" } }), h("div", { class: "hover-zone" }));
    }
    const [p90, p50] = $$("path", s);
    p50.setAttribute("d", path(0)); p90.setAttribute("d", path(1));
    if (first && !REDUCED) for (const p of [p50, p90]) {
      const len = Math.ceil(p.getTotalLength ? p.getTotalLength() : 3000) + 10;
      p.style.setProperty("--len", len); p.classList.add("drawn");
    }
    const zone = $(".hover-zone", plot), cross = $(".crosshair", plot);
    const [d90, d50] = $$(".dotmark", plot);
    zone.onpointermove = (ev) => {
      const r = zone.getBoundingClientRect();
      const i = Math.max(0, Math.min(n - 1, Math.floor(((ev.clientX - r.left) / r.width) * n)));
      const x = ((i + 0.5) / n) * 100;
      cross.style.left = `${x}%`; cross.style.opacity = 1;
      for (const [dot, v] of [[d50, pts[i][0]], [d90, pts[i][1]]]) {
        dot.style.opacity = v == null ? 0 : 1;
        if (v != null) { dot.style.left = `${x}%`; dot.style.top = `${Y(v)}%`; }
      }
      const b = series[i];
      showTip(ev.clientX, ev.clientY, bucketTitle(b.t, step), [
        { label: "Median", value: pts[i][0] == null ? "—" : dur(pts[i][0]), color: "var(--series-1)" },
        { label: "Slowest 10%", value: pts[i][1] == null ? "—" : dur(pts[i][1]), color: "var(--series-2)" },
        { label: "Searches", value: num(b.by.search || 0) }]);
    };
    zone.onpointerleave = () => { cross.style.opacity = 0; d50.style.opacity = 0; d90.style.opacity = 0; hideTip(); };
  }
  function seriesTable(series, step) {
    return h("div", { class: "tablewrap" }, h("table", { class: "t" },
      h("thead", null, h("tr", null, h("th", { text: "Time" }), h("th", { class: "n", text: "Requests" }), h("th", { class: "n", text: "Searches" }),
        h("th", { class: "n", text: "Median search" }), h("th", { class: "n", text: "Failed" }))),
      h("tbody", null, series.filter((s) => s.total).reverse().map((s) => h("tr", null,
        h("td", { class: "nowrap", text: bucketTitle(s.t, step) }), h("td", { class: "n", text: num(s.total) }),
        h("td", { class: "n", text: num(s.by.search || 0) }), h("td", { class: "n", text: s.p50 == null ? "—" : dur(s.p50) }),
        h("td", { class: "n", text: num(s.errors) }))))));
  }
  function viewToggle(id, onChange) {
    const mode = S.show[id] || "chart";
    const wrap = h("div", { class: "toggle-view" });
    const b = (m, ic, label) => h("button", { "aria-pressed": String(mode === m), title: label, "aria-label": label,
      onclick: (e) => { S.show[id] = m; $$("button", wrap).forEach((x) => x.setAttribute("aria-pressed", String(x === e.currentTarget))); onChange(); } }, icon(ic));
    wrap.append(b("chart", "chart", "Show as a chart"), b("table", "table", "Show as a table"));
    return wrap;
  }
  function drawCharts() {
    if (!S.ov) return;
    const { series, step } = S.ov;
    for (const [id, el, draw] of [["req", $("#ov-req"), columns], ["lat", $("#ov-lat"), lines]]) {
      if (!el) continue;
      if ((S.show[id] || "chart") === "table") {
        const total = series.reduce((a, b) => a + b.total, 0);
        el.replaceChildren(total ? seriesTable(series, step) : empty("table", "Nothing to show", `No requests in ${RANGE_WORDS[S.range]}.`));
      } else {
        if (el.querySelector("table")) el.replaceChildren();
        draw(el, series, step);
      }
    }
  }
  function sparkline(values) {
    const s = svg("svg", { class: "spark", viewBox: "0 0 100 30", preserveAspectRatio: "none" });
    if (!values.length || !values.some(Boolean)) return s;
    const max = Math.max(...values, 1), n = values.length;
    const pts = values.map((v, i) => [n === 1 ? 50 : (i / (n - 1)) * 100, 27 - (v / max) * 24]);
    const line = pts.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join("");
    s.append(svg("path", { class: "area", d: `${line}L100,30L0,30Z` }), svg("path", { class: "line", d: line, "vector-effect": "non-scaling-stroke" }));
    return s;
  }

  // ------------------------------------------------------------------ keyed lists
  function syncRows(container, items, keyOf, build, opts = {}) {
    const prev = container._rows || new Map();
    const next = new Map();
    const primed = container._primed;
    let anchor = null;
    items.forEach((item) => {
      const key = keyOf(item);
      const sig = JSON.stringify(item);
      let row = prev.get(key);
      if (!row || row._sig !== sig) {
        const fresh = build(item);
        if (row) row.replaceWith(fresh);
        else if (opts.fresh ? opts.fresh.has(key) : (primed && opts.animate !== false)) fresh.classList.add("new");
        row = fresh;
        row._sig = sig;
      }
      next.set(key, row);
      const want = anchor ? anchor.nextSibling : container.firstChild;
      if (row !== want) container.insertBefore(row, want);
      anchor = row;
    });
    for (const [k, row] of prev) if (!next.has(k)) row.remove();
    container._rows = next;
    container._primed = true;
  }

  // ------------------------------------------------------------------ overview
  const TOOL_NAME = { search: "Search", open: "Open", documents: "Document lists", query: "Number questions",
    sources: "Sources", job: "Job checks" };
  function renderOverview(data) {
    const root = $("#view-overview");
    const o = data.overview, k = o.tiles;
    if (!S.views.overview) {
      const tile = (id, label) => h("section", { class: "card tile", id: `tile-${id}` },
        h("div", { class: "tile-label", text: label }), h("div", { class: "tile-value" }), h("div", { class: "tile-sub" }), h("div", { class: "tile-foot" }));
      root.replaceChildren(
        h("div", { class: "grid g-6" }, tile("people", "People signed in"), tile("active", "Active people"), tile("requests", "Requests"),
          tile("searches", "Searches"), tile("files", "Files searchable"), tile("work", "Working now")),
        h("div", { class: "grid g-2", style: { marginTop: "var(--gap)" } },
          card("Requests over time", h("p", { id: "ov-req-sub" }), h("div", { class: "card-body", id: "ov-req" }),
            viewToggle("req", () => drawCharts())),
          card("How long searches take", "From the question reaching Nebula to the answer leaving it.",
            h("div", { class: "card-body" }, h("div", { class: "legend", style: { marginBottom: "10px" } },
              h("span", null, h("i", { style: { background: "var(--series-1)" } }), "Median"),
              h("span", null, h("i", { style: { background: "var(--series-2)" } }), "Slowest 10%")), h("div", { id: "ov-lat" })),
            viewToggle("lat", () => drawCharts()))),
        h("div", { class: "grid g-2", style: { marginTop: "var(--gap)" } },
          card("Requests by tool", "Which Nebula tools Claude used, and how fast they answered.", h("div", { class: "card-body" }, h("div", { class: "hbars", id: "ov-tools" }))),
          card("Most active people", "Ranked by requests.", h("div", { class: "card-body" }, h("div", { class: "hbars", id: "ov-people" })))),
        h("div", { class: "grid g-2", style: { marginTop: "var(--gap)" } },
          card("Running now", "Background work in progress or about to start.", h("div", { class: "card-body flush" },
            h("div", { id: "ov-running" }), h("div", { class: "subhead", text: "Recently finished" }), h("div", { id: "ov-finished" })),
            h("button", { class: "btn sm", onclick: () => setTab("jobs", true) }, "All jobs")),
          card("Latest activity", "The newest requests, sign-ins and file reads.", h("div", { class: "card-body flush" }, h("div", { class: "feed compact", id: "ov-feed" })),
            h("button", { class: "btn sm", onclick: () => setTab("activity", true) }, "All activity"))));
      S.views.overview = true;
    }
    const prevWords = PREV_WORDS[data.range];
    const delta = (a, b) => {
      if (!o.has_previous || b == null) return h("span", { class: "delta", text: "" });
      if (!b && !a) return h("span", { class: "delta" }, "No change ", h("small", { text: `vs ${prevWords}` }));
      if (!b) return h("span", { class: "delta up" }, `+${num(a)} `, h("small", { text: `vs ${prevWords}` }));
      const pct = Math.round(((a - b) / b) * 100);
      return h("span", { class: `delta ${pct > 0 ? "up" : pct < 0 ? "down" : ""}` }, `${pct > 0 ? "+" : ""}${pct}% `, h("small", { text: `vs ${prevWords}` }));
    };
    const setTile = (id, value, fmt, sub, foot) => {
      const el = $(`#tile-${id}`);
      tween($(".tile-value", el), value, fmt);
      $(".tile-sub", el).replaceChildren(...[].concat(sub));
      $(".tile-foot", el).replaceChildren(...[].concat(foot || []));
    };
    const meter = (part, whole) => h("div", { class: "meter" }, h("i", { style: { width: `${whole ? Math.round((part / whole) * 100) : 0}%` } }));
    setTile("people", k.signed_in, num,
      `${plural(k.connected, "person", "people")} ever connected Claude · ${num(k.shared_drive)} shared Drive`,
      [meter(k.connected, k.signed_in), h("span", { class: "delta", text: k.signed_in ? `${Math.min(100, Math.round((k.connected / k.signed_in) * 100))}% ever connected` : "" })]);
    setTile("active", k.active, num, `Made at least one request in ${RANGE_WORDS[data.range]}.`, delta(k.active, k.active_prev));
    setTile("requests", k.requests, compact, k.errors ? `${plural(k.errors, "request")} failed.` : "None failed.",
      delta(k.requests, k.requests_prev));
    const reqTile = $("#tile-requests");
    $(".tile-spark", reqTile)?.remove();
    const sp = sparkline(o.series.map((s) => s.total));
    sp.classList.add("tile-spark");
    reqTile.append(sp);
    setTile("searches", k.searches, compact,
      k.p50 == null ? "No searches yet." : `Median ${dur(k.p50)} · slowest 10% over ${dur(k.p90)}.`, delta(k.searches, k.searches_prev));
    const total = k.total || 0;
    setTile("files", k.ready, num, `${num(k.waiting)} waiting · ${num(k.failed)} couldn't be read.`,
      [meter(k.ready, total), h("span", { class: "delta", text: `of ${num(total)} files` })]);
    setTile("work", data.pulse.jobs_running, num,
      `${plural(data.pulse.jobs_queued, "job")} waiting · ${plural(data.pulse.pieces_waiting, "piece")} of text to index.`,
      data.pulse.jobs_failed_24h ? tag(`${plural(data.pulse.jobs_failed_24h, "job")} failed today`, "bad") : tag("Nothing failed today", "good"));

    S.ov = { series: o.series, step: data.step_seconds };
    const step = data.step_seconds;
    $("#ov-req-sub").textContent = `Each bar is ${step >= 86400 ? "a day" : step >= 3600 ? `${step / 3600} hour${step > 3600 ? "s" : ""}` : `${step / 60} minutes`}. Failed requests show in red at the top of a bar.`;
    const csig = JSON.stringify(o.series);
    if (root._csig !== csig) { root._csig = csig; drawCharts(); }

    // Tools and people
    const tools = $("#ov-tools");
    if (!o.tools.length) tools.replaceChildren(empty("search", "No requests yet", `Nothing in ${RANGE_WORDS[data.range]}.`));
    else {
      if (tools.querySelector(".empty")) tools.replaceChildren();
      const max = Math.max(...o.tools.map((x) => x.n));
      syncRows(tools, o.tools.map((x) => ({ ...x, max })), (x) => x.tool, (x) => {
        const row = h("div", { class: "hbar" },
          h("div", { class: "lab" }, h("div", { class: "cell-main", text: TOOL_NAME[x.tool] || x.tool }),
            h("div", { class: "cell-sub", text: `median ${dur(x.p50)}${x.errors ? ` · ${num(x.errors)} failed` : ""}` })),
          h("div", { class: "track" }, h("i")), h("div", { class: "val", text: num(x.n) }));
        requestAnimationFrame(() => { row.querySelector(".track i").style.width = `${(x.n / max) * 100}%`; });
        return row;
      }, { animate: false });
    }
    const ppl = $("#ov-people");
    if (!o.top_people.length) ppl.replaceChildren(empty("people", "Nobody yet", `No one has made a request in ${RANGE_WORDS[data.range]}.`));
    else {
      if (ppl.querySelector(".empty")) ppl.replaceChildren();
      const max = Math.max(...o.top_people.map((x) => x.n));
      syncRows(ppl, o.top_people.map((x) => ({ ...x, max })), (x) => x.id, (x) => {
        const row = h("div", { class: "hbar" },
          h("div", { class: "lab" }, h("div", { class: "cell-main", text: x.name || x.email.split("@")[0] }),
            h("div", { class: "cell-sub" }, "last ", h("span", { "data-ago": x.last, text: ago(x.last) }))),
          h("div", { class: "track" }, h("i")), h("div", { class: "val", text: num(x.n) }));
        requestAnimationFrame(() => { row.querySelector(".track i").style.width = `${(x.n / max) * 100}%`; });
        return row;
      }, { animate: false });
    }
    // Running and latest
    const run = $("#ov-running");
    if (!o.running.length) { run.replaceChildren(empty("check", "Nothing running", "Nebula is idle. New work shows up here the moment it starts.")); run._rows = null; }
    else {
      if (run.querySelector(".empty")) run.replaceChildren();
      syncRows(run, o.running, (j) => j.id, jobLine);
    }
    const fin = $("#ov-finished");
    if (!o.finished.length) { fin.replaceChildren(empty("jobs", "Nothing finished yet", "")); fin._rows = null; }
    else { if (fin.querySelector(".empty")) fin.replaceChildren(); syncRows(fin, o.finished, (j) => j.id, jobDone, { animate: false }); }
    const feed = $("#ov-feed");
    if (!o.recent.length) { feed.replaceChildren(empty("activity", "No activity yet", `Nothing in ${RANGE_WORDS[data.range]}.`)); feed._rows = null; }
    else {
      if (feed.querySelector(".empty")) feed.replaceChildren();
      syncRows(feed, o.recent, (r) => r.key, (r) => feedRow(r, true));
    }
  }
  function jobLine(j) {
    const running = j.status === "running";
    const pct = j.progress != null ? Math.round(j.progress * 100) : null;
    return h("div", { class: "job-row" },
      h("div", { class: "feed-what" },
        h("div", { class: "act" }, running ? h("span", { class: "spin", style: { color: "var(--accent)" } }) : icon("clock"), j.label),
        h("div", { class: "txt", text: j.message || j.target || "" })),
      h("div", { class: "side" },
        h("span", { class: "cell-sub", text: running ? (j.eta ? `about ${dur(j.eta * 1000)} left` : "running") : "waiting to start" }),
        h("div", { class: `prog ${running && pct == null ? "indet" : ""}` },
          h("i", { style: { width: `${running ? (pct ?? 30) : 0}%` } }))));
  }

  function jobDone(j) {
    const ok = j.status === "done";
    return h("div", { class: "job-row" },
      h("div", { class: "feed-what" },
        h("div", { class: "act" }, ok ? icon("check") : icon("alert"), j.label),
        h("div", { class: "txt", style: ok ? null : { color: "var(--bad-ink)" }, text: ok ? (resultText(j) || j.target || "") : j.error })),
      h("div", { class: "side" }, h("div", { class: "cell-sub", text: when(j.updated_at) }),
        h("div", { class: "cell-sub", text: j.took != null ? `took ${dur(j.took * 1000)}` : "" })));
  }

  // ------------------------------------------------------------------ activity feed
  const CAT_ICON = { search: "search", open: "open", numbers: "numbers", admin: "admin", sources: "sources",
    "sign-in": "signin", indexing: "indexing", other: "other" };
  function feedRow(r, compactRow) {
    const ms = t(r.at);
    const time = h("div", { class: "feed-time" }, fTime.format(ms), compactRow ? null : h("small", { text: isToday(ms) ? ago(r.at) : fDay.format(ms), "data-ago": isToday(ms) ? r.at : null }));
    const txt = r.text ? h("div", { class: `txt ${r.category === "search" ? "q" : ""}` },
      r.category === "numbers" ? h("code", { class: compactRow ? "" : "block", text: r.text }) : r.text) : null;
    const what = h("div", { class: "feed-what" },
      h("div", { class: "act" }, icon(CAT_ICON[r.category] || "other"), r.label),
      txt, !r.ok && r.detail ? h("div", { class: "err" }, icon("alert"), r.detail) : null,
      !compactRow && r.hits && r.hits.length ? hitList(r.hits, r.trace_id) : null,
      !compactRow && r.trace_id && !(r.hits && r.hits.length) ? h("button", { class: "linkbtn", type: "button", style: { marginTop: "6px" }, onclick: () => openSearch(r.trace_id) }, "See every step") : null);
    if (compactRow) {
      return h("div", { class: `feed-row ${r.ok ? "" : "fail"}` }, h("div", { class: "feed-time", text: fHM.format(ms) }),
        h("div", { style: { minWidth: 0 } }, what, h("div", { class: "cell-sub", style: { marginTop: "2px" }, text: r.person ? (r.person.name || r.person.email) : "Nebula" })),
        h("div", { class: "feed-ms", text: r.ms != null ? dur(r.ms) : "" }));
    }
    return h("div", { class: `feed-row ${r.ok ? "" : "fail"}` }, time, h("div", { class: "feed-who" }, who(r.person, "sm")), what,
      h("div", { class: "feed-res", text: r.ok ? r.detail || "" : "Failed" }), h("div", { class: "feed-ms", text: r.ms != null ? dur(r.ms) : "" }));
  }
  // What a search returned, in the order Claude got it.
  function hitHref(x) {
    const [kind, id] = String(x.id || "").split(":");
    if (x.url) return x.url;
    return kind === "item" ? `/item/${id}` : null;
  }
  function hitList(hits, traceId) {
    return h("details", { class: "hits" },
      h("summary", null, icon("chevron"), `What it found (${hits.length})`,
        traceId ? h("button", { class: "linkbtn", type: "button", onclick: (e) => { e.preventDefault(); e.stopPropagation(); openSearch(traceId); } }, "See every step") : null),
      h("ol", null, ...hits.map((x) => {
        const href = hitHref(x);
        return h("li", null,
          h("div", { class: "hit-main" },
            href ? h("a", { href, target: "_blank", rel: "noopener", text: x.title || x.id }) : h("span", { text: x.title || x.id }),
            h("div", { class: "cell-sub", text: [x.type, x.collection, x.path].filter(Boolean).join(" · ") })),
          h("span", { class: "hit-score", title: "Relevance score after reranking (higher is closer to the question)",
            text: x.score != null ? Number(x.score).toFixed(2) : "" }));
      })));
  }
  function renderActivity(data) {
    const root = $("#view-activity");
    const A = S.act;
    if (!S.views.activity) {
      const status = h("div", { class: "seg", id: "act-status" },
        ...[["", "All"], ["ok", "Succeeded"], ["failed", "Failed"]].map(([v, l]) => h("button", { "data-v": v, "aria-pressed": String(A.status === v),
          onclick: () => { A.status = v; $$("#act-status button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.v === v))); drawActivity(); } }, l)));
      root.replaceChildren(h("section", { class: "card" },
        h("div", { class: "filters" },
          h("select", { class: "select", id: "act-person", "aria-label": "Person", onchange: (e) => { A.person = e.target.value; refresh(true); } }),
          h("input", { class: "input search", id: "act-q", type: "search", placeholder: "Search activity…", "aria-label": "Search activity",
            oninput: (e) => { A.q = e.target.value.trim().toLowerCase(); drawActivity(); } }),
          status, h("span", { class: "grow" }), h("span", { class: "count", id: "act-count" })),
        h("div", { class: "filters", style: { paddingTop: "12px", paddingBottom: "12px" } }, h("div", { class: "chips", id: "act-cats" })),
        h("div", { class: "feed scroll", id: "act-feed" })));
      S.views.activity = true;
    }
    // People filter options
    const sel = $("#act-person");
    const opts = [["", "Everyone"], ...data.people_index.map((p) => [p.id, p.name ? `${p.name} (${p.email})` : p.email])];
    const optSig = JSON.stringify(opts);
    if (sel._sig !== optSig) { sel.replaceChildren(...opts.map(([v, l]) => h("option", { value: v, text: l }))); sel._sig = optSig; }
    sel.value = A.person;
    // Merge new rows
    const incoming = data.activity.rows.filter((r) => !A.keys.has(r.key));
    incoming.forEach((r) => A.keys.add(r.key));
    A.fresh = A.primed ? new Set(incoming.map((r) => r.key)) : new Set();
    A.primed = true;
    A.rows = [...incoming, ...A.rows].sort((a, b) => t(b.at) - t(a.at)).slice(0, 800);
    if (A.rows.length) A.after = A.rows[0].at;
    drawActivity();
  }
  function drawActivity() {
    const A = S.act;
    const counts = {};
    for (const r of A.rows) counts[r.category] = (counts[r.category] || 0) + 1;
    const cats = $("#act-cats");
    const chip = (v, l, c) => h("button", { class: "chip", "aria-pressed": String(A.category === v),
      onclick: () => { A.category = A.category === v && v ? "" : v; drawActivity(); } }, v ? icon(CAT_ICON[v]) : null, l, h("span", { class: "c", text: num(c) }));
    cats.replaceChildren(chip("", "All", A.rows.length),
      ...Object.keys(CAT).filter((k) => counts[k]).map((k) => chip(k, CAT[k][0], counts[k])));
    let rows = A.rows;
    if (A.category) rows = rows.filter((r) => r.category === A.category);
    if (A.status === "ok") rows = rows.filter((r) => r.ok);
    if (A.status === "failed") rows = rows.filter((r) => !r.ok);
    if (A.q) rows = rows.filter((r) => [r.label, r.text, r.detail, r.person && r.person.name, r.person && r.person.email].join(" ").toLowerCase().includes(A.q));
    $("#act-count").textContent = `${plural(rows.length, "event")} in ${RANGE_WORDS[S.range]}`;
    const feed = $("#act-feed");
    if (!rows.length) {
      feed.replaceChildren(empty("activity", A.rows.length ? "Nothing matches these filters" : "No activity yet",
        A.rows.length ? "Try another filter or a wider time range." : `Nothing has happened in ${RANGE_WORDS[S.range]}. New events appear here within a few seconds.`));
      feed._rows = null;
      return;
    }
    if (feed.querySelector(".empty")) { feed.replaceChildren(); feed._rows = null; }
    syncRows(feed, rows.slice(0, 600), (r) => r.key, (r) => feedRow(r, false), { fresh: A.fresh || new Set() });
  }

  // ------------------------------------------------------------------ people
  const PEOPLE_COLS = [
    { key: "person", label: "Person", sort: (p) => (p.name || p.email).toLowerCase(), words: "name" },
    { key: "access", label: "Access" },
    { key: "claude", label: "Claude", sort: (p) => t(p.last_request) || (p.clients[0] ? t(p.clients[0].first_seen_at) / 1e3 : 0), words: "when they last used Claude" },
    { key: "drive", label: "Drive", sort: (p) => p.drive.length, words: "how much they shared from Drive" },
    { key: "files_visible", label: "Files they see", n: true, sort: (p) => p.files_visible, words: "Drive files they can see" },
    { key: "requests", label: "Requests", n: true, sort: (p) => p.requests, words: "requests" },
    { key: "searches", label: "Searches", n: true, sort: (p) => p.searches, words: "searches" },
    { key: "last_request", label: "Last request", sort: (p) => t(p.last_request) || 0, words: "last request" },
  ];
  function renderPeople(data) {
    const root = $("#view-people");
    const P = S.people;
    const rows = data.people.rows;
    S.peopleRows = rows;
    if (!S.views.people) {
      const seg = h("div", { class: "seg", id: "ppl-only" }, ...[["all", "Everyone"], ["claude", "Ever connected Claude"], ["drive", "Shared Drive"], ["never", "Never connected"]]
        .map(([v, l]) => h("button", { "data-v": v, "aria-pressed": String(P.only === v), onclick: () => { P.only = v; $$("#ppl-only button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.v === v))); drawPeople(); } }, l)));
      root.replaceChildren(
        h("div", { class: "grid g-4", id: "ppl-tiles" }),
        h("section", { class: "card", style: { marginTop: "var(--gap)" } },
          h("div", { class: "filters" },
            h("input", { class: "input search", type: "search", placeholder: "Find a person…", "aria-label": "Find a person", oninput: (e) => { P.q = e.target.value.trim().toLowerCase(); drawPeople(); } }),
            seg, h("span", { class: "grow" }), h("span", { class: "sortnote", id: "ppl-sortnote" })),
          h("div", { class: "tablewrap" }, h("table", { class: "t" }, h("thead", null, h("tr", { id: "ppl-head" })), h("tbody", { id: "ppl-body" })))));
      S.views.people = true;
    }
    const signed = rows.filter((p) => p.signed_in).length;
    const claude = rows.filter((p) => p.clients.length || p.requests_all).length;
    const drive = rows.filter((p) => p.drive.length).length;
    const active = rows.filter((p) => p.requests).length;
    const tiles = [["Signed in", signed, "Signed in to Nebula with Google at least once."],
      ["Ever connected Claude", claude, "Connected at least once. Nebula can't see if it's still installed."],
      ["Shared Drive", drive, "Shared a folder, file or Shared Drive with Nebula."],
      [`Active, ${RANGE_WORDS[data.range]}`, active, "Made at least one request in this period."]];
    const tl = $("#ppl-tiles");
    if (!tl.children.length) tl.append(...tiles.map(([l], i) => h("section", { class: "card tile", style: { minHeight: "112px" }, "data-i": i },
      h("div", { class: "tile-label", text: l }), h("div", { class: "tile-value" }), h("div", { class: "tile-sub" }))));
    tiles.forEach(([l, v, sub], i) => {
      const el = tl.children[i];
      $(".tile-label", el).textContent = l;
      tween($(".tile-value", el), v, num);
      $(".tile-sub", el).textContent = sub;
    });
    $("#count-people").hidden = !rows.length;
    $("#count-people").textContent = num(rows.length);
    drawPeople();
  }
  function drawPeople() {
    const P = S.people;
    let rows = (S.peopleRows || []).slice();
    if (P.q) rows = rows.filter((p) => `${p.name} ${p.email}`.toLowerCase().includes(P.q));
    if (P.only === "claude") rows = rows.filter((p) => p.clients.length || p.requests_all);
    if (P.only === "drive") rows = rows.filter((p) => p.drive.length);
    if (P.only === "never") rows = rows.filter((p) => !p.clients.length && !p.requests_all);
    const col = PEOPLE_COLS.find((c) => c.key === P.sort) || PEOPLE_COLS.find((c) => c.key === "last_request");
    rows.sort((a, b) => { const x = col.sort(a), y = col.sort(b); return (x > y ? 1 : x < y ? -1 : 0) * P.dir; });
    const head = $("#ppl-head");
    head.replaceChildren(...PEOPLE_COLS.map((c) => h("th", { class: `${c.n ? "n" : ""} ${c.sort ? "sortable" : ""}`,
      "aria-sort": c.key === P.sort ? (P.dir > 0 ? "ascending" : "descending") : null,
      onclick: c.sort ? () => { if (P.sort === c.key) P.dir *= -1; else { P.sort = c.key; P.dir = c.key === "person" ? 1 : -1; } drawPeople(); } : null },
      c.label, c.key === P.sort ? h("span", { class: "arrow", text: P.dir > 0 ? "↑" : "↓" }) : null)));
    const dirWords = col.key === "person" ? (P.dir > 0 ? "A to Z" : "Z to A") : col.key === "last_request" || col.key === "claude" ? (P.dir > 0 ? "oldest first" : "newest first") : (P.dir > 0 ? "fewest first" : "most first");
    $("#ppl-sortnote").textContent = `${plural(rows.length, "person", "people")} · sorted by ${col.words}, ${dirWords}`;
    const body = $("#ppl-body");
    if (!rows.length) {
      body.replaceChildren(h("tr", null, h("td", { colspan: PEOPLE_COLS.length }, empty("people", "No one here yet",
        (S.peopleRows || []).length ? "Nobody matches these filters." : "People appear once they sign in to Nebula or connect it to Claude."))));
      body._rows = null;
      return;
    }
    if (body.querySelector(".empty")) { body.replaceChildren(); body._rows = null; }
    syncRows(body, rows, (p) => p.id, personRow, { animate: false });
  }
  function personRow(p) {
    const access = h("div", { class: "chips inline", style: { gap: "4px" } },
      p.is_admin ? tag("Admin", "ink", { dot: false }) : null,
      p.rl_member ? tag("RL", "", { dot: false }) : tag("Outside RL", "warn", { dot: false }));
    const claude = claudeStatus(p);
    const drive = p.drive.length ? h("div", null, h("div", { class: "cell-main", text: plural(p.drive.length, "source") }),
      h("div", { class: "cell-sub clip", style: { maxWidth: "150px" }, title: p.drive.map((d) => d.name).join(", "), text: p.drive.map((d) => d.name).join(", ") })) : h("span", { class: "faint", text: "None" });
    const tr = h("tr", { class: "click", onclick: () => openPerson(p), tabindex: "0", onkeydown: (e) => { if (e.key === "Enter") openPerson(p); } },
      h("td", null, who(p)), h("td", { class: "nowrap" }, access),
      h("td", null, claude), h("td", null, drive),
      h("td", { class: "n", text: num(p.files_visible) }),
      h("td", { class: "n" }, h("span", { class: "reqcell" }, sparkline(p.spark), h("span", { text: num(p.requests) }))),
      h("td", { class: "n", text: num(p.searches) }),
      h("td", { class: "nowrap" }, p.last_request ? h("div", null, h("div", { class: "cell-main", "data-ago": p.last_request, text: ago(p.last_request) }),
        h("div", { class: "cell-sub", text: when(p.last_request) })) : h("span", { class: "faint", text: "Never" })));
    return tr;
  }
  const INSTALL_NOTE = "Nebula sees requests, not whether the connector is still installed in Claude.";
  function claudeStatus(p) {
    const c0 = p.clients[0];
    const recent = p.last_request && now() - t(p.last_request) < 86400000;
    const line = (txt) => h("div", { class: "cell-sub", style: { marginTop: "4px" }, text: txt });
    if (p.last_request) return h("div", { class: "nowrap", title: INSTALL_NOTE },
      recent ? tag("Used today", "good") : tag("Not used recently", ""), line(`last used ${ago(p.last_request)}`));
    if (c0) return h("div", { class: "nowrap", title: INSTALL_NOTE }, tag("Connected, no requests yet", "blue"), line(`connected ${when(c0.first_seen_at)}`));
    return h("div", { class: "nowrap", title: INSTALL_NOTE }, tag("Never connected", ""), line(p.signed_in ? "Web sign-in only" : ""));
  }
  function openPerson(p) {
    const d = $("#drawer");
    const li = (main, sub, right) => h("div", { class: "li" }, h("div", { style: { minWidth: 0 } }, h("div", { class: "cell-main", text: main }), sub ? h("div", { class: "cell-sub", text: sub }) : null), right || null);
    $("#drawer-body").replaceChildren(
      avatar(p, "lg"), h("h2", { id: "drawer-title", text: p.name || p.email.split("@")[0] }), h("div", { class: "cell-sub", text: p.email }),
      h("div", { class: "chips", style: { marginTop: "12px" } }, p.is_admin ? tag("Admin", "ink", { dot: false }) : null,
        p.rl_member ? tag("RL member", "", { dot: false }) : tag("Outside RL", "warn", { dot: false }), p.status !== "active" ? tag("Deactivated", "bad") : null),
      h("h3", { text: `In ${RANGE_WORDS[S.range]}` }),
      h("div", { class: "kv" }, ...[["Requests", p.requests], ["Searches", p.searches], ["Failed", p.errors]]
        .map(([l, v]) => h("div", null, h("span", { text: l }), h("b", { text: num(v) })))),
      h("h3", { text: "Signing in" }),
      h("div", { class: "list" }, li("First seen", p.first_seen ? when(p.first_seen) : "Not yet"), li("Last seen", p.last_seen ? `${when(p.last_seen)} (${ago(p.last_seen)})` : "Not yet"),
        li("Requests, all time", num(p.requests_all))),
      h("h3", { text: "Claude" }),
      h("div", { class: "list" },
        li("Last request", p.last_request ? `${when(p.last_request)} (${ago(p.last_request)})` : "None yet"),
        ...p.clients.map((c) => li(c.client, `First connected ${when(c.first_seen_at)} · last seen ${ago(c.last_seen_at)}`,
          h("span", { class: "cell-sub nowrap", text: plural(c.connects, "connection") })))),
      h("p", { class: "cell-sub", style: { marginTop: "8px" }, text: INSTALL_NOTE }),
      h("h3", { text: "Shared with Nebula from Drive" }),
      p.drive.length ? h("div", { class: "list" }, ...p.drive.map((x) => li(x.name || "Untitled", `${x.kind} · shared ${when(x.created_at)}`,
        x.status === "active" ? h("span", { class: "cell-sub nowrap", text: plural(x.files, "file") }) : tag("Can't open", "bad"))))
        : h("p", { class: "muted", text: "Hasn't shared anything with Nebula's Google account." }),
      h("div", { style: { marginTop: "26px" } }, h("button", { class: "btn primary", onclick: () => { closeDrawer(); S.act.person = p.id; setTab("activity", true); } }, icon("activity"), "Show their activity")));
    d.classList.add("open"); d.setAttribute("aria-hidden", "false");
    setTimeout(() => $(".drawer-close").focus(), 50);
  }
  function closeDrawer() { const d = $("#drawer"); d.classList.remove("open", "wide"); d.setAttribute("aria-hidden", "true"); }

  // ------------------------------------------------------------------ one search, every step
  const STEP_WORDS = {
    scope: "Checked what this person can see",
    embed: "Turned the question into a meaning vector",
    meaning: "Searched by meaning",
    words: "Searched by exact words",
    rerank: "Reranker read the top passages",
    assemble: "Grouped passages by file and built the reply",
    other: "Sign-in check, limits and sending the reply",
  };
  async function openSearch(id) {
    const d = $("#drawer");
    $("#drawer-body").replaceChildren(h("h2", { id: "drawer-title", text: "Loading the search…" }));
    d.classList.add("open", "wide"); d.setAttribute("aria-hidden", "false");
    setTimeout(() => $(".drawer-close").focus(), 50);
    let x;
    try {
      const res = await fetch(`/admin/api/search/${id}`, { credentials: "same-origin", headers: { Accept: "application/json" } });
      x = await res.json();
      if (!res.ok) throw new Error(x.error || res.statusText);
    } catch (e) {
      $("#drawer-body").replaceChildren(h("h2", { id: "drawer-title", text: "Couldn't load this search" }), h("p", { class: "muted", text: String(e.message || e) }));
      return;
    }
    $("#drawer-body").replaceChildren(...searchView(x).filter(Boolean));
  }
  function searchView(x) {
    const T = x.trace || {};
    const titles = T.titles || {};
    const title = (src) => titles[src] || src;
    const out = [];
    out.push(h("div", { class: "cell-sub", text: [x.person ? (x.person.name || x.person.email) : "Someone", when(x.at)].join(" · ") }),
      h("h2", { id: "drawer-title", class: "q-title", text: `“${x.question}”` }),
      h("div", { class: "chips", style: { marginTop: "10px" } },
        tag(`${dur(x.ms)} in all`, "", { dot: false }), tag(plural(x.result_count || 0, "result"), "", { dot: false }),
        x.ok ? null : tag("Failed", "bad")));
    if (!x.ok && x.error) out.push(h("div", { class: "err", style: { marginTop: "10px" } }, icon("alert"), x.error));

    // Where the time went: one bar per step, same colour, longest step sets the scale.
    const steps = (T.steps || []).map((s) => ({ ...s }));
    const counted = steps.reduce((a, s) => a + s.ms, 0);
    if (x.ms - counted > 1) steps.push({ step: "other", ms: x.ms - counted });
    if (steps.length) {
      const max = Math.max(...steps.map((s) => s.ms), 1);
      out.push(h("h3", { text: "Where the time went" }),
        h("div", { class: "timebars" }, ...steps.map((s) => h("div", { class: "tb" },
          h("span", { class: "tb-label", text: STEP_WORDS[s.step] || s.step }),
          h("span", { class: "tb-track" }, h("i", { style: { width: `${Math.max(1.5, (s.ms / max) * 100)}%` } })),
          h("span", { class: "tb-ms", text: dur(s.ms) })))));
    }

    // 1. The question
    out.push(h("h3", { text: "1 · The question" }));
    const qrows = [h("div", { class: "li" }, h("span", { class: "muted", text: "Asked" }), h("b", { text: x.question }))];
    out.push(h("div", { class: "list qlist" }, ...qrows));
    if ((T.terms || []).length) out.push(h("p", { class: "cell-sub", style: { margin: "10px 0 6px" }, text: "Words the word search looked for:" }),
      h("div", { class: "chips" }, ...T.terms.map((w) => h("span", { class: "chip static", text: w }))));
    if (!T.steps) { out.push(h("p", { class: "muted", text: "This search ran before Nebula recorded its steps, so only the results are shown." })); }

    // 2 and 3. What each method found
    const found = (rows, label, n) => {
      if (!rows) return [];
      const top = rows.slice(0, 10);
      const li = (r, i) => h("li", null, h("span", { class: "rk", text: i + 1 }),
        h("div", { class: "hit-main" }, h("div", { class: "cell-main", text: title(r.src) }), h("div", { class: "cell-sub", text: r.where || "" })));
      return [h("h3", { text: label }),
        h("p", { class: "cell-sub", style: { margin: "-4px 0 8px" }, text: rows.length ? `${plural(rows.length, "passage")} found${rows.length > 10 ? ", top 10 shown" : ""}. A passage is a short piece of a file.` : "Nothing found this way." }),
        rows.length ? h("ol", { class: "plist" }, ...top.map(li)) : null,
        rows.length > 10 ? h("details", { class: "more" }, h("summary", null, `Show all ${rows.length}`), h("ol", { class: "plist", start: 11 }, ...rows.slice(10).map((r, i) => li(r, i + 10)))) : null];
    };
    out.push(...found(T.meaning, "2 · Closest in meaning"), ...found(T.words, "3 · Matching the exact words"));

    // 4. Combined, then reranked
    if (T.pool) {
      const order = T.reranked || T.pool.map((_, i) => [i, null]);
      out.push(h("h3", { text: "4 · Combined, then reranked" }),
        h("p", { class: "cell-sub", style: { margin: "-4px 0 10px" }, text:
          T.rerank_failed ? `The two lists merged into ${plural(T.combined || T.pool.length, "passage")}. The reranker failed (${T.rerank_failed}), so this is the merged order, not a reranked one.`
          : T.reranker === "none" ? `The two lists merged into ${plural(T.combined || T.pool.length, "passage")}, with passages high on both lists first. No reranker is switched on, so this is the merged order.`
          : `The two lists merged into ${plural(T.combined || T.pool.length, "passage")}, with passages high on both lists first. The top ${T.pool.length} went to the reranker, which read the question next to the first ${num(T.rerank_chars || 0)} characters of each and scored how well it answers it. Higher is better.` }),
        h("div", { class: "rr" }, ...order.map(([i, score], k) => {
          const p = T.pool[i];
          const move = i - k;
          return h("div", { class: "rr-row" },
            h("span", { class: "rk big", text: k + 1 }),
            h("div", { class: "hit-main" },
              h("div", { class: "cell-main", text: title(p.src) }),
              h("div", { class: "cell-sub", text: [p.where, p.meaning ? `meaning #${p.meaning}` : "not in meaning list", p.words ? `words #${p.words}` : "no word match"].filter(Boolean).join(" · ") }),
              p.text ? h("div", { class: "rr-text", text: p.text }) : null),
            h("div", { class: "rr-side" },
              h("b", { text: score != null ? Number(score).toFixed(3) : "—" }),
              h("span", { class: `mv ${move > 0 ? "up" : move < 0 ? "down" : ""}`, title: `Was #${i + 1} after merging`,
                text: move > 0 ? `↑ ${move}` : move < 0 ? `↓ ${-move}` : "same" })));
        })));
    }

    // 5. What Claude got
    out.push(h("h3", { text: `${T.steps ? "5 · " : ""}What went back to Claude` }),
      h("p", { class: "cell-sub", style: { margin: "-4px 0 8px" }, text: "Passages from the same file were grouped, so each file appears once with its best passage's score." }),
      (x.results || []).length ? h("ol", { class: "plist" }, ...x.results.map((r, i) => h("li", null, h("span", { class: "rk", text: i + 1 }),
        h("div", { class: "hit-main" }, hitHref(r) ? h("a", { href: hitHref(r), target: "_blank", rel: "noopener", class: "cell-main", text: r.title || r.id }) : h("div", { class: "cell-main", text: r.title || r.id }),
          h("div", { class: "cell-sub", text: r.type || "" })),
        h("span", { class: "hit-score", text: r.score != null ? Number(r.score).toFixed(3) : "" }))))
        : h("p", { class: "muted", text: "Nothing came back." }));
    return out;
  }

  // ------------------------------------------------------------------ indexing
  const STATES = [["queued", "Waiting to be read"], ["reading", "Reading now"], ["indexing", "Making searchable"],
    ["ready", "Searchable"], ["no_text", "No text found"], ["failed", "Couldn't read"]];
  function stateTag(f) {
    switch (f.state) {
      case "queued": return tag("Waiting", "");
      case "reading": return tag("Reading", "blue", { spin: true });
      case "indexing": return tag("Making searchable", "blue", { spin: true });
      case "ready": return tag("Searchable", "good");
      case "no_text": return tag("No text found", "warn", { title: "Nebula read the file but found no text in it." });
      default: return tag("Couldn't read", "bad", { title: f.status_note });
    }
  }
  function renderIndexing(data) {
    const root = $("#view-indexing");
    const I = S.idx, x = data.indexing;
    if (!S.views.indexing) {
      root.replaceChildren(
        card("Where every file is", h("p", { id: "ix-sub" }), h("div", { class: "card-body" }, h("div", { class: "statebar", id: "ix-bar" },
          ...STATES.map(([k]) => h("i", { class: `st-${k}` }))), h("div", { class: "statekeys", id: "ix-keys" })),
          h("button", { class: "btn sm", onclick: (e) => act("embed_now", {}, e.currentTarget) }, icon("play"), "Make waiting text searchable now")),
        h("div", { class: "grid g-2", style: { marginTop: "var(--gap)" } },
          card("Working on now", "Files being read, and text being made searchable.", h("div", { class: "card-body flush", id: "ix-work" })),
          card("Indexing log", "Every file read, made searchable, removed or failed.", h("div", { class: "card-body flush" }, h("div", { class: "feed compact", id: "ix-log", style: { maxHeight: "360px", overflowY: "auto" } })))),
        h("section", { class: "card", style: { marginTop: "var(--gap)" } },
          h("div", { class: "filters" },
            h("input", { class: "input search", type: "search", placeholder: "Find a file…", "aria-label": "Find a file", oninput: (e) => { I.q = e.target.value.trim().toLowerCase(); drawFiles(); } }),
            h("span", { class: "grow" }), h("span", { class: "count", id: "ix-count" })),
          h("div", { class: "tablewrap" }, h("table", { class: "t" },
            h("thead", null, h("tr", null, ...["File", "Source", "State", "Search pieces", "Parts not read", "Version", "Read", "Changed in Drive", ""].map((l, i) =>
              h("th", { class: i === 5 ? "n" : "", text: l })))),
            h("tbody", { id: "ix-body" })))));
      S.views.indexing = true;
    }
    S.files = x.files;
    const total = x.files.length;
    const keys = $("#ix-keys");
    if (!keys.children.length) keys.append(...STATES.map(([k, l]) => h("button", { class: "statekey", "data-k": k, "aria-pressed": "false",
      onclick: () => { I.state = I.state === k ? "" : k; drawFiles(); } }, h("span", { class: "k" }, h("i", { class: `st-${k}` }), l), h("b"))));
    STATES.forEach(([k], i) => {
      tween(keys.children[i].querySelector("b"), x.counts[k] || 0, num);
      $(`#ix-bar .st-${k}`).style.flexBasis = total ? `${((x.counts[k] || 0) / total) * 100}%` : "0%";
    });
    $("#ix-sub").textContent = `${plural(total, "file")} · ${plural(data.pulse.pieces_waiting, "piece")} of text waiting to be made searchable.`;
    const ids = new Set(x.files.map((f) => f.id));
    I.fresh = I.seen ? new Set([...ids].filter((k) => !I.seen.has(k))) : new Set();
    I.seen = ids;
    const work = $("#ix-work");
    if (!x.work.length) { work.replaceChildren(empty("check", "All caught up", "Nothing is being read or indexed right now.")); work._rows = null; }
    else { if (work.querySelector(".empty")) work.replaceChildren(); syncRows(work, x.work, (j) => j.id, jobLine); }
    const log = $("#ix-log");
    const label = { read: "Read", searchable: "Now searchable", removed: "Removed", failed: "Couldn't read" };
    if (!x.events.length) { log.replaceChildren(empty("indexing", "Nothing indexed yet", "File reads show up here as they happen.")); log._rows = null; }
    else {
      if (log.querySelector(".empty")) log.replaceChildren();
      syncRows(log, x.events, (e) => e.id, (e) => feedRow({ key: e.id, at: e.at, category: "indexing", label: `${label[e.event]} · ${e.title}`,
        text: e.event === "failed" ? "" : e.detail, ok: e.event !== "failed", detail: e.event === "failed" ? e.detail : "", ms: e.ms, person: null }, true));
    }
    drawFiles();
  }
  function drawFiles() {
    const I = S.idx;
    $$("#ix-keys .statekey").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.k === I.state)));
    let files = S.files || [];
    if (I.state) files = files.filter((f) => f.state === I.state);
    if (I.q) files = files.filter((f) => `${f.title} ${f.path} ${f.source} ${f.format}`.toLowerCase().includes(I.q));
    $("#ix-count").textContent = I.state ? `${plural(files.length, "file")} · ${STATES.find((s) => s[0] === I.state)[1].toLowerCase()}` : plural(files.length, "file");
    const body = $("#ix-body");
    if (!files.length) {
      body.replaceChildren(h("tr", null, h("td", { colspan: 9 }, empty("indexing", (S.files || []).length ? "No files match" : "No files yet",
        (S.files || []).length ? "Try another state or search." : "Share a folder with Nebula's Google account."))));
      body._rows = null; return;
    }
    if (body.querySelector(".empty")) { body.replaceChildren(); body._rows = null; }
    syncRows(body, files, (f) => f.id, (f) => {
      const pct = f.pieces ? (f.ready / f.pieces) * 100 : 0;
      return h("tr", null,
        h("td", null, h("div", { class: "file" }, h("span", { class: `fmt ${f.format}`, text: (f.format || "file").slice(0, 4) }),
          h("div", { class: "meta" }, h("div", { class: "cell-main", title: f.title }, f.url ? h("a", { href: f.url, target: "_blank", rel: "noopener", text: f.title }) : f.title),
            h("div", { class: "cell-sub", text: f.path || "" })))),
        h("td", null, h("div", { class: "cell-sub clip", style: { maxWidth: "200px" }, text: f.source })),
        h("td", { style: { maxWidth: "220px" } }, stateTag(f), f.job && f.job.message ? h("div", { class: "cell-sub clamp2", style: { marginTop: "4px" }, text: f.job.message }) : null,
          f.state === "failed" && f.status_note ? h("div", { class: "cell-sub clamp2", style: { marginTop: "4px", color: "var(--bad-ink)" }, title: f.status_note, text: f.status_note }) : null),
        h("td", null, f.pieces ? h("div", { class: "pieces" }, h("div", { class: `prog ${pct >= 100 ? "good" : ""}` }, h("i", { style: { width: `${pct}%` } })),
          h("span", { class: "cell-sub", text: `${num(f.ready)} of ${num(f.pieces)}` }))
          : f.tables ? h("span", { class: "cell-sub", text: plural(f.tables, "table") }) : h("span", { class: "faint", text: "—" })),
        h("td", null, f.unread_parts ? tag(plural(f.unread_parts, "part"), "warn", { title: "Pages or pictures whose text Nebula couldn't extract. Claude can read them as pictures." }) : h("span", { class: "faint", text: "None" })),
        h("td", { class: "n", text: f.version ? `v${f.version}` : "—" }),
        h("td", { class: "nowrap" }, f.indexed_at ? h("div", null, h("div", { class: "cell-main", "data-ago": f.indexed_at, text: ago(f.indexed_at) }), h("div", { class: "cell-sub", text: when(f.indexed_at) })) : h("span", { class: "faint", text: "Not yet" })),
        h("td", { class: "nowrap" }, f.modified_in_source ? h("span", { class: "cell-sub", text: when(f.modified_in_source) }) : h("span", { class: "faint", text: "—" })),
        h("td", { class: "n" }, h("button", { class: "icon-btn bordered", title: "Re-read this file now", "aria-label": `Re-read ${f.title} now`,
          onclick: (e) => act("reread_item", { item_id: f.id }, e.currentTarget) }, icon("refresh"))));
    }, { fresh: I.fresh || new Set() });
  }

  // ------------------------------------------------------------------ sources
  const PRESETS = { seconds: [15, 30, 60, 120, 300, 900, 3600], minutes: [5, 15, 30, 60, 360, 1440] };
  function presetLabel(v, unit) {
    const s = unit === "minutes" ? v * 60 : v;
    if (s < 60) return `Every ${s} seconds`;
    if (s < 3600) return s === 60 ? "Every minute" : `Every ${s / 60} minutes`;
    return s === 3600 ? "Every hour" : `Every ${s / 3600} hours`;
  }
  function updateRing(el) {
    const last = t(el.dataset.last), next = t(el.dataset.next), busy = el.dataset.busy === "1", queued = el.dataset.queued === "1";
    const fg = el.querySelector(".fg"), b = el.querySelector(".lbl b"), sp = el.querySelector(".lbl span");
    el.classList.toggle("busy", busy);
    const C = 2 * Math.PI * 12;
    if (busy) { fg.style.strokeDashoffset = C * 0.7; b.textContent = "Running now"; sp.textContent = Number.isNaN(last) ? "" : `last finished ${ago(el.dataset.last)}`; return; }
    if (queued) {
      fg.style.strokeDashoffset = 0; b.textContent = "Queued";
      sp.textContent = el.dataset.waiting ? `starts after: ${el.dataset.waiting.toLowerCase()}` : "starts in a moment";
      return;
    }
    if (Number.isNaN(next)) { fg.style.strokeDashoffset = C; b.textContent = "Not run yet"; sp.textContent = "runs at the next check"; return; }
    const span = next - last, left = (next - now()) / 1000;
    fg.style.strokeDashoffset = C * Math.max(0, Math.min(1, left * 1000 / span));
    b.textContent = left > 1 ? `Next in ${clockLeft(left)}` : "Due now";
    sp.textContent = `last ran ${ago(el.dataset.last)}`;
  }
  function renderSources(data) {
    const root = $("#view-sources");
    const x = data.sources;
    if (!S.views.sources) {
      root.replaceChildren(
        card("How often Nebula checks", "These timings apply straight away. Use the buttons to run a check now instead of waiting.", h("div", { class: "card-body flush", id: "src-sched" })),
        h("div", { class: "grid g-2", style: { marginTop: "var(--gap)" } },
          card("Re-read one file now", "Paste a link to a Drive file Nebula can open. It's read and made searchable straight away.",
            h("div", null, h("form", { class: "linkbox", onsubmit: (e) => { e.preventDefault(); const i = $("#src-link"); act("sync_link", { link: i.value }, $("#src-link-btn")).then((r) => { if (r) i.value = ""; }); } },
              h("input", { class: "input", id: "src-link", type: "url", required: true, placeholder: "https://docs.google.com/…", "aria-label": "Drive link" }),
              h("button", { class: "btn primary", id: "src-link-btn", type: "submit" }, icon("refresh"), "Read now")))),
          card("Adding something from Drive", "Share it with Nebula's Google account as an Editor. Nebula only reads; Editor lets it see who each file is shared with.",
            h("div", { class: "card-body", id: "src-bot" }))),
        h("section", { class: "card", style: { marginTop: "var(--gap)" } },
          h("div", { class: "card-head" }, h("div", null, h("h2", { text: "Shared with Nebula" }), h("p", { id: "src-count" })),
            h("div", { class: "actions" }, h("button", { class: "btn sm", onclick: (e) => act("full_reread", {}, e.currentTarget) }, icon("refresh"), "Re-read everything"))),
          h("div", { class: "card-body flush" }, h("div", { class: "tablewrap" }, h("table", { class: "t" },
            h("thead", null, h("tr", null, ...["Source", "Shared by", "Added", "Files", "Searchable", "Waiting", "Failed", "Status", ""].map((l, i) =>
              h("th", { class: i >= 3 && i <= 6 ? "n" : "", text: l })))), h("tbody", { id: "src-body" }))))));
      S.views.sources = true;
    }
    // Schedule
    const sched = $("#src-sched");
    syncRows(sched, x.schedule, (s) => s.key, (s) => {
      const opts = PRESETS[s.unit].includes(s.value) ? PRESETS[s.unit] : [...PRESETS[s.unit], s.value].sort((a, b) => a - b);
      const select = h("select", { class: "select", "aria-label": s.label, onchange: (e) => act("set_refresh", { values: { [s.key]: Number(e.target.value) } }) },
        ...opts.map((v) => h("option", { value: v, text: presetLabel(v, s.unit), selected: v === s.value })));
      const C = 2 * Math.PI * 12;
      const ring = h("div", { class: "ring", "data-ring": "1", "data-last": s.last || "", "data-next": s.next || "", "data-busy": s.running ? "1" : "0",
        "data-queued": s.queued ? "1" : "0", "data-waiting": s.waiting_for || "" });
      const sv = svg("svg", { viewBox: "0 0 30 30" });
      sv.append(svg("circle", { class: "bg", cx: 15, cy: 15, r: 12 }), svg("circle", { class: "fg", cx: 15, cy: 15, r: 12, "stroke-dasharray": C, "stroke-dashoffset": C }));
      ring.append(sv, h("div", { class: "lbl" }, h("b"), h("span")));
      updateRing(ring);
      return h("div", { class: "sched-row" }, h("div", null, h("h3", { text: s.label }), h("p", { text: s.help })), select, ring,
        h("button", { class: "btn sm run", onclick: (e) => act(s.action, {}, e.currentTarget) }, icon("play"), "Run now"));
    }, { animate: false });
    // Bot
    const bot = $("#src-bot");
    if (bot._v !== x.bot) {
      bot._v = x.bot;
      bot.replaceChildren(x.bot ? h("div", { style: { display: "flex", gap: "8px", alignItems: "center", flexWrap: "wrap" } },
        h("span", { class: "code", text: x.bot }),
        h("button", { class: "btn sm", onclick: async (e) => { try { await navigator.clipboard.writeText(x.bot); toast("Copied", x.bot); } catch { toast("Couldn't copy", "Select the address and copy it by hand.", true); } } }, icon("copy"), "Copy"))
        : h("p", { class: "muted", text: "Drive isn't set up yet: Nebula has no Google account configured." }),
        h("p", { class: "cell-sub", style: { marginTop: "12px" }, text: "Nebula notices new shares at the next check above. Each file stays visible only to the people Drive already shares it with." }));
    }
    $("#src-count").textContent = x.drive
      ? `${plural(x.roots.filter((r) => r.status === "active").length, "source")} · last checked ${ago(x.drive.last_synced_at)}${x.drive.sync_error ? " · last check failed" : ""}`
      : "Nothing has been shared with Nebula yet.";
    const body = $("#src-body");
    if (!x.roots.length) {
      body.replaceChildren(h("tr", null, h("td", { colspan: 9 }, empty("folder", "Nothing shared yet", "Share a Drive folder or file with Nebula's Google account and it appears here at the next check."))));
      body._rows = null;
    } else {
      if (body.querySelector(".empty")) { body.replaceChildren(); body._rows = null; }
      syncRows(body, x.roots, (r) => r.id, (r) => h("tr", null,
        h("td", null, h("div", { class: "file" }, h("span", { class: "kind-ic", title: { folder: "Folder", file: "File", drive: "Shared Drive" }[r.kind] }, icon(r.kind === "drive" ? "drive" : r.kind === "file" ? "file" : "folder")),
          h("div", { class: "meta" }, h("div", { class: "cell-main" }, h("a", { href: r.url, target: "_blank", rel: "noopener", text: r.name || "Untitled" })),
            h("div", { class: "cell-sub", text: r.added_via === "request" ? "Added through Claude" : "Found by sharing" })))),
        h("td", null, r.manager_email ? who({ name: r.manager_name, email: r.manager_email }, "sm") : h("span", { class: "cell-sub", text: r.shared_by || "—" })),
        h("td", { class: "nowrap" }, h("span", { class: "cell-sub", text: when(r.created_at) })),
        h("td", { class: "n", text: num(r.files) }), h("td", { class: "n", text: num(Math.max(0, r.files - r.waiting)) }),
        h("td", { class: "n", text: num(r.waiting) }), h("td", { class: "n", text: num(r.failed) }),
        h("td", null, r.status === "active" ? tag("Active", "good") : tag(r.status === "lost" ? "Can't open" : "Removed", "bad", { title: r.status_note }),
          r.status_note ? h("div", { class: "cell-sub clamp2", style: { marginTop: "4px", maxWidth: "240px" }, title: r.status_note, text: r.status_note }) : null),
        h("td", { class: "n" }, r.status === "active" ? h("button", { class: "btn sm", onclick: (e) => act("check_root", { root_id: r.id }, e.currentTarget) }, icon("refresh"), "Check now") : null)));
    }
  }

  // ------------------------------------------------------------------ jobs
  function renderJobs(data) {
    const root = $("#view-jobs");
    const J = S.jobs, x = data.jobs;
    if (!S.views.jobs) {
      root.replaceChildren(h("section", { class: "card" },
        h("div", { class: "filters" }, h("div", { class: "chips", id: "job-chips" }), h("span", { class: "grow" }), h("span", { class: "count", id: "job-count" })),
        h("div", { class: "tablewrap" }, h("table", { class: "t" },
          h("thead", null, h("tr", null, ...["Job", "Status", "Asked by", "Queued", "Started", "Took", "Tries", ""].map((l, i) => h("th", { class: i === 5 || i === 6 ? "n" : "", text: l })))),
          h("tbody", { id: "job-body" })))));
      S.views.jobs = true;
    }
    const c = x.counts;
    const ids = new Set(x.rows.map((j) => j.id));
    J.fresh = J.seen && J.seenStatus === J.status ? new Set([...ids].filter((k) => !J.seen.has(k))) : new Set();
    J.seen = ids; J.seenStatus = J.status;
    const chip = (v, l, n) => h("button", { class: "chip", "aria-pressed": String(J.status === v), onclick: () => { J.status = J.status === v ? "" : v; refresh(true); } },
      l, n != null ? h("span", { class: "c", text: num(n) }) : null);
    $("#job-chips").replaceChildren(chip("", "All"), chip("running", "Running", c.running), chip("queued", "Waiting", c.queued),
      chip("done", "Done", c.done), chip("failed", "Failed", c.failed));
    $("#job-count").textContent = `${plural(x.rows.length, "job")} · waiting and running, plus finished in ${RANGE_WORDS[data.range]}`;
    const body = $("#job-body");
    if (!x.rows.length) {
      body.replaceChildren(h("tr", null, h("td", { colspan: 8 }, empty("jobs", "No jobs", J.status ? "None with this status." : `Nothing ran in ${RANGE_WORDS[data.range]}.`))));
      body._rows = null; return;
    }
    if (body.querySelector(".empty")) { body.replaceChildren(); body._rows = null; }
    syncRows(body, x.rows, (j) => j.id, (j) => {
      const pct = j.progress != null ? Math.round(j.progress * 100) : null;
      const delayed = j.status === "queued" && t(j.run_after) > now() + 2000;
      const status = {
        running: () => [tag("Running", "blue", { spin: true }), h("div", { class: `prog ${pct == null ? "indet" : ""}`, style: { marginTop: "6px" } }, h("i", { style: { width: `${pct ?? 30}%` } })),
          j.message ? h("div", { class: "cell-sub", style: { marginTop: "4px" }, text: j.message }) : null],
        queued: () => [tag(delayed ? "Scheduled" : "Waiting", ""), h("div", { class: "cell-sub", style: { marginTop: "4px" }, text: delayed ? `starts ${ago(j.run_after)}` : `usually takes ${dur(j.expected * 1000)}` })],
        done: () => [tag("Done", "good"), j.result ? h("div", { class: "cell-sub clip", style: { marginTop: "4px", maxWidth: "280px" }, text: resultText(j) }) : null],
        failed: () => [tag("Failed", "bad"), h("div", { class: "cell-sub", style: { marginTop: "4px", color: "var(--bad-ink)", maxWidth: "320px" }, text: j.error })],
        cancelled: () => [tag("Cancelled", "")],
      }[j.status] || (() => [tag(j.status, "")]);
      return h("tr", null,
        h("td", null, h("div", { class: "cell-main", text: j.label }), h("div", { class: "cell-sub clip", text: j.target || "" })),
        h("td", null, ...status()),
        h("td", null, j.person ? who(j.person, "sm") : h("span", { class: "cell-sub", text: "Scheduled by Nebula" })),
        h("td", { class: "nowrap" }, h("span", { class: "cell-sub", text: when(j.created_at) })),
        h("td", { class: "nowrap" }, h("span", { class: "cell-sub", text: j.started_at ? when(j.started_at) : "—" })),
        h("td", { class: "n", text: j.took != null ? dur(j.took * 1000) : j.status === "running" && j.started_at ? "…" : "—" }),
        h("td", { class: "n", text: `${j.attempts}` }),
        h("td", { class: "n" }, j.status === "failed" ? h("button", { class: "btn sm", onclick: (e) => act("retry_job", { job_id: j.id }, e.currentTarget) }, icon("refresh"), "Retry")
          : j.status === "queued" ? h("button", { class: "btn sm", onclick: (e) => act("cancel_job", { job_id: j.id }, e.currentTarget) }, icon("x"), "Cancel") : null));
    }, { fresh: J.fresh });
  }
  function resultText(j) {
    const r = j.result || {};
    if (j.kind === "sync") return [r.changed ? `${num(r.changed)} read` : null, r.deleted ? `${num(r.deleted)} removed` : null, r.unchanged ? `${num(r.unchanged)} unchanged` : null, r.errors ? `${num(r.errors)} failed` : null].filter(Boolean).join(" · ") || "No changes";
    if (j.kind === "embed") return `${num(r.embedded || 0)} pieces made searchable`;
    if (j.kind === "sync_item") return r.title ? `${r.title}${r.version ? ` · v${r.version}` : ""}` : (r.note || "");
    if (j.kind === "drive_discover") return r.new != null ? `${num(r.new)} new` : JSON.stringify(r).slice(0, 120);
    return JSON.stringify(r).slice(0, 120);
  }

  const RENDER = { overview: renderOverview, people: renderPeople, activity: renderActivity, indexing: renderIndexing, sources: renderSources, jobs: renderJobs };

  // ------------------------------------------------------------------ boot
  $$(".tabs button").forEach((b) => b.addEventListener("click", () => setTab(b.dataset.tab, true)));
  $$("#range button").forEach((b) => b.addEventListener("click", () => setRange(b.dataset.range)));
  $$("[data-close]").forEach((b) => b.addEventListener("click", closeDrawer));
  addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });
  addEventListener("hashchange", () => { const h_ = location.hash.slice(1); if (h_ in TABS && h_ !== S.tab) setTab(h_, false); });
  addEventListener("resize", () => {
    placeInk($(".tabs"), $(`.tabs button[data-tab="${S.tab}"]`), $(".tab-ink"));
    placeInk($("#range"), $(`#range button[data-range="${S.range}"]`), $(".range-ink"));
  });
  document.addEventListener("visibilitychange", () => { setLive(); if (!document.hidden) refresh(true); });
  if (!(S.range in RANGE_WORDS)) S.range = "24h";
  $$("#range button").forEach((b) => b.setAttribute("aria-checked", String(b.dataset.range === S.range)));
  setTab(S.tab, false);
  requestAnimationFrame(() => placeInk($("#range"), $(`#range button[data-range="${S.range}"]`), $(".range-ink")));
  if (document.fonts) document.fonts.ready.then(() => {
    placeInk($(".tabs"), $(`.tabs button[data-tab="${S.tab}"]`), $(".tab-ink"));
    placeInk($("#range"), $(`#range button[data-range="${S.range}"]`), $(".range-ink"));
  });
  tick();
  setInterval(tick, 1000);
})();

/* Nebula "My files" page. Plain JavaScript, no build step.
   Data comes from /me/api/state; every value from the server is inserted as text, never as HTML. */
(() => {
  "use strict";
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));
  const SVGNS = "http://www.w3.org/2000/svg";

  function h(tag, props, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (v == null || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "text") el.textContent = v;
      else if (k === "style") Object.assign(el.style, v);
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat()) {
      if (kid == null || kid === false) continue;
      el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    }
    return el;
  }
  function icon(id) {
    const s = document.createElementNS(SVGNS, "svg");
    s.setAttribute("aria-hidden", "true");
    const u = document.createElementNS(SVGNS, "use");
    u.setAttribute("href", "#i-" + id);
    s.append(u);
    return s;
  }

  const TZ = "Asia/Kolkata";
  const fTime = new Intl.DateTimeFormat("en-GB", { timeZone: TZ, hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
  const fHM = new Intl.DateTimeFormat("en-GB", { timeZone: TZ, hour: "2-digit", minute: "2-digit", hour12: false });
  const fDay = new Intl.DateTimeFormat("en-GB", { timeZone: TZ, weekday: "short", day: "numeric", month: "short" });
  const fDate = new Intl.DateTimeFormat("en-GB", { timeZone: TZ, day: "numeric", month: "short" });
  const fDayKey = new Intl.DateTimeFormat("en-CA", { timeZone: TZ, year: "numeric", month: "2-digit", day: "2-digit" });
  const nf = new Intl.NumberFormat("en-IN");
  const REDUCED = matchMedia("(prefers-reduced-motion: reduce)").matches;

  const S = { tab: location.hash.slice(1) === "shared" ? "shared" : "mine", offset: 0, timer: null, busy: false, lastOk: 0,
    failing: false, data: null, filter: { mine: { state: "", q: "" }, shared: { state: "", q: "" } }, seen: {}, built: {} };
  const now = () => Date.now() + S.offset;
  const t = (iso) => (iso ? Date.parse(iso) : NaN);
  const isToday = (ms) => fDayKey.format(ms) === fDayKey.format(now());
  const num = (n) => (n == null ? "—" : nf.format(Math.round(n)));
  const plural = (n, one, many) => `${num(n)} ${n === 1 ? one : (many || one + "s")}`;
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
  function dur(ms) {
    if (ms == null) return "";
    if (ms < 1000) return `${Math.round(ms)} ms`;
    if (ms < 60000) return `${(ms / 1000).toFixed(ms < 10000 ? 1 : 0)} s`;
    return `${Math.floor(ms / 60000)} min ${Math.round((ms % 60000) / 1000)} s`;
  }
  function tag(text, kind, opts = {}) {
    return h("span", { class: `tag ${kind || ""}`, title: opts.title },
      opts.spin ? h("span", { class: "spin" }) : (opts.dot === false ? null : h("span", { class: "dot" })), text);
  }
  const empty = (iconId, title, text) => h("div", { class: "empty" }, icon(iconId), h("b", { text: title }), text ? h("span", { text }) : null);
  function card(title, sub, body, actions) {
    return h("section", { class: "card" },
      h("div", { class: "card-head" }, h("div", null, h("h2", { text: title }), sub ? (sub instanceof Node ? sub : h("p", { text: sub })) : null),
        actions ? h("div", { class: "actions" }, actions) : null), body);
  }
  function tween(el, to) {
    const from = el._v == null ? 0 : el._v;
    el._v = to;
    if (from === to || REDUCED) { el.textContent = num(to); return; }
    const start = performance.now();
    const step = (ts) => {
      const k = Math.min(1, (ts - start) / 600);
      el.textContent = num(from + (to - from) * (1 - Math.pow(1 - k, 3)));
      if (k < 1 && el._v === to) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }
  function syncRows(container, items, keyOf, build, fresh) {
    const prev = container._rows || new Map();
    const next = new Map();
    let anchor = null;
    for (const item of items) {
      const key = keyOf(item);
      const sig = JSON.stringify(item);
      let row = prev.get(key);
      if (!row || row._sig !== sig) {
        const made = build(item);
        if (row) row.replaceWith(made);
        else if (fresh && fresh.has(key)) made.classList.add("new");
        row = made;
        row._sig = sig;
      }
      next.set(key, row);
      const want = anchor ? anchor.nextSibling : container.firstChild;
      if (row !== want) container.insertBefore(row, want);
      anchor = row;
    }
    for (const [k, row] of prev) if (!next.has(k)) row.remove();
    container._rows = next;
  }
  function toast(title, text, bad) {
    const el = h("div", { class: `toast ${bad ? "bad" : ""}` }, h("span", { class: "ic" }, icon(bad ? "x" : "check")),
      h("div", null, h("b", { text: title }), text ? h("span", { text }) : null));
    $("#toasts").append(el);
    setTimeout(() => { el.classList.add("out"); setTimeout(() => el.remove(), 320); }, bad ? 6000 : 3200);
  }
  async function act(action, body, btn) {
    const label = btn ? btn.innerHTML : "";
    if (btn) { btn.classList.add("busy"); btn.replaceChildren(h("span", { class: "spin" })); }
    try {
      const res = await fetch("/me/api/action", { method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", Accept: "application/json" }, body: JSON.stringify({ action, ...(body || {}) }) });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
      toast(data.message || "Done", "This page shows the progress as it happens.");
      refresh(true);
      return data;
    } catch (e) {
      toast("That didn't work", e.message, true);
      return null;
    } finally {
      if (btn) { btn.classList.remove("busy"); btn.innerHTML = label; }
    }
  }

  // ------------------------------------------------------------------ states
  const STATES = [["queued", "Waiting to be read"], ["reading", "Reading now"], ["indexing", "Making searchable"],
    ["ready", "Searchable"], ["no_text", "No text found"], ["failed", "Couldn't read"]];
  function stateCell(f) {
    const tagEl = {
      queued: () => tag("Waiting to be read", ""), reading: () => tag("Reading now", "blue", { spin: true }),
      indexing: () => tag("Making searchable", "blue", { spin: true }), ready: () => tag("Searchable", "good"),
      no_text: () => tag("No text found", "warn"), failed: () => tag("Couldn't read", "bad"),
    }[f.state]();
    const lines = [];
    if (f.state === "reading" && f.job && f.job.message) lines.push(f.job.message);
    if (f.state === "queued" || f.state === "indexing") {
      const bits = [f.wait ? `Ready in ${f.wait}` : null, f.ahead].filter(Boolean);
      if (bits.length) lines.push(bits.join(" · "));
    }
    if (f.state === "no_text") lines.push("Nebula read the file but found no text. If it's a scan or pictures, ask Claude to read it as a picture.");
    if (f.state === "failed" && f.status_note) lines.push(f.status_note);
    const sub = lines.map((l, i) => h("div", { class: "cell-sub clamp2", style: { marginTop: "4px", color: f.state === "failed" && i === lines.length - 1 ? "var(--bad-ink)" : null }, text: l }));
    const gaps = (f.gaps || []).length ? h("div", { style: { marginTop: "6px" } },
      ...f.gaps.slice(0, 3).map((g) => h("div", { class: "cell-sub", style: { color: "var(--warn-ink)" }, text: `${g.where}: ${g.why}` })),
      h("div", { class: "cell-sub", text: "Ask Claude to read these parts as pictures; it can save what they say." })) : null;
    return h("td", { style: { maxWidth: "300px" } }, tagEl, ...sub, gaps);
  }
  function pieces(f) {
    if (!f.pieces && f.tables) return h("span", { class: "cell-sub", text: `${plural(f.tables, "table")}, ready for number questions` });
    if (!f.pieces) return h("span", { class: "faint", text: "—" });
    const pct = (f.ready / f.pieces) * 100;
    return h("div", { class: "pieces" }, h("div", { class: `prog ${pct >= 100 ? "good" : ""}` }, h("i", { style: { width: `${pct}%` } })),
      h("span", { class: "cell-sub", text: `${num(f.ready)} of ${num(f.pieces)} searchable` }));
  }
  function fileCell(f) {
    return h("td", null, h("div", { class: "file" }, h("span", { class: `fmt ${f.format}`, text: (f.format || "file").slice(0, 4) }),
      h("div", { class: "meta" }, h("div", { class: "cell-main", title: f.title }, f.url ? h("a", { href: f.url, target: "_blank", rel: "noopener", text: f.title }) : f.title),
        h("div", { class: "cell-sub", text: f.path || "" }))));
  }
  function whoCell(p) {
    if (!p) return h("span", { class: "faint", text: "—" });
    const name = p.name || p.email.split("@")[0];
    let x = 0;
    for (const c of p.email) x = (x * 31 + c.charCodeAt(0)) >>> 0;
    const AV = [["#e8f1fc", "#1c5cab"], ["#fdeee6", "#b84a1d"], ["#e3f6ee", "#0f7a54"], ["#fdf3dc", "#8a5a00"], ["#fcedf3", "#b0306b"], ["#ecebf7", "#4a3aa7"]];
    const [bg, fg] = AV[x % AV.length];
    const ini = name.split(/[\s._-]+/).filter(Boolean).map((w) => w[0]).slice(0, 2).join("").toUpperCase();
    return h("div", { class: "who" }, h("span", { class: "avatar sm", style: { background: bg, color: fg }, text: ini }),
      h("div", { class: "meta" }, h("div", { class: "cell-main", text: name }), h("div", { class: "cell-sub", text: p.email })));
  }
  function summaryTiles(host, counts, total, gaps, label) {
    const inProgress = counts.queued + counts.reading + counts.indexing;
    const tiles = [
      [label, total, total ? `${plural(counts.ready, "file")} searchable now.` : "Nothing yet."],
      ["Searchable", counts.ready, total ? `${Math.round((counts.ready / total) * 100)}% of ${plural(total, "file")}.` : ""],
      ["In progress", inProgress, inProgress ? "Being read or made searchable. Times are shown per file." : "Nothing waiting."],
      ["Couldn't read", counts.failed + counts.no_text, gaps ? `${plural(gaps, "file")} with parts Nebula couldn't read.` : "Everything else was read in full."],
    ];
    if (!host.children.length) host.append(...tiles.map(() => h("section", { class: "card tile", style: { minHeight: "118px" } },
      h("div", { class: "tile-label" }), h("div", { class: "tile-value" }), h("div", { class: "tile-sub" }))));
    tiles.forEach(([l, v, sub], i) => {
      const el = host.children[i];
      $(".tile-label", el).textContent = l;
      tween($(".tile-value", el), v);
      $(".tile-sub", el).textContent = sub;
    });
  }
  function stateChips(host, files, which) {
    const F = S.filter[which];
    const counts = {};
    for (const f of files) counts[f.state] = (counts[f.state] || 0) + 1;
    const chip = (v, l, c) => h("button", { class: "chip", "aria-pressed": String(F.state === v),
      onclick: () => { F.state = F.state === v ? "" : v; draw(); } }, l, h("span", { class: "c", text: num(c) }));
    host.replaceChildren(chip("", "All", files.length), ...STATES.filter(([k]) => counts[k]).map(([k, l]) => chip(k, l, counts[k])));
  }
  function filtered(files, which) {
    const F = S.filter[which];
    return files.filter((f) => (!F.state || f.state === F.state) &&
      (!F.q || `${f.title} ${f.path} ${f.source} ${(f.shared_by || {}).name || ""} ${(f.shared_by || {}).email || ""}`.toLowerCase().includes(F.q)));
  }
  function freshKeys(which, files) {
    const ids = new Set(files.map((f) => f.id));
    const fresh = S.seen[which] ? new Set([...ids].filter((k) => !S.seen[which].has(k))) : new Set();
    S.seen[which] = ids;
    return fresh;
  }

  // ------------------------------------------------------------------ shared by you
  function buildMine() {
    const root = $("#view-mine");
    const add = h("form", { class: "linkbox", onsubmit: (e) => { e.preventDefault(); const i = $("#add-link"); act("add_link", { link: i.value }, $("#add-btn")).then((r) => { if (r) i.value = ""; }); } },
      h("input", { class: "input", id: "add-link", type: "url", required: true, placeholder: "Paste a Google Drive link to a folder or file…", "aria-label": "Drive link" }),
      h("button", { class: "btn primary", id: "add-btn", type: "submit" }, icon("refresh"), "Add or refresh"));
    root.replaceChildren(
      h("div", { class: "grid g-4", id: "mine-tiles" }),
      h("div", { class: "grid g-2", style: { marginTop: "var(--gap)" } },
        card("Add something from Drive", "Share it with Nebula's Google account as an Editor, or paste its link here once it's shared.",
          h("div", null, add, h("div", { class: "botline", id: "bot-line" }))),
        card("What Nebula is doing", "Live. Files you ask for by link go to the front of the queue.", h("div", { class: "card-body", id: "mine-busy" }))),
      h("section", { class: "card", style: { marginTop: "var(--gap)" } },
        h("div", { class: "card-head" }, h("div", null, h("h2", { text: "What you've shared with Nebula" }), h("p", { id: "roots-sub" }))),
        h("div", { class: "card-body flush" }, h("div", { class: "tablewrap" }, h("table", { class: "t" },
          h("thead", null, h("tr", null, ...["Source", "Files", "Progress", "Last checked", ""].map((l, i) => h("th", { class: i === 1 ? "n" : "", text: l })))),
          h("tbody", { id: "roots-body" }))))),
      h("section", { class: "card", style: { marginTop: "var(--gap)" } },
        h("div", { class: "filters" }, h("input", { class: "input search", type: "search", placeholder: "Find a file…", "aria-label": "Find a file",
          oninput: (e) => { S.filter.mine.q = e.target.value.trim().toLowerCase(); draw(); } }), h("span", { class: "grow" }), h("span", { class: "count", id: "mine-count" })),
        h("div", { class: "filters", style: { paddingTop: "12px", paddingBottom: "12px" } }, h("div", { class: "chips", id: "mine-chips" })),
        h("div", { class: "tablewrap" }, h("table", { class: "t" },
          h("thead", null, h("tr", null, ...["File", "Source", "Where it is", "Search pieces", "Read", "Changed in Drive", ""].map((l) => h("th", { text: l })))),
          h("tbody", { id: "mine-body" })))),
      h("div", { style: { marginTop: "var(--gap)" } }, card("What's happened to your files", "Every time Nebula read, removed or failed to read one of them.",
        h("div", { class: "card-body flush" }, h("div", { class: "feed compact", id: "mine-log", style: { maxHeight: "360px", overflowY: "auto" } })))));
  }
  function renderMine(d) {
    if (!S.built.mine) { buildMine(); S.built.mine = true; }
    const m = d.mine;
    const gaps = m.files.filter((f) => f.gaps && f.gaps.length).length;
    summaryTiles($("#mine-tiles"), m.counts, m.files.length, gaps, "Your files in Nebula");
    const bot = $("#bot-line");
    if (bot._v !== d.bot) {
      bot._v = d.bot;
      bot.replaceChildren(...(d.bot ? [h("span", { text: "Nebula's Google account:" }), h("span", { class: "code", text: d.bot }),
        h("button", { class: "btn sm", onclick: async () => { try { await navigator.clipboard.writeText(d.bot); toast("Copied", d.bot); } catch { toast("Couldn't copy", "Select the address and copy it.", true); } } }, icon("copy"), "Copy")]
        : [h("span", { text: "Drive isn't set up on this Nebula yet." })]));
    }
    const p = d.pulse, busy = $("#mine-busy");
    const job = p.worker_job;
    const lines = [
      h("div", { class: "act", style: { display: "flex", gap: "8px", alignItems: "center", fontWeight: 550 } },
        job ? h("span", { class: "spin", style: { color: "var(--accent)" } }) : icon("check"), job ? `Busy: ${job.label.toLowerCase()}` : "Idle and up to date"),
      h("p", { class: "cell-sub", style: { margin: "6px 0 0" }, text: `${plural(p.jobs_queued, "job")} waiting · ${plural(p.pieces_waiting, "piece")} of text, from everyone's files, still to be made searchable.` }),
    ];
    busy.replaceChildren(...lines);
    // Sources
    const rootsBody = $("#roots-body");
    $("#roots-sub").textContent = m.roots.length ? plural(m.roots.filter((r) => r.status === "active").length, "source") : "";
    if (!m.roots.length) {
      rootsBody.replaceChildren(h("tr", null, h("td", { colspan: 5 }, empty("folder", "You haven't shared anything with Nebula yet",
        "Share a Drive folder or file with Nebula's Google account (above) as an Editor. It shows up here within a couple of minutes."))));
      rootsBody._rows = null;
    } else {
      if (rootsBody.querySelector(".empty")) { rootsBody.replaceChildren(); rootsBody._rows = null; }
      syncRows(rootsBody, m.roots, (r) => r.id, (r) => {
        const c = { queued: 0, reading: 0, indexing: 0, ready: 0, no_text: 0, failed: 0, ...r.counts };
        const total = Object.values(c).reduce((a, b) => a + b, 0);
        const bar = h("div", { class: "statebar", style: { height: "8px", minWidth: "160px" } },
          ...STATES.map(([k]) => h("i", { class: `st-${k}`, style: { flexBasis: total ? `${(c[k] / total) * 100}%` : "0%" } })));
        const inProgress = c.queued + c.reading + c.indexing;
        const words = [c.ready ? `${num(c.ready)} searchable` : null, inProgress ? `${num(inProgress)} in progress` : null,
          c.failed + c.no_text ? `${num(c.failed + c.no_text)} couldn't be read` : null].filter(Boolean).join(" · ") || "No files yet";
        const kindIcon = { folder: "folder", file: "file", drive: "drive" }[r.kind] || "folder";
        return h("tr", null,
          h("td", null, h("div", { class: "file" }, h("span", { class: "kind-ic" }, icon(kindIcon)),
            h("div", { class: "meta" }, h("div", { class: "cell-main" }, r.url ? h("a", { href: r.url, target: "_blank", rel: "noopener", text: r.name }) : r.name),
              h("div", { class: "cell-sub", text: r.status === "active" ? `Shared ${when(r.added)}` : r.status_note || "Nebula can't open this any more" })))),
          h("td", { class: "n", text: num(total) }),
          h("td", null, h("div", { style: { display: "grid", gap: "6px" } }, bar, h("span", { class: "cell-sub", text: words }))),
          h("td", { class: "nowrap" }, r.last_checked ? h("span", { class: "cell-sub", "data-ago": r.last_checked, text: ago(r.last_checked) }) : h("span", { class: "faint", text: "—" })),
          h("td", { class: "n" }, r.status === "active" ? h("button", { class: "btn sm", onclick: (e) => act("check_root", { root_id: r.id }, e.currentTarget) }, icon("refresh"), "Check now")
            : r.status !== "active" ? tag("Can't open", "bad") : null));
      });
    }
    // Log
    const log = $("#mine-log");
    const label = { read: "Read", searchable: "Now searchable", removed: "Removed", failed: "Couldn't read" };
    if (!m.events.length) { log.replaceChildren(empty("indexing", "Nothing yet", "Each file Nebula reads or makes searchable appears here.")); log._rows = null; }
    else {
      if (log.querySelector(".empty")) log.replaceChildren();
      syncRows(log, m.events, (e) => e.id, (e) => h("div", { class: `feed-row ${e.event === "failed" ? "fail" : ""}` },
        h("div", { class: "feed-time", text: fHM.format(t(e.at)) }),
        h("div", { class: "feed-what", style: { paddingTop: 0 } }, h("div", { class: "act" }, icon(e.event === "failed" ? "alert" : e.event === "searchable" ? "check" : "indexing"), `${label[e.event]} · ${e.title}`),
          e.detail ? h("div", { class: `txt ${e.event === "failed" ? "" : ""}`, style: e.event === "failed" ? { color: "var(--bad-ink)" } : null, text: e.detail }) : null),
        h("div", { class: "feed-ms", text: e.ms != null ? dur(e.ms) : "" })));
    }
    const cnt = $("#count-mine");
    cnt.hidden = !m.files.length; cnt.textContent = num(m.files.length);
  }
  function drawMineFiles() {
    const m = S.data.mine;
    stateChips($("#mine-chips"), m.files, "mine");
    const files = filtered(m.files, "mine");
    $("#mine-count").textContent = plural(files.length, "file");
    const body = $("#mine-body");
    if (!files.length) {
      body.replaceChildren(h("tr", null, h("td", { colspan: 7 }, empty("indexing", m.files.length ? "No files match" : "No files yet",
        m.files.length ? "Try another filter." : "Once you share something with Nebula, each file appears here with its progress."))));
      body._rows = null; return;
    }
    if (body.querySelector(".empty")) { body.replaceChildren(); body._rows = null; }
    syncRows(body, files, (f) => f.id, (f) => h("tr", null, fileCell(f),
      h("td", null, h("div", { class: "cell-sub clip", style: { maxWidth: "180px" }, text: f.source })),
      stateCell(f), h("td", null, pieces(f)),
      h("td", { class: "nowrap" }, f.indexed_at ? h("div", null, h("div", { class: "cell-main", "data-ago": f.indexed_at, text: ago(f.indexed_at) }), h("div", { class: "cell-sub", text: when(f.indexed_at) })) : h("span", { class: "faint", text: "Not yet" })),
      h("td", { class: "nowrap" }, f.modified_in_source ? h("span", { class: "cell-sub", text: when(f.modified_in_source) }) : h("span", { class: "faint", text: "—" })),
      h("td", { class: "n" }, h("button", { class: "icon-btn bordered", title: "Re-read this file now", "aria-label": `Re-read ${f.title} now`,
        onclick: (e) => act("reread_item", { item_id: f.id }, e.currentTarget) }, icon("refresh")))), S.freshMine);
  }

  // ------------------------------------------------------------------ shared with you
  function buildShared() {
    $("#view-shared").replaceChildren(
      h("div", { class: "grid g-4", id: "shared-tiles" }),
      h("section", { class: "card", style: { marginTop: "var(--gap)" } },
        h("div", { class: "filters" }, h("input", { class: "input search", type: "search", placeholder: "Find a file or a person…", "aria-label": "Find a file",
          oninput: (e) => { S.filter.shared.q = e.target.value.trim().toLowerCase(); draw(); } }), h("span", { class: "grow" }), h("span", { class: "count", id: "shared-count" })),
        h("div", { class: "filters", style: { paddingTop: "12px", paddingBottom: "12px" } }, h("div", { class: "chips", id: "shared-chips" })),
        h("div", { class: "tablewrap" }, h("table", { class: "t" },
          h("thead", null, h("tr", null, ...["File", "Shared by", "Shared with", "Where it is", "Search pieces", "Changed in Drive"].map((l) => h("th", { text: l })))),
          h("tbody", { id: "shared-body" })))));
  }
  function renderShared(d) {
    if (!S.built.shared) { buildShared(); S.built.shared = true; }
    const x = d.shared;
    summaryTiles($("#shared-tiles"), x.counts, x.files.length, 0, "Shared with you");
    const cnt = $("#count-shared");
    cnt.hidden = !x.files.length; cnt.textContent = num(x.files.length);
  }
  function viaText(v) {
    if (!v) return "—";
    if (v === "you") return "You, directly";
    const t_ = v.replace(/(^|, )you(?=,|$)/, "$1you directly");
    return t_.charAt(0).toUpperCase() + t_.slice(1);
  }
  function drawSharedFiles() {
    const x = S.data.shared;
    stateChips($("#shared-chips"), x.files, "shared");
    const files = filtered(x.files, "shared");
    $("#shared-count").textContent = plural(files.length, "file");
    const body = $("#shared-body");
    if (!files.length) {
      body.replaceChildren(h("tr", null, h("td", { colspan: 6 }, empty("people", x.files.length ? "No files match" : "Nothing shared with you yet",
        x.files.length ? "Try another filter." : "When someone shares a Drive file with Nebula that Drive lets you open, it appears here."))));
      body._rows = null; return;
    }
    if (body.querySelector(".empty")) { body.replaceChildren(); body._rows = null; }
    syncRows(body, files, (f) => f.id, (f) => h("tr", null, fileCell(f), h("td", null, whoCell(f.shared_by)),
      h("td", null, h("span", { class: "cell-sub", text: viaText(f.shared_via) })),
      stateCell(f), h("td", null, pieces(f)),
      h("td", { class: "nowrap" }, f.modified_in_source ? h("span", { class: "cell-sub", text: when(f.modified_in_source) }) : h("span", { class: "faint", text: "—" }))), S.freshShared);
  }

  // ------------------------------------------------------------------ chrome & loop
  const TABS = {
    mine: ["Shared by you", "Everything you've shared with Nebula, and where each file is in being read and made searchable."],
    shared: ["Shared with you", "Files other people shared with Nebula that Drive or Nebula lets you open, and where each one is."],
  };
  function placeInk() {
    const nav = $(".tabs"), btn = $(`.tabs button[data-tab="${S.tab}"]`), ink = $(".tab-ink");
    if (!btn) return;
    const c = nav.getBoundingClientRect(), b = btn.getBoundingClientRect();
    ink.style.width = `${b.width - 20}px`;
    ink.style.transform = `translateX(${b.left - c.left + nav.scrollLeft + 10}px)`;
  }
  function setTab(tab) {
    S.tab = tab;
    history.replaceState(null, "", `#${tab}`);
    $$(".tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === tab)));
    $$(".view").forEach((v) => v.classList.toggle("on", v.dataset.view === tab));
    $("#page-title").textContent = TABS[tab][0];
    $("#page-sub").textContent = TABS[tab][1];
    placeInk();
    draw();
  }
  function draw() {
    if (!S.data) return;
    if (S.tab === "mine") drawMineFiles(); else drawSharedFiles();
  }
  function pulse(p) {
    const pill = $("#worker-pill"), label = pill.querySelector("span");
    const age = p.worker_seen ? (now() - t(p.worker_seen)) / 1000 : Infinity;
    if (age < 45 && p.worker_job) { pill.className = "pill ok"; label.textContent = `Nebula is busy · ${p.worker_job.label.toLowerCase()}`; }
    else if (age < 45) { pill.className = "pill ok"; label.textContent = "Nebula is up to date"; }
    else { pill.className = "pill warn"; label.textContent = "Nebula's background work is paused"; }
  }
  function setLive() {
    const pill = $("#live-pill"), label = pill.querySelector("span");
    if (document.hidden) { pill.className = "pill warn"; label.textContent = "Paused"; return; }
    if (S.failing) { pill.className = "pill bad"; label.textContent = "Reconnecting"; return; }
    pill.className = S.lastOk ? "pill ok" : "pill"; label.textContent = "Live";
  }
  async function refresh(force) {
    clearTimeout(S.timer);
    if (document.hidden) { setLive(); return; }
    if (S.busy && !force) { S.timer = setTimeout(refresh, 800); return; }
    S.busy = true;
    try {
      const res = await fetch("/me/api/state", { credentials: "same-origin", headers: { Accept: "application/json" } });
      if (res.status === 403) { location.href = "/login?next=/me"; return; }
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const d = await res.json();
      S.offset = Date.parse(d.now) - Date.now();
      S.lastOk = Date.now(); S.failing = false; S.data = d;
      S.freshMine = freshKeys("mine", d.mine.files);
      S.freshShared = freshKeys("shared", d.shared.files);
      pulse(d.pulse);
      renderMine(d);
      renderShared(d);
      draw();
      $("#foot-me").textContent = `Signed in as ${d.me.email}`;
      $("#foot-updated").textContent = `Updated ${fTime.format(now())} IST`;
      $("#admin-link").hidden = !d.me.admin; $("#admin-sep").hidden = !d.me.admin;
    } catch {
      S.failing = true;
    } finally {
      S.busy = false;
      setLive();
      S.timer = setTimeout(refresh, 4000);
    }
  }
  function tick() {
    const n = now();
    $("#clock-time").textContent = fTime.format(n);
    $("#clock-date").textContent = `${fDay.format(n)} · IST`;
    for (const el of $$("[data-ago]")) el.textContent = ago(el.dataset.ago);
    if (S.lastOk && Date.now() - S.lastOk > 20000 && !document.hidden) { S.failing = true; setLive(); }
  }
  $$(".tabs button").forEach((b) => b.addEventListener("click", () => setTab(b.dataset.tab)));
  addEventListener("hashchange", () => { const x = location.hash.slice(1); if ((x === "mine" || x === "shared") && x !== S.tab) setTab(x); });
  addEventListener("resize", placeInk);
  document.addEventListener("visibilitychange", () => { setLive(); if (!document.hidden) refresh(true); });
  if (document.fonts) document.fonts.ready.then(placeInk);
  setTab(S.tab);
  tick();
  setInterval(tick, 1000);
  refresh(true);
})();

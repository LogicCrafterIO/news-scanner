/* ============================================================ tiny helpers */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[c]));

const state = {
  settings: {},
  positions: [],
  sectors: [],
  topics: [],
  quotes: {},
  etfs: {},
  counts: {},
  topicCounts: {},
  scanner: {},
  presets: {},
  gicsRows: 0,
  db: {},
  items: [],
  itemIndex: new Map(),
  filters: { scope: "all", sector: null, ticker: null, topic: null, q: "", minRel: 0, sort: "smart", hideMarket: false, hours: 48 },
  nextScanAt: null,
  feedLoading: false,
  // technical scanner
  technicals: {},            // light snapshots keyed by ticker (from /api/state + SSE)
  techSummary: {},
  techAt: null,
  techRows: [],              // full snapshots (from /api/technicals)
  techFull: new Map(),       // ticker -> full snapshot
  techLoading: false,
  techLoadedAt: 0,
  view: "news",
  techFilter: { side: "all", conflicts: false, sort: "book", q: "" },
  // macro desk (yields, curve, credit, commodities, economy, calendar)
  macro: null,
  macroAt: null,
};

const SECTOR_COLOR = {};
const SECTOR_NAME = {};
const TOPIC_NAME = {};

function ago(iso) {
  if (!iso) return "";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "";
  const s = Math.max(1, Math.round((Date.now() - t) / 1000));
  if (s < 60) return `${s}s ago`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.round(h / 24)}d ago`;
}

// absolute publish stamp, e.g. "19 Oct 14:32" (year added when not this one)
function stamp(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const sameYear = d.getFullYear() === new Date().getFullYear();
  const date = d.toLocaleDateString([], sameYear
    ? { day: "2-digit", month: "short" }
    : { day: "2-digit", month: "short", year: "numeric" });
  // some sources only give a date — don't invent a midnight time for them
  const dateOnly = d.getUTCHours() === 0 && d.getUTCMinutes() === 0 && d.getUTCSeconds() < 5;
  return dateOnly ? date : `${date} ${d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
}

function fmtPrice(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return v >= 1000 ? v.toFixed(0) : v.toFixed(2);
}
function fmtPct(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return "";
  return `${v >= 0 ? "+" : ""}${v.toFixed(2)}%`;
}
function dirClass(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return "flat";
  return v > 0 ? "up" : v < 0 ? "down" : "flat";
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!res.ok) {
    let detail = "";
    try { detail = (await res.json()).detail || ""; } catch { /* ignore */ }
    throw new Error(`${res.status} ${detail}`.trim());
  }
  return res.json();
}

/* ============================================================ boot */
async function boot() {
  try {
    const s = await api("/api/state");
    state.settings = s.settings || {};
    state.positions = s.positions || [];
    state.sectors = s.sectors || [];
    state.topics = s.topics || [];
    state.counts = s.counts || {};
    state.topicCounts = s.topic_counts || {};
    state.etfs = s.etfs || {};
    state.scanner = s.scanner || {};
    state.presets = s.presets || {};
    state.gicsRows = s.gics_index_rows || 0;
    state.db = s.db || {};
    state.scanner = s.scanner || {};
    state.quotes = { ...(s.quotes || {}) };
    state.technicals = s.technicals || {};
    state.techSummary = s.tech_summary || {};
    state.techAt = s.tech_at || null;
    state.macro = s.macro || null;
    state.macroAt = s.macro_at || null;
    (s.positions || []).forEach((p) => { if (p.quote) state.quotes[p.ticker] = p.quote; });

    state.sectors.forEach((x) => { SECTOR_COLOR[x.key] = x.color; SECTOR_NAME[x.key] = x.name; });
    state.topics.forEach((t) => { TOPIC_NAME[t.key] = t.name; });

    syncControls();
    renderPresets();
    renderLists();
    renderSectors();
    renderTopics();
    renderTape();
    renderBrief();
    renderMacro();
    renderStatus();
    renderSourceStrip();
    renderTechView();
    await loadFeed();
    connectStream();
    startTickerClock();
  } catch (err) {
    console.error(err);
    $("#status-main").textContent = "error";
    $("#status-sub").textContent = String(err.message || err).slice(0, 60);
  }
}

/* ============================================================ topbar */
function syncControls() {
  const interval = String(state.settings.scan_interval_seconds || "180");
  const sel = $("#interval-select");
  if (![...sel.options].some((o) => o.value === interval)) {
    const o = document.createElement("option");
    o.value = interval; o.textContent = `${Math.round(+interval / 60)}m`;
    sel.appendChild(o);
  }
  sel.value = interval;
  $("#auto-refresh").checked = state.settings.auto_refresh === "1";
  $("#min-rel").value = state.filters.minRel;
  $("#min-rel-val").textContent = state.filters.minRel;
}

function renderStatus() {
  const scanning = state.scanner.scanning;
  const status = $("#status");
  status.classList.toggle("scanning", !!scanning);
  status.classList.toggle("ok", !scanning && !!state.scanner.last_scan_at);
  status.classList.toggle("err", !scanning && !!state.scanner.last_stats?.error);
  $("#btn-scan").disabled = !!scanning;
  $("#btn-scan").classList.toggle("scanning", !!scanning);

  if (scanning) {
    $("#status-main").textContent = "scanning";
    $("#status-sub").textContent = state.scanner.progress?.message?.slice(0, 42) || "…";
    $("#progress").hidden = false;
    const p = state.scanner.progress || {};
    const pct = p.total ? Math.round((p.done / p.total) * 100) : 8;
    $("#progress-bar").style.width = `${Math.max(4, pct)}%`;
    $("#progress-label").textContent = `${p.stage || ""} · ${p.message || ""}`;
  } else {
    $("#status-main").textContent = state.scanner.last_stats?.error ? "error" : "live";
    const st = state.scanner.last_stats || {};
    const bits = [];
    if (state.scanner.last_scan_at) bits.push(`scan ${ago(state.scanner.last_scan_at)}`);
    if (st.new !== undefined) bits.push(`${st.new} new`);
    if (state.nextScanAt && state.settings.auto_refresh === "1") {
      const s = Math.max(0, Math.round((state.nextScanAt - Date.now()) / 1000));
      bits.push(`next ${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`);
    }
    $("#status-sub").textContent = bits.join(" · ") || "—";
    $("#progress").hidden = true;
  }
  const live = $(".live");
  if (live) live.classList.toggle("off", !state.es);
  const llabel = $("#live-label");
  if (llabel) llabel.textContent = state.es ? "live" : "offline";
}

function renderTape() {
  const tape = $("#tape");
  const market = ["SPY", "QQQ"].filter((t) => state.etfs[t]?.price !== undefined);
  const marketHtml = market.map((t) => {
    const q = state.etfs[t];
    return `<span class="tape-item market" data-market="${t}" title="market benchmark">
      <span class="sym">${t}</span>
      <span class="px">${fmtPrice(q.price)}</span>
      <span class="chg ${dirClass(q.change_pct)}">${fmtPct(q.change_pct)}</span>
    </span>`;
  }).join("");
  const rows = state.positions.filter((p) => state.quotes[p.ticker]?.price !== undefined);
  const posHtml = rows.map((p) => {
    const q = state.quotes[p.ticker] || {};
    const d = dirClass(q.change_pct);
    return `<span class="tape-item" data-ticker="${esc(p.ticker)}">
      <span class="sym">${esc(p.ticker)}</span>
      <span class="px">${fmtPrice(q.price)}</span>
      <span class="chg ${d}">${fmtPct(q.change_pct)}</span>
    </span>`;
  }).join("");
  if (!rows.length && !market.length) {
    tape.innerHTML = `<span class="tape-empty">add tickers to see live quotes</span>`;
    return;
  }
  tape.innerHTML = marketHtml + (marketHtml && posHtml ? `<span class="tape-sep">|</span>` : "") + posHtml;
  $$(".tape-item[data-ticker]", tape).forEach((el) => el.onclick = () => openTicker(el.dataset.ticker));
}

function renderSourceStrip() {
  const el = $("#source-strip");
  if (!el) return;
  const st = state.scanner.last_stats || {};
  const bits = [
    `<span class="src-pill">${st.sources || 0} feeds</span>`,
    `<span class="src-pill">${st.raw || 0} headlines</span>`,
    `<span class="src-pill">${st.new || 0} new</span>`,
    `<span class="src-pill">${st.analyzed || 0} scored</span>`,
    `<span class="src-pill">${state.db.total || 0} stored</span>`,
  ];
  if (st.errors) bits.push(`<span class="src-pill bad">${st.errors} errors</span>`);
  if (state.gicsRows) bits.push(`<span class="src-pill">GICS ${state.gicsRows}</span>`);
  el.innerHTML = bits.join("");
}

function renderBrief() {
  const b = state.scanner.brief;
  const panel = $("#brief-panel");
  if (!b) { panel.hidden = true; return; }
  panel.hidden = false;
  const hidden = localStorage.getItem("brief-hidden") === "1";
  $("#brief-body").hidden = hidden;
  $("#brief-hide").textContent = hidden ? "show" : "hide";
  $("#brief-body").textContent = b;
  const at = state.scanner.brief_at;
  $("#brief-time").textContent = at ? `${ago(at)} · ${stamp(at)}` : "";
  $("#brief-time").title = at ? `written ${new Date(at).toLocaleString()}` : "";
  const badge = $("#brief-count");
  const n = state.scanner.brief_count || 0;
  if (badge) { badge.textContent = String(n); badge.hidden = !n; }
}

/* ============================================================ macro tape */
function macroItem(m) {
  const chg = m.chg_display
    ? `<span class="mi-chg ${m.dir || "flat"}">${esc(m.chg_display)}</span>` : "";
  const hint = m.hint || [m.label, m.display, m.asof, m.source].filter(Boolean).join(" · ");
  return `<span class="macro-item" title="${esc(hint)}">
    <span class="mi-label">${esc(m.label)}</span>
    <span class="mi-val">${esc(m.display)}</span>${chg}
  </span>`;
}

function renderMacro() {
  const panel = $("#macro-panel");
  if (!panel) return;
  const snap = state.macro;
  if (!snap || !(snap.groups || []).length) { panel.hidden = true; return; }
  panel.hidden = false;
  const asof = snap.as_of ? `as of ${snap.as_of}` : "";
  $("#macro-asof").textContent = asof;
  $("#macro-asof").title = snap.at
    ? `fetched ${stamp(snap.at)} · ${ago(snap.at)}` : "";
  const groups = (snap.groups || []).map((g) => `
    <div class="macro-group">
      <span class="mg-label">${esc(g.label)}</span>
      <div class="mg-items">${(g.metrics || []).map(macroItem).join("")}</div>
    </div>`).join("");
  const calItems = (snap.calendar || []).map((c) => `
    <span class="cal-chip ${c.soon ? "soon" : ""}" title="${esc(c.title)} · forecast ${esc(c.forecast || "—")} · previous ${esc(c.previous || "—")} · ${esc(c.impact || "")} impact">
      ${esc(c.when_label)} · ${esc(c.title)}
    </span>`);
  const calStatus = snap.calendar_status;
  if (calStatus === "stale") calItems.push(`<span class="macro-empty">stale</span>`);
  if (!calItems.length && calStatus === "unavailable") {
    calItems.push(`<span class="macro-empty">feed unavailable</span>`);
  } else if (!calItems.length && calStatus === "off") {
    calItems.push(`<span class="macro-empty">disabled</span>`);
  } else if (!calItems.length && calStatus === "ok") {
    calItems.push(`<span class="macro-empty">none this week</span>`);
  }
  const cal = calItems.length
    ? `<div class="macro-group">
        <span class="mg-label">Calendar</span>
        <div class="mg-items macro-cal">${calItems.join("")}</div>
      </div>`
    : "";
  const errs = (snap.errors || []).filter((e) => e.source !== "macro:calendar");
  const errors = errs.length
    ? `<div class="macro-group"><span class="mg-label">Partial</span>
        <div class="mg-items"><span class="macro-empty">${esc(
          errs.map((e) => e.source.replace("macro:", "")).join(", ")
        )} unavailable — showing the rest</span></div></div>`
    : "";
  $("#macro-body").innerHTML = groups + cal + errors;
}

/* ============================================================ watchlist */
function renderLists() {
  const pos = state.positions.filter((p) => p.kind === "position");
  const watch = state.positions.filter((p) => p.kind !== "position");
  $("#pos-count").textContent = pos.length;
  $("#watch-count").textContent = watch.length;
  $("#positions-list").innerHTML = pos.map(tickerRow).join("");
  $("#watch-list").innerHTML = watch.map(tickerRow).join("");
  $$(".trow").forEach((row) => {
    row.onclick = (e) => {
      if (e.target.closest(".del") || e.target.closest(".move") || e.target.closest(".side-pill")) return;
      openTicker(row.dataset.ticker);
    };
    const mv = row.querySelector(".move");
    if (mv) mv.onclick = async (e) => {
      e.stopPropagation();
      try { await toggleKind(row.dataset.ticker); } catch (err) { showToast(String(err.message), true); }
    };
    const sideBtn = row.querySelector(".side-pill");
    if (sideBtn) sideBtn.onclick = async (e) => {
      e.stopPropagation();
      try { await cycleSide(row.dataset.ticker, sideBtn.dataset.side || ""); }
      catch (err) { showToast(String(err.message), true); }
    };
    const del = row.querySelector(".del");
    if (del) del.onclick = async (e) => {
      e.stopPropagation();
      await api(`/api/positions/${row.dataset.ticker}`, { method: "DELETE" });
      state.positions = state.positions.filter((p) => p.ticker !== row.dataset.ticker);
      delete state.technicals[row.dataset.ticker];
      state.techRows = state.techRows.filter((r) => r.ticker !== row.dataset.ticker);
      state.techFull.delete(row.dataset.ticker);
      renderLists(); renderTape(); renderTechView(); loadFeed();
    };
  });
  if (state.filters.ticker) {
    const row = $$(".trow").find((r) => r.dataset.ticker === state.filters.ticker);
    if (row) row.classList.add("active");
  }
}

function tickerRow(p) {
  const q = state.quotes[p.ticker] || p.quote || {};
  const tech = state.technicals[p.ticker] || p.tech || {};
  const color = SECTOR_COLOR[p.sector_key] || "#33455c";
  const d = dirClass(q.change_pct);
  const active = state.filters.ticker === p.ticker ? "active" : "";
  const trend = tech.trend || "";
  const trendCls = trend.includes("up") ? "up" : trend.includes("down") ? "down" : "";
  const trendIco = trend.includes("up") ? "▲" : trend.includes("down") ? "▼" : "";
  const side = p.side || "";
  const sideTxt = side === "long" ? "L" : side === "short" ? "S" : "–";
  const sideTitle = side
    ? `${side} — click for ${side === "long" ? "short" : "no side"}`
    : "no side — click to tag long";
  const techTitle = tech.trend_label || "";
  return `<div class="trow ${active}" data-ticker="${esc(p.ticker)}" title="${esc(p.sub_industry || "")}">
    <span class="sec-dot" style="background:${color}"></span>
    <div class="mid">
      <div class="sym">${esc(p.ticker)}${trendIco ? `<span class="t-trend ${trendCls}" title="${esc(techTitle)}">${trendIco}</span>` : ""}</div>
      <div class="nm">${esc(p.name || "")}</div>
    </div>
    <button class="side-pill pill-${side || "none"}" data-side="${esc(side)}" title="${esc(sideTitle)}">${sideTxt}</button>
    <div class="right">
      <div class="px">${q.price !== undefined ? fmtPrice(q.price) : "—"}</div>
      <div class="chg ${d}">${fmtPct(q.change_pct)}</div>
    </div>
    <button class="move" title="${p.kind === "position" ? "Move to watchlist" : "Move to positions"}">⇄</button>
    <button class="del" title="Remove">✕</button>
  </div>`;
}

async function cycleSide(ticker, current) {
  const next = current === "long" ? "short" : current === "short" ? null : "long";
  await api(`/api/positions/${encodeURIComponent(ticker)}`, {
    method: "PATCH", body: { side: next },
  });
  const row = state.positions.find((p) => p.ticker === ticker);
  if (row) row.side = next;
  syncTechSide(ticker, next);
  renderLists();
  renderTechView();
  showToast(next ? `${ticker} · ${next}` : `${ticker} · no side`);
}

/* keep the cached light/full technical rows in step with a side change */
function syncTechSide(ticker, side) {
  if (state.technicals[ticker]) state.technicals[ticker].side = side;
  const full = state.techFull.get(ticker);
  if (full) full.side = side;
  const row = state.techRows.find((r) => r.ticker === ticker);
  if (row) row.side = side;
  const pos = state.positions.find((p) => p.ticker === ticker);
  if (pos) pos.side = side;
}

/* move a name between positions and watchlist in one click */
async function toggleKind(ticker) {
  const row = state.positions.find((p) => p.ticker === ticker);
  const kind = row?.kind === "position" ? "watch" : "position";
  await api(`/api/positions/${encodeURIComponent(ticker)}`, { method: "PATCH", body: { kind } });
  row.kind = kind;
  renderLists(); renderTape();
  showToast(`${ticker} → ${kind === "position" ? "positions" : "watchlist"}`);
  loadFeed();
}

function renderPresets() {
  const el = $("#presets");
  el.innerHTML = Object.keys(state.presets || {}).map(
    (k) => `<button class="preset" data-preset="${esc(k)}">+ ${esc(k)}</button>`
  ).join("");
  $$(".preset", el).forEach((b) => b.onclick = async () => {
    const tickers = state.presets[b.dataset.preset];
    await addTickers(tickers, "watch");
  });
}

async function addTickers(tickers, kind, side = "") {
  if (!tickers.length) return;
  await api("/api/positions", { method: "POST", body: { tickers, kind, side: side || undefined } });
  const s = await api("/api/state");
  state.positions = s.positions || [];
  state.counts = s.counts || {};
  state.technicals = s.technicals || state.technicals;
  state.techSummary = s.tech_summary || state.techSummary;
  state.techAt = s.tech_at || state.techAt;
  renderLists(); renderSectors(); renderTape(); renderTechView();
  showToast(`Added ${tickers.join(", ")} — mapping industry…`);
  setTimeout(loadFeed, 9000);
}

/* ======================================================== manage list */
// one name per line, optionally followed by L / S to tag the side
function parseManageList(text) {
  const out = new Map();
  text.split(/\n/).forEach((line) => {
    const parts = line.trim().split(/[\s,;]+/).filter(Boolean);
    if (!parts.length) return;
    const ticker = parts[0].toUpperCase();
    if (!/^[A-Z0-9.^-]{1,10}$/.test(ticker)) return;
    const raw = (parts[1] || "").toLowerCase();
    const side = raw === "l" || raw === "long" ? "long"
      : raw === "s" || raw === "short" ? "short" : null;
    if (!out.has(ticker)) out.set(ticker, side);
  });
  return out;
}

function manageSnapshot() {
  return {
    positions: parseManageList($("#mg-positions").value),
    watch: parseManageList($("#mg-watch").value),
  };
}

function manageDiff() {
  const now = new Map(state.positions.map((p) => [p.ticker, p]));
  const { positions, watch } = manageSnapshot();
  const want = new Map();
  positions.forEach((side, t) => want.set(t, { kind: "position", side }));
  watch.forEach((side, t) => { if (!want.has(t)) want.set(t, { kind: "watch", side }); });
  const add = [], move = [], remove = [], sides = [];
  want.forEach((v, t) => {
    const cur = now.get(t);
    if (!cur) { add.push(t); return; }
    if (cur.kind !== v.kind) move.push(t);
    if ((cur.side || null) !== v.side) sides.push(t);
  });
  now.forEach((_p, t) => { if (!want.has(t)) remove.push(t); });
  $("#mg-pos-count").textContent = positions.size;
  $("#mg-watch-count").textContent = watch.size;
  const parts = [];
  if (add.length) parts.push(`+${add.length} add`);
  if (move.length) parts.push(`~${move.length} move`);
  if (sides.length) parts.push(`↔${sides.length} side`);
  if (remove.length) parts.push(`−${remove.length} remove`);
  $("#mg-diff").textContent = parts.length ? parts.join(" · ") : "no changes";
  return { add, move, remove, sides, want };
}

function openManage() {
  const line = (p) => `${p.ticker}${p.side ? " " + (p.side === "long" ? "L" : "S") : ""}`;
  const pos = state.positions.filter((p) => p.kind === "position").map(line);
  const watch = state.positions.filter((p) => p.kind !== "position").map(line);
  $("#mg-positions").value = pos.join("\n");
  $("#mg-watch").value = watch.join("\n");
  manageDiff();
  $("#manage-modal").showModal();
}

async function saveManage() {
  const { add, move, remove, sides, want } = manageDiff();
  if (!add.length && !move.length && !remove.length && !sides.length) {
    $("#manage-modal").close(); return;
  }
  const btn = $("#mg-save");
  btn.disabled = true; btn.textContent = "saving…";
  try {
    for (const t of remove) {
      await api(`/api/positions/${encodeURIComponent(t)}`, { method: "DELETE" });
    }
    const entries = [...add, ...move].map((t) => {
      const w = want.get(t);
      return { ticker: t, kind: w.kind, side: w.side };
    });
    if (entries.length) {
      await api("/api/positions", { method: "POST", body: { items: entries } });
    }
    for (const t of sides) {
      await api(`/api/positions/${encodeURIComponent(t)}`, { method: "PATCH", body: { side: want.get(t).side } });
    }
    const s = await api("/api/state");
    state.positions = s.positions || [];
    state.technicals = s.technicals || {};
    state.techSummary = s.tech_summary || {};
    state.techAt = s.tech_at || state.techAt;
    renderLists(); renderTape(); renderSectors(); renderTechView();
    $("#manage-modal").close();
    showToast(`Saved: +${add.length} ~${move.length} ↔${sides.length} −${remove.length} — mapping new names…`);
    setTimeout(loadFeed, 8000);
  } catch (err) {
    showToast(String(err.message), true);
  }
  btn.disabled = false; btn.textContent = "Save changes";
}

$("#btn-manage").onclick = openManage;
$("#mg-save").onclick = saveManage;
$("#mg-reset").onclick = openManage;
["#mg-positions", "#mg-watch"].forEach((sel) => {
  $(sel).oninput = manageDiff;
  $(sel).onkeydown = (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); saveManage(); }
  };
});

/* ============================================================ sectors */
function renderSectors() {
  const grid = $("#sector-grid");
  grid.innerHTML = state.sectors.map((s) => {
    const c = state.counts[s.key] || 0;
    const active = state.filters.sector === s.key ? "active" : "";
    const etf = state.etfs[s.etf] || {};
    const etfHtml = etf.change_pct !== undefined
      ? `<span class="st-etf-chg ${dirClass(etf.change_pct)}">${fmtPct(etf.change_pct)}</span>` : "";
    return `<div class="sector-tile ${active}" data-sector="${s.key}" style="--tile-color:${s.color}"
                 title="${esc(s.name)} · ${esc(s.etf || "")} — click to filter">
      <div class="st-top"><span class="st-dot" style="background:${s.color}"></span>
        <span class="st-etf">${esc(s.etf || "")}</span>${etfHtml}</div>
      <div class="st-name">${esc(s.name)}</div>
      <div class="st-count ${c ? "" : "zero"}">${c}</div>
    </div>`;
  }).join("");
  $$(".sector-tile", grid).forEach((t) => t.onclick = () => {
    state.filters.sector = state.filters.sector === t.dataset.sector ? null : t.dataset.sector;
    state.filters.scope = state.filters.sector ? "all" : state.filters.scope;
    renderSectors(); loadFeed();
  });
}

function renderTopics() {
  const row = $("#topic-row");
  if (!row) return;
  row.innerHTML = state.topics.map((t) => {
    const c = state.topicCounts[t.key] || 0;
    const active = state.filters.topic === t.key ? "active" : "";
    return `<button class="topic-chip ${active}" data-topic="${t.key}" title="filter by ${esc(t.name)}">
      ${esc(t.name)}<span class="tc-count">${c}</span></button>`;
  }).join("");
  $$(".topic-chip", row).forEach((b) => b.onclick = () => {
    state.filters.topic = state.filters.topic === b.dataset.topic ? null : b.dataset.topic;
    renderTopics(); loadFeed();
  });
}

/* ============================================================ feed */
function matchFilters(it) {
  const f = state.filters;
  if (f.scope !== "all" && it.scope !== f.scope) return false;
  if (f.scope === "sector" && it.scope !== "sector" && !(it.sectors || []).length) return false;
  if (f.sector && !(it.sectors || []).includes(f.sector)) return false;
  if (f.topic && !(it.topics || []).includes(f.topic)) return false;
  if (f.ticker) {
    const tk = (it.tickers || []).map((x) => String(x).toUpperCase());
    if (!tk.includes(f.ticker.toUpperCase())) return false;
  }
  if (f.hideMarket && it.scope === "market") return false;
  if ((it.relevance || 0) < f.minRel) return false;
  if (f.q) {
    const hay = `${it.title} ${it.summary || ""} ${(it.tickers || []).join(" ")}`.toLowerCase();
    if (!hay.includes(f.q.toLowerCase())) return false;
  }
  return true;
}

async function loadFeed() {
  if (state.feedLoading) return;
  state.feedLoading = true;
  const f = state.filters;
  const params = new URLSearchParams({
    limit: "220",
    min_relevance: String(f.minRel),
    sort: SORT_API[f.sort] || "published",
    hours: String(f.hours || 48),
  });
  if (f.scope !== "all") params.set("scope", f.scope);
  if (f.sector) params.set("sector", f.sector);
  if (f.topic) params.set("topic", f.topic);
  if (f.ticker) params.set("ticker", f.ticker);
  if (f.q) params.set("q", f.q);
  try {
    const data = await api(`/api/items?${params}`);
    state.counts = data.counts || state.counts;
    state.db = data.total || state.db;
    let items = data.items || [];
    items = items.filter((it) => !(f.hideMarket && it.scope === "market"));
    applySort(items);
    state.items = items;
    state.itemIndex = new Map(items.map((i) => [i.id, i]));
    renderFeed();
    renderSectors();
    renderTopics();
    $("#feed-count").textContent = `${items.length} headlines`;
    $("#feed-updated").textContent = `updated ${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
  } catch (err) {
    console.error(err);
  } finally {
    state.feedLoading = false;
  }
}

function sentimentChip(s) {
  if (!s || s === "neutral") return "";
  const cls = `sent-${s}`;
  const arrow = s === "bullish" ? "▲" : s === "bearish" ? "▼" : "◆";
  return `<span class="chip ${cls}">${arrow} ${esc(s)}</span>`;
}

function impactDots(n) {
  if (!n) return "";
  const full = "●".repeat(n);
  const empty = "○".repeat(Math.max(0, 5 - n));
  return `<span class="imp" title="impact ${n}/5">${full}${empty}</span>`;
}

function itemHTML(it) {
  const rel = Math.round(it.relevance || 0);
  const tkChips = (it.tickers || []).slice(0, 5).map(
    (t) => `<span class="chip tk" data-ticker="${esc(t)}">${esc(t)}</span>`
  ).join("");
  const secChips = (it.sectors || []).slice(0, 2).map(
    (s) => `<span class="chip sec" data-sector="${esc(s)}">${esc(SECTOR_NAME[s] || s)}</span>`
  ).join("");
  const topChips = (it.topics || []).slice(0, 2).map(
    (t) => `<span class="chip top" data-topic="${esc(t)}">${esc(TOPIC_NAME[t] || t)}</span>`
  ).join("");
  const dup = it.dup_count ? `<span class="chip dup">+${it.dup_count} sources</span>` : "";
  const src = it.source || it.domain || "unknown";
  const why = it.why ? `<p class="item-why">${esc(it.why)}</p>` : "";
  return `<article class="item scope-${esc(it.scope)} ${it.analyzed ? "analyzed" : ""}" data-id="${it.id}">
    <div class="item-accent"></div>
    <div class="item-body">
      <div class="item-meta">
        <span class="src">${esc(src)}</span>
        <span class="sep">·</span>
        <span class="time" title="published ${esc(stamp(it.published_at) || "unknown")} · pulled ${esc(stamp(it.fetched_at))}">${it.published_at ? ago(it.published_at) : "undated"}</span>
        <span class="time-abs ${it.published_at ? "" : "missing"}" title="${it.published_at ? "publish time" : "source gave no publish time — this is when we pulled it"}">${esc(it.published_at ? stamp(it.published_at) : "pulled " + stamp(it.fetched_at))}</span>
        <span class="sep">·</span>
        <span class="scope-label muted">${esc(it.scope)}</span>
        ${impactDots(it.impact)}
        <span class="rel-badge ${rel >= 60 ? "hot" : ""}" title="relevance ${rel}/100">${rel}</span>
      </div>
      <h3 class="item-title"><a href="${esc(it.url)}" target="_blank" rel="noopener noreferrer">${esc(it.title)}</a></h3>
      ${why}
      <div class="chips">${tkChips}${secChips}${topChips}${sentimentChip(it.sentiment)}${dup}</div>
    </div>
  </article>`;
}

function bindItemActions(root) {
  $$("[data-ticker]", root).forEach((el) => {
    if (el.classList.contains("trow") || el.classList.contains("tape-item")) return;
    el.onclick = (e) => { e.stopPropagation(); openTicker(el.dataset.ticker); };
  });
  $$("[data-sector]", root).forEach((el) => {
    if (el.classList.contains("sector-tile")) return;
    el.onclick = (e) => {
      e.stopPropagation();
      state.filters.sector = el.dataset.sector;
      renderSectors(); loadFeed();
    };
  });
  $$("[data-topic]", root).forEach((el) => {
    el.onclick = (e) => {
      e.stopPropagation();
      state.filters.topic = state.filters.topic === el.dataset.topic ? null : el.dataset.topic;
      loadFeed();
    };
  });
}

function renderFeed() {
  const feed = $("#feed");
  if (!state.items.length) {
    feed.innerHTML = `<div class="empty"><h3>No headlines match</h3>
      <p class="muted">Loosen the filters, or hit <b>Scan now</b>. If nothing arrives, check
      Source health in settings.</p></div>`;
    return;
  }
  feed.innerHTML = state.items.slice(0, 250).map(itemHTML).join("");
  bindItemActions(feed);
}

function prependItems(items) {
  const fresh = items.filter(matchFilters).filter((i) => !state.itemIndex.has(i.id));
  if (!fresh.length) return;
  fresh.sort((a, b) => (Date.parse(b.published_at) || 0) - (Date.parse(a.published_at) || 0));
  fresh.forEach((i) => { state.itemIndex.set(i.id, i); });
  state.items = [...fresh, ...state.items].slice(0, 400);
  const feed = $("#feed");
  if (feed.querySelector(".empty")) { renderFeed(); return; }
  const html = fresh.map(itemHTML).join("");
  feed.insertAdjacentHTML("afterbegin", html);
  $$(".item.fresh", feed).forEach((el) => el.classList.remove("fresh"));
  [...feed.children].slice(0, fresh.length).forEach((el) => el.classList.add("fresh"));
  bindItemActions(feed);
  while (feed.children.length > 250) feed.lastElementChild.remove();
  $("#feed-count").textContent = `${state.items.length} headlines`;
}

function updateItems(items) {
  let changed = 0;
  items.forEach((it) => {
    if (!state.itemIndex.has(it.id)) return;
    state.itemIndex.set(it.id, it);
    const idx = state.items.findIndex((x) => x.id === it.id);
    if (idx >= 0) state.items[idx] = it;
    const node = $(`.item[data-id="${it.id}"]`);
    if (node && it.analyzed) {
      const tmp = document.createElement("div");
      tmp.innerHTML = itemHTML(it);
      node.replaceWith(tmp.firstElementChild);
      changed++;
    }
  });
  if (changed) bindItemActions($("#feed"));
}

/* ============================================================ SSE */
function connectStream() {
  if (state.es) state.es.close();
  const es = new EventSource("/api/stream");
  state.es = es;
  es.onopen = () => { renderStatus(); };
  es.onerror = () => { renderStatus(); };
  es.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    handleEvent(msg);
  };
}

function handleEvent(msg) {
  switch (msg.type) {
    case "hello":
      state.scanner.scanning = msg.scanning;
      state.scanner.progress = msg.progress;
      state.scanner.last_scan_at = state.scanner.last_scan_at || msg.last_scan_at;
      renderStatus();
      break;
    case "scan_start":
      state.scanner.scanning = true;
      state.scanner.progress = { stage: "start", message: "starting", done: 0, total: 0 };
      renderStatus();
      break;
    case "progress":
      state.scanner.progress = msg;
      state.scanner.scanning = msg.stage !== "idle";
      renderStatus();
      break;
    case "items":
      prependItems(msg.items || []);
      break;
    case "items_update":
      updateItems(msg.items || []);
      break;
    case "scan_done":
      state.scanner.scanning = false;
      state.scanner.last_stats = msg.stats;
      state.scanner.last_scan_at = msg.at;
      state.counts = msg.counts || state.counts;
      state.topicCounts = msg.topic_counts || state.topicCounts;
      state.db = msg.db || state.db;
      state.nextScanAt = Date.now() + (+state.settings.scan_interval_seconds || 180) * 1000;
      renderStatus(); renderSectors(); renderTopics(); renderSourceStrip();
      loadFeed();
      break;    case "scan_error":
      state.scanner.scanning = false;
      state.scanner.last_stats = { ...(state.scanner.last_stats || {}), error: msg.error };
      renderStatus();
      showToast(`Scan error: ${msg.error}`, true);
      break;
    case "quotes":
      state.quotes = { ...state.quotes, ...(msg.quotes || {}) };
      if (msg.etfs) state.etfs = { ...state.etfs, ...msg.etfs };
      renderTape(); renderLists(); renderSectors();
      break;
    case "macro":
      state.macro = msg.macro || state.macro;
      state.macroAt = msg.at || state.macroAt;
      renderMacro();
      break;
    case "brief":
      state.scanner.brief = msg.text;
      state.scanner.brief_at = msg.at;
      if (msg.count !== undefined) state.scanner.brief_count = msg.count;
      renderBrief();
      break;
    case "technicals":
      state.technicals = { ...state.technicals, ...(msg.summaries || {}) };
      state.techSummary = msg.summary || state.techSummary;
      state.techAt = msg.at || state.techAt;
      renderLists();
      if (state.view === "tech") {
        renderTechView();
        scheduleTechReload();
      }
      break;
    case "tech_error":
      showToast(`Technical scan: ${msg.error}`, true);
      break;
  }
}

/* ============================================================ ticker drawer */
async function openTicker(ticker) {
  if (!ticker) return;
  ticker = ticker.toUpperCase();
  const modal = $("#ticker-modal");
  $("#tm-ticker").textContent = ticker;
  $("#tm-name").textContent = "loading…";
  $("#tm-kv").innerHTML = "";
  $("#tm-peers").innerHTML = "";
  $("#tm-tech").innerHTML = `<span class="muted small">Reading price action…</span>`;
  $("#tm-news").innerHTML = "";
  $("#tm-edit").innerHTML = "";
  $("#tm-quote").textContent = "";
  modal.showModal();
  try {
    const data = await api(`/api/security/${encodeURIComponent(ticker)}`);
    renderTickerModal(ticker, data);
  } catch (err) {
    $("#tm-name").textContent = `error: ${err.message}`;
  }
}

function renderTickerModal(ticker, data) {
  const sec = data.security || {};
  const pos = state.positions.find((p) => p.ticker === ticker);
  const q = data.quote || state.quotes[ticker] || {};
  const color = SECTOR_COLOR[sec.sector_key] || "#33455c";
  $("#tm-name").textContent = sec.name || ticker;
  $("#tm-quote").innerHTML = q.price !== undefined
    ? `<span class="${dirClass(q.change_pct)}">${fmtPrice(q.price)} ${fmtPct(q.change_pct)}</span>` : "";
  $("#tm-kv").innerHTML = `
    <div class="kv"><span class="k">GICS sector</span><span class="v sans">
      <span class="sec-dot" style="display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:5px;background:${color}"></span>
      ${esc(sec.sector || "—")}</span></div>
    <div class="kv"><span class="k">Sub-industry</span><span class="v sans">${esc(sec.sub_industry || "—")}</span></div>
    <div class="kv"><span class="k">Mapping source</span><span class="v">${esc(sec.source || "—")} ${
      sec.confidence ? `· ${Math.round((sec.confidence || 0) * 100)}%` : ""}</span></div>
    <div class="kv"><span class="k">CIK / exchange</span><span class="v">${esc(sec.cik || "—")} · ${esc(sec.exchange || "—")}</span></div>
    <div class="kv"><span class="k">In your book</span><span class="v sans">${pos ? esc(pos.kind) + (pos.side ? ` · ${esc(pos.side)}` : " · no side") : "not tracked"}</span></div>
    <div class="kv"><span class="k">IR RSS feed</span><span class="v" style="font-size:11px">${esc(pos?.ir_feed || "—")}</span></div>`;

  const peers = sec.peers || [];
  $("#tm-peers").innerHTML = peers.length
    ? peers.map((p) => `<span class="peer-chip" data-ticker="${esc(p.ticker)}">
        <span class="psym">${esc(p.ticker)}</span>
        <span class="pnm">${esc(p.name || "")}</span></span>`).join("")
    : `<span class="muted small">No peers found — try “Re-map industry”.</span>`;
  $$(".peer-chip", $("#tm-peers")).forEach((el) => el.onclick = () => openTicker(el.dataset.ticker));

  renderTickerTech(data.technical, ticker, pos);

  $("#tm-edit").innerHTML = `
    <div class="kv"><span class="k">Track as</span>
      <select id="tm-kind">
        <option value="watch" ${pos?.kind !== "position" ? "selected" : ""}>watchlist</option>
        <option value="position" ${pos?.kind === "position" ? "selected" : ""}>position</option>
        ${pos ? "" : `<option value="add">add to watchlist</option>`}
      </select></div>
    <div class="kv"><span class="k">Side</span>
      <select id="tm-side" title="Long or short — the technical read is contextualised against this">
        <option value="" ${!pos?.side ? "selected" : ""}>— no view</option>
        <option value="long" ${pos?.side === "long" ? "selected" : ""}>long</option>
        <option value="short" ${pos?.side === "short" ? "selected" : ""}>short</option>
      </select></div>
    <div class="kv"><span class="k">Shares</span><input id="tm-shares" type="number" step="any" value="${pos?.shares ?? ""}" placeholder="—" /></div>
    <div class="kv"><span class="k">Avg cost</span><input id="tm-cost" type="number" step="any" value="${pos?.avg_cost ?? ""}" placeholder="—" /></div>
    <div class="kv"><span class="k">Unrealised P&L</span><span class="v ${dirClass((q.price || 0) - (pos?.avg_cost || 0))}">${
      pos?.shares && pos?.avg_cost && q.price
        ? `${((q.price - pos.avg_cost) * pos.shares).toLocaleString(undefined, { maximumFractionDigits: 0 })} (${(((q.price / pos.avg_cost) - 1) * 100).toFixed(1)}%)`
        : "—"}</span></div>`;

  $("#tm-news").innerHTML = (data.items || []).length
    ? (data.items || []).map((it) => `<div class="mini-item">
        <div class="mi-meta"><span>${esc(it.source)}</span><span>·</span>
          <span class="time" data-ts="${esc(it.published_at)}">${it.published_at ? ago(it.published_at) : "undated"}</span>
          <span class="time-abs">${esc(it.published_at ? stamp(it.published_at) : "pulled " + stamp(it.fetched_at))}</span>
          <span>·</span><span>rel ${Math.round(it.relevance || 0)}</span></div>
        <div class="mi-title"><a href="${esc(it.url)}" target="_blank" rel="noopener noreferrer">${esc(it.title)}</a></div>
      </div>`).join("")
    : `<span class="muted small">No headlines for this ticker yet.</span>`;

  $("#tm-ir").onclick = async () => {
    $("#tm-ir").textContent = "searching…";
    try {
      const r = await api(`/api/positions/${ticker}/ir`, { method: "POST" });
      showToast(r.feed_url ? `IR feed: ${r.feed_url}` : "No IR RSS feed found", !r.feed_url);
      if (r.feed_url) openTicker(ticker);
    } catch (e) { showToast(String(e.message), true); }
    $("#tm-ir").textContent = "Find IR RSS feed";
  };
  $("#tm-refresh").onclick = async () => {
    $("#tm-refresh").textContent = "mapping…";
    try {
      const d = await api(`/api/security/${ticker}?refresh=true`);
      renderTickerModal(ticker, d);
      state.positions = (await api("/api/state")).positions;
      renderLists();
    } catch (e) { showToast(String(e.message), true); }
    $("#tm-refresh").textContent = "Re-map industry";
  };
  $("#tm-remove").onclick = async () => {
    if (!pos) return;
    await api(`/api/positions/${ticker}`, { method: "DELETE" });
    state.positions = state.positions.filter((p) => p.ticker !== ticker);
    delete state.technicals[ticker];
    state.techRows = state.techRows.filter((r) => r.ticker !== ticker);
    state.techFull.delete(ticker);
    renderLists(); renderTape(); renderTechView(); $("#ticker-modal").close(); loadFeed();
  };
  const kindSel = $("#tm-kind");
  const save = async () => {
    const kind = kindSel.value === "add" ? "watch" : kindSel.value;
    const sideVal = $("#tm-side").value || null;
    const shares = parseFloat($("#tm-shares").value);
    const avg_cost = parseFloat($("#tm-cost").value);
    await api("/api/positions", { method: "POST", body: { items: [{
      ticker, kind,
      shares: Number.isNaN(shares) ? null : shares,
      avg_cost: Number.isNaN(avg_cost) ? null : avg_cost,
    }] } });
    await api(`/api/positions/${encodeURIComponent(ticker)}`, { method: "PATCH", body: { side: sideVal } });
    const s = await api("/api/state");
    state.positions = s.positions || [];
    state.technicals = s.technicals || state.technicals;
    state.techSummary = s.tech_summary || state.techSummary;
    state.techAt = s.tech_at || state.techAt;
    syncTechSide(ticker, sideVal);
    const saved = state.positions.find((p) => p.ticker === ticker) || { ...pos, side: sideVal };
    renderTickerTech(data.technical, ticker, saved);
    renderLists(); renderTape(); renderTechView(); showToast("Saved");
  };
  kindSel.onchange = save;
  $("#tm-side").onchange = save;
  $("#tm-shares").onchange = save;
  $("#tm-cost").onchange = save;
}

/* ============================================================ technicals */
function sparklineSVG(values, tone = "flat", height = 38) {
  if (!Array.isArray(values) || values.length < 2) return "";
  const nums = values.map(Number).filter(Number.isFinite);
  if (nums.length < 2) return "";
  const w = 300, h = height, pad = 3;
  const min = Math.min(...nums), max = Math.max(...nums);
  const span = max - min || 1;
  const pts = nums.map((v, i) => [
    (i / (nums.length - 1)) * w,
    h - pad - ((v - min) / span) * (h - pad * 2),
  ]);
  const color = tone === "up" ? "var(--up)" : tone === "down" ? "var(--down)" : "var(--muted)";
  const [lx, ly] = pts[pts.length - 1];
  return `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" role="img"
    aria-label="60-session price line">
    <polyline points="${pts.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ")}"
      fill="none" stroke="${color}" stroke-width="1.5" vector-effect="non-scaling-stroke"
      stroke-linejoin="round" stroke-linecap="round" />
    <circle cx="${lx.toFixed(1)}" cy="${ly.toFixed(1)}" r="2.2" fill="${color}" />
  </svg>`;
}

function techTone(row) {
  return row.bias === "bullish" ? "up" : row.bias === "bearish" ? "down" : "flat";
}

function techCard(row) {
  const side = row.side || "";
  const ctx = (row.context || {})[side] || null;
  const tone = techTone(row);
  const score = Math.round(Number(row.score) || 0);
  const scorePos = Math.max(0, Math.min(100, (score + 100) / 2));
  const m = row.momentum || {};
  const v = row.volatility || {};
  const vol = row.volume || {};
  const lv = row.levels || {};
  const rsi = m.rsi14 ?? row.rsi;
  const atr = v.atr_pct ?? row.atr_pct;
  const macd = m.macd_state ?? row.macd_state;
  const tags = [
    rsi !== null && rsi !== undefined ? `RSI ${Math.round(rsi)}` : null,
    macd ? `MACD ${macd}` : null,
    atr !== null && atr !== undefined ? `ATR ${(+atr).toFixed(1)}%` : null,
    vol.ratio !== null && vol.ratio !== undefined ? `vol ${(+vol.ratio).toFixed(2)}×` : null,
  ].filter(Boolean);
  const sup = (lv.support || [])[0];
  const res = (lv.resistance || [])[0];
  const ctxHtml = side && ctx
    ? `<div class="tc-context align-${esc(ctx.alignment || "mixed")}"><b>${esc(side)}</b>${esc(ctx.verdict || "")}</div>`
    : `<div class="tc-context align-mixed"><b>no side</b> Tag L or S in the rail — the read is then judged against that side.</div>`;
  const levelHtml = (sup || res)
    ? `<div class="tc-levels">
        ${sup ? `<span class="s">S ${fmtPrice(sup.price)} <span class="muted">${fmtPct(sup.distance_pct)}</span></span>` : ""}
        ${res ? `<span class="r">R ${fmtPrice(res.price)} <span class="muted">${fmtPct(res.distance_pct)}</span></span>` : ""}
        ${row.setup_label ? `<span class="muted">${esc(row.setup_label)}</span>` : ""}
      </div>`
    : "";
  return `<article class="tech-card tone-${tone}" data-ticker="${esc(row.ticker)}"
      title="Open ${esc(row.ticker)} — full technical read">
    <div class="tc-head">
      <span class="tc-sym">${esc(row.ticker)}</span>
      <span class="side-pill sm pill-${side || "none"}">${side === "long" ? "L" : side === "short" ? "S" : "–"}</span>
      <span class="tc-name">${esc(row.name || "")}</span>
      <span class="tc-px">${row.price !== null && row.price !== undefined ? fmtPrice(row.price) : "—"}
        <span class="chg ${dirClass(row.change_pct)}">${fmtPct(row.change_pct)}</span></span>
    </div>
    <div class="tc-spark">${sparklineSVG(row.spark, tone, 38)}</div>
    <div class="tc-line">${esc(row.headline || row.trend_label || "")}</div>
    <div class="tc-tags">${tags.map((t) => `<span class="tc-tag">${esc(t)}</span>`).join("")}</div>
    <div class="tc-score">
      <div class="tc-meter"><i></i><b style="left:${scorePos}%"></b></div>
      <span class="tc-score-n ${dirClass(score)}">${score > 0 ? "+" : ""}${score}</span>
    </div>
    ${ctxHtml}
    ${levelHtml}
  </article>`;
}

function techRowFromPosition(p) {
  const full = state.techFull.get(p.ticker) || {};
  const light = state.technicals[p.ticker] || p.tech || {};
  return {
    ...light, ...full,
    ticker: p.ticker, name: full.name || p.name || p.ticker,
    side: p.side || null, kind: p.kind, sector_key: p.sector_key,
  };
}

const ALIGN_WORDS = { supportive: "with", mixed: "mixed", hostile: "against" };
const ALIGN_PHRASE = { supportive: "with the tape", mixed: "mixed vs the tape", hostile: "against the tape" };

function renderTechSummary(sum) {
  const el = $("#tech-summary");
  if (!el) return;
  if (!sum.total) {
    el.textContent = sum.headline || "No technical snapshots yet — run a scan.";
    return;
  }
  const b = sum.by_bias || {};
  const sides = sum.sides || {};
  const al = sum.alignment || {};
  const n = (v, tone = "") => `<b class="ts-n ${tone}">${v || 0}</b>`;
  const rows = [`<div class="ts-k">Breadth</div><div class="ts-v">
      ${n(b.bullish, "bull")} bullish · ${n(b.neutral)} neutral · ${n(b.bearish, "bear")} bearish
      ${(sum.overbought || []).length ? ` <span class="ts-muted warm">· ${sum.overbought.length} overbought</span>` : ""}
      ${(sum.oversold || []).length ? ` <span class="ts-muted cold">· ${sum.oversold.length} oversold</span>` : ""}
    </div>`];
  const sideRow = (label, side) => {
    const c = al[side] || {};
    const total = (c.supportive || 0) + (c.mixed || 0) + (c.hostile || 0);
    if (!total) return "";
    return `<div class="ts-k">${label}</div><div class="ts-v">
      ${n(c.supportive, "sup")} with · ${n(c.mixed, "mix")} mixed · ${n(c.hostile, "hos")} against
      <span class="ts-muted">· ${total} idea${total === 1 ? "" : "s"}</span></div>`;
  };
  rows.push(sideRow("Longs", "long"), sideRow("Shorts", "short"));
  if (!sides.long && !sides.short) {
    rows.push(`<div class="ts-k">Ideas</div><div class="ts-v ts-muted">
      none tagged — click the L/S pill in the rail to judge an idea against the tape</div>`);
  }
  el.innerHTML = `<div class="ts-grid">${rows.join("")}</div>`;
}

function ideaChip(i) {
  const score = i.score === null || i.score === undefined
    ? "" : `${i.score > 0 ? "+" : ""}${Math.round(i.score)}`;
  const bits = [i.trend_label || i.trend, i.setup_label].filter(Boolean).join(", ");
  const title = `${i.ticker} · ${i.side} · ${ALIGN_PHRASE[i.alignment] || "mixed vs the tape"}`
    + (bits ? ` · ${bits}` : "") + (score ? ` · score ${score}` : "");
  return `<button class="idea-chip ${esc(i.alignment || "mixed")}" data-ticker="${esc(i.ticker)}"
    title="${esc(title)}">${esc(i.ticker)}</button>`;
}

function alignSideRow(kind, side, data, ideas) {
  const c = data[side] || {};
  const total = (c.supportive || 0) + (c.mixed || 0) + (c.hostile || 0);
  const parts = ["supportive", "mixed", "hostile"].map((k) =>
    `<span class="al-count ${k} ${c[k] ? "" : "zero"}"><b>${c[k] || 0}</b> ${ALIGN_WORDS[k]}</span>`
  ).join('<span class="al-sep">·</span>');
  const bar = total
    ? `<div class="al-bar" aria-hidden="true">${["supportive", "mixed", "hostile"]
        .filter((k) => c[k])
        .map((k) => `<i class="${k}" style="width:${(c[k] / total * 100).toFixed(1)}%"></i>`)
        .join("")}</div>`
    : "";
  const chips = ideas.filter((i) => i.kind === kind && i.side === side).map(ideaChip).join("");
  return `<div class="al-row">
    <div class="al-row-top">
      <span class="side-pill sm pill-${side}">${side === "long" ? "L" : "S"}</span>
      <span class="al-counts">${parts}</span>
      <span class="al-total">${total} idea${total === 1 ? "" : "s"}</span>
    </div>
    ${bar}
    <div class="al-chips">${chips || `<span class="align-empty">no ${side} ideas tagged</span>`}</div>
  </div>`;
}

function alignCard(kind, label, data, ideas) {
  const meta = `${data.tagged || 0} tagged${data.none ? ` · ${data.none} untagged` : ""}`;
  return `<div class="align-card">
    <div class="align-card-head"><b>${label}</b><span class="muted small">${meta}</span></div>
    ${alignSideRow(kind, "long", data, ideas)}
    ${alignSideRow(kind, "short", data, ideas)}
  </div>`;
}

function renderTechAlignment(sum) {
  const el = $("#tech-align");
  if (!el) return;
  const tagged = sum.tagged ?? ((sum.sides?.long || 0) + (sum.sides?.short || 0));
  if (!tagged) {
    el.innerHTML = `<div class="align-hint">
      <span class="side-pill sm pill-none">–</span>
      Tag a position or watchlist idea <b>long</b> or <b>short</b> — this board then splits
      positions and watchlist ideas into with / mixed / against the tape.</div>`;
    return;
  }
  const by = sum.alignment_by_kind || {};
  const ideas = sum.ideas || [];
  const al = sum.alignment || {};
  const against = (al.long?.hostile || 0) + (al.short?.hostile || 0);
  el.innerHTML = `<div class="align-board">
    <div class="align-board-head">
      <h3>Ideas vs the tape</h3>
      <span class="muted small">tagged long/short · judged on trend, momentum and levels</span>
      <span class="align-badge ${against ? "warn" : "ok"}">${
        against ? `${against} against the tape` : "all tagged ideas aligned"}</span>
    </div>
    <div class="align-cards">
      ${alignCard("position", "Positions", by.position || {}, ideas)}
      ${alignCard("watch", "Watchlist", by.watch || {}, ideas)}
    </div>
  </div>`;
  $$(".idea-chip", el).forEach((chip) => {
    chip.onclick = () => openTicker(chip.dataset.ticker);
  });
}

function renderTechView() {
  const grid = $("#tech-grid");
  const empty = $("#tech-empty");
  if (!grid) return;
  const sum = state.techSummary || {};
  renderTechSummary(sum);
  renderTechAlignment(sum);
  const sideCounts = {
    all: state.positions.length,
    long: sum.sides?.long || 0,
    short: sum.sides?.short || 0,
    none: sum.sides?.none || 0,
  };
  const sideLabels = { all: "All", long: "Longs", short: "Shorts", none: "No side" };
  $$("#tech-side-tabs button").forEach((btn) => {
    const key = btn.dataset.side;
    if (sideLabels[key]) btn.textContent = `${sideLabels[key]} ${sideCounts[key] ?? 0}`;
  });
  $("#tech-at").textContent = state.techAt ? `updated ${ago(state.techAt)}` : "";
  const chips = [];
  const t = sum.by_trend || {};
  const up = (t.strong_up || 0) + (t.up || 0);
  const down = (t.strong_down || 0) + (t.down || 0);
  const flat = t.sideways || 0;
  if (up || down || flat) {
    chips.push(`<span class="breadth-chip bull">▲ ${up} up</span>`);
    chips.push(`<span class="breadth-chip bear">▼ ${down} down</span>`);
    if (flat) chips.push(`<span class="breadth-chip dim">▬ ${flat} sideways</span>`);
  }
  if ((sum.overbought || []).length) chips.push(`<span class="breadth-chip warn">${sum.overbought.length} overbought · ${esc(sum.overbought.slice(0, 4).join(", "))}</span>`);
  if ((sum.oversold || []).length) chips.push(`<span class="breadth-chip">${sum.oversold.length} oversold · ${esc(sum.oversold.slice(0, 4).join(", "))}</span>`);
  if ((sum.near_highs || []).length) chips.push(`<span class="breadth-chip bull">${sum.near_highs.length} at 52w highs</span>`);
  if ((sum.near_lows || []).length) chips.push(`<span class="breadth-chip bear">${sum.near_lows.length} at 52w lows</span>`);
  if ((sum.errors || []).length) chips.push(`<span class="breadth-chip warn">${sum.errors.length} no data</span>`);
  $("#tech-breadth").innerHTML = chips.join("");

  let rows = state.positions.map(techRowFromPosition)
    .filter((r) => r.trend || r.score !== undefined);
  const f = state.techFilter;
  if (f.side !== "all") rows = rows.filter((r) => (r.side || "none") === f.side);
  if (f.conflicts) rows = rows.filter((r) => r.side && ((r.context || {})[r.side] || {}).alignment === "hostile");
  if (f.q) {
    const q = f.q.toLowerCase();
    rows = rows.filter((r) => `${r.ticker} ${r.name || ""} ${r.headline || ""} ${r.setup_label || ""} ${r.sector_key || ""}`
      .toLowerCase().includes(q));
  }
  const order = { position: 0, watch: 1 };
  const alignRank = { supportive: 0, mixed: 1, hostile: 2 };
  rows.sort((a, b) => {
    if (f.sort === "score") return (b.score || 0) - (a.score || 0);
    if (f.sort === "weakest") return (a.score || 0) - (b.score || 0);
    if (f.sort === "ticker") return a.ticker.localeCompare(b.ticker);
    if (f.sort === "alignment") {
      const ra = alignRank[(a.context || {})[a.side]?.alignment] ?? 9;
      const rb = alignRank[(b.context || {})[b.side]?.alignment] ?? 9;
      return ra - rb || (b.score || 0) - (a.score || 0);
    }
    return (order[a.kind] ?? 2) - (order[b.kind] ?? 2) || a.ticker.localeCompare(b.ticker);
  });
  const hasBook = state.positions.length > 0;
  const loading = hasBook && state.positions.some(
    (p) => !state.technicals[p.ticker] && !state.techFull.has(p.ticker));
  if (!rows.length) {
    grid.innerHTML = "";
    empty.hidden = false;
    const scanBtn = $("#tech-empty-scan");
    if (!hasBook) {
      empty.querySelector("h3").textContent = "No tickers yet";
      empty.querySelector("p").textContent = "Add positions or watchlist names in the left rail to start reading price action.";
      scanBtn.hidden = true;
    } else if (loading) {
      empty.querySelector("h3").textContent = "No technical data yet";
      empty.querySelector("p").textContent = "The technical scanner reads a year of daily bars for every position and watchlist name. It runs with each news scan; you can also run it on its own.";
      scanBtn.hidden = false;
    } else if (f.side !== "all") {
      empty.querySelector("h3").textContent = `No ${f.side}-tagged names`;
      empty.querySelector("p").textContent = `Click the L/S pill in the rail (or edit the side in a ticker card) to tag a ${f.side} — the technical read is then judged against it.`;
      scanBtn.hidden = true;
    } else if (f.conflicts) {
      empty.querySelector("h3").textContent = "No conflicts right now";
      empty.querySelector("p").textContent = "Every tagged long/short matches its technical bias. Uncheck “conflicts only” to see the whole book.";
      scanBtn.hidden = true;
    } else {
      empty.querySelector("h3").textContent = "No names match this filter";
      empty.querySelector("p").textContent = "Clear the search or loosen the filters.";
      scanBtn.hidden = true;
    }
    return;
  }
  empty.hidden = true;
  grid.innerHTML = rows.map(techCard).join("");
  $$(".tech-card", grid).forEach((el) => {
    el.onclick = () => openTicker(el.dataset.ticker);
  });
}

let techReloadTimer = null;
function scheduleTechReload(delay = 400) {
  clearTimeout(techReloadTimer);
  techReloadTimer = setTimeout(() => {
    if (state.view === "tech") loadTechnicals();
  }, delay);
}

async function loadTechnicals() {
  if (state.techLoading) return;
  state.techLoading = true;
  try {
    const d = await api("/api/technicals");
    state.techRows = d.technicals || [];
    state.techFull = new Map(state.techRows.map((r) => [r.ticker, r]));
    state.techSummary = d.summary || state.techSummary;
    state.techAt = d.at || state.techAt;
    state.techLoadedAt = Date.now();
    renderTechView();
  } catch (err) {
    showToast(`Technical data: ${err.message}`, true);
  } finally {
    state.techLoading = false;
  }
}

function setView(view) {
  state.view = view;
  $("#news-view").hidden = view !== "news";
  $("#tech-view").hidden = view !== "tech";
  $$("#view-tabs button").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  $("#view-hint").textContent = view === "tech"
    ? "daily bars · trend · momentum · volatility · levels"
    : "news scan · macro, sectors, tickers, peers";
  if (view === "tech") {
    renderTechView();
    loadTechnicals();
  }
}

async function runTechScan() {
  try {
    const r = await api("/api/technicals/scan", { method: "POST" });
    showToast(r.message || (r.ok ? "Technical scan started" : "Could not start scan"), !r.ok);
    if (r.ok) {
      const btn = $("#tech-rescan");
      btn.disabled = true;
      setTimeout(() => { btn.disabled = false; loadTechnicals(); }, 9000);
    }
  } catch (err) {
    showToast(String(err.message), true);
  }
}

function renderTickerTech(snap, ticker, pos) {
  const el = $("#tm-tech");
  if (!el) return;
  if (!snap) {
    el.innerHTML = `<div class="muted small">No technical snapshot for ${esc(ticker)} yet.
      It is computed on the next scan — or hit “Rescan” in the Technicals tab.</div>`;
    return;
  }
  if (snap.error) {
    el.innerHTML = `<div class="muted small">${esc(snap.error)}</div>`;
    return;
  }
  const tone = techTone(snap);
  const score = Math.round(Number(snap.score) || 0);
  const posPct = Math.max(0, Math.min(100, (score + 100) / 2));
  const t = snap.trend_detail || {};
  const m = snap.momentum || {};
  const v = snap.volatility || {};
  const vol = snap.volume || {};
  const r = snap.relative || {};
  const lv = snap.levels || {};
  const side = pos?.side || null;
  const fmtN = (x, d = 0) => (x === null || x === undefined || Number.isNaN(Number(x))) ? null : Number(x).toFixed(d);
  const kv = [
    ["RSI 14", fmtN(m.rsi14, 0)],
    ["RSI zone", m.rsi_zone ? `${m.rsi_zone}${m.rsi_chg5 != null ? ` (${m.rsi_chg5 > 0 ? "+" : ""}${m.rsi_chg5} in 5d)` : ""}` : null],
    ["MACD", m.macd_state],
    ["ADX", fmtN(t.adx, 0)],
    ["ATR %", fmtN(v.atr_pct, 1)],
    ["Volatility", v.regime],
    ["Volume vs 20d", vol.ratio != null ? `${fmtN(vol.ratio, 2)}×` : null],
    ["OBV", vol.obv_trend],
    ["20-day", fmtPct(m.roc20_pct)],
    ["60-day", fmtPct(m.roc60_pct)],
    [`${r.benchmark || "Index"} 20d`, fmtPct(r.rs20)],
    ["From 52w high", fmtPct(lv.from_high_pct)],
    ["SMA 20 / 50 / 200", [t.sma20, t.sma50, t.sma200].map((x) => x != null ? fmtPrice(x) : "—").join(" / ")],
    ["BB width %ile", fmtN(v.bb_width_pctile, 0)],
  ].filter(([, value]) => value !== null && value !== undefined && value !== "");
  const levels = [
    ...(lv.resistance || []).map((x) => ["R", x]),
    ...(lv.support || []).map((x) => ["S", x]),
  ].map(([tag, x]) => `<div class="level-row">
      <span class="lv ${tag === "R" ? "r" : "s"}">${tag} ${fmtPrice(x.price)}</span>
      <span class="ld">${esc(x.kind || "")}</span>
      <span class="lp">${fmtPct(x.distance_pct)}</span>
      <span class="lm">${x.atr_mult != null ? `${x.atr_mult} ATR` : ""}</span>
    </div>`).join("");
  const signals = (snap.signals || []).map((sig) =>
    `<span class="signal-chip ${esc(sig.tone || "")}">${esc(sig.label)}</span>`).join("");
  const risks = (snap.risks || []).map((x) => `<div class="muted small">⚠ ${esc(x)}</div>`).join("");
  const contexts = ["long", "short"].map((s) => {
    const c = (snap.context || {})[s] || {};
    return `<div class="side-ctx ${side === s ? "active" : ""}">
      <div class="sc-head">
        <span class="side-pill sm pill-${s}">${s === "long" ? "L" : "S"}</span>
        <span>${s}</span>
        <span class="muted" style="margin-left:auto;font-family:var(--sans)">${esc(c.alignment_label || "")}</span>
      </div>
      <div class="sc-verdict">${esc(c.verdict || "")}</div>
      <div class="sc-meta">${c.stop != null ? `stop ${fmtPrice(c.stop)} · ` : ""}${c.target != null ? `target ${fmtPrice(c.target)}` : ""}</div>
    </div>`;
  }).join("");
  el.innerHTML = `<div class="tech-panel">
    <div class="tech-top">
      <span class="tech-bias ${esc(snap.bias || "")}">${esc(snap.bias || "")} ${score > 0 ? "+" : ""}${score}</span>
      <span class="muted small">${esc(snap.trend_label || snap.trend || "")} · ${esc(snap.setup_label || snap.setup || "")}</span>
      <span class="tech-scale"><i></i><b style="left:${posPct}%"></b></span>
      <span class="muted small">${snap.provisional ? "intraday" : "close"} ${esc(snap.bar_date || "")}</span>
    </div>
    <div class="tc-spark" style="height:64px">${sparklineSVG(snap.spark, tone, 64)}</div>
    <div class="tech-kv">${kv.map(([k, value]) =>
      `<div><div class="k">${esc(k)}</div><div class="v">${esc(value)}</div></div>`).join("")}</div>
    ${signals ? `<div class="signal-chips">${signals}</div>` : ""}
    ${levels ? `<div class="level-grid">${levels}</div>` : ""}
    <div class="tech-narrative">${esc(snap.narrative || "")}</div>
    ${snap.read ? `<div class="tech-llm">Desk read: ${esc(snap.read)}</div>` : ""}
    ${risks ? `<div>${risks}</div>` : ""}
    <div>
      <div class="muted small" style="margin-bottom:5px">Position context${side ? "" : " — no side tagged, showing both"}</div>
      <div class="side-context-grid">${contexts}</div>
    </div>
  </div>`;
}

/* ============================================================ settings */
const SETTINGS_SCHEMA = [
  { key: "auto_refresh", label: "Auto refresh (server)", type: "check" },
  { key: "scan_interval_seconds", label: "Refresh interval (s)", type: "number", min: 30 },
  { key: "item_max_age_hours", label: "Max headline age (h)", type: "number", min: 1 },
  { key: "max_items_per_feed", label: "Items per feed", type: "number", min: 5 },
  { key: "peer_limit", label: "Peers per ticker", type: "number", min: 0 },
  { key: "llm_enabled", label: "Ollama (master switch)", type: "check" },
  { key: "llm_classify", label: "LLM headline scoring", type: "check" },
  { key: "llm_model_fast", label: "Fast model (scoring)", type: "text" },
  { key: "llm_model_deep", label: "Deep model (mapping/brief)", type: "text" },
  { key: "llm_max_analyze", label: "Max items scored / scan", type: "number", min: 0 },
  { key: "llm_min_rule", label: "Rule-score floor for LLM", type: "number", min: 0 },
  { key: "llm_batch_size", label: "Headlines per LLM call", type: "number", min: 4 },
  { key: "llm_brief", label: "Desk note", type: "check" },
  { key: "llm_brief_min_minutes", label: "Desk note min age (min)", type: "number", min: 0 },
  { key: "llm_brief_min_new", label: "Desk note min new items", type: "number", min: 0 },
  { key: "llm_brief_keep", label: "Desk notes kept", type: "number", min: 0 },
  { key: "web_search_enabled", label: "Ollama web search", type: "check" },
  { key: "discovery_enabled", label: "LLM news discovery", type: "check" },
  { key: "discovery_per_scan", label: "Discovery tickers / scan", type: "number", min: 0 },
  { key: "sec_enabled", label: "SEC EDGAR filings", type: "check" },
  { key: "stocktwits_enabled", label: "StockTwits (social)", type: "check" },
  { key: "tech_enabled", label: "Technical scanner", type: "check" },
  { key: "tech_llm", label: "LLM technical read", type: "check" },
  { key: "tech_refresh_minutes", label: "Daily-bar cache (min)", type: "number", min: 5 },
  { key: "tech_bars", label: "Bars per ticker", type: "number", min: 60 },
  { key: "tech_benchmark", label: "Relative-strength benchmark", type: "text" },
  { key: "macro_enabled", label: "Macro desk (yields & data)", type: "check" },
  { key: "macro_refresh_minutes", label: "Macro refresh (min)", type: "number", min: 5 },
  { key: "macro_calendar", label: "US data calendar", type: "check" },
  { key: "macro_brief_trigger", label: "Desk note on rates move", type: "check" },
];

async function openSettings() {
  const modal = $("#settings-modal");
  const s = await api("/api/settings");
  state.settings = s;
  $("#settings-grid").innerHTML = SETTINGS_SCHEMA.map((f) => {
    const v = s[f.key] ?? "";
    if (f.type === "check") {
      return `<div class="setting check-row"><input type="checkbox" id="set-${f.key}" ${v === "1" ? "checked" : ""} />
        <label for="set-${f.key}">${esc(f.label)}</label></div>`;
    }
    return `<div class="setting"><label>${esc(f.label)}</label>
      <input type="${f.type}" id="set-${f.key}" value="${esc(v)}" ${f.min ? `min="${f.min}"` : ""} /></div>`;
  }).join("");
  modal.showModal();
  loadHealth();
  loadUsage();
}

async function loadUsage() {
  try {
    const st = await api("/api/state");
    const u = st.llm_usage || {};
    const st_stats = st.scanner?.last_stats || {};
    const tags = Object.entries(u.by_tag || {})
      .sort((a, b) => b[1].in - a[1].in)
      .map(([tag, v]) => `${tag}: ${v.calls} calls · ${fmtTokens(v.in + v.out)}`)
      .join(" · ");
    $("#llm-usage").innerHTML = `
      <div><strong>${fmtTokens(st_stats.llm_tokens_in + st_stats.llm_tokens_out || 0)}</strong>
        last scan (${st_stats.llm_calls || 0} calls)</div>
      <div class="muted">session total ${fmtTokens((u.tokens_in || 0) + (u.tokens_out || 0))}
        over ${u.calls || 0} calls</div>
      ${tags ? `<div class="muted small">${esc(tags)}</div>` : ""}`;
  } catch { /* ignore */ }
}

function fmtTokens(n) {
  if (!n) return "0";
  return n >= 1e6 ? `${(n / 1e6).toFixed(2)}M` : n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);
}

async function saveSettings() {
  const payload = {};
  SETTINGS_SCHEMA.forEach((f) => {
    const el = $(`#set-${f.key}`);
    if (!el) return;
    payload[f.key] = f.type === "check" ? (el.checked ? "1" : "0") : el.value;
  });
  state.settings = await api("/api/settings", { method: "POST", body: payload });
  state.nextScanAt = Date.now() + (+state.settings.scan_interval_seconds || 180) * 1000;
  syncControls(); renderStatus();
  showToast("Settings saved");
}

async function loadHealth() {
  try {
    const h = await api("/api/health");
    const rows = (h.sources || []).slice(0, 40);
    $("#health-table").innerHTML = rows.length
      ? rows.map((r) => `<div class="health-row">
          <span class="hn">${esc(r.name || r.source_id)}</span>
          <span class="${r.last_ok ? "hok" : ""}">${r.last_ok ? "ok" : "—"}</span>
          <span class="${r.last_error ? "hbad" : "muted"}" title="${esc(r.last_error || "")}">${
            r.last_error ? `${r.err_count} err` : `${r.total_items || 0} items`}</span>
        </div>`).join("")
      : `<span class="muted small">No scans yet.</span>`;
  } catch { /* ignore */ }
}

/* ============================================================ toast */
let toastTimer = null;
function showToast(text, isError = false) {
  let el = $("#toast");
  if (!el) {
    el = document.createElement("div");
    el.id = "toast";
    el.style.cssText = `position:fixed;bottom:18px;left:50%;transform:translateX(-50%);
      background:#16202c;border:1px solid #2c3f57;color:#e8eef7;padding:9px 16px;border-radius:8px;
      font-size:12px;z-index:100;box-shadow:0 10px 30px rgba(0,0,0,.5);max-width:80vw;`;
    document.body.appendChild(el);
  }
  el.textContent = text;
  el.style.borderColor = isError ? "#f05252" : "#2c3f57";
  el.style.opacity = "1";
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.style.opacity = "0"; }, 3200);
}

/* ============================================================ ticker clock */
function startTickerClock() {
  setInterval(() => {
    $$("[data-ts]").forEach((el) => { el.textContent = ago(el.dataset.ts); });
    if (state.scanner.last_scan_at) {
      const bits = [`scan ${ago(state.scanner.last_scan_at)}`];
      if (state.scanner.last_stats?.new !== undefined) bits.push(`${state.scanner.last_stats.new} new`);
      if (state.nextScanAt && state.settings.auto_refresh === "1") {
        const s = Math.max(0, Math.round((state.nextScanAt - Date.now()) / 1000));
        bits.push(`next ${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`);
      }
      if (!state.scanner.scanning) $("#status-sub").textContent = bits.join(" · ");
    }
    if (state.scanner.brief_at) $("#brief-time").textContent = ago(state.scanner.brief_at);
  }, 1000);
}

/* ============================================================ events */
$("#btn-scan").onclick = async () => {
  try {
    const r = await api("/api/scan", { method: "POST" });
    if (!r.ok) showToast(r.message || "already scanning", true);
    else { state.nextScanAt = Date.now() + (+state.settings.scan_interval_seconds || 180) * 1000; renderStatus(); }
  } catch (e) { showToast(String(e.message), true); }
};

$("#auto-refresh").onchange = async (e) => {
  state.settings = await api("/api/settings", { method: "POST", body: { auto_refresh: e.target.checked ? "1" : "0" } });
  state.nextScanAt = Date.now() + (+state.settings.scan_interval_seconds || 180) * 1000;
  renderStatus();
};

$("#interval-select").onchange = async (e) => {
  state.settings = await api("/api/settings", { method: "POST", body: { scan_interval_seconds: e.target.value } });
  state.nextScanAt = Date.now() + (+state.settings.scan_interval_seconds || 180) * 1000;
  renderStatus();
};

/* ------------------------------------------------------------ view switch */
$("#view-tabs").onclick = (e) => {
  const btn = e.target.closest("button[data-view]");
  if (btn) setView(btn.dataset.view);
};

$("#tech-side-tabs").onclick = (e) => {
  const btn = e.target.closest("button[data-side]");
  if (!btn) return;
  state.techFilter.side = btn.dataset.side;
  $$("#tech-side-tabs button").forEach((b) => b.classList.toggle("active", b === btn));
  renderTechView();
};
$("#tech-conflicts").onchange = (e) => { state.techFilter.conflicts = e.target.checked; renderTechView(); };
$("#tech-sort").onchange = (e) => { state.techFilter.sort = e.target.value; renderTechView(); };
$("#tech-search").oninput = debounce((e) => { state.techFilter.q = e.target.value.trim(); renderTechView(); }, 160);
$("#tech-rescan").onclick = runTechScan;
$("#tech-empty-scan").onclick = runTechScan;

$("#add-form").onsubmit = async (e) => {
  e.preventDefault();
  const raw = $("#add-input").value.trim();
  if (!raw) return;
  const tickers = raw.toUpperCase().split(/[\s,]+/).filter(Boolean);
  $("#add-input").value = "";
  $("#suggest").hidden = true;
  const side = $("#add-side").value;
  $("#add-side").value = "";
  await addTickers(tickers, $("#add-kind").value, side);
};

let suggestTimer = null;
$("#add-input").oninput = (e) => {
  const q = e.target.value.trim();
  clearTimeout(suggestTimer);
  if (q.length < 1) { $("#suggest").hidden = true; return; }
  suggestTimer = setTimeout(async () => {
    try {
      const r = await api(`/api/search?q=${encodeURIComponent(q)}`);
      const res = (r.results || []).slice(0, 8);
      if (!res.length) { $("#suggest").hidden = true; return; }
      $("#suggest").innerHTML = res.map((x) => `<div class="suggest-item" data-t="${esc(x.ticker)}">
        <span class="s-sym">${esc(x.ticker)}</span>
        <span class="s-sub">${esc(x.name || "")} ${x.sub_industry ? `· ${esc(x.sub_industry)}` : ""}</span></div>`).join("");
      $("#suggest").hidden = false;
      $$(".suggest-item", $("#suggest")).forEach((el) => el.onclick = async () => {
        $("#suggest").hidden = true;
        $("#add-input").value = "";
        await addTickers([el.dataset.t], $("#add-kind").value, $("#add-side").value);
      });
    } catch { $("#suggest").hidden = true; }
  }, 260);
};
$("#add-input").onblur = () => setTimeout(() => { $("#suggest").hidden = true; }, 220);

$("#scope-tabs").onclick = (e) => {
  const btn = e.target.closest("button[data-scope]");
  if (!btn) return;
  $$("#scope-tabs button").forEach((b) => b.classList.toggle("active", b === btn));
  state.filters.scope = btn.dataset.scope;
  loadFeed();
};

$("#search").oninput = debounce((e) => {
  state.filters.q = e.target.value.trim();
  loadFeed();
}, 320);

// publish-time key for sorting; undated items fall to the end of either direction
function pubKey(it, missing) {
  const t = it.published_at ? Date.parse(it.published_at) : NaN;
  return Number.isNaN(t) ? missing : t;
}

const SORT_API = {
  smart: "smart", published: "published", published_asc: "published_asc",
  added: "added", relevance: "relevance",
};

function applySort(items, mode) {
  const f = mode || state.filters.sort;
  if (f === "relevance") return items.sort((a, b) => (b.relevance || 0) - (a.relevance || 0));
  if (f === "smart") {
    const score = (it) => {
      const ageH = it.published_at
        ? Math.max(0, (Date.now() - (Date.parse(it.published_at) || Date.now())) / 3.6e6)
        : 24;
      return (it.relevance || 0) + Math.max(0, 16 - ageH * 1.1);
    };
    return items.sort((a, b) => score(b) - score(a));
  }
  if (f === "added") {
    return items.sort((a, b) => (Date.parse(b.fetched_at) || 0) - (Date.parse(a.fetched_at) || 0));
  }
  if (f === "published_asc") return items.sort((a, b) => pubKey(a, Infinity) - pubKey(b, Infinity));
  return items.sort((a, b) => pubKey(b, -Infinity) - pubKey(a, -Infinity));
}

$("#hours").onchange = (e) => {
  state.filters.hours = +e.target.value;
  loadFeed();
};

$("#sort").onchange = (e) => {
  state.filters.sort = e.target.value;
  loadFeed();          // server-side ordering decides which 220 rows we get
};

$("#min-rel").oninput = (e) => {
  state.filters.minRel = +e.target.value;
  $("#min-rel-val").textContent = e.target.value;
};
$("#min-rel").onchange = () => loadFeed();

$("#hide-market").onchange = (e) => {
  state.filters.hideMarket = e.target.checked;
  loadFeed();
};

$("#btn-settings").onclick = openSettings;
$("#btn-health").onclick = async () => {
  await openSettings();
  $("#health-table").scrollIntoView({ behavior: "smooth", block: "center" });
};
$("#settings-save").onclick = saveSettings;
$("#settings-test-llm").onclick = async () => {
  $("#llm-result").textContent = "testing…";
  try {
    const r = await api("/api/llm/test", { method: "POST" });
    $("#llm-result").textContent = JSON.stringify(r, null, 1).slice(0, 1400);
  } catch (e) { $("#llm-result").textContent = String(e.message); }
};
$("#settings-rebuild").onclick = async () => {
  $("#llm-result").textContent = "rebuilding GICS index from Wikipedia…";
  try {
    const r = await api("/api/industry/rebuild", { method: "POST" });
    $("#llm-result").textContent = `GICS index rebuilt: ${r.rows} tickers`;
    state.gicsRows = r.rows;
  } catch (e) { $("#llm-result").textContent = String(e.message); }
};

$("#brief-refresh").onclick = async (e) => {
  const btn = e.target;
  btn.disabled = true; btn.textContent = "writing…";
  try {
    const r = await api("/api/brief?force=true", { method: "POST" });
    if (r.brief) {
      state.scanner.brief = r.brief;
      state.scanner.brief_at = r.at || state.scanner.brief_at;
      if (r.count !== undefined) state.scanner.brief_count = r.count;
      renderBrief();
    }
  } catch (err) { showToast("Desk note failed", true); }
  btn.disabled = false; btn.textContent = "regenerate";
};

$("#brief-hide").onclick = () => {
  const hidden = localStorage.getItem("brief-hidden") === "1";
  localStorage.setItem("brief-hidden", hidden ? "0" : "1");
  renderBrief();
};

/* ============================================================ macro tape */
$("#macro-refresh").onclick = async (e) => {
  const btn = e.currentTarget;
  const label = btn.textContent;
  btn.disabled = true; btn.textContent = "fetching…";
  try {
    const r = await api("/api/macro?refresh=true");
    state.macro = r.macro || state.macro;
    state.macroAt = r.at || state.macroAt;
    renderMacro();
  } catch (err) {
    showToast("Macro refresh failed", true);
  }
  btn.disabled = false; btn.textContent = label;
};

/* ============================================================ brief history */
async function openBriefHistory() {
  const modal = $("#brief-modal");
  const list = $("#brief-history-list");
  list.innerHTML = `<div class="muted small">loading…</div>`;
  modal.showModal();
  try {
    const r = await api("/api/briefs?limit=100");
    const rows = r.briefs || [];
    state.scanner.brief_count = r.count;
    $("#bh-count").textContent = rows.length
      ? `${r.count} archived note${r.count === 1 ? "" : "s"} · newest first · click a note to expand`
      : "no notes archived yet";
    if (!rows.length) {
      list.innerHTML = `<div class="empty-note">No desk notes archived yet.
        One is saved every time a note is written (each scan that regenerates it, or “regenerate”).</div>`;
      return;
    }
    list.innerHTML = rows.map((b, i) => `
      <article class="bh-item${i === 0 ? " latest" : ""}" data-id="${b.id}">
        <div class="bh-meta">
          <span class="bh-when">${esc(stamp(b.created_at))}</span>
          <span>${esc(ago(b.created_at))}</span>
          ${i === 0 ? `<span class="chip">latest</span>` : ""}
          <span class="grow"></span>
          <span>${esc(b.model || "")}${b.trigger ? ` · ${esc(b.trigger)}` : ""}</span>
        </div>
        <div class="bh-text">${esc(b.text)}</div>
        ${b.macro && b.macro.line ? `<div class="bh-macro">${esc(b.macro.line)}</div>` : ""}
      </article>`).join("");
    $$(".bh-item", list).forEach((el) => el.onclick = () => el.classList.toggle("open"));
  } catch (err) {
    list.innerHTML = `<div class="muted small">error: ${esc(err.message)}</div>`;
  }
}

$("#brief-history").onclick = openBriefHistory;

$$("[data-close]").forEach((b) => b.onclick = () => b.closest("dialog").close());

document.addEventListener("keydown", (e) => {
  if (e.target.matches("input, textarea, select")) return;
  if (e.key === "s" || e.key === "S") { e.preventDefault(); $("#btn-scan").click(); }
  if (e.key === "/") { e.preventDefault(); $("#search").focus(); }
  if (e.key === "t" || e.key === "T") setView(state.view === "tech" ? "news" : "tech");
  if (e.key === "n" || e.key === "N") setView("news");
});

function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

boot();

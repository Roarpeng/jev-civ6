/* Jev × Civ VI war-room UI — incremental timeline (append-only refresh) */
"use strict";

const $ = (sel) => document.querySelector(sel);
const timeline = $("#timeline");
const openKeys = new Set();   // expanded details / results survive re-render
let filterType = "all";
let lastEventId = 0;          // max event id already in the DOM (per filter)
let lastTurnRendered = null;  // for divider continuity when appending

const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const confClass = (c) => (c >= 0.8 ? "hi" : c >= 0.5 ? "mid" : "lo");
const fmtTime = (iso) => {
  const d = new Date(iso);
  return isNaN(d) ? "" : d.toLocaleTimeString("zh-CN", { hour12: false });
};
const fmtTok = (n) => (n >= 1000 ? (n / 1000).toFixed(1) + "k" : String(n));

/* Update a panel only when its HTML actually changed — prevents the 4s poll
   from re-running bar animations and flickering the sidebar. */
function setHTMLIfChanged(el, html) {
  if (el.__h !== html) {
    el.__h = html;
    el.innerHTML = html;
  }
}

async function getJSON(url) {
  try {
    const r = await fetch(url);
    if (!r.ok) throw new Error(r.status);
    return await r.json();
  } catch {
    return null;
  }
}

/* ── event card renderers ────────────────────────────────────────── */

function jevQuestionHTML(qid, spec, ans) {
  const kind = (spec && spec.type) || "choice";
  const kindTag = `<span class="q-kind">${kind.toUpperCase()}</span>`;
  let body;
  if (!ans) {
    body = `<div class="q-error">无响应（调用失败）</div>`;
  } else if (kind === "noul") {
    const p = ans.noul ?? 0;
    body = `
      <span class="q-answer">P(yes) = ${p.toFixed(2)}</span>
      ${pbars([["yes", p, true]])}`;
  } else if (kind === "score") {
    const levels = (spec.criteria || []).map(String);
    const probs = Object.entries(ans.probabilities || {}).sort((a, b) => b[1] - a[1]);
    body = `
      <span class="q-answer">score = ${ans.score?.toFixed?.(2) ?? ans.score}</span>
      ${pbars(probs.map(([k, v]) => [levels[+k] ?? String(k), v, +k === Math.round(ans.score)]))}`;
  } else {
    const probs = Object.entries(ans.probabilities || {}).sort((a, b) => b[1] - a[1]);
    const conf = ans.confidence;
    body = `
      <span class="q-answer">${esc(ans.choice)}</span>
      <span class="conf ${confClass(conf)}">conf ${conf?.toFixed?.(2) ?? "—"}</span>
      ${pbars(probs.map(([k, v]) => [k, v, k === ans.choice]))}`;
  }
  return `<div class="q">
    <span class="q-id">${esc(qid)}</span>${kindTag}
    ${body}
  </div>`;
}

function pbars(rows) {
  return `<div class="pbars">` + rows.slice(0, 8).map(([label, v, win]) => `
    <div class="prow">
      <span class="label ${win ? "win" : ""}" title="${esc(label)}">${esc(label)}</span>
      <div class="track"><div class="fill ${win ? "win" : ""}" data-w="${(v * 100).toFixed(1)}"></div></div>
      <span class="pct ${win ? "win" : ""}">${(v * 100).toFixed(0)}%</span>
    </div>`).join("") + `</div>`;
}

function jevCard(ev) {
  const d = ev.data;
  const resp = d.response || {};
  const answers = resp.answers || {};
  const questions = (d.request && d.request.questions) || {};
  const usage = resp.usage || {};
  const tok = usage.input_tokens || usage.output_tokens
    ? `${fmtTok(usage.input_tokens || 0)}→${fmtTok(usage.output_tokens || 0)} tok` : "";
  const err = d.error
    ? `<span class="badge b-error">ERROR</span>`
    : `<span class="badge">JUDGMENT</span>`;
  const title = d.meta && d.meta.title ? ` · ${esc(d.meta.title)}` : "";
  const qs = Object.keys(questions).map((qid) =>
    jevQuestionHTML(qid, questions[qid], answers[qid])).join("");
  const key = `jev:${ev.id}`;
  return `<article class="card t-jev" data-ev="${ev.id}">
    <div class="card-head">${err}
      <span>jev ${esc((d.request && d.request.model) || "")}</span>
      <span>${tok}</span>${d.latency_ms ? `<span>${d.latency_ms}ms</span>` : ""}
      <span class="right">${fmtTime(ev.ts)}</span>${title}
    </div>
    ${qs || `<div class="q-error">${esc(d.error || "empty")}</div>`}
    <details class="raw" data-key="${key}" ${openKeys.has(key) ? "open" : ""}>
      <summary>RAW PAYLOAD</summary><pre>${esc(JSON.stringify(d, null, 2))}</pre>
    </details>
  </article>`;
}

function resultText(res) {
  if (res == null) return "";
  if (typeof res === "string") return res;
  if (typeof res === "object" && typeof res.result === "string") return res.result;
  return JSON.stringify(res);
}

function actionCard(ev) {
  const d = ev.data;
  const res = resultText(d.result);
  const isErr = /^error|cannot|blocked|fail/i.test(res);
  const cls = isErr ? "err" : res ? "ok" : "";
  const args = JSON.stringify(d.args ?? {});
  const key = `res:${ev.id}`;
  return `<article class="card t-action" data-ev="${ev.id}">
    <div class="card-head">
      <span class="badge ${isErr ? "b-error" : "b-action"}">${isErr ? "FAILED" : "ACTION"}</span>
      <span class="action-tool">${esc(d.tool)}</span>
      <span class="right">${fmtTime(ev.ts)}</span>
    </div>
    <div class="action-line"><span class="action-args">${esc(args)}</span></div>
    ${res ? `<div class="result ${cls}" data-key="${key}">${esc(res)}</div>` : ""}
  </article>`;
}

function stateSummary(d) {
  const s = (d && d.snapshot) || {};
  const bits = [];
  if (s.civ) bits.push(`${s.civ}${s.leader ? " · " + s.leader : ""}`);
  if (s.cities?.length) bits.push(`${s.cities.length} city · ` +
    s.cities.map((c) => `${c.name}(${c.pop})`).join(", "));
  if (s.units?.length) bits.push(`${s.units.length} unit`);
  if (s.threats?.length) bits.push(`⚠ ${s.threats.length} threat`);
  return bits.join(" · ");
}

function stateCard(ev) {
  const d = ev.data;
  const key = `state:${ev.id}`;
  return `<article class="card t-state" data-ev="${ev.id}">
    <div class="card-head">
      <span class="badge b-state">STATE</span>
      <span>${esc(stateSummary(d) || "snapshot")}</span>
      <span class="right">${fmtTime(ev.ts)}</span>
    </div>
    <details class="raw" data-key="${key}" ${openKeys.has(key) ? "open" : ""}>
      <summary>SNAPSHOT JSON</summary><pre>${esc(JSON.stringify(d.snapshot, null, 2))}</pre>
    </details>
  </article>`;
}

function gateCard(ev) {
  const r = (ev.data && ev.data.result) || {};
  const ask = r.should_ask;
  const trig = (r.triggers || []).map((t) =>
    `<span class="trigger ${ask ? "hot" : ""}">${esc(t)}</span>`).join("");
  return `<article class="card t-gate" data-ev="${ev.id}">
    <div class="card-head">
      <span class="badge ${ask ? "" : "b-state"}">${ask ? "GATE · ASK" : "GATE · SKIP"}</span>
      <span>${ask
        ? `${r.question_ids.length} 个决策点 → 定向调用 Jev`
        : esc(r.skip_reason || "无决策点，未调用 Jev")}</span>
      <span class="right">${fmtTime(ev.ts)}</span>
    </div>
    ${trig ? `<div class="triggers">${trig}</div>` : ""}
  </article>`;
}

function eventCardHTML(ev) {
  return ev.type === "jev" ? jevCard(ev)
    : ev.type === "action" ? actionCard(ev)
    : ev.type === "gate" ? gateCard(ev)
    : stateCard(ev);
}

/* ── timeline: newest-on-top, prepend-only refresh ────────────────── */

function wire(scope) {
  scope.querySelectorAll(".fill[data-w]").forEach((f) => {
    requestAnimationFrame(() => { f.style.width = f.dataset.w + "%"; });
  });
  scope.querySelectorAll("details.raw").forEach((d) => {
    d.open = openKeys.has(d.dataset.key);
    d.addEventListener("toggle", () =>
      d.open ? openKeys.add(d.dataset.key) : openKeys.delete(d.dataset.key));
  });
  scope.querySelectorAll(".result").forEach((r) => {
    if (openKeys.has(r.dataset.key)) r.classList.add("open");
    r.addEventListener("click", () => {
      r.classList.toggle("open");
      r.classList.contains("open") ? openKeys.add(r.dataset.key) : openKeys.delete(r.dataset.key);
    });
  });
}

function dividerHTML(turn) {
  return `<div class="turn-divider">TURN ${esc(turn)}</div>`;
}

function renderTimeline(events) {  // events: newest-first from the API
  const asc = [...events].reverse();
  const gap = asc.length && lastEventId > 0 && asc[0].id > lastEventId + 1;

  if (lastEventId === 0 || gap || !timeline.querySelector(".card")) {
    fullRender(events);
    return;
  }
  const fresh = events.filter(
    (e) => e.id > lastEventId && (filterType === "all" || e.type === filterType));
  if (!fresh.length) return;

  // Build the block in chronological order (oldest→newest), emitting a
  // divider whenever the turn changes — "previous" starts at the turn of
  // the card currently on top, so a continuing turn gets no divider.
  let prevTurn = lastTurnRendered;
  const pieces = [];
  let seen = lastEventId;
  for (const ev of asc) {
    if (ev.id <= lastEventId) continue;
    seen = Math.max(seen, ev.id);
    if (filterType !== "all" && ev.type !== filterType) continue;
    const turn = ev.turn ?? "?";
    if (turn !== prevTurn) {
      pieces.push(dividerHTML(turn));
      prevTurn = turn;
    }
    pieces.push(eventCardHTML(ev));
  }
  lastEventId = Math.max(seen, lastEventId);
  if (!pieces.length) return;
  lastTurnRendered = prevTurn;
  // newest must end up on top → prepend the reversed block
  timeline.insertAdjacentHTML("afterbegin", pieces.reverse().join(""));
  wire(timeline);
}

function fullRender(events) {  // events: newest-first
  const visible = filterType === "all"
    ? events : events.filter((e) => e.type === filterType);
  if (!visible.length) {
    timeline.innerHTML = `<div class="empty"><div class="empty-glyph">❖</div>
      <p>没有匹配的记录。</p></div>`;
    lastTurnRendered = null;
  } else {
    let html = "";
    let prevTurn = null;
    for (const ev of visible) {  // newest → oldest, divider above its group
      const turn = ev.turn ?? "?";
      if (turn !== prevTurn) {
        html += dividerHTML(turn);
        prevTurn = turn;
      }
      html += eventCardHTML(ev);
    }
    timeline.innerHTML = html;
    wire(timeline);
    lastTurnRendered = visible[0].turn ?? "?";
  }
  lastEventId = events.length ? events[0].id : 0;
}

/* ── sidebar ─────────────────────────────────────────────────────── */

function renderBattlefield(latest) {
  const box = $("#battlefield");
  if (!latest) {
    setHTMLIfChanged(box, `<p class="muted">等待战报……（POST /api/state）</p>`);
    return;
  }
  const s = latest.data.snapshot || {};
  const y = s.yields || {};
  const yields = [
    ["🪙 " + (y.gold ?? "—"), "GOLD"],
    ["✦ " + (y.science ?? "—"), "SCI"],
    ["❖ " + (y.culture ?? "—"), "CUL"],
    ["✜ " + (y.faith ?? "—"), "FAITH"],
  ].map(([v, k]) => `<div class="yield"><span class="v">${v}</span><span class="k">${k}</span></div>`).join("");

  const res = s.research;
  const resHTML = res ? `
    <div class="sect-label">RESEARCH · ${esc(res.name)}（剩 ${esc(res.turns_left ?? "?")} 回合）</div>
    <div class="prog gold"><i style="width:${res.progress_pct ?? 0}%"></i></div>` : "";

  const cities = (s.cities || []).map((c) => `
    <div class="kv"><span>${esc(c.name)} · P${esc(c.pop)} · 增长 ${esc(c.growth ?? "—")}</span>
    <b>${esc(c.production ?? "")}</b></div>`).join("");

  const units = (s.units || []).map((u) => `
    <div class="kv"><span>${esc(u.type)}</span><b>${esc(u.at?.[0])},${esc(u.at?.[1])} · ${esc(u.moves ?? "")}</b></div>`).join("");

  const threats = (s.threats || []).map((t) =>
    `<div>⚔ ${esc(t.type)} @ (${esc(t.at?.[0])},${esc(t.at?.[1])}) CS ${esc(t.cs ?? "?")}</div>`).join("");

  const notes = (s.notes || []).map((n) => `<div class="kv"><span>${esc(n)}</span></div>`).join("");

  setHTMLIfChanged(box, `
    <div class="bf-turn"><b>TURN ${esc(s.turn ?? "?")}</b><span>${esc(s.difficulty ?? "")}</span></div>
    <div class="bf-civ">${esc(s.civ ?? "")}${s.leader ? " — " + esc(s.leader) : ""}${s.score != null ? " · score " + esc(s.score) : ""}</div>
    <div class="yields">${yields}</div>
    ${resHTML}
    ${cities ? `<div class="sect-label">CITIES</div>${cities}` : ""}
    ${units ? `<div class="sect-label">UNITS</div>${units}` : ""}
    ${threats ? `<div class="threat"><div class="th-head">THREAT DETECTED</div>${threats}</div>` : ""}
    ${notes}`);
  $("#chip-turn").textContent = `TURN ${s.turn ?? "—"}`;
  $("#chip-civ").textContent = s.civ ?? "—";
}

function renderVerdicts(latestJev) {
  const box = $("#verdicts");
  if (!latestJev) {
    setHTMLIfChanged(box, `<p class="muted">尚无 Jev 判断。</p>`);
    return;
  }
  const ans = (latestJev.data.response || {}).answers || {};
  const rows = Object.keys(ans).slice(0, 6).map((qid) => {
    const a = ans[qid];
    const v = a?.choice ?? (a?.noul != null ? `P(yes)=${a.noul.toFixed(2)}` : a?.score != null ? `score=${a.score}` : "—");
    return `<div class="verdict"><span class="vq">${esc(qid)}</span>
      <span class="va">${esc(v)}</span></div>`;
  }).join("");
  setHTMLIfChanged(box, rows || `<p class="muted">该次调用无答案。</p>`);
}

function renderGate(latestGate, stats) {
  const box = $("#gate"), meter = $("#gate-meter");
  let html = `<p class="muted">尚无闸门检查。（POST /api/gate）</p>`;
  if (latestGate) {
    const r = (latestGate.data && latestGate.data.result) || {};
    const ask = r.should_ask;
    const chips = (r.triggers || []).map((t) =>
      `<span class="trigger ${ask ? "hot" : ""}">${esc(t)}</span>`).join("")
      || `<span class="muted">无触发</span>`;
    html = `
      <div class="gate-verdict ${ask ? "ask" : "skip"}">
        ${ask ? `ASK · ${r.question_ids.length} 个决策点` : "SKIP · 未调用 Jev"}
      </div>
      <div class="triggers">${chips}</div>`;
  }
  setHTMLIfChanged(box, html);
  if (stats) {
    setHTMLIfChanged(meter, `<div class="kv"><span>闸门检查 / 跳过</span>
      <b>${stats.gate_checks} / ${stats.gate_skipped}</b></div>
      <div class="kv"><span>Jev 实际调用</span><b>${stats.jev}</b></div>`);
  }
}

/* ── control mode (auto / manual) ────────────────────────────────── */

const modeSwitch = $("#mode-switch");
const pilotChip = $("#chip-pilot");

async function postMode(mode) {
  modeSwitch.classList.add("busy");
  try {
    const r = await fetch("/api/mode", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode }),
    });
    if (r.ok) renderMode(await r.json());
  } finally {
    modeSwitch.classList.remove("busy");
  }
}

function renderMode(m) {
  modeSwitch.classList.toggle("auto", m.mode === "auto");
  modeSwitch.classList.toggle("manual", m.mode !== "auto");
  if (m.mode === "auto") {
    pilotChip.classList.remove("hidden");
    pilotChip.classList.add("chip-pilot-on");
    const step = (m.step || "running").replace(/_/g, " ");
    let extra = "";
    if (m.blocker) extra += ` · 阻塞:${m.blocker}`;
    if (m.waiting_s != null) extra += ` · 等待 ${Math.round(m.waiting_s)}s`;
    $("#pilot-step").textContent =
      `AUTO · ${step}${extra}${m.turn ? " · T" + m.turn : ""}` +
      (m.cycles ? ` · ${m.cycles} 回合` : "");
  } else {
    pilotChip.classList.add("hidden");
  }
}

modeSwitch.addEventListener("click", () => {
  const target = modeSwitch.classList.contains("auto") ? "manual" : "auto";
  postMode(target).then(refresh);
});

/* ── polling ─────────────────────────────────────────────────────── */

async function refresh() {
  const [events, stats, latestState, latestJev, latestGate, mode] =
    await Promise.all([
      getJSON("/api/events?types=gate,jev,action,state&limit=80"),
      getJSON("/api/stats"),
      getJSON("/api/state/latest"),
      getJSON("/api/events?types=jev&limit=1"),
      getJSON("/api/gate/latest"),
      getJSON("/api/mode"),
    ]);
  if (mode) renderMode(mode);
  if (events) renderTimeline(events);
  if (latestState) renderBattlefield(latestState);
  if (latestJev && latestJev.length) renderVerdicts(latestJev[0]);
  renderGate(latestGate, stats);
  if (stats) {
    $("#st-jev").textContent = stats.jev;
    $("#st-actions").textContent = stats.actions;
    $("#st-tin").textContent = fmtTok(stats.tokens_in);
    $("#st-tout").textContent = fmtTok(stats.tokens_out);
    $("#chip-tokens").textContent = `✦ ${fmtTok(stats.tokens_in + stats.tokens_out)} tok`;
  }
}

async function refreshLive() {
  const [live, mode] = await Promise.all([
    getJSON("/api/live"),
    getJSON("/api/mode"),
  ]);
  const dot = $("#chip-live .dot");
  const label = $("#live-label");
  if (live && live.live) {
    dot.className = "dot on"; label.textContent = "GAME LIVE";
  } else if (mode && mode.mode === "auto" && mode.running) {
    // autopilot is working the link (game transitions / popup windows close
    // the tuner port for a while) — not an error state
    dot.className = "dot warn"; label.textContent = "LINKING…";
  } else {
    dot.className = "dot off"; label.textContent = "OFFLINE";
  }
}

document.querySelectorAll(".filter").forEach((btn) =>
  btn.addEventListener("click", () => {
    if (btn.dataset.t === filterType) return;
    document.querySelectorAll(".filter").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    filterType = btn.dataset.t;
    lastEventId = 0;       // filter switch → rebuild once from the current fetch
    refresh();
  }));

refresh();
refreshLive();
setInterval(refresh, 4000);
setInterval(refreshLive, 12000);

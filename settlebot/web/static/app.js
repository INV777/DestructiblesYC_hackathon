"use strict";
/* SettleBot console. Plain JS, no build step: reads the JSON API and redraws when data changes. */

const $ = (sel, el = document) => el.querySelector(sel);
const main = $("#main");
const state = { info: null, timer: null, runId: null, lastJson: "", expanded: new Set(), stamps: new Map(), busy: false };

const inr = new Intl.NumberFormat("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const money = (n) => (n == null ? "–" : "₹" + inr.format(n));
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
const STEP_NAMES = { normalize: "Read descriptor", dedupe: "Duplicate check", resolve: "Look up payee", decide: "Decide",
  bank_check: "Bank details check", post: "Post to books" };
const MODE_NAMES = { replay: "Recorded responses", fixtures: "Test fixtures", live: "Live" };
const SOURCE_NAMES = { fake: "Test fixture", crustdata_web: "Crustdata web search",
  crustdata_identify: "Crustdata company identify", claude: "Claude" };
const sourceName = (s) => SOURCE_NAMES[s] || s;
const PROVIDER_NAMES = { crustdata: "Crustdata", "crustdata-replay": "Crustdata (replay)", anthropic: "Claude",
  fake: "Test fixture", razorpay: "Razorpay (test mode)" };
const providerName = (p, step) => (p === "fake" && step === "pay" ? "Simulated payments" : PROVIDER_NAMES[p] || p);
const opName = (o) => { const t = String(o || "").replace(/_/g, " "); return t.charAt(0).toUpperCase() + t.slice(1); };
const unitsText = (n, unit) => !n ? "" : unit === "credits" && n === 1 ? "1 credit" : `${n} ${unit || ""}`;

function fmtDate(d) {
  if (!d) return "";
  const t = new Date(d.length === 10 ? d + "T00:00:00" : d);
  return isNaN(t) ? d : t.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
}
function fmtWhen(d) {
  if (!d) return "";
  const t = new Date(d);
  if (isNaN(t)) return d;
  return d.length <= 10 ? fmtDate(d) : t.toLocaleString("en-GB", { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });
}

async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "content-type": "application/json" }, ...opts });
  let body = null;
  try { body = await r.json(); } catch { /* empty body */ }
  if (!r.ok) {
    const d = body && body.detail;
    throw new Error(typeof d === "string" ? d : Array.isArray(d) ? d.map((x) => x.msg).join("; ") : `The server answered ${r.status}.`);
  }
  return body;
}

let toastTimer;
function toast(msg, isError = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.toggle("is-error", isError);
  t.classList.add("is-on");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("is-on"), isError ? 6000 : 3000);
}

/* ---- approver name (kept in this browser only) ---- */
const approverInput = $("#approver");
try { approverInput.value = localStorage.getItem("settlebot.approver") || ""; } catch { /* storage blocked */ }
approverInput.addEventListener("input", () => {
  try { localStorage.setItem("settlebot.approver", approverInput.value.trim()); } catch { /* storage blocked */ }
});
function approverName() {
  const name = approverInput.value.trim();
  if (!name) {
    approverInput.focus();
    toast("Type your name in “Approving as” first. It is recorded with the decision.", true);
  }
  return name;
}

/* ---- routing ---- */
function stopPolling() { clearTimeout(state.timer); state.timer = null; }
function route() {
  stopPolling();
  state.lastJson = "";
  const m = location.hash.match(/^#\/run\/([\w-]+)/);
  if (m) showRun(m[1]); else showHome();
}
window.addEventListener("hashchange", route);

/* ================= home: start a run + recent runs ================= */
async function showHome() {
  state.runId = null;
  document.title = "SettleBot console";
  const info = state.info;
  const samples = Object.entries(info.samples);
  main.innerHTML = `
    <section class="start" aria-labelledby="start-title">
      <form id="start" novalidate>
        <h1 id="start-title">Start a run</h1>
        <p class="muted">Pick a batch, watch each line get checked, then approve what should be paid.</p>
        <fieldset>
          <legend>Batch</legend>
          ${samples.map(([key, s], i) => `
            <label class="choice"><input type="radio" name="batch" value="${esc(key)}" data-mode="${esc(s.mode)}" ${i === 0 ? "checked" : ""}>
              <strong>${esc(s.title)}</strong><span>${esc(s.about)}</span></label>`).join("")}
          <label class="choice"><input type="radio" name="batch" value="upload">
            <strong>Your own batch</strong><span>A JSON list of bank lines or invoices, in the format shown in the README.
            <input type="file" id="file" accept=".json,application/json" aria-label="Batch file"></span></label>
        </fieldset>
        <fieldset>
          <legend>Data</legend>
          ${Object.entries(info.modes).map(([key, m]) => `
            <label class="choice ${m.available ? "" : "is-off"}"><input type="radio" name="mode" value="${esc(key)}" ${m.available ? "" : "disabled"}>
              <strong>${esc(MODE_NAMES[key])}</strong><span>${esc(m.about)}${m.available ? "" : key === "live" && info.demo ? " Not available in the public demo." : " Needs CRUSTDATA_API_KEY in your .env file."}</span></label>`).join("")}
        </fieldset>
        <label class="check"><input type="checkbox" id="slow" checked>
          <span>Slow motion: each lookup takes about a second, so you can crash the worker halfway through and watch it recover.</span></label>
        <p class="form-error" id="start-error" role="alert"></p>
        <button class="primary" type="submit">Start run</button>
      </form>
      <div>
        <h2 id="runs-title" style="margin-top:0">Recent runs</h2>
        <div id="runs"><p class="muted">Loading…</p></div>
      </div>
    </section>`;
  const form = $("#start");
  const pickMode = () => {
    const b = form.querySelector('input[name="batch"]:checked');
    const want = b && b.dataset.mode;
    const r = want && form.querySelector(`input[name="mode"][value="${want}"]:not(:disabled)`);
    if (r) r.checked = true;
    else if (!form.querySelector('input[name="mode"]:checked')) form.querySelector('input[name="mode"]:not(:disabled)').checked = true;
  };
  form.addEventListener("change", (e) => { if (e.target.name === "batch") pickMode(); });
  $("#file").addEventListener("change", () => { form.querySelector('input[value="upload"]').checked = true; });
  pickMode();
  form.addEventListener("submit", startRun);
  loadRuns();
}

async function startRun(e) {
  e.preventDefault();
  if (state.busy) return;
  const form = e.target;
  const err = $("#start-error");
  err.textContent = "";
  const batch = form.querySelector('input[name="batch"]:checked').value;
  const mode = form.querySelector('input[name="mode"]:checked').value;
  const body = { mode, slow: $("#slow").checked };
  if (batch === "upload") {
    const f = $("#file").files[0];
    if (!f) { err.textContent = "Choose a JSON file for your batch, or pick one of the samples."; return; }
    try {
      const data = JSON.parse(await f.text());
      body.lines = Array.isArray(data) ? data : (data.lines || data.items || data.bank_lines || data.invoices);
      if (!Array.isArray(body.lines)) throw new Error("expected a list of lines");
      body.label = f.name.replace(/\.json$/i, "");
    } catch (x) {
      err.textContent = `That file isn’t a batch SettleBot can read: ${x.message}.`;
      return;
    }
  } else {
    body.sample = batch;
  }
  state.busy = true;
  try {
    const r = await api("/api/runs", { method: "POST", body: JSON.stringify(body) });
    toast("Run started.");
    location.hash = `#/run/${r.run_id}`;
  } catch (x) {
    err.textContent = x.message;
  } finally {
    state.busy = false;
  }
}

function countsText(c) {
  const parts = [];
  const held = (c.HELD_DUPLICATE || 0) + (c.HELD_FRAUD || 0);
  if (c.VERIFIED) parts.push(`${c.VERIFIED} verified`);
  if (held) parts.push(`${held} held`);
  if (c.CANNOT_VERIFY) parts.push(`${c.CANNOT_VERIFY} cannot verify`);
  if (c.PENDING) parts.push(`${c.PENDING} still to check`);
  return parts.join(", ") || "Starting";
}
function workerText(r) {
  if (r.worker.running) return r.worker.kind === "settle" ? "Paying" : "Checking";
  if (r.status === "RUNNING" || r.status === "STARTING") return "Stopped mid-run";
  return r.status === "LIMIT_REACHED" ? "Stopped at a limit" : "Finished";
}

async function loadRuns() {
  if (state.runId) return;
  try {
    const runs = await api("/api/runs");
    const box = $("#runs");
    if (!box) return;
    const json = JSON.stringify(runs);
    if (json !== state.lastJson) {
      state.lastJson = json;
      box.innerHTML = runs.length ? `
        <table class="ledger runs">
          <thead><tr><th scope="col">Started</th><th scope="col">Batch</th><th scope="col">Result</th><th scope="col">Worker</th></tr></thead>
          <tbody>${runs.map((r) => `
            <tr><td>${esc(fmtWhen(r.created_at))}</td>
              <td><a href="#/run/${esc(r.run_id)}">${esc(r.label)}</a><br><span class="small muted">${esc(MODE_NAMES[r.mode])}, ${plural(r.lines, "line", "lines")}</span></td>
              <td class="small">${esc(countsText(r.counts))}</td>
              <td class="small">${esc(workerText(r))}</td></tr>`).join("")}</tbody>
        </table>` : `<p class="muted">No runs yet. Start one with a sample batch: it takes a few seconds.</p>`;
    }
  } catch (x) {
    toast(x.message, true);
  }
  state.timer = setTimeout(loadRuns, 3000);
}

/* ================= run page ================= */
async function showRun(runId) {
  state.runId = runId;
  state.expanded = new Set();
  main.innerHTML = `<p class="muted">Loading run…</p>`;
  main.focus();
  poll();
}

async function poll() {
  const runId = state.runId;
  if (!runId) return;
  let data;
  try {
    data = await api(`/api/runs/${runId}`);
  } catch (x) {
    main.innerHTML = `<p><a class="back" href="#/">All runs</a></p><p>${esc(x.message)}</p>`;
    return;
  }
  if (runId !== state.runId) return;
  const json = JSON.stringify(data);
  if (json !== state.lastJson) {
    state.lastJson = json;
    renderRun(data);
  }
  const live = data.worker.running || data.status === "STARTING";
  state.timer = setTimeout(poll, live ? 600 : 4000);
}

function stampFor(l, running) {
  const p = l.payment;
  if (p && p.status === "CREATED") return { word: "Paid", note: p.provider === "fake" ? "simulated provider" : "Razorpay test", cls: "stamp--paid" };
  if (p && p.status === "IN_FLIGHT") return { word: "Paying", note: "order being placed", cls: "" };
  switch (l.status) {
    case "VERIFIED": return { word: "Verified", note: l.posted_entry ? "posted" : "", cls: "" };
    case "HELD_DUPLICATE":
      if (l.posted_entry) return { word: "Released", note: "posted after approval", cls: "" };
      return { word: "Held", note: `duplicate of line ${l.related_line}`, cls: "stamp--red" };
    case "HELD_FRAUD": return { word: "Held", note: "do not pay", cls: "stamp--red" };
    case "CANNOT_VERIFY": return { word: "Cannot verify", note: "needs a person", cls: "stamp--amber" };
    default: return null;
  }
}

function stampHtml(l, data) {
  const s = stampFor(l, data.worker.running);
  if (!s) return `<span class="checking">${data.worker.running ? "Checking…" : "Not checked yet"}</span>`;
  const key = `${data.meta.run_id}:${l.line}`;
  const sig = s.word + s.note;
  const prev = state.stamps.get(key);
  state.stamps.set(key, sig);
  const isNew = prev !== undefined && prev !== sig;
  return `<span class="stamp ${s.cls} ${isNew ? "is-new" : ""}">${esc(s.word)}${s.note ? `<small>${esc(s.note)}</small>` : ""}</span>`;
}

function stepsHtml(l) {
  const label = l.steps.map((s) => `${STEP_NAMES[s.step]}: ${s.status === "DONE" ? "done" : s.status === "STARTED" ? "in progress" : "waiting"}${s.attempts > 1 ? `, redone after a crash` : ""}`).join("; ");
  return `<span class="steps" role="img" aria-label="${esc(label)}" title="${esc(label)}">${l.steps.map((s) =>
    `<i class="${s.status === "DONE" ? "done" : s.status === "STARTED" ? "started" : ""} ${s.attempts > 1 ? "redone" : ""}"></i>`).join("")}</span>`;
}

function detailHtml(l, runId) {
  const cites = (l.citations || []).map((c, i) => `
    <figure class="cite">
      <blockquote>“${esc(c.field_or_passage)}”</blockquote>
      <p>${esc(sourceName(c.source))}${c.field ? `, field <code>${esc(c.field)}</code>` : ""}. Retrieved ${esc(fmtWhen(c.retrieved_at))}${c.record_date ? `; record dated ${esc(fmtDate(c.record_date))}` : ""}.
        <button type="button" class="link" data-evidence="${esc(c.evidence_id)}" data-line="${l.line}" data-cite="${i}">Show the stored response</button></p>
    </figure>`).join("");
  const facts = [];
  if (l.invoice_no) facts.push(["Invoice", l.invoice_no]);
  if (l.vendor_id) facts.push(["Vendor", l.vendor_id]);
  if (l.bank_account) facts.push(["Pay to", `${l.bank_account.name || ""}, account ${l.bank_account.account}, IFSC ${l.bank_account.ifsc}`]);
  if (l.sender_email) facts.push(["Sent from", l.sender_email]);
  if (l.account) facts.push(["Account", l.account]);
  if (l.posted_entry) facts.push(["Journal entry", l.posted_entry]);
  for (const a of l.approvals || []) facts.push([a.decision === "APPROVE" ? "Approved" : "Rejected", `${a.action === "PAY" ? "payment" : "release"} by ${a.approver}, ${fmtWhen(a.decided_at)}${a.reason ? `: ${a.reason}` : ""}`]);
  if (l.payment) facts.push(["Payment", `${l.payment.status === "CREATED" ? "order placed" : l.payment.status.toLowerCase()} with ${l.payment.provider === "fake" ? "the simulated provider" : l.payment.provider}, order ${l.payment.provider_id || "pending"}, receipt ${l.payment.receipt}`]);
  return `
    <div class="detail-grid">
      <div>
        <p class="why">${esc(l.explanation || "Not decided yet.")}</p>
        ${l.missing && l.missing.length ? `<h3>What’s missing</h3><ul class="missing">${l.missing.map((m) => `<li>${esc(m)}</li>`).join("")}</ul>` : ""}
        ${cites ? `<h3>Evidence</h3>${cites}` : ""}
      </div>
      <dl class="facts">${facts.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}</dl>
    </div>`;
}

function recoveryText(m, t) {
  const redone = t ? t.steps_redone_after_crash : 0;
  const adopted = t ? t.orders_adopted_after_crash : 0;
  let out = `Crashed ${m.kills === 1 ? "once" : plural(m.kills, "time", "times")} and resumed. `;
  out += redone ? `${plural(redone, "step was", "steps were")} cut off and redone from saved state. `
    : "No step was cut off mid-way, so nothing had to be redone. ";
  if (adopted) out += `${plural(adopted, "payment order", "payment orders")} placed just before a crash ${adopted === 1 ? "was" : "were"} found at the provider and adopted, not placed again. `;
  if (t) out += t.posted_twice || t.paid_twice ? `Problem: ${t.posted_twice} lines posted twice, ${t.paid_twice} paid twice.`
    : "Checked: no line was posted twice and no invoice was paid twice.";
  return out;
}

function workerHtml(data) {
  const w = data.worker, m = data.meta, t = data.totals;
  const crashed = !w.running && (data.status === "RUNNING" || data.status === "STARTING");
  const recovery = m.kills ? " " + recoveryText(m, t) : "";
  if (w.running) {
    const doing = w.kind === "settle" ? "Paying approved invoices." : "Checking lines.";
    return `<div class="worker is-running"><p><span class="pulse" aria-hidden="true"></span>${doing} The worker is a separate process; crashing it is a real kill, with no chance to clean up.${recovery}</p>
      <button class="danger" data-act="kill">Crash the worker</button></div>`;
  }
  if (crashed) {
    return `<div class="worker is-crashed"><p>The worker stopped before finishing${m.kills ? " because you crashed it" : ""}. Finished steps are saved, so resuming picks up where it stopped.${recovery}</p>
      <button class="primary" data-act="resume">Resume run</button></div>`;
  }
  const failed = w.exit_code && w.exit_code !== 0 && !m.kills;
  return `<div class="worker"><p>${data.status === "LIMIT_REACHED" ? "Stopped at a cost or time limit; the remaining lines were left unverified." : `Finished in ${t.elapsed_seconds.toFixed(1)} s.`}${recovery}</p>
    ${failed ? `<pre class="json">${esc(data.log)}</pre>` : ""}</div>`;
}

function renderRun(data) {
  const m = data.meta, t = data.totals, ue = data.unit_economics;
  document.title = `${m.label} | SettleBot`;
  const modeAbout = state.info.modes[m.mode].about;
  const lines = data.lines;
  const n = (s) => lines.filter((l) => l.status === s).length;
  const held = n("HELD_DUPLICATE") + n("HELD_FRAUD");
  const approvedUnpaid = lines.filter((l) => (l.kind === "invoice" && l.status === "VERIFIED" && !l.payment &&
      l.approvals.some((a) => a.action === "PAY" && a.decision === "APPROVE") && !data.pending.some((p) => p.line === l.line && p.action === "PAY")) ||
    (l.status === "HELD_DUPLICATE" && !l.posted_entry && l.approvals.some((a) => a.action === "RELEASE_HOLD" && a.decision === "APPROVE")));
  const finished = !data.worker.running && (data.status === "DONE" || data.status === "LIMIT_REACHED");
  const lineFor = (no) => lines.find((l) => l.line === no);

  main.innerHTML = `
    <p><a class="back" href="#/">All runs</a></p>
    <div class="run-head"><h1>${esc(m.label)}</h1><span class="small muted">Run ${esc(m.run_id)}</span></div>
    <p class="mode"><strong>${esc(MODE_NAMES[m.mode])}.</strong> ${esc(modeAbout)}${m.slow ? " Slow motion is on." : ""}</p>
    ${workerHtml(data)}
    ${t ? `<ul class="tally">
      <li><b>${lines.length}</b>lines</li>
      <li><b>${n("VERIFIED")}</b>verified</li>
      <li><b>${held}</b>held</li>
      <li><b>${n("CANNOT_VERIFY")}</b>cannot verify</li>
      <li><b>${t.posted}</b>posted to the books</li>
      <li><b>${money(t.total_cost_inr)}</b>spent${t.replay ? " (replay)" : ""}</li>
    </ul>` : ""}
    <table class="ledger">
      <caption class="small muted" style="text-align:left;caption-side:bottom;padding-top:.5rem">Select a description to see why, and the exact passage each fact comes from.${lines.some((l) => l.steps.some((s) => s.attempts > 1)) ? " A red-outlined check is a step a crash cut off; it was redone from saved state." : ""}</caption>
      <thead><tr><th scope="col">Line</th><th scope="col" class="col-date">Date</th><th scope="col">Description</th>
        <th scope="col" class="num">Amount</th><th scope="col" class="col-steps">Checks</th><th scope="col">Verdict</th><th scope="col">Payee</th></tr></thead>
      <tbody>${lines.map((l) => {
        const open = state.expanded.has(l.line);
        return `
        <tr class="row">
          <td class="line-no">${l.line}</td>
          <td class="col-date">${esc(fmtDate(l.date))}</td>
          <td class="desc"><button class="open" data-line="${l.line}" aria-expanded="${open}" aria-controls="d${l.line}">${esc(l.description)}</button>
            ${l.invoice_no ? `<small>Invoice ${esc(l.invoice_no)}${l.vendor_id ? `, vendor ${esc(l.vendor_id)}` : ""}</small>` : `<small>Bank line</small>`}</td>
          <td class="num">${money(l.amount_inr)}</td>
          <td class="col-steps">${stepsHtml(l)}</td>
          <td class="col-verdict">${stampHtml(l, data)}</td>
          <td class="col-payee">${l.payee ? `${esc(l.payee)}${l.account ? `<br><span class="small muted">${esc(l.account)}</span>` : ""}` : `<span class="muted">–</span>`}</td>
        </tr>
        <tr class="detail" id="d${l.line}" ${open ? "" : "hidden"}><td colspan="7">${detailHtml(l, m.run_id)}</td></tr>`;
      }).join("")}</tbody>
    </table>

    ${data.pending.length || approvedUnpaid.length ? `
      <h2>Waiting for you</h2>
      ${data.pending.length ? `<ul class="queue">${data.pending.map((p) => {
        const l = lineFor(p.line);
        const yes = p.have.filter((h) => h.decision === "APPROVE").map((h) => h.approver);
        const what = p.action === "PAY" ? `Pay ${money(p.amount_inr)} to ${l.payee || l.description}` : `Release line ${p.line} (${money(p.amount_inr)}) and post it, although it looks like a duplicate of line ${l.related_line}`;
        return `<li>
          <div><p><b>Line ${p.line}.</b> ${esc(what)}.</p>
            <p class="small muted">Needs ${plural(p.need, "approval", "different approvers")}${yes.length ? `; approved so far by ${esc(yes.join(", "))}` : ""}.</p></div>
          <div class="actions">
            <input aria-label="Reason for line ${p.line} (optional)" placeholder="Reason (optional)" data-reason="${p.line}-${p.action}" maxlength="300">
            <button class="approve" data-act="decide" data-line="${p.line}" data-action="${p.action}" data-approve="1">${p.action === "PAY" ? "Approve payment" : "Release hold"}</button>
            <button data-act="decide" data-line="${p.line}" data-action="${p.action}" data-approve="0">Reject</button>
          </div></li>`;
      }).join("")}</ul>` : ""}
      ${approvedUnpaid.length ? `<p>${plural(approvedUnpaid.length, "approved item is", "approved items are")} ready. Payments go to ${m.pay_provider === "fake" ? "a simulated provider; no money moves" : "Razorpay in test mode"}, and each invoice is paid at most once, even if the worker crashes mid-payment.</p>
        <button class="primary" data-act="settle" ${finished ? "" : "disabled"}>Pay approved invoices</button>` : ""}` : ""}

    <details class="costs" id="costs">
      <summary>What this run cost</summary>
      ${ue ? `<dl class="economics">
        <div><dt>Spent on this run</dt><dd>${money(ue.measured_cost_inr)}</dd></div>
        ${ue.replay ? `<div><dt>Same calls live, estimated</dt><dd>${money(ue.live_cost_estimate_inr)}</dd></div>` : ""}
        <div><dt>Our price per finished line</dt><dd>${money(ue.price_inr_per_line)}</dd></div>
        <div><dt>Outsourced AP, per invoice</dt><dd>${ue.benchmark_inr_per_line ? money(ue.benchmark_inr_per_line) : "–"}</dd></div>
        <div><dt>Margin${ue.replay ? " at live cost" : ""}</dt><dd>${ue.margin_pct == null ? "–" : ue.margin_pct + "%"}</dd></div>
      </dl>
      <p class="small muted">We charge only for lines finished without a person. Benchmark: <a href="${esc(ue.benchmark_source_url)}">Pazy’s guide to AP outsourcing in India</a>, low end of ₹50–80 per invoice.</p>` : ""}
      <div id="calls"><p class="muted small">Open this section to load every call this run made.</p></div>
    </details>`;

  const costs = $("#costs");
  if (state.costsOpen) { costs.open = true; loadCalls(); }
  costs.addEventListener("toggle", () => { state.costsOpen = costs.open; if (costs.open) loadCalls(); });
}

async function loadCalls() {
  const box = $("#calls");
  if (!box) return;
  try {
    const rows = await api(`/api/runs/${state.runId}/ledger`);
    box.innerHTML = rows.length ? `<div style="overflow-x:auto"><table class="calls">
      <thead><tr><th scope="col">Line</th><th scope="col">Step</th><th scope="col">Provider</th><th scope="col">Call</th>
        <th scope="col" class="num">Units</th><th scope="col" class="num">Cost</th><th scope="col">Result</th></tr></thead>
      <tbody>${rows.map((r) => `<tr><td>${r.line ?? "–"}</td><td>${esc(r.step || "")}${r.attempt > 1 ? ` (try ${r.attempt})` : ""}</td>
        <td>${esc(providerName(r.provider, r.step))}</td><td>${esc(opName(r.op))}</td>
        <td class="num">${esc(unitsText(r.units, r.unit_type))}</td><td class="num">${money(r.cost_inr)}</td>
        <td>${r.cache_hit ? "reused, not paid again" : r.ok ? "ok" : esc(r.detail || "failed")}</td></tr>`).join("")}</tbody></table></div>`
      : `<p class="muted small">No calls yet.</p>`;
  } catch (x) {
    box.innerHTML = `<p class="small">${esc(x.message)}</p>`;
  }
}

/* ---- stored response viewer: highlights the cited field and passage ---- */
const norm = (s) => String(s).replace(/\s+/g, " ").trim().toLowerCase();
function jsonHtml(value, field, passage) {
  let found = 0;
  const mark = (str, path) => {
    const quoted = JSON.stringify(str);
    const hit = (field && path === field) || (!field && passage && norm(str).includes(norm(passage)));
    if (!hit) return esc(quoted);
    found += 1;
    return `<mark id="hit">${esc(quoted)}</mark>`;
  };
  const walk = (v, path, ind) => {
    const pad = "  ".repeat(ind);
    if (Array.isArray(v)) {
      if (!v.length) return "[]";
      return "[\n" + v.map((x, i) => pad + "  " + walk(x, `${path}[${i}]`, ind + 1)).join(",\n") + "\n" + pad + "]";
    }
    if (v && typeof v === "object") {
      const keys = Object.keys(v);
      if (!keys.length) return "{}";
      return "{\n" + keys.map((k) => `${pad}  ${esc(JSON.stringify(k))}: ${walk(v[k], path ? `${path}.${k}` : k, ind + 1)}`).join(",\n") + "\n" + pad + "}";
    }
    if (typeof v === "string") return mark(v, path);
    return esc(JSON.stringify(v));
  };
  const html = walk(value, "", 0);
  return { html, found };
}

async function showEvidence(btn) {
  const dlg = $("#evidence");
  const box = $("#evidence-body");
  const data = JSON.parse(state.lastJson);
  const line = data.lines.find((l) => String(l.line) === btn.dataset.line);
  const cite = line.citations[Number(btn.dataset.cite)];
  box.innerHTML = `<p class="muted">Loading…</p>`;
  dlg.showModal();
  try {
    const ev = await api(`/api/runs/${state.runId}/evidence/${btn.dataset.evidence}`);
    let parsed;
    try { parsed = JSON.parse(ev.body); } catch { parsed = ev.body; }
    const { html, found } = jsonHtml(parsed, cite.field, cite.field_or_passage);
    const inField = cite.field ? `the field <code>${esc(cite.field)}</code>` : "this response";
    box.innerHTML = `
      <p>${esc(sourceName(ev.source))} response stored during the run, retrieved ${esc(fmtWhen(ev.retrieved_at))}. SHA-256 <code>${esc(ev.body_sha256.slice(0, 16))}…</code></p>
      <p class="found">${found ? `The quoted passage is in ${inField}, highlighted below. SettleBot only keeps a fact when its quote is found in a stored response like this one.` : "The cited field could not be highlighted in this response."}</p>
      <pre class="json" tabindex="0">${html}</pre>`;
    const hit = $("#hit", box);
    if (hit) hit.scrollIntoView({ block: "center" });
  } catch (x) {
    box.innerHTML = `<p>${esc(x.message)}</p>`;
  }
}

/* ---- clicks on the run page ---- */
main.addEventListener("click", async (e) => {
  const open = e.target.closest("button.open");
  if (open) {
    const no = Number(open.dataset.line);
    const row = $(`#d${no}`);
    const now = !state.expanded.has(no);
    if (now) state.expanded.add(no); else state.expanded.delete(no);
    row.hidden = !now;
    open.setAttribute("aria-expanded", String(now));
    return;
  }
  const ev = e.target.closest("button[data-evidence]");
  if (ev) { showEvidence(ev); return; }
  const btn = e.target.closest("button[data-act]");
  if (!btn || state.busy) return;
  const runId = state.runId;
  state.busy = true;
  btn.disabled = true;
  try {
    if (btn.dataset.act === "kill") {
      const r = await api(`/api/runs/${runId}/kill`, { method: "POST" });
      toast(`Worker crashed (crash ${r.kills}). Resume it to watch what gets redone.`);
    } else if (btn.dataset.act === "resume") {
      await api(`/api/runs/${runId}/resume`, { method: "POST" });
      toast("Run resumed.");
    } else if (btn.dataset.act === "settle") {
      await api(`/api/runs/${runId}/settle`, { method: "POST" });
      toast("Paying approved invoices.");
    } else if (btn.dataset.act === "decide") {
      const name = approverName();
      if (!name) return;
      const approve = btn.dataset.approve === "1";
      const reasonEl = main.querySelector(`[data-reason="${btn.dataset.line}-${btn.dataset.action}"]`);
      const r = await api(`/api/runs/${runId}/decisions`, { method: "POST", body: JSON.stringify({
        line: Number(btn.dataset.line), action: btn.dataset.action, approve, approver: name,
        reason: reasonEl ? reasonEl.value : null }) });
      toast(!approve ? "Rejected. It will not be paid." : r.state === "APPROVED"
        ? (btn.dataset.action === "PAY" ? "Payment approved." : "Hold released.")
        : `Approved by you. It needs ${r.need - r.approvals} more approval from someone else.`);
    }
    stopPolling();
    state.lastJson = "";
    poll();
  } catch (x) {
    toast(x.message, true);
  } finally {
    state.busy = false;
    btn.disabled = false;
  }
});

/* ---- start ---- */
(async function init() {
  try {
    state.info = await api("/api/info");
  } catch (x) {
    main.innerHTML = `<p>The console could not reach its server: ${esc(x.message)}</p>`;
    return;
  }
  $("#demo-note").hidden = !state.info.demo;
  route();
})();

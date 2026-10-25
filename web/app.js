// SkyDesk Airways demo: mock site + docked support agent over POST /api/chat and /api/approve.
const $ = (s, el = document) => el.querySelector(s);
const { esc } = window.ApprovalCard;
const msgs = $("#msgs"), input = $("#input"), send = $("#send"), chat = $("#chat"), launcher = $("#launcher");
const CHIPS = [
  ["Refund a booking I made today", "Hi, I booked RSK7FT this morning by mistake. I'm Maria Garcia, card ending 5147. Please cancel it and refund me."],
  ["What would I get back?", "Linh Nguyen here, LTEXPD, card 5584. What would I get back if I cancelled? Just asking for now."],
  ["Move me to another flight", "Kenji Tanaka, booking SCH95M, card 2634. You moved my flight by more than an hour and a half. Please put me on flight SD300 on October 27 instead, at no cost."],
  ["Bereavement", "My father passed away last night and I need to cancel BRVMNT and get a refund. Emre Yilmaz, card 1332."],
];
let busy = false, awaiting = false, lastFocus = null, lastRun = null, waitPoll = null;

function threadId(fresh) {
  let id = null;
  try { id = fresh ? null : sessionStorage.getItem("skydesk-thread"); } catch {}
  if (!id) {
    id = "web-" + Math.random().toString(36).slice(2, 12);
    try { sessionStorage.setItem("skydesk-thread", id); } catch {}
  }
  return id;
}
let TID = threadId();

function el(tag, cls, html) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (html != null) e.innerHTML = html;
  return e;
}

// Light markdown: paragraphs, "- " / "1. " lists, **bold**.
function md(text) {
  const inline = (s) => esc(s).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>");
  return String(text || "").trim().split(/\n{2,}/).map((block) => {
    const lines = block.split("\n");
    if (lines.every((l) => /^\s*([-*]|\d+\.)\s+/.test(l)))
      return "<ul>" + lines.map((l) => `<li>${inline(l.replace(/^\s*([-*]|\d+\.)\s+/, ""))}</li>`).join("") + "</ul>";
    return `<p>${lines.map(inline).join("<br>")}</p>`;
  }).join("");
}

function scrollDown() { msgs.scrollTop = msgs.scrollHeight; }
function addUser(text) { msgs.append(el("div", "m user", esc(text))); scrollDown(); }
function addBot(node) {
  const m = el("div", "m bot");
  if (node) m.append(node);
  msgs.append(m);
  scrollDown();
  return m;
}

function greet() {
  msgs.innerHTML = "";
  const b = el("div", "bubble", "<p>Hi, I'm the <b>SkyDesk Assistant</b>. I can look up your booking, check what our policy allows, and make changes once a supervisor approves them. What can I help you with?</p>");
  const chips = el("div", "chips");
  CHIPS.forEach(([label, q]) => {
    const c = el("button", "chip", esc(label));
    c.type = "button";
    c.onclick = () => ask(q);
    chips.append(c);
  });
  addBot(b).append(chips);
}

// ---- agent activity strip
const ICON = { ok: "i-ok", failed: "i-x", timeout: "i-clock", withheld: "i-shield", rejected: "i-x", edited: "i-edit" };
function renderSteps(strip, steps, live) {
  strip.innerHTML = "";
  steps.forEach((s) => {
    const tip = [s.summary, s.retries ? `retried ${s.retries}x` : "", s.ms ? `${Math.round(s.ms)} ms` : ""].filter(Boolean).join(" · ");
    const st = el("span", `st ${s.status}`, `<svg><use href="#${ICON[s.status] || "i-ok"}"/></svg>${esc(s.label)}`);
    st.title = tip;
    st.dataset.tool = s.tool;
    if (s.retries) st.append(el("span", "retry", `<svg><use href="#i-retry"/></svg>retried ${s.retries}x`));
    strip.append(st);
  });
  if (live) strip.append(el("span", "st live", "<i></i>Working"));
  strip.hidden = !strip.children.length;
}
function liveSteps(strip, since) {
  // Poll the checkpointed thread state while the agent runs, so steps appear as they finish.
  let stop = false;
  (async function tick() {
    while (!stop) {
      await new Promise((r) => setTimeout(r, 300));
      if (stop) break;
      try {
        const s = await (await fetch(`/api/thread/${TID}`)).json();
        if (!stop && s.steps && (since == null ? s.run_id !== lastRun : true)) renderSteps(strip, since == null ? s.steps : s.steps.slice(since), true);
      } catch {}
    }
  })();
  return () => { stop = true; };
}

// ---- turn rendering
function render(r, wrap) {
  lastRun = r.run_id || lastRun;
  const strip = $(".steps", wrap);
  renderSteps(strip, r.steps || [], false);
  if (r.reply) {
    const b = el("div", "bubble", md(r.reply));
    wrap.append(b);
    const v = r.verified || {};
    const meta = el("div", "meta");
    if (v.withheld && v.withheld.length) meta.append(el("span", "conf-pill mid", `<i></i>${v.withheld.length} unverified figure${v.withheld.length > 1 ? "s" : ""} withheld`));
    else if (v.numbers_checked) meta.append(el("span", "conf-pill", `<i></i>Figures checked against our systems`));
    if (meta.children.length) wrap.append(meta);
  }
  if (r.escalation) wrap.append(handoff(r.escalation));
  if (r.interrupt) wrap.append(approvalCard(r.interrupt, wrap));
  scrollDown();
}

function handoff(h) {
  const card = el("div", "handoff", `<h4><span class="pulse"></span>Connecting you to ${/agent$/i.test(h.team) ? "a" : "the"} ${esc(h.team)}</h4>
    <dl><dt>Team</dt><dd>${esc(h.team)}</dd><dt>Reason</dt><dd>${esc(h.reason)}</dd>
    ${h.ticket_id ? `<dt>Reference</dt><dd>${esc(h.ticket_id)}</dd>` : ""}
    <dt>Priority</dt><dd><span class="prio">${esc(h.priority)}</span></dd>
    ${h.expected_response ? `<dt>Response</dt><dd>${esc(h.expected_response)}</dd>` : ""}</dl>
    <div class="small">Your conversation has been passed on, so you won't need to repeat yourself.</div>`);
  const act = el("div", "actions");
  act.append(actionBtn("Talk to an agent", "i-chat", `Demo: a ${h.team} agent would join this chat now.`, "btn-primary"),
    actionBtn("Request a callback", "i-phone", "Demo: a callback has been requested."));
  card.append(act);
  return card;
}

function actionBtn(label, icon, msg, cls = "btn-soft") {
  const b = el("button", "btn " + cls, `<svg><use href="#${icon}"/></svg>${esc(label)}`);
  b.type = "button";
  b.onclick = () => { b.disabled = true; addBot(el("div", "bubble", `<p>${esc(msg)}</p>`)); };
  return b;
}

function approvalCard(req, wrap) {
  let settled = false;
  const card = window.ApprovalCard.render(req, async (decision, edited, note) => {
    settled = true;
    return decide(decision, edited, note);
  });
  // A supervisor may decide on /supervisor instead: watch for the interrupt to clear.
  (async function watch() {
    while (!settled && document.contains(card)) {
      await new Promise((r) => setTimeout(r, 2000));
      if (settled || (busy && !awaiting)) continue;
      try {
        const s = await (await fetch(`/api/thread/${TID}`)).json();
        if (!s.interrupt || s.interrupt.approval_id !== req.approval_id) {
          settled = true;
          card.markDecided("Decided on the supervisor page");
          setBusy(false);
          const w = addBot(null); w.append(el("div", "steps"));
          render({ ...s, steps: [] }, w);
        }
      } catch {}
    }
  })();
  setBusy(true, "Waiting for supervisor approval...");
  return card;
}

async function post(url, body) {
  const res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  return res.json().catch(() => ({ error: "Sorry, something went wrong. Please try again." }));
}

async function decide(decision, edited_args, note) {
  const wrap = addBot(null);
  const strip = el("div", "steps");
  wrap.append(strip);
  const typing = el("div", "bubble typing", "<i></i><i></i><i></i>");
  wrap.append(typing);
  setBusy(true);
  const before = (await (await fetch(`/api/thread/${TID}`)).json().catch(() => ({}))).steps?.length;
  const stop = liveSteps(strip, before);
  const data = await post("/api/approve", { thread_id: TID, decision, edited_args, note, approver: "supervisor@skydesk" });
  stop(); typing.remove();
  setBusy(false);
  if (data.error) { wrap.append(el("div", "bubble", `<p>${esc(data.error)}</p>`)); return false; }
  render(data, wrap);
  loadCost();
  return true;
}

function setBusy(on, placeholder) {
  busy = on; awaiting = on && !!placeholder; send.disabled = on; input.disabled = on && !!placeholder;
  input.placeholder = placeholder || "Booking reference, name, what you need...";
}

async function ask(q) {
  q = (q || "").trim();
  if (!q || busy) return;
  openChat();
  $(".chips", msgs)?.remove();
  setBusy(true); input.value = "";
  addUser(q);
  const wrap = addBot(null);
  const strip = el("div", "steps");
  strip.hidden = true;
  wrap.append(strip);
  const typing = el("div", "bubble typing", "<i></i><i></i><i></i>");
  typing.setAttribute("aria-label", "Assistant is working");
  wrap.append(typing);
  const stop = liveSteps(strip, null);
  try {
    const data = await post("/api/chat", { thread_id: TID, message: q });
    stop(); typing.remove(); setBusy(false);
    if (data.error) {
      wrap.append(el("div", "bubble", `<p>${esc(data.error)}</p>`));
    } else render(data, wrap);
    loadCost();
  } catch {
    stop(); typing.remove(); setBusy(false);
    wrap.append(el("div", "bubble", "<p>I can't reach SkyDesk right now. Please check your connection and try again.</p>"));
  } finally {
    if (!busy) input.focus();
  }
}

function openChat(prefill) {
  if (chat.hidden) { lastFocus = document.activeElement; chat.hidden = false; launcher.classList.add("hide"); launcher.setAttribute("aria-expanded", "true"); }
  if (prefill && !busy) input.value = prefill;
  input.focus();
}
function closeChat(reset) {
  chat.hidden = true; launcher.classList.remove("hide"); launcher.setAttribute("aria-expanded", "false");
  if (reset && (!busy || awaiting)) { TID = threadId(true); lastRun = null; setBusy(false); greet(); }
  (lastFocus && lastFocus !== document.body ? lastFocus : launcher).focus();
}

let toastT;
function toast(text) {
  const t = $("#toast");
  t.textContent = text; t.classList.add("show");
  clearTimeout(toastT); toastT = setTimeout(() => t.classList.remove("show"), 2600);
}

async function loadCost() {
  try {
    const s = await (await fetch("/api/cost")).json();
    const p = s.planner === "rules" ? "offline keyword baseline (no model)" : s.planner;
    $("#cost").textContent = `Planner: ${p}. AI cost so far: $${s.total_usd.toFixed(4)} of $${s.budget_usd.toFixed(2)} cap (${s.calls} model calls).`;
    $("#plannerTag").textContent = s.planner === "rules" ? "Offline planner" : s.planner;
  } catch { $("#cost").textContent = "AI cost so far: unavailable."; }
}

// ---- demo controls drawer
async function loadFaults() {
  try {
    const f = await (await fetch("/api/faults")).json();
    const r = document.querySelector(`#drawer input[value="${f.preset}"]`);
    if (r) r.checked = true;
    $("#gearBtn").classList.toggle("armed", f.preset !== "none");
  } catch {}
}
$("#gearBtn").onclick = () => {
  const d = $("#drawer");
  d.hidden = !d.hidden;
  $("#gearBtn").setAttribute("aria-expanded", String(!d.hidden));
  if (!d.hidden) loadFaults();
};
$("#drawer").addEventListener("change", async (e) => {
  if (e.target.name !== "fault") return;
  await post("/api/faults", { preset: e.target.value });
  $("#gearBtn").classList.toggle("armed", e.target.value !== "none");
  toast(e.target.value === "none" ? "Fault injection off" : `Fault armed: ${e.target.closest("label").innerText.trim()}`);
});
$("#resetBtn").onclick = async () => {
  await post("/api/reset", {});
  loadFaults();
  TID = threadId(true); lastRun = null; setBusy(false); greet();
  toast("Demo data reset");
};

function openModal() { $("#modal").hidden = false; loadCost(); $("#modal [data-close]").focus(); }
function closeModal() { $("#modal").hidden = true; }

// Wire up: mock buttons toast, help buttons open the chat with a request pre-filled.
document.addEventListener("click", (e) => {
  const t = e.target.closest("[data-toast],[data-ask],[data-open-chat],[data-modal],[data-close]");
  if (!t) return;
  if (t.matches("[data-ask]")) openChat(t.dataset.ask);
  else if (t.matches("[data-open-chat]")) openChat();
  else if (t.matches("[data-modal]")) openModal();
  else if (t.matches("[data-close]")) closeModal();
  else toast("Demo site: nothing here is bookable. Try the assistant, bottom right.");
});
$("#modal").addEventListener("click", (e) => { if (e.target.id === "modal") closeModal(); });
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (!$("#modal").hidden) closeModal();
  else if (!$("#drawer").hidden) $("#drawer").hidden = true;
  else if (!chat.hidden) closeChat(false);
});
launcher.onclick = () => openChat();
$("#minBtn").onclick = () => closeChat(false);
$("#closeBtn").onclick = () => closeChat(true);
$("#composer").onsubmit = (e) => { e.preventDefault(); ask(input.value); };
$("#logLink").onclick = (e) => { e.currentTarget.href = `/api/log?thread_id=${TID}`; };
document.querySelectorAll(".trip button").forEach((b) => b.onclick = () => {
  document.querySelectorAll(".trip button").forEach((x) => { x.classList.toggle("on", x === b); x.setAttribute("aria-checked", x === b); });
});

greet();
fetch("/api/health").then((r) => r.ok && $("#dot").classList.add("on")).catch(() => {});
loadCost();
loadFaults();

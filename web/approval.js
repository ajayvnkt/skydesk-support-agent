// Shared by the chat widget and /supervisor: renders an approval request (POST /api/chat interrupt) as a card.
(() => {
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const money = (n, cur) => new Intl.NumberFormat("en-US", { style: "currency", currency: cur || "USD" }).format(n);
  const TITLE = { cancel_booking: "Cancel booking", issue_refund: "Issue refund", rebook_flight: "Rebook flight",
    issue_voucher: "Issue voucher", waive_fee: "Waive change fee", update_contact: "Update contact details" };

  function headline(req) {
    const q = req.quote || {}, a = req.args || {}, cur = q.currency;
    if (q.refund_to_original_payment > 0) return ["Refund to original card", money(q.refund_to_original_payment, cur)];
    if (q.travel_credit > 0) return ["Travel credit", money(q.travel_credit, cur)];
    if (q.collect_from_customer > 0) return ["Customer pays", money(q.collect_from_customer, cur)];
    if (q.meal_voucher > 0) return ["Meal voucher", money(q.meal_voucher, cur)];
    if (q.skymiles > 0) return ["SkyMiles", q.skymiles.toLocaleString("en-US")];
    if (req.action === "update_contact") return ["New contact", `<span class="txt">${esc(a.email || a.phone || "")}</span>`];
    if (req.action === "rebook_flight") {
      const m = /^([A-Z]+\d+)-(\d{4})(\d{2})(\d{2})$/.exec(a.new_flight_id || "");
      const day = m ? new Date(Date.UTC(+m[2], +m[3] - 1, +m[4])).toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" }) : "";
      return ["New flight", `<span class="txt">${esc(m ? `${m[1]} · ${day}` : a.new_flight_id)}</span><em>No charge</em>`];
    }
    if (q.lines) return ["Amount", money(0, cur)];
    return null;
  }

  function editFields(req) {
    const a = req.args || {}, f = [];
    if (typeof a.amount === "number") f.push(["amount", `Amount (up to ${money(a.amount, (req.quote || {}).currency)})`, a.amount, "number"]);
    if (a.new_flight_id) f.push(["new_flight_id", "New flight id", a.new_flight_id, "text"]);
    if (req.action === "update_contact") f.push(["email", "Email", a.email || "", "email"]);
    return f;
  }

  // onDecide(decision, edited_args, note) -> Promise. Returns the card element.
  function render(req, onDecide) {
    const q = req.quote || {}, el = req.eligibility || {}, a = req.args || {};
    const target = a.booking_ref || (req.customer || {}).customer_id || "";
    const hl = headline(req);
    const conds = el.conditions || [];
    const met = conds.filter((c) => c.passed).length;
    const lines = (q.lines || []).map((l) => `<tr><td>${esc(l.label)}</td><td class="r">${money(l.amount, q.currency)}</td><td><code>${esc(l.rule_id)}</code></td></tr>`).join("");
    const card = document.createElement("div");
    card.className = "approval";
    card.dataset.approvalId = req.approval_id || "";
    card.innerHTML = `
      <div class="ap-head"><span class="ap-badge"><svg><use href="#i-shield"/></svg>Supervisor approval</span><span class="ap-state" data-state>Waiting</span></div>
      <h4>${esc(TITLE[req.action] || req.action)} <span>${esc(target)}</span></h4>
      ${hl ? `<div class="ap-amt"><small>${hl[0]}</small><b>${hl[1]}</b></div>` : ""}
      ${lines ? `<table class="ap-lines" aria-label="Breakdown"><tbody>${lines}</tbody></table>` : ""}
      <details class="ap-pol"><summary><span class="${el.eligible === false ? "no" : "yes"}">${el.eligible === false ? "Needs a human decision" : "Eligible"}</span> ${met} of ${conds.length} policy conditions met</summary>
        <ul>${conds.map((c) => `<li class="${c.passed ? "p" : "f"}"><code>${esc(c.rule_id)}</code>${esc(c.condition)}${c.required ? "" : " <em>(optional)</em>"}</li>`).join("")}</ul></details>
      ${(el.exceptions_applied || []).map((x) => `<div class="ap-exc">Exception applied: ${esc(typeof x === "string" ? x : x.rule_id + " " + (x.description || x.condition || ""))}</div>`).join("")}
      ${(req.risk || []).length ? `<ul class="ap-risk">${req.risk.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>` : ""}
      <form class="ap-edit" hidden>
        ${editFields(req).map(([k, label, v, t]) => `<label><span>${esc(label)}</span><input name="${k}" type="${t}" ${t === "number" ? `step="0.01" min="0.01" max="${v}"` : ""} value="${esc(v)}"></label>`).join("")}
        <label><span>Note (optional)</span><input name="note" maxlength="300" placeholder="Why the change"></label>
        <div class="ap-btns"><button type="submit" class="btn btn-primary" data-act="submit-edit">Send edited request</button><button type="button" class="btn btn-soft" data-act="cancel-edit">Cancel</button></div>
      </form>
      <div class="ap-btns" data-main>
        <button type="button" class="btn btn-ok" data-act="approve"><svg><use href="#i-ok"/></svg>Approve</button>
        <button type="button" class="btn btn-soft" data-act="edit">Edit</button>
        <button type="button" class="btn btn-soft danger" data-act="reject">Reject</button>
      </div>
      <div class="ap-foot">Approval <code>${esc(req.approval_id)}</code>. Nothing changes until a supervisor approves.</div>`;
    const form = card.querySelector(".ap-edit"), main = card.querySelector("[data-main]");
    const state = card.querySelector("[data-state]");
    const lock = (text, cls) => {
      card.querySelectorAll("button, input").forEach((b) => (b.disabled = true));
      state.textContent = text; card.classList.add("decided", cls);
    };
    async function go(decision, edited, note) {
      lock(decision === "approve" ? "Approving..." : decision === "reject" ? "Rejecting..." : "Re-quoting...", "busy");
      const ok = await onDecide(decision, edited, note);
      card.classList.remove("busy");
      if (ok === false) { card.querySelectorAll("button, input").forEach((b) => (b.disabled = false)); card.classList.remove("decided"); state.textContent = "Waiting"; return; }
      lock(decision === "approve" ? "Approved" : decision === "reject" ? "Rejected" : "Edited", decision);
    }
    card.querySelector('[data-act="approve"]').onclick = () => go("approve");
    card.querySelector('[data-act="reject"]').onclick = () => go("reject");
    card.querySelector('[data-act="edit"]').onclick = () => { form.hidden = false; main.hidden = true; form.querySelector("input")?.focus(); };
    card.querySelector('[data-act="cancel-edit"]').onclick = () => { form.hidden = true; main.hidden = false; };
    form.onsubmit = (e) => {
      e.preventDefault();
      const fd = new FormData(form), edited = {};
      for (const [k, v] of fd) if (k !== "note" && String(v).trim() !== "") edited[k] = k === "amount" ? Number(v) : String(v).trim();
      form.hidden = true; main.hidden = false;
      go("edit", edited, fd.get("note") || null);
    };
    card.markDecided = (text) => lock(text, "approve");
    return card;
  }

  window.ApprovalCard = { render, esc, money };
})();

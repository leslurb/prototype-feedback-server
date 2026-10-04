// Dashboard interactions. Every action is one call to the work-queue API; the server
// enforces the rules (409 stale / 422 not allowed) and this file reports them verbatim.
(function () {
  if (window.__pfbDashboard) return;           // included by several partials on one page
  window.__pfbDashboard = true;

  async function api(method, url, body) {
    const opt = { method, headers: { "Content-Type": "application/json" } };
    if (body !== undefined) opt.body = JSON.stringify(body);
    const r = await fetch(url, opt);
    let json = null;
    try { json = await r.json(); } catch (e) { /* non-JSON error body */ }
    return { ok: r.ok, status: r.status, json };
  }
  function say(key, text, isErr) {
    const el = document.querySelector(`[data-msg="${key}"]`);
    if (el) { el.textContent = text; el.className = "small " + (isErr ? "err" : "ok"); }
  }
  function detail(res) {
    const d = res.json && res.json.detail;
    if (!d) return `HTTP ${res.status}`;
    if (typeof d === "string") return d;
    if (d.current) return `Changed elsewhere: it is now ${d.current}. Reload.`;
    return JSON.stringify(d);
  }

  // ---- dashboard selection ------------------------------------------------
  function selectedRounds() { return [...document.querySelectorAll('input[name="round"]:checked')]; }
  function refreshQueueButton() {
    const btn = document.querySelector('[data-action="queue-selected"]');
    if (!btn) return;
    const n = selectedRounds().reduce((s, el) => s + Number(el.dataset.new || 0), 0);
    btn.disabled = n === 0;
    btn.textContent = n ? `Queue ${n} & copy` : "Select to queue";
  }

  // ---- composer -------------------------------------------------------------
  function composerState(f) {
    const llm = f.elements.recipient.value === "llm";
    const repo = f.querySelector('[data-check-src="repo"]').dataset.ok === "1";
    const checks = [
      ["repo mapped", repo],
      ["what", f.elements.title.value.trim() !== ""],
      ["where", f.elements.page.value.trim() !== ""],
      ["expected result", !llm || f.elements.expected_result.value.trim() !== ""],
    ];
    return { llm, checks, ready: llm && checks.every((c) => c[1]) };
  }
  function composerChanged(t) {
    const f = document.getElementById("new-issue");
    if (!f) return;
    if (t && t.name === "project") { location.href = `/new-issue?project=${encodeURIComponent(t.value)}`; return; }
    const st = composerState(f);
    f.querySelector("[data-checks]").innerHTML = st.checks
      .map(([label, ok]) => `<li class="${ok ? "ok" : "err"}">${ok ? "✓" : "✗"} ${label}</li>`).join("");
    f.querySelector("[data-queue-button]").disabled = !st.ready;
    const pin = f.elements.pin.value;
    const target = pin
      ? f.querySelector(`input[name="pin"][value="${CSS.escape(pin)}"]`).parentElement.querySelector(".mono").textContent
      : "the whole page";
    f.querySelector("[data-what-checked]").textContent = st.llm
      ? `Automatic: once the preview build matches the branch, open ${f.elements.page.value} at ${f.elements.viewport_w.value}px, find ${target}, capture an after-shot. You: compare against the expected result, then mark it Done.`
      : "Not checked by Claude. You mark it done yourself.";
    if (t && t.name === "pin") repeatCheck(f, pin);
  }
  async function repeatCheck(f, pin) {
    if (!pin) { say("repeat", ""); return; }
    const res = await api("GET", `/api/projects/${f.dataset.project}/repeat-check?pin_comment_id=${encodeURIComponent(pin)}`);
    if (!res.ok) { say("repeat", detail(res), true); return; }
    const r = res.json.repeat;
    say("repeat", r ? `${r.kind === "regression" ? "Regression" : "Fix didn't take"} of #${r.id.slice(0, 8)}: ${r.comment || ""}` : "", !!r);
  }

  document.addEventListener("change", (ev) => {
    const t = ev.target;
    if (t.dataset && t.dataset.projectToggle) {
      document.querySelectorAll(`input[name="round"][data-project="${CSS.escape(t.dataset.projectToggle)}"]:not(:disabled)`)
        .forEach((el) => { el.checked = t.checked; });
    }
    if (t.name === "round" || (t.dataset && t.dataset.projectToggle)) refreshQueueButton();
    if (t.closest && t.closest("#new-issue")) composerChanged(t);
  });
  document.addEventListener("input", (ev) => {
    if (ev.target.closest && ev.target.closest("#new-issue")) composerChanged(ev.target);
  });

  // ---- click actions ----------------------------------------------------------
  document.addEventListener("click", async (ev) => {
    const b = ev.target.closest("[data-action]");
    if (!b) return;
    const a = b.dataset.action;
    if (a === "status") {
      b.disabled = true;
      const res = await api("POST", `/api/comment/${b.dataset.id}/status`,
        { to: b.dataset.to, expected_from: b.dataset.from, actor: "user" });
      if (res.ok) location.reload(); else { say(b.dataset.id, detail(res), true); b.disabled = false; }
    } else if (a === "queue-selected") {
      b.disabled = true;
      const res = await api("POST", "/api/comments/queue", { batch_ids: selectedRounds().map((el) => el.value) });
      if (!res.ok) { say("queue", detail(res), true); b.disabled = false; return; }
      let copied = true;
      try { await navigator.clipboard.writeText("/feedback run"); } catch (e) { copied = false; }
      const sk = res.json.skipped || [];
      say("queue", `Queued ${res.json.queued.length}`
        + (sk.length ? `, skipped ${sk.length} (${[...new Set(sk.map((s) => s.reason))].join(", ")})` : "")
        + (copied ? " · /feedback run copied" : " · copy failed: type /feedback run"), sk.length > 0);
      setTimeout(() => location.reload(), 1500);
    } else if (a === "ship") {
      const res = await api("POST", `/api/rounds/${b.dataset.batch}/mark`, { what: "shipped" });
      if (res.ok) location.reload(); else say("ship", detail(res), true);
    } else if (a === "remind") {
      const res = await api("POST", `/api/projects/${b.dataset.project}/remind`, { draft_only: true });
      say(`remind-${b.dataset.project}`, res.ok ? "Reminder drafted in Gmail" : detail(res), !res.ok);
    } else if (a === "pick-page") {
      const f = document.getElementById("new-issue");
      f.elements.page.value = b.dataset.page;
      composerChanged(f.elements.page);
    }
  });

  const form = document.getElementById("new-issue");
  if (form) {
    composerChanged(null);
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const e = form.elements;
      const body = {
        title: e.title.value, page: e.page.value, recipient: e.recipient.value,
        details: e.details.value || null, viewport_w: Number(e.viewport_w.value),
        expected_result: e.expected_result.value || null, pin_comment_id: e.pin.value || null,
        queue: !!(ev.submitter && ev.submitter.value === "queue"),
      };
      const res = await api("POST", `/api/projects/${form.dataset.project}/issues`, body);
      if (res.ok) location.href = `/round/${res.json.batch_id}`; else say("save", detail(res), true);
    });
  }
  const mapping = document.getElementById("repo-mapping");
  if (mapping) {
    mapping.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const e = mapping.elements;
      const res = await api("POST", `/api/projects/${mapping.dataset.project}`, {
        repo: e.repo.value || null, local_path: e.local_path.value || null,
        url_prefixes: e.url_prefixes.value.split("\n").map((s) => s.trim()).filter(Boolean),
        preview_cmd: e.preview_cmd.value || null, preview_url: e.preview_url.value || null,
      });
      say("mapping", res.ok ? "Saved" : detail(res), !res.ok);
    });
  }
  refreshQueueButton();
})();

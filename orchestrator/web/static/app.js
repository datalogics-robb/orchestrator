// Agents and Runs tabs: polls /api/agents, /api/runs, and /api/health every 5 s while the page is
// visible. The Runs tab submits issues with POST /api/runs and cancels, resumes, and retries runs
// with POST /api/runs/{id}/cancel and /resume. Text from Jira and agents is inserted with
// textContent only.
"use strict";

const REFRESH_MS = 5000;
const STALE_AFTER_FAILURES = 2;
let failures = 0;
let lastRuns = [];
let stopped = false;
let timer = null;

const $ = (id) => document.getElementById(id);

function el(tag, attrs = {}, text) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

function duration(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  if (h) return `${h}h${String(m).padStart(2, "0")}m`;
  if (m) return `${m}m${String(s).padStart(2, "0")}s`;
  return `${s}s`;
}

function shortRun(runId) {
  // 20260924-141500-a1b2c3 -> …141500-a1b2
  const parts = runId.split("-");
  return parts.length === 3 ? `…${parts[1]}-${parts[2].slice(0, 4)}` : runId;
}

function chip(status) {
  return el("span", { class: `chip ${status.replace(/\s+/g, "-")}` }, status);
}

function cells(a) {
  const run = el("td", { class: "run", title: a.run_id + (a.dry_run ? " (dry run)" : "") },
    shortRun(a.run_id) + (a.dry_run ? " ·dry" : ""));

  const issue = el("td");
  const link = el("a", { href: a.issue_url, target: "_blank", rel: "noopener", title: a.summary }, a.key);
  issue.append(link);
  if (a.pr_url) issue.append(el("a", { class: "pr", href: a.pr_url, target: "_blank", rel: "noopener" }, "PR"));

  const status = el("td");
  status.append(chip(a.status));
  if (a.error && (a.status === "blocked" || a.status === "failed" || a.status === "orphaned")) {
    status.append(el("span", { class: "error", title: a.error }, a.error.split("\n")[0].slice(0, 140)));
  }

  return [
    run,
    issue,
    el("td", {}, a.role),
    el("td", { class: "model" }, `${a.runner} / ${a.model || "default"}`),
    el("td", { class: "label" }, a.label || "—"),
    status,
    el("td", { class: "stage", title: a.round ? `fix round ${a.round}` : "" }, a.task_state),
    el("td", { class: "num" }, duration(a.elapsed_seconds)),
    el("td", { class: "num" }, a.turns ?? "—"),
    el("td", { class: "num" }, a.role === "worker" ? `$${a.task_cost_usd.toFixed(2)}` : ""),
  ];
}

function renderAgents(data) {
  const body = $("agents");
  const existing = new Map([...body.rows].map((tr) => [tr.dataset.key, tr]));
  const wanted = [];
  let previousIssue = null;
  for (const a of data.agents) {
    const key = `${a.run_id}/${a.key}/${a.role}`;
    const tr = existing.get(key) || el("tr");
    tr.dataset.key = key;
    tr.replaceChildren(...cells(a));
    const issue = `${a.run_id}/${a.key}`;
    tr.classList.toggle("group-start", previousIssue !== null && issue !== previousIssue);
    previousIssue = issue;
    wanted.push(tr);
    existing.delete(key);
  }
  for (const tr of existing.values()) tr.remove();
  wanted.forEach((tr, i) => {
    if (body.rows[i] !== tr) body.insertBefore(tr, body.rows[i] || null);
  });
  $("empty").hidden = data.agents.length > 0;

  const counts = {};
  for (const a of data.agents) counts[a.status] = (counts[a.status] || 0) + 1;
  $("summary").replaceChildren(
    ...Object.entries(counts).map(([status, n]) => {
      const c = chip(status);
      c.textContent = `${n} ${status}`;
      return c;
    })
  );

  const d = data.daemon;
  $("repo").textContent = d.repo;
  $("daemon").textContent = `daemon ${d.pid} on ${d.host}:${d.port}`;
  $("updated").textContent = new Date(data.generated_at).toLocaleTimeString();
  $("updated").dateTime = data.generated_at;
}

function renderHealth(health) {
  const banner = $("banner");
  const failed = Object.entries(health.logins).filter(([, l]) => l.status === "failed");
  if (failed.length) {
    banner.className = "banner";
    banner.textContent = failed
      .map(([role, l]) => `${role} login (${l.runner}) failed at ${new Date(l.checked).toLocaleTimeString()}: ${l.message}`)
      .join("  ·  ");
    banner.hidden = false;
  } else if (!health.loopback && !health.tls) {
    banner.className = "banner warn";
    banner.textContent = "Served over plain HTTP on a network address: the operator token is not encrypted in transit.";
    banner.hidden = false;
  } else {
    banner.hidden = true;
  }
}

function taskSummary(tasks) {
  return Object.entries(tasks).map(([state, n]) => `${n} ${state}`).join(", ") || "—";
}

const ACTIONS = {
  cancel: {
    label: "Cancel",
    path: "cancel",
    body: {},
    confirm: (r) => `Cancel run ${r.run_id}? Its agents are stopped now; it can be resumed later.`,
    done: (r) => `Run ${r.run_id} cancelled.`,
  },
  resume: {
    label: "Resume",
    path: "resume",
    body: {},
    done: (r) => `Run ${r.run_id} queued to resume.`,
  },
  "retry-failed": {
    label: "Retry failed",
    path: "resume",
    body: { retry_failed: true },
    done: (r) => `Run ${r.run_id} queued to retry its failed tasks.`,
  },
  "retry-blocked": {
    label: "Retry blocked",
    path: "resume",
    body: { retry_blocked: true },
    done: (r) => `Run ${r.run_id} queued to retry its blocked tasks.`,
  },
};

function showRunsResult(text, ok) {
  const result = $("runs-result");
  result.textContent = text;
  result.className = `result ${ok ? "ok" : "bad"}`;
  result.hidden = false;
}

async function runAction(run, name, button) {
  const action = ACTIONS[name];
  let question = action.confirm ? action.confirm(run) : null;
  if (!question && !run.dry_run) {
    question = `${action.label} run ${run.run_id}? It is not a dry run: it pushes branches, opens PRs, and writes to Jira.`;
  }
  if (question && !confirm(question)) return;
  button.disabled = true;
  try {
    const r = await fetch(`/api/runs/${encodeURIComponent(run.run_id)}/${action.path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(action.body),
    });
    const body = await r.json().catch(() => ({}));
    if (r.ok) showRunsResult(action.done(run), true);
    else showRunsResult(typeof body.detail === "string" ? body.detail : `HTTP ${r.status}`, false);
    refresh();
  } catch (e) {
    showRunsResult(`Could not reach the daemon: ${e.message}`, false);
  } finally {
    button.disabled = false;
  }
}

function actionCell(run) {
  const td = el("td");
  const box = el("div", { class: "actions" });
  for (const name of run.actions) {
    const button = el("button", { type: "button", class: `action ${name}` }, ACTIONS[name].label);
    button.addEventListener("click", () => runAction(run, name, button));
    box.append(button);
  }
  td.append(box);
  return td;
}

function renderRuns(data) {
  lastRuns = data.runs;
  const body = $("runs");
  body.replaceChildren(
    ...data.runs.map((r) => {
      const tr = el("tr");
      const status = el("td");
      status.append(chip(r.status));
      tr.append(
        el("td", { class: "run", title: r.run_id }, r.run_id + (r.dry_run ? " ·dry" : "")),
        status,
        el("td", { class: "keys" }, r.keys.join(" ")),
        el("td", { class: "tasks" }, taskSummary(r.tasks)),
        el("td", { title: r.started }, new Date(r.started).toLocaleString()),
        el("td", { class: "model" }, r.by_this_daemon ? "this daemon" : r.owner || "—"),
        el("td", { class: "num" }, `$${r.cost_usd.toFixed(2)}`),
        actionCell(r),
      );
      return tr;
    })
  );
  $("runs-empty").hidden = data.runs.length > 0;
}

function showResult(text, ok) {
  const result = $("submit-result");
  result.textContent = text;
  result.className = `result ${ok ? "ok" : "bad"}`;
  result.hidden = false;
}

async function submitRun(event) {
  event.preventDefault();
  const keys = $("keys").value.split(/[\s,]+/).filter(Boolean);
  const dryRun = $("dry-run").checked;
  if (!keys.length) return;
  if (!dryRun && !confirm(`Start a real run for ${keys.join(", ")}? It pushes branches, opens PRs, and writes to Jira.`)) return;
  const button = $("submit-button");
  button.disabled = true;
  showResult("Reading the issues from Jira…", true);
  try {
    const r = await fetch("/api/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ keys, workflow: $("workflow").value, dry_run: dryRun }),
    });
    const body = await r.json().catch(() => ({}));
    if (r.ok) {
      showResult(`Run ${body.run_id} queued: ${body.keys.join(", ")}${body.dry_run ? " (dry run)" : ""}.`, true);
      $("keys").value = "";
      refresh();
    } else {
      const detail = typeof body.detail === "string" ? body.detail : `HTTP ${r.status}`;
      showResult(detail, false);
    }
  } catch (e) {
    showResult(`Could not reach the daemon: ${e.message}`, false);
  } finally {
    button.disabled = false;
  }
}

function selectTab(name) {
  for (const tab of document.querySelectorAll(".tab[aria-controls]")) {
    const selected = tab.id === `tab-${name}`;
    tab.setAttribute("aria-selected", String(selected));
    $(tab.getAttribute("aria-controls")).hidden = !selected;
  }
  try { localStorage.setItem("tab", name); } catch (e) { /* storage unavailable */ }
}

function showStopped(interrupted) {
  stopped = true;
  clearInterval(timer);
  const banner = $("banner");
  banner.className = "banner stopped";
  banner.textContent =
    "The daemon has shut down" +
    (interrupted.length ? `; ${interrupted.length} run(s) were interrupted and can be resumed` : "") +
    ". Start it again with orchestrator serve.";
  banner.hidden = false;
  $("stale").hidden = true;
  for (const b of document.querySelectorAll("button")) if (!b.classList.contains("tab")) b.disabled = true;
}

async function shutdown() {
  const live = lastRuns.filter((r) => r.actions.includes("cancel"));
  const question = live.length
    ? `Shut down the orchestrator? ${live.length} run(s) it is driving will be interrupted: their agents are ` +
      "stopped now and their tasks keep their progress, so they can be resumed later."
    : "Shut down the orchestrator? This page stops working until it is started again.";
  if (!confirm(question)) return;
  $("shutdown").disabled = true;
  try {
    const r = await fetch("/api/shutdown", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(typeof body.detail === "string" ? body.detail : `HTTP ${r.status}`);
    showStopped(body.interrupted || []);
  } catch (e) {
    $("shutdown").disabled = false;
    alert(`Could not shut down: ${e.message}`);
  }
}

function markStale() {
  failures += 1;
  $("stale").hidden = failures < STALE_AFTER_FAILURES;
}

async function refresh() {
  if (document.hidden || stopped) return;
  try {
    const [agents, runs, health] = await Promise.all([
      fetch("/api/agents", { cache: "no-store" }),
      fetch("/api/runs", { cache: "no-store" }),
      fetch("/api/health", { cache: "no-store" }),
    ]);
    if (!agents.ok || !runs.ok || !health.ok) throw new Error(`HTTP ${agents.status}/${runs.status}/${health.status}`);
    renderAgents(await agents.json());
    renderRuns(await runs.json());
    renderHealth(await health.json());
    failures = 0;
    $("stale").hidden = true;
  } catch (e) {
    markStale();
  }
}

for (const tab of document.querySelectorAll(".tab[aria-controls]")) {
  tab.addEventListener("click", () => selectTab(tab.id.replace("tab-", "")));
}
$("submit").addEventListener("submit", submitRun);
$("shutdown").addEventListener("click", shutdown);
$("dry-run").addEventListener("change", () => {
  $("dry-hint").hidden = !$("dry-run").checked;
  $("live-hint").hidden = $("dry-run").checked;
});
let initialTab = location.hash.slice(1);
try { initialTab = initialTab || localStorage.getItem("tab"); } catch (e) { /* storage unavailable */ }
if (initialTab && $(`tab-${initialTab}`)) selectTab(initialTab);

timer = setInterval(refresh, REFRESH_MS);
document.addEventListener("visibilitychange", refresh);
refresh();

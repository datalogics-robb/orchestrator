// Agents and Runs tabs: polls /api/agents, /api/runs, and /api/health every 5 s while the page is
// visible. The Runs tab submits issues with POST /api/runs and cancels, resumes, and retries runs
// with POST /api/runs/{id}/cancel and /resume. A task's Details drawer explains where it stands
// and why it stopped, and lets the operator ask its worker a question or send it back with an
// instruction. Text from Jira and agents is inserted with textContent only.
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
  if (a.role === "worker") {
    const why = a.status === "blocked" || a.status === "failed";
    const details = el("button", { type: "button", class: "link", title: `What happened to ${a.key}` }, why ? "why?" : "details");
    details.addEventListener("click", () => openDetail(a.run_id, a.key));
    issue.append(details);
  }

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

function taskCell(run) {
  // one details link per task, so a run that left the Agents tab can still be looked into
  const td = el("td", { class: "tasks", title: taskSummary(run.tasks) });
  for (const [key, state] of Object.entries(run.task_states)) {
    const b = el("button", { type: "button", class: `link task ${state.toLowerCase()}`, title: `${key}: ${state}` },
      `${key} ${state.toLowerCase()}`);
    b.addEventListener("click", () => openDetail(run.run_id, key));
    td.append(b);
  }
  if (!td.childNodes.length) td.textContent = "—";
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
        taskCell(r),
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

// --- task detail drawer ------------------------------------------------------------------
// GET /api/runs/{id}/tasks/{key} while the drawer is open; POST .../ask and .../instruct.

let openTask = null; // {run_id, key}
let lastDetail = "";
let reportOpen = null; // the operator's choice for the findings <details>, kept across refreshes

function inline(parent, text) {
  // `code`, **bold**, and [label](https://…) links; everything else is plain text
  const re = /(`[^`]+`|\*\*[^*]+\*\*|\[[^\]]+\]\((https?:\/\/[^)\s]+)\))/g;
  let last = 0;
  let m;
  while ((m = re.exec(text))) {
    if (m.index > last) parent.append(text.slice(last, m.index));
    const t = m[0];
    if (t.startsWith("`")) parent.append(el("code", {}, t.slice(1, -1)));
    else if (t.startsWith("**")) parent.append(el("strong", {}, t.slice(2, -2)));
    else parent.append(el("a", { href: m[2], target: "_blank", rel: "noopener" }, t.slice(1, t.indexOf("]("))));
    last = re.lastIndex;
  }
  if (last < text.length) parent.append(text.slice(last));
}

function markdown(text) {
  // headings, lists, fenced code, and paragraphs, built as DOM nodes: agent text never becomes HTML
  const root = el("div", { class: "md" });
  const lines = (text || "").replace(/\r\n/g, "\n").split("\n");
  let list = null;
  let para = [];
  const flush = () => {
    if (para.length) {
      const p = el("p");
      inline(p, para.join(" "));
      root.append(p);
      para = [];
    }
  };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (line.trimStart().startsWith("```")) {
      flush();
      list = null;
      const code = [];
      while (++i < lines.length && !lines[i].trimStart().startsWith("```")) code.push(lines[i]);
      root.append(el("pre", {}, code.join("\n")));
      continue;
    }
    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    if (heading) {
      flush();
      list = null;
      const node = el(`h${Math.min(6, heading[1].length + 2)}`);
      inline(node, heading[2]);
      root.append(node);
      continue;
    }
    const item = /^\s*([-*]|\d+\.)\s+(.*)$/.exec(line);
    if (item) {
      flush();
      if (!list) {
        list = el(/\d/.test(item[1]) ? "ol" : "ul");
        root.append(list);
      }
      const li = el("li");
      inline(li, item[2]);
      list.append(li);
      continue;
    }
    if (!line.trim()) {
      flush();
      list = null;
      continue;
    }
    if (list && /^\s{2,}/.test(line)) {
      inline(list.lastChild, " " + line.trim());
      continue;
    }
    list = null;
    para.push(line.trim());
  }
  flush();
  return root;
}

function section(title, ...children) {
  const box = el("section");
  box.append(el("h3", {}, title), ...children);
  return box;
}

function link(href, text) {
  return el("a", { href, target: "_blank", rel: "noopener" }, text);
}

const SPEAKER = {
  question: (m) => `You asked (${m.by})`,
  answer: () => "Worker",
  instruction: (m) => `You instructed (${m.by})`,
  error: () => "Error",
};

function renderDetail(d) {
  $("detail-title").textContent = `${d.key} · ${d.summary}`;
  const body = $("detail-body");
  const scroll = body.scrollTop;
  const parts = [];

  const meta = el("div", { class: "detail-meta" });
  meta.append(chip(d.state.toLowerCase()), el("span", { title: d.run_id }, `run ${d.run_id}${d.dry_run ? " · dry run" : ""}`));
  meta.append(link(d.issue_url, "Jira"));
  if (d.pr_url) meta.append(link(d.pr_url, "Pull request"));
  if (d.confluence_url) meta.append(link(d.confluence_url, "Confluence report"));
  parts.push(meta, el("p", { class: `headline ${d.state}` }, d.headline));

  if (d.questions.length) {
    const ul = el("ul");
    for (const q of d.questions) ul.append(el("li", {}, q));
    parts.push(section("Questions for the reporter", ul));
  }

  if (d.findings_markdown) {
    const report = el("details", { class: "report" });
    report.open = reportOpen ?? d.state === "BLOCKED";
    report.addEventListener("toggle", () => { reportOpen = report.open; });
    report.append(el("summary", {}, "Findings report"), markdown(d.findings_markdown));
    parts.push(section("Why it stopped", report));
  } else if (d.blocked_details) {
    parts.push(section("Why it stopped", markdown(d.blocked_details)));
  } else if (d.state === "FAILED" && d.reason) {
    parts.push(section("Error", el("pre", { class: "md" }, d.reason)));
  }
  if (d.worker_summary && d.state !== "BLOCKED") parts.push(section("Worker's summary", markdown(d.worker_summary)));

  const facts = el("dl", { class: "facts" });
  const fact = (k, v) => { if (v !== null && v !== undefined && v !== "") facts.append(el("dt", {}, k), el("dd", {}, v)); };
  fact("Stopped in", d.stopped_at);
  fact("Fix rounds used", String(d.round));
  fact("Branch", d.branch);
  fact("Worktree", d.worktree_path && `${d.worktree_path}${d.worktree_exists ? "" : " (removed)"}`);
  fact("Changed files", d.changed_paths.join(", "));
  fact("Cost", `$${d.cost_usd.toFixed(2)}`);
  parts.push(section("Details", facts));

  if (d.conversation.length || d.answering) {
    const box = el("div", { class: "messages" });
    for (const m of d.conversation) {
      const msg = el("div", { class: `message ${m.kind}` });
      msg.append(el("div", { class: "who" }, `${SPEAKER[m.kind](m)} · ${new Date(m.ts).toLocaleString()}`), markdown(m.text));
      box.append(msg);
    }
    if (d.answering) box.append(el("p", { class: "answering" }, "The worker is answering…"));
    parts.push(section("Conversation", box));
  }

  body.replaceChildren(...parts);
  body.scrollTop = scroll;

  const canAsk = d.actions.includes("ask");
  const canInstruct = d.actions.includes("instruct");
  $("talk").hidden = !(canAsk || canInstruct || d.answering || d.run_status === "running");
  $("ask").disabled = !canAsk;
  $("instruct").disabled = !canInstruct;
  $("talk-text").disabled = !(canAsk || canInstruct);
  let hint;
  if (d.answering) hint = "The worker is answering your question; you can write again when it has.";
  else if (!canAsk && !canInstruct) hint = "The run is busy; you can write to the worker once it stops.";
  else {
    hint = canAsk ? "Ask: the worker answers from its own session, read-only; the task does not change. " : "";
    if (canInstruct) {
      hint += "Instruct and retry: reopens this task where it stopped and resumes the worker with your instruction; " +
        "if it then completes, the usual build, tests, review, and pull request follow.";
    }
  }
  $("talk-hint").textContent = hint;
}

async function refreshDetail() {
  if (!openTask || document.hidden || stopped) return;
  const { run_id, key } = openTask;
  const r = await fetch(`/api/runs/${encodeURIComponent(run_id)}/tasks/${encodeURIComponent(key)}`, { cache: "no-store" });
  if (!openTask || openTask.run_id !== run_id || openTask.key !== key) return; // closed or switched meanwhile
  if (!r.ok) {
    $("detail-body").replaceChildren(el("p", { class: "result bad" }, `Could not load the task: HTTP ${r.status}`));
    return;
  }
  const text = await r.text();
  if (text === lastDetail) return; // unchanged: keep the reader's place and open sections
  lastDetail = text;
  const data = JSON.parse(text);
  openTask.data = data;
  renderDetail(data);
}

function openDetail(run_id, key) {
  openTask = { run_id, key };
  lastDetail = "";
  reportOpen = null;
  $("detail-title").textContent = key;
  $("detail-body").replaceChildren(el("p", { class: "empty" }, "Loading…"));
  $("talk").hidden = true;
  $("talk-result").hidden = true;
  $("detail").hidden = false;
  refreshDetail().catch(() => {});
}

function closeDetail() {
  openTask = null;
  $("detail").hidden = true;
}

function showTalkResult(text, ok) {
  const result = $("talk-result");
  result.textContent = text;
  result.className = `result ${ok ? "ok" : "bad"}`;
  result.hidden = false;
}

async function talk(kind) {
  if (!openTask || !openTask.data) return;
  const d = openTask.data;
  const text = $("talk-text").value.trim();
  if (!text) {
    showTalkResult(kind === "ask" ? "Write a question first." : "Write the instruction first.", false);
    return;
  }
  if (kind === "instruct" && !d.dry_run &&
      !confirm(`Send ${d.key} back to the worker? Run ${d.run_id} is not a dry run: if the worker completes ` +
               "the task, the orchestrator pushes a branch, opens a draft PR, and writes to Jira.")) return;
  const buttons = [$("ask"), $("instruct")];
  buttons.forEach((b) => { b.disabled = true; });
  try {
    const r = await fetch(`/api/runs/${encodeURIComponent(d.run_id)}/tasks/${encodeURIComponent(d.key)}/${kind}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(kind === "ask" ? { question: text } : { instruction: text }),
    });
    const body = await r.json().catch(() => ({}));
    if (r.ok) {
      $("talk-text").value = "";
      showTalkResult(kind === "ask"
        ? "Asked. The answer appears in the conversation when the worker replies."
        : `Run ${d.run_id} queued: ${d.key} goes back to the worker with your instruction.`, true);
    } else {
      showTalkResult(typeof body.detail === "string" ? body.detail : `HTTP ${r.status}`, false);
    }
  } catch (e) {
    showTalkResult(`Could not reach the daemon: ${e.message}`, false);
  } finally {
    lastDetail = "";
    refresh();
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
    await refreshDetail();
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
$("detail-close").addEventListener("click", closeDetail);
$("ask").addEventListener("click", () => talk("ask"));
$("instruct").addEventListener("click", () => talk("instruct"));
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && openTask) closeDetail(); });
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

// Agents tab: polls /api/agents every 5 s while the page is visible. Text from Jira and agents is
// inserted with textContent only.
"use strict";

const REFRESH_MS = 5000;
const STALE_AFTER_FAILURES = 2;
let failures = 0;

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

function markStale() {
  failures += 1;
  $("stale").hidden = failures < STALE_AFTER_FAILURES;
}

async function refresh() {
  if (document.hidden) return;
  try {
    const [agents, health] = await Promise.all([
      fetch("/api/agents", { cache: "no-store" }),
      fetch("/api/health", { cache: "no-store" }),
    ]);
    if (!agents.ok || !health.ok) throw new Error(`HTTP ${agents.status}/${health.status}`);
    renderAgents(await agents.json());
    renderHealth(await health.json());
    failures = 0;
    $("stale").hidden = true;
  } catch (e) {
    markStale();
  }
}

setInterval(refresh, REFRESH_MS);
document.addEventListener("visibilitychange", refresh);
refresh();

// Harness² Lab — single-page front end. No build step: plain ES modules + inline SVG.
import { dag, familyLegend, genomeHTML, hideTip, pareto, scoreBars } from "./charts.js";

const REPO = "https://github.com/jacobhroutzong/harness2-lab";
const $ = (sel, root = document) => root.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const fmt = (v, d = 1) => (v === null || v === undefined || Number.isNaN(v) ? "—" : Number(v).toFixed(d));
const signed = (v, d = 1) => (v >= 0 ? "+" : "−") + Math.abs(v).toFixed(d);

function store(key, value) {
  try { if (value === undefined) return localStorage.getItem(key); localStorage.setItem(key, value); } catch { return null; }
  return null;
}

async function api(path, body) {
  const res = await fetch(path, body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  if (!res.ok) {
    let msg = `${res.status}`;
    try { msg = (await res.json()).detail || msg; } catch { /* keep status */ }
    throw new Error(msg);
  }
  return res.json();
}

// ------------------------------------------------------------------ theme
(function theme() {
  const saved = store("h2-theme");
  if (saved) document.documentElement.dataset.theme = saved;
  $("#theme-toggle").addEventListener("click", () => {
    const cur = document.documentElement.dataset.theme
      || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    const next = cur === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    store("h2-theme", next);
  });
})();

// ------------------------------------------------------------------ drawer
const drawer = { tabs: [] };
function openDrawer(kicker, title, tabs) {
  drawer.tabs = tabs;
  $("#drawer-kicker").textContent = kicker;
  $("#drawer-title").textContent = title;
  const bar = $("#drawer-tabs");
  bar.innerHTML = "";
  tabs.forEach((t, i) => {
    const b = document.createElement("button");
    b.type = "button";
    b.role = "tab";
    b.textContent = t.name;
    b.addEventListener("click", () => showTab(i));
    bar.append(b);
  });
  showTab(0);
  $("#drawer").classList.add("open");
  $("#drawer").setAttribute("aria-hidden", "false");
  $("#scrim").hidden = false;
}
function showTab(i) {
  [...$("#drawer-tabs").children].forEach((b, j) => b.classList.toggle("on", i === j));
  const body = $("#drawer-body");
  body.innerHTML = "";
  const content = drawer.tabs[i].render();
  if (typeof content === "string") body.innerHTML = content; else body.append(content);
}
function closeDrawer() {
  $("#drawer").classList.remove("open");
  $("#drawer").setAttribute("aria-hidden", "true");
  $("#scrim").hidden = true;
}
$("#drawer-close").addEventListener("click", closeDrawer);
$("#scrim").addEventListener("click", closeDrawer);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });

function diffHTML(patch) {
  if (!patch) return '<p class="muted">No change (KEEP_BASE).</p>';
  const lines = patch.split("\n").map((ln) => {
    const cls = ln.startsWith("+++") || ln.startsWith("---") ? "hunk" : ln.startsWith("@@") ? "hunk"
      : ln.startsWith("+") ? "add" : ln.startsWith("-") ? "del" : "";
    return cls ? `<span class="${cls}">${esc(ln)}</span>` : esc(ln) + "\n";
  });
  return `<pre class="diff">${lines.join("")}</pre>`;
}

function filesView(files) {
  const wrap = document.createElement("div");
  const names = Object.keys(files || {}).sort();
  if (!names.length) { wrap.innerHTML = '<p class="muted">No files.</p>'; return wrap; }
  const tabs = document.createElement("div");
  tabs.className = "file-tabs";
  const pre = document.createElement("pre");
  pre.className = "code wrap";
  const pick = (name) => {
    [...tabs.children].forEach((b) => b.classList.toggle("on", b.dataset.name === name));
    pre.textContent = files[name] || "(empty file)";
  };
  for (const n of names) {
    const b = document.createElement("button");
    b.type = "button";
    b.dataset.name = n;
    b.textContent = n;
    b.addEventListener("click", () => pick(n));
    tabs.append(b);
  }
  wrap.append(tabs, pre);
  pick(names.includes("systemprompt.md") ? names.find((n) => n.startsWith("skills/")) || "systemprompt.md" : names[0]);
  return wrap;
}

function trajectoryView(traj) {
  const wrap = document.createElement("div");
  if (!traj) { wrap.innerHTML = '<p class="muted">Not available.</p>'; return wrap; }
  const head = `<p class="small ink-2">stop: <b>${esc(traj.stop_reason)}</b> · ${traj.turns} turns · ${traj.tool_calls} tool calls · ${traj.tool_errors} errors`
    + (traj.plugin_firings?.length ? ` · ${traj.plugin_firings.length} plugin firings` : "") + "</p>";
  const steps = traj.steps.map((s) => {
    if (s.kind === "assistant") return s.text ? `<div class="step"><div class="k">t${s.turn} · model</div><div class="small">${esc(s.text)}</div></div>` : "";
    if (s.kind === "call") return `<div class="step call"><div class="k">t${s.turn} · call ${esc(s.name)}</div><pre class="code wrap">${esc(JSON.stringify(s.args, null, 1)).slice(0, 4000)}</pre></div>`;
    if (s.kind === "result") return `<div class="step ${s.is_error ? "err" : ""}"><div class="k">${s.is_error ? "error" : "result"}</div><pre class="code wrap">${esc(s.output).slice(0, 4000)}</pre></div>`;
    return `<div class="step"><div class="k">runtime</div><div class="small">${esc(s.text)}</div></div>`;
  }).join("");
  wrap.innerHTML = head + steps + `<p class="small"><b>Final:</b> ${esc(traj.final || "(none)")}</p>`;
  return wrap;
}

// ------------------------------------------------------------------ router
const routes = { overview: viewOverview, lab: viewLab, library: viewLibrary, budget: viewBudget, code: viewCode };
let META = null;
async function route() {
  const name = (location.hash.replace(/^#\/?/, "") || "overview").split("?")[0] || "overview";
  const key = routes[name] ? name : "overview";
  document.querySelectorAll(".tabs a").forEach((a) => a.classList.toggle("active", a.dataset.route === key));
  if (!META) {
    try { META = await api("/api/meta"); } catch (e) { $("#view").innerHTML = `<div class="empty">Could not reach the server: ${esc(e.message)}</div>`; return; }
  }
  hideTip();
  $("#view").innerHTML = "";
  routes[key]($("#view"));
  window.scrollTo(0, 0);
}
window.addEventListener("hashchange", route);
let resizeTimer = null;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {
    if (location.hash.startsWith("#/lab")) renderLabMain();
    if (location.hash.startsWith("#/budget")) renderBudget();
  }, 200);
});

// ------------------------------------------------------------------ overview
function pipelineDiagram() {
  const box = (x, y, w, label, sub, color) => `
    <g transform="translate(${x},${y})">
      <rect width="${w}" height="58" rx="10" fill="var(--surface)" stroke="var(--border-strong)"/>
      <rect x="0" y="11" width="4" height="36" rx="2" fill="${color}"/>
      <text x="15" y="25" font-size="14" font-weight="650">${label}</text>
      <text x="15" y="43" font-size="11.5" style="fill:var(--muted)">${sub}</text>
    </g>`;
  const arrow = (d) => `<path d="${d}" stroke="var(--axis)" stroke-width="2" marker-end="url(#ah)" fill="none"/>`;
  const label = (x, y, t) => `<text x="${x}" y="${y}" font-size="11" font-weight="700" letter-spacing="0.08em" style="fill:var(--muted)">${t}</text>`;
  return `<svg viewBox="0 0 1000 330" role="img" aria-label="The Harness² pipeline: task-level recursion above, harnessing-level recursion below">
    <defs><marker id="ah" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="var(--axis)"/></marker>
      <marker id="ahd" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="var(--dom)"/></marker></defs>
    ${label(10, 18, "TASK-LEVEL RECURSION · Φ(q, H, E; R) — no ground truth, only behavioural contrasts")}
    ${box(10, 34, 150, "Reference", "k runs of the base H0", "var(--neutral-mark)")}
    ${arrow("M162,63 L196,63")}
    <g transform="translate(198,34)">
      <rect width="182" height="58" rx="10" fill="var(--surface)" stroke="var(--border-strong)"/>
      <rect x="0" y="11" width="4" height="17" rx="2" fill="var(--inv)"/><rect x="0" y="30" width="4" height="17" rx="2" fill="var(--dom)"/>
      <text x="15" y="25" font-size="14" font-weight="650">Propose</text>
      <text x="15" y="43" font-size="11.5" style="fill:var(--muted)">INV + DOM candidate edits</text>
    </g>
    ${arrow("M382,63 L416,63")}
    ${box(418, 34, 150, "Probe", "run each candidate once", "var(--neutral-mark)")}
    ${arrow("M570,63 L604,63")}
    ${box(606, 34, 170, "Compose", "merge · adopt one · keep", "var(--compose)")}
    ${arrow("M778,63 L812,63")}
    ${box(814, 34, 176, "Final → Select", "blind pick among k finals", "var(--ink-2)")}
    <path d="M690,94 C690,132 290,132 290,96" stroke="var(--axis)" stroke-width="2" fill="none" marker-end="url(#ah)"/>
    <text x="490" y="146" text-anchor="middle" font-size="11.5" style="fill:var(--muted)">sequential mode: each checkpoint becomes the next round's H (depth k) · parallel: k composers side by side (width k)</text>
    ${label(10, 190, "HARNESSING-LEVEL RECURSION · R(t+1) = Update(R(t), H*, D)")}
    ${box(814, 206, 176, "Reflect", "edit verdicts + ≤ 1 lesson", "var(--compose)")}
    ${arrow("M902,94 L902,204")}
    ${arrow("M812,235 L612,235")}
    ${box(430, 206, 180, "Update", "store H* + EXPERIENCE.md", "var(--compose)")}
    ${arrow("M428,235 L232,235")}
    ${box(10, 206, 220, "Library R (one per domain)", "starts empty for every stream", "var(--dom)")}
    <path d="M120,204 C120,160 250,160 250,96" stroke="var(--dom)" stroke-width="2" fill="none" stroke-dasharray="5 4" marker-end="url(#ahd)"/>
    <text x="10" y="300" font-size="11.5" style="fill:var(--muted)">The next task starts from H0 again — the library is what carries forward: its proposers read earlier adopted harnesses and lessons.</text>
  </svg>`;
}

function viewOverview(root) {
  const comps = META.components.map((c) => `
    <div class="card flat comp">
      <div class="spread"><span class="key">${c.key}</span><span class="chip">${esc(c.activation)}</span></div>
      <h3 style="margin-top:6px">${esc(c.name)}</h3>
      <div class="path">${esc(c.path)}</div>
      <p class="small ink-2" style="margin:6px 0 0">${esc(c.role)}</p>
    </div>`).join("");
  root.innerHTML = `
    <section class="hero">
      <div>
        <div class="kicker">Recursive agent harnessing · arXiv 2610.14224</div>
        <h1>Keep the model frozen.<br>Improve its harness — recursively.</h1>
        <div class="equation">Agent = Model + Harness</div>
        <p class="lede">Harness² improves the scaffolding around a frozen LLM — prompts, rules, memory, tools,
          skills, scripts, subagents and middleware — <b>without any ground-truth feedback</b>. For each new task it
          proposes edits, probes them on the same task, and composes a better harness from the behavioural
          contrasts. Then it turns what it learned into a library that the next task's improvers read.</p>
        <div class="row">
          <a class="btn primary" href="#/lab?demo=1">Run the offline demo →</a>
          <a class="btn" href="#/budget">Side quest: adaptive recursion</a>
          <a class="btn" href="${REPO}" target="_blank" rel="noopener">Source</a>
        </div>
      </div>
    </section>
    <section class="card diagram" style="margin-top:22px">${pipelineDiagram()}</section>

    <section class="section grid cols-3">
      <div class="card"><div class="kicker">Level 1 · task</div><h3>Propose → Probe → Compose</h3>
        <p class="small ink-2">Two candidates per step: <b>INV</b> fixes <i>how the agent works</i> (tool use, verification, budget),
        <b>DOM</b> adds <i>what a practitioner knows</i>. Each is run once on the same task; the composer keeps only edits a
        trajectory shows working. Run it wide (parallel, k composers + blind selection) or deep (sequential, k rounds).</p></div>
      <div class="card"><div class="kicker">Level 2 · harnessing</div><h3>Reflect → Update the library</h3>
        <p class="small ink-2">After a task, a reflector reads the whole record and writes per-edit verdicts plus at most
        <b>one</b> lesson the contrasts support. The adopted harness and its EXPERIENCE.md enter a per-domain library.
        No evidence, no entry.</p></div>
      <div class="card"><div class="kicker">No ground truth</div><h3>Contrasts, not scores</h3>
        <p class="small ink-2">Improvers never see a rubric, grader or score — the evidence builder refuses one. Here a
        hidden grader scores every deliverable <i>after</i> each task, purely for the scoreboard ("oracle view").</p></div>
    </section>

    <section class="section">
      <div class="spread"><h2>The eight editable components</h2>
        <div class="legend-row"><span><i class="dot ref"></i>always-on costs context on every run</span><span><i class="dot compose"></i>on-demand costs nothing until used</span></div></div>
      <div class="grid cols-4" style="margin-top:12px">${comps}</div>
    </section>

    <section class="section grid cols-2">
      <div class="card">
        <h2>What is real, what is simulated</h2>
        <ul class="small ink-2 prose" style="padding-left:18px;margin:0">
          <li><b>Real everywhere:</b> the agent loop, sandboxed tools, plugin middleware, the harness validator
            (trigger lines, template braces, dangling scripts), the evidence workspaces, Propose/Probe/Compose,
            parallel & sequential recursion, blind selection, reflection, the library, provenance and the hidden graders.</li>
          <li><b>Simulator (default, no key):</b> a scripted-but-stochastic stand-in model drives every role through
            those same code paths, with persistent knowledge gaps and visible symptoms. Its numbers describe a toy
            world, not the paper's benchmarks.</li>
          <li><b>Real models:</b> paste an Anthropic key or any OpenAI-compatible endpoint (Hugging Face router,
            OpenRouter, Gemini, a local vLLM) in Run Lab; keys are used for that run only and never stored.</li>
        </ul>
      </div>
      <div class="card">
        <h2>Built-in task suite</h2>
        <p class="small ink-2">Nine professional tasks in three domains, each with input files, an exact deliverable spec
        and a hidden grader. Domain knowledge recurs within a domain — which is what the library can carry.</p>
        <table class="data"><thead><tr><th>Task</th><th>Domain</th><th>Trap</th></tr></thead><tbody>
          ${META.tasks.map((t) => `<tr><td><code>${t.id}</code> ${esc(t.title)}</td><td>${t.domain}</td><td class="small ink-2">${esc(t.blurb)}</td></tr>`).join("")}
        </tbody></table>
      </div>
    </section>`;
}

// ------------------------------------------------------------------ lab state
const LAB = {
  cfg: {
    backend: "sim", api_key: "", base_url: "huggingface", executor_model: "", editor_model: "",
    tasks: ["fin-01", "fin-02", "fin-03"], mode: "parallel", k: 2, seed: 0,
    library: true, scoping: true, composition: true, adaptive_budget: 2.0, max_cost_usd: 5,
  },
  speed: "narrated",
  run: null, source: null, queue: [], timer: null, last: 0,
  tasks: {}, order: [], current: null, pinned: null, feed: [], results: [], summary: null,
  libraries: {}, error: "", status: "idle",
};

const COLS = [
  ["reference", "Reference"], ["propose", "Propose"], ["probe", "Probe"], ["compose", "Compose"],
  ["final", "Final"], ["adopt", "Select · Reflect · Update"],
];

// "skills/x/SKILL.md" -> "skill:x", "guardrails.md" -> "guardrails" ...
function shortPath(p) {
  let m;
  if ((m = p.match(/^skills\/([^/]+)\/SKILL\.md$/))) return `skill:${m[1]}`;
  if ((m = p.match(/^skills\/([^/]+)\/scripts\/(.+)$/))) return `skill-script:${m[2]}`;
  if ((m = p.match(/^sub_agents\/([^/]+)\//))) return `agent:${m[1]}`;
  if ((m = p.match(/^plugin\/(.+)\.json$/))) return `plugin:${m[1]}`;
  if ((m = p.match(/^scripts\/(.+)$/))) return `script:${m[1]}`;
  return { "LongTermMEMORY.md": "memory", "tool_descriptions/tool_guidance.md": "tool-guidance" }[p] || p.replace(/\.md$/, "");
}
function changeChips(diff) {
  const items = [...diff.added.map((p) => ["add", "+", p]), ...diff.modified.map((p) => ["", "~", p]),
    ...diff.removed.map((p) => ["del", "−", p])];
  return `<div class="chg">${items.map(([c, sym, p]) => `<span class="${c}" title="${esc(p)}">${sym}${esc(shortPath(p))}</span>`).join("")}</div>`;
}

function kindOf(label) {
  if (/^base\d+$/.test(label)) return "ref";
  if (/^inv\d+$/.test(label)) return "inv";
  if (/^dom\d+$/.test(label)) return "dom";
  return "compose";
}

function T(id) {
  if (!LAB.tasks[id]) {
    const meta = META.tasks.find((t) => t.id === id) || { id, title: id, domain: "" };
    LAB.tasks[id] = { id, title: meta.title, domain: meta.domain, status: "pending", nodes: {}, phase: null };
  }
  return LAB.tasks[id];
}

function node(task, label, col) {
  const n = task.nodes[label] || (task.nodes[label] = { label, kind: kindOf(label), col });
  n.col = n.col || col;
  return n;
}

function apply(ev) {
  const t = ev.task ? T(ev.task) : null;
  switch (ev.type) {
    case "stream_start":
      LAB.order = ev.tasks;
      ev.tasks.forEach((id) => T(id));
      LAB.executor = ev.executor; LAB.editor = ev.editor;
      break;
    case "task_start":
      t.status = "running"; t.k = ev.k; t.mode = ev.mode; t.libraryEntries = ev.library_entries;
      LAB.current = ev.task;
      break;
    case "phase":
      t.phase = ev.name;
      if (ev.name === "select") t.selectPending = true;
      break;
    case "rollout_start": {
      const lbl = ev.label;
      const col = lbl.startsWith("base") ? "reference" : /^(inv|dom)/.test(lbl) ? "probe" : "final";
      const n = node(t, `${lbl}#roll`, col);
      Object.assign(n, { kind: kindOf(lbl), name: lbl, status: "running", isRollout: true });
      break;
    }
    case "rollout_end": {
      const n = node(t, `${ev.label}#roll`);
      Object.assign(n, { status: "done", rollout: ev.rollout, trajectory: ev.trajectory, outputs: ev.outputs });
      break;
    }
    case "stage_start": {
      const col = ev.dim === "COMPOSE" ? "compose" : "propose";
      Object.assign(node(t, ev.label, col), { name: ev.label, status: "running", dim: ev.dim });
      break;
    }
    case "stage_end":
      Object.assign(node(t, ev.stage.label), { status: "done", stage: ev.stage });
      break;
    case "selection": t.selection = ev.selection; t.selectPending = false; break;
    case "reflection": t.experience = ev.experience; break;
    case "library_update": t.libraryAdded = ev.added; LAB.libraries[ev.domain] = ev.library; break;
    case "adaptive": t.adaptive = { ...ev }; break;
    case "adaptive_round": (t.adaptiveRounds = t.adaptiveRounds || []).push(ev); break;
    case "task_end":
      t.status = "done"; t.result = ev.result;
      LAB.results = LAB.results.filter((r) => r.task_id !== ev.result.task_id).concat([ev.result]);
      break;
    case "task_error": t.status = "error"; t.error = ev.error; break;
    case "step":
      LAB.feed.push(ev);
      if (LAB.feed.length > 300) LAB.feed.splice(0, LAB.feed.length - 300);
      break;
    case "stream_end": LAB.summary = ev.summary; LAB.spent = ev.spent; break;
    case "budget_exhausted": LAB.error = `Stopped: spend cap reached ($${ev.spent}).`; break;
    case "run_error": LAB.error = ev.error; break;
    case "done": LAB.status = ev.status; if (ev.error) LAB.error = ev.error; break;
    default: break;
  }
}

const STRUCTURAL_DELAY = { narrated: 260, fast: 45, instant: 0 };
function pump() {
  clearTimeout(LAB.timer);
  const delay = STRUCTURAL_DELAY[LAB.speed];
  let changed = false;
  const t0 = performance.now();
  while (LAB.queue.length) {
    const ev = LAB.queue[0];
    const structural = ev.type !== "step";
    if (structural && delay && performance.now() - LAB.last < delay) break;
    LAB.queue.shift();
    apply(ev);
    changed = true;
    if (structural && delay) { LAB.last = performance.now(); break; }
    if (performance.now() - t0 > 30) break;
  }
  if (changed && location.hash.startsWith("#/lab")) renderLabMain();
  if (LAB.queue.length) LAB.timer = setTimeout(pump, delay ? Math.max(16, delay / 4) : 16);
}

function connect(id) {
  LAB.source?.close();
  const src = new EventSource(`/api/runs/${id}/events`);
  LAB.source = src;
  src.onmessage = (m) => {
    const ev = JSON.parse(m.data);
    LAB.queue.push(ev);
    if (ev.type === "done") src.close();
    pump();
  };
  src.onerror = () => { if (LAB.status === "running") LAB.error = "Lost the event stream; the run may still be going."; };
}

async function startRun() {
  const c = LAB.cfg;
  const body = { ...c, k: Number(c.k), seed: Number(c.seed), adaptive_budget: Number(c.adaptive_budget),
    max_cost_usd: Number(c.max_cost_usd) };
  if (c.backend !== "openai") delete body.base_url;
  Object.assign(LAB, { tasks: {}, order: [], current: null, pinned: null, feed: [], results: [], summary: null,
    libraries: {}, error: "", queue: [], status: "running" });
  renderLabMain();
  try {
    const res = await api("/api/runs", body);
    LAB.run = res;
    connect(res.id);
  } catch (e) {
    LAB.status = "idle";
    LAB.error = e.message;
    renderLabMain();
  }
  renderConfig();
}

// ------------------------------------------------------------------ lab view
function viewLab(root) {
  root.innerHTML = `<div class="lab"><aside class="card config" id="config"></aside><div id="lab-main" class="stack"></div></div>`;
  renderConfig();
  renderLabMain();
  if (location.hash.includes("demo=1") && LAB.status === "idle" && !LAB.results.length) {
    history.replaceState(null, "", "#/lab");
    startRun();
  }
}

function seg(name, options, value) {
  return `<div class="seg" data-seg="${name}">${options.map(([v, l]) =>
    `<button type="button" class="${String(v) === String(value) ? "on" : ""}" data-v="${v}">${l}</button>`).join("")}</div>`;
}

function renderConfig() {
  const box = $("#config");
  if (!box) return;
  const c = LAB.cfg;
  const be = META.backends;
  const running = LAB.status === "running";
  const byDomain = META.domains.map((d) => `<div class="domain-head">${d}</div>` + META.tasks.filter((t) => t.domain === d).map((t) =>
    `<label class="task-pick"><input type="checkbox" value="${t.id}" ${c.tasks.includes(t.id) ? "checked" : ""}><span><b>${t.id}</b> ${esc(t.title)}</span></label>`).join("")).join("");
  const modelField = c.backend === "anthropic"
    ? `<div class="grid cols-2"><label class="field"><span>Executor (frozen) model</span><select data-k="executor_model">${be.anthropic.models.map((m) => `<option ${m === (c.executor_model || be.anthropic.default_executor) ? "selected" : ""}>${m}</option>`).join("")}</select></label>
       <label class="field"><span>Editor (improver) model</span><select data-k="editor_model">${be.anthropic.models.map((m) => `<option ${m === (c.editor_model || be.anthropic.default_editor) ? "selected" : ""}>${m}</option>`).join("")}</select></label></div>`
    : c.backend === "openai"
      ? `<label class="field"><span>Endpoint</span><select data-k="base_url">${Object.entries(be.openai.presets).map(([k, v]) => `<option value="${k}" ${k === c.base_url ? "selected" : ""}>${k} — ${esc(v)}</option>`).join("")}</select></label>
         <label class="field"><span>Executor model id</span><input type="text" data-k="executor_model" value="${esc(c.executor_model)}" placeholder="a model id this endpoint serves"></label>
         <label class="field"><span>Editor model id (blank = same)</span><input type="text" data-k="editor_model" value="${esc(c.editor_model)}"></label>`
      : "";
  const needsKey = c.backend !== "sim" && !(be[c.backend]?.server_key);
  box.innerHTML = `<div class="stack">
    <div class="spread"><h2 style="margin:0">Run Lab</h2><span class="badge-sim">${c.backend === "sim" ? "offline" : "live model"}</span></div>
    <label class="field"><span>Model backend</span><select data-k="backend">
      ${Object.entries(be).map(([k, v]) => `<option value="${k}" ${k === c.backend ? "selected" : ""}>${esc(v.label)}</option>`).join("")}
    </select></label>
    ${modelField}
    ${c.backend !== "sim" ? `<label class="field"><span>API key ${needsKey ? "" : "(optional — the Space has one)"}</span><input type="password" autocomplete="off" data-k="api_key" value="${esc(c.api_key)}" placeholder="used for this run only, never stored"></label>
      <p class="tiny muted" style="margin:0">Real runs: ≤ ${META.limits.max_real_tasks} tasks, k ≤ ${META.limits.max_real_k}. One task at k=1 is ~8 agent sessions. The cost threshold is checked between tasks using estimates; a task can exceed it. Dollar costs are unavailable for OpenAI-compatible endpoints. Set billing limits with your provider.</p>
      <label class="field"><span>Estimated cost threshold (USD)</span><input type="number" min="0.5" step="0.5" data-k="max_cost_usd" value="${c.max_cost_usd}"></label>` : ""}
    <div class="field"><span>Recursion mode</span>${seg("mode", [["parallel", "Parallel"], ["sequential", "Sequential"], ["adaptive", "Adaptive ✦"]], c.mode)}</div>
    <div class="field"><span>${c.mode === "adaptive" ? "Max depth per task" : c.mode === "parallel" ? "Width k" : "Depth k"}</span>${seg("k", [[1, "1"], [2, "2"], [3, "3"], [4, "4"], [5, "5"]], c.k)}</div>
    ${c.mode === "adaptive" ? `<label class="field"><span>Stream budget (Φ steps per task, average)</span><input type="number" min="1" max="3" step="0.5" data-k="adaptive_budget" value="${c.adaptive_budget}"></label>` : ""}
    <div class="field"><span>Tasks (run in domain order; each domain's library starts empty)</span><div class="task-picks" id="task-picks">${byDomain}</div></div>
    <div class="field"><span>Ablations</span>
      <label class="check"><input type="checkbox" data-k="library" ${c.library ? "checked" : ""}> Library (harnessing-level recursion)</label>
      <label class="check"><input type="checkbox" data-k="scoping" ${c.scoping ? "checked" : ""}> INV / DOM scoping</label>
      <label class="check"><input type="checkbox" data-k="composition" ${c.composition ? "checked" : ""}> Composition</label></div>
    <label class="field"><span>Seed</span><input type="number" data-k="seed" value="${c.seed}"></label>
    <div class="field"><span>Playback speed</span>${seg("speed", [["narrated", "Narrated"], ["fast", "Fast"], ["instant", "Instant"]], LAB.speed)}</div>
    <div class="row">
      <button class="btn primary" id="go" type="button" ${running ? "disabled" : ""}>${running ? "Running…" : "Start run"}</button>
      ${running ? '<button class="btn" id="stop" type="button">Stop after this task</button>' : ""}
    </div>
  </div>`;
  box.querySelectorAll("[data-k]").forEach((inp) => {
    inp.addEventListener("change", () => {
      const k = inp.dataset.k;
      LAB.cfg[k] = inp.type === "checkbox" ? inp.checked : inp.value;
      if (k === "backend") {
        LAB.cfg.executor_model = ""; LAB.cfg.editor_model = "";
        if (LAB.cfg.backend !== "sim") { LAB.cfg.tasks = LAB.cfg.tasks.slice(0, 1); LAB.cfg.k = 1; }
        renderConfig();
      }
    });
  });
  box.querySelectorAll("[data-seg]").forEach((g) => g.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    const name = g.dataset.seg;
    if (name === "speed") { LAB.speed = b.dataset.v; pump(); } else LAB.cfg[name] = name === "k" ? Number(b.dataset.v) : b.dataset.v;
    renderConfig();
  }));
  $("#task-picks").addEventListener("change", () => {
    LAB.cfg.tasks = [...box.querySelectorAll("#task-picks input:checked")].map((i) => i.value);
  });
  $("#go")?.addEventListener("click", startRun);
  $("#stop")?.addEventListener("click", () => LAB.run && api(`/api/runs/${LAB.run.id}/cancel`, {}));
}

function nodeCard(t, n) {
  const st = n.stage, ro = n.rollout;
  const oracle = t.result?.oracle?.[n.name];
  let sub = "", meta = "";
  if (n.isRollout) {
    if (n.status === "running") sub = "executing…";
    else {
      sub = ro.delivered ? "delivered" : "<b>delivered nothing</b>";
      meta = `<span>${ro.turns} turns</span><span>${ro.tool_errors} err</span>${ro.plugin_firings ? `<span>${ro.plugin_firings} plugin</span>` : ""}`;
    }
  } else if (st) {
    sub = st.fell_back ? "<b>fell back</b> (invalid twice)" : st.kept_base ? "KEEP_BASE" : changeChips(st.diff);
    meta = `${st.attempts > 1 ? `<span>retry ×${st.attempts - 1}</span>` : ""}${st.repaired?.length ? "<span>auto-trigger</span>" : ""}`;
  } else sub = n.status === "running" ? "improver session…" : "";
  const adopted = t.selection && n.isRollout && t.selection.chosen === n.name;
  return `<button type="button" class="node k-${n.kind} ${n.status === "running" ? "running" : ""} ${adopted ? "adopted" : ""} ${(ro && !ro.delivered) || st?.fell_back ? "fail" : ""}" data-node="${esc(n.label)}">
    <span class="bar"></span>
    <div class="lbl">${n.status === "running" ? '<span class="spinner"></span>' : ""}${esc(n.name || n.label)}${oracle ? `<span class="oracle" title="hidden grader, revealed after the task">${Math.round(oracle.score)}</span>` : ""}</div>
    <div class="sub">${sub}</div>${meta ? `<div class="meta">${meta}</div>` : ""}</button>`;
}

function boardHTML(t) {
  const cols = Object.fromEntries(COLS.map(([k]) => [k, []]));
  for (const n of Object.values(t.nodes)) cols[n.col]?.push(n);
  const sel = t.selection, exp = t.experience;
  cols.adopt = [];
  if (sel) cols.adopt.push(`<div class="node k-ref" style="cursor:default"><div class="lbl">select → ${esc(sel.chosen)}</div><div class="sub">${sel.trivial ? esc(sel.why) : "blind pick among " + sel.candidates.length + " finals"}</div></div>`);
  else if (t.selectPending) cols.adopt.push('<div class="node running"><div class="lbl"><span class="spinner"></span>selector</div></div>');
  if (exp) cols.adopt.push(`<button type="button" class="node k-compose" data-special="reflect"><div class="lbl">reflect</div><div class="sub">${exp.lesson ? "lesson: " + esc(exp.cue || "").slice(0, 80) : "NO_ENTRY — evidence insufficient"}</div></button>`);
  if (t.libraryAdded !== undefined) cols.adopt.push(`<div class="node k-dom" style="cursor:default"><div class="lbl">update</div><div class="sub">${t.libraryAdded ? "+1 entry in" : "nothing added to"} the ${esc(t.domain)} library</div></div>`);
  const active = { reference: "reference", propose: "propose", probe: "probe", compose: "compose", final: "final",
    select: "adopt", reflect: "adopt", update: "adopt" }[t.phase];
  return `<div class="board-wrap"><div class="board">${COLS.map(([k, name]) => {
    const items = cols[k];
    const html = items.map((x) => (typeof x === "string" ? x : nodeCard(t, x))).join("");
    return `<div><div class="col-head ${active === k && t.status === "running" ? "active" : ""}">${name}${items.length && typeof items[0] !== "string" ? `<span class="n">${items.length}</span>` : ""}</div><div class="col">${html}</div></div>`;
  }).join("")}</div></div>`;
}

function renderLabMain() {
  const main = $("#lab-main");
  if (!main) return;
  const shown = LAB.pinned || LAB.current;
  const t = shown ? LAB.tasks[shown] : null;
  const chips = LAB.order.map((id) => {
    const x = LAB.tasks[id];
    const d = x.result ? x.result.gain : null;
    return `<button type="button" class="t ${x.status} ${id === shown ? "selected" : ""}" data-task="${id}">
      <i class="dot ${x.status === "done" ? "good" : x.status === "error" ? "bad" : "ref"}"></i>${id}
      ${d !== null ? `<span class="delta ${d >= 0 ? "up" : "down"}">${signed(d)}</span>` : x.status === "running" ? '<span class="spinner"></span>' : ""}</button>`;
  }).join("");
  const res = LAB.results;
  const mean = (a) => (a.length ? a.reduce((x, y) => x + y, 0) / a.length : 0);
  const base = mean(res.map((r) => r.base_score)), fin = mean(res.map((r) => r.final_score));
  const sessions = res.reduce((s, r) => s + Object.keys(r.mode.rollouts).length + r.mode.stages.length, 0);
  const cost = res.reduce((s, r) => s + r.usage.executor.cost_usd + r.usage.improver.cost_usd, 0);
  const libN = Object.values(LAB.libraries).reduce((s, l) => s + l.entries.length, 0);

  if (!LAB.order.length && LAB.status !== "running") {
    main.innerHTML = `${LAB.error ? `<div class="callout" style="border-color:var(--critical)">${esc(LAB.error)}</div>` : ""}
      <div class="card"><h2>Watch the two-level loop run</h2>
      <p class="ink-2">Pick tasks and a recursion mode, then <b>Start run</b>. The board shows every reference execution,
      INV/DOM proposal, probe, composition, blind selection, reflection and library update as it happens. Click any card to
      open its diff, notes, harness files or full trajectory. Hidden-grader scores (<span class="oracle">87</span>) appear only
      after a task is finished — the method never sees them.</p>
      <p class="small muted">The simulator runs offline in seconds; "Narrated" playback paces the events so you can follow them.</p></div>`;
    return;
  }

  main.innerHTML = `
    ${LAB.error ? `<div class="callout" style="border-color:var(--critical)">${esc(LAB.error)}</div>` : ""}
    <div class="card">
      <div class="spread"><div><div class="kicker">Task stream</div><div class="small muted">${esc(LAB.executor || "")}${LAB.editor && LAB.editor !== LAB.executor ? " · editor " + esc(LAB.editor) : ""}</div></div>
        <span class="small muted">${LAB.status === "running" || LAB.queue.length ? '<span class="spinner"></span> running' : LAB.status}</span></div>
      <div class="stream" style="margin-top:10px">${chips || '<span class="muted small">starting…</span>'}</div>
    </div>
    ${t ? `<div class="card">
      <div class="spread"><div><div class="kicker">${esc(t.domain)} · ${esc(t.mode || "")} k=${t.k ?? "?"}</div><h2 style="margin:0">${esc(t.id)} — ${esc(t.title)}</h2></div>
        <div class="row small">${t.libraryEntries !== undefined ? `<span class="chip"><i class="dot dom"></i>library had ${t.libraryEntries} entr${t.libraryEntries === 1 ? "y" : "ies"}</span>` : ""}
        ${t.result ? `<span class="chip">base ${fmt(t.result.base_score)} → H² ${fmt(t.result.final_score)}</span>` : ""}
        ${t.adaptive ? `<span class="chip"><i class="dot compose"></i>difficulty ${t.adaptive.difficulty.difficulty} → granted k=${t.adaptive.granted}</span>` : ""}</div></div>
      <div class="legend-row" style="margin:8px 0 4px"><span><i class="dot ref"></i>reference / base</span><span><i class="dot inv"></i>INV — how the agent works</span><span><i class="dot dom"></i>DOM — practitioner knowledge</span><span><i class="dot compose"></i>Compose</span><span><span class="oracle">00</span> hidden-grader score</span></div>
      ${boardHTML(t)}
      ${t.selection && !t.selection.trivial ? `<p class="why" style="margin-top:10px"><b>Selector:</b> ${esc(t.selection.why)}</p>` : ""}
      ${t.adaptiveRounds?.length ? `<p class="small ink-2" style="margin-top:6px"><b>Adaptive:</b> ${t.adaptiveRounds.map((r) => `round ${r.round}: probe gain ${r.gain}${r.kept_base ? " (KEEP_BASE)" : ""}${r.stop ? " → stop" : ""}`).join(" · ")}</p>` : ""}
    </div>` : ""}
    <div class="grid cols-2">
      <div class="card"><div class="spread"><h3 style="margin:0">Live agent activity</h3><span class="tiny muted">${LAB.feed.length} recent steps</span></div>
        <div class="feed" id="feed" style="margin-top:8px">${LAB.feed.slice(-80).map((s) => `<div><span class="who">${esc(s.agent)}</span><span class="${s.kind === "error" ? "err" : s.kind === "think" ? "think" : ""}">${s.kind === "call" ? `${esc(s.name)}(${esc(s.text)})` : esc(s.text)}</span></div>`).join("")}</div></div>
      <div class="card"><h3>Stream so far</h3>
        <div class="tiles">
          <div class="tile"><div class="label">Base harness H0</div><div class="value">${res.length ? fmt(base) : "—"}</div><div class="delta-line muted">mean of references</div></div>
          <div class="tile"><div class="label">Harness²</div><div class="value">${res.length ? fmt(fin) : "—"}</div><div class="delta-line ${fin - base >= 0 ? "delta up" : "delta down"}">${res.length ? signed(fin - base) + " points" : ""}</div></div>
          <div class="tile"><div class="label">Library entries</div><div class="value">${libN}</div><div class="delta-line muted">${res.length} tasks done</div></div>
          <div class="tile"><div class="label">Agent sessions</div><div class="value">${sessions}</div><div class="delta-line muted">${cost > 0 ? "$" + cost.toFixed(3) + " spent" : "simulated tokens"}</div></div>
        </div></div>
    </div>
    ${res.length ? `<div class="card"><div class="spread"><h3 style="margin:0">Hidden-grader score per task</h3>
        <div class="legend-row"><span><i class="dot ref"></i>Base harness H0</span><span><i class="dot inv"></i>Harness² (adopted H*)</span></div></div>
      <div class="chart" id="score-chart" style="margin-top:8px"></div>
      <details style="margin-top:8px"><summary class="small">Table view</summary><div class="table-wrap"><table class="data"><thead><tr><th>Task</th><th class="num">Base</th><th class="num">H²</th><th class="num">Δ</th><th>Adopted</th><th>Lesson stored</th></tr></thead><tbody>
        ${res.map((r) => `<tr><td>${r.task_id}</td><td class="num">${fmt(r.base_score)}</td><td class="num">${fmt(r.final_score)}</td><td class="num">${signed(r.gain)}</td><td><code>${r.adopted}</code></td><td>${r.library_entry ? "yes" : "no"}</td></tr>`).join("")}
      </tbody></table></div></details></div>` : ""}
    ${t?.result ? `<div class="card"><div class="spread"><div><div class="kicker">Observatory</div><h3 style="margin:0">Lineage of ${esc(t.id)}</h3></div>
        <div class="legend-row"><span><svg width="22" height="8"><path d="M0,4 H22" stroke="var(--compose)" stroke-width="2"/></svg>edit kept by a composer</span><span><svg width="22" height="8"><path d="M0,4 H22" stroke="var(--dom)" stroke-width="2" stroke-dasharray="5 4"/></svg>component reused from the library</span></div></div>
      <div class="dag" id="dag" style="margin-top:8px;overflow-x:auto"></div>
      <p class="small muted" style="margin-top:6px">Library provenance: ${t.result.lineage.library_provenance.from_library} of ${t.result.lineage.library_provenance.modified_files} files modified by proposers came from earlier tasks' harnesses (paper App. E.2 reports 22–47%).</p>
      <div class="grid cols-2" style="margin-top:8px">
        <div><div class="small ink-2" style="margin-bottom:6px">Adopted harness genome (C1–C8, chars)</div>${genomeHTML(t.result.adopted_genome)}</div>
        <div><button class="btn sm" type="button" id="open-adopted">Inspect adopted harness diff</button></div>
      </div></div>` : ""}`;

  main.querySelectorAll("[data-task]").forEach((b) => b.addEventListener("click", () => {
    LAB.pinned = b.dataset.task === LAB.current && LAB.status === "running" ? null : b.dataset.task;
    renderLabMain();
  }));
  main.querySelectorAll("[data-node]").forEach((b) => b.addEventListener("click", () => openNode(t, t.nodes[b.dataset.node])));
  main.querySelector("[data-special=reflect]")?.addEventListener("click", () => openReflection(t));
  $("#open-adopted")?.addEventListener("click", () => openAdopted(t));
  const feed = $("#feed");
  if (feed) feed.scrollTop = feed.scrollHeight;
  if (res.length) scoreBars($("#score-chart"), res.map((r) => ({ id: r.task_id, base: r.base_score, final: r.final_score })),
    { onClick: (id) => { LAB.pinned = id; renderLabMain(); } });
  if (t?.result) dag($("#dag"), t.result.lineage, { onNode: (n) => { const x = t.nodes[n.id] || t.nodes[`${n.id}#roll`]; if (x) openNode(t, x); } });
}

function oracleHTML(t, name) {
  const o = t.result?.oracle?.[name];
  if (!o) return '<p class="muted">Revealed when the task finishes. The method never sees this.</p>';
  const task = META.tasks.find((x) => x.id === t.id);
  return `<p><span class="oracle">${fmt(o.score)}</span> hidden-grader score for <code>${esc(name)}</code> on ${esc(task?.title || t.id)}</p>
    <table class="data"><tbody>${o.passed.map((c) => `<tr><td>✓</td><td>${esc(c)}</td></tr>`).join("")}${o.failed.map((c) => `<tr><td style="color:var(--critical)">✗</td><td>${esc(c)}</td></tr>`).join("")}</tbody></table>`;
}

function openNode(t, n) {
  if (!n) return;
  if (n.isRollout) {
    const ro = n.rollout;
    const roll = t.nodes[`${n.name}#roll`];
    openDrawer(`${t.id} · rollout`, n.name, [
      { name: "Trajectory", render: () => trajectoryView(roll?.trajectory) },
      { name: "Deliverable", render: () => filesView(roll?.outputs || {}) },
      { name: "Oracle", render: () => oracleHTML(t, n.name) },
      { name: "Meta", render: () => `<pre class="code">${esc(JSON.stringify(ro, null, 2))}</pre>` },
    ]);
    return;
  }
  const st = n.stage;
  if (!st) return;
  openDrawer(`${t.id} · ${st.dim} stage`, st.label, [
    { name: "Notes", render: () => `${st.fell_back ? `<div class="callout" style="border-color:var(--critical)">Fell back to its starting harness: ${esc(st.error)}</div>` : ""}
        ${st.repaired?.length ? `<p class="small ink-2">Auto-trigger repaired: ${st.repaired.map(esc).join(", ")}</p>` : ""}
        <pre class="note-lines">${esc(st.notes || "(no notes)")}</pre>
        <p class="small muted">${esc(st.inventory)} · ${st.attempts} attempt(s)</p>` },
    { name: "Diff", render: () => Object.values(st.diff.patches).map(diffHTML).join("") || diffHTML("") },
    { name: "Harness files", render: () => { const w = document.createElement("div"); w.innerHTML = genomeHTML(st.genome) + "<div style='height:12px'></div>"; w.append(filesView(st.files)); return w; } },
    { name: "Improver session", render: () => trajectoryView(st.trajectory) },
  ]);
}

function openReflection(t) {
  const e = t.experience || {};
  openDrawer(`${t.id} · reflect`, e.lesson ? "Lesson stored" : "NO_ENTRY", [
    { name: "Lesson", render: () => (e.lesson ? `<table class="data"><tbody>${["cue", "lesson", "evidence", "confidence"].map((k) => `<tr><th>${k}</th><td>${esc(e[k] || "")}</td></tr>`).join("")}</tbody></table>`
      : '<p class="ink-2">The reflector found no concrete, reusable execution contrast, so nothing entered the library. That is part of the method: no evidence, no entry.</p>') },
    { name: "Edit verdicts", render: () => `<pre class="note-lines">${esc(e.edits || "(none)")}</pre>` },
  ]);
}

function openAdopted(t) {
  const r = t.result;
  openDrawer(`${t.id} · adopted harness`, `H* = ${r.adopted}`, [
    { name: "Diff vs H0", render: () => Object.values(r.adopted_diff.patches).map(diffHTML).join("") || diffHTML("") },
    { name: "Files", render: () => filesView(r.adopted_harness) },
  ]);
}

// ------------------------------------------------------------------ library view
function viewLibrary(root) {
  const libs = Object.values(LAB.libraries);
  if (!libs.length) {
    root.innerHTML = `<div class="card"><h2>The improvement library R</h2>
      <p class="ink-2">Each domain has its own library. It starts empty for every stream; after each task the adopted harness and an
      EXPERIENCE.md (why it was chosen, what each edit did, at most one transferable lesson) are stored — but only when execution
      contrasts support the lesson. Later tasks in the domain read it before proposing.</p>
      <div class="empty">No library yet — <a href="#/lab?demo=1">run the demo</a> and come back.</div></div>`;
    return;
  }
  root.innerHTML = `<div class="spread"><div><div class="kicker">Harnessing-level recursion</div><h1 style="font-size:2rem">Library</h1></div>
    <p class="small muted" style="max-width:52ch">R<sub>t+1</sub> = Update(R<sub>t</sub>, H*<sub>t</sub>, D<sub>t</sub>). Each entry carries the FULL adopted tree, not a summary.</p></div>
    ${libs.map((lib) => `<section class="section"><h2>${esc(lib.domain)} <span class="pill">${lib.entries.length} entr${lib.entries.length === 1 ? "y" : "ies"}</span></h2>
      <div class="grid cols-2">${lib.entries.map((e, i) => `<div class="card">
        <div class="spread"><h3 style="margin:0"><code>${esc(e.task_id)}</code> → H* = ${esc(e.adopted)}</h3><span class="chip">${esc(e.experience.confidence?.split(" ")[0] || "")}</span></div>
        <p class="small ink-2" style="margin-top:8px"><b>Cue.</b> ${esc(e.experience.cue)}</p>
        <p class="small"><b>Lesson.</b> ${esc(e.experience.lesson)}</p>
        <p class="small muted"><b>Evidence.</b> ${esc(e.experience.evidence)}</p>
        ${genomeHTML(e.genome)}
        <div class="row" style="margin-top:10px"><button class="btn sm" type="button" data-entry="${esc(lib.domain)}:${i}">Open harness</button><span class="tiny muted">${esc(e.inventory)}</span></div>
      </div>`).join("") || '<div class="empty">No entry yet: the reflector declined every lesson so far.</div>'}</div></section>`).join("")}`;
  root.querySelectorAll("[data-entry]").forEach((b) => b.addEventListener("click", () => {
    const [d, i] = b.dataset.entry.split(":");
    const e = LAB.libraries[d].entries[Number(i)];
    openDrawer(`${d} library`, e.task_id, [
      { name: "EXPERIENCE", render: () => `<p class="small ink-2"><b>Why chosen.</b> ${esc(e.why || "single final")}</p><pre class="note-lines">${esc(e.experience.edits || "")}</pre>` },
      { name: "Harness files", render: () => filesView(e.files) },
    ]);
  }));
}

// ------------------------------------------------------------------ budget lab view
let BUDGET = null;
function viewBudget(root) {
  root.innerHTML = `<div class="kicker">Side quest · paper App. G, "future directions"</div>
    <h1 style="font-size:2rem">Budget Lab: adaptive recursion</h1>
    <p class="lede ink-2 prose">The paper fixes the recursion budget k per task and lists <i>"adaptive recursion that allocates budget by
    difficulty or stops when probes add little"</i> as future work. This lab implements both — using only label-free signals — and asks
    whether it beats fixed-k recursion on the compute/score Pareto frontier.</p>
    <div class="grid cols-3 section" style="margin-top:18px">
      <div class="card"><div class="kicker">Signal 1 · before editing</div><h3>Difficulty d(q)</h3><p class="small ink-2">A noisy-OR of the reference runs'
        non-delivery rate, tool-error rate and <b>disagreement between their deliverables</b>. "The runs disagreed" is the paper's top-priority
        evidence — it doubles as a free uncertainty estimate.</p></div>
      <div class="card"><div class="kicker">Signal 2 · after each round</div><h3>Probe gain g<sub>t</sub></h3><p class="small ink-2">How far checkpoint
        c<sub>t</sub>'s rollout moved from the previous on-policy rollout (delivery flipped, deliverable changed, errors fixed). If Compose kept the
        checkpoint or the probe barely moved, stop.</p></div>
      <div class="card"><div class="kicker">Allocator</div><h3>Stream budget B</h3><p class="small ink-2"><code>k_t = clamp(round(share·(0.6+0.8·d)), 1, …)</code>
        with <code>share = B_left / tasks_left</code>: easy tasks lend depth, hard ones borrow it, early stops return steps to the pool.</p></div>
    </div>
    <div class="card section" id="budget-card"><div class="empty">Loading the precomputed sweep…</div></div>`;
  const load = BUDGET ? Promise.resolve(BUDGET) : api("/api/budget-lab");
  load.then((lab) => { BUDGET = lab; renderBudget(); }).catch((e) => {
    $("#budget-card").innerHTML = `<div class="empty">${esc(e.message)}</div>`;
  });
}

function renderBudget(progress) {
  const card = $("#budget-card");
  if (!card || !BUDGET) return;
  const lab = BUDGET;
  const pts = [...lab.points].sort((a, b) => a.sessions - b.sessions);
  card.innerHTML = `<div class="spread"><div><h2 style="margin:0">Mean score vs cost</h2><p class="small muted" style="margin:0">${lab.tasks.length} tasks × ${lab.seeds.length} seeds per policy · ${esc(lab.note)}</p></div>
      <div class="row"><button class="btn sm" type="button" id="rerun" ${progress ? "disabled" : ""}>Re-run live (3 seeds, ~2 min)</button></div></div>
    ${progress ? `<div class="progress" style="margin:10px 0"><i style="width:${progress}%"></i></div>` : ""}
    <div class="legend-row" id="fam-legend" style="margin:10px 0"></div>
    <div class="chart" id="pareto"></div>
    <div class="table-wrap" style="margin-top:12px"><table class="data"><thead><tr><th>Policy</th><th class="num">Score</th><th class="num">± SE</th><th class="num">Sessions / task</th><th class="num">Mean k</th><th>Frontier</th></tr></thead>
      <tbody>${pts.map((p) => `<tr><td>${esc(p.name)}</td><td class="num">${fmt(p.score)}</td><td class="num">${fmt(p.score_se)}</td><td class="num">${fmt(p.sessions)}</td><td class="num">${p.mean_k || "—"}</td><td>${lab.frontier.includes(p.name) ? "●" : ""}</td></tr>`).join("")}</tbody></table></div>
    <div class="callout" style="margin-top:14px"><b>Reading it.</b> In this toy world adaptive allocation lands <i>on</i> the frontier between fixed
      k=1 and k=2 — it buys roughly what its spend predicts, but it does not beat fixed recursion at equal cost. The reason is instructive:
      the simulator's knowledge gaps are <b>persistent</b>, so reference runs agree on their mistakes and disagreement under-estimates
      difficulty. A label-free difficulty signal can only see uncertainty, not shared bias — the same limitation the paper flags for
      behavioural contrasts. Ablations at k=1 are within one or two standard errors of each other here; read them with that in mind.</div>`;
  $("#fam-legend").append(...familyLegend());
  pareto($("#pareto"), lab.points, lab.frontier);
  $("#rerun").addEventListener("click", rerunBudget);
}

async function rerunBudget() {
  try {
    const { id } = await api("/api/budget-lab", { seeds: 3 });
    let jobs = 1, done = 0;
    renderBudget(1);
    const src = new EventSource(`/api/runs/${id}/events`);
    src.onmessage = (m) => {
      const ev = JSON.parse(m.data);
      if (ev.type === "lab_start") jobs = ev.jobs;
      if (ev.type === "lab_progress") { done += 1; renderBudget(Math.max(2, Math.round((100 * done) / jobs))); }
      if (ev.type === "lab_end") { BUDGET = ev.lab; renderBudget(); }
      if (ev.type === "run_error") { alert(ev.error); }
      if (ev.type === "done") { src.close(); renderBudget(); }
    };
  } catch (e) { alert(e.message); }
}

// ------------------------------------------------------------------ paper <-> code view
function viewCode(root) {
  const rows = [
    ["Agent = Model + Harness, H = (c¹…c⁸)", "h2lab/harness.py", "Harness, COMPONENTS, validate(), autotrigger()"],
    ["Executing A(q; M, H)", "h2lab/runtime.py · agent.py", "execute(), render_system(), Agent.run()"],
    ["C8 plugins enforce, not instruct", "h2lab/plugins.py", "PluginHost.before_tool / after_tool / before_finish"],
    ["Contrastive step Φ (Eq. 1): Propose → Probe → Compose", "h2lab/recursion.py · stages.py", "Recursion.stage(), probe(), run_stage()"],
    ["Parallel recursion, width k (Eq. 2)", "h2lab/recursion.py", "Recursion.parallel()"],
    ["Sequential recursion, depth k (Eq. 3)", "h2lab/recursion.py", "Recursion.sequential()"],
    ["Evidence as files, no grader in context", "h2lab/context.py", "build(), assert_clean()"],
    ["Blind agentic selector", "h2lab/selection.py", "select()"],
    ["Reflect: edit verdicts + one lesson", "h2lab/reflection.py", "reflect(), parse_experience()"],
    ["Library update R_{t+1} = Update(R_t, H*_t, D_t) (Eq. 4)", "h2lab/library.py", "Library.add(), rebuild_index()"],
    ["The two-level loop over a task stream", "h2lab/pipeline.py", "Pipeline.run_task(), run_stream()"],
    ["Stage prompts (App. H)", "h2lab/prompts.py", "propose(), aggregate(), compose_round(), select(), experience()"],
    ["Ablations: w/o library / scoping / composition", "h2lab/pipeline.py", "RunConfig(library, scoping, composition)"],
    ["Side quest: adaptive recursion", "h2lab/adaptive.py", "difficulty(), probe_gain(), BudgetAllocator"],
    ["Side quest: lineage & library provenance", "h2lab/provenance.py", "lineage()"],
    ["Side quest: Budget Lab sweep", "h2lab/budget_lab.py", "run_lab(), frontier()"],
  ];
  const promptNames = Object.keys(META.prompts);
  root.innerHTML = `<div class="kicker">For readers of the paper</div><h1 style="font-size:2rem">Paper ↔ Code</h1>
    <p class="ink-2 prose">Every concept in the paper maps to one small, readable module. Start at <code>pipeline.py</code> and follow the calls.</p>
    <div class="card"><div class="table-wrap"><table class="data map"><thead><tr><th>Paper</th><th>File</th><th>Where</th></tr></thead><tbody>
      ${rows.map(([a, f, w]) => `<tr><td>${esc(a)}</td><td><a href="${REPO}/blob/main/${f.split(" · ")[0]}" target="_blank" rel="noopener"><code>${esc(f)}</code></a></td><td class="small ink-2"><code>${esc(w)}</code></td></tr>`).join("")}
    </tbody></table></div></div>
    <section class="section grid cols-2">
      <div class="card"><h2>Stage prompts</h2><p class="small ink-2">Condensed from the official release (Apache-2.0). Rendered here for task <code>fin-01</code>.</p>
        <select id="prompt-pick">${promptNames.map((n) => `<option>${esc(n)}</option>`).join("")}</select>
        <pre class="code wrap" id="prompt-text" style="margin-top:10px;max-height:520px"></pre></div>
      <div class="card"><h2>The base harness H0</h2><p class="small ink-2">Every task in a stream starts here; only the library carries forward.</p>
        <div id="h0"></div>
        <h2 style="margin-top:18px">Run it yourself</h2>
        <pre class="code">pip install -r requirements.txt
python -m h2lab.cli --tasks fin-01,fin-02 --k 2        # simulator
ANTHROPIC_API_KEY=… python -m h2lab.cli --backend anthropic \\
    --tasks fin-01 --k 1                                # real model
uvicorn app.server:app --port 7860                    # this web app</pre></div>
    </section>`;
  const pick = $("#prompt-pick");
  const show = () => { $("#prompt-text").textContent = META.prompts[pick.value]; };
  pick.addEventListener("change", show);
  show();
  $("#h0").append(filesView(META.base_harness));
}

route();

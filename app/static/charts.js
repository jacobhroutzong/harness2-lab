// Inline-SVG charts. Colors come from CSS roles (var(--inv) ...), so both themes just work.
const NS = "http://www.w3.org/2000/svg";

export function el(tag, attrs = {}, ...children) {
  const node = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

const tip = () => document.getElementById("tooltip");
export function showTip(evt, html) {
  const t = tip();
  t.innerHTML = html;
  t.hidden = false;
  const pad = 14;
  const w = t.offsetWidth, h = t.offsetHeight;
  let x = evt.clientX + pad, y = evt.clientY + pad;
  if (x + w > window.innerWidth - 8) x = evt.clientX - w - pad;
  if (y + h > window.innerHeight - 8) y = evt.clientY - h - pad;
  t.style.left = `${Math.max(8, x)}px`;
  t.style.top = `${Math.max(8, y)}px`;
}
export function hideTip() { tip().hidden = true; }
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

// A bar with a 4px rounded data-end and a square base (marks spec).
function bar(x, y, w, h, r = 4) {
  if (h <= 0) return `M${x},${y} h${w}`;
  const rr = Math.min(r, h, w / 2);
  return `M${x},${y + h} V${y + rr} Q${x},${y} ${x + rr},${y} H${x + w - rr} Q${x + w},${y} ${x + w},${y + rr} V${y + h} Z`;
}

/** Per-task base vs Harness² columns. rows: [{id, base, final}] */
export function scoreBars(container, rows, { onClick } = {}) {
  container.innerHTML = "";
  if (!rows.length) return;
  const W = Math.max(320, container.clientWidth || 720), H = 240, m = { t: 18, r: 10, b: 34, l: 34 };
  const iw = W - m.l - m.r, ih = H - m.t - m.b;
  const y = (v) => m.t + ih - (v / 100) * ih;
  const band = iw / rows.length, bw = Math.min(24, band / 3);
  const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Score per task: base harness vs Harness²" });
  for (const t of [0, 25, 50, 75, 100]) {
    svg.append(el("line", { class: t ? "gridline" : "baseline", x1: m.l, x2: W - m.r, y1: y(t), y2: y(t) }));
    svg.append(el("text", { x: m.l - 6, y: y(t) + 4, "text-anchor": "end" }, t));
  }
  rows.forEach((r, i) => {
    const cx = m.l + band * i + band / 2;
    const x0 = cx - bw - 1, x1 = cx + 1;
    const tipHtml = `<b>${esc(r.id)}</b><br>Base harness H0: ${r.base.toFixed(1)}<br>Harness²: ${r.final.toFixed(1)}`
      + `<br>Δ ${(r.final - r.base >= 0 ? "+" : "") + (r.final - r.base).toFixed(1)}`;
    const g = el("g", {
      onmousemove: (e) => showTip(e, tipHtml), onmouseleave: hideTip,
      onclick: () => onClick && onClick(r.id), style: onClick ? "cursor:pointer" : null,
    });
    g.append(el("path", { d: bar(x0, y(r.base), bw, y(0) - y(r.base)), fill: "var(--neutral-mark)" }));
    g.append(el("path", { d: bar(x1, y(r.final), bw, y(0) - y(r.final)), fill: "var(--inv)" }));
    g.append(el("text", { class: "value-label", x: x1 + bw / 2, y: y(r.final) - 5, "text-anchor": "middle" }, Math.round(r.final)));
    g.append(el("rect", { class: "hit", x: cx - band / 2, y: m.t, width: band, height: ih }));
    g.append(el("text", { class: "label", x: cx, y: H - m.b + 16, "text-anchor": "middle" }, r.id));
    svg.append(g);
  });
  container.append(svg);
}

const FAMILY = {
  base: { color: "var(--neutral-mark)", shape: "square", label: "Base harness" },
  parallel: { color: "var(--inv)", shape: "circle", label: "Parallel (width k)" },
  sequential: { color: "var(--dom)", shape: "circle", label: "Sequential (depth k)" },
  adaptive: { color: "var(--compose)", shape: "diamond", label: "Adaptive (side quest)" },
  ablation: { color: "var(--neutral-mark)", shape: "hollow", label: "Ablations (k=1)" },
};

function marker(shape, x, y, color) {
  const ring = { stroke: "var(--surface)", "stroke-width": 2 };
  if (shape === "square") return el("rect", { x: x - 5, y: y - 5, width: 10, height: 10, rx: 2, fill: color, ...ring });
  if (shape === "diamond") return el("path", { d: `M${x},${y - 7} L${x + 7},${y} L${x},${y + 7} L${x - 7},${y} Z`, fill: color, ...ring });
  if (shape === "hollow") return el("circle", { cx: x, cy: y, r: 5, fill: "var(--surface)", stroke: color, "stroke-width": 2 });
  return el("circle", { cx: x, cy: y, r: 5.5, fill: color, ...ring });
}

export function familyLegend() {
  return Object.values(FAMILY).map((f) => {
    const s = el("svg", { width: 14, height: 14, viewBox: "0 0 14 14", "aria-hidden": "true" }, marker(f.shape, 7, 7, f.color));
    const span = document.createElement("span");
    span.append(s, f.label);
    return span;
  });
}

/** Cost vs score scatter with the Pareto frontier. points: [{name, family, score, score_se, sessions}] */
export function pareto(container, points, frontierNames = []) {
  container.innerHTML = "";
  const W = Math.max(340, container.clientWidth || 760), H = Math.round(Math.min(420, Math.max(300, W * 0.48)));
  const m = { t: 16, r: W < 560 ? 24 : 170, b: 44, l: 46 };
  const iw = W - m.l - m.r, ih = H - m.t - m.b;
  const xmax = Math.ceil((Math.max(...points.map((p) => p.sessions)) * 1.08) / 5) * 5;
  const lo = Math.floor((Math.min(...points.map((p) => p.score - (p.score_se || 0))) - 4) / 10) * 10;
  const x = (v) => m.l + (v / xmax) * iw, y = (v) => m.t + ih - ((v - lo) / (100 - lo)) * ih;
  const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Mean score vs agent sessions per task" });
  for (let t = lo; t <= 100; t += 10) {
    svg.append(el("line", { class: t === lo ? "baseline" : "gridline", x1: m.l, x2: m.l + iw, y1: y(t), y2: y(t) }));
    svg.append(el("text", { x: m.l - 6, y: y(t) + 4, "text-anchor": "end" }, t));
  }
  for (let t = 0; t <= xmax; t += 5) {
    svg.append(el("text", { x: x(t), y: m.t + ih + 16, "text-anchor": "middle" }, t));
  }
  svg.append(el("text", { x: m.l + iw / 2, y: H - 6, "text-anchor": "middle", class: "label" }, "agent sessions per task (rollouts + improver stages + selection + reflection)"));
  svg.append(el("text", { x: 12, y: m.t + ih / 2, transform: `rotate(-90 12 ${m.t + ih / 2})`, "text-anchor": "middle", class: "label" }, "mean hidden-grader score"));
  const front = points.filter((p) => frontierNames.includes(p.name)).sort((a, b) => a.sessions - b.sessions);
  if (front.length > 1) {
    let d = `M${x(front[0].sessions)},${y(front[0].score)}`;
    for (const p of front.slice(1)) d += ` H${x(p.sessions)} V${y(p.score)}`;
    svg.append(el("path", { d, fill: "none", stroke: "var(--ink-2)", "stroke-opacity": 0.35, "stroke-width": 2 }));
  }
  const labelled = new Set([...frontierNames, ...points.filter((p) => p.family === "adaptive").map((p) => p.name)]);
  const used = [];
  for (const p of points) {
    const f = FAMILY[p.family] || FAMILY.ablation;
    const px = x(p.sessions), py = y(p.score);
    const html = `<b>${esc(p.name)}</b><br>score ${p.score.toFixed(1)} ± ${(p.score_se || 0).toFixed(1)} (SE)`
      + `<br>${p.sessions.toFixed(1)} sessions / task` + (p.mean_k ? `<br>mean depth/width ${p.mean_k}` : "")
      + (frontierNames.includes(p.name) ? "<br><i>on the Pareto frontier</i>" : "");
    const g = el("g", { onmousemove: (e) => showTip(e, html), onmouseleave: hideTip });
    if (p.score_se) g.append(el("line", { x1: px, x2: px, y1: y(Math.min(100, p.score + p.score_se)), y2: y(p.score - p.score_se), stroke: f.color, "stroke-width": 1.5, "stroke-opacity": 0.6 }));
    g.append(marker(f.shape, px, py, f.color));
    g.append(el("circle", { class: "hit", cx: px, cy: py, r: 12 }));
    if (labelled.has(p.name) && W >= 560) {
      let ly = py + 4;
      while (used.some((u) => Math.abs(u.y - ly) < 13 && Math.abs(u.x - px) < 120)) ly += 13;
      used.push({ x: px, y: ly });
      g.append(el("text", { class: "label", x: px + 10, y: ly }, p.name));
    }
    svg.append(g);
  }
  container.append(svg);
}

/** Lineage DAG of one task: H0 -> references / candidates -> composers -> H* -> library. */
export function dag(container, full, { onNode } = {}) {
  container.innerHTML = "";
  if (!full || !full.nodes) return;
  // lineage of harnesses: reference rollouts are executions, not harnesses, so they stay on the board
  const lineage = { nodes: full.nodes.filter((n) => n.kind !== "reference"),
    edges: full.edges.filter((e) => e.kind !== "rollout") };
  const colOf = (n) => ({
    library: 0, base: 1, reference: 2, inv: 3, dom: 3, compose: 4, selected: 5, "library-new": 6,
  }[n.kind] ?? 3);
  // sequential checkpoints feed later rounds: push a round's proposals right of its parent
  const round = (n) => Number((n.id.match(/^(?:inv|dom|c)(\d+)$/) || [0, 1])[1]);
  const isSeq = lineage.nodes.some((n) => /^c\d+$/.test(n.id));
  const col = (n) => {
    if (!isSeq) return colOf(n);
    if (/^(inv|dom)\d+$/.test(n.id)) return 1 + 2 * round(n);
    if (/^c\d+$/.test(n.id)) return 2 + 2 * round(n);
    if (n.kind === "reference") return 1.5;
    const maxR = Math.max(...lineage.nodes.map((m) => (/^c\d+$/.test(m.id) ? round(m) : 1)));
    if (n.kind === "selected") return 3 + 2 * maxR;
    if (n.kind === "library-new") return 4 + 2 * maxR;
    return colOf(n) === 0 ? 0 : 1;
  };
  const cols = new Map();
  for (const n of lineage.nodes) {
    const c = col(n);
    if (!cols.has(c)) cols.set(c, []);
    cols.get(c).push(n);
  }
  const order = [...cols.keys()].sort((a, b) => a - b);
  const NW = 112, NH = 40, GX = 34, GY = 14;
  const pos = new Map();
  const maxRows = Math.max(...[...cols.values()].map((v) => v.length));
  const H = maxRows * (NH + GY) + 20;
  order.forEach((c, ci) => {
    const nodes = cols.get(c);
    const top = (H - nodes.length * (NH + GY) + GY) / 2;
    nodes.forEach((n, i) => pos.set(n.id, { x: 10 + ci * (NW + GX), y: top + i * (NH + GY) }));
  });
  const W = 20 + order.length * (NW + GX) - GX;
  const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Lineage of harnesses for this task" });
  for (const e of lineage.edges) {
    const a = pos.get(e.from), b = pos.get(e.to);
    if (!a || !b) continue;
    const x1 = a.x + NW, y1 = a.y + NH / 2, x2 = b.x, y2 = b.y + NH / 2;
    const mx = (x1 + x2) / 2;
    const path = el("path", { class: `edge ${e.kind}`, d: `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}` });
    if (e.files) {
      path.addEventListener("mousemove", (ev) => showTip(ev, `<b>${esc(e.kind)}</b><br>${e.files.map(esc).join("<br>")}`));
      path.addEventListener("mouseleave", hideTip);
    }
    svg.append(path);
  }
  const fill = { inv: "var(--inv)", dom: "var(--dom)", compose: "var(--compose)", reference: "var(--neutral-mark)",
    base: "var(--neutral-mark)", library: "var(--dom)", "library-new": "var(--compose)", selected: "var(--ink)" };
  for (const n of lineage.nodes) {
    const p = pos.get(n.id);
    const g = el("g", {
      class: `nd ${n.adopted ? "adopted" : ""}`, transform: `translate(${p.x},${p.y})`,
      style: onNode ? "cursor:pointer" : null, onclick: () => onNode && onNode(n),
      onmousemove: (ev) => showTip(ev, `<b>${esc(n.label)}</b>${n.changes ? "<br>" + esc(n.changes) : ""}`
        + (n.score !== null && n.score !== undefined ? `<br>oracle score ${n.score}` : "")
        + (n.fell_back ? "<br>fell back to its start harness" : "")),
      onmouseleave: hideTip,
    });
    g.append(el("rect", { width: NW, height: NH, rx: 8 }));
    g.append(el("rect", { width: 4, height: NH - 12, y: 6, rx: 2, fill: fill[n.kind] || "var(--axis)", stroke: "none" }));
    g.append(el("text", { x: 12, y: 17 }, n.label.length > 14 ? n.label.slice(0, 13) + "…" : n.label));
    const sub = n.kind === "library" ? "library entry" : n.kind === "library-new" ? "stored in library"
      : n.score !== null && n.score !== undefined ? `score ${Math.round(n.score)}` : n.kind;
    g.append(el("text", { x: 12, y: 31, class: "s" }, sub));
    svg.append(g);
  }
  container.append(svg);
}

/** The 8-component strip; fill steps a single blue ramp by size (sequential = magnitude). */
export function genomeHTML(genome) {
  if (!genome) return "";
  const level = (c) => (c <= 0 ? 0 : c < 200 ? 1 : c < 600 ? 2 : c < 1500 ? 3 : c < 3500 ? 4 : 5);
  return `<div class="genome" role="list" aria-label="Harness components by size">${genome.map((g) =>
    `<div class="g l${level(g.chars)}" role="listitem" title="${esc(g.name)}: ${g.chars.toLocaleString()} chars${g.files.length ? " — " + esc(g.files.join(", ")) : ""}"><b>${g.key}</b>${g.chars ? (g.chars >= 1000 ? (g.chars / 1000).toFixed(1) + "k" : g.chars) : "—"}</div>`).join("")}</div>`;
}

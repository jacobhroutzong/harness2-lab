# Design notes

These notes cover the decisions behind Harness² Lab: where it follows the paper, where it departs, and why.

## 1. Fidelity to the paper

| Paper / official release | This project | Why |
|---|---|---|
| Executors are OpenCode or Codex CLI sessions in Docker | A ~200-line tool-use loop (`agent.py`) with file, Python and harness tools | A reader can follow every line, and it runs on a free CPU Space |
| Eight components; C8 is a TypeScript plugin | Same eight; C8 is a **declarative JSON hook spec** (`plugins.py`) | A public demo must not execute model-written middleware. The spec covers the paper's four recurring plugin shapes (trim, hint, cap, verify) plus `block` and `require_reread` |
| Improvers are agentic sessions over a `context/` workspace | Same: improvers are agents (the same loop) that edit `context/harness/` in place; the files on disk are the proposal | This is the core idea; kept as-is |
| Validation: trigger lines, no template braces, scripts must exist, one retry, then fallback | Same rules (`harness.validate`), plus the release's `autotrigger` repair | Kept as-is |
| Parallel recursion: 2k candidates, k composers, blind selector over the k finals | Same (`Recursion.parallel`, `selection.select`) | Kept as-is |
| Sequential recursion: k rounds, each composing a checkpoint from the whole history | Same (`Recursion.sequential`) | Kept as-is |
| 4k rollouts and 3k improvement stages per mode, counting the shared references | Same, and asserted in `tests/test_method.py` | Kept as-is |
| Reflect: per-edit verdicts plus at most one lesson; otherwise NO_ENTRY | Same (`reflection.py`, prompt in `prompts.experience`) | Kept as-is |
| Library: per-domain, full adopted tree plus EXPERIENCE.md plus INDEX.md | Same (`library.py`) | Kept as-is |
| Stage prompts (App. H) | Condensed adaptations (`prompts.py`) with the same structure and hard rules | Credited in NOTICE |
| WorkBuddy-Bench, JobBench, LAB | Nine built-in tasks in three domains, each with a hidden grader | The benchmarks need Docker, judges and licences; the built-ins run anywhere in seconds |

## 2. The no-ground-truth contract

The improver never sees a score. Three mechanisms enforce this:

1. **Construction.** A `Task`'s `truth` and `checks` are never passed to `context.build`, `selection.select` or `reflection.reflect`. `Task.public()` (what the web client gets) omits both.
2. **A guard.** `context.assert_clean` runs on every evidence workspace. It rejects files whose names look like grader artifacts, a `meta.json` that carries a score, and any file that quotes a hidden check's description. `context.strip_verdicts` rejects harnesses that reference a grader.
3. **Timing.** `Pipeline.run_task` grades rollouts only after selection, reflection and the library update have all happened. In the UI, those scores appear as dashed "oracle" badges once a task is done.

## 3. The simulator

The simulator (`h2lab/sim/`) exists so that anyone can watch the whole method run without an API key, and so the test suite can cover every code path. It is a **test double, not a model**, and its numbers describe a toy world.

**What it is.** One generator policy per role. Each yields tool calls into the real agent loop and receives real tool results:

- **Executor.** It lists and reads inputs, loads skills named in its system prompt, runs harness scripts, writes the deliverable, and sometimes reopens it. Its outcome on each latent requirement depends on what its context holds: a rule in a loaded skill (strong), a rule in always-on text (weaker, and diluted as always-on text grows), a plugin hint or verification signal, or a working script.
- **Proposer.** It reads trajectories and deliverables, spots *symptoms* (what a run says when it gets a requirement wrong, a malformed file, a run that ran out of turns), and writes a fix in the cheapest effective form, sometimes a worse one. It also reuses library components. It makes realistic mistakes: vague rules, misdiagnoses, carefulness paragraphs, and dangling script references that the validator catches.
- **Composer.** It keeps an edit when that edit's probe shows the targeted symptom gone, treats edits from non-delivering probes as unsafe, merges skill rules, and cuts speculative always-on prose.
- **Selector.** It runs label-free checks on anonymous deliverables (parse, conformance, internal consistency) and spot-checks figures against the source, with noise.
- **Reflector.** It writes per-edit verdicts (helped, hurt, no observed effect, confounded) and at most one lesson, or NO_ENTRY.

**The world model** (`sim/catalog.py`). Four INV requirements (verify, shape, arith, budget) and four DOM requirements per domain. Model knowledge is **persistent**: for each seed the frozen model either knows a requirement (and slips about 4% of the time) or has a gap (and fails about 88% of runs). DOM gaps are shared across a domain and INV habits across everything. That persistence is what makes base runs agree on their mistakes and lets the library pay off on later tasks.

**Calibration.** The knobs were tuned so the base harness scores about 60, one improvement step adds roughly 10–15 points, and width or depth keeps helping with diminishing returns. That is the qualitative shape the paper reports, without claiming its magnitudes. Every knob is a named constant in `catalog.py`.

**Known artefacts.** At k=1, the "w/o composition" ablation (select between the two probed candidates) scores higher than composing and re-running, and it is cheaper. The cause is that selecting between two already-executed deliverables is a best-of-2 draw, while composition produces a single fresh rollout. The paper reports the opposite ordering on real benchmarks. The Budget Lab shows this result as it comes out rather than tuning it away.

## 4. The side quest: adaptive recursion and the observatory

- `adaptive.py` implements the two halves of the paper's future-work item, using only label-free signals: a difficulty estimate from the reference runs, and an early stop when Compose keeps the checkpoint or the probe barely moves. A stream-level `BudgetAllocator` moves depth from easy tasks to hard ones.
- `budget_lab.py` sweeps twelve policies over many seeds, using common random numbers, and reports the compute/score Pareto frontier. Result: adaptive allocation sits **on** the frontier and does not beat it, because label-free difficulty sees uncertainty but not shared bias.
- `provenance.py` records, for every task, which candidates' edits survived composition and which components were copied from library entries (textual attribution on the lines each file gained). The web app draws this as a lineage DAG.

## 5. Safety in a public Space

- Executed code runs in a subprocess with a scrubbed environment, CPU, memory and file-size limits, a wall-clock timeout, and the workspace as its working directory. This is defence in depth, not a security boundary.
- API keys sent from the browser are used to build that run's client only. They are never stored, logged or returned. Server-side keys are ignored unless `H2_ALLOW_SERVER_KEYS=1`.
- Runs with real models are capped by task count, k and a USD spend limit (`H2_MAX_REAL_TASKS`, `H2_MAX_REAL_K`, per-run `max_cost_usd`). The server caps concurrent runs with `H2_MAX_ACTIVE_RUNS`.

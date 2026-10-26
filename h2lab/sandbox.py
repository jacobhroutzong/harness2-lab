"""The workspace an agent acts in, and the guarded runner for code it executes.

Layout of one rollout's workspace:

    input/                 the task's source material (the agent must not modify it)
    output/                where deliverables go -- this is what gets judged
    scripts/, skills/...   the harness's executable components, mounted so they can run

Executed code runs in a subprocess with a scrubbed environment (no API keys), resource
limits, a wall-clock timeout and the workspace as cwd. This is defence in depth for a
public demo, not a security boundary: run untrusted tasks inside a container.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .harness import Harness

MAX_OUTPUT_CHARS = 8000
DEFAULT_TIMEOUT_S = 20
SAFE_ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1"}


class WorkspaceError(Exception):
    """A tool call the workspace refuses (bad path, missing file, ...)."""


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False

    def render(self) -> str:
        parts = [f"exit code: {self.exit_code}" + (" (TIMED OUT)" if self.timed_out else "")]
        if self.stdout.strip():
            parts.append("stdout:\n" + _clip(self.stdout))
        if self.stderr.strip():
            parts.append("stderr:\n" + _clip(self.stderr))
        return "\n".join(parts)


def _clip(text: str, n: int = MAX_OUTPUT_CHARS) -> str:
    return text if len(text) <= n else text[:n] + f"\n... [{len(text) - n:,} more chars]"


# The child applies its own resource limits, then execs the real script. (`preexec_fn` would
# do the same but is unsafe in a multi-threaded server.)
_LAUNCHER = """
import os, sys
try:
    import resource
    t = int(sys.argv[1])
    resource.setrlimit(resource.RLIMIT_CPU, (t + 1, t + 2))
    resource.setrlimit(resource.RLIMIT_AS, (1 << 30, 1 << 30))
    resource.setrlimit(resource.RLIMIT_FSIZE, (20 << 20, 20 << 20))
except Exception:
    pass
os.execv(sys.executable, [sys.executable, "-I", "-B", *sys.argv[2:]])
"""


class Workspace:
    """A directory the agent's file tools are confined to."""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self._n_exec = 0

    @classmethod
    def create(cls, files: dict[str, str], harness: Harness | None = None,
               parent: Path | None = None) -> "Workspace":
        root = Path(tempfile.mkdtemp(prefix="h2ws-", dir=parent))
        ws = cls(root)
        for rel, text in files.items():
            ws._put(rel, text)
        (root / "output").mkdir(exist_ok=True)
        if harness is not None:
            for rel, text in harness.scripts().items():
                ws._put(rel, text)
        return ws

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    # ---- paths ---------------------------------------------------------------------------
    def resolve(self, rel: str) -> Path:
        rel = (rel or ".").replace("\\", "/").lstrip("/")
        p = (self.root / rel).resolve()
        if p != self.root and self.root not in p.parents:
            raise WorkspaceError(f"path `{rel}` escapes the workspace")
        return p

    def rel(self, p: Path) -> str:
        return p.relative_to(self.root).as_posix()

    def _put(self, rel: str, text: str) -> None:
        p = self.resolve(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    # ---- file tools ----------------------------------------------------------------------
    def list(self, rel: str = ".") -> str:
        p = self.resolve(rel)
        if not p.exists():
            raise WorkspaceError(f"`{rel}` does not exist")
        if p.is_file():
            return f"{self.rel(p)}  ({p.stat().st_size:,} bytes)"
        rows = []
        for q in sorted(p.rglob("*")):
            if q.is_file() and ".h2" not in q.parts:
                rows.append(f"{self.rel(q)}  ({q.stat().st_size:,} bytes)")
        return "\n".join(rows) or "(empty)"

    def read(self, rel: str, offset: int = 0, limit: int = 4000) -> str:
        p = self.resolve(rel)
        if not p.is_file():
            raise WorkspaceError(f"`{rel}` is not a file")
        text = p.read_text(encoding="utf-8", errors="replace")
        offset = max(0, int(offset))
        window = text[offset: offset + max(1, int(limit))]
        rest = len(text) - offset - len(window)
        if rest > 0:
            window += f"\n... [{rest:,} more chars; call read_file with offset={offset + len(window)}]"
        return window

    def read_all(self, rel: str) -> str | None:
        try:
            p = self.resolve(rel)
        except WorkspaceError:
            return None
        return p.read_text(encoding="utf-8", errors="replace") if p.is_file() else None

    def write(self, rel: str, content: str) -> str:
        rel = rel.replace("\\", "/").lstrip("/")
        if rel.startswith("input/") or rel == "input":
            raise WorkspaceError("`input/` is read-only")
        self._put(rel, content)
        return f"wrote {len(content):,} chars to {rel}"

    def delete(self, rel: str) -> str:
        p = self.resolve(rel)
        if not p.is_file():
            raise WorkspaceError(f"`{rel}` is not a file")
        p.unlink()
        return f"deleted {rel}"

    def outputs(self) -> dict[str, str]:
        out_dir = self.root / "output"
        if not out_dir.is_dir():
            return {}
        return {self.rel(p): p.read_text(encoding="utf-8", errors="replace")
                for p in sorted(out_dir.rglob("*")) if p.is_file()}

    # ---- code execution ------------------------------------------------------------------
    def run_python(self, code: str, timeout: int = DEFAULT_TIMEOUT_S) -> ExecResult:
        self._n_exec += 1
        tmp = self.root / ".h2" / f"snippet_{self._n_exec}.py"
        tmp.parent.mkdir(exist_ok=True)
        tmp.write_text(code, encoding="utf-8")
        return self._exec([str(tmp)], timeout)

    def run_script(self, rel: str, args: list[str] | None = None,
                   timeout: int = DEFAULT_TIMEOUT_S) -> ExecResult:
        p = self.resolve(rel)
        rp = self.rel(p) if p.exists() else rel
        if not (rp.startswith("scripts/") or (rp.startswith("skills/") and "/scripts/" in rp)):
            raise WorkspaceError("only harness scripts (scripts/ or skills/<name>/scripts/) can be run")
        if not p.is_file():
            raise WorkspaceError(f"script `{rel}` does not exist")
        return self._exec([str(p), *[str(a) for a in (args or [])]], timeout)

    def _exec(self, argv: list[str], timeout: int) -> ExecResult:
        env = dict(SAFE_ENV, HOME=str(self.root))
        posix = os.name == "posix"
        cmd = ([sys.executable, "-I", "-B", "-c", _LAUNCHER, str(timeout), *argv] if posix
               else [sys.executable, "-I", "-B", *argv])
        proc = subprocess.Popen(
            cmd, cwd=self.root, env=env, text=True, start_new_session=posix,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
        )
        try:
            out, err = proc.communicate(timeout=timeout)
            return ExecResult(proc.returncode, out, err)
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:  # pragma: no cover
                proc.kill()
            out, err = proc.communicate()
            return ExecResult(-9, out or "", err or "", timed_out=True)

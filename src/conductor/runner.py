from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import tarfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, Field

from . import workspace
from .engine import ARTIFACT_NAME_RE
from .models import TaskSpec

log = logging.getLogger("conductor.runner")

SUMMARY_CHARS = 4000


class RunnerAgent(BaseModel):
    command: list[str]
    env: dict[str, str] = Field(default_factory=dict)
    timeout: int = 3600
    slots: int = 1


class RunnerConfig(BaseModel):
    server: str
    zone: str
    token_env: str
    data_dir: str = "./data/runner"
    poll_interval: float = 3.0
    heartbeat_interval: float = 15.0
    inherit_env: list[str] = Field(default_factory=lambda: ["PATH", "HOME", "LANG", "LC_ALL", "TERM"])
    workspace_exclude: list[str] = Field(default_factory=list)
    allowed_sources: list[str] = Field(default_factory=list)
    agents: dict[str, RunnerAgent]


class Runner:
    def __init__(self, cfg: RunnerConfig, client: httpx.Client | None = None):
        self.cfg = cfg
        token = os.environ.get(cfg.token_env, "")
        if not token:
            raise RuntimeError(f"environment variable {cfg.token_env} is not set")
        self.http = client or httpx.Client(base_url=cfg.server, timeout=60)
        self.http.headers["Authorization"] = f"Bearer {token}"
        self.data_dir = Path(cfg.data_dir).resolve()
        self.local_artifacts = self.data_dir / "artifacts"
        self._active: dict[str, int] = {a: 0 for a in cfg.agents}
        self._lock = threading.Lock()
        self._stop = threading.Event()

    # ------------------------------------------------------------ loop

    def claim(self) -> dict[str, Any] | None:
        with self._lock:
            offers = [
                {"agent_id": aid, "slots": a.slots - self._active[aid]}
                for aid, a in self.cfg.agents.items()
                if a.slots - self._active[aid] > 0
            ]
        if not offers:
            return None
        resp = self.http.post("/api/runner/claim", json={"agents": offers})
        resp.raise_for_status()
        return resp.json()["run"]

    def run_once(self) -> bool:
        run = self.claim()
        if run:
            self.execute(run)
        return run is not None

    def run_forever(self) -> None:
        workers = sum(a.slots for a in self.cfg.agents.values())
        signal.signal(signal.SIGTERM, lambda *_: self._stop.set())
        with ThreadPoolExecutor(max_workers=workers) as pool:
            while not self._stop.is_set():
                try:
                    run = self.claim()
                except httpx.HTTPError as exc:
                    log.warning("claim failed: %s", exc)
                    run = None
                if run is None:
                    self._stop.wait(self.cfg.poll_interval)
                    continue
                with self._lock:
                    self._active[run["agent_id"]] += 1
                pool.submit(self._execute_tracked, run)

    def stop(self) -> None:
        self._stop.set()

    def _execute_tracked(self, run: dict[str, Any]) -> None:
        try:
            self.execute(run)
        finally:
            with self._lock:
                self._active[run["agent_id"]] -= 1

    # ------------------------------------------------------------ execution

    def execute(self, run: dict[str, Any]) -> None:
        run_id, sealed = run["run_id"], run["sealed"]
        task = TaskSpec.model_validate(run["task"])
        agent = self.cfg.agents[run["agent_id"]]
        root = self.data_dir / "runs" / run_id
        ws, inputs_dir, out = root / "workspace", root / "inputs", root / "output"
        for d in (inputs_dir, out):
            d.mkdir(parents=True, exist_ok=True)
        log.info("run %s: task %s/%s on %s (level %s, sealed=%s)", run_id, run["workflow_id"], task.id, run["agent_id"], run["level"], sealed)

        beat = _Heartbeat(self.http, run_id, sealed, self.cfg.heartbeat_interval)
        beat.start()
        status, exit_code, error = "failed", None, None
        try:
            workspace.prepare(task.workspace, ws, self.cfg.workspace_exclude, self.cfg.allowed_sources, self.data_dir / "git-cache")
            self._fetch_inputs(run, inputs_dir)
            prompt = build_prompt(task, ws, inputs_dir, out)
            (root / "prompt.md").write_text(prompt, encoding="utf-8")
            exit_code = self._run_agent(agent, prompt, root, ws, inputs_dir, out, run, beat)
            if exit_code == 0:
                error = self._acceptance(task, ws, root, beat)
                status = "succeeded" if error is None else "failed"
            else:
                error = f"agent exited with {exit_code}"
            artifacts = self._collect(task, ws, out, root)
            if sealed:
                self._store_locally(run, task, artifacts)
            else:
                for path in artifacts:
                    self.http.put(f"/api/runner/runs/{run_id}/artifacts/{path.name}", content=path.read_bytes()).raise_for_status()
        except Exception as exc:
            log.exception("run %s failed", run_id)
            status, error = "failed", f"{type(exc).__name__}: {exc}"
        finally:
            beat.stop()

        body: dict[str, Any] = {"status": status, "exit_code": exit_code}
        if not sealed:
            body["summary"] = _tail(root / "agent.log", SUMMARY_CHARS)
            body["error"] = error
        elif error:
            (root / "error.txt").write_text(error, encoding="utf-8")
        self.http.post(f"/api/runner/runs/{run_id}/complete", json=body).raise_for_status()
        log.info("run %s: %s", run_id, status)

    def _fetch_inputs(self, run: dict[str, Any], dest: Path) -> None:
        resp = self.http.get(f"/api/runner/runs/{run['run_id']}/inputs")
        resp.raise_for_status()
        for src in resp.json():
            target = dest / src["task_id"]
            target.mkdir(parents=True, exist_ok=True)
            if src["sealed"]:
                local = self.local_artifacts / run["workflow_id"] / src["task_id"]
                if not local.is_dir():
                    raise RuntimeError(f"sealed input {src['task_id']} not found in this zone's local artifact store")
                shutil.copytree(local, target, dirs_exist_ok=True)
                continue
            for name in src["artifacts"]:
                r = self.http.get(f"/api/runner/runs/{run['run_id']}/inputs/{src['task_id']}/{name}")
                r.raise_for_status()
                (target / name).write_bytes(r.content)

    def _agent_env(self, agent: RunnerAgent, extra: dict[str, str]) -> dict[str, str]:
        env = {k: os.environ[k] for k in self.cfg.inherit_env if k in os.environ}
        env.update(agent.env)
        env.update(extra)
        return env

    def _run_agent(
        self,
        agent: RunnerAgent,
        prompt: str,
        root: Path,
        ws: Path,
        inputs_dir: Path,
        out: Path,
        run: dict[str, Any],
        beat: "_Heartbeat",
    ) -> int:
        values = {
            "{prompt}": prompt,
            "{prompt_file}": str(root / "prompt.md"),
            "{workspace}": str(ws),
            "{inputs_dir}": str(inputs_dir),
            "{output_dir}": str(out),
        }
        argv = []
        for token in agent.command:
            for key, val in values.items():
                token = token.replace(key, val)
            argv.append(token)
        env = self._agent_env(
            agent,
            {
                "CONDUCTOR_RUN_ID": run["run_id"],
                "CONDUCTOR_TASK_ID": run["task"]["id"],
                "CONDUCTOR_LEVEL": run["level"],
                "CONDUCTOR_WORKSPACE": str(ws),
                "CONDUCTOR_INPUTS_DIR": str(inputs_dir),
                "CONDUCTOR_OUTPUT_DIR": str(out),
                "CONDUCTOR_PROMPT_FILE": str(root / "prompt.md"),
            },
        )
        return _run_logged(argv, ws, env, agent.timeout, root / "agent.log", beat)

    def _acceptance(self, task: TaskSpec, ws: Path, root: Path, beat: "_Heartbeat") -> str | None:
        env = {k: os.environ[k] for k in self.cfg.inherit_env if k in os.environ}
        for cmd in task.acceptance:
            beat.add("acceptance", f"$ {cmd}")
            rc = _run_logged(["/bin/sh", "-c", cmd], ws, env, 1800, root / "acceptance.log", beat)
            if rc != 0:
                return f"acceptance check failed ({rc}): {cmd}"
        return None

    def _collect(self, task: TaskSpec, ws: Path, out: Path, root: Path) -> list[Path]:
        bundle = root / "artifacts"
        bundle.mkdir(exist_ok=True)
        if task.workspace.type == "git":
            patch = workspace.diff(ws)
            if patch:
                (bundle / "changes.diff").write_bytes(patch)
        for entry in sorted(out.iterdir()):
            name = entry.name if entry.is_file() else f"{entry.name}.tar.gz"
            if not ARTIFACT_NAME_RE.match(name):
                log.warning("skipping artifact with unsupported name %r", entry.name)
                continue
            if entry.is_file():
                shutil.copy2(entry, bundle / name)
            elif entry.is_dir():
                with tarfile.open(bundle / name, "w:gz") as tar:
                    tar.add(entry, arcname=entry.name)
        return sorted(p for p in bundle.iterdir() if p.is_file())

    def _store_locally(self, run: dict[str, Any], task: TaskSpec, artifacts: list[Path]) -> None:
        dest = self.local_artifacts / run["workflow_id"] / task.id
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True)
        for path in artifacts:
            shutil.copy2(path, dest / path.name)


def build_prompt(task: TaskSpec, ws: Path, inputs_dir: Path, out: Path) -> str:
    lines = [f"# Task: {task.title}", ""]
    if task.description:
        lines += [task.description.strip(), ""]
    lines += ["## Environment", f"- Working directory: {ws} (type: {task.workspace.type})"]
    if task.workspace.type == "git":
        lines.append("- Edit files in the working directory directly; the diff is collected automatically. Do not commit.")
    files = sorted(p.relative_to(inputs_dir).as_posix() for p in inputs_dir.rglob("*") if p.is_file())
    if files:
        lines.append(f"- Outputs of upstream tasks are in {inputs_dir}:")
        lines += [f"  - {f}" for f in files]
    lines.append(f"- Write deliverables (reports, documents, data files) to {out}")
    if task.acceptance:
        lines += ["", "## Acceptance checks (run after you finish)"]
        lines += [f"- `{c}`" for c in task.acceptance]
    return "\n".join(lines) + "\n"


def _run_logged(argv: list[str], cwd: Path, env: dict[str, str], timeout: int, log_path: Path, beat: "_Heartbeat") -> int:
    with open(log_path, "a", encoding="utf-8") as logf:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            start_new_session=True,
        )
        timer = threading.Timer(timeout, lambda: _kill(proc))
        timer.start()
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                logf.write(line)
                beat.add("log", line.rstrip("\n"))
            return proc.wait()
        finally:
            timer.cancel()


def _kill(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _tail(path: Path, chars: int) -> str:
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    return text[-chars:]


class _Heartbeat:
    """Extends the lease periodically; forwards log lines only for non-sealed zones."""

    def __init__(self, http: httpx.Client, run_id: str, sealed: bool, interval: float):
        self.http, self.run_id, self.sealed, self.interval = http, run_id, sealed, interval
        self._buf: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def add(self, kind: str, text: str) -> None:
        if self.sealed:
            return
        with self._lock:
            self._buf.append({"kind": kind, "text": text, "ts": time.time()})

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join()
        self._flush()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            self._flush()

    def _flush(self) -> None:
        with self._lock:
            events, self._buf = self._buf, []
        try:
            self.http.post(f"/api/runner/runs/{self.run_id}/heartbeat", json={"events": events}).raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("heartbeat for %s failed: %s", self.run_id, exc)

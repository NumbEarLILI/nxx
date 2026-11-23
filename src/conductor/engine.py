from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from .levels import allows
from .models import ServerConfig, TaskSpec, WorkflowSpec
from .policy import can_execute, classify_workflow
from .store import TERMINAL, Store, new_id

ARTIFACT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
MAX_EVENT_TEXT = 8000


class PolicyError(Exception):
    """The request violates an isolation rule (maps to HTTP 403)."""


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


class Engine:
    def __init__(self, cfg: ServerConfig):
        self.cfg = cfg
        self.data_dir = Path(cfg.data_dir)
        self.artifact_dir = self.data_dir / "artifacts"
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.data_dir / "conductor.db")

    # ------------------------------------------------------------ workflows

    def submit(self, spec: WorkflowSpec, actor: str, source: str = "api") -> dict[str, Any]:
        levels = classify_workflow(spec.tasks, self.cfg.classification)
        wf_id = new_id("wf")
        now = time.time()
        with self.store.tx() as db:
            db.execute(
                "INSERT INTO workflows (id, name, status, source, created_at, updated_at) VALUES (?, ?, 'running', ?, ?, ?)",
                (wf_id, spec.name, source, now, now),
            )
            for t in spec.tasks:
                c = levels[t.id]
                db.execute(
                    "INSERT INTO tasks (workflow_id, id, spec, level, level_reasons, status, max_attempts, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
                    (
                        wf_id,
                        t.id,
                        t.model_dump_json(),
                        c.level,
                        json.dumps(c.reasons, ensure_ascii=False),
                        t.max_attempts or self.cfg.max_attempts,
                        now,
                    ),
                )
            self.store.audit(
                db,
                actor,
                "workflow.submit",
                workflow_id=wf_id,
                source=source,
                levels={tid: c.level for tid, c in levels.items()},
            )
        return self.workflow(wf_id)

    def workflow(self, wf_id: str) -> dict[str, Any]:
        wf = self.store.one("SELECT * FROM workflows WHERE id = ?", (wf_id,))
        if not wf:
            raise NotFound(wf_id)
        tasks = self.store.query(
            "SELECT id, level, level_reasons, status, attempts, agent_id, zone, sealed, exit_code, summary, error, updated_at"
            " FROM tasks WHERE workflow_id = ? ORDER BY rowid",
            (wf_id,),
        )
        for t in tasks:
            t["level_reasons"] = json.loads(t["level_reasons"])
            t["sealed"] = bool(t["sealed"])
            t["artifacts"] = [
                a["name"]
                for a in self.store.query(
                    "SELECT name FROM artifacts WHERE workflow_id = ? AND task_id = ? ORDER BY name", (wf_id, t["id"])
                )
            ]
        wf["tasks"] = tasks
        return wf

    def workflows(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.store.query("SELECT * FROM workflows ORDER BY created_at DESC LIMIT ?", (limit,))

    def cancel(self, wf_id: str, actor: str) -> dict[str, Any]:
        with self.store.tx() as db:
            if not db.execute("SELECT 1 FROM workflows WHERE id = ?", (wf_id,)).fetchone():
                raise NotFound(wf_id)
            db.execute(
                "UPDATE tasks SET status = 'cancelled', run_id = NULL, lease_expires = NULL, updated_at = ?"
                " WHERE workflow_id = ? AND status IN ('pending', 'running')",
                (time.time(), wf_id),
            )
            self._refresh_workflow(db, wf_id)
            self.store.audit(db, actor, "workflow.cancel", workflow_id=wf_id)
        return self.workflow(wf_id)

    def events(self, wf_id: str, task_id: str) -> list[dict[str, Any]]:
        return self.store.query(
            "SELECT e.ts, e.kind, e.text, e.run_id FROM events e JOIN runs r ON r.id = e.run_id"
            " WHERE r.workflow_id = ? AND r.task_id = ? ORDER BY e.seq",
            (wf_id, task_id),
        )

    def artifact_path(self, wf_id: str, task_id: str, name: str) -> Path:
        row = self.store.one(
            "SELECT 1 FROM artifacts WHERE workflow_id = ? AND task_id = ? AND name = ?", (wf_id, task_id, name)
        )
        if not row:
            raise NotFound(name)
        return self.artifact_dir / wf_id / task_id / name

    def audit_log(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.store.query("SELECT * FROM audit ORDER BY seq DESC LIMIT ?", (limit,))
        for r in rows:
            r["detail"] = json.loads(r["detail"])
        return rows

    # ------------------------------------------------------------ runner protocol

    def claim(self, zone_name: str, offers: list[dict[str, Any]]) -> dict[str, Any] | None:
        zone = self.cfg.zones[zone_name]
        now = time.time()
        with self.store.tx() as db:
            self._requeue_expired(db, now)

            slots: dict[str, int] = {}
            for offer in offers:
                agent = self.cfg.agent(offer["agent_id"])
                if agent is None or agent.zone != zone_name:
                    raise PolicyError(f"agent {offer['agent_id']} is not registered in zone {zone_name}")
                busy = db.execute(
                    "SELECT COUNT(*) FROM tasks WHERE agent_id = ? AND status = 'running'", (agent.id,)
                ).fetchone()[0]
                free = min(int(offer.get("slots", 1)), agent.max_concurrency - busy)
                if free > 0:
                    slots[agent.id] = free
            if not slots:
                return None

            pending = db.execute(
                "SELECT t.* FROM tasks t JOIN workflows w ON w.id = t.workflow_id"
                " WHERE t.status = 'pending' AND w.status = 'running' ORDER BY w.created_at, t.rowid"
            ).fetchall()
            for row in pending:
                spec = TaskSpec.model_validate_json(row["spec"])
                deps = self._task_rows(db, row["workflow_id"], spec.depends_on)
                if any(d["status"] != "succeeded" for d in deps):
                    continue
                pinned = self._pinned_zone(deps, spec)
                for agent_id in slots:
                    agent = self.cfg.agent(agent_id)
                    if not can_execute(spec, row["level"], agent, zone_name, zone, self.cfg.min_level(zone_name), pinned):
                        continue
                    return self._assign(db, row, spec, agent_id, zone_name, now)
        return None

    def heartbeat(self, zone_name: str, run_id: str, events: list[dict[str, Any]]) -> None:
        zone = self.cfg.zones[zone_name]
        if zone.sealed and events:
            raise PolicyError("sealed zones may not send event content")
        now = time.time()
        with self.store.tx() as db:
            task = self._active_task(db, zone_name, run_id)
            db.execute(
                "UPDATE tasks SET lease_expires = ? WHERE workflow_id = ? AND id = ?",
                (now + self.cfg.lease_seconds, task["workflow_id"], task["id"]),
            )
            for ev in events:
                db.execute(
                    "INSERT INTO events (run_id, ts, kind, text) VALUES (?, ?, ?, ?)",
                    (run_id, ev.get("ts", now), str(ev.get("kind", "log"))[:32], str(ev.get("text", ""))[:MAX_EVENT_TEXT]),
                )

    def put_artifact(self, zone_name: str, run_id: str, name: str, data: bytes) -> dict[str, Any]:
        if self.cfg.zones[zone_name].sealed:
            raise PolicyError("sealed zones may not upload artifacts")
        if not ARTIFACT_NAME_RE.match(name):
            raise PolicyError(f"invalid artifact name {name!r}")
        with self.store.tx() as db:
            task = self._active_task(db, zone_name, run_id)
            target = self.artifact_dir / task["workflow_id"] / task["id"] / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            meta = {"name": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            db.execute(
                "INSERT OR REPLACE INTO artifacts (workflow_id, task_id, name, label, size, sha256) VALUES (?, ?, ?, ?, ?, ?)",
                (task["workflow_id"], task["id"], name, task["level"], meta["size"], meta["sha256"]),
            )
        return meta

    def complete(
        self,
        zone_name: str,
        run_id: str,
        status: str,
        exit_code: int | None,
        summary: str | None,
        error: str | None,
    ) -> dict[str, Any]:
        if status not in ("succeeded", "failed"):
            raise Conflict(f"invalid status {status}")
        sealed = self.cfg.zones[zone_name].sealed
        if sealed and (summary or error):
            raise PolicyError("sealed zones may not report summary or error text")
        now = time.time()
        with self.store.tx() as db:
            task = self._active_task(db, zone_name, run_id)
            wf_id, tid = task["workflow_id"], task["id"]
            final = status
            if status == "failed" and task["attempts"] < task["max_attempts"]:
                final = "pending"
            db.execute(
                "UPDATE tasks SET status = ?, exit_code = ?, summary = ?, error = ?, run_id = NULL,"
                " lease_expires = NULL, updated_at = ?, agent_id = CASE WHEN ? = 'pending' THEN NULL ELSE agent_id END"
                " WHERE workflow_id = ? AND id = ?",
                (final, exit_code, summary, error, now, final, wf_id, tid),
            )
            db.execute("UPDATE runs SET status = ?, finished_at = ? WHERE id = ?", (status, now, run_id))
            if final == "failed":
                self._skip_dependents(db, wf_id, tid)
            self._refresh_workflow(db, wf_id)
            self.store.audit(
                db, f"zone:{zone_name}", "run.complete", run_id=run_id, workflow_id=wf_id, task_id=tid, status=status, retry=final == "pending"
            )
        return {"task_status": final}

    def run_inputs(self, zone_name: str, run_id: str) -> list[dict[str, Any]]:
        with self.store.tx() as db:
            task = self._active_task(db, zone_name, run_id)
            spec = TaskSpec.model_validate_json(task["spec"])
            out = []
            for src in self._task_rows(db, task["workflow_id"], spec.input_tasks):
                names = [
                    r["name"]
                    for r in db.execute(
                        "SELECT name FROM artifacts WHERE workflow_id = ? AND task_id = ? ORDER BY name",
                        (task["workflow_id"], src["id"]),
                    ).fetchall()
                ]
                out.append({"task_id": src["id"], "sealed": bool(src["sealed"]), "zone": src["zone"], "artifacts": names})
            return out

    def read_input(self, zone_name: str, run_id: str, src_task: str, name: str) -> Path:
        zone = self.cfg.zones[zone_name]
        denied = None
        with self.store.tx() as db:
            task = self._active_task(db, zone_name, run_id)
            spec = TaskSpec.model_validate_json(task["spec"])
            art = db.execute(
                "SELECT label FROM artifacts WHERE workflow_id = ? AND task_id = ? AND name = ?",
                (task["workflow_id"], src_task, name),
            ).fetchone()
            if src_task not in spec.input_tasks:
                denied = f"{src_task} is not an input of this run"
            elif not art:
                raise NotFound(name)
            elif not allows(zone.clearance, art["label"]):
                denied = f"zone {zone_name} clearance {zone.clearance} < artifact label {art['label']}"
            self.store.audit(
                db,
                f"zone:{zone_name}",
                "artifact.denied" if denied else "artifact.read",
                run_id=run_id,
                task_id=src_task,
                name=name,
                reason=denied,
            )
        if denied:
            raise PolicyError(denied)
        return self.artifact_dir / task["workflow_id"] / src_task / name

    # ------------------------------------------------------------ internals

    def _assign(self, db: sqlite3.Connection, row: sqlite3.Row, spec: TaskSpec, agent_id: str, zone_name: str, now: float) -> dict[str, Any]:
        run_id = new_id("run")
        sealed = self.cfg.zones[zone_name].sealed
        db.execute(
            "UPDATE tasks SET status = 'running', attempts = attempts + 1, agent_id = ?, zone = ?, sealed = ?,"
            " run_id = ?, lease_expires = ?, updated_at = ? WHERE workflow_id = ? AND id = ?",
            (agent_id, zone_name, int(sealed), run_id, now + self.cfg.lease_seconds, now, row["workflow_id"], row["id"]),
        )
        db.execute(
            "INSERT INTO runs (id, workflow_id, task_id, agent_id, zone, status, started_at) VALUES (?, ?, ?, ?, ?, 'running', ?)",
            (run_id, row["workflow_id"], row["id"], agent_id, zone_name, now),
        )
        self.store.audit(
            db,
            "scheduler",
            "task.assign",
            workflow_id=row["workflow_id"],
            task_id=row["id"],
            level=row["level"],
            agent_id=agent_id,
            zone=zone_name,
            run_id=run_id,
        )
        return {
            "run_id": run_id,
            "workflow_id": row["workflow_id"],
            "agent_id": agent_id,
            "level": row["level"],
            "sealed": sealed,
            "lease_seconds": self.cfg.lease_seconds,
            "task": spec.model_dump(),
        }

    @staticmethod
    def _pinned_zone(deps: list[sqlite3.Row], spec: TaskSpec) -> str | None:
        """Outputs of sealed tasks never leave their zone, so consumers must run there too."""
        zones = {d["zone"] for d in deps if d["id"] in spec.input_tasks and d["sealed"]}
        if len(zones) > 1:
            return "<unsatisfiable>"
        return zones.pop() if zones else None

    @staticmethod
    def _task_rows(db: sqlite3.Connection, wf_id: str, ids: list[str]) -> list[sqlite3.Row]:
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        return db.execute(f"SELECT * FROM tasks WHERE workflow_id = ? AND id IN ({marks})", (wf_id, *ids)).fetchall()

    def _active_task(self, db: sqlite3.Connection, zone_name: str, run_id: str) -> sqlite3.Row:
        task = db.execute("SELECT * FROM tasks WHERE run_id = ? AND status = 'running'", (run_id,)).fetchone()
        if not task:
            raise Conflict(f"run {run_id} is not active")
        if task["zone"] != zone_name:
            raise PolicyError(f"run {run_id} does not belong to zone {zone_name}")
        return task

    def _requeue_expired(self, db: sqlite3.Connection, now: float) -> None:
        expired = db.execute(
            "SELECT workflow_id, id, run_id, attempts, max_attempts FROM tasks WHERE status = 'running' AND lease_expires < ?",
            (now,),
        ).fetchall()
        for t in expired:
            final = "pending" if t["attempts"] < t["max_attempts"] else "failed"
            db.execute(
                "UPDATE tasks SET status = ?, run_id = NULL, lease_expires = NULL, agent_id = NULL, error = ?, updated_at = ?"
                " WHERE workflow_id = ? AND id = ?",
                (final, None if final == "pending" else "lease expired", now, t["workflow_id"], t["id"]),
            )
            db.execute("UPDATE runs SET status = 'expired', finished_at = ? WHERE id = ?", (now, t["run_id"]))
            if final == "failed":
                self._skip_dependents(db, t["workflow_id"], t["id"])
            self._refresh_workflow(db, t["workflow_id"])
            self.store.audit(db, "scheduler", "run.expired", run_id=t["run_id"], task_id=t["id"], requeued=final == "pending")

    def _skip_dependents(self, db: sqlite3.Connection, wf_id: str, failed_id: str) -> None:
        rows = db.execute("SELECT id, spec, status FROM tasks WHERE workflow_id = ?", (wf_id,)).fetchall()
        deps = {r["id"]: TaskSpec.model_validate_json(r["spec"]).depends_on for r in rows}
        status = {r["id"]: r["status"] for r in rows}
        blocked = {failed_id}
        changed = True
        while changed:
            changed = False
            for tid, ds in deps.items():
                if tid not in blocked and blocked & set(ds):
                    blocked.add(tid)
                    changed = True
        for tid in blocked - {failed_id}:
            if status[tid] == "pending":
                db.execute(
                    "UPDATE tasks SET status = 'skipped', error = ?, updated_at = ? WHERE workflow_id = ? AND id = ?",
                    (f"upstream {failed_id} failed", time.time(), wf_id, tid),
                )

    def _refresh_workflow(self, db: sqlite3.Connection, wf_id: str) -> None:
        statuses = [r[0] for r in db.execute("SELECT status FROM tasks WHERE workflow_id = ?", (wf_id,)).fetchall()]
        if not all(s in TERMINAL for s in statuses):
            return
        if "cancelled" in statuses:
            final = "cancelled"
        elif all(s == "succeeded" for s in statuses):
            final = "succeeded"
        else:
            final = "failed"
        db.execute("UPDATE workflows SET status = ?, updated_at = ? WHERE id = ?", (final, time.time(), wf_id))

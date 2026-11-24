from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from .levels import LEVELS, rank

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def _check_level(v: str) -> str:
    rank(v)
    return v


def _check_id(v: str) -> str:
    if not ID_RE.match(v):
        raise ValueError(f"invalid id {v!r}")
    return v


# ---------------------------------------------------------------- server config


class ZoneConfig(BaseModel):
    clearance: str
    sealed: bool = False
    token_env: str
    min_level: str | None = None
    model_endpoints: list[str] = Field(default_factory=list)

    @field_validator("clearance", "min_level")
    @classmethod
    def _v(cls, v: str | None) -> str | None:
        return v if v is None else _check_level(v)


class AgentConfig(BaseModel):
    id: str
    zone: str
    capabilities: list[str] = Field(default_factory=list)
    max_concurrency: int = 1

    _v = field_validator("id")(_check_id)


class PathRule(BaseModel):
    glob: str
    level: str

    _v = field_validator("level")(_check_level)


class ContentRule(BaseModel):
    name: str
    pattern: str
    level: str

    _v = field_validator("level")(_check_level)


class ClassificationConfig(BaseModel):
    paths: list[PathRule] = Field(default_factory=list)
    content: list[ContentRule] = Field(default_factory=list)
    builtin_detectors: bool = True


class TriggerConfig(BaseModel):
    name: str
    secret_env: str
    when: dict[str, Any] = Field(default_factory=dict)
    workflow: dict[str, Any]

    _v = field_validator("name")(_check_id)


class ServerConfig(BaseModel):
    data_dir: str = "./data/server"
    admin_token_env: str = "CONDUCTOR_ADMIN_TOKEN"
    lease_seconds: int = 300
    max_attempts: int = 2
    zones: dict[str, ZoneConfig]
    agents: list[AgentConfig]
    classification: ClassificationConfig = Field(default_factory=ClassificationConfig)
    triggers: list[TriggerConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_refs(self) -> "ServerConfig":
        seen = set()
        for a in self.agents:
            if a.zone not in self.zones:
                raise ValueError(f"agent {a.id} references unknown zone {a.zone}")
            if a.id in seen:
                raise ValueError(f"duplicate agent id {a.id}")
            seen.add(a.id)
        return self

    def min_level(self, zone_name: str) -> str:
        """Lowest task level a zone accepts.

        Sealed zones default to work no open zone may run: anything else would
        have its outputs locked away and use up scarce trusted capacity.
        """
        zone = self.zones[zone_name]
        if zone.min_level is not None:
            return zone.min_level
        if not zone.sealed:
            return LEVELS[0]
        open_max = max((rank(z.clearance) for z in self.zones.values() if not z.sealed), default=-1)
        return LEVELS[min(open_max + 1, len(LEVELS) - 1)]

    def agent(self, agent_id: str) -> AgentConfig | None:
        return next((a for a in self.agents if a.id == agent_id), None)

    def secret(self, env: str) -> str:
        value = os.environ.get(env, "")
        if not value:
            raise RuntimeError(f"environment variable {env} is not set")
        return value


# ---------------------------------------------------------------- workflow spec


class WorkspaceSpec(BaseModel):
    type: Literal["none", "git", "dir"] = "none"
    source: str | None = None
    ref: str = "HEAD"

    @model_validator(mode="after")
    def _need_source(self) -> "WorkspaceSpec":
        if self.type != "none" and not self.source:
            raise ValueError(f"workspace type {self.type} requires source")
        return self


class TaskSpec(BaseModel):
    id: str
    title: str
    description: str = ""
    level: str = LEVELS[0]
    requires: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    inputs: list[str] | None = None
    paths: list[str] = Field(default_factory=list)
    workspace: WorkspaceSpec = Field(default_factory=WorkspaceSpec)
    acceptance: list[str] = Field(default_factory=list)
    max_attempts: int | None = None

    _v_id = field_validator("id")(_check_id)
    _v_level = field_validator("level")(_check_level)

    @property
    def input_tasks(self) -> list[str]:
        return self.depends_on if self.inputs is None else self.inputs


class WorkflowSpec(BaseModel):
    name: str
    tasks: list[TaskSpec]

    @model_validator(mode="after")
    def _check_graph(self) -> "WorkflowSpec":
        ids = [t.id for t in self.tasks]
        if not ids:
            raise ValueError("workflow has no tasks")
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate task ids")
        known = set(ids)
        for t in self.tasks:
            for d in t.depends_on:
                if d not in known:
                    raise ValueError(f"task {t.id} depends on unknown task {d}")
            for i in t.input_tasks:
                if i not in t.depends_on:
                    raise ValueError(f"task {t.id} input {i} must also be in depends_on")
        topo_order(self.tasks)
        return self


def topo_order(tasks: list[TaskSpec]) -> list[TaskSpec]:
    by_id = {t.id: t for t in tasks}
    state: dict[str, int] = {}
    out: list[TaskSpec] = []

    def visit(tid: str) -> None:
        if state.get(tid) == 2:
            return
        if state.get(tid) == 1:
            raise ValueError(f"dependency cycle through task {tid}")
        state[tid] = 1
        for d in by_id[tid].depends_on:
            visit(d)
        state[tid] = 2
        out.append(by_id[tid])

    for t in tasks:
        visit(t.id)
    return out


def load_yaml(path: str | Path) -> Any:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)

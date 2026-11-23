from __future__ import annotations

import re
from dataclasses import dataclass, field
from fnmatch import fnmatchcase

from .levels import allows, rank
from .models import AgentConfig, ClassificationConfig, ContentRule, TaskSpec, ZoneConfig, topo_order

BUILTIN_DETECTORS = [
    ContentRule(name="private-key", pattern=r"-----BEGIN [A-Z ]*PRIVATE KEY-----", level="L3"),
    ContentRule(name="aws-access-key", pattern=r"\bAKIA[0-9A-Z]{16}\b", level="L3"),
    ContentRule(
        name="secret-assignment",
        pattern=r"(?i)\b(password|passwd|secret|api[_-]?key|access[_-]?token)\b\s*[:=]\s*['\"]?[^\s'\"]{6,}",
        level="L3",
    ),
    ContentRule(name="cn-resident-id", pattern=r"(?<!\d)\d{17}[\dXx](?!\d)", level="L2"),
    ContentRule(name="cn-mobile", pattern=r"(?<!\d)1[3-9]\d{9}(?!\d)", level="L2"),
]


@dataclass
class Classified:
    level: str
    reasons: list[str] = field(default_factory=list)


def classify_task(task: TaskSpec, cfg: ClassificationConfig) -> Classified:
    """Raise (never lower) the declared level based on touched paths and content."""
    level = task.level
    reasons = [f"declared {task.level}"]

    for rule in cfg.paths:
        hit = next((p for p in task.paths if fnmatchcase(p, rule.glob)), None)
        if hit and rank(rule.level) > rank(level):
            level = rule.level
            reasons.append(f"path {hit} matches {rule.glob} -> {rule.level}")

    text = f"{task.title}\n{task.description}"
    rules = (BUILTIN_DETECTORS if cfg.builtin_detectors else []) + cfg.content
    for rule in rules:
        if rank(rule.level) > rank(level) and re.search(rule.pattern, text):
            level = rule.level
            reasons.append(f"content matches {rule.name} -> {rule.level}")

    return Classified(level, reasons)


def classify_workflow(tasks: list[TaskSpec], cfg: ClassificationConfig) -> dict[str, Classified]:
    """Classify every task, then propagate levels along data inputs (taint)."""
    result: dict[str, Classified] = {}
    for task in topo_order(tasks):
        c = classify_task(task, cfg)
        for src in task.input_tasks:
            src_level = result[src].level
            if rank(src_level) > rank(c.level):
                c.level = src_level
                c.reasons.append(f"reads output of {src} ({src_level})")
        result[task.id] = c
    return result


def can_execute(
    task: TaskSpec,
    level: str,
    agent: AgentConfig,
    zone_name: str,
    zone: ZoneConfig,
    zone_min_level: str,
    pinned_zone: str | None,
) -> bool:
    if agent.zone != zone_name:
        return False
    if pinned_zone is not None and pinned_zone != zone_name:
        return False
    if pinned_zone is None and rank(level) < rank(zone_min_level):
        return False
    if not allows(zone.clearance, level):
        return False
    return set(task.requires) <= set(agent.capabilities)

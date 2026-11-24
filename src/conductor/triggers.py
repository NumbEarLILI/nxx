from __future__ import annotations

import hashlib
import hmac
import re
from typing import Any, Mapping

from .models import TriggerConfig

PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z0-9_.]+)\s*\}\}")


def verify(secret: str, body: bytes, headers: Mapping[str, str]) -> bool:
    signature = headers.get("x-hub-signature-256")
    if signature:
        expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected)
    token = headers.get("x-conductor-token")
    return bool(token) and hmac.compare_digest(token, secret)


def lookup(data: Any, path: str) -> Any:
    cur = data
    for part in path.split("."):
        if isinstance(cur, Mapping) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
    return cur


def matches(trigger: TriggerConfig, payload: Any) -> bool:
    ctx = {"payload": payload}
    return all(lookup(ctx, path) == expected for path, expected in trigger.when.items())


def render(template: Any, payload: Any) -> Any:
    ctx = {"payload": payload}
    if isinstance(template, str):
        return PLACEHOLDER.sub(lambda m: _text(lookup(ctx, m.group(1))), template)
    if isinstance(template, list):
        return [render(v, payload) for v in template]
    if isinstance(template, dict):
        return {k: render(v, payload) for k, v in template.items()}
    return template


def _text(value: Any) -> str:
    return "" if value is None else str(value)

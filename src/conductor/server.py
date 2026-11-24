from __future__ import annotations

import hmac
import json
from typing import Any

import yaml
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, ValidationError

from . import triggers
from .engine import Conflict, Engine, NotFound, PolicyError
from .models import ServerConfig, WorkflowSpec


class Offer(BaseModel):
    agent_id: str
    slots: int = 1


class ClaimRequest(BaseModel):
    agents: list[Offer]


class Event(BaseModel):
    kind: str = "log"
    text: str
    ts: float | None = None


class HeartbeatRequest(BaseModel):
    events: list[Event] = Field(default_factory=list)


class CompleteRequest(BaseModel):
    status: str
    exit_code: int | None = None
    summary: str | None = None
    error: str | None = None


def _bearer(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token")
    return auth[7:].strip()


def create_app(cfg: ServerConfig) -> FastAPI:
    engine = Engine(cfg)
    admin_token = cfg.secret(cfg.admin_token_env)
    zone_tokens = {name: cfg.secret(z.token_env) for name, z in cfg.zones.items()}

    app = FastAPI(title="conductor")
    app.state.engine = engine

    @app.exception_handler(PolicyError)
    async def _policy(_: Request, exc: PolicyError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=403)

    @app.exception_handler(NotFound)
    async def _not_found(_: Request, exc: NotFound) -> JSONResponse:
        return JSONResponse({"detail": f"not found: {exc}"}, status_code=404)

    @app.exception_handler(Conflict)
    async def _conflict(_: Request, exc: Conflict) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=409)

    def admin(request: Request) -> str:
        if not hmac.compare_digest(_bearer(request), admin_token):
            raise HTTPException(401, "invalid admin token")
        return "admin"

    def zone(request: Request) -> str:
        token = _bearer(request)
        for name, expected in zone_tokens.items():
            if hmac.compare_digest(token, expected):
                return name
        raise HTTPException(401, "invalid zone token")

    def parse_workflow(data: Any) -> WorkflowSpec:
        try:
            return WorkflowSpec.model_validate(data)
        except ValidationError as exc:
            raise HTTPException(422, json.loads(exc.json())) from None

    # ------------------------------------------------------------ admin API

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/workflows")
    async def submit(request: Request, actor: str = Depends(admin)) -> dict[str, Any]:
        body = await request.body()
        try:
            data = yaml.safe_load(body)
        except yaml.YAMLError as exc:
            raise HTTPException(400, f"invalid YAML/JSON: {exc}") from None
        return engine.submit(parse_workflow(data), actor)

    @app.get("/api/workflows")
    def list_workflows(limit: int = 50, _: str = Depends(admin)) -> list[dict[str, Any]]:
        return engine.workflows(limit)

    @app.get("/api/workflows/{wf_id}")
    def get_workflow(wf_id: str, _: str = Depends(admin)) -> dict[str, Any]:
        return engine.workflow(wf_id)

    @app.post("/api/workflows/{wf_id}/cancel")
    def cancel(wf_id: str, actor: str = Depends(admin)) -> dict[str, Any]:
        return engine.cancel(wf_id, actor)

    @app.get("/api/workflows/{wf_id}/tasks/{task_id}/events")
    def task_events(wf_id: str, task_id: str, _: str = Depends(admin)) -> list[dict[str, Any]]:
        return engine.events(wf_id, task_id)

    @app.get("/api/workflows/{wf_id}/tasks/{task_id}/artifacts/{name}")
    def task_artifact(wf_id: str, task_id: str, name: str, _: str = Depends(admin)) -> FileResponse:
        return FileResponse(engine.artifact_path(wf_id, task_id, name))

    @app.get("/api/audit")
    def audit(limit: int = 200, _: str = Depends(admin)) -> list[dict[str, Any]]:
        return engine.audit_log(limit)

    # ------------------------------------------------------------ webhooks

    @app.post("/api/hooks/{name}")
    async def hook(name: str, request: Request) -> JSONResponse:
        trigger = next((t for t in cfg.triggers if t.name == name), None)
        if trigger is None:
            raise HTTPException(404, "unknown trigger")
        body = await request.body()
        if not triggers.verify(cfg.secret(trigger.secret_env), body, request.headers):
            raise HTTPException(401, "invalid signature")
        try:
            payload = json.loads(body or b"null")
        except json.JSONDecodeError:
            raise HTTPException(400, "payload must be JSON") from None
        if not triggers.matches(trigger, payload):
            return JSONResponse({"triggered": False}, status_code=202)
        spec = parse_workflow(triggers.render(trigger.workflow, payload))
        wf = engine.submit(spec, actor=f"trigger:{name}", source=f"hook:{name}")
        return JSONResponse({"triggered": True, "workflow_id": wf["id"]}, status_code=201)

    # ------------------------------------------------------------ runner API

    @app.post("/api/runner/claim")
    def claim(req: ClaimRequest, zone_name: str = Depends(zone)) -> dict[str, Any]:
        return {"run": engine.claim(zone_name, [o.model_dump() for o in req.agents])}

    @app.post("/api/runner/runs/{run_id}/heartbeat")
    def heartbeat(run_id: str, req: HeartbeatRequest, zone_name: str = Depends(zone)) -> dict[str, bool]:
        engine.heartbeat(zone_name, run_id, [e.model_dump() for e in req.events])
        return {"ok": True}

    @app.put("/api/runner/runs/{run_id}/artifacts/{name}")
    async def put_artifact(run_id: str, name: str, request: Request, zone_name: str = Depends(zone)) -> dict[str, Any]:
        return engine.put_artifact(zone_name, run_id, name, await request.body())

    @app.post("/api/runner/runs/{run_id}/complete")
    def complete(run_id: str, req: CompleteRequest, zone_name: str = Depends(zone)) -> dict[str, Any]:
        return engine.complete(zone_name, run_id, req.status, req.exit_code, req.summary, req.error)

    @app.get("/api/runner/runs/{run_id}/inputs")
    def inputs(run_id: str, zone_name: str = Depends(zone)) -> list[dict[str, Any]]:
        return engine.run_inputs(zone_name, run_id)

    @app.get("/api/runner/runs/{run_id}/inputs/{task_id}/{name}")
    def read_input(run_id: str, task_id: str, name: str, zone_name: str = Depends(zone)) -> FileResponse:
        return FileResponse(engine.read_input(zone_name, run_id, task_id, name))

    return app

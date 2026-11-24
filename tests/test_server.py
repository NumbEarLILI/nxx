import hashlib
import hmac
import json
import time

from conftest import server_config
from fastapi.testclient import TestClient

from conductor.server import create_app

TRUSTED = {"Authorization": "Bearer trusted-secret"}
GENERAL = {"Authorization": "Bearer general-secret"}


def submit(client, admin, tasks, name="wf"):
    resp = client.post("/api/workflows", headers=admin, content=json.dumps({"name": name, "tasks": tasks}))
    assert resp.status_code == 200, resp.text
    return resp.json()


def claim(client, headers, agent_id):
    resp = client.post("/api/runner/claim", headers=headers, json={"agents": [{"agent_id": agent_id}]})
    assert resp.status_code == 200, resp.text
    return resp.json()["run"]


def test_auth_required(client, admin):
    assert client.get("/api/workflows").status_code == 401
    assert client.get("/api/workflows", headers=GENERAL).status_code == 401
    assert client.post("/api/runner/claim", headers=admin, json={"agents": []}).status_code == 401


def test_yaml_submission(client, admin):
    body = "name: y\ntasks:\n  - id: a\n    title: hello\n"
    resp = client.post("/api/workflows", headers=admin, content=body)
    assert resp.status_code == 200
    assert resp.json()["tasks"][0]["status"] == "pending"


def test_sensitive_task_only_goes_to_trusted_zone(client, admin):
    submit(client, admin, [{"id": "s", "title": "rotate keys", "paths": ["secrets/prod.env"], "requires": ["code"]}])
    assert claim(client, GENERAL, "claude-code") is None
    run = claim(client, TRUSTED, "opencode-private")
    assert run["task"]["id"] == "s" and run["level"] == "L3" and run["sealed"] is True


def test_runner_cannot_claim_for_agent_in_other_zone(client, admin):
    resp = client.post("/api/runner/claim", headers=GENERAL, json={"agents": [{"agent_id": "opencode-private"}]})
    assert resp.status_code == 403


def test_capabilities_are_respected(client, admin):
    submit(client, admin, [{"id": "ui", "title": "button", "requires": ["frontend"]}])
    assert claim(client, TRUSTED, "opencode-private") is None
    assert claim(client, GENERAL, "claude-code")["task"]["id"] == "ui"


def test_sealed_zone_only_takes_work_open_zones_cannot_do(client, admin, tmp_path):
    submit(client, admin, [{"id": "doc", "title": "user guide", "requires": ["docs"]}])
    assert claim(client, TRUSTED, "opencode-private") is None
    assert claim(client, GENERAL, "claude-code")["task"]["id"] == "doc"

    cfg = server_config(tmp_path / "override")
    cfg.zones["trusted"].min_level = "L0"
    override = TestClient(create_app(cfg))
    submit(override, admin, [{"id": "doc", "title": "user guide", "requires": ["docs"]}])
    assert claim(override, TRUSTED, "opencode-private")["task"]["id"] == "doc"


def test_sealed_zone_cannot_export_content(client, admin):
    submit(client, admin, [{"id": "s", "title": "x", "level": "L3"}])
    run = claim(client, TRUSTED, "opencode-private")
    rid = run["run_id"]
    assert client.put(f"/api/runner/runs/{rid}/artifacts/out.txt", headers=TRUSTED, content=b"data").status_code == 403
    hb = client.post(f"/api/runner/runs/{rid}/heartbeat", headers=TRUSTED, json={"events": [{"text": "secret log"}]})
    assert hb.status_code == 403
    assert client.post(f"/api/runner/runs/{rid}/heartbeat", headers=TRUSTED, json={"events": []}).status_code == 200
    done = client.post(f"/api/runner/runs/{rid}/complete", headers=TRUSTED, json={"status": "succeeded", "summary": "leak"})
    assert done.status_code == 403
    done = client.post(f"/api/runner/runs/{rid}/complete", headers=TRUSTED, json={"status": "succeeded", "exit_code": 0})
    assert done.status_code == 200


def test_consumers_of_sealed_output_are_pinned_to_the_sealed_zone(client, admin):
    wf = submit(
        client,
        admin,
        [
            {"id": "contract", "title": "api contract", "requires": ["code"]},
            {"id": "signing", "title": "sign", "level": "L3", "depends_on": ["contract"]},
            {"id": "notes", "title": "notes", "depends_on": ["signing"], "requires": ["docs"]},
            {"id": "ui", "title": "ui", "depends_on": ["contract"], "requires": ["frontend"]},
        ],
    )
    levels = {t["id"]: t["level"] for t in wf["tasks"]}
    assert levels == {"contract": "L0", "signing": "L3", "notes": "L3", "ui": "L0"}

    run = claim(client, GENERAL, "claude-code")
    assert run["task"]["id"] == "contract"
    client.put(f"/api/runner/runs/{run['run_id']}/artifacts/contract.md", headers=GENERAL, content=b"POST /pay")
    client.post(f"/api/runner/runs/{run['run_id']}/complete", headers=GENERAL, json={"status": "succeeded"})

    run = claim(client, TRUSTED, "opencode-private")
    assert run["task"]["id"] == "signing"
    inputs = client.get(f"/api/runner/runs/{run['run_id']}/inputs", headers=TRUSTED).json()
    assert inputs == [{"task_id": "contract", "sealed": False, "zone": "general", "artifacts": ["contract.md"]}]
    got = client.get(f"/api/runner/runs/{run['run_id']}/inputs/contract/contract.md", headers=TRUSTED)
    assert got.content == b"POST /pay"
    client.post(f"/api/runner/runs/{run['run_id']}/complete", headers=TRUSTED, json={"status": "succeeded"})

    run = claim(client, GENERAL, "claude-code")
    assert run["task"]["id"] == "ui"
    assert claim(client, GENERAL, "claude-code") is None
    assert claim(client, TRUSTED, "opencode-private")["task"]["id"] == "notes"


def test_general_zone_cannot_read_higher_labelled_artifacts(tmp_path, monkeypatch, admin):
    monkeypatch.setenv("INTERNAL_TOKEN", "internal-secret")
    cfg = server_config(
        tmp_path,
        zones={
            "trusted": {"clearance": "L3", "sealed": True, "token_env": "TRUSTED_TOKEN"},
            "general": {"clearance": "L1", "token_env": "GENERAL_TOKEN"},
            "internal": {"clearance": "L2", "token_env": "INTERNAL_TOKEN"},
        },
        agents=[
            {"id": "opencode-private", "zone": "trusted", "capabilities": ["code"]},
            {"id": "claude-code", "zone": "general", "capabilities": ["code"]},
            {"id": "internal-bot", "zone": "internal", "capabilities": ["code"]},
        ],
    )
    client = TestClient(create_app(cfg))
    internal = {"Authorization": "Bearer internal-secret"}
    submit(
        client,
        admin,
        [
            {"id": "pii", "title": "analyse 13812345678 call logs"},
            {"id": "public", "title": "write blog", "depends_on": ["pii"], "inputs": []},
        ],
    )
    run = claim(client, internal, "internal-bot")
    assert run["level"] == "L2" and run["sealed"] is False
    client.put(f"/api/runner/runs/{run['run_id']}/artifacts/r.md", headers=internal, content=b"pii")
    client.post(f"/api/runner/runs/{run['run_id']}/complete", headers=internal, json={"status": "succeeded"})

    run = claim(client, GENERAL, "claude-code")
    assert run["task"]["id"] == "public"
    denied = client.get(f"/api/runner/runs/{run['run_id']}/inputs/pii/r.md", headers=GENERAL)
    assert denied.status_code == 403
    actions = [a["action"] for a in client.get("/api/audit", headers=admin).json()]
    assert "artifact.denied" in actions


def test_failure_retries_then_skips_dependents(client, admin):
    wf = submit(client, admin, [{"id": "a", "title": "a"}, {"id": "b", "title": "b", "depends_on": ["a"]}])
    for _ in range(2):
        run = claim(client, GENERAL, "claude-code")
        assert run["task"]["id"] == "a"
        client.post(f"/api/runner/runs/{run['run_id']}/complete", headers=GENERAL, json={"status": "failed", "error": "boom"})
    state = client.get(f"/api/workflows/{wf['id']}", headers=admin).json()
    assert state["status"] == "failed"
    assert [(t["id"], t["status"]) for t in state["tasks"]] == [("a", "failed"), ("b", "skipped")]


def test_expired_lease_is_requeued(tmp_path, admin):
    client = TestClient(create_app(server_config(tmp_path, lease_seconds=0)))
    submit(client, admin, [{"id": "a", "title": "a"}])
    first = claim(client, GENERAL, "claude-code")
    time.sleep(0.01)
    second = claim(client, GENERAL, "claude-code")
    assert second["task"]["id"] == "a" and second["run_id"] != first["run_id"]
    stale = client.post(f"/api/runner/runs/{first['run_id']}/complete", headers=GENERAL, json={"status": "succeeded"})
    assert stale.status_code == 409


def test_cancel(client, admin):
    wf = submit(client, admin, [{"id": "a", "title": "a"}])
    resp = client.post(f"/api/workflows/{wf['id']}/cancel", headers=admin)
    assert resp.json()["status"] == "cancelled"
    assert claim(client, GENERAL, "claude-code") is None


def test_webhook_trigger(client, admin):
    payload = {"action": "labeled", "label": {"name": "agent"}, "issue": {"number": 7, "title": "Fix login", "body": "steps"}}
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(b"hook-secret", body, hashlib.sha256).hexdigest()

    assert client.post("/api/hooks/issues", content=body, headers={"X-Hub-Signature-256": "sha256=bad"}).status_code == 401
    resp = client.post("/api/hooks/issues", content=body, headers={"X-Hub-Signature-256": sig})
    assert resp.status_code == 201
    wf = client.get(f"/api/workflows/{resp.json()['workflow_id']}", headers=admin).json()
    assert wf["name"] == "issue 7" and wf["source"] == "hook:issues"

    other = json.dumps({**payload, "action": "opened"}).encode()
    resp = client.post("/api/hooks/issues", content=other, headers={"X-Conductor-Token": "hook-secret"})
    assert resp.status_code == 202 and resp.json() == {"triggered": False}

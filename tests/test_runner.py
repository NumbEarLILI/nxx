import json
import subprocess

from conftest import make_runner


def submit(client, admin, tasks):
    resp = client.post("/api/workflows", headers=admin, content=json.dumps({"name": "e2e", "tasks": tasks}))
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def test_end_to_end_mixed_sensitivity(client, admin, tmp_path, repo):
    wf_id = submit(
        client,
        admin,
        [
            {
                "id": "contract",
                "title": "Write the API contract",
                "requires": ["code"],
                "workspace": {"type": "git", "source": str(repo)},
            },
            {
                "id": "signing",
                "title": "Implement signing",
                "requires": ["security"],
                "paths": ["secrets/key.pem"],
                "depends_on": ["contract"],
                "workspace": {"type": "git", "source": str(repo)},
            },
            {"id": "audit-notes", "title": "Summarise signing review", "requires": ["docs"], "depends_on": ["signing"]},
        ],
    )
    general = make_runner(client, tmp_path, "general", "claude-code", workspace_exclude=["secrets/**"])
    trusted = make_runner(client, tmp_path, "trusted", "opencode-private")

    assert general.run_once()
    assert not general.run_once()
    assert trusted.run_once()
    assert trusted.run_once()

    wf = client.get(f"/api/workflows/{wf_id}", headers=admin).json()
    tasks = {t["id"]: t for t in wf["tasks"]}
    assert wf["status"] == "succeeded", tasks
    assert tasks["contract"]["artifacts"] == ["changes.diff", "report.md"]
    assert "fake agent done" in tasks["contract"]["summary"]

    report = client.get(f"/api/workflows/{wf_id}/tasks/contract/artifacts/report.md", headers=admin).text
    assert "workspace=README.md,src/app.py\n" in report, "excluded paths must not reach the general zone"
    assert "tokens_in_env=\n" in report, "runner credentials must not leak into the agent"
    diff = client.get(f"/api/workflows/{wf_id}/tasks/contract/artifacts/changes.diff", headers=admin).text
    assert "+edited by contract" in diff

    for tid in ("signing", "audit-notes"):
        assert tasks[tid]["sealed"] and tasks[tid]["artifacts"] == [] and tasks[tid]["summary"] is None
        assert client.get(f"/api/workflows/{wf_id}/tasks/{tid}/events", headers=admin).json() == []

    local = tmp_path / "runner-trusted" / "artifacts" / wf_id
    signing = (local / "signing" / "report.md").read_text()
    assert "secrets/key.pem" in signing and "inputs=contract/changes.diff,contract/report.md" in signing
    notes = (local / "audit-notes" / "report.md").read_text()
    assert "inputs=signing/changes.diff,signing/report.md" in notes
    assert "task=signing" in notes


def test_excluded_content_is_absent_from_git_history(client, admin, tmp_path, repo):
    submit(client, admin, [{"id": "a", "title": "t", "workspace": {"type": "git", "source": str(repo)}}])
    runner = make_runner(client, tmp_path, "general", "claude-code", workspace_exclude=["secrets/**"])
    run = runner.claim()
    runner.execute(run)
    ws = tmp_path / "runner-general" / "runs" / run["run_id"] / "workspace"
    objects = subprocess.run(["git", "-C", str(ws), "log", "--all", "-p"], capture_output=True, text=True).stdout
    assert "TOP SECRET" not in objects


def test_failed_acceptance_marks_task_failed(client, admin, tmp_path):
    wf_id = submit(client, admin, [{"id": "a", "title": "t", "acceptance": ["exit 3"], "max_attempts": 1}])
    runner = make_runner(client, tmp_path, "general", "claude-code")
    assert runner.run_once()
    task = client.get(f"/api/workflows/{wf_id}", headers=admin).json()["tasks"][0]
    assert task["status"] == "failed" and "acceptance check failed (3)" in task["error"]


def test_disallowed_workspace_source_fails_run(client, admin, tmp_path, repo):
    wf_id = submit(
        client, admin, [{"id": "a", "title": "t", "max_attempts": 1, "workspace": {"type": "dir", "source": str(repo)}}]
    )
    runner = make_runner(client, tmp_path, "general", "claude-code", allowed_sources=[str(tmp_path / "elsewhere")])
    assert runner.run_once()
    task = client.get(f"/api/workflows/{wf_id}", headers=admin).json()["tasks"][0]
    assert task["status"] == "failed" and "allowed_sources" in task["error"]


def test_sealed_failure_keeps_error_local(client, admin, tmp_path):
    wf_id = submit(client, admin, [{"id": "a", "title": "t", "level": "L3", "max_attempts": 1}])
    runner = make_runner(client, tmp_path, "trusted", "opencode-private", agent_env={"FAKE_EXIT": "2"})
    run = runner.claim()
    runner.execute(run)
    task = client.get(f"/api/workflows/{wf_id}", headers=admin).json()["tasks"][0]
    assert task["status"] == "failed" and task["exit_code"] == 2 and task["error"] is None
    assert (tmp_path / "runner-trusted" / "runs" / run["run_id"] / "error.txt").exists()

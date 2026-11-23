import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conductor.models import ServerConfig
from conductor.runner import Runner, RunnerConfig
from conductor.server import create_app

FAKE_AGENT = str(Path(__file__).with_name("fake_agent.py"))
TOKENS = {
    "CONDUCTOR_ADMIN_TOKEN": "admin-secret",
    "TRUSTED_TOKEN": "trusted-secret",
    "GENERAL_TOKEN": "general-secret",
    "HOOK_SECRET": "hook-secret",
}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for k, v in TOKENS.items():
        monkeypatch.setenv(k, v)


def server_config(tmp_path: Path, **overrides) -> ServerConfig:
    data = {
        "data_dir": str(tmp_path / "server"),
        "zones": {
            "trusted": {"clearance": "L3", "sealed": True, "token_env": "TRUSTED_TOKEN"},
            "general": {"clearance": "L1", "token_env": "GENERAL_TOKEN"},
        },
        "agents": [
            {"id": "opencode-private", "zone": "trusted", "capabilities": ["code", "security", "docs"]},
            {"id": "claude-code", "zone": "general", "capabilities": ["code", "docs", "frontend"], "max_concurrency": 2},
        ],
        "classification": {"paths": [{"glob": "secrets/**", "level": "L3"}]},
        "triggers": [
            {
                "name": "issues",
                "secret_env": "HOOK_SECRET",
                "when": {"payload.action": "labeled", "payload.label.name": "agent"},
                "workflow": {
                    "name": "issue {{payload.issue.number}}",
                    "tasks": [{"id": "fix", "title": "{{payload.issue.title}}", "description": "{{payload.issue.body}}", "requires": ["code"]}],
                },
            }
        ],
    }
    data.update(overrides)
    return ServerConfig.model_validate(data)


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(server_config(tmp_path)))


@pytest.fixture
def admin():
    return {"Authorization": "Bearer admin-secret"}


def make_runner(client: TestClient, tmp_path: Path, zone: str, agent_id: str, agent_env=None, **overrides) -> Runner:
    token_env = "TRUSTED_TOKEN" if zone == "trusted" else "GENERAL_TOKEN"
    cfg = RunnerConfig.model_validate(
        {
            "server": "http://testserver",
            "zone": zone,
            "token_env": token_env,
            "data_dir": str(tmp_path / f"runner-{zone}"),
            "heartbeat_interval": 3600,
            "agents": {agent_id: {"command": [sys.executable, FAKE_AGENT, "{prompt_file}"], "env": agent_env or {}}},
            **overrides,
        }
    )
    http = TestClient(client.app)
    return Runner(cfg, client=http)


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    (path / "secrets").mkdir(parents=True)
    (path / "src").mkdir()
    (path / "README.md").write_text("hello\n")
    (path / "src" / "app.py").write_text("print('hi')\n")
    (path / "secrets" / "key.pem").write_text("TOP SECRET\n")
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run([*git, "-C", str(path), "add", "-A"], check=True)
    subprocess.run([*git, "-C", str(path), "commit", "-qm", "init"], check=True)
    return path

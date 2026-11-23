from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

import httpx

from .models import ServerConfig, load_yaml


def _admin_client(args: argparse.Namespace) -> httpx.Client:
    token = os.environ.get("CONDUCTOR_ADMIN_TOKEN", "")
    if not token:
        sys.exit("CONDUCTOR_ADMIN_TOKEN is not set")
    return httpx.Client(base_url=args.server, headers={"Authorization": f"Bearer {token}"}, timeout=30)


def cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn

    from .server import create_app

    cfg = ServerConfig.model_validate(load_yaml(args.config))
    uvicorn.run(create_app(cfg), host=args.host, port=args.port)


def cmd_runner(args: argparse.Namespace) -> None:
    from .runner import Runner, RunnerConfig

    cfg = RunnerConfig.model_validate(load_yaml(args.config))
    Runner(cfg).run_forever()


def cmd_submit(args: argparse.Namespace) -> None:
    with _admin_client(args) as http:
        resp = http.post("/api/workflows", content=Path(args.workflow).read_bytes())
        if resp.status_code >= 400:
            sys.exit(f"{resp.status_code}: {resp.text}")
        wf = resp.json()
        print(f"workflow {wf['id']} submitted")
        for t in wf["tasks"]:
            print(f"  {t['id']:<24} {t['level']}  {'; '.join(t['level_reasons'])}")
        if args.wait:
            _wait(http, wf["id"])


def cmd_status(args: argparse.Namespace) -> None:
    with _admin_client(args) as http:
        if args.workflow_id:
            resp = http.get(f"/api/workflows/{args.workflow_id}")
            resp.raise_for_status()
            _print_workflow(resp.json())
        else:
            resp = http.get("/api/workflows")
            resp.raise_for_status()
            for wf in resp.json():
                print(f"{wf['id']}  {wf['status']:<10} {wf['name']}")


def _wait(http: httpx.Client, wf_id: str) -> None:
    while True:
        wf = http.get(f"/api/workflows/{wf_id}").json()
        if wf["status"] != "running":
            _print_workflow(wf)
            return
        time.sleep(3)


def _print_workflow(wf: dict) -> None:
    print(f"{wf['id']}  {wf['status']}  {wf['name']}")
    for t in wf["tasks"]:
        where = f"{t['agent_id'] or '-'}@{t['zone'] or '-'}"
        flag = " sealed" if t["sealed"] else ""
        print(f"  {t['id']:<24} {t['status']:<10} {t['level']} {where}{flag}")
        if t["error"]:
            print(f"    error: {t['error']}")
        if t["artifacts"]:
            print(f"    artifacts: {json.dumps(t['artifacts'])}")


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(prog="conductor")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("serve", help="run the orchestrator service")
    p.add_argument("--config", required=True)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8700)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("runner", help="run a zone runner")
    p.add_argument("--config", required=True)
    p.set_defaults(func=cmd_runner)

    for name, func in (("submit", cmd_submit), ("status", cmd_status)):
        p = sub.add_parser(name)
        p.add_argument("--server", default=os.environ.get("CONDUCTOR_SERVER", "http://127.0.0.1:8700"))
        p.set_defaults(func=func)
    sub.choices["submit"].add_argument("workflow")
    sub.choices["submit"].add_argument("--wait", action="store_true")
    sub.choices["status"].add_argument("workflow_id", nargs="?")

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()

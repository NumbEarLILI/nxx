"""Stand-in for a real agent CLI: records what it saw and edits the workspace."""

import os
import sys
from pathlib import Path

ws = Path(os.environ["CONDUCTOR_WORKSPACE"])
out = Path(os.environ["CONDUCTOR_OUTPUT_DIR"])
inputs = Path(os.environ["CONDUCTOR_INPUTS_DIR"])
prompt = Path(os.environ["CONDUCTOR_PROMPT_FILE"]).read_text()

seen_inputs = sorted(p.relative_to(inputs).as_posix() for p in inputs.rglob("*") if p.is_file())
seen_ws = sorted(p.relative_to(ws).as_posix() for p in ws.rglob("*") if p.is_file() and ".git" not in p.parts)
upstream = "".join(p.read_text() for p in sorted(inputs.rglob("report.md")))

(out / "report.md").write_text(
    f"task={os.environ['CONDUCTOR_TASK_ID']}\n"
    f"inputs={','.join(seen_inputs)}\n"
    f"workspace={','.join(seen_ws)}\n"
    f"tokens_in_env={','.join(sorted(k for k in os.environ if k.endswith('_TOKEN')))}\n"
    f"upstream<<\n{upstream}>>\n"
)
if (ws / "README.md").exists():
    with open(ws / "README.md", "a") as f:
        f.write(f"edited by {os.environ['CONDUCTOR_TASK_ID']}\n")

print("fake agent done:", prompt.splitlines()[0])
sys.exit(int(os.environ.get("FAKE_EXIT", "0")))

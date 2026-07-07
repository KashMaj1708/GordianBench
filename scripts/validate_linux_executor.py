#!/usr/bin/env python3
"""Smoke test for the Linux tooling-container executor (Test-3 prerequisite).

Brings up the archetype-d-stub broken stack, attaches the debug tooling
container to the stack network, and drives ToolExecutor.run_bash exactly as an
agent would — proving the agent can now: resolve service-name URLs, curl the
API, run concurrent load, and query Postgres with psql, all from a real Linux
shell. This is the capability whose absence made Phase 5's Test 3 uninterpretable.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.executor import ExecutorConfig, ToolExecutor
from agent.types import ToolCall
from harness.archetype_spec import ARCHETYPE_D_STUB
from harness.debug_container import debug_container_for_spec
from harness.lifecycle import stack_session
from harness.workspace import agent_workspace_session

CONCURRENT_LOAD = r"""
python - <<'PY'
import os, threading, requests
api = os.environ["API_URL"]
barrier = threading.Barrier(2)
def transfer(acct):
    barrier.wait()
    requests.post(api + "/transfer", json={"from_account": acct, "amount_cents": 1500}, timeout=10)
threads = [threading.Thread(target=transfer, args=(a,)) for a in ("pool-a", "pool-b")]
for t in threads: t.start()
for t in threads: t.join()
print("concurrent transfers fired")
PY
"""

PROBES = [
    ("uname + shell sanity", "uname -a; echo API_URL=$API_URL; echo DATABASE_URL=$DATABASE_URL"),
    ("curl /balances (service-name DNS)", "curl -s $API_URL/balances"),
    ("concurrent $15 transfers pool-a/pool-b", CONCURRENT_LOAD),
    ("curl /balances after load", "curl -s $API_URL/balances"),
    ("psql balances", 'psql "$DATABASE_URL" -t -c "SELECT id, balance_cents FROM accounts ORDER BY id;"'),
]


def main() -> int:
    spec = ARCHETYPE_D_STUB
    failures = 0
    with stack_session("broken", spec=spec) as session, agent_workspace_session(spec=spec) as workspace:
        with debug_container_for_spec(spec, workspace.root) as debug:
            executor = ToolExecutor(
                config=ExecutorConfig(
                    workspace_root=workspace.src_root,
                    repo_root=workspace.root,
                    gateway_url=session.gateway_url,
                    database_url=session.database_url,
                    debug=debug,
                )
            )
            for i, (label, cmd) in enumerate(PROBES):
                result = executor.dispatch(
                    ToolCall(id=f"probe-{i}", name="run_bash", input={"command": cmd})
                )
                status = "ERR " if result.is_error else "ok  "
                print(f"[{status}] {label}")
                print("    " + result.output.strip().replace("\n", "\n    "))
                if result.is_error:
                    failures += 1
    print()
    if failures:
        print(f"FAIL: {failures} probe(s) errored")
        return 1
    print("PASS: agent run_bash has real Linux shell + in-network reach")
    return 0


if __name__ == "__main__":
    sys.exit(main())

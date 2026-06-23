"""Shared tool executor — provider-agnostic container I/O."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from agent.patch_util import sanitize_model_patch
from agent.types import ToolCall, ToolResult

ARCHETYPE_ROOT = Path(__file__).resolve().parents[1] / "archetype-a"
DEFAULT_BASH_TIMEOUT_SEC = 120


@dataclass
class ExecutorConfig:
    workspace_root: Path = ARCHETYPE_ROOT / "src"
    gateway_url: str = "http://localhost:8080"
    database_url: str = "postgresql://bench:bench@localhost:5433/payments"
    bash_timeout_sec: float = DEFAULT_BASH_TIMEOUT_SEC


class ToolExecutor:
    """Dispatches canonical ToolCall → ToolResult. Shared by all providers."""

    def __init__(self, config: ExecutorConfig | None = None):
        self.config = config or ExecutorConfig()

    def dispatch(self, call: ToolCall) -> ToolResult:
        try:
            if call.name == "read_file":
                return self._read_file(call)
            if call.name == "write_file":
                return self._write_file(call)
            if call.name == "run_bash":
                return self._run_bash(call)
            if call.name == "run_tier1_test":
                return self._run_tier1(call)
            if call.name == "submit_patch":
                patch = sanitize_model_patch(str(call.input.get("model_patch", "")))
                return ToolResult(
                    tool_call_id=call.id,
                    output=f"patch received ({len(patch)} bytes); call grade_patch after loop ends",
                )
            return ToolResult(
                tool_call_id=call.id,
                output=f"unknown tool: {call.name}",
                is_error=True,
            )
        except Exception as exc:
            return ToolResult(tool_call_id=call.id, output=str(exc), is_error=True)

    def _resolve(self, rel: str) -> Path:
        rel = rel.replace("\\", "/").lstrip("/")
        if rel.startswith("src/"):
            rel = rel[4:]
        path = (self.config.workspace_root / rel).resolve()
        if not str(path).startswith(str(self.config.workspace_root.resolve())):
            raise ValueError("path escapes workspace")
        return path

    def _read_file(self, call: ToolCall) -> ToolResult:
        path = self._resolve(call.input["path"])
        if not path.exists():
            return ToolResult(tool_call_id=call.id, output=f"not found: {path}", is_error=True)
        return ToolResult(tool_call_id=call.id, output=path.read_text(encoding="utf-8"))

    def _write_file(self, call: ToolCall) -> ToolResult:
        path = self._resolve(call.input["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(call.input["content"], encoding="utf-8")
        return ToolResult(tool_call_id=call.id, output=f"wrote {path}")

    def _run_bash(self, call: ToolCall) -> ToolResult:
        timeout = float(call.input.get("timeout_sec", self.config.bash_timeout_sec))
        env = os.environ.copy()
        env["DEBIAN_FRONTEND"] = "noninteractive"
        env["GATEWAY_URL"] = self.config.gateway_url
        env["DATABASE_URL"] = self.config.database_url
        proc = subprocess.run(
            call.input["command"],
            shell=True,
            cwd=self.config.workspace_root,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode != 0:
            return ToolResult(
                tool_call_id=call.id,
                output=f"exit {proc.returncode}\n{out}",
                is_error=True,
            )
        return ToolResult(tool_call_id=call.id, output=out or "(no output)")

    def _run_tier1(self, call: ToolCall) -> ToolResult:
        env = os.environ.copy()
        env["GATEWAY_URL"] = self.config.gateway_url
        env["DATABASE_URL"] = self.config.database_url
        env["PYTHONPATH"] = str(ARCHETYPE_ROOT)
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "tier1_regression_test.py", "-q"],
            cwd=ARCHETYPE_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode != 0:
            return ToolResult(tool_call_id=call.id, output=out, is_error=True)
        return ToolResult(tool_call_id=call.id, output=out or "tier1 passed")

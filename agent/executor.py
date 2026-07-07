"""Shared tool executor — provider-agnostic container I/O."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from agent.patch_util import repair_model_patch, sanitize_model_patch
from agent.types import ToolCall, ToolResult

if TYPE_CHECKING:
    from harness.archetype_spec import ArchetypeSpec
    from harness.debug_container import DebugContainerSession

ARCHETYPE_ROOT = Path(__file__).resolve().parents[1] / "archetype-a"
DEFAULT_BASH_TIMEOUT_SEC = 120


@dataclass
class ExecutorConfig:
    workspace_root: Path = ARCHETYPE_ROOT / "src"
    repo_root: Path | None = None
    gateway_url: str = "http://localhost:8080"
    database_url: str = "postgresql://bench:bench@localhost:5433/payments"
    bash_timeout_sec: float = DEFAULT_BASH_TIMEOUT_SEC
    tier1_tests_workspace: bool = False
    stack_variant: str = "broken"
    # Per-archetype config: lets the loop compute the workspace diff over the
    # right service dirs and validate `git apply` against the right broken src.
    spec: "ArchetypeSpec | None" = None
    # When set, run_bash executes inside this Linux tooling container (on the
    # stack network) instead of the host shell. Required for investigation-chain
    # archetypes (Archetype D); see harness/debug_container.py.
    debug: "DebugContainerSession | None" = None

    @property
    def bash_cwd(self) -> Path:
        return self.repo_root or self.workspace_root


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
        # Force LF so the workspace stays byte-aligned with the LF-normalized
        # corpus; otherwise Windows newline translation corrupts the computed
        # diff and breaks git apply.
        content = call.input["content"].replace("\r\n", "\n").replace("\r", "\n")
        path.write_text(content, encoding="utf-8", newline="\n")
        return ToolResult(tool_call_id=call.id, output=f"wrote {path}")

    def workspace_diff(self) -> str:
        """Compute the patch from the workspace git state over service src dirs."""
        from harness.workspace import compute_workspace_diff

        repo = self.config.repo_root or self.config.workspace_root.parent
        service_dirs = self.config.spec.service_src_dirs if self.config.spec else ("src",)
        return compute_workspace_diff(repo, service_dirs)

    def workspace_patch_files(self) -> list[Path]:
        from harness.workspace import find_workspace_patch_files

        repo = self.config.repo_root or self.config.workspace_root.parent
        return find_workspace_patch_files(repo)

    def _run_bash(self, call: ToolCall) -> ToolResult:
        timeout = float(call.input.get("timeout_sec", self.config.bash_timeout_sec))
        if self.config.debug is not None:
            return self._run_bash_container(call, timeout)
        env = os.environ.copy()
        env["DEBIAN_FRONTEND"] = "noninteractive"
        env["GATEWAY_URL"] = self.config.gateway_url
        env["DATABASE_URL"] = self.config.database_url
        proc = subprocess.run(
            call.input["command"],
            shell=True,
            cwd=self.config.bash_cwd,
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

    def _run_bash_container(self, call: ToolCall, timeout: float) -> ToolResult:
        rc, out = self.config.debug.exec(call.input["command"], timeout=timeout)
        if rc != 0:
            return ToolResult(
                tool_call_id=call.id,
                output=f"exit {rc}\n{out}",
                is_error=True,
            )
        return ToolResult(tool_call_id=call.id, output=out or "(no output)")

    def _run_tier1(self, call: ToolCall) -> ToolResult:
        if self.config.tier1_tests_workspace:
            return self._run_tier1_workspace(call)
        return self._run_tier1_deployed(call)

    def _run_tier1_deployed(self, call: ToolCall) -> ToolResult:
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

    def _workspace_patch(self) -> str:
        repo = self.config.bash_cwd
        proc = subprocess.run(
            ["git", "diff", "--", "src/"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
        return repair_model_patch(proc.stdout or "")

    def _run_tier1_workspace(self, call: ToolCall) -> ToolResult:
        """Run Tier 1 against the agent's workspace diff (swap stack temporarily)."""
        from harness.lifecycle import deploy_variant, patch_session, teardown_stack, wait_for_healthy

        patch = self._workspace_patch()
        if not patch.strip():
            return ToolResult(
                tool_call_id=call.id,
                output="no workspace changes yet — edit gateway/upstream before run_tier1_test",
                is_error=True,
            )

        variant = self.config.stack_variant
        teardown_stack(variant)
        try:
            with patch_session(patch) as session:
                env = os.environ.copy()
                env["GATEWAY_URL"] = session.gateway_url
                env["DATABASE_URL"] = session.database_url
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
            return ToolResult(tool_call_id=call.id, output=out or "tier1 passed (workspace patch)")
        finally:
            deploy_variant(variant)
            wait_for_healthy()

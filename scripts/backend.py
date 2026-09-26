#!/usr/bin/env python3
"""Which engine runs the flow: "claude" (Claude Code) or "cursor" (Cursor CLI + Playwright).

  claude  fetch screening via `claude -p`; apply via Claude Code's codingjobboard-apply-agent
          driving your Chrome through the Claude in Chrome extension (auto-submits).
  cursor  fetch screening via `cursor-agent -p`; apply via scripts/apply_playwright.py, which
          opens Chrome with Simplify, autofills, and stops for you to review and submit.

Resolution order: AUTOAPPLY_BACKEND env var, then the .backend file at the repo root (written by
`./autoapply.sh backend <name>`), then "claude".

Usage:
    python3 scripts/backend.py            # print the active backend
    python3 scripts/backend.py cursor     # switch
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND_FILE = ROOT / ".backend"
BACKENDS = ("claude", "cursor")
DEFAULT_BACKEND = "claude"

CLAUDE_MODEL = os.environ.get("SCREEN_MODEL", "haiku")
# Unset means the Cursor account's default model; see `cursor-agent models` for choices.
CURSOR_MODEL = os.environ.get("CURSOR_SCREEN_MODEL", "")


def get_backend() -> str:
    name = os.environ.get("AUTOAPPLY_BACKEND") or (
        BACKEND_FILE.read_text(encoding="utf-8").strip() if BACKEND_FILE.exists() else DEFAULT_BACKEND
    )
    if name not in BACKENDS:
        raise SystemExit(f"Unknown backend {name!r} — expected one of {', '.join(BACKENDS)}")
    return name


def set_backend(name: str) -> None:
    if name not in BACKENDS:
        raise SystemExit(f"Unknown backend {name!r} — expected one of {', '.join(BACKENDS)}")
    BACKEND_FILE.write_text(name + "\n", encoding="utf-8")


def model_label(backend: str) -> str:
    return CLAUDE_MODEL if backend == "claude" else (CURSOR_MODEL or "account default")


def _cursor_bin() -> str:
    found = shutil.which("cursor-agent") or shutil.which("agent")
    fallback = Path.home() / ".local/bin/cursor-agent"
    if found:
        return found
    if fallback.exists():
        return str(fallback)
    raise RuntimeError("cursor-agent not found — install it: curl https://cursor.com/install -fsS | bash")


def _run(cmd: list, stdin: str | None) -> str:
    # Runs from a temp dir so neither CLI auto-discovers this repo's rules/CLAUDE.md and prepends
    # them to every single screening call.
    result = subprocess.run(
        cmd, input=stdin, capture_output=True, text=True, timeout=120, cwd=tempfile.gettempdir()
    )
    if result.returncode != 0:
        raise RuntimeError(f"{Path(cmd[0]).name} failed (exit {result.returncode}): {result.stderr.strip() or result.stdout.strip()}")
    output = result.stdout.strip()
    if not output:
        raise RuntimeError(f"{Path(cmd[0]).name} returned empty output. stderr: {result.stderr.strip()}")
    return output


def call_llm(prompt: str, system_prompt: str, backend: str | None = None) -> str:
    backend = backend or get_backend()
    if backend == "claude":
        return _run(
            [
                "claude", "-p",
                "--model", CLAUDE_MODEL,
                "--system-prompt", system_prompt,
                "--tools", "",
                "--no-session-persistence",
                "--strict-mcp-config",
            ],
            prompt,
        )
    cmd = [_cursor_bin(), "-p", "--mode", "ask", "--output-format", "text", "--trust",
           "--workspace", tempfile.gettempdir()]
    if CURSOR_MODEL:
        cmd += ["--model", CURSOR_MODEL]
    return _run(cmd + [f"{system_prompt}\n\n{prompt}"], None)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        set_backend(sys.argv[1])
        print(f"Backend set to: {sys.argv[1]}")
    else:
        print(get_backend())

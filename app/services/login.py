from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from pathlib import Path
from typing import Any, Protocol

from app.domain import LoginResult, LoginSecret


class LoginProvider(Protocol):
    async def login(self, secret: LoginSecret) -> LoginResult:
        """通过受控边界执行一次登录。"""


class CnblogsLoginProvider:
    def __init__(self, *, timeout_seconds: float, terminate_grace_seconds: float = 5.0) -> None:
        if terminate_grace_seconds <= 0:
            raise ValueError("terminate_grace_seconds 必须为正数")
        self._timeout_seconds = timeout_seconds
        self._terminate_grace_seconds = terminate_grace_seconds
        self._project_root = Path(__file__).resolve().parents[2]
        self._script = self._project_root / "scripts" / "cnblogs_signin.py"

    async def login(self, secret: LoginSecret) -> LoginResult:
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                str(self._script),
                "--submit",
                "--credentials-stdin",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=self._child_environment(),
                cwd=str(self._project_root),
                start_new_session=True,
            )
        except OSError:
            return LoginResult("refresh_unavailable")
        assert process.stdin is not None
        payload = json.dumps({"username": secret.username, "password": secret.password}) + "\n"
        try:
            process.stdin.write(payload.encode("utf-8"))
            await process.stdin.drain()
            process.stdin.close()
            stdout, _ = await asyncio.wait_for(process.communicate(), self._timeout_seconds)
        except asyncio.CancelledError:
            await self._terminate_and_reap(process)
            raise
        except asyncio.TimeoutError:
            await self._terminate_and_reap(process)
            return LoginResult("refresh_unavailable")
        except (BrokenPipeError, ConnectionResetError, OSError):
            await self._terminate_and_reap(process)
            return LoginResult("protocol_error")
        try:
            value: Any = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return LoginResult("protocol_error")
        if not isinstance(value, dict):
            return LoginResult("protocol_error")
        if value.get("success") is True:
            cookies = value.get("cookies")
            cookie_header = value.get("cookie_header")
            if self._valid_cookie_map(cookies) and isinstance(cookie_header, str) and cookie_header:
                session_id = value.get("session_id")
                return LoginResult(
                    "success",
                    cookies=cookies,
                    cookie_header=cookie_header,
                    session_id=session_id if isinstance(session_id, str) else None,
                )
            return LoginResult("protocol_error")
        category = value.get("category")
        return LoginResult(category if isinstance(category, str) else "protocol_error")

    @staticmethod
    def _valid_cookie_map(value: object) -> bool:
        return isinstance(value, dict) and all(
            isinstance(name, str) and isinstance(cookie_value, str)
            for name, cookie_value in value.items()
        )

    def _child_environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment.pop("CNBLOGS_PASSWORD", None)
        environment.pop("CNBLOGS_USERNAME", None)
        existing_python_path = environment.get("PYTHONPATH")
        python_path_entries = [str(self._project_root)]
        if existing_python_path:
            python_path_entries.append(existing_python_path)
        environment["PYTHONPATH"] = os.pathsep.join(python_path_entries)
        return environment

    async def _terminate_and_reap(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        self._signal_process_group(process, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), self._terminate_grace_seconds)
            return
        except asyncio.TimeoutError:
            self._signal_process_group(process, signal.SIGKILL)
        try:
            await asyncio.wait_for(process.wait(), self._terminate_grace_seconds)
        except asyncio.TimeoutError:
            return

    @staticmethod
    def _signal_process_group(process: asyncio.subprocess.Process, signal_number: signal.Signals) -> None:
        try:
            os.killpg(process.pid, signal_number)
        except ProcessLookupError:
            return

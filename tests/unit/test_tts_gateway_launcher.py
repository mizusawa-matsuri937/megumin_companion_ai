from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path

from app.tts_gateway.launcher import run_launcher
from app.workers.process import ManagedProcess


class _Process:
    pid = 123
    create_no_window = True

    def __init__(self) -> None:
        self.stdout = [b"private path and prompt", b""]
        self.stderr = [b"private weight name", b""]
        self.terminated = False
        self.closed = False

    async def write_stdin(self, data: bytes) -> None:
        del data

    async def read_stdout(self, max_bytes: int) -> bytes:
        assert max_bytes == 64 * 1024
        await asyncio.sleep(0)
        return self.stdout.pop(0)

    async def read_stderr(self, max_bytes: int) -> bytes:
        assert max_bytes == 64 * 1024
        await asyncio.sleep(0)
        return self.stderr.pop(0)

    async def wait(self) -> int:
        await asyncio.sleep(0.01)
        return 7

    async def terminate_tree(self, exit_code: int = 1) -> None:
        assert exit_code == 1
        self.terminated = True

    async def active_process_count(self) -> int | None:
        return 0

    async def close(self) -> None:
        self.closed = True


class _Adapter:
    supports_job_objects = True

    def __init__(self, process: _Process) -> None:
        self.process = process
        self.command: tuple[str, ...] | None = None

    async def spawn(
        self,
        command: Sequence[str],
        *,
        inherited_handles: Sequence[int] = (),
    ) -> ManagedProcess:
        assert not inherited_handles
        self.command = tuple(command)
        return self.process


def test_launcher_uses_job_owned_hidden_child_and_discards_output(tmp_path: Path) -> None:
    async def scenario() -> None:
        executable = tmp_path / "python.exe"
        executable.write_bytes(b"synthetic")
        manifest = tmp_path / "manifest.json"
        manifest.write_text("{}", encoding="utf-8")
        process = _Process()
        adapter = _Adapter(process)

        result = await run_launcher(
            manifest,
            adapter=adapter,
            python_executable=executable,
        )

        assert result == 7
        assert adapter.command == (
            str(executable.absolute()),
            "-B",
            "-m",
            "app.tts_gateway.entrypoint",
            "--manifest",
            str(manifest.absolute()),
        )
        assert process.terminated
        assert process.closed
        assert process.stdout in ([], [b""])
        assert process.stderr in ([], [b""])

    asyncio.run(scenario())


def test_launcher_requires_job_object_support(tmp_path: Path) -> None:
    class Unsupported:
        supports_job_objects = False

        async def spawn(
            self,
            command: Sequence[str],
            *,
            inherited_handles: Sequence[int] = (),
        ) -> ManagedProcess:
            del command, inherited_handles
            raise AssertionError("must not spawn")

    async def scenario() -> None:
        try:
            await run_launcher(
                tmp_path / "manifest.json",
                adapter=Unsupported(),
            )
        except Exception as exc:
            assert getattr(exc, "code", None) == "tts_gateway_job_object_required"
        else:
            raise AssertionError("missing job-object failure")

    asyncio.run(scenario())

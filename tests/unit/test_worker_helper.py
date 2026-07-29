"""In-process contract tests for the reusable helper runtime."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from app.workers.access import ApprovedResourcePolicy, AuthorizedResource
from app.workers.helper import HelperRuntime, emit_job_progress
from app.workers.protocol import FrameDecoder, HelperMessage, encode_message


class _Handler:
    def __init__(self, *, fail_close: bool = False) -> None:
        self.calls: list[tuple[str, tuple[AuthorizedResource, ...]]] = []
        self.closed = False
        self._fail_close = fail_close
        self.hold_started = asyncio.Event()
        self.hold_release = asyncio.Event()
        self.late_progress_release = asyncio.Event()
        self.late_progress_result: asyncio.Future[bool] | None = None
        self.flood_emitted = 0

    async def run_job(
        self,
        job_kind: str,
        resources: tuple[AuthorizedResource, ...],
        cancelled: asyncio.Event,
        *,
        job_id: str,
    ) -> dict[str, Any]:
        del job_id
        self.calls.append((job_kind, resources))
        if job_kind == "complete":
            return {"status": "ok"}
        if job_kind == "cancel.wait":
            await cancelled.wait()
            return {}
        if job_kind == "hold":
            self.hold_started.set()
            await self.hold_release.wait()
            return {}
        if job_kind == "progress":
            for index in range(1, 1001):
                assert emit_job_progress("mouth_envelope", index / 1000)
            self.hold_started.set()
            await self.hold_release.wait()
            return {"status": "ok"}
        if job_kind == "progress.late":
            loop = asyncio.get_running_loop()
            self.late_progress_result = loop.create_future()

            async def publish_after_result() -> None:
                await self.late_progress_release.wait()
                assert self.late_progress_result is not None
                self.late_progress_result.set_result(emit_job_progress("mouth_envelope", 0.75))

            asyncio.create_task(publish_after_result())
            return {"status": "ok"}
        if job_kind == "progress.flood":
            for _batch in range(100):
                for _ in range(10):
                    self.flood_emitted += 1
                    assert emit_job_progress(
                        "mouth_envelope",
                        (self.flood_emitted % 1000) / 1000,
                    )
                await asyncio.sleep(0)
            return {"status": "ok"}
        if job_kind == "fail":
            raise RuntimeError("W12_HELPER_EXCEPTION_BODY_SENTINEL")
        if job_kind == "invalid.result":
            return []  # type: ignore[return-value]
        return {}

    async def close(self) -> None:
        self.closed = True
        if self._fail_close:
            raise RuntimeError("W12_HELPER_CLOSE_BODY_SENTINEL")


class _MemoryPipes:
    def __init__(self) -> None:
        self.input: asyncio.Queue[bytes] = asyncio.Queue()
        self.output: asyncio.Queue[bytes] = asyncio.Queue()
        self.decoder = FrameDecoder()

    async def read(self, size: int) -> bytes:
        del size
        return await self.input.get()

    async def write(self, data: bytes) -> None:
        await self.output.put(data)

    async def next_message(self, selected_type: str | None = None) -> HelperMessage:
        async with asyncio.timeout(1):
            while True:
                for message in self.decoder.feed(await self.output.get()):
                    if selected_type is None or message.message_type == selected_type:
                        return message


def test_helper_handshake_heartbeat_jobs_cancel_resources_and_shutdown(tmp_path: Path) -> None:
    async def scenario() -> None:
        root = tmp_path / "approved"
        root.mkdir()
        source = root / "input.bin"
        source.write_bytes(b"synthetic")
        pipes = _MemoryPipes()
        handler = _Handler()
        runtime = HelperRuntime(
            role="media",
            handler=handler,
            resource_policy=ApprovedResourcePolicy(
                roots={"input": root},
                inherited_handles={"buffer": 7},
            ),
            heartbeat_interval_seconds=0.005,
            read=pipes.read,
            write=pipes.write,
        )
        task = asyncio.create_task(runtime.run())
        assert (await pipes.next_message()).payload == {"role": "media"}
        await pipes.input.put(
            encode_message(
                HelperMessage(
                    message_type="handshake.accepted",
                    payload={"role": "media"},
                )
            )
        )
        heartbeat = await pipes.next_message("heartbeat")
        assert heartbeat.payload["sequence"] >= 1

        await pipes.input.put(
            encode_message(
                HelperMessage(
                    message_type="job.start",
                    request_id="complete-1",
                    payload={
                        "job_kind": "complete",
                        "resources": [
                            {
                                "resource_id": "source",
                                "root_id": "input",
                                "relative_path": "input.bin",
                            },
                            {"resource_id": "pcm", "handle_id": "buffer"},
                        ],
                    },
                )
            )
        )
        completed = await pipes.next_message("job.completed")
        assert completed.request_id == "complete-1" and completed.payload == {"status": "ok"}
        resources = handler.calls[0][1]
        assert resources[0].owns_descriptor and resources[0].closed
        assert resources[1].value == 7 and not resources[1].owns_descriptor

        await pipes.input.put(
            encode_message(
                HelperMessage(
                    message_type="job.start",
                    request_id="cancel-1",
                    payload={"job_kind": "cancel.wait", "resources": []},
                )
            )
        )
        await asyncio.sleep(0)
        await pipes.input.put(
            encode_message(
                HelperMessage(message_type="job.cancel", request_id="cancel-1", payload={})
            )
        )
        assert (await pipes.next_message("job.cancelled")).request_id == "cancel-1"

        for job_id, job_kind, resources_payload in (
            ("failed-1", "fail", []),
            ("invalid-1", "invalid.result", []),
            (
                "resource-1",
                "complete",
                [{"resource_id": "source", "handle_id": "not-approved"}],
            ),
        ):
            await pipes.input.put(
                encode_message(
                    HelperMessage(
                        message_type="job.start",
                        request_id=job_id,
                        payload={"job_kind": job_kind, "resources": resources_payload},
                    )
                )
            )
            failed = await pipes.next_message("job.failed")
            assert failed.request_id == job_id
            assert failed.payload == {"error_code": "worker_job_failed"}

        await pipes.input.put(encode_message(HelperMessage(message_type="shutdown", payload={})))
        assert await task == 0
        assert handler.closed
        stopped = await pipes.next_message("stopped")
        assert stopped.payload == {}

    asyncio.run(scenario())


def test_helper_rejects_bad_handshake_truncated_input_and_unexpected_message() -> None:
    async def run_case(frame: bytes) -> tuple[int, bool]:
        pipes = _MemoryPipes()
        handler = _Handler()
        runtime = HelperRuntime(
            role="media",
            handler=handler,
            read=pipes.read,
            write=pipes.write,
        )
        task = asyncio.create_task(runtime.run())
        await pipes.next_message("hello")
        await pipes.input.put(frame)
        await pipes.input.put(b"")
        return await task, handler.closed

    async def scenario() -> None:
        wrong_role = encode_message(
            HelperMessage(
                message_type="handshake.accepted",
                payload={"role": "perception"},
            )
        )
        assert await run_case(wrong_role) == (2, True)
        assert await run_case(b"\x00\x00") == (2, True)

        pipes = _MemoryPipes()
        handler = _Handler()
        runtime = HelperRuntime(role="media", handler=handler, read=pipes.read, write=pipes.write)
        task = asyncio.create_task(runtime.run())
        await pipes.next_message("hello")
        await pipes.input.put(
            encode_message(
                HelperMessage(
                    message_type="handshake.accepted",
                    payload={"role": "media"},
                )
            )
        )
        await pipes.input.put(encode_message(HelperMessage(message_type="unexpected", payload={})))
        assert await task == 2
        assert handler.closed

    asyncio.run(scenario())


def test_helper_close_failure_returns_nonzero_and_never_reports_stopped() -> None:
    async def scenario() -> None:
        pipes = _MemoryPipes()
        handler = _Handler(fail_close=True)
        runtime = HelperRuntime(role="media", handler=handler, read=pipes.read, write=pipes.write)
        task = asyncio.create_task(runtime.run())
        await pipes.next_message("hello")
        await pipes.input.put(
            encode_message(
                HelperMessage(
                    message_type="handshake.accepted",
                    payload={"role": "media"},
                )
            )
        )
        await pipes.input.put(encode_message(HelperMessage(message_type="shutdown", payload={})))
        assert await task == 3
        assert handler.closed and pipes.output.empty()

    asyncio.run(scenario())


def test_helper_initial_pipe_failure_is_content_free_and_closes_handler() -> None:
    async def scenario() -> None:
        handler = _Handler()

        async def fail_write(data: bytes) -> None:
            del data
            raise OSError("W12_INITIAL_WRITE_BODY_SENTINEL")

        runtime = HelperRuntime(role="media", handler=handler, write=fail_write)
        assert await runtime.run() == 2
        assert handler.closed

        close_failure_handler = _Handler(fail_close=True)
        close_failure_runtime = HelperRuntime(
            role="media",
            handler=close_failure_handler,
            write=fail_write,
        )
        assert await close_failure_runtime.run() == 3
        assert close_failure_handler.closed

    asyncio.run(scenario())


def test_helper_progress_is_bounded_latest_wins_and_terminal_has_priority() -> None:
    async def scenario() -> None:
        assert not emit_job_progress("mouth_envelope", 0.5)
        pipes = _MemoryPipes()
        handler = _Handler()
        runtime = HelperRuntime(
            role="media",
            handler=handler,
            heartbeat_interval_seconds=0.005,
            read=pipes.read,
            write=pipes.write,
        )
        task = asyncio.create_task(runtime.run())
        await pipes.next_message("hello")
        await pipes.input.put(
            encode_message(
                HelperMessage(
                    message_type="handshake.accepted",
                    payload={"role": "media"},
                )
            )
        )
        await pipes.input.put(
            encode_message(
                HelperMessage(
                    message_type="job.start",
                    request_id="progress-1",
                    payload={"job_kind": "progress", "resources": []},
                )
            )
        )
        await asyncio.wait_for(handler.hold_started.wait(), timeout=1)
        progress = await pipes.next_message("job.progress")
        assert progress.request_id == "progress-1"
        assert progress.payload == {
            "kind": "mouth_envelope",
            "sequence": 1000,
            "value": 1.0,
        }
        handler.hold_release.set()
        completed = await pipes.next_message("job.completed")
        assert completed.request_id == "progress-1"
        assert completed.payload == {"status": "ok"}
        await pipes.input.put(encode_message(HelperMessage(message_type="shutdown", payload={})))
        assert await task == 0

    asyncio.run(scenario())


def test_helper_rejects_progress_once_terminal_delivery_has_started() -> None:
    async def scenario() -> None:
        pipes = _MemoryPipes()
        handler = _Handler()
        terminal_write_started = asyncio.Event()
        terminal_write_release = asyncio.Event()
        output_decoder = FrameDecoder()

        async def controlled_write(data: bytes) -> None:
            messages = output_decoder.feed(data)
            assert len(messages) == 1
            if messages[0].message_type == "job.completed":
                terminal_write_started.set()
                await terminal_write_release.wait()
            await pipes.output.put(data)

        runtime = HelperRuntime(
            role="media",
            handler=handler,
            heartbeat_interval_seconds=10.0,
            read=pipes.read,
            write=controlled_write,
        )
        task = asyncio.create_task(runtime.run())
        await pipes.next_message("hello")
        await pipes.input.put(
            encode_message(
                HelperMessage(
                    message_type="handshake.accepted",
                    payload={"role": "media"},
                )
            )
        )
        await pipes.input.put(
            encode_message(
                HelperMessage(
                    message_type="job.start",
                    request_id="progress-late",
                    payload={"job_kind": "progress.late", "resources": []},
                )
            )
        )
        await asyncio.wait_for(terminal_write_started.wait(), timeout=1)
        handler.late_progress_release.set()
        assert handler.late_progress_result is not None
        assert not await asyncio.wait_for(handler.late_progress_result, timeout=1)
        terminal_write_release.set()
        completed = await pipes.next_message("job.completed")
        assert completed.request_id == "progress-late"
        await asyncio.sleep(0.02)
        assert pipes.output.empty()

        await pipes.input.put(encode_message(HelperMessage(message_type="shutdown", payload={})))
        assert await task == 0

    asyncio.run(scenario())


def test_helper_progress_flood_coalesces_without_starving_heartbeat_or_terminal() -> None:
    async def scenario() -> None:
        pipes = _MemoryPipes()
        handler = _Handler()
        output_decoder = FrameDecoder()

        async def slow_progress_write(data: bytes) -> None:
            messages = output_decoder.feed(data)
            assert len(messages) == 1
            if messages[0].message_type == "job.progress":
                await asyncio.sleep(0.003)
            await pipes.output.put(data)

        runtime = HelperRuntime(
            role="media",
            handler=handler,
            heartbeat_interval_seconds=0.005,
            read=pipes.read,
            write=slow_progress_write,
        )
        task = asyncio.create_task(runtime.run())
        await pipes.next_message("hello")
        await pipes.input.put(
            encode_message(
                HelperMessage(
                    message_type="handshake.accepted",
                    payload={"role": "media"},
                )
            )
        )
        await pipes.input.put(
            encode_message(
                HelperMessage(
                    message_type="job.start",
                    request_id="progress-flood",
                    payload={"job_kind": "progress.flood", "resources": []},
                )
            )
        )

        observed: list[HelperMessage] = []
        async with asyncio.timeout(1):
            while not any(message.message_type == "job.completed" for message in observed):
                observed.append(await pipes.next_message())

        progress = [item for item in observed if item.message_type == "job.progress"]
        heartbeats = [item for item in observed if item.message_type == "heartbeat"]
        assert handler.flood_emitted == 1000
        assert 0 < len(progress) < handler.flood_emitted
        assert heartbeats
        assert observed[-1].message_type == "job.completed"
        assert observed[-1].request_id == "progress-flood"
        await asyncio.sleep(0)
        assert "progress-flood" not in runtime._progress_latest
        assert "progress-flood" not in runtime._progress_open_jobs

        await pipes.input.put(encode_message(HelperMessage(message_type="shutdown", payload={})))
        assert await task == 0

    asyncio.run(scenario())


def test_helper_enforces_its_own_active_job_ceiling() -> None:
    async def scenario() -> None:
        pipes = _MemoryPipes()
        handler = _Handler()
        runtime = HelperRuntime(
            role="media",
            handler=handler,
            maximum_active_jobs=1,
            read=pipes.read,
            write=pipes.write,
        )
        task = asyncio.create_task(runtime.run())
        await pipes.next_message("hello")
        await pipes.input.put(
            encode_message(
                HelperMessage(
                    message_type="handshake.accepted",
                    payload={"role": "media", "maximum_active_jobs": 1},
                )
            )
        )
        await pipes.input.put(
            encode_message(
                HelperMessage(
                    message_type="job.start",
                    request_id="one",
                    payload={"job_kind": "hold", "resources": []},
                )
            )
        )
        await asyncio.wait_for(handler.hold_started.wait(), timeout=0.1)
        await pipes.input.put(
            encode_message(
                HelperMessage(
                    message_type="job.start",
                    request_id="two",
                    payload={"job_kind": "hold", "resources": []},
                )
            )
        )
        failed = await pipes.next_message("job.failed")
        assert failed.request_id == "two"
        assert failed.payload == {"error_code": "worker_job_capacity"}
        assert len(handler.calls) == 1

        handler.hold_release.set()
        assert (await pipes.next_message("job.completed")).request_id == "one"
        await pipes.input.put(encode_message(HelperMessage(message_type="shutdown", payload={})))
        assert await task == 0

    asyncio.run(scenario())


def test_helper_closes_earlier_descriptors_when_later_resource_is_rejected(
    tmp_path: Path,
) -> None:
    class TrackingPolicy(ApprovedResourcePolicy):
        opened: AuthorizedResource | None = None

        def authorize_wire(self, raw: object) -> AuthorizedResource:
            resource = super().authorize_wire(raw)
            if resource.owns_descriptor:
                self.opened = resource
            return resource

    async def scenario() -> None:
        root = tmp_path / "approved"
        root.mkdir()
        (root / "input.bin").write_bytes(b"SAFE")
        policy = TrackingPolicy(roots={"input": root})
        pipes = _MemoryPipes()
        runtime = HelperRuntime(
            role="media",
            handler=_Handler(),
            resource_policy=policy,
            read=pipes.read,
            write=pipes.write,
        )
        task = asyncio.create_task(runtime.run())
        await pipes.next_message("hello")
        await pipes.input.put(
            encode_message(
                HelperMessage(
                    message_type="handshake.accepted",
                    payload={"role": "media"},
                )
            )
        )
        await pipes.input.put(
            encode_message(
                HelperMessage(
                    message_type="job.start",
                    request_id="partial-resource",
                    payload={
                        "job_kind": "complete",
                        "resources": [
                            {
                                "resource_id": "source",
                                "root_id": "input",
                                "relative_path": "input.bin",
                            },
                            {"resource_id": "bad", "handle_id": "missing"},
                        ],
                    },
                )
            )
        )
        assert (await pipes.next_message("job.failed")).request_id == "partial-resource"
        assert policy.opened is not None and policy.opened.closed
        await pipes.input.put(encode_message(HelperMessage(message_type="shutdown", payload={})))
        assert await task == 0

    asyncio.run(scenario())

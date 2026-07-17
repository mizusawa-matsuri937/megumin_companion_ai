"""Verify the production desktop entry point owns no TCP socket on Windows."""

from __future__ import annotations

import json
import subprocess
import sys


def _tcp_rows_for_process(process_id: int) -> list[str]:
    completed = subprocess.run(
        ["netstat", "-ano", "-p", "TCP"],
        check=True,
        capture_output=True,
        text=True,
        errors="replace",
    )
    process_text = str(process_id)
    rows: list[str] = []
    for line in completed.stdout.splitlines():
        fields = line.split()
        if fields and fields[0].upper() == "TCP" and fields[-1] == process_text:
            rows.append(" ".join(fields[:-1]))
    return rows


def main() -> int:
    if sys.platform != "win32":
        print(json.dumps({"status": "skipped", "reason": "windows_only"}, sort_keys=True))
        return 0

    child_code = (
        "import sys,time; "
        "from desktop_client.entrypoint import main; "
        "code=main([]); "
        "print(f'desktop_return={code};uvicorn_loaded={str(\"uvicorn\" in sys.modules).lower()}', "
        "flush=True); "
        "time.sleep(10)"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", child_code],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    stderr = ""
    try:
        assert process.stdout is not None
        marker = process.stdout.readline().strip()
        if marker != "desktop_return=3;uvicorn_loaded=false":
            raise RuntimeError(f"unexpected desktop preflight marker: {marker!r}")
        rows = _tcp_rows_for_process(process.pid)
        if rows:
            raise RuntimeError(f"desktop entry point owns TCP sockets: {rows}")
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            _stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            _stdout, stderr = process.communicate(timeout=5)

    if "desktop_unavailable" not in stderr:
        raise RuntimeError("desktop entry point did not report its current W13 preflight boundary")
    print(
        json.dumps(
            {
                "status": "ok",
                "desktop_return": 3,
                "uvicorn_loaded": False,
                "tcp_socket_count": 0,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

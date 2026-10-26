"""The sandbox container's entry point: `hk_entry.py script|inspect|pages`.

Reads /job/manifest.json, does the job, writes /out/status.json, then writes an
uncompressed tar of /out (members relative to /out, regular files only) to its own stdout
and exits 0. Nothing else is ever written to stdout.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import threading
import time
from pathlib import Path

HK_DIR = Path(__file__).resolve().parent
STDOUT_LIMIT = 262_144
DEFAULT_TIMEOUT_S = 110
DEFAULT_DPI = 150
MAX_PAGE_SIDE = 2000
_CHUNK = 65_536

if str(HK_DIR) not in sys.path:  # run with -I, so PYTHONPATH is ignored
    sys.path.insert(0, str(HK_DIR))


def _replace(path: Path) -> None:
    """Make room for a file the entry writes, whatever the script left at that path."""
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _replace(path)
    path.write_bytes(data)


def _drain(pipe, kept: bytearray, flags: dict) -> None:
    """Keep the first STDOUT_LIMIT bytes, read and drop the rest so the child never blocks."""
    while True:
        chunk = pipe.read1(_CHUNK) if hasattr(pipe, "read1") else pipe.read(_CHUNK)
        if not chunk:
            break
        room = STDOUT_LIMIT - len(kept)
        if room > 0:
            kept += chunk[:room]
        if len(chunk) > max(room, 0):
            flags["truncated"] = True
    pipe.close()


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.kill()
    except ProcessLookupError:
        pass


def run_script(job: Path, out: Path, work: Path, timeout_s: int) -> dict:
    env = {
        "HOME": str(work),
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "PYTHONPATH": str(HK_DIR),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        "LANG": "C.UTF-8",
        "MPLCONFIGDIR": str(work),
        "HK_JOB": str(job),
        "HK_OUT": str(out),
    }
    kept = bytearray()
    flags = {"truncated": False}
    started = time.monotonic()
    proc = subprocess.Popen(
        [sys.executable, "-B", str(job / "main.py")],
        cwd=str(work),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    reader = threading.Thread(target=_drain, args=(proc.stdout, kept, flags), daemon=True)
    reader.start()
    timed_out = False
    try:
        exit_code: int | None = proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        exit_code = None
        _kill_group(proc)
        proc.wait()
    # Anything the script left running in its group goes too, so the pipe closes.
    _kill_group(proc)
    reader.join(timeout=5)
    duration_ms = int((time.monotonic() - started) * 1000)
    _write(out / "stdout.txt", bytes(kept))
    return {
        "exit_code": exit_code,
        "timed_out": timed_out,
        "stdout_truncated": flags["truncated"],
        "duration_ms": duration_ms,
    }


def _entry_for(manifest: dict, file: str) -> dict:
    for key in ("id", "name"):
        for entry in manifest.get("files", []):
            if entry.get(key) == file:
                return entry
    raise ValueError("the file is not in the manifest")


def run_inspect(manifest: dict, out: Path) -> None:
    from hk._inspect import inspect

    entry = _entry_for(manifest, manifest.get("args", {}).get("file"))
    report = inspect(entry["path"], entry.get("kind"))
    _write(out / "inspect.json", json.dumps(report).encode("utf-8"))


def run_pages(manifest: dict, out: Path) -> None:
    import pypdfium2 as pdfium

    args = manifest.get("args", {})
    entry = _entry_for(manifest, args.get("file"))
    dpi = int(args.get("dpi") or DEFAULT_DPI)
    pages_dir = out / "pages"
    _replace(pages_dir)
    pages_dir.mkdir(parents=True)
    pdf = pdfium.PdfDocument(entry["path"])
    try:
        count = len(pdf)
        for n in args.get("pages") or []:
            if not isinstance(n, int) or not 1 <= n <= count:
                continue
            page = pdf[n - 1]
            try:
                image = page.render(scale=dpi / 72).to_pil()
            finally:
                page.close()
            if max(image.size) > MAX_PAGE_SIDE:
                image.thumbnail((MAX_PAGE_SIDE, MAX_PAGE_SIDE))
            image.save(pages_dir / f"{n}.png", format="PNG")
    finally:
        pdf.close()


def write_tar(out: Path, stream) -> None:
    """An uncompressed tar of `out`: regular files only (no links, devices or fifos)."""
    with tarfile.open(fileobj=stream, mode="w|", format=tarfile.PAX_FORMAT) as tar:
        for root, dirs, names in os.walk(out, followlinks=False):
            dirs.sort()
            for name in sorted(names):
                path = Path(root) / name
                info = os.lstat(path)
                if not stat.S_ISREG(info.st_mode):
                    continue
                member = tarfile.TarInfo(path.relative_to(out).as_posix())
                member.size = info.st_size
                member.mtime = int(info.st_mtime)
                member.mode = 0o644
                with path.open("rb") as fh:
                    tar.addfile(member, fh)


def run(
    mode: str,
    *,
    job: Path = Path("/job"),
    out: Path = Path("/out"),
    work: Path = Path("/tmp"),
    stdout=None,
) -> int:
    stream = stdout if stdout is not None else sys.stdout.buffer
    out.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    status = {"exit_code": None, "timed_out": False, "stdout_truncated": False, "duration_ms": 0}
    try:
        manifest = json.loads((job / "manifest.json").read_text(encoding="utf-8"))
        if mode == "script":
            timeout_s = int(manifest.get("script_timeout_s") or DEFAULT_TIMEOUT_S)
            status = run_script(job, out, work, timeout_s)
        elif mode in ("inspect", "pages"):
            (run_inspect if mode == "inspect" else run_pages)(manifest, out)
            status["exit_code"] = 0
        else:
            raise ValueError(f"unknown mode {mode!r}")
    except Exception as exc:  # report it in the output; stdout stays a clean tar
        status["exit_code"] = 1
        message = f"sandbox error: {type(exc).__name__}: {exc}\n".encode()
        if mode != "script" or not (out / "stdout.txt").exists():
            _write(out / "stdout.txt", message[:STDOUT_LIMIT])
    if not status.get("duration_ms"):
        status["duration_ms"] = int((time.monotonic() - started) * 1000)
    _write(out / "status.json", json.dumps(status).encode("utf-8"))
    write_tar(out, stream)
    stream.flush()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    mode = args[0] if args else ""
    return run(mode)


if __name__ == "__main__":
    sys.exit(main())

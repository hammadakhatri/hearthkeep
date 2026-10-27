"""The sandbox container's entry point: `hk_entry.py script|inspect|pages`.

Reads /job/manifest.json, does the job, writes /out/status.json, then writes an
uncompressed tar of /out (members relative to /out, regular files only) to its own stdout
and exits 0. Nothing else is ever written to stdout.

The script can leave anything in /out, so the entry trusts none of it for its own files:
status.json and stdout.txt go into the tar from memory, first, and every other member is
copied through a no-follow file descriptor whose size is fixed when it is opened.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
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
MIN_DPI, MAX_DPI = 50, 300
MAX_PAGE_SIDE = 2000
MAX_PAGE_ERRORS = 50  # page_errors entries kept in status.json
_CHUNK = 65_536
MAX_MEMBER_BYTES = 300 * 1024 * 1024  # a single tar member is capped at 300 MB
SPARSE_RATIO = 8  # allocated space under 1/8 of the size: a sparse file
SPARSE_MIN_BYTES = 1 << 20  # only members over 1 MiB are judged sparse
# The names the host takes back (a copy of runner.MEMBER_NAME); anything else is left out.
MEMBER_NAME = re.compile(
    r"status\.json|stdout\.txt|results\.jsonl|inspect\.json"
    r"|files/[A-Za-z0-9][A-Za-z0-9._ -]{0,99}|pages/(?:[1-9][0-9]{0,2}|1[0-9]{3}|2000)\.png",
    re.ASCII,
)
MAX_IGNORED_NOTES = 10
IGNORED_HINT = "save files with hk.file(name, data)."

if str(HK_DIR) not in sys.path:  # run with -I, so PYTHONPATH is ignored
    sys.path.insert(0, str(HK_DIR))


def _unlock(path: Path) -> None:
    """Give the entry (which owns everything in /out) full access to a directory tree the
    script may have locked, so it can be removed."""
    os.chmod(path, stat.S_IRWXU)
    for root, dirs, _names in os.walk(path):
        for name in dirs:
            sub = os.path.join(root, name)
            if not os.path.islink(sub):
                os.chmod(sub, stat.S_IRWXU)


def _replace(path: Path) -> None:
    """Make room for a file the entry writes, whatever the script left at that path."""
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        _unlock(path)
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()  # a fifo, socket or device node


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


def _kill_everything_else() -> None:
    """As the container's init (pid 1), kill every other process, including any the script
    detached into a new session, and reap them, so nothing writes to /out any more."""
    if os.getpid() != 1:
        return
    try:
        os.kill(-1, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return  # no children left
        if pid == 0:
            time.sleep(0.01)


def run_script(job: Path, out: Path, work: Path, timeout_s: int) -> tuple[dict, bytes]:
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
    # Anything the script left running goes too (its group, then anything that escaped
    # it), so the pipe closes and nothing writes to /out any more.
    _kill_group(proc)
    _kill_everything_else()
    reader.join(timeout=5)
    status = {
        "exit_code": exit_code,
        "timed_out": timed_out,
        "stdout_truncated": flags["truncated"],
        "duration_ms": int((time.monotonic() - started) * 1000),
    }
    return status, bytes(kept)


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


def run_pages(manifest: dict, out: Path) -> dict:
    """Render the pages asked for; returns what status.json adds: the PDF's page count and,
    for each page that failed to render, its exception's type name (at most
    MAX_PAGE_ERRORS of them)."""
    import pypdfium2 as pdfium

    args = manifest.get("args", {})
    entry = _entry_for(manifest, args.get("file"))
    dpi = min(max(int(args.get("dpi") or DEFAULT_DPI), MIN_DPI), MAX_DPI)
    pages_dir = out / "pages"
    _replace(pages_dir)
    pages_dir.mkdir(parents=True)
    errors: dict[str, str] = {}
    pdf = pdfium.PdfDocument(entry["path"])
    try:
        count = len(pdf)
        for n in args.get("pages") or []:
            if not isinstance(n, int) or not 1 <= n <= count:
                continue
            # A page that can't be rendered is left out (the host reports it); the others
            # still come back.
            target = pages_dir / f"{n}.png"
            try:
                _render_page(pdf, n, dpi, target)
            except Exception as exc:
                with contextlib.suppress(OSError):
                    target.unlink()  # never a half-written image
                if len(errors) < MAX_PAGE_ERRORS:
                    errors[str(n)] = type(exc).__name__[:64]
    finally:
        pdf.close()
    return {"page_count": count, "page_errors": errors}


def _render_page(pdf, n: int, dpi: int, target: Path) -> None:
    page = pdf[n - 1]
    try:
        # Size the render so the longest side fits before any pixels exist.
        longest = max(page.get_size()) or 1
        scale = min(dpi / 72, (MAX_PAGE_SIDE - 1) / longest)
        image = page.render(scale=scale).to_pil()
    finally:
        page.close()
    if max(image.size) > MAX_PAGE_SIDE:
        image.thumbnail((MAX_PAGE_SIDE, MAX_PAGE_SIDE))
    image.save(target, format="PNG")


class _Exact:
    """Reads exactly `size` bytes from `fd`: zeros pad a file that shrank, and the copy
    stops at `size` for one that grew. Read errors also become zeros, so a member that
    has started can always be finished."""

    def __init__(self, fd: int, size: int) -> None:
        self.fd, self.left, self.eof = fd, size, False

    def read(self, n: int = -1) -> bytes:
        n = self.left if n is None or n < 0 else min(n, self.left)
        data = b""
        if not self.eof and n:
            try:
                data = os.read(self.fd, n)
            except OSError:
                data = b""
            if not data:
                self.eof = True
        data += b"\0" * (n - len(data))
        self.left -= len(data)
        return data


def _add_bytes(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    member = tarfile.TarInfo(name)
    member.size = len(data)
    member.mtime = int(time.time())
    member.mode = 0o644
    tar.addfile(member, io.BytesIO(data))


def _add_file(tar: tarfile.TarFile, path: Path, name: str) -> None:
    """Add one file if it is a regular file the entry can open; skip it otherwise."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_MEMBER_BYTES:
            return
        if info.st_size > SPARSE_MIN_BYTES and info.st_blocks * 512 * SPARSE_RATIO < info.st_size:
            return  # sparse: a huge size that costs the script nothing to make
        member = tarfile.TarInfo(name)
        member.size = info.st_size
        member.mtime = int(info.st_mtime)
        member.mode = 0o644
        tar.addfile(member, _Exact(fd, info.st_size))
    except (OSError, ValueError):  # ValueError: a name ustar can't hold; that file is left out
        return
    finally:
        os.close(fd)


def _left_in_out(
    out: Path, owned: dict[str, bytes]
) -> tuple[list[tuple[Path, str]], list[str], int]:
    """(files the host takes, the first names it doesn't, how many it doesn't) among what
    the script left in `out`."""
    taken: list[tuple[Path, str]] = []
    ignored: list[str] = []
    count = 0
    try:
        for root, dirs, names in os.walk(out, followlinks=False):
            dirs.sort()
            for name in sorted(names):
                path = Path(root) / name
                rel = path.relative_to(out).as_posix()
                if rel in owned:
                    continue
                if MEMBER_NAME.fullmatch(rel):
                    taken.append((path, rel))
                else:
                    count += 1
                    if len(ignored) < MAX_IGNORED_NOTES:
                        ignored.append(rel)
    except Exception:  # never lose status.json over a strange /out
        pass
    return taken, ignored, count


def ignored_notes(ignored: list[str], count: int) -> bytes:
    """One line per file left out of the output (the first 10), then how many more."""
    lines = []
    for rel in ignored[:MAX_IGNORED_NOTES]:
        shown = "".join(ch if ch.isprintable() else "?" for ch in rel)[:120]
        lines.append(f"Ignored /out/{shown}: {IGNORED_HINT}")
    if count > len(lines):
        lines.append(f"Ignored {count - len(lines)} more files in /out: {IGNORED_HINT}")
    return "".join(line + "\n" for line in lines).encode("utf-8")


def write_tar(out: Path, stream, owned: dict[str, bytes], taken=None) -> None:
    """An uncompressed tar of `out`. The entry's own files (`owned`) come first, from
    memory; then every other regular file the host takes (no links, devices, fifos,
    unreadable or sparse files, nothing over 300 MB, only the names it knows)."""
    if taken is None:
        taken, _, _ = _left_in_out(out, owned)
    # Plain ustar, never PAX or GNU extensions: the host reads exactly this format and
    # refuses anything else.
    with tarfile.open(fileobj=stream, mode="w|", format=tarfile.USTAR_FORMAT) as tar:
        for name, data in owned.items():
            _add_bytes(tar, name, data)
        for path, rel in taken:
            try:
                _add_file(tar, path, rel)
            except Exception:  # never lose status.json over a strange /out
                pass


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
    owned: dict[str, bytes] = {}
    try:
        manifest = json.loads((job / "manifest.json").read_text(encoding="utf-8"))
        if mode == "script":
            timeout_s = int(manifest.get("script_timeout_s") or DEFAULT_TIMEOUT_S)
            status, owned["stdout.txt"] = run_script(job, out, work, timeout_s)
        elif mode == "inspect":
            run_inspect(manifest, out)
            status["exit_code"] = 0
        elif mode == "pages":
            status.update(run_pages(manifest, out))
            status["exit_code"] = 0
        else:
            raise ValueError(f"unknown mode {mode!r}")
    except Exception as exc:  # report it in the output; stdout stays a clean tar
        status["exit_code"] = 1
        message = f"sandbox error: {type(exc).__name__}: {exc}\n".encode()
        owned.setdefault("stdout.txt", message[:STDOUT_LIMIT])
    _kill_everything_else()
    if not status.get("duration_ms"):
        status["duration_ms"] = int((time.monotonic() - started) * 1000)
    taken, ignored, count = _left_in_out(out, {"status.json": b"", **owned})
    if count:  # tell the script's author, inside the printed-output limit
        notes = ignored_notes(ignored, count)
        printed = owned.get("stdout.txt", b"")
        room = STDOUT_LIMIT - len(notes)
        if len(printed) > room:
            printed = printed[:room]
            status["stdout_truncated"] = True
        owned["stdout.txt"] = printed + notes
    owned = {"status.json": json.dumps(status).encode("utf-8"), **owned}
    for name, data in owned.items():
        try:
            _write(out / name, data)
        except Exception:  # best effort: the tar carries the entry's copy either way
            pass
    write_tar(out, stream, owned, taken)
    stream.flush()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    mode = args[0] if args else ""
    return run(mode)


if __name__ == "__main__":
    sys.exit(main())

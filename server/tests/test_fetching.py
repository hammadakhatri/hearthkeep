"""Shared HTTPS fetching: the host rules, a resumable verified download, and safe unzipping.
Every test uses `httpx.MockTransport`; none touches the network."""

from __future__ import annotations

import errno
import hashlib
import io
import zipfile
from pathlib import Path

import httpx
import pytest

from hearthkeep import fetching
from hearthkeep.fetching import (
    MODEL_HOST_SUFFIXES,
    MODEL_HOSTS,
    FetchError,
    Progress,
    allowed_url,
    download_file,
    new_client,
    safe_extract_zip,
    sha256_file,
)

URL = "https://huggingface.co/org/repo/resolve/" + "a" * 40 + "/model.gguf"
BODY = bytes(range(256)) * 40  # 10 240 bytes


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def client_for(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)


def never_cancelled() -> bool:
    return False


async def run(
    handler,
    part: Path,
    *,
    body: bytes = BODY,
    size: int | None = None,
    digest: str | None = None,
    cancelled=never_cancelled,
    url: str = URL,
    progress: list | None = None,
) -> None:
    seen = progress if progress is not None else []
    async with client_for(handler) as client:
        await download_file(
            url,
            part,
            size=len(body) if size is None else size,
            sha256=sha(body) if digest is None else digest,
            client=client,
            on_progress=seen.append,
            cancelled=cancelled,
        )


# ---- allowed_url ----------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/x",
        "https://huggingface.co/x",
        "https://huggingface.co:443/x",
        "https://cas-bridge.xethub.hf.co/x",
        "https://release-assets.githubusercontent.com/x",
        "https://HUGGINGFACE.CO/x",
    ],
)
def test_allowed_url_accepts_catalog_hosts(url):
    assert allowed_url(httpx.URL(url), exact=MODEL_HOSTS, suffixes=MODEL_HOST_SUFFIXES)


@pytest.mark.parametrize(
    "url",
    [
        "http://huggingface.co/x",
        "https://evil.example/x",
        "https://hf.co/x",  # only subdomains of hf.co are listed
        "https://githubusercontent.com/x",
        "https://huggingface.co.evil.example/x",
        "https://user@huggingface.co/x",
        "https://huggingface.co:8443/x",
        "https://xhf.co/x",
        "https://huggingface.co./x",  # a trailing dot is a different name
        "https://.hf.co/x",  # a suffix match must leave a label
        "https://a..hf.co/x",
    ],
)
def test_allowed_url_refuses_everything_else(url):
    assert not allowed_url(httpx.URL(url), exact=MODEL_HOSTS, suffixes=MODEL_HOST_SUFFIXES)


def test_allowed_url_without_suffixes_is_exact_only():
    exact = frozenset({"github.com"})
    assert allowed_url(httpx.URL("https://github.com/x"), exact=exact, suffixes=())
    assert not allowed_url(httpx.URL("https://a.githubusercontent.com/x"), exact=exact, suffixes=())


def test_the_model_host_lists():
    assert MODEL_HOSTS == frozenset({"github.com", "huggingface.co"})
    assert MODEL_HOST_SUFFIXES == (".githubusercontent.com", ".hf.co")


def test_new_client_settings():
    client = new_client()
    assert client.follow_redirects is False
    assert client.trust_env is False
    assert client.timeout == httpx.Timeout(30, read=60)


# ---- download_file ----------------------------------------


async def test_download_writes_and_verifies(tmp_path):
    part = tmp_path / "dl" / "m.part"
    progress: list[Progress] = []
    await run(lambda r: httpx.Response(200, content=BODY), part, progress=progress)
    assert part.read_bytes() == BODY
    assert progress[-1] == Progress(len(BODY), len(BODY))
    assert all(p.total == len(BODY) for p in progress)


async def test_resume_sends_range_and_hashes_whole_file(tmp_path):
    part = tmp_path / "m.part"
    half = len(BODY) // 2
    part.write_bytes(BODY[:half])
    size = len(BODY)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Range"] == f"bytes={half}-"
        return httpx.Response(
            206,
            content=BODY[half:],
            headers={"Content-Range": f"bytes {half}-{size - 1}/{size}"},
        )

    progress: list[Progress] = []
    await run(handler, part, progress=progress)
    assert part.read_bytes() == BODY
    assert progress[-1] == Progress(size, size)


async def test_resume_with_a_mismatched_content_range_is_a_network_error(tmp_path):
    part = tmp_path / "m.part"
    half = len(BODY) // 2
    part.write_bytes(BODY[:half])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            206, content=BODY[half:], headers={"Content-Range": f"bytes 0-9/{len(BODY)}"}
        )

    with pytest.raises(FetchError) as info:
        await run(handler, part)
    assert info.value.reason == "network"


async def test_server_ignoring_range_restarts_cleanly(tmp_path):
    part = tmp_path / "m.part"
    part.write_bytes(BODY[: len(BODY) // 2])
    await run(lambda r: httpx.Response(200, content=BODY), part)
    assert part.read_bytes() == BODY


async def test_a_part_longer_than_size_is_deleted_and_started_over(tmp_path):
    part = tmp_path / "m.part"
    part.write_bytes(BODY + b"extra")

    def handler(request: httpx.Request) -> httpx.Response:
        assert "Range" not in request.headers
        return httpx.Response(200, content=BODY)

    await run(handler, part)
    assert part.read_bytes() == BODY


async def test_a_complete_part_is_only_verified(tmp_path):
    part = tmp_path / "m.part"
    part.write_bytes(BODY)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("nothing should be requested")

    await run(handler, part)
    assert part.read_bytes() == BODY


@pytest.mark.parametrize(
    "target",
    [
        "https://cas-bridge.xethub.hf.co/x",
        "https://us.aws.cdn.hf.co/x",
        "https://release-assets.githubusercontent.com/x",
    ],
)
async def test_redirects_to_allowed_hosts_succeed(tmp_path, target):
    part = tmp_path / "m.part"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "huggingface.co":
            return httpx.Response(302, headers={"Location": target})
        assert str(request.url) == target
        return httpx.Response(200, content=BODY)

    await run(handler, part)
    assert part.read_bytes() == BODY


@pytest.mark.parametrize(
    "target",
    [
        "http://huggingface.co/x",
        "https://evil.example/x",
        "https://huggingface.co.evil.example/x",
        "https://hf.co.evil.example/x",
        "https://user@huggingface.co/x",
        "https://huggingface.co:8443/x",
    ],
)
async def test_redirects_off_the_allow_list_are_refused(tmp_path, target):
    part = tmp_path / "m.part"
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.path == "/org/repo/resolve/" + "a" * 40 + "/model.gguf":
            return httpx.Response(302, headers={"Location": target})
        return httpx.Response(200, content=BODY)

    with pytest.raises(FetchError) as info:
        await run(handler, part)
    assert info.value.reason == "network"
    assert requested == [URL]  # the bad hop was never requested
    assert not part.exists()


async def test_a_start_url_off_the_allow_list_is_refused(tmp_path):
    part = tmp_path / "m.part"
    with pytest.raises(FetchError) as info:
        await run(lambda r: httpx.Response(200, content=BODY), part, url="https://evil.example/x")
    assert info.value.reason == "network"
    assert not part.exists()


async def test_five_redirects_are_followed_and_six_are_not(tmp_path):
    def chain(hops: int):
        def handler(request: httpx.Request) -> httpx.Response:
            n = int(request.url.params.get("n", "0"))
            if n < hops:
                return httpx.Response(
                    302, headers={"Location": f"https://huggingface.co/x?n={n + 1}"}
                )
            return httpx.Response(200, content=BODY)

        return handler

    ok = tmp_path / "ok.part"
    await run(chain(5), ok)
    assert ok.read_bytes() == BODY

    bad = tmp_path / "bad.part"
    with pytest.raises(FetchError) as info:
        await run(chain(6), bad)
    assert info.value.reason == "network"
    assert not bad.exists()


async def test_a_following_client_cannot_skip_the_hop_check(tmp_path):
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.host)
        if request.url.host == "huggingface.co":
            return httpx.Response(302, headers={"Location": "https://evil.example/x"})
        return httpx.Response(200, content=BODY)

    part = tmp_path / "m.part"
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), trust_env=False, follow_redirects=True
    )
    async with client:
        with pytest.raises(FetchError) as info:
            await download_file(
                URL,
                part,
                size=len(BODY),
                sha256=sha(BODY),
                client=client,
                on_progress=lambda p: None,
                cancelled=never_cancelled,
            )
    assert info.value.reason == "network"
    assert requested == ["huggingface.co"]
    assert not part.exists()


async def test_every_request_asks_for_identity_encoding(tmp_path):
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("Accept-Encoding"))
        if len(seen) == 1:
            return httpx.Response(302, headers={"Location": "https://cas-bridge.xethub.hf.co/x"})
        return httpx.Response(200, content=BODY)

    await run(handler, tmp_path / "m.part")
    assert seen == ["identity", "identity"]


async def test_an_error_while_hashing_is_a_network_error(tmp_path, monkeypatch):
    def broken(path):
        raise OSError(errno.EIO, "read error")

    monkeypatch.setattr(fetching, "sha256_file", broken)
    with pytest.raises(FetchError) as info:
        await run(lambda r: httpx.Response(200, content=BODY), tmp_path / "m.part")
    assert info.value.reason == "network"


async def test_a_redirect_without_a_location_is_a_network_error(tmp_path):
    with pytest.raises(FetchError) as info:
        await run(lambda r: httpx.Response(302), tmp_path / "m.part")
    assert info.value.reason == "network"


async def test_checksum_mismatch_deletes_part(tmp_path):
    part = tmp_path / "m.part"
    with pytest.raises(FetchError) as info:
        await run(lambda r: httpx.Response(200, content=BODY), part, digest=sha(b"other"))
    assert info.value.reason == "checksum"
    assert not part.exists()


async def test_too_big_stops(tmp_path):
    part = tmp_path / "m.part"
    with pytest.raises(FetchError) as info:
        await run(lambda r: httpx.Response(200, content=BODY + b"more"), part)
    assert info.value.reason == "too_big"
    assert not part.exists()


async def test_cancel_keeps_part(tmp_path, monkeypatch):
    monkeypatch.setattr(fetching, "CHUNK", 1024)
    part = tmp_path / "m.part"
    checks: list[int] = []

    def cancelled() -> bool:
        checks.append(1)
        return True  # turns true once the first chunk is in

    with pytest.raises(FetchError) as info:
        await run(lambda r: httpx.Response(200, content=BODY), part, cancelled=cancelled)
    assert info.value.reason == "cancelled"
    assert part.read_bytes() == BODY[:1024]
    assert checks == [1]


async def test_disk_full_keeps_part(tmp_path, monkeypatch):
    monkeypatch.setattr(fetching, "CHUNK", 1024)

    class FullDisk:
        """Writes the first chunk for real, then the disk fills up."""

        def __init__(self, path):
            self.f = path.open("wb")
            self.writes = 0

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.f.close()
            return False

        def write(self, data):
            if self.writes:
                raise OSError(errno.ENOSPC, "No space left on device")
            self.writes += 1
            self.f.write(data)

    part = tmp_path / "m.part"
    monkeypatch.setattr(fetching, "_open_part", lambda path, resume: FullDisk(path))
    with pytest.raises(FetchError) as info:
        await run(lambda r: httpx.Response(200, content=BODY), part)
    assert info.value.reason == "no_space"
    assert part.read_bytes() == BODY[:1024]


async def test_windows_disk_full_error_is_no_space(tmp_path, monkeypatch):
    class FullDisk:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def write(self, data):
            error = OSError(0, "There is not enough space on the disk")
            error.winerror = 112  # type: ignore[attr-defined]
            raise error

    monkeypatch.setattr(fetching, "_open_part", lambda path, resume: FullDisk())
    with pytest.raises(FetchError) as info:
        await run(lambda r: httpx.Response(200, content=BODY), tmp_path / "m.part")
    assert info.value.reason == "no_space"


async def test_404_is_not_found(tmp_path):
    part = tmp_path / "m.part"
    with pytest.raises(FetchError) as info:
        await run(lambda r: httpx.Response(404), part)
    assert info.value.reason == "not_found"
    assert not part.exists()


async def test_other_statuses_are_network_errors(tmp_path):
    with pytest.raises(FetchError) as info:
        await run(lambda r: httpx.Response(503), tmp_path / "m.part")
    assert info.value.reason == "network"


async def test_a_transport_error_is_a_network_error(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    with pytest.raises(FetchError) as info:
        await run(handler, tmp_path / "m.part")
    assert info.value.reason == "network"


async def test_progress_is_throttled_but_ends_exactly(tmp_path, monkeypatch):
    monkeypatch.setattr(fetching, "CHUNK", 512)
    monkeypatch.setattr(fetching, "_now", lambda: 100.0)  # a frozen clock
    progress: list[Progress] = []
    await run(lambda r: httpx.Response(200, content=BODY), tmp_path / "m.part", progress=progress)
    assert progress[-1] == Progress(len(BODY), len(BODY))
    assert len(progress) <= 2


def test_fetch_error_carries_reason_and_detail():
    error = FetchError("checksum", "expected a, got b")
    assert error.reason == "checksum"
    assert error.detail == "expected a, got b"


def test_sha256_file_is_lowercase_hex(tmp_path):
    f = tmp_path / "f"
    f.write_bytes(BODY)
    assert sha256_file(f) == sha(BODY)


# ---- safe_extract_zip ----------------------------------------


def make_zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            info = zipfile.ZipInfo(name)
            info.filename = name  # keep backslashes as written (ZipInfo turns os.sep into /)
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, data)
    return buf.getvalue()


def test_safe_extract_zip_caps_are_parameters(tmp_path):
    z = tmp_path / "b.zip"
    z.write_bytes(make_zip({"a": b"1", "b": b"2", "c": b"3"}))
    with pytest.raises(FetchError) as info:
        safe_extract_zip(z, tmp_path / "few", max_files=2, max_bytes=1000)
    assert info.value.reason == "unpack"
    safe_extract_zip(z, tmp_path / "ok", max_files=3, max_bytes=1000)
    assert (tmp_path / "ok" / "c").read_bytes() == b"3"

    big = tmp_path / "big.zip"
    big.write_bytes(make_zip({"x.bin": b"\0" * 2000}))
    with pytest.raises(FetchError) as info:
        safe_extract_zip(big, tmp_path / "small", max_files=10, max_bytes=1000)
    assert info.value.reason == "unpack"


@pytest.mark.parametrize("name", ["../x", "/abs", "C:/x", "a\\b"])
def test_safe_extract_zip_refuses_bad_names(tmp_path, name):
    z = tmp_path / "b.zip"
    z.write_bytes(make_zip({"ok.txt": b"ok", name: b"x"}))
    with pytest.raises(FetchError) as info:
        safe_extract_zip(z, tmp_path / "out", max_files=10, max_bytes=1000)
    assert info.value.reason == "unpack"
    assert not (tmp_path / "out" / "ok.txt").exists()


def test_safe_extract_zip_refuses_a_bad_zip(tmp_path):
    z = tmp_path / "b.zip"
    z.write_bytes(b"not a zip")
    with pytest.raises(FetchError) as info:
        safe_extract_zip(z, tmp_path / "out", max_files=10, max_bytes=1000)
    assert info.value.reason == "unpack"

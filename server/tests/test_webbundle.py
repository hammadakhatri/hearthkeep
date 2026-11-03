"""The web bundle: a release checkout uses (or downloads and verifies) the release's prebuilt
web app; `main` builds it with Node or explains that it is the developers' version."""

from __future__ import annotations

import hashlib
import io
import subprocess
import tomllib
import zipfile
import zlib
from pathlib import Path

import httpx
import pytest
from copy_rules import forbidden_in

import hearthkeep
from hearthkeep import webbundle
from hearthkeep.host import REPO_ROOT
from hearthkeep.webbundle import (
    BUILD_FAILED,
    BUILDING,
    BUNDLE_BAD_CHECKSUM,
    BUNDLE_DOWNLOAD_FAILED,
    BUNDLE_NOT_PUBLISHED,
    BUNDLE_READY,
    BUNDLE_SWAP_FAILED,
    BUNDLE_UNUSABLE,
    MAIN_IS_FOR_DEVELOPERS,
    MAX_BUNDLE_FILES,
    NO_BUNDLE_NO_REPO,
    RELEASES_PAGE_FALLBACK,
    USING_EXISTING_BUILD,
    WEB_DIR,
    BundleError,
    checksum_line,
    download,
    github_repo,
    parse_checksum,
    release_version,
    safe_extract,
    verify_dist,
    write_manifest,
)

ORIGIN = "https://github.com/example/hearthkeep.git"
BASE = "https://github.com/example/hearthkeep/releases/download/v0.1.0/"
WEB_ZIP = "hearthkeep-web-0.1.0.zip"


# ---- helpers ----------------------------------------


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            info = zipfile.ZipInfo(name)
            info.filename = name  # keep backslashes as written (ZipInfo turns os.sep into /)
            info.compress_type = zipfile.ZIP_DEFLATED
            if name.endswith("/"):
                info.external_attr = 0o40755 << 16 | 0x10
            zf.writestr(info, data)
    return buf.getvalue()


class MockClient(httpx.Client):
    """An httpx client whose transport answers from a dict of URL -> body (or a Response)."""

    def __init__(self, routes: dict):
        self.urls: list[str] = []
        self.routes = routes

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            self.urls.append(url)
            answer = self.routes.get(url)
            if answer is None:
                return httpx.Response(404)
            if isinstance(answer, httpx.Response):
                return answer
            if isinstance(answer, str):
                answer = answer.encode()
            return httpx.Response(200, content=answer)

        super().__init__(transport=httpx.MockTransport(handler), trust_env=False)


class RaisingClient(httpx.Client):
    """Any request is a test failure: proves no network was used."""

    def __init__(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError(f"unexpected request to {request.url}")

        super().__init__(transport=httpx.MockTransport(handler), trust_env=False)


def fake_git(origin: str | None):
    def run(argv, **kwargs):
        assert argv[1:] == ["-C", str(REPO_ROOT), "remote", "get-url", "origin"]
        assert kwargs["timeout"] == 10
        if origin is None:
            return subprocess.CompletedProcess(argv, 2, stdout="", stderr="no such remote")
        return subprocess.CompletedProcess(argv, 0, stdout=origin + "\n", stderr="")

    return run


def find_git_only(name):
    return Path("/usr/bin/git") if name == "git" else None


def ensure(tmp_path: Path, version: str, client, *, origin=ORIGIN, said=None):
    return webbundle.ensure_web_bundle(
        say=(said if said is not None else []).append,
        version=version,
        dist=tmp_path / "dist",
        run=fake_git(origin),
        client=client,
        find=find_git_only,
        cache=tmp_path / ".cache",
    )


def ensure_dev(tmp_path: Path, *, find, say, origin=ORIGIN, run=None):
    calls = []

    def default_run(argv, **kwargs):
        if argv[1:2] == ["-C"]:
            return fake_git(origin)(argv, **kwargs)
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0)

    return webbundle.ensure_web_bundle(
        say=say,
        version="0.2.0.dev0",
        dist=tmp_path / "dist",
        run=run or default_run,
        client=RaisingClient(),
        find=find,
        cache=tmp_path / ".cache",
    )


def built_dist(tmp_path: Path, version: str = "0.1.0") -> Path:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_bytes(b"<html>")
    (dist / "assets" / "a.js").write_bytes(b"x")
    write_manifest(dist, version)
    return dist


def served(zip_bytes: bytes, sha_text: str | None = None) -> dict:
    routes = {BASE + WEB_ZIP: zip_bytes}
    routes[BASE + WEB_ZIP + ".sha256"] = (
        sha_text if sha_text is not None else checksum_line(sha(zip_bytes), WEB_ZIP)
    )
    return routes


# ---- version ----------------------------------------


def test_version_matches_pyproject():
    pyproject = tomllib.loads((REPO_ROOT / "server" / "pyproject.toml").read_text())
    assert hearthkeep.__version__ == pyproject["project"]["version"]


@pytest.mark.parametrize(
    "v, out",
    [
        ("0.1.0", "0.1.0"),
        ("0.0.0", None),
        ("0.2.0.dev0", None),
        ("1.0", None),
        ("v1.0.0", None),
        ("0.1.0\n", None),  # `$` would match before a trailing newline
        ("０.1.0", None),  # a full-width digit is a `\d` but not [0-9]
    ],
)
def test_release_version(v, out):
    assert release_version(v) == out


def test_asset_names_and_checksum_lines():
    assert webbundle.bundle_name("0.1.0") == "hearthkeep-web-0.1.0.zip"
    assert webbundle.release_zip_name("0.1.0") == "hearthkeep-0.1.0.zip"
    line = checksum_line("a" * 64, "x.zip")
    assert line == "a" * 64 + "  x.zip\n"
    assert parse_checksum(line, "x.zip") == "a" * 64
    assert parse_checksum("a" * 64 + " *x.zip\n", "x.zip") == "a" * 64
    assert parse_checksum(line, "y.zip") is None
    assert parse_checksum("A" * 64 + "  x.zip\n", "x.zip") is None
    assert parse_checksum("a" * 63 + "  x.zip\n", "x.zip") is None
    assert parse_checksum("", "x.zip") is None


# ---- release checkouts ----------------------------------------


def test_release_checkout_with_a_verified_bundle_is_ready(tmp_path):
    built_dist(tmp_path)
    said = []
    assert ensure(tmp_path, "0.1.0", RaisingClient(), said=said) == "ready"
    assert said == []


def test_manifest_names_the_version_and_every_file(tmp_path):
    dist = built_dist(tmp_path)
    assert verify_dist(dist, "0.1.0")
    assert not verify_dist(dist, "0.1.1")
    (dist / "extra.js").write_bytes(b"y")
    assert not verify_dist(dist, "0.1.0")
    (dist / "extra.js").unlink()
    (dist / "assets" / "a.js").unlink()
    assert not verify_dist(dist, "0.1.0")


def test_missing_or_broken_manifest_is_not_verified(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_bytes(b"<html>")
    assert not verify_dist(dist, "0.1.0")
    (dist / ".bundle.json").write_text("{not json")
    assert not verify_dist(dist, "0.1.0")
    assert not verify_dist(tmp_path / "absent", "0.1.0")


def test_tampered_bundle_file_is_not_ready(tmp_path):
    dist = built_dist(tmp_path)
    (dist / "index.html").write_bytes(b"<html!")
    assert not verify_dist(dist, "0.1.0")
    zip_bytes = make_zip({"index.html": b"<html>", "assets/a.js": b"x"})
    client = MockClient(served(zip_bytes))
    assert ensure(tmp_path, "0.1.0", client) == "downloaded"
    assert (dist / "index.html").read_bytes() == b"<html>"
    assert verify_dist(dist, "0.1.0")


def test_tag_clone_downloads_and_verifies(tmp_path):
    zip_bytes = make_zip({"index.html": b"<html>", "assets/a.js": b"x"})
    client = MockClient(served(zip_bytes))
    said = []
    assert ensure(tmp_path, "0.1.0", client, said=said) == "downloaded"
    assert verify_dist(tmp_path / "dist", "0.1.0")
    assert client.urls[0].startswith(
        "https://github.com/example/hearthkeep/releases/download/v0.1.0/"
    )
    assert said[-1] == BUNDLE_READY
    # Nothing half-made is left beside the bundle, and the downloads are cleaned up.
    assert sorted(p.name for p in tmp_path.iterdir()) == [".cache", "dist"]
    assert list((tmp_path / ".cache").iterdir()) == []


def test_tag_clone_without_a_github_origin_explains(tmp_path):
    said = []
    assert ensure(tmp_path, "0.1.0", RaisingClient(), origin=None, said=said) == "failed"
    assert said == [NO_BUNDLE_NO_REPO.format(v="0.1.0")]
    said.clear()
    gitlab = "https://gitlab.com/example/hearthkeep.git"
    assert ensure(tmp_path, "0.1.0", RaisingClient(), origin=gitlab, said=said) == "failed"
    assert said == [NO_BUNDLE_NO_REPO.format(v="0.1.0")]


def test_bad_checksum_is_refused_and_dist_untouched(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_bytes(b"old")
    zip_bytes = make_zip({"index.html": b"<html>"})
    client = MockClient(served(zip_bytes, checksum_line("0" * 64, WEB_ZIP)))
    said = []
    assert ensure(tmp_path, "0.1.0", client, said=said) == "failed"
    assert said == [BUNDLE_BAD_CHECKSUM]
    assert (dist / "index.html").read_bytes() == b"old"
    assert list((tmp_path / ".cache").iterdir()) == []
    assert not (tmp_path / "dist.new").exists()


def test_missing_checksum_is_refused(tmp_path):
    zip_bytes = make_zip({"index.html": b"<html>"})
    routes = served(zip_bytes)
    del routes[BASE + WEB_ZIP + ".sha256"]
    said = []
    assert ensure(tmp_path, "0.1.0", MockClient(routes), said=said) == "failed"
    assert said == [BUNDLE_BAD_CHECKSUM]
    assert not (tmp_path / "dist").exists()
    assert list((tmp_path / ".cache").iterdir()) == []


def test_checksum_for_another_file_is_refused(tmp_path):
    zip_bytes = make_zip({"index.html": b"<html>"})
    other = checksum_line(sha(zip_bytes), "hearthkeep-web-0.0.9.zip")
    said = []
    assert ensure(tmp_path, "0.1.0", MockClient(served(zip_bytes, other)), said=said) == "failed"
    assert said == [BUNDLE_BAD_CHECKSUM]


def test_network_error_says_download_failed(tmp_path):
    def handler(request):
        raise httpx.ConnectError("offline", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
    said = []
    assert ensure(tmp_path, "0.1.0", client, said=said) == "failed"
    assert said == [BUNDLE_DOWNLOAD_FAILED]


def test_missing_release_asset_says_not_published_yet(tmp_path):
    said = []
    assert ensure(tmp_path, "0.1.0", MockClient({}), said=said) == "failed"
    assert said == [BUNDLE_NOT_PUBLISHED]


def test_server_error_for_the_asset_says_download_failed(tmp_path):
    routes = {BASE + WEB_ZIP: httpx.Response(503)}
    said = []
    assert ensure(tmp_path, "0.1.0", MockClient(routes), said=said) == "failed"
    assert said == [BUNDLE_DOWNLOAD_FAILED]


def test_a_failed_swap_says_so_and_keeps_the_old_app(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_bytes(b"old")

    def refuse(new, dist):
        raise PermissionError("in use")

    monkeypatch.setattr(webbundle, "_swap_in", refuse)
    zip_bytes = make_zip({"index.html": b"<html>"})
    said = []
    assert ensure(tmp_path, "0.1.0", MockClient(served(zip_bytes)), said=said) == "failed"
    assert said == [BUNDLE_SWAP_FAILED]
    assert (dist / "index.html").read_bytes() == b"old"
    assert not (tmp_path / "dist.new").exists()


def test_an_interrupted_swap_is_put_back_first(tmp_path):
    built_dist(tmp_path).rename(tmp_path / "dist.old")  # a swap stopped between its renames
    said = []
    assert ensure(tmp_path, "0.1.0", RaisingClient(), said=said) == "ready"
    assert verify_dist(tmp_path / "dist", "0.1.0")
    assert not (tmp_path / "dist.old").exists()


def test_http_redirect_is_refused(tmp_path):
    zip_bytes = make_zip({"index.html": b"<html>"})
    routes = served(zip_bytes)
    routes[BASE + WEB_ZIP] = httpx.Response(302, headers={"location": "http://example.com/b.zip"})
    client = MockClient(routes)
    said = []
    assert ensure(tmp_path, "0.1.0", client, said=said) == "failed"
    assert said == [BUNDLE_DOWNLOAD_FAILED]
    assert not any(u.startswith("http://") for u in client.urls)
    assert not (tmp_path / "dist").exists()


def test_redirect_to_github_asset_host_is_followed(tmp_path):
    zip_bytes = make_zip({"index.html": b"<html>"})
    asset = "https://release-assets.githubusercontent.com/x/web.zip"
    routes = served(zip_bytes)
    routes[BASE + WEB_ZIP] = httpx.Response(302, headers={"location": asset})
    routes[asset] = zip_bytes
    assert ensure(tmp_path, "0.1.0", MockClient(routes)) == "downloaded"


def test_redirect_to_another_https_host_is_refused(tmp_path):
    routes = served(make_zip({"index.html": b"<html>"}))
    elsewhere = "https://example.com/web.zip"
    routes[BASE + WEB_ZIP] = httpx.Response(302, headers={"location": elsewhere})
    routes[elsewhere] = make_zip({"index.html": b"evil"})
    client = MockClient(routes)
    assert ensure(tmp_path, "0.1.0", client) == "failed"
    assert elsewhere not in client.urls


def test_download_refuses_plain_http_and_other_hosts(tmp_path):
    client = RaisingClient()
    for url in ("http://github.com/a/b", "https://example.com/a", "https://github.com:8443/a"):
        with pytest.raises(BundleError):
            download(url, tmp_path / "f", client=client)


def test_download_stops_at_the_limit(tmp_path):
    client = MockClient({"https://github.com/a/big": b"x" * 2000})
    with pytest.raises(BundleError):
        download("https://github.com/a/big", tmp_path / "f", client=client, limit=1000)
    assert list(tmp_path.iterdir()) == []


def test_download_gives_up_on_a_redirect_loop(tmp_path):
    url = "https://github.com/a/loop"
    client = MockClient({url: httpx.Response(302, headers={"location": url})})
    with pytest.raises(BundleError):
        download(url, tmp_path / "f", client=client)


def test_bundle_that_will_not_unpack_leaves_dist_alone(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_bytes(b"old")
    zip_bytes = make_zip({"../evil.html": b"x"})
    said = []
    assert ensure(tmp_path, "0.1.0", MockClient(served(zip_bytes)), said=said) == "failed"
    assert said == [BUNDLE_UNUSABLE]
    assert (dist / "index.html").read_bytes() == b"old"
    assert not (tmp_path / "evil.html").exists()
    assert not (tmp_path / "dist.new").exists()


def test_own_client_ignores_proxies_and_does_not_follow_redirects(tmp_path, monkeypatch):
    made = []
    real = httpx.Client

    class Spy(real):
        def __init__(self, **kwargs):
            made.append(kwargs)
            super().__init__(**kwargs, transport=httpx.MockTransport(lambda r: httpx.Response(404)))

    monkeypatch.setattr(webbundle.httpx, "Client", Spy)
    assert ensure(tmp_path, "0.1.0", None) == "failed"
    assert made and made[0]["trust_env"] is False
    assert made[0].get("follow_redirects", False) is False


# ---- safe extraction ----------------------------------------


@pytest.mark.parametrize("name", ["../x", "/abs", "C:/x", "a\\b", "a/../../x", "c:x", "a:b"])
def test_safe_extract_refuses(tmp_path, name):
    z = tmp_path / "b.zip"
    z.write_bytes(make_zip({"index.html": b"ok", name: b"x"}))
    with pytest.raises(BundleError):
        safe_extract(z, tmp_path / "out")
    assert not (tmp_path / "out" / "index.html").exists()


def test_safe_extract_refuses_symlinks(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        info = zipfile.ZipInfo("link")
        info.create_system = 3
        info.external_attr = (0o120777) << 16
        zf.writestr(info, "/etc/passwd")
    z = tmp_path / "b.zip"
    z.write_bytes(buf.getvalue())
    with pytest.raises(BundleError):
        safe_extract(z, tmp_path / "out")


def test_safe_extract_refuses_a_huge_entry(tmp_path):
    z = tmp_path / "b.zip"
    z.write_bytes(make_zip({"big.bin": b"\0" * 60_000_000}))
    with pytest.raises(BundleError):
        safe_extract(z, tmp_path / "out")


def test_safe_extract_refuses_too_many_entries(tmp_path):
    z = tmp_path / "b.zip"
    z.write_bytes(make_zip({f"f{i}.txt": b"" for i in range(MAX_BUNDLE_FILES + 1)}))
    with pytest.raises(BundleError):
        safe_extract(z, tmp_path / "out")


@pytest.mark.parametrize(
    "error",
    [
        zlib.error("invalid stored block lengths"),
        RuntimeError("encrypted"),
        NotImplementedError("compression type 99"),
        ValueError("bad entry"),
    ],
)
def test_safe_extract_turns_unpacking_errors_into_bundle_errors(tmp_path, monkeypatch, error):
    z = tmp_path / "b.zip"
    z.write_bytes(make_zip({"index.html": b"<html>"}))

    def broken(self, *args, **kwargs):
        raise error

    monkeypatch.setattr(zipfile.ZipFile, "open", broken)
    with pytest.raises(BundleError):
        safe_extract(z, tmp_path / "out")


def test_safe_extract_refuses_corrupt_compressed_data(tmp_path):
    data = bytearray(make_zip({"index.html": b"<html>" * 200}))
    start = data.index(b"index.html") + len(b"index.html")  # the local header's name ends here
    for i in range(start, start + 20):
        data[i] ^= 0xFF
    z = tmp_path / "b.zip"
    z.write_bytes(bytes(data))
    with pytest.raises(BundleError):
        safe_extract(z, tmp_path / "out")


def test_safe_extract_refuses_a_bad_zip(tmp_path):
    z = tmp_path / "b.zip"
    z.write_bytes(b"not a zip")
    with pytest.raises(BundleError):
        safe_extract(z, tmp_path / "out")


def test_safe_extract_writes_the_files(tmp_path):
    z = tmp_path / "b.zip"
    z.write_bytes(make_zip({"index.html": b"<html>", "assets/": b"", "assets/a.js": b"x"}))
    safe_extract(z, tmp_path / "out")
    assert (tmp_path / "out" / "index.html").read_bytes() == b"<html>"
    assert (tmp_path / "out" / "assets" / "a.js").read_bytes() == b"x"


# ---- main: developers' version ----------------------------------------


def test_main_with_node_builds(tmp_path):
    calls = []

    def run(argv, **kwargs):
        if argv[1:2] == ["-C"]:
            return fake_git(ORIGIN)(argv, **kwargs)
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0)

    npm = Path("/opt/node/npm")
    said = []
    outcome = ensure_dev(
        tmp_path, find=lambda name: npm if name == "npm" else None, say=said.append, run=run
    )
    assert outcome == "built"
    assert [c[0] for c in calls] == [[str(npm), "ci"], [str(npm), "run", "build"]]
    assert all(c[1]["cwd"] == WEB_DIR for c in calls)
    assert said == [BUILDING]


def test_main_prefers_npm_cmd(tmp_path):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    def find(name):
        return {"npm.cmd": Path("/n/npm.cmd"), "npm": Path("/n/npm")}.get(name)

    assert ensure_dev(tmp_path, find=find, say=[].append, run=run) == "built"
    assert calls[0][0] == str(Path("/n/npm.cmd"))


def test_main_build_failure_is_reported(tmp_path):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1)

    said = []
    outcome = ensure_dev(tmp_path, find=lambda name: Path("/n/npm"), say=said.append, run=run)
    assert outcome == "failed"
    assert said == [BUILDING, BUILD_FAILED]
    assert len(calls) == 1  # no build after a failed install


def test_main_without_node_uses_existing_build_or_explains(tmp_path):
    said = []
    assert ensure_dev(tmp_path, find=lambda *a: None, say=said.append, origin=None) == "failed"
    assert said[0] == MAIN_IS_FOR_DEVELOPERS and "Releases page" in said[1]
    assert said[1] == RELEASES_PAGE_FALLBACK
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "index.html").write_text("")
    said.clear()
    assert ensure_dev(tmp_path, find=lambda *a: None, say=said.append, origin=None) == "existing"
    assert said == [USING_EXISTING_BUILD]


def test_main_without_node_links_the_latest_release(tmp_path):
    said = []
    find = find_git_only
    assert ensure_dev(tmp_path, find=find, say=said.append) == "failed"
    assert said == [MAIN_IS_FOR_DEVELOPERS, "https://github.com/example/hearthkeep/releases/latest"]


# ---- origin parsing ----------------------------------------


@pytest.mark.parametrize(
    "origin, repo",
    [
        ("https://github.com/example/hearthkeep.git", ("example", "hearthkeep")),
        ("https://github.com/example/hearthkeep", ("example", "hearthkeep")),
        ("git@github.com:ex-ample/hearth_keep.git", ("ex-ample", "hearth_keep")),
        ("git@github.com:example/hearth.keep", ("example", "hearth.keep")),
        ("https://gitlab.com/example/hearthkeep.git", None),
        ("http://github.com/example/hearthkeep.git", None),
        ("https://github.com.evil.example/example/hearthkeep.git", None),
        ("https://github.com/exa mple/hearthkeep.git", None),
        ("https://github.com/../hearthkeep.git", None),
        ("https://github.com/example/../x", None),
        ("https://github.com/example/hearthkeep/extra", None),
        ("https://user@github.com/example/hearthkeep.git", None),
        ("", None),
    ],
)
def test_github_repo_parses_origin(origin, repo):
    assert github_repo(run=fake_git(origin), find=find_git_only) == repo


def test_github_repo_without_git_or_origin_is_none():
    assert github_repo(run=fake_git(None), find=find_git_only) is None
    assert github_repo(run=fake_git(ORIGIN), find=lambda name: None) is None

    def boom(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 10)

    assert github_repo(run=boom, find=find_git_only) is None


# ---- copy ----------------------------------------


def test_bundle_copy_is_ascii_and_has_no_commands():
    copy = [
        BUNDLE_READY,
        BUNDLE_DOWNLOAD_FAILED,
        BUNDLE_BAD_CHECKSUM,
        BUNDLE_UNUSABLE,
        BUNDLE_SWAP_FAILED,
        BUNDLE_NOT_PUBLISHED,
        NO_BUNDLE_NO_REPO.format(v="0.1.0"),
        BUILDING,
        BUILD_FAILED,
        USING_EXISTING_BUILD,
        MAIN_IS_FOR_DEVELOPERS,
        RELEASES_PAGE_FALLBACK,
    ]
    for text in copy:
        assert text.isascii(), text
        assert forbidden_in(text) == [], text
        assert "{" not in text and "<" not in text, text

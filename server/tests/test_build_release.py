"""scripts/build_release.py: the two release zips and their checksums."""

from __future__ import annotations

import hashlib
import importlib.util
import zipfile
from pathlib import Path

from hearthkeep.webbundle import parse_checksum, safe_extract, verify_dist

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "build_release.py"


def load_script():
    spec = importlib.util.spec_from_file_location("hk_build_release_for_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build_release = load_script()


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fake_dist(tmp_path: Path) -> Path:
    dist = tmp_path / "web" / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_bytes(b"<html>")
    (dist / "assets" / "a.js").write_bytes(b"x")
    return dist


def fake_git_archive(prefix: str, out: Path) -> None:
    # Like git archive: fixed entry times (the commit's), so the output depends only on HEAD.
    with zipfile.ZipFile(out, "w") as zf:
        for name, text in (("README.md", "# Hearthkeep\n"), ("Setup.cmd", "@echo off\r\n")):
            zf.writestr(zipfile.ZipInfo(prefix + name, (2026, 11, 4, 0, 0, 0)), text)


def build(tmp_path: Path, out: Path, *, version: str = "0.1.0", argv_version: str = "0.1.0"):
    return build_release.main(
        ["--version", argv_version, "--out", str(out)],
        git_archive=fake_git_archive,
        version=version,
        dist=tmp_path / "web" / "dist",
    )


def test_build_release_makes_both_zips_and_checksums(tmp_path):
    fake_dist(tmp_path)
    out = tmp_path / "out"
    assert build(tmp_path, out) == 0
    names = sorted(p.name for p in out.iterdir())
    assert names == [
        "hearthkeep-0.1.0.zip",
        "hearthkeep-0.1.0.zip.sha256",
        "hearthkeep-web-0.1.0.zip",
        "hearthkeep-web-0.1.0.zip.sha256",
    ]
    for name in ("hearthkeep-web-0.1.0.zip", "hearthkeep-0.1.0.zip"):
        assert parse_checksum((out / f"{name}.sha256").read_text(), name) == sha256_of(out / name)
    release = zipfile.ZipFile(out / "hearthkeep-0.1.0.zip")
    assert "hearthkeep-0.1.0/web/dist/.bundle.json" in release.namelist()
    assert "hearthkeep-0.1.0/web/dist/assets/a.js" in release.namelist()
    assert "hearthkeep-0.1.0/README.md" in release.namelist()
    safe_extract(out / "hearthkeep-web-0.1.0.zip", tmp_path / "x")
    assert verify_dist(tmp_path / "x", "0.1.0")
    # The full zip unpacks to a folder whose web app verifies too.
    safe_extract_all = tmp_path / "full"
    release.extractall(safe_extract_all)
    assert verify_dist(safe_extract_all / "hearthkeep-0.1.0" / "web" / "dist", "0.1.0")


def test_web_zip_entries_are_sorted_with_fixed_times_and_modes(tmp_path):
    fake_dist(tmp_path)
    out = tmp_path / "out"
    assert build(tmp_path, out) == 0
    infos = zipfile.ZipFile(out / "hearthkeep-web-0.1.0.zip").infolist()
    assert [i.filename for i in infos] == [".bundle.json", "assets/a.js", "index.html"]
    for info in infos:
        assert info.date_time == (1980, 1, 1, 0, 0, 0)
        assert (info.external_attr >> 16) & 0o777 == 0o644


def test_build_release_refuses_a_mismatched_version(tmp_path):
    fake_dist(tmp_path)
    out = tmp_path / "out"
    assert build(tmp_path, out, argv_version="0.2.0", version="0.1.0") == 2
    assert not out.exists() or list(out.iterdir()) == []


def test_build_release_refuses_a_development_version(tmp_path):
    fake_dist(tmp_path)
    for v in ("0.0.0", "0.2.0.dev0"):
        assert build(tmp_path, tmp_path / "out", argv_version=v, version=v) == 2


def test_build_release_refuses_without_a_built_web_app(tmp_path):
    assert build(tmp_path, tmp_path / "out") == 2


def test_web_zip_is_reproducible(tmp_path):
    fake_dist(tmp_path)
    assert build(tmp_path, tmp_path / "a") == 0
    assert build(tmp_path, tmp_path / "b") == 0
    for name in ("hearthkeep-web-0.1.0.zip", "hearthkeep-0.1.0.zip"):
        assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes()


# ---- the release workflow ----------------------------------------

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "release.yml"


def _jobs(text: str) -> dict[str, str]:
    """Each job's lines under `jobs:`, by name (two-space indented keys)."""
    body = text.split("\njobs:\n", 1)[1]
    jobs: dict[str, list[str]] = {}
    name = None
    for line in body.splitlines():
        if line.startswith("  ") and not line.startswith("   ") and line.rstrip().endswith(":"):
            name = line.strip()[:-1]
            jobs[name] = []
        elif name is not None:
            jobs[name].append(line)
    return {k: "\n".join(v) for k, v in jobs.items()}


def test_release_workflow_builds_read_only_and_publishes_in_a_separate_job():
    import re

    text = WORKFLOW.read_text(encoding="utf-8")
    jobs = _jobs(text)
    assert set(jobs) == {"build", "release"}
    build, release = jobs["build"], jobs["release"]
    # Everything that runs the repo's code or npm packages has a read-only token.
    assert "contents: read" in build and "contents: write" not in build
    for step in ("npm ci --ignore-scripts", "npm test", "npm run build", "uv run --locked pytest"):
        assert step in build, step
    assert "scripts/build_release.py" in build and "actions/upload-artifact@" in build
    assert "persist-credentials: false" in build
    # The job that can write only downloads the assets and makes a draft release.
    assert "needs: build" in release and "contents: write" in release
    assert "actions/download-artifact@" in release
    assert "gh release create" in release and "--draft" in release
    # The assets are the two zips of this tag's version, checked against their .sha256 first.
    assert 'v="${GITHUB_REF_NAME#v}"' in release
    assert '"hearthkeep-$v.zip" "hearthkeep-web-$v.zip"' in release
    assert release.index("sha256sum -c") < release.index("gh release create")
    assert '--repo "$REPO"' in release and "REPO: ${{ github.repository }}" in release
    for banned in ("checkout", "npm", "uv run", "setup-node", "setup-uv", "build_release"):
        assert banned not in release, banned
    # Every action is pinned to a full commit SHA, as in the other workflows.
    uses = re.findall(r"uses: (\S+)", text)
    assert uses and all(re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", u) for u in uses), uses

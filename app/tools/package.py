"""python -m app.tools.package

Builds the `nexora` wheel from `pyproject.toml`, versioned `0.<git commit count>.0+<short
sha>` (or `0.0.0+nogit` outside a git repository), copies `config/*.yaml` into
`build/config/`, and writes `build/manifest.json` with the wheel name, the wheel hash
and the config file hashes. Deterministic: rerunning with nothing changed produces the
same wheel hash (SOURCE_DATE_EPOCH pins the zip entry timestamps).
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = PROJECT_ROOT / "pyproject.toml"
DIST_DIR = PROJECT_ROOT / "dist"
BUILD_DIR = PROJECT_ROOT / "build"
CONFIG_DIR = PROJECT_ROOT / "config"


class PackageError(Exception):
    """Packaging failed for a reason that is already readable."""


def _git(*args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    if result.returncode != 0:
        return None

    return result.stdout.strip()


def compute_version() -> str:
    """0.<commit count>.0+<short sha>, or 0.0.0+nogit outside a git repository."""
    count = _git("rev-list", "--count", "HEAD")
    sha = _git("rev-parse", "--short", "HEAD")

    if count and sha:
        return f"0.{count}.0+{sha}"

    return "0.0.0+nogit"


_ZIP_EPOCH_FLOOR = "315532800"  # 1980-01-01T00:00:00Z: the earliest timestamp a ZIP (and so a wheel) can hold


def _source_date_epoch() -> str:
    timestamp = _git("show", "-s", "--format=%ct", "HEAD")
    return timestamp if timestamp else _ZIP_EPOCH_FLOOR


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return f"sha256:{digest.hexdigest()}"


def _set_pyproject_version(version: str) -> str:
    """Rewrite the `version = ...` line in pyproject.toml; returns the original text
    so the caller can restore it."""
    original = PYPROJECT.read_text(encoding="utf-8")
    lines = original.splitlines(keepends=True)

    for index, line in enumerate(lines):
        if line.strip().startswith("version ="):
            lines[index] = f'version = "{version}"\n'
            break
    else:
        raise PackageError(f"{PYPROJECT} has no version = line to rewrite")

    PYPROJECT.write_text("".join(lines), encoding="utf-8")
    return original


def build_wheel(version: str) -> Path:
    if DIST_DIR.exists():
        shutil.rmtree(DIST_DIR)
    DIST_DIR.mkdir(parents=True, exist_ok=True)

    original = _set_pyproject_version(version)
    env = {**os.environ, "SOURCE_DATE_EPOCH": _source_date_epoch(), "PYTHONHASHSEED": "0"}

    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "wheel", ".", "--no-deps", "-w", str(DIST_DIR)],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
    finally:
        PYPROJECT.write_text(original, encoding="utf-8")

    if result.returncode != 0:
        sys.stderr.write(result.stdout)
        sys.stderr.write(result.stderr)
        raise PackageError("pip wheel failed; see the output above")

    wheels = sorted(DIST_DIR.glob("nexora-*.whl"))
    if not wheels:
        raise PackageError("pip wheel produced no nexora-*.whl")

    return wheels[0]


def copy_config(dest: Path) -> list[Path]:
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)

    copied = []
    for source in sorted(CONFIG_DIR.glob("*.yaml")):
        target = dest / source.name
        shutil.copy2(source, target)
        copied.append(target)

    return copied


def write_manifest(wheel: Path, config_files: list[Path]) -> Path:
    manifest = {
        "wheel": wheel.name,
        "wheel_hash": _sha256(wheel),
        "config": {f.name: _sha256(f) for f in config_files},
    }

    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    manifest_path = BUILD_DIR / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    return manifest_path


def package() -> dict:
    """Build the wheel and manifest; returns `{"version", "wheel", "config_files", "manifest"}`."""
    version = compute_version()
    wheel = build_wheel(version)
    config_files = copy_config(BUILD_DIR / "config")
    manifest_path = write_manifest(wheel, config_files)

    return {
        "version": version,
        "wheel": wheel,
        "config_files": config_files,
        "manifest": manifest_path,
    }


def main() -> int:
    try:
        result = package()
    except PackageError as error:
        print(error, file=sys.stderr)
        return 1

    print(f"Built {result['wheel'].name} (version {result['version']})")
    print(f"Copied {len(result['config_files'])} config file(s) to {BUILD_DIR / 'config'}")
    print(f"Wrote {result['manifest']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

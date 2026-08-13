"""Shared plumbing for the cdp-harness scripts.

Paths, az invocation (with the /opt/homebrew/bin PATH fix), atomic JSON IO,
timestamps, git commits, and company-directory helpers. Everything here is
stdlib-only. See CLAUDE.md for the schemas and conventions.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_COMPANIES_ROOT = REPO_ROOT / "companies"
SUBSCRIPTION = "m1 corpus"

# az is at /opt/homebrew/bin/az, which is not on the default non-interactive PATH.
AZ_PATH_PREFIX = "/opt/homebrew/bin"


class HarnessError(Exception):
    """Failure with a human-readable reason (becomes outcome reason)."""


def az_env() -> dict:
    env = dict(os.environ)
    env["PATH"] = AZ_PATH_PREFIX + os.pathsep + env.get("PATH", "")
    return env


def run_az(args: list[str], dry_run: bool = False, timeout: int = 300,
           check: bool = True) -> subprocess.CompletedProcess:
    """Run an az command. In dry-run mode, print it and return a fake success."""
    cmd = ["az"] + args
    if dry_run:
        print("DRY-RUN: " + " ".join(_shell_quote(a) for a in cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    proc = subprocess.run(cmd, capture_output=True, text=True, env=az_env(),
                          timeout=timeout)
    if check and proc.returncode != 0:
        raise HarnessError(
            f"az {' '.join(args[:3])}... failed (rc={proc.returncode}): "
            f"{proc.stderr.strip()[:400]}")
    return proc


def az_json(args: list[str], dry_run: bool = False, timeout: int = 300):
    """Run az with -o json and parse. Returns None in dry-run or on empty output."""
    proc = run_az(args + ["-o", "json"], dry_run=dry_run, timeout=timeout)
    if dry_run or not proc.stdout.strip():
        return None
    return json.loads(proc.stdout)


def _shell_quote(s: str) -> str:
    if not s or any(c in s for c in " \"'$`\\\n*?[]{}();<>|&"):
        return "'" + s.replace("'", "'\\''") + "'"
    return s


def ensure_subscription(dry_run: bool = False) -> None:
    run_az(["account", "set", "-s", SUBSCRIPTION], dry_run=dry_run)


# ── time ─────────────────────────────────────────────────────────────────────

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_now() -> str:
    return iso(utc_now())


def ts_basic(dt: datetime | None = None) -> str:
    """Filename-safe ISO-8601 basic form: 20260813T140000Z."""
    return (dt or utc_now()).strftime("%Y%m%dT%H%M%SZ")


def parse_iso(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


# ── JSON IO ──────────────────────────────────────────────────────────────────

def read_json(path: Path):
    with open(path) as f:
        return json.load(f)


def write_json(path: Path, data) -> None:
    """Atomic write: temp file + rename, so a crash never leaves half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, sort_keys=False)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# ── companies ────────────────────────────────────────────────────────────────

def company_dir(root: Path, slug: str) -> Path:
    return root / slug


def list_companies(root: Path) -> list[str]:
    """Slugs of onboarded companies (dirs containing config.json)."""
    if not root.is_dir():
        return []
    return sorted(d.name for d in root.iterdir()
                  if d.is_dir() and not d.name.startswith(".")
                  and (d / "config.json").exists())


def load_config(root: Path, slug: str) -> dict:
    return read_json(company_dir(root, slug) / "config.json")


def load_expected(root: Path, slug: str) -> dict | None:
    p = company_dir(root, slug) / "expected-data-sizes.json"
    return read_json(p) if p.exists() else None


def load_status(root: Path, slug: str) -> dict:
    p = company_dir(root, slug) / "status.json"
    if p.exists():
        return read_json(p)
    return {"slug": slug, "stage": "pushing",
            "last_run": {"timestamp": None, "outcome": None, "reason": None},
            "last_change_detected_at": None}


def save_status(root: Path, slug: str, status: dict) -> None:
    write_json(company_dir(root, slug) / "status.json", status)


def sizing_runs(root: Path, slug: str) -> list[Path]:
    """Run files, oldest → newest (filenames sort chronologically)."""
    d = company_dir(root, slug) / "sizing-runs"
    if not d.is_dir():
        return []
    return sorted(p for p in d.glob("*.json") if not p.name.startswith("."))


def latest_runs(root: Path, slug: str, n: int = 2) -> list[dict]:
    """Newest-first list of the last n parsed run files."""
    return [read_json(p) for p in reversed(sizing_runs(root, slug)[-n:])]


# ── git ──────────────────────────────────────────────────────────────────────

def git_commit(paths: list[Path], message: str, root: Path) -> bool:
    """Commit paths, best-effort. Disabled (returns False) when operating on a
    non-default companies root (fixtures) so tests never touch git."""
    if root != DEFAULT_COMPANIES_ROOT:
        return False
    try:
        rels = [str(p) for p in paths]
        subprocess.run(["git", "add", "--"] + rels, cwd=REPO_ROOT, check=True,
                       capture_output=True, text=True)
        proc = subprocess.run(["git", "commit", "-m", message, "--"] + rels,
                              cwd=REPO_ROOT, capture_output=True, text=True)
        return proc.returncode == 0  # nonzero = nothing to commit; fine
    except Exception as exc:  # noqa: BLE001 — commit failure must not kill a run
        print(f"WARNING: git commit failed: {exc}", file=sys.stderr)
        return False


# ── presentation units (decimal — ÷1e9; see CLAUDE.md units convention) ──────

def human_bytes(n) -> str:
    """Headline convention: TB when ≥1 TB, else GB, else MB/KB/B. Decimal."""
    if n is None:
        return "—"
    n = float(n)
    for unit, div in (("TB", 1e12), ("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if n >= div:
            return f"{n / div:.2f} {unit}"
    return f"{int(n)} B"


def gb(n) -> float:
    return float(n) / 1e9

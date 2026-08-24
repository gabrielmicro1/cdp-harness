#!/usr/bin/env python3
"""VM-side GitHub puller: mirror clones + wikis + issue/PR JSONL -> azcopy.

Harness-idiom rewrite of cdp-platform-backend/scripts/github_pull.py
@ dc320d1 (2026-08-06). The proven mechanisms are preserved verbatim —
GIT_ASKPASS token custody, mirror-clone with retry + .cdp-complete resume
markers, the 4 paginated JSONL exports, the rate-limit/403 failure
taxonomy. What changed for this harness: wikis are pulled, LFS objects are
fetched automatically when a repo uses them, and the upload leg is
stage-all + `azcopy --overwrite=false` that runs EVEN when repos failed
(upload-what-succeeded; failed repos are mopped up by a re-run and
verify is the completeness authority — the upstream skipped the upload
entirely on any failure).

Runs on the transfer VM inside tmux, launched by scripts/github_transfer.py
with ~/.config/xfer/{github.env,dest.env} sourced:
  GITHUB_TOKEN     — fine-grained PAT (Contents/Issues/Pull requests read)
  AZURE_DEST_URL   — https://ACCT.blob.core.windows.net/<container>/<prefix>
  AZURE_DEST_SAS   — racwl container SAS

The token never appears in a clone URL, argv, or any log line: git
authenticates through a 0700 GIT_ASKPASS helper that reads it from this
process's environment only.

Stdlib-only (git + git-lfs + azcopy on PATH — bootstrap-vm.sh installs
them). Import-safe on the laptop so the pure functions (wiki_absent,
build_manifest) are unit-testable offline; nothing runs at import time.

Per repo:
  1. `git clone --mirror`         -> <dest>/repos/<name>.git/
  2. wiki clone (when enabled)    -> <dest>/wikis/<name>.wiki.git/
  3. `git lfs fetch --all` when .gitattributes declares filter=lfs
  4. issues / pulls / issue_comments / review_comments (?state=all,
     100/page)                    -> <dest>/json/<name>/<kind>.jsonl
Then manifest.json is written and the whole dest dir azcopies to the
container (manifest rides the same job, so verify can read it from blob).

Exit codes: 0 = complete success, 1 = fatal setup error (bad token/scopes,
no git), 2 = finished but one or more repos failed (see manifest.json).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = "https://api.github.com"
CLONE_RETRIES = 3
API_RETRIES = 4
PER_PAGE = 100

# The 4 metadata exports. issues/pulls need ?state=all (default is open
# only); the comment endpoints are repo-wide and take no state param.
JSON_ENDPOINTS = {
    "issues":          "/repos/{login}/{name}/issues?state=all",
    "pulls":           "/repos/{login}/{name}/pulls?state=all",
    "issue_comments":  "/repos/{login}/{name}/issues/comments",
    "review_comments": "/repos/{login}/{name}/pulls/comments",
}


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] {msg}", flush=True)


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1000.0
    return f"{n} B"


def write_progress(dest: Path, phase: str, done: int, total: int,
                   message: str) -> None:
    """Heartbeat github_transfer.py's status subcommand reads. Never fatal."""
    try:
        (dest / "progress.json").write_text(json.dumps(
            {"phase": phase, "done": done, "total": total,
             "message": message}))
    except OSError:
        pass


def dir_size(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file() and not p.is_symlink():
                total += p.stat().st_size
        except OSError:
            pass
    return total


# ── GitHub REST client (urllib; pagination; the §2.5 failure taxonomy) ───────

class GitHub:
    def __init__(self, token: str):
        self.token = token

    def _request(self, url: str):
        """One GET -> (parsed json, Link header). Sleeps through rate
        limits; a 403 with the rate limit INTACT is a scope problem and
        fatal — retrying cannot help."""
        last_err = None
        for attempt in range(1, API_RETRIES + 1):
            req = urllib.request.Request(url, headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "cdp-harness-github-transfer/1.0",
            })
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    return json.loads(resp.read()), resp.headers.get("Link", "")
            except urllib.error.HTTPError as e:
                last_err = e
                if e.code in (403, 429):
                    remaining = e.headers.get("X-RateLimit-Remaining")
                    reset = e.headers.get("X-RateLimit-Reset")
                    retry_after = e.headers.get("Retry-After")
                    if retry_after:
                        wait = int(retry_after) + 1
                    elif remaining == "0" and reset:
                        wait = max(int(reset) - int(time.time()), 0) + 5
                    else:
                        raise SystemExit(
                            f"FATAL: HTTP 403 on {url} with the rate limit "
                            "intact — an auth/scope problem, not throttling. "
                            "The PAT is likely missing Contents/Issues/"
                            "Pull requests: read, or the org has not "
                            "approved it.")
                    log(f"rate-limited; sleeping {min(wait, 3900)}s")
                    time.sleep(min(wait, 3900))
                elif e.code == 401:
                    raise SystemExit("FATAL: HTTP 401 — token invalid or "
                                     "expired.")
                elif e.code == 404:
                    raise  # caller decides (missing org vs disabled feature)
                elif e.code >= 500:
                    time.sleep(2 ** attempt)
                else:
                    raise
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last_err = e
                time.sleep(2 ** attempt)
        raise RuntimeError(f"GET {url} failed after {API_RETRIES} tries: "
                           f"{last_err}")

    def paginate(self, path: str):
        sep = "&" if "?" in path else "?"
        url = f"{API}{path}{sep}per_page={PER_PAGE}"
        while url:
            data, link = self._request(url)
            if isinstance(data, dict):  # non-list response
                yield data
                return
            yield from data
            url = None
            for part in link.split(","):
                if 'rel="next"' in part:
                    url = part[part.find("<") + 1:part.find(">")]
                    break

    def whoami_check(self, login: str, owner_type: str) -> None:
        """Fail fast before any heavy work: /rate_limit validates the token
        itself, then the org/user lookup catches a wrong login or a
        fine-grained PAT the org never approved."""
        self._request(f"{API}/rate_limit")
        probe = "orgs" if owner_type == "org" else "users"
        try:
            self._request(f"{API}/{probe}/{login}")
        except urllib.error.HTTPError as e:
            raise SystemExit(
                f"FATAL: cannot see {owner_type} '{login}' (HTTP {e.code}). "
                "Wrong login/owner-type, or the org has not approved the "
                "fine-grained PAT.")

    def list_repos(self, login: str, owner_type: str) -> list:
        # PAT path only. The upstream preferred /installation/repositories
        # (GitHub App installation grants) and fell back to this — that
        # branch is deliberately dropped: this harness is PAT-only (the
        # App flow needs a callback URL + KMS key + installation_id store,
        # none of which a local operator tool has; see
        # docs/github-transfer-handoff.md §0), and probe already told the
        # operator that scope = everything the PAT can see.
        path = (f"/orgs/{login}/repos?type=all" if owner_type == "org"
                else f"/users/{login}/repos?type=all")
        return list(self.paginate(path))


# ── git legs (token via GIT_ASKPASS — never in URL or argv) ──────────────────

def make_askpass(tmpdir: Path) -> Path:
    helper = tmpdir / "askpass.sh"
    helper.write_text(
        '#!/bin/sh\n'
        'case "$1" in\n'
        '  Username*) echo "x-access-token" ;;\n'
        '  Password*) echo "$GITHUB_TOKEN" ;;\n'
        'esac\n'
    )
    helper.chmod(stat.S_IRWXU)  # 0700
    return helper


def git_env(askpass: Path, token: str) -> dict:
    env = os.environ.copy()
    env["GIT_ASKPASS"] = str(askpass)
    env["GITHUB_TOKEN"] = token
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _git(cmd: list, env: dict) -> None:
    subprocess.run(cmd, env=env, check=True, stdout=subprocess.DEVNULL,
                   stderr=subprocess.PIPE, text=True)


def detect_lfs(target: Path) -> bool:
    """Does any .gitattributes in HEAD declare filter=lfs? Empty repos and
    repos with no HEAD grep nonzero — that's a clean False, not an error."""
    proc = subprocess.run(
        ["git", "-C", str(target), "grep", "-q", "-e", "filter=lfs",
         "HEAD", "--", ".gitattributes", "*/.gitattributes"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc.returncode == 0


def clone_mirror(repo: dict, dest: Path, env: dict, refresh: bool,
                 lfs_all: bool) -> dict:
    """Mirror-clone one repo with retry + resume, then LFS when the repo
    declares it. LFS failure is recorded, never fatal — the missing bytes
    surface in verify's per-repo rollup."""
    name = repo["name"]
    target = dest / "repos" / f"{name}.git"
    marker = target / ".cdp-complete"
    url = repo["clone_url"]  # https://github.com/<org>/<repo>.git — no token

    if marker.exists() and not refresh:
        log(f"  clone: {name} already complete — skipping (resume)")
        return {"clone": "skipped", "bytes": dir_size(target)}
    if target.exists() and not marker.exists():
        log(f"  clone: {name} partial from a previous run — re-cloning")
        shutil.rmtree(target)

    for attempt in range(1, CLONE_RETRIES + 1):
        try:
            if target.exists() and refresh:
                cmd = ["git", "-C", str(target), "remote", "update",
                       "--prune"]
            else:
                cmd = ["git", "clone", "--mirror", url, str(target)]
            log(f"  clone: {name} (attempt {attempt}/{CLONE_RETRIES})")
            _git(cmd, env)
            res = {"clone": "ok"}
            if lfs_all or detect_lfs(target):
                log(f"  lfs:   {name} fetching LFS objects")
                try:
                    _git(["git", "-C", str(target), "lfs", "fetch",
                          "--all"], env)
                    res["lfs"] = "ok"
                except subprocess.CalledProcessError as e:
                    tail = (e.stderr or "").strip().splitlines()
                    log(f"  lfs:   {name} FAILED: "
                        f"{tail[-1] if tail else 'no stderr'}")
                    res["lfs"] = "failed"
            marker.touch()
            res["bytes"] = dir_size(target)
            log(f"  clone: {name} DONE ({human_bytes(res['bytes'])})")
            return res
        except subprocess.CalledProcessError as e:
            err = (e.stderr or "").strip().splitlines()
            tail = err[-1] if err else "no stderr"
            log(f"  clone: {name} FAILED attempt {attempt}: {tail}")
            if "403" in tail or "Authentication failed" in tail:
                raise SystemExit(
                    f"FATAL: git auth failed on {name} — the PAT almost "
                    "certainly lacks 'Contents: read'. Fix the token "
                    "before continuing.")
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            time.sleep(5 * attempt)
    return {"clone": "failed", "bytes": 0}


def wiki_absent(stderr_tail: str) -> bool:
    """A wiki that is enabled (has_wiki) but was never created clones as
    not-found — a skip, not a failure. Pure so tests can pin the
    classification."""
    t = stderr_tail.lower()
    return ("not found" in t or "access denied" in t
            or "does not exist" in t)


def clone_wiki(repo: dict, dest: Path, env: dict, refresh: bool) -> dict:
    """Wikis are separate <full_name>.wiki.git repos, not in the repo
    listing — cloned by constructed URL, gated on the has_wiki flag."""
    name = repo["name"]
    if not repo.get("has_wiki"):
        return {"wiki": "none", "wiki_bytes": 0}
    target = dest / "wikis" / f"{name}.wiki.git"
    marker = target / ".cdp-complete"
    url = f"https://github.com/{repo['full_name']}.wiki.git"

    if marker.exists() and not refresh:
        log(f"  wiki:  {name} already complete — skipping (resume)")
        return {"wiki": "skipped", "wiki_bytes": dir_size(target)}
    if target.exists() and not marker.exists():
        shutil.rmtree(target)

    for attempt in range(1, CLONE_RETRIES + 1):
        try:
            log(f"  wiki:  {name} (attempt {attempt}/{CLONE_RETRIES})")
            _git(["git", "clone", "--mirror", url, str(target)], env)
            marker.touch()
            size = dir_size(target)
            log(f"  wiki:  {name} DONE ({human_bytes(size)})")
            return {"wiki": "ok", "wiki_bytes": size}
        except subprocess.CalledProcessError as e:
            err = (e.stderr or "").strip().splitlines()
            tail = err[-1] if err else "no stderr"
            if wiki_absent(tail):
                log(f"  wiki:  {name} absent (enabled but never created) "
                    "— skipping")
                shutil.rmtree(target, ignore_errors=True)
                return {"wiki": "absent", "wiki_bytes": 0}
            log(f"  wiki:  {name} FAILED attempt {attempt}: {tail}")
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            time.sleep(5 * attempt)
    return {"wiki": "failed", "wiki_bytes": 0}


# ── issues / PRs / comments -> JSONL ─────────────────────────────────────────

def pull_json(gh: GitHub, login: str, repo: dict, dest: Path,
              refresh: bool) -> dict:
    name = repo["name"]
    outdir = dest / "json" / name
    marker = outdir / ".cdp-complete"
    if marker.exists() and not refresh:
        log(f"  json:  {name} already complete — skipping (resume)")
        return {"json": "skipped"}
    outdir.mkdir(parents=True, exist_ok=True)

    counts = {}
    try:
        for kind, tmpl in JSON_ENDPOINTS.items():
            path = tmpl.format(login=login, name=name)
            n = 0
            with open(outdir / f"{kind}.jsonl", "w") as f:
                try:
                    for item in gh.paginate(path):
                        f.write(json.dumps(item) + "\n")
                        n += 1
                except urllib.error.HTTPError as e:
                    if e.code == 404:  # feature disabled on the repo
                        log(f"  json:  {name}/{kind}: 404 — skipping")
                    else:
                        raise
            counts[kind] = n
        marker.touch()
        log(f"  json:  {name} DONE ({counts})")
        return {"json": "ok", "counts": counts}
    except Exception as e:
        log(f"  json:  {name} FAILED: {e}")
        return {"json": "failed", "error": str(e)}


# ── manifest + upload ────────────────────────────────────────────────────────

def build_manifest(login: str, owner_type: str, finished_utc: str,
                   results: list) -> dict:
    """Pure. failed_repos = anything whose clone/wiki/json leg failed;
    a failed LFS fetch also fails the repo (its bytes are missing and a
    re-run must revisit it)."""
    failed = sorted({r["repo"] for r in results
                     if r.get("clone") == "failed"
                     or r.get("wiki") == "failed"
                     or r.get("json") == "failed"
                     or r.get("lfs") == "failed"})
    total = sum(r.get("bytes", 0) + r.get("wiki_bytes", 0) for r in results)
    return {
        "login": login,
        "owner_type": owner_type,
        "finished_utc": finished_utc,
        "repo_count": len(results),
        "total_clone_bytes": total,
        "failed_repos": failed,
        "results": results,
    }


def upload(dest: Path, dest_url: str, sas: str) -> bool:
    """azcopy the staged tree to the container prefix. --overwrite=false is
    the write invariant on this path (client-side no-overwrite — the same
    choice as azcopy-runner.sh on the S3 path; NOT the API-enforced
    If-None-Match of the local pulls). CompletedWithSkipped is the normal
    resume outcome. SAS never printed — the URL is only ever passed as
    argv to azcopy on this VM, and output is scanned, not echoed raw."""
    if shutil.which("azcopy") is None:
        log("upload: FATAL azcopy not found on PATH — bootstrap incomplete")
        return False
    log(f"upload: azcopy {dest} -> {dest_url.split('?')[0]}")
    # Trailing /* copies dir CONTENTS so the container prefix isn't nested.
    proc = subprocess.run(
        ["azcopy", "copy", str(dest) + "/*", f"{dest_url}?{sas}",
         "--recursive", "--overwrite=false", "--log-level", "ERROR"],
        capture_output=True, text=True)
    out = (proc.stdout or "") + (proc.stderr or "")
    failed = "0"
    status = ""
    for ln in out.splitlines():
        if "Transfers Failed" in ln:
            failed = ln.split(":")[-1].strip()
        if "Final Job Status" in ln:
            status = ln.split(":")[-1].strip()
    ok = failed == "0" and (proc.returncode == 0
                            or status.startswith("Completed"))
    log(f"upload: {'DONE' if ok else 'FAILED'} "
        f"(status={status or 'rc=%d' % proc.returncode}, failed={failed})")
    if not ok:
        # never echo raw azcopy output wholesale — a URL line would leak
        # the SAS; keep only sig-free lines
        tail = [ln for ln in out.splitlines()[-15:] if "sig=" not in ln]
        for ln in tail:
            log(f"upload:   {ln}")
    return ok


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        description="VM-side GitHub puller (github-azure-transfer)")
    ap.add_argument("--login", required=True)
    ap.add_argument("--owner-type", choices=["org", "user"], default="org")
    ap.add_argument("--dest", required=True,
                    help="staging dir on the VM, e.g. ~/xfer-gh/dest")
    ap.add_argument("--limit", type=int, default=0,
                    help="only the first N repos (pilot)")
    ap.add_argument("--only", metavar="REPO",
                    help="only this one repo by name")
    ap.add_argument("--refresh", action="store_true",
                    help="re-fetch even if marked complete")
    ap.add_argument("--lfs-all", action="store_true",
                    help="run lfs fetch on every repo, not just detected")
    ap.add_argument("--skip-upload", action="store_true")
    args = ap.parse_args()

    if shutil.which("git") is None:
        log("FATAL: git not found on PATH")
        return 1
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        log("FATAL: GITHUB_TOKEN not in environment (github.env not "
            "sourced?)")
        return 1
    dest_url = os.environ.get("AZURE_DEST_URL", "").strip()
    dest_sas = os.environ.get("AZURE_DEST_SAS", "").strip()
    if not args.skip_upload and not (dest_url and dest_sas):
        log("FATAL: AZURE_DEST_URL/AZURE_DEST_SAS not in environment "
            "(dest.env not sourced?)")
        return 1

    dest = Path(os.path.expanduser(args.dest))
    dest.mkdir(parents=True, exist_ok=True)

    gh = GitHub(token)
    log(f"validating token + visibility of {args.owner_type} "
        f"'{args.login}' ...")
    gh.whoami_check(args.login, args.owner_type)

    repos = gh.list_repos(args.login, args.owner_type)
    log(f"enumerated {len(repos)} repos")
    if args.only:
        repos = [r for r in repos if r["name"] == args.only]
        if not repos:
            log(f"FATAL: repo '{args.only}' not found")
            return 1
    if args.limit:
        repos = repos[: args.limit]
        log(f"--limit: processing first {len(repos)} repos only")

    results = []
    with tempfile.TemporaryDirectory(prefix="ghpull.") as td:
        askpass = make_askpass(Path(td))
        env = git_env(askpass, token)
        for i, repo in enumerate(repos, 1):
            log(f"[{i}/{len(repos)}] {repo['full_name']}")
            write_progress(dest, "pull", i, len(repos), repo["full_name"])
            res = {"repo": repo["name"]}
            res.update(clone_mirror(repo, dest, env, args.refresh,
                                    args.lfs_all))
            res.update(clone_wiki(repo, dest, env, args.refresh))
            res.update(pull_json(gh, args.login, repo, dest, args.refresh))
            results.append(res)

    manifest = build_manifest(
        args.login, args.owner_type,
        datetime.now(timezone.utc).isoformat(), results)
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2))
    failed = manifest["failed_repos"]
    log(f"SUMMARY: {len(results)} repos, "
        f"{human_bytes(manifest['total_clone_bytes'])} staged, "
        f"{len(failed)} failed {failed if failed else ''}")

    upload_ok = True
    if not args.skip_upload:
        # upload-what-succeeded: completed repos land now even when others
        # failed — their markers keep them out of the next run's work, and
        # verify surfaces the failed set until a re-run clears it
        write_progress(dest, "upload", len(results), len(results), "azcopy")
        upload_ok = upload(dest, dest_url, dest_sas)
    write_progress(dest, "done", len(results), len(results),
                   f"{len(failed)} failed")
    return 2 if failed or not upload_ok else 0


if __name__ == "__main__":
    sys.exit(main())

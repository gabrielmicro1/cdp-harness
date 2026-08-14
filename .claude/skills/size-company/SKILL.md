---
name: size-company
description: Use when measuring one company's pushed data — "size <company>", "how big is <company>'s data", "did <company> push anything", or reconciling what landed in their -raw container against the manifest.
---

# Size one company

Measures compressed + uncompressed size per source for `<slug>-raw`. The sizer
runs **locally on this machine** as a detached background process — no VM
involved (it reads only list pages, zip central directories, and gz trailers:
kilobytes per blob). Everything is **read-only** against client storage
(rl SAS, 1-day expiry; we never write blobs/tags/metadata). Repeat runs are
incremental: per-blob results are cached in `companies/<slug>/blob-index.tsv.gz`
(ETag-validated), so an unchanged container costs only its listing time.
Output: a new `companies/<slug>/sizing-runs/<ts>.json` + updated status.json
(gitignored runtime state — never committed).

Prereq: the company is onboarded (config.json exists). If not → onboard-company.

## Run it

```bash
export PATH="/opt/homebrew/bin:$PATH"
python3 scripts/size_company.py <slug>
```

That does all phases: UsedCapacity skip-check → IP-firewall-rule-if-needed →
SAS mint → detached local launch → poll → harvest → cleanup. It's bounded at
~8 minutes of polling per invocation; **big containers need more** (blob COUNT
drives runtime — ~3–4k blobs/sec was the in-region figure, local is slower;
millions of blobs means hours). The detached sizer keeps running between
invocations, so just keep going across turns:

```bash
python3 scripts/size_company.py <slug> --phase poll --max-wait 480
python3 scripts/size_company.py <slug> --phase harvest   # once terminal
```

`--no-cache` forces a full re-size (use when numbers look suspicious and you
want zero reuse). First run for a company has no cache — budget the full
time; repeat runs skip the per-blob reads for every unchanged blob. Note
editing a company's `expected-data-sizes.json` services also forces a full
re-size automatically on the next run (the cache's matcher fingerprint no
longer matches) — no `--no-cache` needed for that case.

Never reimplement the phases in Bash — the deterministic logic lives in
`scripts/phases.py` (see CLAUDE.md). Your job is orchestration + judgment.

## Before a long run

- The launcher wraps the sizer in `caffeinate -i`, which blocks *idle* sleep
  only — **a closed lid still suspends it**. For a monster container, tell the
  user to keep the laptop open and on power until harvest.
- A network drop mid-listing kills the run (per-blob errors are just counted;
  a listing error is fatal). Re-running is idempotent and safe.

## Interpreting outcomes

- `sized` — fresh numbers. Sanity-check against the lore in
  [references/sizing-lore.md](references/sizing-lore.md) before presenting
  (store-mode zips, timestamp prefixes, tar.gz undercount, BadZipFile).
  Check `cache.hits`/`cache.misses` in the run file — a warm run with
  unexpected mass misses means the client re-uploaded (overwrote) blobs,
  which is itself worth mentioning to the user. Check `detected_services`:
  declared services found embedded inside another source (e.g. CRM exports
  inside a Workspace/Takeout archive) are flagged `found-embedded` in
  reports — present them as found, with their host prefix, not as missing.
- `skipped-unchanged` — UsedCapacity metric matched the last run; a
  copied-forward run file was written. Expected on most days. Note the metric
  is account-level: a scrub-side write can force one redundant re-size (harmless).
- `failed` — read the reason:
  - **403 AuthorizationFailure in the sizer log** — firewall, NOT a bad SAS.
    If we just added the IP rule it's propagation: wait ~60s and relaunch.
    Never re-mint the SAS for a 403.
  - **could not determine public IP** — the machine is offline or the IP
    services are blocked; check connectivity and retry.
  - **sizer process gone without .done** — the laptop slept, the network
    dropped, or the sizer crashed. Check
    `companies/.sizer-work/<slug>-sizer.log` (tail) and `.stdout`, then
    relaunch; see "Manual rescue" in CLAUDE.md. If `.done` actually exists,
    the run finished — just run `--phase harvest`.

## Judgment calls

- First run for a company is never skipped (no baseline) — expect the full
  listing time.
- If the numbers look absurd (ratio 50×, a source at 0 that was huge
  yesterday), check the run's `errors.by_type` and the log tail before
  reporting; a burst of URLError/timeouts means the laptop's connection
  wobbled and sizes floored to stored — re-size rather than present those.
- Per-blob detail survives harvest in `companies/<slug>/blob-index.tsv.gz`
  (zip/gz rows: name, etag, sizes, method, embedded-service hits). `zcat` it
  for on-demand per-blob answers. Deleting the file is always safe — the next
  run just does a full re-size.
- Present sizes in decimal GB/TB (÷10⁹); mention the GiB mismatch only if the
  user compares against a pre-harness report.

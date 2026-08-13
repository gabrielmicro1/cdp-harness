---
name: size-company
description: Use when measuring one company's pushed data — "size <company>", "how big is <company>'s data", "did <company> push anything", or reconciling what landed in their -raw container against the manifest.
---

# Size one company

Measures compressed + uncompressed size per source for `<slug>-raw`, on an
in-region VM via **managed run commands** — nothing large touches the laptop,
and everything is **read-only** against client storage (rl SAS, 1-day expiry;
we never write blobs/tags/metadata). Output: a new
`companies/<slug>/sizing-runs/<ts>.json` + updated status.json, committed.

Prereq: the company is onboarded (config.json exists). If not → onboard-company.

## Run it

```bash
export PATH="/opt/homebrew/bin:$PATH"
python3 scripts/size_company.py <slug>
```

That does all phases: UsedCapacity skip-check → firewall-if-needed → SAS mint →
launch → poll → harvest → cleanup → commit. It's bounded at ~8 minutes of
polling per invocation; **big containers need more** (blob COUNT drives
runtime, ~3–4k blobs/sec — webspiders' 10.9M blobs ≈ 45 min). When the output
says still in flight, keep going across turns:

```bash
python3 scripts/size_company.py <slug> --phase poll --max-wait 480
python3 scripts/size_company.py <slug> --phase harvest   # once terminal
```

Never reimplement the phases in Bash — the deterministic logic lives in
`scripts/phases.py` (see CLAUDE.md). Your job is orchestration + judgment.

## Interpreting outcomes

- `sized` — fresh numbers. Sanity-check against the lore in
  [references/sizing-lore.md](references/sizing-lore.md) before presenting
  (store-mode zips, timestamp prefixes, tar.gz undercount, BadZipFile).
- `skipped-unchanged` — UsedCapacity metric matched the last run; a
  copied-forward run file was written. Expected on most days. Note the metric
  is account-level: a scrub-side write can force one redundant re-size (harmless).
- `no-vm` — **flag the user, don't guess.** ~40% of companies have no verify
  VM. Options for the user: start a stopped VM, provision a sizer VM, or skip.
- `failed` — read the reason:
  - **`VM ... is not running`** — tell the user which VM and ask before
    starting it (their call, it costs money).
  - **403 AuthorizationFailure in the sizer log** — firewall, NOT a bad SAS.
    If we just added the rule it's propagation: wait ~60s and relaunch. Never
    re-mint the SAS for a 403.
  - **`Conflict` / "execution in progress"** — a legacy invoke is still
    finishing on that VM (one-at-a-time rule). Wait ~15s, retry. The nohup'd
    sizer keeps running regardless.
  - **stale run-command create failure** — a leftover `sizer-<slug>` resource;
    `phases.delete_run_command` clears it; re-launch.
  - **no summary recoverable** — go to "Manual rescue" in CLAUDE.md: the
    sizer's `/var/tmp/<slug>-sizer.*` files on the VM survive agent death.

## Judgment calls

- First run for a company is never skipped (no baseline) — expect the full
  listing time.
- If the numbers look absurd (ratio 50×, a source at 0 that was huge
  yesterday), check the run's `errors.by_type` and the log tail before
  reporting; don't present suspicious numbers as fact.
- Per-blob detail (`sizes.tsv`) exists on the VM if needed — fetch via
  `phases.fetch_sizes_tsv` for SMALL containers only; for huge ones propose an
  alternative (it cannot ride home through 4KB invoke chunks).
- Present sizes in decimal GB/TB (÷10⁹); mention the GiB mismatch only if the
  user compares against a pre-harness report.

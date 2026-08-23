---
name: deep-verify
description: Use when a company's sizing numbers need to be certified as measurements before a commercial milestone — "deep verify <company>", "certify <company>'s sizes", "stream-verify the archives", before verify-completion sign-off, or when archive numbers look suspicious (a lying zip, gz uncertainty that a bigger budget can't clear).
---

# Deep-verify one company

Stream-decompresses EVERY compressed blob (zip, gz, bz2, xz) in
`<slug>-raw` and measures exact uncompressed sizes, instead of trusting zip
central directories and gz trailers. Runs on a temporary **in-region Azure
VM** (`deepv-<slug>`) — same-region egress is free and moves multiple TB per
hour; the laptop would pay ~$85/TB and days of wall clock for the same
bytes. Output is a normal sizing run (method `sized`) with a `verification`
coverage block (measured / metadata-trusted / unmeasurable-format buckets,
CD-mismatch count), and the harvested blob-index caches every measurement by
ETag: later shallow daily runs replay the exact numbers at zero HTTP, and a
repeat deep run on an unchanged container is listing-only.

Read-only stays absolute: the SAS is account-level `rl` (1-day default).
The VM lifecycle borrows the transfer engines' rules — vnet-rule network
grant (IP rules never match same-region VMs), secrets over ssh stdin only,
no state file (the VM + its tags are the truth), auto-teardown at harvest.

Prereqs: company onboarded; ideally a recent shallow run (the pushed cache
narrows the work). Daily sizing is untouched — run this at engagement
milestones, typically right before verify-completion.

## Run it (the step loop)

```bash
export PATH="/opt/homebrew/bin:$PATH"
python3 scripts/deep_verify.py step <slug>
```

`step` inspects Azure and advances exactly ONE phase per invocation:
create VM → grant network + push sizer/cache + launch (tmux `deepverify`)
→ poll → harvest + auto-teardown. Just keep calling `step` across turns
until `"phase": "complete"`. Poll cadence should match container size:
minutes apart for croplabel-scale, hours apart for multi-TB. `status <slug>`
inspects without advancing; `discover <slug>` reconstructs everything in a
fresh session (no state file exists to lose).

- **Cost/duration framing for the user:** VM ≈ $0.40/h, egress $0.
  Duration ≈ listing time + compressed archive bytes ÷ ~4–5 TB/h. A repeat
  run ≈ listing time only. `--sas-days 2` for containers whose compressed
  bytes exceed ~a day of streaming.
- `--keep-vm` harvests without tearing down (debugging only — VM keeps
  billing). `teardown <slug> --confirmed` finishes cleanup manually.
- `--no-cache` re-measures every blob from scratch (paranoia mode).

## Interpreting the verification block

- `measured_*` — bytes we (or the listing) actually observed: streamed
  archives, truncated-stream exact partials, loose stored files. The goal
  is measured ≈ 100% of bytes.
- `trusted_*` — still metadata: unstreamable zip entries (encrypted or
  exotic compression → `zip-partial(k/Ncd)` rows), stream failures
  (`*-stored` fallbacks). A re-run retries exactly these.
- `unmeasurable_*` — formats with no stdlib codec (.7z/.rar/.zst), counted
  at stored size, broken out per-format. A client conversation, not a bug.
- `cd_mismatches` — zips whose central directory LIED; the streamed value
  is already in the totals (silently — no run note, by policy). For the
  per-blob list: `zcat companies/<slug>/blob-index.tsv.gz | grep
  zip-exact-mismatch` (the method carries `cd=<claimed>`).
- Reports get a "deep-verified <date>" badge; verify_completion shows an
  informational `deep_verify` check that never gates. With a clean block
  (trusted 0, unmeasurable 0) do NOT loosen verify-completion tolerance —
  a shortfall is real.

## Failures

- **403 from the VM** → vnet-rule propagation (~10–30s) or an external
  reconciler stripped the rule; `step` re-grants and retries. NEVER re-mint
  the SAS for a 403.
- **Sizer died mid-run** → `step` relaunches seeded from the partial TSV on
  the VM; nothing is lost. VM itself gone → `step` recreates it; the
  laptop-side cache still caps the redo cost.
- **A teardown failure after harvest** is reported but doesn't lose the
  run — the run file is already on disk; finish with
  `teardown <slug> --confirmed`.

Afterwards: report-company (the badge and notes update), then
verify-completion for sign-off.

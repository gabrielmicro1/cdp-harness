---
name: verify-completion
description: Use when a company looks done and needs sign-off — "is <company> complete", "verify <company>", "can we close out <company>", or their report shows ~100%.
---

# Verify a company is complete

```bash
python3 scripts/verify_completion.py <slug> --tolerance 0.98
```

The script runs the deterministic checklist (headline within tolerance, every
byte-declared service within tolerance, zero-byte blob scan, unexpected-source
scan, error counts surfaced). Your job is the **judgment layer** on top:

1. **Recent numbers first.** If the latest run is a days-old copy-forward or
   predates zero-byte tracking, re-size (size-company) before verifying —
   never sign off on stale data.
2. **Resolve the `needs_judgment` block with the user:**
   - **Record-count services** — not byte-comparable. Ask the user whether
     each looks plausible (blob presence, rough size) or needs a manual count
     with the client.
   - **Overshoot (>100%)** — often a wrong manifest; commercially significant
     either way (we may be buying more than declared, or the manifest
     undersold). Raise it explicitly.
   - **Zero-declared-with-data / unexpected sources** — data we didn't buy?
     Scope creep or mislabeled prefix (check for timestamp prefixes — see
     size-company's references/sizing-lore.md). User decides.
3. **Produce a go/no-go summary**: each check with pass/fail, the open
   judgment items with your recommendation, and a clear verdict sentence.
4. **Only on a clean GO from the user:**
   ```bash
   python3 scripts/verify_completion.py <slug> --mark-complete
   ```
   which sets `stage: complete`. Never mark complete on a failing
   or ambiguous checklist; a wrongly-closed company silently drops out of the
   morning stall radar.

Tolerance is a parameter — the user may loosen it for a company with a known
tar.gz undercount (trailer-floored multi-GB tarballs legitimately read low).
Suggest that explicitly rather than fudging numbers.

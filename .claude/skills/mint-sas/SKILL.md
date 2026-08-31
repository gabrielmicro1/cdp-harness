---
name: mint-sas
description: Use when a client needs credentials to push data into their -raw container — "mint a SAS for <company>", "create a push token", "extend <company>'s SAS", a client reports 403/expired on upload, or the user wants to see the tokens page / which SAS tokens are outstanding.
---

# Mint a client push SAS

Input: a company slug (must already be onboarded — config.json is the source
of the storage account + container). Output: a password-protected zip in
`companies/<slug>/` holding the `racwl` (no delete) container SAS +
push instructions + the `client_push.sh` upload helper, the zip password
(stdout only), a ledger entry in `companies/.sas-ledger.json`, and on
request `reports/sas-tokens.html`.

## Steps

1. **Mint** (deterministic — never hand-roll the az call):
   ```bash
   export PATH="/opt/homebrew/bin:$PATH"
   python3 scripts/mint_push_sas.py mint <slug> --note "<who asked / why>"
   ```
   - Expiry defaults to **14 days** (`DEFAULT_EXPIRY_DAYS` in the script —
     that constant is the fleet policy; edit it there to change the default).
     A single engagement that needs longer: `--days N`. Do not creep the
     default upward per-request.
   - Signing is **account-key** (like the transfer engines), not sas-mint's
     user-delegation: user-delegation SAS caps at 7 days, so 14 is only
     possible on this path — and account-key signing is control-plane only,
     so the SA firewall is irrelevant (no IP-rule add, no propagation wait,
     no 403 dance).
   - Always pass a `--note` — it is the only "who/why" the ledger and the
     tokens page will ever show.

2. **Deliver zip + password on separate channels.** The mint output names
   the `zip` (in `companies/<slug>/` — gitignored, so it can never be
   committed) and prints the `password` — which exists ONLY on stdout, never
   on disk. Delivery is a split: zip over one channel (email/drive),
   password over another (Slack). The SAS URL lives only inside the zip
   (`sas-credentials.txt`, with azcopy/curl push instructions); never paste
   it into a file, a report, a commit, or a Slack draft yourself.
   Encryption is ZipCrypto (`unzip -P` works everywhere, not AES) — the
   out-of-band split is doing the real protective work.
   - The zip is **self-sufficient**: it also carries `client_push.sh`, so
     the client needs no second attachment. `--read-only` ships credentials
     ONLY — a buyer downloads, never pushes (the platform's
     partner-vs-client bundle split).
   - **Never hand-edit `sas-credentials.txt`.** Its `SAS URL:` label and the
     URL on the immediately following line are a machine-read contract:
     `client_push.sh` pulls them with one awk line, and decorating the label
     or adding a blank line under it fails every client upload with
     "could not read SAS URL from sas-credentials.txt".

3. **Regenerate the tokens page** after every mint, unprompted (the
   always-regenerate-report discipline):
   ```bash
   python3 scripts/mint_push_sas.py page
   ```
   Writes `reports/sas-tokens.html` — company, container, perms, created,
   expires, active/expiring/expired badge, fingerprint, note. Metadata only;
   the page can never leak a token. `list` prints the same as JSON.

## Edge cases

- **Extending a token:** there is no in-place extension of a SAS — mint a
  fresh one (`--days` if 14 isn't enough) and have the client swap URLs. The
  old entry stays in the ledger and simply goes expired; that history is the
  point of the ledger.
- **Revocation:** an account-key SAS dies only with account-key rotation,
  which kills EVERY outstanding SAS on that account — including the client's
  active push and any transfer-engine SAS. Policy: never revoke, let it
  lapse. If a token is truly compromised, key rotation is a user decision,
  not yours.
- **Which container:** always from config.json — container names don't
  always match the slug (helpsy's is `helpsy-holdings-pbc-raw`, kidinme's
  `kidinme-corporation-raw`). Never guess `<slug>-raw` by hand.
- **Read-only principle:** minting a write SAS does not breach the
  read-only rule — the CLIENT writes with it; the harness still never
  writes to client storage. Same sanction as the ingest paths.
- **Not onboarded yet:** `mint` fails without config.json — run
  onboard-company first.

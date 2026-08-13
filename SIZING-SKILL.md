---
name: corpus-unzip-and-size
description: >-
  Size (or unzip) any corpus company's `-raw` Azure blob container, run on an
  in-region VM via `az vm run-command` — nothing large touches the laptop.
  Claude can drive it end-to-end itself. Use whenever the user wants to
  "size <company>", "get the data size / per-source size report", "how big is
  <company>'s data", "unzip/decompress/extract the raw blobs", measure
  compressed vs uncompressed size, or reconcile declared manifest sizes against
  what actually landed in `-raw`. Trigger even if they only say "size latchel"
  or "how big is croplabel" without naming a VM or container. Works on ANY
  company — verify VM or not. Companion to the corpus-sizer-runbook memory and
  manifest-to-cdp-import skill. Grounded in real croplabel / webspiders /
  latchel runs, 2026-07.
---

# Size (or unzip) a corpus `-raw` container — any company

Measures compressed + uncompressed size per source for a company's `-raw`
container. It runs on an **in-region VM** (pushed via `az vm run-command`), so
downloads/parsing happen on the VM in-region — **nothing large hits the
laptop**. Default job is **SIZE** (measure, read-only). EXTRACT (writing the
unzipped tree back to blob) is a separate, heavier job — see
`references/unzip-path.md`.

## Claude can run this itself
`az` is at `/opt/homebrew/bin/az` (not on the default non-interactive PATH) and
the login is cached in `~/.azure`. So prefix `export PATH="/opt/homebrew/bin:$PATH"`
and drive it directly — you don't have to hand commands to the user (though you
can, interactively, if they prefer). Sub is `m1 corpus` (selected by name below;
the id `600b01d1…` is just a sanity-check hint).

First set `SKILL_DIR` to the base directory this skill loads from (printed as
"Base directory for this skill: …" at load) so the bundled sizer resolves
wherever the plugin is installed:
```bash
export SKILL_DIR="<this skill's base directory>"
```

## Key facts learned the hard way (read these)
- **~40% of companies have NO `verify-vm-<slug>`.** They have a `vm-*-extract`,
  `vm-dwt-transfer`, a leftover `vm-sizer-*`, or nothing. So **never assume a
  verify VM** — auto-discover (Step 1).
- **Use the dependency-free sizer `scripts/corpus_sizer_rest.py` by default.**
  Stdlib-only + a SAS — no `azure-storage-blob`, no managed identity, no
  outbound internet. It runs on *any* VM (transfer VMs lack the SDK and can't
  `pip install`; some have no managed identity → IMDS 400). The SDK-based
  `scripts/corpus_sizer.py` is a legacy alt for verify VMs only.
- **The firewall may already be open.** Transfer VMs' subnets are usually
  already whitelisted on their SA; verify VMs' often aren't. **Check first; only
  add if missing; only remove a rule YOU added** (never delete a pre-existing one).
- **`az vm run-command` allows ONE at a time per VM.** Never leave a long
  VM-side sleep loop running (it jams the slot → `Conflict` on every later
  call). Poll with *instant* scripts and do the waiting locally, bounded under
  the 10-min Bash timeout.
- **Huge containers are real:** webspiders was 10.9M blobs (~45 min); croplabel
  was 806 (~1 min); latchel 2,289 (~2 min). File *count* drives time (~3–4k
  blobs/sec), not bytes.

## Step 0 — Resolve SA / RG / container
```bash
export PATH="/opt/homebrew/bin:$PATH"; az account set -s "m1 corpus" >/dev/null
SLUG=<company>
SA=$(az storage account list --query "[?contains(resourceGroup,'$SLUG')].name | [0]" -o tsv)
SA_RG=$(az storage account show -n "$SA" --query resourceGroup -o tsv)
SUB=$(az account show --query id -o tsv)
az rest --method get \
  --url "https://management.azure.com/subscriptions/$SUB/resourceGroups/$SA_RG/providers/Microsoft.Storage/storageAccounts/$SA/blobServices/default/containers?api-version=2023-05-01" \
  --query "value[].name" -o tsv           # pick <slug>-raw (leave -scrubbed / insights-logs-*)
CONTAINER=<slug>-raw
```

## Step 1 — Discover a usable VM (verify VM, else any in-region VM)
```bash
VM=$(az vm list -g "$SA_RG" --query "[?starts_with(name,'verify-vm-')].name | [0]" -o tsv)
[ -z "$VM" ] && VM=$(az vm list -g "$SA_RG" --query "[0].name" -o tsv)   # any VM in the SA's RG
VM_RG="$SA_RG"
echo "using VM=$VM"
# must be running:
az vm get-instance-view -g "$VM_RG" -n "$VM" --query "instanceView.statuses[?starts_with(code,'PowerState')].displayStatus|[0]" -o tsv
```
If no VM exists in the RG (a couple of companies have none), you must start one
(a stopped VM) or provision a sizer VM — flag this to the user; don't guess.

## Step 2 — Firewall (conditional — only touch if needed)
```bash
NIC_ID=$(az vm show -g "$VM_RG" -n "$VM" --query "networkProfile.networkInterfaces[0].id" -o tsv)
SUBNET_ID=$(az network nic show --ids "$NIC_ID" --query "ipConfigurations[0].subnet.id" -o tsv)
ALREADY=$(az storage account show -n "$SA" -g "$SA_RG" \
  --query "contains(networkRuleSet.virtualNetworkRules[].virtualNetworkResourceId, '$SUBNET_ID')" -o tsv)
if [ "$ALREADY" != "true" ]; then
  az network vnet subnet update --ids "$SUBNET_ID" --service-endpoints Microsoft.Storage -o none
  az storage account network-rule add -g "$SA_RG" --account-name "$SA" --subnet "$SUBNET_ID" -o none
  WE_ADDED_FW=1; sleep 60   # propagation
else
  WE_ADDED_FW=0             # pre-existing — do NOT remove in cleanup
fi
```
A `403 AuthorizationFailure` on the first read = firewall (subnet not allowed),
**not** a bad SAS. That's what this step prevents.

## Step 3 — Mint a SAS + launch the portable sizer
`rl` is enough for SIZE. Base64 both the SAS and the script through run-command.
```bash
TAG=$SLUG-sizer
EXPIRY=$(python3 -c "import datetime;print((datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(days=1)).strftime('%Y-%m-%dT%H:%MZ'))")
SAS=$(az storage account generate-sas --account-name "$SA" --services b --resource-types sco --permissions rl --expiry "$EXPIRY" --https-only -o tsv 2>/dev/null)
SAS_B64=$(printf '%s' "$SAS" | base64 | tr -d '\n')
B64=$(base64 -i "$SKILL_DIR/scripts/corpus_sizer_rest.py" | tr -d '\n')
az vm run-command invoke -g "$VM_RG" -n "$VM" --command-id RunShellScript \
  --scripts "echo '$B64' | base64 -d > /var/tmp/corpus_sizer_rest.py
    SA=$SA CONTAINER=$CONTAINER TAG=$TAG AZURE_STORAGE_SAS=\$(echo '$SAS_B64' | base64 -d) \
      nohup python3 /var/tmp/corpus_sizer_rest.py > /var/tmp/$TAG.stdout 2>&1 </dev/null &
    echo pid=\$!; sleep 8; tail -6 /var/tmp/$TAG.log" \
  --query "value[0].message" -o tsv
```

## Step 4 — Watch to completion (instant polls + local waits)
Loop *locally*; each poll is an instant VM script (no VM-side sleep). Keep each
Bash call under its 10-min timeout; relaunch across turns for huge containers.
```bash
for i in $(seq 1 9); do
  OUT=$(az vm run-command invoke -g "$VM_RG" -n "$VM" --command-id RunShellScript \
    --scripts "ls /var/tmp/$TAG.done 2>/dev/null && echo DONE || echo NOT-YET; tail -1 /var/tmp/$TAG.log; echo rows=\$(wc -l < /var/tmp/$TAG.sizes.tsv 2>/dev/null)" \
    --query "value[0].message" -o tsv 2>&1 | grep -E 'DONE|progress|rows=|big:' | tail -2)
  echo "[poll $i] $OUT"; echo "$OUT" | grep -q DONE && break; sleep 40
done
```
`Conflict`/"execution in progress" = another run-command still finishing — wait
~15s and retry; the background sizer keeps running regardless.

## Step 5 — Fetch report + error breakdown
```bash
az vm run-command invoke -g "$VM_RG" -n "$VM" --command-id RunShellScript \
  --scripts "cat /var/tmp/$TAG.summary; echo '=== ERRORS ==='; grep -c ' ERROR ' /var/tmp/$TAG.log; grep ' ERROR ' /var/tmp/$TAG.log | grep -oE '[A-Za-z]+Error|BadZipFile' | sort | uniq -c" \
  --query "value[0].message" -o tsv
```

## Step 6 — Cleanup
```bash
[ "$WE_ADDED_FW" = 1 ] && az storage account network-rule remove -g "$SA_RG" --account-name "$SA" --subnet "$SUBNET_ID" -o none
az vm run-command invoke -g "$VM_RG" -n "$VM" --command-id RunShellScript \
  --scripts "rm -f /var/tmp/$TAG.* /var/tmp/corpus_sizer_rest.py" --query "value[0].message" -o tsv
# SAS expires on its own (1 day).
```

## Interpreting the report
- **Ratio ≈ 1.0 across everything** = store-mode zips (bundled, not compressed —
  croplabel). The tell: total uncompressed slightly *below* compressed. Real.
- **High per-source ratios** (latchel slack 11×, hubspot 19×; webspiders code
  ~2×) = genuinely compressed exports. Also real — the zip CD reports true
  uncompressed sizes.
- **Top-level prefix may be an export *timestamp*** (latchel `20260707T180401Z`,
  a Google Takeout run) rather than a source name — the real sources are one
  level deeper. Say so in the report; optionally re-split on the 2nd path segment.
- **`.tar.gz` is trailer-floored** (fast): exact <4 GiB, and floored at
  compressed size above (so multi-GB DB-backup tarballs read as ~stored, a small
  undercount — the price of not streaming them for hours).
- **`BadZipFile` errors** = corrupt/mislabeled `.zip` files (common in scraped/
  backup trees); counted at stored size, negligible.
- **Units:** decimal GB (÷10⁹). The canonical `size_corpus.py` uses **GiB**
  (÷1024³, ~7% lower) — match that if comparing to older reports.

## Reconciling vs declared + presenting
- **Compare declared vs the `uncompressed` column** — the manifest declares the
  client's *logical* data size, so uncompressed is the like-for-like number
  (store-mode makes it moot — croplabel; heavy-compress makes it matter —
  webspiders `code` ~8×).
- **Skip services declared in record counts** (not bytes — not size-comparable),
  and **flag services declared `0 B` that actually hold data.**
- The manifest's stated **total** can exceed the dashboard's itemized byte sum
  (record-declared services + rounding) — use the manifest total for the headline.
- **Viz:** grouped horizontal bar, declared vs uncompressed, sorted by
  uncompressed desc. **Units — headline totals in TB when ≥1 TB; keep the
  per-source breakdown in GB** (most sources are sub-TB and read cleaner than
  `0.89 TB`). When sources span many orders of magnitude (latchel: 476 MB →
  2.6 TB) use a **log x-axis** so the small ones stay visible. See the
  croplabel / webspiders / latchel runs.

## Scripts
- `scripts/corpus_sizer_rest.py` — **default.** stdlib + SAS; any VM.
- `scripts/corpus_sizer.py` — SDK-based (needs `azure-storage-blob`); verify VMs.
- EXTRACT (unzip to blob) → `references/unzip-path.md`.
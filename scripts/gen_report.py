#!/usr/bin/env python3
"""gen_report.py — per-company HTML report (client-shareable).

companies/<slug>/reports/<date>.html — self-contained (inline CSS/SVG, no CDN,
no JS dependencies), micro1-branded, dark. Declared vs UNCOMPRESSED per source
(grouped horizontal bars, sorted by uncompressed desc, log x-axis when sources
span ≥3 orders of magnitude), headline % vs the manifest total, per-service
flags, delta + ETA, stall flag, and interpretation notes from sizing lore.

Units: headline totals in TB when ≥1 TB; per-source breakdown in GB (decimal,
÷10⁹ — NOT the older GiB convention). All math comes from reconcile.py.

  gen_report.py <slug> [--root companies] [--out path.html]
"""
from __future__ import annotations

import argparse
import html
import math
import sys
from pathlib import Path

import common
import reconcile

# micro1 design tokens (docs/foundations/* in the design-system repo).
# Outfit with system fallback: the report is self-contained (no font CDN).
CSS = """
:root {
  --surface-1:#121212; --surface-2:#191919; --surface-3:#313131;
  --white-100:rgba(255,255,255,1); --white-80:rgba(255,255,255,.8);
  --white-60:rgba(255,255,255,.6); --white-40:rgba(255,255,255,.4);
  --white-20:rgba(255,255,255,.2); --white-10:rgba(255,255,255,.1);
  --white-6:rgba(255,255,255,.06);
  --accent:#7065F0; --success:#47B85F; --warning:#E6B95F; --error:#EC6D6D;
  --success-10:rgba(71,184,95,.1); --warning-10:rgba(230,185,95,.1);
  --error-10:rgba(236,109,109,.1); --accent-10:rgba(112,101,240,.1);
}
* { box-sizing:border-box; margin:0; padding:0; }
body { background:var(--surface-1); color:var(--white-80);
  font-family:'Outfit',-apple-system,'Segoe UI',Roboto,sans-serif;
  font-size:16px; line-height:1.5; padding:32px 24px; }
.wrap { max-width:960px; margin:0 auto; }
header { display:flex; justify-content:space-between; align-items:baseline;
  margin-bottom:24px; flex-wrap:wrap; gap:8px; }
.brand { font-weight:600; color:var(--white-100); font-size:20px; }
.brand span { color:var(--accent); }
h1 { font-size:28px; line-height:1.32; color:var(--white-100); font-weight:600; }
h2 { font-size:20px; line-height:1.4; color:var(--white-100); font-weight:600;
  margin:32px 0 12px; }
.meta { color:var(--white-40); font-size:14px; }
.cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr));
  gap:12px; margin:16px 0 8px; }
.card { background:var(--surface-2); border-radius:8px; padding:16px; }
.card .label { font-size:12px; line-height:1.4; color:var(--white-40);
  text-transform:none; margin-bottom:4px; }
.card .value { font-size:28px; line-height:1.32; color:var(--white-100);
  font-weight:600; }
.card .sub { font-size:12px; color:var(--white-40); margin-top:4px; }
.badge { display:inline-block; font-size:12px; line-height:1.4; padding:2px 8px;
  border-radius:4px; font-weight:500; vertical-align:middle; }
.badge.ok { background:var(--success-10); color:var(--success); }
.badge.warn { background:var(--warning-10); color:var(--warning); }
.badge.err { background:var(--error-10); color:var(--error); }
.badge.info { background:var(--accent-10); color:var(--accent); }
.progress { height:8px; background:var(--white-10); border-radius:4px;
  overflow:hidden; margin-top:8px; }
.progress > div { height:100%; background:var(--accent); border-radius:4px; }
table { width:100%; border-collapse:collapse; background:var(--surface-2);
  border-radius:8px; overflow:hidden; font-size:14px; }
th { text-align:left; color:var(--white-40); font-weight:500; font-size:12px;
  padding:12px 16px; border-bottom:1px solid var(--white-6); }
td { padding:12px 16px; border-bottom:1px solid var(--white-6);
  color:var(--white-80); }
tr:last-child td { border-bottom:none; }
td.num, th.num { text-align:right; font-variant-numeric:tabular-nums; }
.chart { background:var(--surface-2); border-radius:8px; padding:16px; }
.legend { display:flex; gap:16px; font-size:12px; color:var(--white-60);
  margin-bottom:8px; }
.legend i { display:inline-block; width:12px; height:12px; border-radius:2px;
  margin-right:4px; vertical-align:-1px; }
ul.notes { list-style:none; }
ul.notes li { background:var(--surface-2); border-left:2px solid var(--accent);
  border-radius:4px; padding:12px 16px; margin-bottom:8px; font-size:14px;
  color:var(--white-60); }
footer { margin-top:48px; color:var(--white-20); font-size:12px; }
"""

DECLARED_COLOR = "rgba(255,255,255,.4)"
ACTUAL_COLOR = "#7065F0"


def esc(s) -> str:
    return html.escape(str(s))


def badge(text: str, kind: str) -> str:
    return f'<span class="badge {kind}">{esc(text)}</span>'


FLAG_BADGES = {
    "record-count": ("record count", "info"),
    "declared-empty": ("declared, no data yet", "err"),
    "zero-declared-has-data": ("declared 0 B, has data", "warn"),
    "overshoot": ("over 100%", "warn"),
    "found-embedded": ("embedded in another source", "info"),
    "deduplicated": ("duplicate share excluded", "info"),
}


def bar_chart(rows: list[dict], unexpected: list[str], sources: dict) -> str:
    """Grouped horizontal bars, declared vs uncompressed, as inline SVG."""
    entries = []
    for r in rows:
        if r["declared_records"] is not None:
            continue  # record-count services: listed in the table, not the chart
        entries.append((r["service"], r["declared_bytes"] or 0, r["actual_bytes"]))
    for s in unexpected:
        entries.append((s + " *", 0, sources[s]["uncompressed_bytes"]))
    entries.sort(key=lambda e: -e[2])
    if not entries:
        return '<p class="meta">No byte-declared services to chart.</p>'

    vals = [v for e in entries for v in e[1:] if v > 0]
    if not vals:
        return '<p class="meta">No nonzero sizes to chart yet.</p>'
    vmax = max(vals)
    vmin = min(vals)
    log_scale = vmin > 0 and vmax / vmin >= 1000  # spans ≥3 orders of magnitude
    floor = vmin / 2 if log_scale else 0

    label_w, chart_w, bar_h, gap, group_gap = 260, 560, 12, 2, 14
    group_h = bar_h * 2 + gap
    height = len(entries) * (group_h + group_gap) + 8

    def width(v):
        if v <= 0:
            return 0
        if log_scale:
            return max(2, chart_w * (math.log10(v / floor) / math.log10(vmax / floor)))
        return max(2, chart_w * v / vmax)

    parts = [f'<svg viewBox="0 0 {label_w + chart_w + 90} {height}" '
             f'xmlns="http://www.w3.org/2000/svg" role="img" '
             f'style="width:100%;height:auto;font-family:inherit">']
    y = 4
    for name, decl, act in entries:
        ty = y + group_h / 2 + 4
        parts.append(f'<text x="{label_w - 8}" y="{ty}" text-anchor="end" '
                     f'font-size="13" fill="rgba(255,255,255,.8)">{esc(name)}</text>')
        for i, (v, color) in enumerate(((decl, DECLARED_COLOR), (act, ACTUAL_COLOR))):
            by = y + i * (bar_h + gap)
            w = width(v)
            parts.append(f'<rect x="{label_w}" y="{by}" width="{w:.1f}" '
                         f'height="{bar_h}" rx="2" fill="{color}"/>')
            label = common.human_bytes(v) if v > 0 else ("—" if i == 0 else "0")
            parts.append(f'<text x="{label_w + w + 6:.1f}" y="{by + bar_h - 2}" '
                         f'font-size="11" fill="rgba(255,255,255,.4)">{esc(label)}</text>')
        y += group_h + group_gap
    parts.append("</svg>")
    scale_note = (" · log scale (sources span orders of magnitude)"
                  if log_scale else "")
    legend = (f'<div class="legend"><span><i style="background:{DECLARED_COLOR}">'
              f'</i>declared</span><span><i style="background:{ACTUAL_COLOR}"></i>'
              f'uncompressed (actual)</span><span class="meta">'
              f'* not in manifest{scale_note}</span></div>')
    return f'<div class="chart">{legend}{"".join(parts)}</div>'


def service_table(rows: list[dict], unexpected: list[str], sources: dict) -> str:
    out = ['<table><tr><th>Service</th><th class="num">Declared</th>'
           '<th class="num">Uncompressed (actual)</th><th class="num">%</th>'
           '<th>Flags</th></tr>']
    for r in rows:
        decl = (f'{r["declared_records"]:,} records'
                if r["declared_records"] is not None
                else common.human_bytes(r["declared_bytes"]))
        pct = f'{r["pct"]:.1f}%' if r["pct"] is not None else "—"
        flags = " ".join(badge(*FLAG_BADGES[f]) for f in r["flags"]) or ""
        out.append(f'<tr><td>{esc(r["service"])}</td>'
                   f'<td class="num">{esc(decl)}</td>'
                   f'<td class="num">{common.human_bytes(r["actual_bytes"])}</td>'
                   f'<td class="num">{pct}</td><td>{flags}</td></tr>')
    for s in unexpected:
        out.append(f'<tr><td>{esc(s)}</td><td class="num">—</td>'
                   f'<td class="num">'
                   f'{common.human_bytes(sources[s]["uncompressed_bytes"])}</td>'
                   f'<td class="num">—</td>'
                   f'<td>{badge("not in manifest", "warn")}</td></tr>')
    out.append("</table>")
    return "".join(out)


L2_BREAKDOWN_MIN = 50_000_000_000  # break down undeclared sources ≥50 GB


def unexpected_breakdown(unexpected: list[str], run: dict | None,
                         sources: dict) -> str:
    """For each large not-in-manifest source, a folder-level composition table
    from the run's sources_l2 — answers "what actually IS this prefix?"."""
    l2 = (run or {}).get("sources_l2") or {}
    sections = []
    for s in unexpected:
        total_unc = sources[s]["uncompressed_bytes"]
        if total_unc < L2_BREAKDOWN_MIN:
            continue
        children = sorted(((k[len(s) + 1:], v) for k, v in l2.items()
                           if k.startswith(s + "/")), key=lambda kv: -kv[1][2])
        if not children:
            continue
        rows = ['<table><tr><th>Folder</th><th class="num">Files</th>'
                '<th class="num">Uncompressed</th></tr>']
        seen_unc = 0
        for child, (files, _comp, unc) in children:
            seen_unc += unc
            rows.append(f'<tr><td>{esc(child)}</td>'
                        f'<td class="num">{files:,}</td>'
                        f'<td class="num">{common.human_bytes(unc)}</td></tr>')
        rest = total_unc - seen_unc
        if rest > 0:
            rows.append(f'<tr><td class="meta">(everything else)</td>'
                        f'<td class="num"></td>'
                        f'<td class="num">{common.human_bytes(rest)}</td></tr>')
        rows.append("</table>")
        sections.append(f'<h2>Inside {esc(s)} '
                        f'{badge("not in manifest", "warn")}</h2>'
                        + "".join(rows))
    return "".join(sections)


def build_html(s: dict) -> str:
    slug = s["slug"]
    run = s["latest_run"]
    pct = s["pct_complete"]
    stage = s["status"].get("stage", "pushing")

    header_badges = []
    if stage == "complete":
        header_badges.append(badge("complete", "ok"))
    elif s["stalled"]:
        header_badges.append(badge(
            f"stalled · no growth in {s['days_since_change']:.0f} days", "err"))
    if not s["expected_confirmed"]:
        header_badges.append(badge("manifest figures unconfirmed", "warn"))

    kpis = []
    pct_txt = f"{pct:.1f}%" if pct is not None else "—"
    bar = (f'<div class="progress"><div style="width:{min(pct or 0, 100):.1f}%">'
           f'</div></div>' if pct is not None else "")
    kpis.append(f'<div class="card"><div class="label">Complete vs manifest'
                f'</div><div class="value">{pct_txt}</div>{bar}</div>')
    excl_bits = ([f'{common.human_bytes(s["duplicate_bytes"])} duplicate data']
                 if s.get("duplicate_bytes") else []) + \
                ([f'{common.human_bytes(s["excluded_bytes"])} non-corpus data']
                 if s.get("excluded_bytes") else [])
    kpis.append(f'<div class="card"><div class="label">Received (uncompressed)'
                f'</div><div class="value">'
                f'{common.human_bytes(s["uncompressed_total"])}</div>'
                f'<div class="sub">compressed in storage: '
                f'{common.human_bytes(run["totals"]["compressed_bytes"]) if run else "—"}'
                + (f'<br>{" + ".join(excl_bits)} excluded (raw '
                   f'{common.human_bytes(s["uncompressed_total_raw"])})'
                   if excl_bits else "")
                + f'</div></div>')
    kpis.append(f'<div class="card"><div class="label">Declared total (manifest)'
                f'</div><div class="value">'
                f'{common.human_bytes(s["manifest_total_bytes"])}</div></div>')
    rate = s["rate_bytes_per_day"]
    eta_txt = s["eta"][:10] if s["eta"] else "—"
    rate_txt = f'{common.human_bytes(rate)}/day' if rate and rate > 0 else "—"
    kpis.append(f'<div class="card"><div class="label">Projected completion'
                f'</div><div class="value">{esc(eta_txt)}</div>'
                f'<div class="sub">rate: {rate_txt}</div></div>')

    run_meta = "no sizing runs yet"
    if run:
        run_meta = (f'sized {esc(run["timestamp"][:10])} · '
                    f'{run["totals"]["blob_count"]:,} blobs · '
                    f'{run["errors"]["total"]} sizing errors')
        if run.get("method") == "copied-forward":
            run_meta += " · unchanged since last full sizing (copied forward)"

    notes_html = ""
    if s["notes"]:
        notes_html = ("<h2>Reading these numbers</h2><ul class=\"notes\">"
                      + "".join(f"<li>{esc(n)}</li>" for n in s["notes"])
                      + "</ul>")

    return f"""<!-- generated by cdp-harness gen_report.py -->
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(slug)} data transfer progress</title>
<style>{CSS}</style>
<div class="wrap">
<header>
  <div>
    <div class="brand">micro<span>1</span></div>
    <h1>{esc(slug)} · data transfer progress</h1>
    <div class="meta">{run_meta}</div>
  </div>
  <div>{' '.join(header_badges)}</div>
</header>
<div class="cards">{''.join(kpis)}</div>
<h2>Declared vs received, per source</h2>
{bar_chart(s["service_rows"], s["unexpected_sources"], s["sources"])}
<h2>Per-service detail</h2>
{service_table(s["service_rows"], s["unexpected_sources"], s["sources"])}
{unexpected_breakdown(s["unexpected_sources"], run, s["sources"])}
{notes_html}
<footer>Generated {common.iso_now()[:10]} by micro1 · sizes are decimal
(GB = 10⁹ bytes) · comparison column is uncompressed logical size</footer>
</div>
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("slug")
    ap.add_argument("--root", type=Path, default=common.DEFAULT_COMPANIES_ROOT)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()
    try:
        s = reconcile.company_summary(args.root, args.slug)
    except FileNotFoundError as exc:
        print(f"{args.slug}: not onboarded ({exc})", file=sys.stderr)
        return 1
    out = args.out or (common.company_dir(args.root, args.slug) / "reports"
                       / f"{common.utc_now():%Y-%m-%d}.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_html(s))
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())

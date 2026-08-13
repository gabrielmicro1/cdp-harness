#!/usr/bin/env python3
"""gen_dashboard.py — fleet dashboard → reports/index.html.

The FDE's working view (per-company reports are the client-shareable ones):
one row per company with progress bar, % complete, 24h delta, ETA, stage,
stall/failure badges, and a link to the latest per-company report. Also prints
a machine-readable fleet summary JSON on stdout for daily-brief.

  gen_dashboard.py [--root companies] [--out reports/index.html]
"""
from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path

import common
import reconcile

CSS = """
:root { --s1:#121212; --s2:#191919; --w80:rgba(255,255,255,.8);
  --w60:rgba(255,255,255,.6); --w40:rgba(255,255,255,.4);
  --w10:rgba(255,255,255,.1); --w6:rgba(255,255,255,.06);
  --accent:#7065F0; --ok:#47B85F; --warn:#E6B95F; --err:#EC6D6D; }
* { box-sizing:border-box; margin:0; }
body { background:var(--s1); color:var(--w80); padding:32px 24px;
  font-family:'Outfit',-apple-system,'Segoe UI',Roboto,sans-serif; font-size:14px; }
.wrap { max-width:1200px; margin:0 auto; }
h1 { color:#fff; font-size:28px; font-weight:600; margin-bottom:4px; }
.meta { color:var(--w40); font-size:12px; margin-bottom:24px; }
.fleet { background:var(--s2); border-radius:8px; padding:16px; margin-bottom:16px;
  display:flex; gap:32px; align-items:center; flex-wrap:wrap; }
.fleet .big { font-size:32px; color:#fff; font-weight:600; }
.fleet .lbl { font-size:12px; color:var(--w40); }
table { width:100%; border-collapse:collapse; background:var(--s2);
  border-radius:8px; overflow:hidden; }
th { text-align:left; color:var(--w40); font-size:12px; font-weight:500;
  padding:10px 12px; border-bottom:1px solid var(--w6); }
td { padding:10px 12px; border-bottom:1px solid var(--w6); }
tr:last-child td { border-bottom:none; }
td.num, th.num { text-align:right; font-variant-numeric:tabular-nums; }
a { color:var(--accent); text-decoration:none; }
a:hover { text-decoration:underline; }
.bar { width:120px; height:6px; background:var(--w10); border-radius:4px;
  display:inline-block; overflow:hidden; vertical-align:middle; margin-right:8px; }
.bar > i { display:block; height:100%; background:var(--accent); }
.badge { font-size:11px; padding:1px 6px; border-radius:4px; white-space:nowrap; }
.b-ok { background:rgba(71,184,95,.1); color:var(--ok); }
.b-warn { background:rgba(230,185,95,.1); color:var(--warn); }
.b-err { background:rgba(236,109,109,.1); color:var(--err); }
.b-info { background:rgba(112,101,240,.1); color:var(--accent); }
.up { color:var(--ok); } .flat { color:var(--w40); }
"""

STAGE_BADGE = {"onboarding": "b-info", "pushing": "b-info", "stalled": "b-err",
               "verifying": "b-warn", "complete": "b-ok"}


def esc(s) -> str:
    return html.escape(str(s))


def latest_report_link(root: Path, slug: str) -> str | None:
    d = common.company_dir(root, slug) / "reports"
    if not d.is_dir():
        return None
    reports = sorted(d.glob("*.html"))
    if not reports:
        return None
    # index.html lives in reports/ at repo root; company reports one level over
    return f"../companies/{slug}/reports/{reports[-1].name}"


def build(root: Path):
    slugs = common.list_companies(root)
    rows = []
    for slug in slugs:
        try:
            s = reconcile.company_summary(root, slug)
            rows.append((slug, s, None))
        except Exception as exc:  # noqa: BLE001 — isolation: keep the dashboard alive
            rows.append((slug, None, str(exc)))

    total_decl = sum(s["manifest_total_bytes"] or 0 for _, s, _ in rows if s)
    total_unc = sum(s["uncompressed_total"] or 0
                    for _, s, _ in rows if s and s["manifest_total_bytes"])
    fleet_pct = total_unc / total_decl * 100 if total_decl else None

    body = ['<table><tr><th>Company</th><th>Stage</th><th>Progress</th>'
            '<th class="num">Received</th><th class="num">Δ 24h</th>'
            '<th class="num">ETA</th><th>Last run</th><th></th></tr>']
    summary = {"fleet_pct": fleet_pct, "total_declared": total_decl,
               "total_uncompressed": total_unc, "companies": []}
    for slug, s, err in rows:
        if s is None:
            body.append(f'<tr><td>{esc(slug)}</td><td colspan="6">'
                        f'<span class="badge b-err">summary failed: {esc(err)}'
                        f'</span></td><td></td></tr>')
            summary["companies"].append({"slug": slug, "error": err})
            continue
        stage = s["status"].get("stage", "pushing")
        badges = [f'<span class="badge {STAGE_BADGE.get(stage, "b-info")}">'
                  f'{esc(stage)}</span>']
        lr = s["status"].get("last_run", {})
        if lr.get("outcome") == "failed":
            badges.append(f'<span class="badge b-err" title="{esc(lr.get("reason"))}"'
                          f'>run failed</span>')
        if not s["config"].get("vm", {}).get("exists", False):
            badges.append('<span class="badge b-err">no VM</span>')
        if s["stalled"] and stage != "stalled":
            badges.append('<span class="badge b-err">stalled</span>')
        if not s["expected_confirmed"]:
            badges.append('<span class="badge b-warn">manifest unconfirmed</span>')

        pct = s["pct_complete"]
        pct_txt = f"{pct:.1f}%" if pct is not None else "—"
        bar = (f'<span class="bar"><i style="width:{min(pct or 0, 100):.0f}%">'
               f'</i></span>{pct_txt}')
        d24 = s["delta_24h"]
        d24_txt = ('<span class="flat">—</span>' if d24 is None else
                   f'<span class="up">+{common.human_bytes(d24)}</span>' if d24 > 0
                   else '<span class="flat">no change</span>')
        eta = s["eta"][:10] if s["eta"] else "—"
        run = s["latest_run"]
        last = (f'{esc(run["timestamp"][:10])} · {esc(run["method"])}'
                if run else "never")
        link = latest_report_link(root, slug)
        link_html = f'<a href="{esc(link)}">report</a>' if link else ""
        body.append(f'<tr><td>{esc(slug)}</td><td>{" ".join(badges)}</td>'
                    f'<td>{bar}</td>'
                    f'<td class="num">{common.human_bytes(s["uncompressed_total"])}</td>'
                    f'<td class="num">{d24_txt}</td><td class="num">{esc(eta)}</td>'
                    f'<td>{last}</td><td>{link_html}</td></tr>')
        summary["companies"].append({
            "slug": slug, "stage": stage, "pct_complete": pct,
            "uncompressed_total": s["uncompressed_total"],
            "delta_24h": d24, "eta": s["eta"], "stalled": s["stalled"],
            "last_outcome": lr.get("outcome"), "last_reason": lr.get("reason"),
            "no_vm": not s["config"].get("vm", {}).get("exists", False),
        })
    body.append("</table>")

    fleet_pct_txt = f"{fleet_pct:.1f}%" if fleet_pct is not None else "—"
    page = f"""<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Corpus fleet dashboard</title>
<style>{CSS}</style>
<div class="wrap">
<h1>Corpus fleet</h1>
<div class="meta">generated {common.iso_now()} · internal view</div>
<div class="fleet">
  <div><div class="big">{fleet_pct_txt}</div><div class="lbl">fleet complete</div></div>
  <div><div class="big">{common.human_bytes(total_unc)}</div>
       <div class="lbl">received (uncompressed)</div></div>
  <div><div class="big">{common.human_bytes(total_decl)}</div>
       <div class="lbl">declared across manifests</div></div>
  <div><div class="big">{len(slugs)}</div><div class="lbl">companies</div></div>
</div>
{''.join(body)}
</div>
"""
    return page, summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", type=Path, default=common.DEFAULT_COMPANIES_ROOT)
    ap.add_argument("--out", type=Path,
                    default=common.REPO_ROOT / "reports" / "index.html")
    args = ap.parse_args()
    page, summary = build(args.root)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(page)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

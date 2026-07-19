"""
AGENT_H.dashboard — Dashboards & Visualisation (T74)
=====================================================

Self-contained HTML dashboards over the AVA run reports. Closes the
"automated dashboards" gap in taxonomy levels 16 and 20 (roadmap 🟡).

Audiences
---------
- **Executive** — one screen: overall health band, confidence, blocker count,
  module scorecards, trend sparkline. No jargon.
- **Engineer** — per-agent violation tables, suspiciousness ranking, drill-down.
- **Regression** — pass rate, flakiness, makespan, cost saving.
- **Coverage** — bins hit/total per group, holes, heatmap.
- **Bug** — severity mix, root-cause breakdown, reopen risk.
- **Failure** — clusters, trends, Sankey of failure propagation.

Visualisations (all inline SVG, no JS libraries, no CDN)
---------------------------------------------------------
- `sparkline()` — historical trend line.
- `heatmap()` — module × metric grid with a perceptually ordered colour ramp.
- `sankey()` — failure propagation / workflow flow diagram with proportional
  link widths.
- `scorecard()` — per-module health card (grade A–F from a weighted score).
- Interactive **drill-down** uses native `<details>` elements, so it works in
  any browser with zero dependencies and degrades gracefully when printed.

Everything is generated as a single standalone `.html` file with inline CSS —
safe to email, archive next to the run, or open from a network share.
"""

from __future__ import annotations

import html
import json
import logging
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

log = logging.getLogger("AGENT_H.dashboard")

SCHEMA_VERSION = "2.1.0"
AGENT_NAME = "dashboard"

_BAND_COLOUR = {
    "VERIFIED": "#1a7f37", "CLEAN": "#1a7f37", "HIGH": "#3fb950",
    "MEDIUM": "#d29922", "DEGRADED": "#d29922",
    "LOW": "#db6d28", "CRITICAL": "#cf222e",
}
_GRADE_BOUNDS = [(0.90, "A"), (0.80, "B"), (0.70, "C"), (0.55, "D"), (0.0, "F")]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _esc(x: Any) -> str:
    return html.escape(str(x), quote=True)


def grade(score: float) -> str:
    for bound, g in _GRADE_BOUNDS:
        if score >= bound:
            return g
    return "F"


# ─────────────────────────────────────────────────────────────────────────────
# SVG primitives
# ─────────────────────────────────────────────────────────────────────────────
def sparkline(values: Sequence[float], width: int = 240, height: int = 40,
              colour: str = "#3fb950") -> str:
    vals = [float(v) for v in (values or [])]
    if len(vals) < 2:
        return f'<svg width="{width}" height="{height}"></svg>'
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1.0
    step = width / (len(vals) - 1)
    pts = " ".join(
        f"{i * step:.1f},{height - ((v - lo) / rng) * (height - 4) - 2:.1f}"
        for i, v in enumerate(vals))
    return (f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
            f'role="img" aria-label="trend">'
            f'<polyline points="{pts}" fill="none" stroke="{colour}" '
            f'stroke-width="2" stroke-linejoin="round"/></svg>')


def _ramp(t: float) -> str:
    """Green -> amber -> red ramp for t in [0,1] (0 = good)."""
    t = max(0.0, min(1.0, t))
    if t < 0.5:
        f = t / 0.5
        r, g, b = int(26 + f * (210 - 26)), int(127 + f * (153 - 127)), int(55 + f * (34 - 55))
    else:
        f = (t - 0.5) / 0.5
        r, g, b = int(210 + f * (207 - 210)), int(153 + f * (34 - 153)), int(34 + f * (46 - 34))
    return f"rgb({r},{g},{b})"


def heatmap(rows: Sequence[str], cols: Sequence[str],
            values: Dict[Tuple[str, str], float],
            cell: int = 34) -> str:
    """rows × cols grid; ``values`` in [0,1] where 0 is healthy."""
    if not rows or not cols:
        return ""
    lm, tm = 150, 70
    w = lm + len(cols) * cell + 10
    h = tm + len(rows) * cell + 10
    out = [f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" role="img" '
           f'aria-label="heatmap">']
    for j, c in enumerate(cols):
        x = lm + j * cell + cell / 2
        out.append(f'<text x="{x}" y="{tm - 8}" font-size="10" fill="#8b949e" '
                   f'text-anchor="end" transform="rotate(-45 {x} {tm - 8})">'
                   f'{_esc(c)}</text>')
    for i, r in enumerate(rows):
        y = tm + i * cell
        out.append(f'<text x="{lm - 8}" y="{y + cell * 0.65}" font-size="11" '
                   f'fill="#c9d1d9" text-anchor="end">{_esc(r)}</text>')
        for j, c in enumerate(cols):
            v = float(values.get((r, c), 0.0) or 0.0)
            x = lm + j * cell
            out.append(
                f'<rect x="{x}" y="{y}" width="{cell - 3}" height="{cell - 3}" '
                f'rx="3" fill="{_ramp(v)}"><title>{_esc(r)} / {_esc(c)}: '
                f'{v:.2f}</title></rect>')
    out.append("</svg>")
    return "".join(out)


def sankey(flows: Sequence[Tuple[str, str, float]],
           width: int = 720, height: int = 320) -> str:
    """Two-stage Sankey: (source, target, value). Link width ∝ value."""
    flows = [(str(s), str(t), float(v)) for s, t, v in (flows or []) if v > 0]
    if not flows:
        return ""
    srcs, tgts = [], []
    for s, t, _ in flows:
        if s not in srcs:
            srcs.append(s)
        if t not in tgts:
            tgts.append(t)
    total = sum(v for _, _, v in flows) or 1.0
    pad, node_w = 8, 14
    lx, rx = 10, width - node_w - 10
    avail = height - pad * (max(len(srcs), len(tgts)) - 1 or 1)

    def spans(names: List[str]) -> Dict[str, Tuple[float, float]]:
        tot = {n: sum(v for s, t, v in flows if (s == n or t == n)) for n in names}
        # totals per side
        side = {n: sum(v for s, t, v in flows
                       if (s == n if names is srcs else t == n)) for n in names}
        acc, out = 0.0, {}
        for n in names:
            hgt = max(6.0, (side[n] / total) * avail)
            out[n] = (acc, hgt)
            acc += hgt + pad
        return out

    ls, rs = spans(srcs), spans(tgts)
    out = [f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
           f'role="img" aria-label="sankey">']
    off_l = {n: 0.0 for n in srcs}
    off_r = {n: 0.0 for n in tgts}
    for s, t, v in sorted(flows, key=lambda f: -f[2]):
        y0, h0 = ls[s]
        y1, h1 = rs[t]
        th = max(1.5, (v / total) * avail)
        a = y0 + off_l[s]
        b = y1 + off_r[t]
        off_l[s] += th
        off_r[t] += th
        mid = (lx + node_w + rx) / 2
        out.append(
            f'<path d="M{lx + node_w},{a + th / 2} C{mid},{a + th / 2} '
            f'{mid},{b + th / 2} {rx},{b + th / 2}" stroke="#58a6ff" '
            f'stroke-opacity="0.35" stroke-width="{th:.1f}" fill="none">'
            f'<title>{_esc(s)} → {_esc(t)}: {v:g}</title></path>')
    for n in srcs:
        y, h = ls[n]
        out.append(f'<rect x="{lx}" y="{y}" width="{node_w}" height="{h:.1f}" '
                   f'rx="3" fill="#3fb950"/>'
                   f'<text x="{lx + node_w + 6}" y="{y + h / 2 + 4}" font-size="11" '
                   f'fill="#c9d1d9">{_esc(n)}</text>')
    for n in tgts:
        y, h = rs[n]
        out.append(f'<rect x="{rx}" y="{y}" width="{node_w}" height="{h:.1f}" '
                   f'rx="3" fill="#cf222e"/>'
                   f'<text x="{rx - 6}" y="{y + h / 2 + 4}" font-size="11" '
                   f'fill="#c9d1d9" text-anchor="end">{_esc(n)}</text>')
    out.append("</svg>")
    return "".join(out)


def scorecard(module: str, score: float, detail: Dict[str, Any]) -> str:
    g = grade(score)
    colour = {"A": "#1a7f37", "B": "#3fb950", "C": "#d29922",
              "D": "#db6d28", "F": "#cf222e"}[g]
    bits = " · ".join(f"{_esc(k)}: {_esc(v)}" for k, v in list(detail.items())[:4])
    return (f'<div class="card"><div class="grade" style="background:{colour}">'
            f'{g}</div><div><b>{_esc(module)}</b><div class="muted">{bits}</div>'
            f'<div class="muted">score {score:.2f}</div></div></div>')


# ─────────────────────────────────────────────────────────────────────────────
# Page assembly
# ─────────────────────────────────────────────────────────────────────────────
_CSS = """
:root{--bg:#0d1117;--fg:#c9d1d9;--mut:#8b949e;--pan:#161b22;--brd:#30363d}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
 font:14px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif}
header{padding:20px 24px;border-bottom:1px solid var(--brd)}
h1{margin:0;font-size:20px}h2{font-size:15px;margin:0 0 10px}
.sub{color:var(--mut);font-size:12px;margin-top:4px}
.wrap{padding:20px 24px;display:grid;gap:16px}
.panel{background:var(--pan);border:1px solid var(--brd);border-radius:8px;padding:16px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.kpi{background:var(--pan);border:1px solid var(--brd);border-radius:8px;padding:14px}
.kpi .v{font-size:24px;font-weight:600}
.kpi .l{color:var(--mut);font-size:12px;text-transform:uppercase;letter-spacing:.04em}
.badge{display:inline-block;padding:3px 10px;border-radius:999px;color:#fff;font-size:12px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--brd)}
th{color:var(--mut);font-weight:600;font-size:12px}
.card{display:flex;gap:12px;align-items:center;background:var(--pan);
 border:1px solid var(--brd);border-radius:8px;padding:12px}
.grade{width:38px;height:38px;border-radius:8px;color:#fff;font-weight:700;
 display:flex;align-items:center;justify-content:center;font-size:18px;flex:0 0 auto}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}
.muted{color:var(--mut);font-size:12px}
details{border:1px solid var(--brd);border-radius:6px;padding:8px 12px;margin:6px 0}
summary{cursor:pointer;font-weight:600}
code{background:#0d1117;padding:1px 5px;border-radius:4px;font-size:12px}
"""


def _kpi(label: str, value: Any, colour: Optional[str] = None) -> str:
    style = f' style="color:{colour}"' if colour else ""
    return (f'<div class="kpi"><div class="l">{_esc(label)}</div>'
            f'<div class="v"{style}>{_esc(value)}</div></div>')


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]],
           limit: int = 50) -> str:
    if not rows:
        return '<div class="muted">No data.</div>'
    head = "".join(f"<th>{_esc(h)}</th>" for h in headers)
    body = ""
    for r in list(rows)[:limit]:
        body += "<tr>" + "".join(f"<td>{_esc(c)}</td>" for c in r) + "</tr>"
    more = ("" if len(rows) <= limit else
            f'<div class="muted">… {len(rows) - limit} more rows</div>')
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>{more}"


class DashboardBuilder:
    """Builds standalone HTML dashboards from AVA report dicts."""

    def __init__(self, reports: Optional[Dict[str, Any]] = None,
                 title: str = "AVA Verification Dashboard"):
        self.reports = reports or {}
        self.title = title

    # ── derived data ───────────────────────────────────────────────────────
    def module_scores(self) -> Dict[str, Tuple[float, Dict[str, Any]]]:
        out: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        for name, rep in self.reports.items():
            if not isinstance(rep, dict):
                continue
            viol = int(rep.get("violations", rep.get("total_violations", 0)) or 0)
            band = str(rep.get("band", "CLEAN"))
            passed = bool(rep.get("pass", True))
            score = 1.0 if passed and viol == 0 else max(
                0.0, 1.0 - min(1.0, viol / 20.0) - (0.0 if passed else 0.25))
            out[name] = (score, {"violations": viol, "band": band,
                                 "pass": passed})
        return out

    def _header(self, subtitle: str) -> str:
        return (f"<header><h1>{_esc(self.title)}</h1>"
                f'<div class="sub">{_esc(subtitle)} · generated {_esc(_now())}'
                f"</div></header>")

    def _page(self, subtitle: str, body: str) -> str:
        return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
                f'<meta name="viewport" content="width=device-width,initial-scale=1">'
                f"<title>{_esc(self.title)}</title><style>{_CSS}</style></head>"
                f"<body>{self._header(subtitle)}<div class='wrap'>{body}</div>"
                f"</body></html>")

    # ── executive ──────────────────────────────────────────────────────────
    def executive(self, confidence: Optional[float] = None,
                  trend: Optional[Sequence[float]] = None) -> str:
        scores = self.module_scores()
        total_v = sum(d["violations"] for _, d in scores.values())
        failing = [m for m, (_, d) in scores.items() if not d["pass"]]
        overall = (sum(s for s, _ in scores.values()) / len(scores)) if scores else 1.0
        band = ("CRITICAL" if failing else
                ("VERIFIED" if overall >= 0.9 else "MEDIUM"))
        kpis = "".join([
            _kpi("Overall health", f"{overall * 100:.0f}%",
                 _BAND_COLOUR.get(band)),
            _kpi("Status", band, _BAND_COLOUR.get(band)),
            _kpi("Total violations", total_v),
            _kpi("Failing agents", len(failing),
                 "#cf222e" if failing else "#1a7f37"),
            _kpi("Confidence", f"{confidence:.2f}" if confidence is not None else "n/a"),
        ])
        cards = "".join(scorecard(m, s, d) for m, (s, d) in
                        sorted(scores.items(), key=lambda kv: kv[1][0])[:12])
        spark = (f'<div class="panel"><h2>Health trend</h2>'
                 f'{sparkline(trend or [])}</div>') if trend else ""
        return self._page("Executive summary", f"""
<div class="kpis">{kpis}</div>
{spark}
<div class="panel"><h2>Module health scorecards</h2>
<div class="cards">{cards or '<div class="muted">No agent reports.</div>'}</div>
</div>""")

    # ── engineer ───────────────────────────────────────────────────────────
    def engineer(self, localization: Optional[Sequence[Dict[str, Any]]] = None
                 ) -> str:
        blocks = []
        for name, rep in sorted(self.reports.items()):
            if not isinstance(rep, dict):
                continue
            viol = rep.get("violations", rep.get("total_violations", 0))
            band = rep.get("band", "CLEAN")
            colour = _BAND_COLOUR.get(str(band), "#8b949e")
            rows = [(v.get("check"), v.get("severity"),
                     str(v.get("detail", ""))[:160])
                    for v in (rep.get("violations_list") or [])]
            blocks.append(
                f"<details><summary>{_esc(name)} "
                f'<span class="badge" style="background:{colour}">{_esc(band)}'
                f"</span> · {_esc(viol)} violation(s)</summary>"
                f"{_table(['Check', 'Severity', 'Detail'], rows)}</details>")
        loc = ""
        if localization:
            rows = [(r.get("rank"), r.get("element"),
                     f"{r.get('suspiciousness', 0):.4f}",
                     r.get("failed_tests"), r.get("passed_tests"))
                    for r in localization[:25]]
            loc = ('<div class="panel"><h2>Suspicious modules '
                   '(Ochiai spectrum-based localization)</h2>'
                   + _table(["Rank", "Element", "Suspiciousness",
                             "Failed", "Passed"], rows) + "</div>")
        return self._page("Engineer view", f"""
{loc}
<div class="panel"><h2>Agent reports (click to drill down)</h2>
{''.join(blocks) or '<div class="muted">No agent reports.</div>'}</div>""")

    # ── regression ─────────────────────────────────────────────────────────
    def regression(self, health: Dict[str, Any],
                   cost: Optional[Dict[str, Any]] = None,
                   schedule_info: Optional[Dict[str, Any]] = None) -> str:
        health = health or {}
        cost = cost or {}
        pr = float(health.get("pass_rate", 0.0) or 0.0)
        kpis = "".join([
            _kpi("Pass rate", f"{pr * 100:.1f}%",
                 "#1a7f37" if pr >= 0.95 else "#d29922"),
            _kpi("Tests", health.get("total_tests", 0)),
            _kpi("Failed", health.get("failed", 0),
                 "#cf222e" if health.get("failed") else "#1a7f37"),
            _kpi("Flaky", health.get("flaky_count", 0)),
            _kpi("Total runtime", f"{health.get('total_runtime_s', 0):.0f}s"),
            _kpi("CPU saved", f"{cost.get('saved_pct', 0):.0f}%"),
        ])
        flaky_rows = [(f.get("test"), f"{f.get('flakiness', 0):.2f}",
                       f.get("runs")) for f in health.get("flaky_tests", [])]
        sched = ""
        if schedule_info:
            rows = [(w, len(t), ", ".join(map(str, t))[:80])
                    for w, t in (schedule_info.get("assignment") or {}).items()]
            sched = ('<div class="panel"><h2>Schedule (LPT, makespan '
                     f"{schedule_info.get('makespan_s', 0):.1f}s)</h2>"
                     + _table(["Worker", "Tests", "Assignment"], rows) + "</div>")
        return self._page("Regression view", f"""
<div class="kpis">{kpis}</div>
<div class="panel"><h2>Flaky tests (result flips, not consistent failures)</h2>
{_table(['Test', 'Flakiness', 'Runs'], flaky_rows)}</div>
{sched}""")

    # ── coverage ───────────────────────────────────────────────────────────
    def coverage(self, summary: Dict[str, Any]) -> str:
        summary = summary or {}
        bins = summary.get("bins", {}) or {}
        rows, hm_rows, hm_vals = [], [], {}
        for group, d in sorted(bins.items()):
            hit = int((d or {}).get("hit", 0) or 0)
            tot = int((d or {}).get("total", 0) or 0) or 1
            pct = hit / tot
            rows.append((group, hit, tot, f"{pct * 100:.1f}%"))
            hm_rows.append(group)
            hm_vals[(group, "coverage")] = 1.0 - pct
            hm_vals[(group, "holes")] = min(1.0, (tot - hit) / tot)
        hm = heatmap(hm_rows, ["coverage", "holes"], hm_vals) if hm_rows else ""
        holes = [(h.get("bin", h) if isinstance(h, dict) else h,)
                 for h in (summary.get("holes") or [])]
        return self._page("Coverage view", f"""
<div class="kpis">
{_kpi('Overall coverage', f"{float(summary.get('overall', 0) or 0) * 100:.1f}%")}
{_kpi('Bin groups', len(bins))}
{_kpi('Holes', len(summary.get('holes') or []))}</div>
<div class="panel"><h2>Coverage by group</h2>
{_table(['Group', 'Hit', 'Total', 'Coverage'], rows)}</div>
<div class="panel"><h2>Heatmap (darker = worse)</h2>{hm}</div>
<div class="panel"><h2>Holes</h2>{_table(['Bin'], holes)}</div>""")

    # ── bug ────────────────────────────────────────────────────────────────
    def bug(self, bug_report: Dict[str, Any]) -> str:
        bug_report = bug_report or {}
        m = bug_report.get("metrics", {}) or {}
        rc = m.get("root_cause_breakdown", {}) or {}
        rows = []
        for b in (bug_report.get("bugs") or [])[:50]:
            rows.append((
                b.get("module"), b.get("check"),
                (b.get("severity") or {}).get("severity"),
                (b.get("root_cause") or {}).get("root_cause"),
                f"{(b.get('reopen') or {}).get('reopen_probability', 0):.2f}",
                (b.get("lifetime") or {}).get("estimated_days"),
            ))
        flows = [("failures", k, float(v)) for k, v in rc.items() if v]
        return self._page("Bug view", f"""
<div class="kpis">
{_kpi('Bugs analysed', m.get('bugs_analysed', 0))}
{_kpi('Critical', m.get('critical_bugs', 0), '#cf222e' if m.get('critical_bugs') else '#1a7f37')}
{_kpi('Top suspect', m.get('top_suspect') or 'n/a')}</div>
<div class="panel"><h2>Root-cause distribution</h2>{sankey(flows)}</div>
<div class="panel"><h2>Bugs</h2>{_table(
    ['Module', 'Check', 'Severity', 'Root cause', 'P(reopen)', 'Est. days'], rows)}
</div>""")

    # ── failure ────────────────────────────────────────────────────────────
    def failure(self, analytics: Dict[str, Any]) -> str:
        analytics = analytics or {}
        m = analytics.get("metrics", {}) or {}
        clusters = analytics.get("clusters") or []
        rows = [(c.get("rank"), c.get("cluster_id"), c.get("size"),
                 c.get("severity"), "yes" if c.get("regression_blocker") else "",
                 str(c.get("representative", ""))[:110]) for c in clusters[:40]]
        trend = (analytics.get("trends") or {}).get("summary", {}) or {}
        flows = [(k, "clusters", float(v)) for k, v in trend.items() if v]
        return self._page("Failure view", f"""
<div class="kpis">
{_kpi('Total failures', analytics.get('total_failures', 0))}
{_kpi('Clusters', m.get('clusters', 0))}
{_kpi('Unique', m.get('unique_failures', 0))}
{_kpi('Dedup ratio', f"{float(m.get('dedup_ratio', 0) or 0) * 100:.0f}%")}
{_kpi('Blockers', m.get('regression_blockers', 0),
      '#cf222e' if m.get('regression_blockers') else '#1a7f37')}</div>
<div class="panel"><h2>Failure trend flow</h2>{sankey(flows)}</div>
<div class="panel"><h2>Clusters (ranked)</h2>{_table(
    ['#', 'Cluster', 'Size', 'Severity', 'Blocker', 'Representative'], rows)}</div>""")


# ─────────────────────────────────────────────────────────────────────────────
# Entry points
# ─────────────────────────────────────────────────────────────────────────────
def write_dashboards(out_dir: Path, reports: Dict[str, Any],
                     failure_analytics: Optional[Dict[str, Any]] = None,
                     bug_report: Optional[Dict[str, Any]] = None,
                     regression: Optional[Dict[str, Any]] = None,
                     coverage_summary: Optional[Dict[str, Any]] = None,
                     confidence: Optional[float] = None,
                     trend: Optional[Sequence[float]] = None) -> List[str]:
    """Write every applicable dashboard; returns the list of files written."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    b = DashboardBuilder(reports)
    written: List[str] = []

    def _w(name: str, content: str) -> None:
        p = out_dir / name
        try:
            p.write_text(content, encoding="utf-8")
            written.append(str(p))
        except OSError as exc:
            log.warning("dashboard: cannot write %s: %s", name, exc)

    _w("dashboard_executive.html", b.executive(confidence, trend))
    loc = (bug_report or {}).get("localization")
    _w("dashboard_engineer.html", b.engineer(loc))
    if regression:
        _w("dashboard_regression.html",
           b.regression(regression.get("health", {}), regression.get("cost"),
                        (regression.get("plan") or {}).get("schedule")))
    if coverage_summary:
        _w("dashboard_coverage.html", b.coverage(coverage_summary))
    if bug_report:
        _w("dashboard_bug.html", b.bug(bug_report))
    if failure_analytics:
        _w("dashboard_failure.html", b.failure(failure_analytics))
    return written


def run_from_manifest(manifest_path: str) -> int:
    mp = Path(manifest_path)
    try:
        manifest = json.loads(mp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("dashboard: cannot read manifest: %s", exc)
        return 0
    run_dir = Path(manifest.get("run_dir", mp.parent))

    def _load(name: str) -> Optional[Dict[str, Any]]:
        p = run_dir / name
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    reports = manifest.get("reports") or {}
    if not reports:
        vr = _load("verification_report.json") or {}
        reports = vr.get("reports", {}) or {}
    written = write_dashboards(
        run_dir, reports,
        _load("failure_analytics_report.json"),
        _load("bug_intelligence_report.json"),
        _load("regression_intelligence_report.json"),
        _load("coverage_summary.json"),
        manifest.get("confidence"),
        manifest.get("health_trend"),
    )
    try:
        (run_dir / "dashboard_index.json").write_text(
            json.dumps({"schema_version": SCHEMA_VERSION, "agent": AGENT_NAME,
                        "generated_at": _now(), "files": written,
                        "status": "completed", "pass": True}, indent=2),
            encoding="utf-8")
    except OSError as exc:
        log.warning("dashboard: cannot write index: %s", exc)
    return 0


if __name__ == "__main__":  # pragma: no cover
    import argparse
    ap = argparse.ArgumentParser(description="AVA dashboard generator")
    ap.add_argument("--manifest", required=True)
    args = ap.parse_args()
    raise SystemExit(run_from_manifest(args.manifest))

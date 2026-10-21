"""
HTML desk view — the same book Slack posts, as a page you can open.

Renders a `ReportData` (see `report.py`) into one self-contained HTML file:
no server, no network, no JavaScript libraries. Open it with a browser, mail
it, or archive it as the month's report.

The rule this module lives by: it renders, it does not compute. Every figure
comes from `analytics`, so the page and the Slack digest can never disagree
about the book.
"""

from __future__ import annotations

import html
from datetime import UTC, date, datetime
from decimal import Decimal

from defi_tracker.report import ReportData, unclaimed_known

# Series slots from the validated categorical palette, in fixed order.
# Chain -> slot is stable so a chain never changes colour between runs.
_CHAIN_LABEL = {"peaq": "MachineX", "bsc": "PancakeSwap", "base": "Uniswap V3"}
_CHAIN_SLOT = {"peaq": 1, "bsc": 2, "base": 3}

# ── formatting ────────────────────────────────────────────────────────────


def _coverage(costs: dict) -> str:
    """' (12 of 239 tx)' when the cost base is partial, '' when it is whole.
    A total that covers a twentieth of the history must say so where it is
    read, not only in a footnote."""
    if costs["gas_is_complete"] or not costs["txs"]:
        return ""
    return f" ({costs['gas_known_txs']} of {costs['txs']} tx)"


def _usd(v: float | None, dp: int = 0) -> str:
    if v is None:
        return "—"
    return f"${v:,.{dp}f}" if v >= 0 else f"-${abs(v):,.{dp}f}"


def _k(v: float | None) -> str:
    """Compact money for tiles and chart labels."""
    if v is None:
        return "—"
    a = abs(v)
    sign = "-" if v < 0 else ""
    if a >= 1_000_000:
        return f"{sign}${a / 1_000_000:.2f}M"
    if a >= 1_000:
        return f"{sign}${a / 1_000:.1f}k"
    return f"{sign}${a:.0f}"


def _pct(v: float | None, dp: int = 1) -> str:
    return "—" if v is None else f"{v:.{dp}f}%"


def _e(s) -> str:
    return html.escape(str(s), quote=True)


def _link(url: str, label: str) -> str:
    """`label` linked to `url`; just nothing when no URL is configured."""
    return f'<a href="{_e(url)}">{_e(label)}</a>' if url else ""


def _sign_class(v: float | None) -> str:
    if v is None or v == 0:
        return "flat"
    return "up" if v > 0 else "down"


# V3-family ticks saturate at ±887272; a position pinned near those bounds is
# full-range, and 1.0001**443636 is not a percentage anyone can read.
_FULL_RANGE_TICK = 887_000
_WIDE_PCT = 999.0


def _range_pct(row: dict) -> str:
    """Half-width of the position's range, as ±%."""
    lo, hi = row.get("tick_lower"), row.get("tick_upper")
    if lo is None or hi is None:
        return "—"
    if lo <= -_FULL_RANGE_TICK and hi >= _FULL_RANGE_TICK:
        return "full"
    half = (hi - lo) / 2
    try:
        pct = (1.0001**half - 1) * 100
    except OverflowError:
        return "full"
    return f">±{_WIDE_PCT:.0f}%" if pct > _WIDE_PCT else f"±{pct:.0f}%"


# ── fee chart ─────────────────────────────────────────────────────────────


def _fee_chart(months: list[str], chains: list[str], cells: dict) -> str:
    """
    Stacked bars: claimed fees per accrual month, split by venue.

    Magnitude over time with a part-to-whole split, so: bars, one axis,
    stacked with a 2px surface gap between segments, hover per segment, and
    the table below doubles as the table view.
    """
    if not months:
        return '<p class="empty">No claimed fees yet.</p>'

    W, H = 760, 240
    pad_l, pad_b, pad_t = 8, 28, 22
    plot_h = H - pad_b - pad_t
    n = len(months)
    slot = (W - pad_l * 2) / n
    bar_w = min(38.0, slot * 0.62)
    peak = max(
        (sum(float(cells.get((m, c), 0)) for c in chains) for m in months),
        default=0.0,
    )
    if peak <= 0:
        return '<p class="empty">No claimed fees yet.</p>'

    bars: list[str] = []
    for i, m in enumerate(months):
        cx = pad_l + slot * (i + 0.5)
        x = cx - bar_w / 2
        total = sum(float(cells.get((m, c), 0)) for c in chains)
        y = float(pad_t + plot_h)  # stack upward from the baseline
        segs = [(c, float(cells.get((m, c), 0))) for c in chains]
        for j, (c, v) in enumerate(segs):
            if v <= 0:
                continue
            h = (v / peak) * plot_h
            # 2px surface gap between stacked segments; the topmost segment
            # carries the 4px rounded data-end, the rest stay square.
            gap = 2 if j < len([1 for _, vv in segs if vv > 0]) - 1 else 0
            top = y - h
            r = 4 if abs(top - (pad_t + plot_h - (total / peak) * plot_h)) < 0.01 else 0
            bars.append(
                f'<rect class="seg s{_CHAIN_SLOT.get(c, 1)}" x="{x:.1f}" y="{top:.1f}" '
                f'width="{bar_w:.1f}" height="{max(0.0, h - gap):.1f}" rx="{r}" '
                f"><title>{_e(m)} · {_e(_CHAIN_LABEL.get(c, c))} — {_e(_usd(v))}</title></rect>"
            )
            y = top
        # Direct-label the peak month only — never a number on every bar.
        if total >= peak * 0.999:
            bars.append(
                f'<text class="bar-label" x="{cx:.1f}" y="{pad_t + plot_h - (total / peak) * plot_h - 6:.1f}" '
                f'text-anchor="middle">{_e(_k(total))}</text>'
            )
        bars.append(
            f'<text class="tick" x="{cx:.1f}" y="{H - 9:.1f}" text-anchor="middle">'
            f"{_e(m[2:])}</text>"
        )

    baseline = pad_t + plot_h
    legend = " ".join(
        f'<span class="key"><i class="s{_CHAIN_SLOT.get(c, 1)}"></i>'
        f"{_e(_CHAIN_LABEL.get(c, c))}</span>"
        for c in chains
    )
    return (
        f'<div class="legend">{legend}</div>'
        f'<svg class="chart" viewBox="0 0 {W} {H}" role="img" '
        f'aria-label="Claimed fees by accrual month, split by venue">'
        f'<line class="axis" x1="{pad_l}" y1="{baseline}" x2="{W - pad_l}" y2="{baseline}"/>'
        + "".join(bars)
        + "</svg>"
    )


# ── page ──────────────────────────────────────────────────────────────────

_CSS = """
:root{color-scheme:light;
--plane:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink-2:#52514e;--muted:#898781;
--grid:#e1e0d9;--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;
--good:#0ca30c;--warning:#fab219;--serious:#ec835a;--critical:#d03b3b;
--up:#006300;--down:#b3261e;}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;
--plane:#0d0d0d;--surface:#1a1a19;--ink:#fff;--ink-2:#c3c2b7;--muted:#898781;
--grid:#2c2c2a;--s1:#3987e5;--s2:#d95926;--s3:#199e70;
--up:#5fd35f;--down:#ff8a80;}}
:root[data-theme="dark"]{color-scheme:dark;
--plane:#0d0d0d;--surface:#1a1a19;--ink:#fff;--ink-2:#c3c2b7;--muted:#898781;
--grid:#2c2c2a;--s1:#3987e5;--s2:#d95926;--s3:#199e70;
--up:#5fd35f;--down:#ff8a80;}
*{box-sizing:border-box}
body{margin:0;background:var(--plane);color:var(--ink);
font:15px/1.6 ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
.wrap{max-width:1120px;margin:0 auto;padding:28px 16px 64px}
header{display:flex;flex-wrap:wrap;gap:12px;align-items:baseline;justify-content:space-between;margin-bottom:6px}
h1{font-size:15px;font-weight:600;letter-spacing:.02em;margin:0;color:var(--ink-2)}
.asof{font-size:12px;color:var(--muted)}
.hero{font-size:44px;font-weight:650;letter-spacing:-.02em;margin:2px 0 2px;
font-variant-numeric:tabular-nums}
.hero-sub{font-size:13px;color:var(--ink-2);margin-bottom:22px}
h2{font-size:12px;font-weight:650;text-transform:uppercase;letter-spacing:.07em;
color:var(--muted);margin:30px 0 10px}
.verdict.warn{border-left:4px solid var(--warning)}.verdict.ok{border-left:4px solid var(--good)}
.card{background:var(--surface);border:1px solid var(--grid);border-radius:10px;padding:16px 18px;
overflow-x:auto}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.tile .lbl{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
.tile .val{font-size:22px;font-weight:600;font-variant-numeric:tabular-nums;margin-top:3px}
.tile .note{font-size:12px;color:var(--ink-2);margin-top:2px}
.up{color:var(--up)}.down{color:var(--down)}.flat{color:var(--ink-2)}
.cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:10px}
.kv{display:flex;justify-content:space-between;gap:16px;padding:4px 0;font-variant-numeric:tabular-nums}
.kv span:first-child{color:var(--ink-2)}
.kv.total{border-top:1px solid var(--grid);margin-top:6px;padding-top:8px;font-weight:650}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
th{text-align:right;font-size:11px;text-transform:uppercase;letter-spacing:.05em;
color:var(--muted);font-weight:600;padding:0 12px 8px;white-space:nowrap}
th:first-child,td:first-child{text-align:left;padding-left:0}
th:last-child,td:last-child{padding-right:0}
td{text-align:right;padding:8px 12px;border-top:1px solid var(--grid);white-space:nowrap}
td.pair{max-width:260px;overflow:hidden;text-overflow:ellipsis}
tr.sub td{color:var(--ink-2)}
tfoot td{font-weight:650;border-top:2px solid var(--grid)}
.chip{display:inline-flex;align-items:center;gap:5px;font-size:11px;font-weight:600;
padding:2px 8px;border-radius:999px;border:1px solid var(--grid);color:var(--ink-2)}
.chip.in{color:var(--good)}.chip.out{color:var(--critical)}
.chip.st-good{color:var(--good)}.chip.st-warning{color:var(--serious)}.chip.st-muted{color:var(--muted)}
span.ok,span.warn,span.bad{padding:0 4px;border-radius:4px}
span.days{display:inline-block;margin:1px 4px 1px 0}
.seats{grid-template-columns:repeat(auto-fit,minmax(420px,1fr))}.seat .call{font-size:17px;font-weight:650;margin:2px 0 10px}.seat ul.why{margin:10px 0 0;padding-left:18px;font-size:13px;color:var(--ink-2)}
table.perf td small,table.quotes td small{max-width:220px}
table.compare td{white-space:normal;text-align:left;vertical-align:top;min-width:170px}table.compare th{text-align:left}table.compare td.pair{min-width:150px;color:var(--ink-2)}
.wrap:has(#rfq.on){max-width:1400px}
td.ok,span.ok{background:color-mix(in srgb,var(--good) 14%,transparent)}
td.warn,span.warn{background:color-mix(in srgb,var(--warning) 18%,transparent)}
td.bad,span.bad{background:color-mix(in srgb,var(--critical) 15%,transparent)}
table.mx td,table.mx th{padding:5px 8px;font-size:13px}table.mx td small{display:block;font-size:11px;color:var(--muted);white-space:normal;line-height:1.35}
table.mx tr.reading td{border-top:0;text-align:left;white-space:normal;padding:0 8px 12px;font-size:13px}
table.mx tr.reading ul{margin:2px 0 0;padding-left:18px;color:var(--ink-2)}
table.mx tr.blk td{text-align:left;font-weight:650;font-size:12px;padding-top:14px;white-space:normal}
table.mx th.dex,table.mx td.dex0{border-left:2px solid var(--grid)}table.mx th small{display:block;font-weight:500;color:var(--muted)}
td.wrapcell{white-space:normal;text-align:left;vertical-align:top;padding-right:12px;min-width:120px}
h2 a{color:var(--s1);text-transform:none;letter-spacing:0}
.banner{border-left:3px solid var(--warning);background:var(--surface);
border-radius:0 8px 8px 0;padding:12px 16px;margin-bottom:8px}
.banner.crit{border-left-color:var(--critical)}
.banner .t{font-weight:650;font-size:13px;margin-bottom:4px}
.banner p{margin:3px 0;font-size:13px;color:var(--ink-2)}
.sig{display:flex;gap:12px;align-items:flex-start;padding:11px 0;border-top:1px solid var(--grid)}
.sig:first-child{border-top:0}
.sig .dot{font-size:11px;font-weight:700;white-space:nowrap;padding-top:2px}
.dot.critical{color:var(--critical)}.dot.serious{color:var(--serious)}.dot.warning{color:var(--warning)}
.sig .body{flex:1;min-width:0;overflow-wrap:anywhere}
.sig .hd{font-weight:600}
.sig .dt{font-size:12px;color:var(--ink-2);margin-top:2px}
.sig .dt.sug{color:var(--ink)}
.sig .calc{font-size:12px;color:var(--ink-2);margin:6px 0 0;padding-left:12px;border-left:2px solid var(--grid)}
.sig .calc div{margin-top:2px}
.plans{display:flex;flex-direction:column;gap:14px}
.plan{background:var(--surface);border:1px solid var(--grid);border-radius:10px;padding:18px 22px}
.plan-hd{display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 14px;padding-bottom:12px;border-bottom:1px solid var(--grid)}
.plan-hd .pool{font-size:17px;font-weight:650}
.plan-hd .venue{font-size:14px;color:var(--muted)}
.plan-hd .ev{margin-left:auto;font-size:17px;font-weight:650;font-variant-numeric:tabular-nums}
.leg{padding-top:14px}.leg+.leg{border-top:1px dashed var(--grid);margin-top:14px}
.leg-hd{display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 12px;margin-bottom:10px}
.leg-hd .no{color:var(--muted);font-weight:650;font-variant-numeric:tabular-nums}
.leg-hd .act{font-size:15px;font-weight:700;letter-spacing:.03em}
.leg-hd .ref{font-size:14px;color:var(--ink-2);font-variant-numeric:tabular-nums}
.leg-hd .ev{margin-left:auto;font-size:15px;font-weight:650;font-variant-numeric:tabular-nums}
.leg-hd .ev small{font-weight:500;color:var(--ink-2);margin-left:6px}
.tag{font-size:11px;font-weight:700;letter-spacing:.06em;padding:2px 7px;border-radius:4px;border:1px solid currentColor}
.tag.high{color:var(--good)}.tag.med{color:var(--serious)}.tag.low{color:var(--critical)}
.kv2{display:grid;grid-template-columns:118px 1fr;gap:6px 16px;margin:0;font-size:14px;line-height:1.55}
.kv2 dt{color:var(--muted);font-weight:500}
.kv2 dd{margin:0;color:var(--ink);font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
.kv2 dt.key,.kv2 dd.key{font-weight:650}
.plan details{margin-top:12px;font-size:13px}
.plan summary{cursor:pointer;color:var(--s1);font-weight:600}
.plan details .lines{margin-top:8px;padding-left:12px;border-left:2px solid var(--grid)}
.plan details .lines div{padding:2px 0;line-height:1.55;color:var(--ink-2);overflow-wrap:anywhere}
.group-hd{font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:.07em;color:var(--ink);margin:24px 0 2px}
.group-sub{font-size:13px;color:var(--muted);margin:0 0 10px}
.intro{font-size:13px;line-height:1.55;color:var(--muted);margin:0 0 4px}
@media(max-width:600px){.kv2{grid-template-columns:1fr;gap:0}.kv2 dt{margin-top:8px}.plan{padding:16px}}
.tier{margin:14px 0 2px;font-size:12px}.tier:first-child{margin-top:0}
.tier .dot{font-weight:700}
.legend{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:6px;font-size:12px;color:var(--ink-2)}
.key i{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:6px}
i.s1{background:var(--s1)}i.s2{background:var(--s2)}i.s3{background:var(--s3)}
.chart{width:100%;height:auto;display:block;overflow:visible}
.seg.s1{fill:var(--s1)}.seg.s2{fill:var(--s2)}.seg.s3{fill:var(--s3)}
.seg{transition:opacity .12s}.chart:hover .seg{opacity:.45}.chart .seg:hover{opacity:1}
.axis{stroke:var(--grid);stroke-width:1}
.tick{fill:var(--muted);font-size:10px}
.bar-label{fill:var(--ink-2);font-size:11px;font-weight:600}
.empty{color:var(--muted);font-size:13px;margin:4px 0}
.mm{margin-top:12px}.mm-hd{font-size:16px;font-weight:650;margin-bottom:10px}.mm-hd .asof{margin-left:12px;font-weight:400}.mm-blocks{display:grid;grid-template-columns:repeat(3,1fr);gap:28px;margin-bottom:14px}.blk{font-size:11px;font-weight:650;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);margin:4px 0 8px}tr.grp td{font-size:11px;font-weight:650;letter-spacing:.06em;color:var(--ink-2);background:var(--plane);padding-top:10px}@media(max-width:760px){.mm-blocks{grid-template-columns:1fr}}table.terms td{white-space:normal;text-align:left;vertical-align:top;min-width:150px}table.terms th{text-align:left}.kv2.exit{grid-template-columns:170px 1fr;margin-bottom:14px}.kv2.terms{grid-template-columns:auto 1fr;font-size:13px;gap:4px 14px}details.src{margin-top:10px;font-size:12px;color:var(--ink-2)}details.src summary{cursor:pointer;color:var(--muted)}details.status{font-size:12px;color:var(--muted);margin:0 0 8px}details.status>summary{cursor:pointer}details.status p{margin:4px 0 0 14px;font-size:12px}
.pricebar{margin:0 0 14px;font-size:15px;font-variant-numeric:tabular-nums}.pricebar span{color:var(--muted);font-size:12px;margin-left:8px}
table.brief td{text-align:left;padding:8px 12px 8px 0}table.brief td.asof{white-space:nowrap}
details.more{margin-top:22px}details.more>summary{cursor:pointer;font-size:12px;font-weight:650;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);margin-bottom:10px}
.leg details summary{font-size:12px}
.foot{margin-top:34px;font-size:12px;color:var(--muted)}
.foot p{margin:5px 0}
#theme{position:fixed;top:calc(14px + env(safe-area-inset-top, 0px));right:14px;background:var(--surface);color:var(--ink-2);
border:1px solid var(--grid);border-radius:8px;padding:6px 10px;font-size:12px;cursor:pointer}
@media(max-width:600px){.hero{font-size:34px}.wrap{padding:22px 16px 48px}}
.tabs{position:sticky;top:0;z-index:5;display:flex;gap:2px;overflow-x:auto;scrollbar-width:none;
background:var(--plane);border-bottom:1px solid var(--grid);margin:18px -4px 0;padding:0 4px}
.tabs::-webkit-scrollbar{display:none}
.tabs a{flex:none;padding:11px 12px 10px;font-size:13px;font-weight:600;color:var(--muted);
text-decoration:none;border-bottom:2px solid transparent;white-space:nowrap}
.tabs a:hover{color:var(--ink)}
.tabs a.on{color:var(--ink);border-bottom-color:var(--s1)}
.panel{padding-top:6px}
.js .panel{display:none}.js .panel.on{display:block}
.no-js-label{display:none}

"""

_JS = """
(function(){var b=document.getElementById('theme');
function cur(){return document.documentElement.getAttribute('data-theme')||
(matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light');}
function set(t){document.documentElement.setAttribute('data-theme',t);
b.textContent=t==='dark'?'Light':'Dark';try{localStorage.setItem('deskTheme',t);}catch(e){}}
try{var s=localStorage.getItem('deskTheme');if(s)set(s);else b.textContent=cur()==='dark'?'Light':'Dark';}
catch(e){b.textContent=cur()==='dark'?'Light':'Dark';}
b.addEventListener('click',function(){set(cur()==='dark'?'light':'dark');});})();
(function(){var d=document.documentElement,tabs=document.querySelectorAll('.tabs a');
if(!tabs.length)return;d.classList.add('js');
function show(id){var hit=false;tabs.forEach(function(t){var on=t.getAttribute('href')==='#'+id;
t.classList.toggle('on',on);if(on)hit=true;});
document.querySelectorAll('.panel').forEach(function(p){p.classList.toggle('on',p.id===id);});
if(hit){try{localStorage.setItem('deskTab',id);}catch(e){}}return hit;}
function initial(){var h=(location.hash||'').slice(1);if(h&&document.getElementById(h))return h;
try{var s=localStorage.getItem('deskTab');if(s&&document.getElementById(s))return s;}catch(e){}
return tabs[0].getAttribute('href').slice(1);}
show(initial());
tabs.forEach(function(t){t.addEventListener('click',function(e){e.preventDefault();
var id=t.getAttribute('href').slice(1);show(id);history.replaceState(null,'','#'+id);
window.scrollTo(0,0);});});
window.addEventListener('hashchange',function(){var h=location.hash.slice(1);if(h)show(h);});})();
"""


def _signed(v) -> str:
    return (
        f'<span class="{_sign_class(float(v))}">{_e(_usd(float(v)))}</span>'
        if v is not None
        else "—"
    )


def _performance_section(data: ReportData) -> str:
    """Every window's PnL with flows netted out, against the HODL benchmark.

    Excess vs HODL is IL + fees earned − gas over the positions whose marks
    can be split; a window whose every position was excluded says so rather
    than printing zeros."""
    if not data.periods:
        return ""
    out = ['<h2>Performance by period <span class="asof">vs holding the deposited '
           "tokens — flows netted out</span></h2><div class=\"card\">"]  # fmt: skip
    out.append(
        "<table><thead><tr><th>Window</th><th>Positions</th><th>Market (benchmark)</th>"
        "<th>IL</th><th>Fees earned</th><th>Gas</th><th>PnL</th><th>Excess return</th>"
        "<th>Excluded</th></tr></thead><tbody>"
    )
    for p in data.periods:
        t = p.totals()
        excluded = f"{len(p.excluded)} · {_usd(float(p.excluded_value()))}" if p.excluded else "—"
        if not p.rows:
            out.append(
                f'<tr><td>{_e(p.label)}</td><td>0</td><td colspan=6 class="pair" '
                f'style="text-align:left">every position excluded — no figure, not zero</td>'
                f"<td>{_e(excluded)}</td></tr>"
            )
            continue
        excess = t["il"] + t["fees_earned"]
        gas = t["gas"]
        out.append(
            f"<tr><td>{_e(p.label)}</td><td>{t['positions']}</td><td>{_signed(t['market'])}</td>"
            f"<td>{_signed(t['il'])}</td><td>{_signed(t['fees_earned'])}</td>"
            f"<td>{_e(_usd(-float(gas) or 0.0, dp=2)) if gas is not None else '—'}</td>"
            f"<td>{_signed(t['pnl'])}</td><td>{_signed(excess)}</td><td>{_e(excluded)}</td></tr>"
        )
    out.append("</tbody></table>")
    reasons: dict[str, int] = {}
    for p in data.periods:
        for e in p.excluded:
            reasons[e.reason.split(" (")[0]] = reasons.get(e.reason.split(" (")[0], 0) + 1
    if reasons:
        out.append(
            '<p class="empty">Excluded, never estimated, because: '
            + _e("; ".join(sorted(reasons)))
            + ". `defi-tracker periods` lists each position.</p>"
        )
    out.append("</div>")
    return "".join(out)


def _risk_section(data: ReportData) -> str:
    """The Phase 1 answer to "what happens if price moves": the book under a
    PEAQ shock, and per position its greeks, range odds and breakeven fee APR
    next to the fee yield it actually earned."""
    from defi_tracker.analytics import SHOCKS, position_id, window_yields
    from defi_tracker.report import YIELD_WINDOW_DAYS

    risk = data.risk
    if not risk or not risk.get("positions"):
        return ""
    peaq = risk["peaq"]
    out = ['<h2>Risk <span class="asof">closed-form from today\'s marks — no history '
           "needed</span></h2>"]  # fmt: skip
    out.append('<div class="card"><table><thead><tr><th>PEAQ move</th>')
    out += [f"<th>{s:+.0%}</th>" for s in SHOCKS]
    out.append("</tr></thead><tbody><tr><td>Book value change</td>")
    out += [f"<td>{_signed(peaq['changes'][s])}</td>" for s in SHOCKS]
    out.append("</tr></tbody></table>")
    out.append(
        f'<p class="empty">{peaq["positions"]} PEAQ-exposed positions; every other '
        "token's USD price held.</p></div>"
    )
    yields = window_yields(data.window_stats, data.window_collects, window_days=YIELD_WINDOW_DAYS)
    horizon = int(risk.get("horizon_days", 30))
    out.append(
        '<div class="card" style="margin-top:10px"><table><thead><tr><th>Position</th>'
        "<th>Value</th><th>Δ per 1%</th><th>Γ per 1%²</th><th>at −20%</th>"
        f"<th>P(exit, {horizon}d)</th><th>Breakeven fee APR</th><th>Realized 30d yield</th>"
        "</tr></thead><tbody>"
    )
    rows = sorted(risk["positions"], key=lambda p: -p["row"]["current_value_usd"])
    for p in rows[:14]:
        row, r = p["row"], p["risk"]
        if r is None:
            continue
        y = yields.get(position_id(row))
        realized = y["apy_pct"] if y and y["apy_pct"] is not None else None
        be = r["breakeven_apr"]
        band = r["breakeven_band"]
        be_s = (
            "—"
            if be is None
            else f"{be:.0f}%" + (f" ({band[0]:.0f}–{band[1]:.0f})" if band and be else "")
        )
        short = be is not None and realized is not None and be > 0 and realized < be
        odds = r["exit_prob"] if r["exit_prob"] is not None else None
        odds_s = (
            f"{odds:.0%}" if odds is not None
            else (f"re-entry {r['reentry_prob']:.0%}" if r["reentry_prob"] is not None else "—")
        )  # fmt: skip
        out.append(
            f'<tr><td class="pair">{_e(row["pair_label"])}</td><td>{_e(_usd(r["value"]))}</td>'
            f"<td>{_e(_usd(r['delta_1pct']))}</td><td>{r['gamma_1pct']:,.1f}</td>"
            f"<td>{_signed(r['shocks'][-0.2]['change'])}</td><td>{_e(odds_s)}</td>"
            f"<td>{_e(be_s)}</td>"
            f'<td class="{"down" if short else ""}">'
            f"{_e(_pct(realized, 0)) if realized is not None else '—'}</td></tr>"
        )
    out.append("</tbody></table>")
    out.append(
        '<p class="empty">Γ negative = short gamma. Breakeven APR is the fee rate that pays '
        "for the IL expected at the pool's daily σ; the band is 95% on σ from its sample. A "
        "realized yield in red is below breakeven: at this σ its fees are not paying for "
        "its IL.</p></div>"
    )
    return "".join(out)


_SCORE_STATUS = {
    "measured": ("good", "Measured"),
    "needs_input": ("warning", "Needs input"),
    "not_here": ("muted", "Measured elsewhere"),
    "not_yet": ("muted", "Not yet"),
}


def _scorecard_table(rows: list[dict]) -> str:
    """The proposal's success metrics, each with where the book stands."""
    if not rows:
        return ""
    out = [
        '<div class="card"><div class="lbl" style="color:var(--muted);font-size:11px;'
        'text-transform:uppercase;letter-spacing:.06em;margin-bottom:8px">Success metrics · '
        "target end-2026</div>"
        "<table><thead><tr><th>Metric</th><th>Target</th><th>Now</th><th>Status</th>"
        "</tr></thead><tbody>"
    ]
    for r in rows:
        cls, label = _SCORE_STATUS.get(r["status"], ("muted", r["status"]))
        out.append(
            f'<tr><td class="wrapcell">{_e(r["metric"])}</td>'
            f'<td class="wrapcell">{_e(r["target"])}</td>'
            f'<td class="wrapcell" title="{_e(r["note"])}">{_e(r["now"])}</td>'
            f'<td><span class="chip st-{cls}">{_e(label)}</span></td></tr>'
        )
    out.append("</tbody></table></div>")
    return "".join(out)


def _charter_section(ch: dict) -> str:
    """The renewed mandate as proposed: books, approval authority and plan,
    as tables. Rendered from content/mandate.toml; nothing here is computed."""
    if not ch:
        return ""
    if ch.get("error"):
        return f'<p class="empty down">Mandate file invalid: {_e(ch["error"])}</p>'
    out = []
    if ch.get("priority"):
        out.append(f'<p class="pricebar">Priority <span>{_e(ch["priority"])}</span></p>')

    def table(title: str, cols: list[tuple[str, str]], rows: list[dict]) -> None:
        if not rows:
            return
        out.append(f'<div class="card" style="margin-bottom:10px"><div class="blk">{_e(title)}</div>'
                   '<table class="grid terms"><thead><tr>')  # fmt: skip
        out.append("".join(f"<th>{_e(h)}</th>" for _, h in cols) + "</tr></thead><tbody>")
        for r in rows:
            cells = []
            for key, _ in cols:
                v = str(r.get(key, "—"))
                if key == "status":
                    tone = {"done": "good", "in progress": "warning"}.get(v.lower(), "muted")
                    cells.append(f'<td><span class="chip st-{tone}">{_e(v)}</span></td>')
                else:
                    cells.append(f"<td>{_e(v)}</td>")
            out.append("<tr>" + "".join(cells) + "</tr>")
        out.append("</tbody></table></div>")

    table("Books", [("name", "Book"), ("covers", "Covers"), ("capital", "Capital")],
          ch.get("books", []))  # fmt: skip
    table("Approval authority", [("decision", "Decision"), ("approver", "Approver")],
          ch.get("approvals", []))  # fmt: skip
    table("Plan", [("when", "When"), ("milestone", "Milestone"), ("deliverable", "Deliverable"),
                   ("status", "Status")], ch.get("plan", []))  # fmt: skip
    return "".join(out)


def _mandate_section(data: ReportData) -> str:
    """Limits, exceptions, incidents and the contracts in force — the mandate
    view the monthly report freezes, live."""

    from defi_tracker.policy import LIMITS

    m = data.mandate
    if not m:
        return ""
    from defi_tracker.analytics import PROPOSAL_URL

    out = [f'<h2>Mandate <span class="asof">{_link(PROPOSAL_URL, "Proposal ↗")}</span></h2>']
    out.append(_charter_section(m.get("charter") or {}))
    out.append(_scorecard_table(m.get("scorecard", [])))
    out.append('<div class="cols" style="margin-top:10px">')
    # Limits
    out.append('<div class="card"><div class="lbl" style="color:var(--muted);font-size:11px;'
               'text-transform:uppercase;letter-spacing:.06em;margin-bottom:8px">Policy '
               f"limits ({_e(m['policy_source'])})</div>")  # fmt: skip
    if m.get("policy_error"):
        out.append(f'<p class="down">Policy file invalid — no limit evaluated: '
                   f"{_e(m['policy_error'])}</p>")  # fmt: skip
    checks = m.get("checks", [])
    counts = {
        k: sum(1 for c in checks if c.status == k) for k in ("breach", "unknown", "ok", "unset")
    }
    for lbl, key in (("In breach", "breach"), ("Unknown", "unknown"), ("Within limit", "ok"),
                     ("Not set", "unset")):  # fmt: skip
        cls = "down" if key == "breach" and counts[key] else ""
        out.append(
            f'<div class="kv"><span>{lbl}</span><span class="{cls}">{counts[key]}</span></div>'
        )
    desc = {d.id: d.description for d in LIMITS}
    for c in [c for c in checks if c.status == "breach"][:6]:
        out.append(f'<div class="kv"><span class="down">{_e(desc.get(c.limit_id, c.limit_id))}'
                   f" — {_e(c.subject)}</span><span>{_e(c.observed)} / {_e(c.threshold)} "
                   f"{_e(c.unit)}</span></div>")  # fmt: skip

    out.append("</div>")
    # Exceptions + incidents
    exc = m.get("exceptions", [])
    open_exc = sum(1 for e in exc if e["cleared_at"] is None)
    unreviewed = sum(1 for e in exc if not e["reviewed_at"])
    inc = m.get("incidents", [])
    open_inc = sum(1 for i in inc if not i["resolved_at"])
    out.append('<div class="card"><div class="lbl" style="color:var(--muted);font-size:11px;'
               'text-transform:uppercase;letter-spacing:.06em;margin-bottom:8px">Exceptions '
               "and incidents</div>")  # fmt: skip
    for lbl, v, bad in (("Exceptions open", open_exc, open_exc > 0),
                        ("Exceptions unreviewed", unreviewed, unreviewed > 0),
                        ("Incidents open", open_inc, open_inc > 0)):  # fmt: skip
        out.append(f'<div class="kv"><span>{lbl}</span>'
                   f'<span class="{"down" if bad else ""}">{v}</span></div>')  # fmt: skip
    out.append("</div>")
    # Contracts: a pointer, not a second copy — the MM book tab carries them
    out.append('<div class="card"><div class="lbl" style="color:var(--muted);font-size:11px;'
               'text-transform:uppercase;letter-spacing:.06em;margin-bottom:8px">Contracts in '
               "force</div>")  # fmt: skip
    book = data.mm_book
    if not book:
        out.append('<p class="empty">No MM agreements, loans/options or OTC placements keyed. '
                   "Counterparty exposure is unknown, not zero — `defi-tracker ledger add`.</p>")  # fmt: skip
    else:
        now = [c["usd_now"] for c in book]
        total = _usd(float(sum(now))) if all(v is not None for v in now) else "unknown"
        soonest = min((c["days_left"] for c in book if c["days_left"] is not None), default=None)
        out.append(f'<div class="kv"><span>{len(book)} in force, at PEAQ now</span>'
                   f"<span>{_e(total)}</span></div>")  # fmt: skip
        if soonest is not None:
            cls = "down" if soonest <= 90 else ""
            out.append(f'<div class="kv"><span>Next expiry</span>'
                       f'<span class="{cls}">in {soonest}d</span></div>')  # fmt: skip
        out.append('<p class="empty"><a href="#mm-book">MM book</a> has each contract.</p>')
    out.append("</div></div>")
    return "".join(out)


def _brief_section(data: ReportData, total_value: float, head: dict | None, book_net: float) -> str:
    """The two-minute read: six numbers, what falls due in the next 60 days,
    and the MM exposure in one table. Everything else is a tab away."""
    from defi_tracker.analytics import mm_calendar, mm_exposure

    today = datetime.now(UTC).date()
    mark = data.peaq
    mtd = next((p for p in data.periods if p.kind == "MTD" and p.rows), None)
    t = mtd.totals() if mtd else None
    excess = float(t["il"] + t["fees_earned"]) if t else None
    book = data.mm_book
    now_vals = [c["usd_now"] for c in book]
    mm_now = float(sum(now_vals)) if book and all(v is not None for v in now_vals) else None
    owed = sum((c["outstanding"] for c in book if c.get("outstanding") is not None), Decimal(0))
    costs = [c["annual_cost_usd"] for c in book]
    cost = float(sum(costs)) if book and all(v is not None for v in costs) else None
    pnl = float(head["pnl"]) if head else None
    covered = head.get("covered_pct") if head else None
    vd = data.value_delta
    day_move = f"{len(data.snaps)} positions"
    if vd is not None and vd.pct is not None:
        day_move = f"{_usd(vd.delta)} ({vd.pct:+.1f}%) {vd.window_label()}"

    out = []
    if mark is not None and mark.price is not None:
        src = "CoinGecko" if mark.source == "market" else "book pools"
        when = _when(mark.ts)
        x = (
            f" · pools {_price(mark.pool_price)} ({mark.gap_pct:+.1f}%)"
            if mark.gap_pct is not None
            else ""
        )
        out.append(f'<p class="pricebar">PEAQ <strong>{_e(_price(mark.price))}</strong> '
                   f"<span>{_e(src)} · {_e(when)}{_e(x)}</span></p>")  # fmt: skip
    tiles = [
        ("DEX book", _k(total_value), day_move, "flat"),
        (
            "PnL MTD",
            _k(pnl),
            f"flows netted · covers {covered:.0f}% of book" if covered else "not established",
            _sign_class(pnl),
        ),
        ("Excess return MTD", _k(excess), "vs holding · fees + IL", _sign_class(excess)),
        ("Book net", _k(book_net), "since inception", _sign_class(book_net)),
        ("MM loans", _k(mm_now), f"{float(owed) / 1e6:,.1f}m PEAQ at spot", "flat"),
        ("MM cost / yr", _k(cost), "as keyed" if cost is not None else "not all keyed", "flat"),
    ]
    out.append('<div class="tiles">')
    for lbl, val, note, cls in tiles:
        out.append(f'<div class="card tile"><div class="lbl">{_e(lbl)}</div>'
                   f'<div class="val {cls}">{_e(val)}</div><div class="note">{_e(note)}</div></div>')  # fmt: skip
    out.append("</div>")

    # Next 60 days: dated MM events, then the standing items.
    rows = [(r["date"], f"{r['days']}d", r["what"], r["detail"]) for r in mm_calendar(book, today)]
    q = data.actions
    if q is not None:
        robust = [k for k in q.tickets if k.tier == "now" and k.uid not in data.snoozed_uids]
        if robust:
            ev = sum(k.score_usd for k in robust)
            rows.append(
                (
                    "—",
                    "",
                    "Rebalance tickets",
                    f"{len(robust)} robust, {_signed_usd(ev)} / 30d · advisory",
                )
            )
    checks = data.mandate.get("checks", []) if data.mandate else []
    unset = sum(1 for c in checks if c.status == "unset")
    if unset:
        rows.append(
            ("—", "", "Mandate limits not set", f"{unset} of {len(checks)} · leadership to set")
        )
    out.append("<h2>Next 60 days</h2>")
    if rows:
        out.append('<div class="card"><table class="brief"><tbody>')
        for d, days, what, detail in rows:
            out.append(f'<tr><td>{_e(d)}</td><td class="asof">{_e(days)}</td>'
                       f'<td class="pair">{_e(what)}</td><td class="wrapcell">{_e(detail)}</td></tr>')  # fmt: skip
        out.append("</tbody></table></div>")
    else:
        out.append('<div class="card"><p class="empty">Nothing falls due.</p></div>')

    # MM exposure
    exp = mm_exposure(book, today)
    exits = {c["counterparty"]: _terms(c).get("Exit, short", "—") for c in book}
    if exp:
        out.append('<h2>MM exposure <span class="asof"><a href="#mm-book">MM book</a></span></h2>')
        out.append('<div class="card"><table><thead><tr><th>MM</th><th>PEAQ out</th>'
                   "<th>Value now</th><th>Loan ends</th><th>Next option expiry</th>"
                   "<th>Lowest strike</th><th>peaq early exit</th></tr></thead><tbody>")  # fmt: skip
        for r in exp:
            if not r["options_keyed"]:
                strike, cls = "not on file", "asof"
            elif r["lowest_strike_x"] is None and r["lowest_strike_range"]:
                lo, hi = r["lowest_strike_range"]
                strike, cls = f"about {lo:.1f}–{hi:.1f}× spot", ("down" if lo < 1.5 else "")
            elif r["lowest_strike_x"] is None:
                strike, cls = ("not fixed" if r["unfixed_strikes"] else "—"), "asof"
            else:
                strike = f"{r['lowest_strike_x']:.1f}× spot"
                cls = "down" if r["in_money"] else ""
            ends = f"{r['loan_end']} ({r['days_left']}d)" if r["loan_end"] else "—"
            soon = r["days_left"] is not None and r["days_left"] <= 60
            peaq_out = (
                f"{float(r['outstanding']) / 1e6:,.2f}m" if r["outstanding"] is not None else "—"
            )
            out.append(
                f'<tr><td class="pair">{_e(r["counterparty"])}</td>'
                f"<td>{_e(peaq_out)}</td>"
                f"<td>{_e(_usd(float(r['usd_now'])) if r['usd_now'] is not None else '—')}</td>"
                f'<td class="{"down" if soon else ""}">{_e(ends)}</td>'
                f"<td>{_e(r['next_option_expiry'] or '—')}</td>"
                f'<td class="{cls}">{_e(strike)}</td>'
                f"<td>{_e(exits.get(r['counterparty'], '—'))}</td></tr>"
            )
        out.append("</tbody></table></div>")
    return "".join(out)


def _price(v) -> str:
    return f"${float(v):.4f}" if v is not None else "—"


def _when(ts: int | None) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M UTC") if ts else "—"


def _m(v) -> str:
    """PEAQ amounts in millions, like the operator's sheet."""
    return f"{float(v) / 1e6:,.2f}m" if v is not None else "—"


def _pnl(v) -> str:
    if v is None:
        return "—"
    f = float(v)
    return f'<span class="{_sign_class(f)}">{_e(_usd(f))}</span>' if f else "$0"


def _mm_book_section(data: ReportData) -> str:
    """The MM book in the shape of the operator's sheet: a summary line per
    MM, then per MM its loan terms beside its tranche breakdown, marked to
    PEAQ now. Contract terms and sources sit behind a toggle."""
    from defi_tracker.analytics import PROPOSAL_URL

    mark = data.peaq
    book = data.mm_book
    px = mark.price if mark else None
    out = [f'<h2>MM book <span class="asof">{_link(PROPOSAL_URL, "MM Comps")}</span></h2>']
    if px is not None:
        out.append(f'<p class="pricebar">PEAQ <strong>{_e(_price(px))}</strong>'
                   f"<span>CoinGecko · {_e(_when(mark.ts if mark else None))}</span></p>")  # fmt: skip
    else:
        out.append('<p class="empty">PEAQ price unknown: values withheld, never guessed.</p>')
    if not book:
        out.append('<div class="card"><p class="empty">No contracts keyed.</p></div>')
        return "".join(out)

    # Summary — one row per MM, as the sheet's Summary block.
    out.append('<div class="card"><table class="grid"><thead><tr><th>MM</th><th>Loan</th>'
               "<th>Interest</th><th>Exercised</th><th>Returned</th><th>To be returned</th><th>Value now</th>"
               "<th>Loan ends</th><th>peaq P&amp;L at exercise</th><th>peaq P&amp;L today</th>"
               "</tr></thead><tbody>")  # fmt: skip
    tot = dict.fromkeys(("loan", "ret", "now", "pnl", "pnl_x"), Decimal(0))
    for c in book:
        interest = c.get("interest_payable")
        returned = (
            (c["outstanding"] + (interest or 0)) if c.get("outstanding") is not None else None
        )
        pnl = -c["mm_pnl_usd"] if c.get("mm_pnl_usd") is not None else None
        pnl_x = (
            -c["mm_pnl_at_exercise_usd"] if c.get("mm_pnl_at_exercise_usd") is not None else None
        )
        days = c["days_left"]
        ends = f"{c['end_date']} · {days}d" if c.get("end_date") else "—"
        for key, v in (("loan", c["amount"]), ("ret", returned), ("now", c["usd_now"]),
                       ("pnl", pnl), ("pnl_x", pnl_x)):  # fmt: skip
            tot[key] += v or 0
        rate = c.get("interest_rate_pct")
        out.append(
            f'<tr><td class="pair">{_e(c["counterparty"])}</td><td>{_m(c["amount"])}</td>'
            f"<td>{_m(interest) if interest else ('0' if rate == 0 else '—')}</td>"
            f"<td>{_m(c.get('exercised')) if c['tranches'] else '—'}</td>"
            f"<td>{_m(c.get('returned')) if c.get('returned') else '0'}</td><td>{_m(returned)}</td>"
            f"<td>{_e(_usd(float(c['usd_now'])) if c['usd_now'] is not None else '—')}</td>"
            f'<td class="{"down" if days is not None and days <= 60 else ""}">{_e(ends)}</td>'
            f"<td>{_pnl(pnl_x) if c['tranches'] else '—'}</td>"
            f"<td>{_pnl(pnl) if c['tranches'] else '—'}</td></tr>"
        )
    out.append(
        f"</tbody><tfoot><tr><td>Total</td><td>{_m(tot['loan'])}</td><td></td><td></td><td></td>"
        f"<td>{_m(tot['ret'])}</td><td>{_e(_usd(float(tot['now'])))}</td><td></td>"
        f"<td>{_pnl(tot['pnl_x'])}</td><td>{_pnl(tot['pnl'])}</td></tr></tfoot></table></div>"
    )

    # Per MM — the sheet's MM tab: Summary | Loan terms | Dates, then the ladder.
    today = datetime.now(UTC).date().isoformat()
    for c in book:
        rate = c.get("interest_rate_pct")
        cost = c.get("annual_cost_usd")
        tr = c["tranches"]
        amt = c["amount"]
        exercised = [t for t in tr if t["status"] == "exercised"]
        n_open = sum(t["status"] == "open" for t in tr)
        n_lapsed = sum(t["status"] == "lapsed" for t in tr)
        ex_tok = sum((t["amount"] for t in exercised), Decimal(0))
        back = c.get("returned") or Decimal(0)
        interest = c.get("interest_payable")
        returned = (
            (c["outstanding"] + (interest or 0)) if c.get("outstanding") is not None else None
        )
        mm_pnl = c.get("mm_pnl_usd")
        mm_pnl_x = c.get("mm_pnl_at_exercise_usd")
        sizes = {t["amount"] for t in tr}
        pct_loan = float(ex_tok / amt * 100) if amt else 0.0
        blocks = [
            ("Summary", [
                ("Tranches",
                 f"{len(exercised)} exercised · {n_open} open · {n_lapsed} lapsed" if tr else "not on file"),
                ("Tokens exercised", f"{_m(ex_tok)} · {pct_loan:.1f}% of loan" if tr else "—"),
                ("Returned early", f"{_m(back)} PEAQ" if back else "—"),
                ("To be returned", f"{_m(returned)} PEAQ"),
                ("peaq P&L at exercise", _usd(-float(mm_pnl_x) or 0.0) if mm_pnl_x is not None else "—"),
                ("peaq P&L today", _usd(-float(mm_pnl) or 0.0) if mm_pnl is not None else "—"),
            ]),
            ("Loan terms", [
                ("Loan amount", f"{_m(amt)} PEAQ"),
                ("Tenure", f"{c['tenure_months']} months" if c.get("tenure_months") else "—"),
                ("Interest rate", f"{rate:g}%" if rate is not None else "not on file"),
                ("# Tranches", str(len(tr)) if tr else "not on file"),
                ("Tokens / tranche", _m(next(iter(sizes))) if len(sizes) == 1 else ("varies" if tr else "—")),
                ("Interest payable", f"{_m(interest)} PEAQ" if interest else "—"),
            ]),
            ("Dates", [
                ("Loan ends", f"{c['end_date']} · {c['days_left']}d" if c.get("end_date") else "—"),
                ("Notice by", c.get("notice_by") or "—"),
                ("Cost / yr", _usd(float(cost)) if cost is not None else "not keyed"),
            ]),
        ]  # fmt: skip
        out.append(f'<div class="mm card"><div class="mm-hd">{_e(c["counterparty"])}'
                   f'<span class="asof">{_m(c.get("outstanding"))} PEAQ out · '
                   f'{_e(_usd(float(c["usd_now"])) if c["usd_now"] is not None else "—")} at spot</span></div>'
                   '<div class="mm-blocks">')  # fmt: skip
        for title, rows in blocks:
            out.append(f'<div><div class="blk">{_e(title)}</div><dl class="kv2 terms">')
            for k, v in rows:
                bad = v in ("not on file", "not keyed")
                out.append(f'<dt>{_e(k)}</dt><dd class="{"down" if bad else ""}">{_e(v)}</dd>')
            out.append("</dl></div>")
        out.append("</div>")
        exit_rows = [
            (k, v) for k, v in _terms(c).items() if _is_exit_term(k) and k != "Exit, short"
        ]
        if exit_rows:
            out.append('<div class="blk">If peaq terminates early</div><dl class="kv2 terms exit">')
            for k, v in exit_rows:
                out.append(f"<dt>{_e(k)}</dt><dd>{_e(v)}</dd>")
            out.append("</dl>")
        out.append('<div class="blk">Option ladder</div>')
        if not tr:
            out.append('<p class="empty down">Not in the documents on file.</p></div>')
            continue
        out.append('<table class="grid ladder"><thead><tr><th>#</th><th>Tokens</th><th>Strike</th>'
                   "<th>Basis</th><th>× spot</th><th>Exercised</th><th>MM P&amp;L at exercise</th>"
                   "<th>peaq P&amp;L at exercise</th><th>MM P&amp;L today</th>"
                   "<th>peaq P&amp;L today</th></tr></thead><tbody>")  # fmt: skip
        mm_tot = Decimal(0)
        mm_tot_x = Decimal(0)
        last_window = None
        for t in tr:
            frm, to = t["exercisable_from"], t["exercisable_to"]
            if (frm, to) != last_window:
                last_window = (frm, to)
                live = (
                    t["status"] == "open" and frm and frm <= today and (to is None or today <= to)
                )
                past = to is not None and to < today
                months = (
                    round((date.fromisoformat(frm) - date.fromisoformat(c["start_date"])).days / 30.44)
                    if frm and c.get("start_date") else None
                )  # fmt: skip
                label = f"{months} months · " if months else ""
                label += f"{frm or '?'} → {to or '?'}"
                label += " · WINDOW OPEN" if live else (" · closed" if past else "")
                out.append(f'<tr class="grp"><td colspan="10" class="{"down" if live else ""}">'
                           f"{_e(label)}</td></tr>")  # fmt: skip
            k = t["strike_usd"]
            if k is not None:
                strike = _price(k)
                x = f"{t['strike_x_spot']:.1f}×" if t["strike_x_spot"] is not None else "—"
                if t["status"] != "open":
                    x = "—"  # moneyness only matters while it can still be exercised
            elif t.get("strike_x_range"):
                lo, hi = t["strike_x_range"]
                strike = f"{_price(t['strike_low_usd'])}–{_price(t['strike_high_usd'])}"
                x = f"{lo:.1f}–{hi:.1f}×"
            else:
                strike, x = "not fixed", "—"
            # Today marks only what can still be exercised: an exercised tranche
            # settled on its own day, a lapsed one is gone.
            gain = t["mm_gain_usd"] if t["status"] == "open" else None
            gx = t.get("mm_gain_at_exercise_usd")
            mm_tot += gain or 0
            mm_tot_x += gx or 0
            done = t["status"] == "exercised"
            at_mm, at_peaq = (
                (_pnl(gx), _pnl(-gx if gx is not None else None)) if done else ("$0", "$0")
            )
            state = {"exercised": "YES", "lapsed": "LAPSED"}.get(t["status"], "NO")
            out.append(
                f"<tr><td>{_e(t['tranche'])}</td><td>{_m(t['amount'])}</td><td>{_e(strike)}</td>"
                f'<td class="asof">{_e(t.get("strike_rule") or "—")}</td>'
                f'<td class="{"down" if t["in_money"] else ""}">{_e(x)}</td>'
                f"<td>{state}</td><td>{at_mm}</td><td>{at_peaq}</td>"
                f"<td>{_pnl(gain) if t['status'] == 'open' else '—'}</td>"
                f"<td>{_pnl(-gain if gain is not None else None) if t['status'] == 'open' else '—'}</td></tr>"
            )
        in_force = sum((t["amount"] for t in tr if t["status"] == "open"), Decimal(0))
        out.append(f"</tbody><tfoot><tr><td>Open</td><td>{_m(in_force)}</td>"
                   "<td></td><td></td><td></td><td></td>"
                   f"<td>{_pnl(mm_tot_x)}</td><td>{_pnl(-mm_tot_x)}</td>"
                   f"<td>{_pnl(mm_tot)}</td><td>{_pnl(-mm_tot)}</td></tr></tfoot></table>")  # fmt: skip
        out.append(_proposed_ladder(data, c) + "</div>")
    out.append(_key_terms_grid(book))
    out.append('<p class="asof">P&amp;L at exercise: the market price on the exercise day vs the '
               "strike. P&amp;L today: open tranches only — PEAQ now vs the strike, what exercising "
               "today would hand the MM (zero when out of the money). Exercised and lapsed tranches "
               "are settled and stay out of today's total. peaq's P&amp;L is the MM's, "
               "reversed.</p>")  # fmt: skip
    return "".join(out)


def _proposed_ladder(data: ReportData, c: dict) -> str:
    """The incumbent's renewal proposal under its signed ladder, marked as
    not signed: loan, each proposed call and its fair value at PEAQ now.
    Read from the RFQ bid whose `incumbent` is this MM; nothing when none."""
    from defi_tracker.analytics import bid_cost, bs_call

    bid = next(
        (b for b in (data.rfq or {}).get("bid", []) if b.get("incumbent") == c["counterparty"]),
        None,
    )
    # A counter of ours is not their proposal: only what the MM itself put forward.
    if not bid or not bid.get("options") or str(bid.get("kind", "")).startswith("our counter"):
        return ""
    spot = float(data.peaq.price) if data.peaq and data.peaq.price is not None else None
    sig = data.peaq_sigma
    cost = bid_cost(bid, spot, sig[0] if sig else None, sig[1] if sig else 0,
                    (data.rfq or {}).get("carry_rate_pct"), None)  # fmt: skip
    loan = cost["loan_peaq"] or 0.0
    held = float(c.get("outstanding") or 0)
    ref = bid.get("ref_price")
    months = float(bid.get("tenor_months") or 12)
    s_ann = cost["sigma_annual"]
    new = loan - held
    head = (f"Proposed · not signed · {_e(bid.get('kind', ''))} · "
            f"{_m(Decimal(str(round(loan))))} PEAQ for {months:g} months"
            + (f" ({'+' if new >= 0 else ''}{_m(Decimal(str(round(new))))} on the {_m(Decimal(str(round(held))))} held)"
               if held else ""))  # fmt: skip
    rows = []
    for i, o in enumerate(bid["options"], 1):
        qty = float(o["peaq"]) if o.get("peaq") is not None else float(o.get("share", 1)) * loan
        pct = o.get("strike_pct")
        k_quote: float | None
        k_now: float | None
        if o.get("strike_usd") is not None:
            k_quote = k_now = float(o["strike_usd"])
            basis = "Fixed"
        else:
            k_quote = float(ref) * float(pct) / 100 if ref else None
            k_now = spot * float(pct) / 100 if spot is not None else None
            basis = f"{float(pct):g}% of spot at signing" + (
                f" (quoted on {_price(Decimal(str(ref)))})" if ref else ""
            )
        k_show = k_quote if k_quote is not None else k_now
        t = float(o["months"]) / 12 if o.get("months") is not None else months / 12
        fv = (qty * bs_call(spot, k_now, s_ann, t)
              if spot is not None and s_ann and k_now is not None else None)  # fmt: skip
        rows.append(
            f"<tr><td>P{i}</td><td>{_m(Decimal(str(round(qty))))}</td>"
            f"<td>{_e(_price(Decimal(str(k_show))) if k_show is not None else 'not fixed')}</td>"
            f'<td class="asof">{_e(basis)}</td>'
            f"<td>{f'{k_now / spot:.1f}×' if k_now is not None and spot else '—'}</td>"
            f"<td>{t * 12:g} months</td>"
            f"<td>{_e(_usd(fv)) if fv is not None else '—'}</td></tr>"
        )
    total = cost["options_usd"] * cost["years"] if cost["options_usd"] is not None else None
    return (
        f'<div class="blk">{head}</div>'
        '<table class="grid ladder"><thead><tr><th>#</th><th>Tokens</th><th>Strike</th>'
        "<th>Basis</th><th>× spot</th><th>Term</th><th>Fair value now</th></tr></thead><tbody>"
        + "".join(rows)
        + f"</tbody><tfoot><tr><td>Total</td><td>{_m(Decimal(str(round(loan))))}</td>"
        "<td></td><td></td><td></td><td></td>"
        f"<td>{_e(_usd(total)) if total is not None else '—'}</td></tr></tfoot></table>"
        '<p class="asof">Fair value: Black–Scholes at PEAQ now and its own volatility'
        + (f" ({s_ann:.0%}/yr)" if s_ann else "")
        + ", strike set at signing. Terms and depth in the RFQ tab.</p>"
    )


def _terms(c: dict) -> dict:
    import json

    try:
        terms = json.loads(c.get("key_terms_json") or "{}")
    except ValueError:
        return {}
    return terms if isinstance(terms, dict) else {}


def _is_exit_term(key: str) -> bool:
    """Early-termination terms render in each MM's card, not the grid."""
    return key.startswith(("peaq's right to terminate", "Cost if ", "Exit, short"))


_VENUE_LABEL = {"gate": "Gate", "kucoin": "KuCoin", "kraken": "Kraken", "bitget": "Bitget",
                "mexc": "MEXC", "crypto.com": "Crypto.com", "bybit": "Bybit", "coinbase": "Coinbase",
                "binance": "Binance", "bithumb": "Bithumb", "coinw": "CoinW", "bitvavo": "Bitvavo"}  # fmt: skip
_MX_ROWS = (
    ("depth_200", "±2% depth"),
    ("depth_100", "±1% depth"),
    ("depth_50", "±0.5% depth"),
    ("spread", "Spread"),
    ("on_target", "Days on target · ±2%"),
    ("all_kpis", "Days on target · all KPIs"),
    ("rest", "Rest of book · ±2%"),
)


def _mx_fmt(metric: str, v: float | None) -> str:
    if v is None:
        return "—"
    if metric == "spread":
        return f"{v:.2f}%"
    if metric in ("on_target", "all_kpis"):
        return f"{v:.0f}%"
    return _k(v)


def _target_line(t: dict) -> str:
    """A contract's KPIs as one line: venues · spread · depth at each band."""
    parts = [", ".join(_VENUE_LABEL.get(v, v) for v in t.get("venues", []))]
    if t.get("spread_pct") is not None:
        parts.append(f"spread ≤{t['spread_pct']:.2f}%")
    for key, band in (
        ("depth_05pct_usd", "0.5%"),
        ("depth_1pct_usd", "1%"),
        ("depth_2pct_usd", "2%"),
    ):
        if t.get(key) is not None:
            cap = t.get("book_share_cap") if key == "depth_2pct_usd" else None
            parts.append(
                f"{_k(t[key])} @{band}" + (f" (or {cap * 100:.0f}% of book, lower)" if cap else "")
            )
    for label, usd in (t.get("dex_required") or {}).items():
        parts.append(f"{_k(usd)} on {label}")
    if t.get("binding"):
        parts.append(f"binds {t['binding']}")
    return " · ".join(parts)


def _delivery_matrix(data: ReportData) -> str:
    """Delivered vs contracted: one block per MM, a row per metric, a column
    per venue (CEX from Coinwatch, then DEX from the book and CoinGecko).
    Each cell is the last-90-day median over the full-history median, coloured
    against the contract's KPI on the venues the contract names."""
    from defi_tracker.analytics import _grade, _venue_key, dex_depth, mm_delivery

    q = data.rfq
    m = mm_delivery(data.delivery, q.get("target", []))
    if not m:
        return ('<div class="card"><div class="blk">Delivered vs contracted</div><p class="empty">'
                "Coinwatch history not read for this build (COINWATCH_DB).</p></div>")  # fmt: skip
    vol = {_venue_key(v["venue"]): v["volume_usd"] for v in data.venues}
    cex = sorted(m["venues"], key=lambda v: (-vol.get(v, 0.0), v))  # by 24h volume, as Venues
    dex = dex_depth(q.get("dex", []), data.actions.plans if data.actions else [], data.venues)
    ncol = 1 + len(cex) + len(dex)
    (s90, e90), (sf, _) = m["window"], m["full"]
    out = ['<div class="card"><div class="blk">Delivered vs contracted · median, last 90 days '
           f"({_e(s90)} → {_e(e90)}) over full history (from {_e(sf)}) · Coinwatch, own book</div>"
           '<table class="grid mx"><thead><tr><th>Metric</th>'
           + "".join(f"<th>{_e(_VENUE_LABEL.get(v, v))}</th>" for v in cex)
           + "".join(f'<th class="{"dex" if i == 0 else ""}">{_e(d["label"])}<small>DEX</small></th>'
                     for i, d in enumerate(dex))
           + "</tr></thead><tbody>"]  # fmt: skip

    def dex_cells(fill) -> str:
        return "".join(f'<td class="{"dex0 " if i == 0 else ""}{c}">{t}</td>'
                       for i, (t, c) in enumerate(fill(d) for d in dex))  # fmt: skip

    for b in m["blocks"]:
        t = b["target"]
        out.append(f'<tr class="blk"><td colspan="{ncol}">{_e(b["mm"])}'
                   f'<span class="asof"> · contract: {_e(_target_line(t) or "not keyed")}'
                   f'{" · " + _e(t["source"]) if t.get("source") else ""}</span></td></tr>')  # fmt: skip
        for metric, label in _MX_ROWS:
            row = b["cells"].get(metric)
            if not row:
                continue
            cells = []
            for v in cex:
                c = row.get(v)
                if c is None:
                    contracted = v in b["contracted"]
                    cells.append(f'<td class="{"bad" if contracted else ""}">'
                                 f'{"no data" if contracted else ""}</td>')  # fmt: skip
                    continue
                tgt = ""  # the KPI is in the block's header line
                cells.append(f'<td class="{c["status"]}">{_mx_fmt(metric, c["v90"])}'
                             f"<small>{_mx_fmt(metric, c['full'])} full{tgt}</small></td>")  # fmt: skip
            req = t.get("dex_required") or {}

            held = {x["venue"]: x for x in t.get("dex_verified") or []}

            def fill(d, metric=metric, req=req, held=held):
                if metric != "depth_200":
                    return "", ""
                hit = [usd for k, usd in req.items() if d["label"].startswith(k)]
                h = held.get(d["label"])
                if h:
                    need = f" · req. {_k(hit[0])}" if hit else ""
                    st = _grade(h["value_usd"], hit[0]) if hit else ""
                    return (
                        f"{_k(h['value_usd'])} held<small>{_e(h.get('note', ''))}{need}</small>",
                        st,
                    )
                if hit and not held:
                    return f"req. {_k(hit[0])}<small>unverified · needs wallets</small>", "warn"
                return "", ""

            out.append(
                f'<tr><td class="pair">{_e(label)}</td>{"".join(cells)}{dex_cells(fill)}</tr>'
            )

    live = {_venue_key(v["venue"]): v for v in data.venues}
    out.append(f'<tr class="blk"><td colspan="{ncol}">Market<span class="asof"> · whole book, every '
               "MM and the rest</span></td></tr>")  # fmt: skip
    mk = "".join(f"<td>{_k(m['market'][v]['v90'])}<small>{_k(m['market'][v]['full'])} full</small></td>"
                 if v in m["market"] else "<td>—</td>" for v in cex)  # fmt: skip
    out.append(f'<tr><td class="pair">Market total · ±2% (Coinwatch)</td>{mk}'
               + dex_cells(lambda d: (_k(d["pool_usd"]) + "<small>book pool state, now</small>"
                                      if d["pool_usd"] is not None else "—", ""))
               + "</tr>")  # fmt: skip
    cg = "".join(f"<td>{_k(live[v]['depth_up_usd'] + live[v]['depth_down_usd'])}"
                 f"<small>{_k(live[v]['volume_usd'])} 24h vol</small></td>" if v in live else "<td>—</td>"
                 for v in cex)  # fmt: skip
    out.append(f'<tr><td class="pair">CoinGecko now · ±2%, both sides</td>{cg}'
               + dex_cells(lambda d: (_k(d["coingecko_usd"]) + f"<small>{_k(d['volume_usd'])} 24h vol</small>"
                                      if d["coingecko_usd"] is not None else "not on CoinGecko", ""))
               + "</tr>")  # fmt: skip
    out.append(f'<tr><td class="pair">Supplied by peaq\'s book</td>{"<td></td>" * len(cex)}'
               + dex_cells(lambda d: ((f"{d['peaq_share'] * 100:.0f}%" if d["peaq_share"] is not None
                                       else "—") + (f"<small>{_e(d['owner'])}</small>" if d.get("owner") else ""), ""))
               + "</tr>")  # fmt: skip
    out.append("</tbody></table>"
               '<p class="asof">Green: at the KPI · amber: at half or better (spread: within 2×; days: ≥50%) · '
               "red: below. Days on target ≥90% is green. Colour only on venues the contract names. "
               "Coinwatch figures as reported.</p></div>")  # fmt: skip
    return "".join(out)


def _delivered(data: ReportData) -> dict[str, dict[str, dict]]:
    """{mm: {metric: {venue: median of the last 7 complete days}}} — what each
    incumbent delivers now (a venue it has left counts as what it is now, not
    at its 90-day median). Rows without a 7-day figure fall back to 90 days."""
    from defi_tracker.analytics import mm_delivery

    m = mm_delivery(data.delivery, data.rfq.get("target", []))
    return {
        b["mm"]: {
            k: {v: c.get("v7", c["v90"]) for v, c in row.items()} for k, row in b["cells"].items()
        }
        for b in m.get("blocks", [])
    }


def _delivered_d2(got: dict[str, dict[str, dict]], mm: str | None) -> float | None:
    """An incumbent's ±2% depth summed over every venue it is seen on."""
    d = (got.get(mm or "") or {}).get("depth_200") or {}
    vals = [v for v in d.values() if v is not None]
    return sum(vals) if vals else None


def _dex_held(data: ReportData, mm: str, label: str, req: dict) -> str:
    """An incumbent's DEX column in the grid: liquidity verified on-chain in
    pools whose label starts with the required venue, against the requirement."""
    need = req.get(mm, {}).get(label)
    if need is None:
        return ""
    held = [x for t in data.rfq.get("target", []) if t.get("mm") == mm
            for x in t.get("dex_verified") or [] if x["venue"].startswith(label)]  # fmt: skip
    if not held:
        return f"req. {_k(need)}<small>unverified</small>"
    return f"{_k(sum(x['value_usd'] for x in held))} held<small>req. {_k(need)} · verified on-chain</small>"


def _bids_grid(data: ReportData) -> str:
    """Each bid's commitment on the matrix's grid: rows grouped by metric,
    incumbents' delivered (last-7-day median) above each bid's committed figure,
    a column per venue — listed spot by volume, then venues where PEAQ is not
    listed, perps, DEX. A committed cell is green where it beats what the
    seat's incumbent delivers there, red where it is below."""
    from defi_tracker.analytics import _venue_key, bid_commitments

    bids = data.rfq.get("bid", [])
    if not bids:
        return ""
    vol = {_venue_key(v["venue"]): v["volume_usd"] for v in data.venues}
    listed = set(vol) if data.venues else None
    got = _delivered(data)
    com = [(b, bid_commitments(b, listed)) for b in bids]
    spot = {v for c in got.values() for row in c.values() for v in row}
    spot |= {v for _, c in com for row in c["cells"].values() for v in row}
    perps = sorted(v for v in spot if v.endswith(" perp"))
    spot = {v for v in spot if not v.endswith(" perp")}
    cols = sorted(spot, key=lambda v: (listed is not None and v not in listed, -vol.get(v, 0.0), v))
    dex = sorted({d["venue"] for b in bids for d in b.get("dex", [])}
                 | {k for t in data.rfq.get("target", []) for k in (t.get("dex_required") or {})})  # fmt: skip
    head = (
        "".join(
            f"<th>{_e(_VENUE_LABEL.get(v, v.title()))}"
            + ("<small>not listed</small>" if listed is not None and v not in listed else "")
            + "</th>"
            for v in cols
        )
        + "".join(
            f"<th>{_e(v.replace(' perp', '').title())} perp"
            + (
                "<small>no live market</small>"
                if (data.orderbooks or {}).get(v, {}).get("status") in ("not listed", "empty")
                else ""
            )
            + "</th>"
            for v in perps
        )
        + "".join(
            f'<th class="{"dex" if i == 0 else ""}">{_e(d)}<small>DEX</small></th>'
            for i, d in enumerate(dex)
        )
    )
    ncol = 2 + len(cols) + len(perps) + len(dex)
    out = ['<div class="card" style="margin-top:10px"><div class="blk">Bids on the same grid · '
           "committed vs incumbents' delivered now (last-7-day median, Coinwatch)</div>"
           f'<table class="grid mx"><thead><tr><th>Metric · who</th>{head}<th>Listed spot ±2%</th>'
           "</tr></thead><tbody>"]  # fmt: skip
    req = {t["mm"]: t.get("dex_required") or {} for t in data.rfq.get("target", []) if t.get("mm")}
    for metric, label in (("depth_200", "±2% depth"), ("depth_100", "±1% depth"),
                          ("depth_50", "±0.5% depth"), ("spread", "Spread")):  # fmt: skip
        fmt = (lambda x: f"{x:.2f}%") if metric == "spread" else _k
        out.append(f'<tr class="blk"><td colspan="{ncol}">{_e(label)}</td></tr>')
        for mm, cells in got.items():
            row = cells.get(metric) or {}
            if not row:
                continue
            tds = "".join(
                f"<td>{fmt(row[v]) if row.get(v) is not None else ''}</td>" for v in cols + perps
            )
            tds += "".join(
                f'<td class="{"dex0" if i == 0 else ""}">'
                + (_dex_held(data, mm, d, req) if metric == "depth_200" else "")
                + "</td>"
                for i, d in enumerate(dex)
            )  # fmt: skip
            tot = _delivered_d2(got, mm) if metric == "depth_200" else None
            out.append(
                f'<tr><td class="pair">{_e(mm)} · delivered</td>{tds}<td>{_k(tot) if tot else ""}</td></tr>'
            )
        for b, c in com:
            row = c["cells"].get(metric) or {}
            if metric != "depth_200" and not row:
                continue
            if not c["cells"]:
                out.append(f'<tr><td class="pair">{_e(b.get("name", "?"))} · committed</td>'
                           f'<td class="asof" colspan="{ncol - 1}" style="text-align:left">'
                           "no depth committed in $</td></tr>")  # fmt: skip
                continue
            inc = (got.get(b.get("incumbent") or "") or {}).get(metric) or {}
            bid_tds: list[str] = []
            for v in cols + perps:
                e = row.get(v)
                if e is None:
                    bid_tds.append('<td class="asof">not quoted</td>')
                    continue
                if e["group"]:
                    bid_tds.append(
                        f"<td>{_e(e['group'])}: {fmt(e['usd'])}<small>aggregated</small></td>"
                    )
                    continue
                d = inc.get(v)
                better = (
                    None if d is None else (e["usd"] <= d if metric == "spread" else e["usd"] >= d)
                )
                cls = "" if d is None else ("ok" if better else "bad")
                vs = f"<small>vs {fmt(d)} delivered</small>" if d is not None else ""
                bid_tds.append(f'<td class="{cls}">{fmt(e["usd"])}{vs}</td>')
            dx = {d["venue"]: d for d in b.get("dex", [])}
            bid_tds += [
                f'<td class="{"dex0" if i == 0 else ""}">'
                + (f"TVL {_k(dx[d]['tvl_usd'])}<small>TVL, not ±2% depth</small>" if metric == "depth_200" and d in dx else "")
                + "</td>"
                for i, d in enumerate(dex)
            ]  # fmt: skip
            tot = c["listed_d2_usd"] if metric == "depth_200" else None
            perp = (
                f"<small>+{_k(c['perp_d2_usd'])} perps</small>"
                if metric == "depth_200" and c["perp_d2_usd"]
                else ""
            )
            out.append(f'<tr><td class="pair">{_e(b.get("name", "?"))} · committed</td>{"".join(bid_tds)}'
                       f"<td>{_k(tot) if tot else '—'}{perp}</td></tr>")  # fmt: skip
    groups = [f"{b.get('name')}: {g['group']} = {', '.join(_VENUE_LABEL.get(v, v.title()) for v in g['venues'])}"
              for b, c in com for g in c["groups"]]  # fmt: skip
    out.append("</tbody></table>"
               '<p class="asof">Listed spot ±2%: committed depth on venues where PEAQ trades spot (CoinGecko '
               "now); perps, DEX TVL and venues without a PEAQ market are shown, not summed. An aggregated "
               "commitment counts once. "
               + (_e(" · ".join(groups)) + "." if groups else "") + "</p></div>")  # fmt: skip
    return "".join(out)


def _rfq_context(data: ReportData) -> dict:
    """Everything the RFQ tab's summaries share, computed once: PEAQ now, σ,
    listed venues, incumbents' delivery and every bid's commitment and cost."""
    from defi_tracker.analytics import _venue_key, bid_commitments, bid_cost, mm_delivery

    spot = float(data.peaq.price) if data.peaq and data.peaq.price is not None else None
    sig = data.peaq_sigma
    listed = {_venue_key(v["venue"]) for v in data.venues} if data.venues else None
    carry = data.rfq.get("carry_rate_pct")
    bids = []
    for b in data.rfq.get("bid", []):
        com = bid_commitments(b, listed)
        cost = bid_cost(b, spot, sig[0] if sig else None, sig[1] if sig else 0, carry,
                        com["listed_d2_usd"])  # fmt: skip
        bids.append({"bid": b, "com": com, "cost": cost})
    m = mm_delivery(data.delivery, data.rfq.get("target", []))
    return {"spot": spot, "sig": sig, "listed": listed, "bids": bids, "matrix": m,
            "blocks": {b["mm"]: b for b in m.get("blocks", [])},
            "book": {c["counterparty"]: c for c in data.mm_book}}  # fmt: skip


def _seat_cards(data: ReportData, ctx: dict) -> str:
    """One card per seat: the call, when to act, one line of why, then what
    to request and what to check. The numbers live in the tables above."""
    seats = data.rfq.get("seat", [])
    if not seats:
        return ""
    today = datetime.now(UTC).date()
    out = ['<div class="cols seats">']
    for st in seats:
        c = ctx["book"].get(st.get("incumbent", st.get("seat", "")), {})
        exit_by = st.get("exit_by")
        act = (f"{_e(exit_by)} ({(date.fromisoformat(exit_by) - today).days} days) · {_e(st.get('exit_how', ''))}"
               if exit_by else _e(st.get("exit_how", "—")))  # fmt: skip

        def items(key: str, st: dict = st) -> str:
            return "".join(f"<li>{_e(x)}</li>" for x in st.get(key, []))

        out.append(
            f'<div class="card seat"><div class="blk">{_e(st.get("seat", ""))}'
            + (f" · ends {_e(c['end_date'])}" if c.get("end_date") else "")
            + "</div>"
            f'<div class="call">{_e(st.get("call", "—"))}</div>'
            f'<dl class="kv2 terms"><dt>Act by</dt><dd>{act}</dd><dt>Why</dt><dd>{_e(st.get("why", ""))}</dd>'
            + (f'<dt>Exit cost</dt><dd>{_e(st["exit_cost"])}</dd>' if st.get("exit_cost") else "")
            + (f'<dt>Size</dt><dd>{_e(st["size_note"])}</dd>' if st.get("size_note") else "")
            + "</dl>"
            + (f'<div class="blk">Request</div><ul class="why">{items("request")}</ul>' if st.get("request") else "")
            + (f'<div class="blk">Check</div><ul class="why">{items("check")}</ul>' if st.get("check") else "")
            + "</div>"
        )  # fmt: skip
    out.append("</div>")
    return "".join(out)


def _performance_table(data: ReportData, ctx: dict) -> str:
    """Today's MMs in one table: a row per MM, a column per venue. Each cell:
    the ±2% depth it keeps now (median of the last 7 days), with its 90-day
    median and how often it met its contract beneath. The bottom rows check
    the whole book two ways: Coinwatch's total and the exchange's own public
    order book read at build — an independent source, not the MM's word."""
    from defi_tracker.analytics import _grade, _venue_key

    m = ctx["matrix"]
    if not m:
        return '<div class="card"><p class="empty">Coinwatch history not read for this build.</p></div>'
    vol = {_venue_key(v["venue"]): v["volume_usd"] for v in data.venues}
    cex = sorted(m["venues"], key=lambda v: (-vol.get(v, 0.0), v))
    out = ['<div class="card"><table class="grid mx perf"><thead><tr><th>Market maker</th>'
           "<th>Contract asks</th>"
           + "".join(f"<th>{_e(_VENUE_LABEL.get(v, v))}</th>" for v in cex)
           + '<th class="dex">DEX (on-chain)</th><th>Total ±2% now</th></tr></thead><tbody>']  # fmt: skip
    for b in m["blocks"]:
        t, cells = b["target"], b["cells"]
        ask = []
        if t.get("depth_2pct_usd") is not None:
            ask.append(f"{_k(t['depth_2pct_usd'])} per venue")
        if t.get("spread_pct") is not None:
            ask.append(f"spread ≤{t['spread_pct']:.2f}%")
        if t.get("binding"):
            ask.append(f"binds {t['binding']}")
        if b.get("floor"):
            ask.append(
                f"measured at ±{float(t['depth_measured_at_pct']):g}%: the ±2% figure here is a floor"
            )
        tds = []
        for v in cex:
            d = (cells.get("depth_200") or {}).get(v)
            if d is None:
                tds.append('<td class="bad">no data</td>' if v in b["contracted"] else "<td></td>")
                continue
            on = (cells.get("on_target") or {}).get(v)
            sub = f"90-day {_k(d['v90'])}"
            if on and on["v90"] is not None:
                sub += f" · on target {on['v90']:.0f}% of days"
            st = _grade(d.get("v7"), d.get("target"))
            st = (
                st if not b.get("floor") or st == "ok" else ""
            )  # a ±4% contract read at ±2%: a miss is unknown
            tds.append(f'<td class="{st}">{_k(d.get("v7"))}<small>{_e(sub)}</small></td>')
        dv = t.get("dex_verified") or []
        req = t.get("dex_required") or {}
        if dv:
            dex = " · ".join(f"{_e(x['venue'])} {_k(x['value_usd'])}" for x in dv)
            need = f"needs {_k(sum(req.values()))} · " if req else ""
            dex_td = f'<td class="dex0 warn">{dex}<small>{need}wide ranges: little ±2% depth</small></td>'
        elif req:
            dex_td = f'<td class="dex0 bad">not verified<small>needs {_k(sum(req.values()))}</small></td>'
        else:
            dex_td = '<td class="dex0"></td>'
        now = [
            x.get("v7") for x in (cells.get("depth_200") or {}).values() if x.get("v7") is not None
        ]
        then = [x["v90"] for x in (cells.get("depth_200") or {}).values() if x["v90"] is not None]
        out.append(f'<tr><td class="pair"><strong>{_e(b["mm"])}</strong></td>'
                   f'<td class="wrapcell"><small>{_e(" · ".join(ask))}</small></td>{"".join(tds)}{dex_td}'
                   f"<td><strong>{_k(sum(now) if now else None)}</strong><small>90-day {_k(sum(then) if then else None)}</small></td></tr>")  # fmt: skip
    mk = "".join(
        f"<td>{_k(m['market'][v].get('v7'))}<small>90-day {_k(m['market'][v]['v90'])}</small></td>"
        if v in m["market"] else "<td>—</td>" for v in cex
    )  # fmt: skip
    out.append(
        f'<tr class="sub"><td class="pair">Whole book · Coinwatch</td><td></td>{mk}<td class="dex0"></td><td></td></tr>'
    )
    ob = data.orderbooks or {}
    if ob:
        cells_ob = []
        for v in cex:
            x = ob.get(v)
            if not x:
                cells_ob.append("<td>—</td>")
            elif x["status"] != "ok":
                cells_ob.append(f'<td class="asof">{_e(x["status"])}</td>')
            else:
                cv = (m["market"].get(v) or {}).get("v7")
                gap = (x["d2"] / cv - 1) * 100 if cv else None
                flag = "warn" if gap is not None and abs(gap) > 50 else ""
                note = f"spread {x['spread_pct']:.2f}%" + (
                    f" · {gap:+.0f}% vs Coinwatch" if gap is not None else ""
                )
                cells_ob.append(f'<td class="{flag}">{_k(x["d2"])}<small>{_e(note)}</small></td>')
        out.append('<tr class="sub"><td class="pair">Whole book · exchange API, now</td><td></td>'
                   + "".join(cells_ob) + '<td class="dex0"></td><td></td></tr>')  # fmt: skip
    (s7, e7), (s90, e90) = m["last7"], m["window"]
    out.append("</tbody></table>"
               f'<p class="asof">±2% depth = dollars of orders within 2% of the price, both sides. Headline: median of '
               f"the last 7 days ({_e(s7)} → {_e(e7)}); beneath, the 90-day median ({_e(s90)} → {_e(e90)}). MM figures "
               "are Coinwatch's (it attributes orders to each MM); the last row is each exchange's own public order "
               "book at build — amber where it differs from Coinwatch by more than half. Colour (on the 7-day figure): "
               "green meets the contract, amber at least half, red below half. Days on target are over 90 days.</p></div>")  # fmt: skip
    return "".join(out)


def _arrow(today: str, offer: str) -> str:
    return f'<span class="asof">{_e(today)}</span> → <strong>{_e(offer)}</strong>'


def _rfq_compare(data: ReportData, ctx: dict) -> str:
    """The RFQ in one short table: a column per offer, each row today → offer.
    No prose: what is new and what to ask for lives in the Decisions cards."""
    bids = ctx["bids"]
    if not bids:
        return ""
    got = _delivered(data)
    ob = data.orderbooks or {}
    listed = ctx["listed"]
    best = min((x["cost"]["per_1k"] for x in bids if x["cost"]["per_1k"] is not None), default=None)

    def live(v: str) -> bool:
        st = (ob.get(v) or {}).get("status")
        return bool(st == "ok") if st is not None else (listed is None or v in listed)

    def lend(x: dict) -> str:
        b, new = x["bid"], x["cost"]["loan_peaq"] or 0.0
        today = float(b.get("keeps_peaq") or b.get("replaces_peaq") or 0)
        total = new + float(b.get("keeps_peaq") or 0)
        return _arrow(_m(today) if today else "—", _m(total))

    def depth(x: dict) -> str:
        inc = x["bid"].get("incumbent")
        now = _k(_delivered_d2(got, inc)) if inc else "—"
        promised = x["com"]["listed_d2_usd"]
        es = [e for e in x["bid"].get("depth", []) if e.get("d2") is not None
              and all(v.endswith(" perp") for v in e.get("venues", []))]  # fmt: skip
        perp = sum(float(e["d2"]) for e in es if all(live(v) for v in e["venues"]))
        sub = f"<small>+{_k(perp)} on live perp markets</small>" if perp else ""
        return _arrow(now, _k(promised) if promised else "none in $") + sub

    def cost(x: dict) -> str:
        c = x["cost"]
        if c["total_usd"] is None:
            return "—"
        months = round(c["years"] * 12)
        fee = "" if c["fee_usd"] is not None else " · + fee, not stated"
        return (f"<strong>{_k(c['total_usd'] * c['years'])}</strong> for {months} mo"
                f"<small>≈ {_k(c['total_usd'])} a year{fee}</small>")  # fmt: skip

    def per1k(x: dict) -> str:
        c = x["cost"]
        if c["per_1k"] is not None:
            cls = "ok" if c["per_1k"] == best else ""
            return f'<span class="{cls}"><strong>{_k(c["per_1k"])}</strong></span>'
        inc = _delivered_d2(got, x["bid"].get("incumbent"))
        if c["total_usd"] is not None and inc:
            return f"<strong>{_k(c['total_usd'] / (inc / 1000))}</strong><small>on depth it delivers now</small>"
        return "—"

    circ = float(data.rfq.get("circulating_supply") or 0) or None

    fdv = float(data.rfq.get("fdv_supply") or 0) or None

    def share(x: dict, basis: float | None, show_new: bool = True) -> str:
        if not basis:
            return "—"
        b, new = x["bid"], x["cost"]["loan_peaq"] or 0.0
        today = float(b.get("keeps_peaq") or b.get("replaces_peaq") or 0)
        total = new + float(b.get("keeps_peaq") or 0)
        added = total - today
        sub = ""
        if show_new:
            sub = ("<small>no new PEAQ</small>" if not added
                   else f"<small>+{_m(added)} new PEAQ</small>" if added > 0
                   else f"<small>{_m(-added)} less PEAQ: no new loan</small>")  # fmt: skip
        return (
            _arrow(f"{today / basis * 100:.2f}%" if today else "—", f"{total / basis * 100:.2f}%")
            + sub
        )

    rows = [
        ("PEAQ lent", [lend(x) for x in bids]),
        ("% of circulating supply", [share(x, circ) for x in bids]),
        ("% of FDV (total supply)", [share(x, fdv, show_new=False) for x in bids]),
        (
            "Option",
            [
                _arrow(x["bid"].get("option_today", "—"), x["bid"].get("option_offer", "—"))
                for x in bids
            ],
        ),
        ("±2% depth", [depth(x) for x in bids]),
        (
            "DEX liquidity",
            [_arrow(x["bid"].get("dex_today", "—"), x["bid"].get("dex_offer", "—")) for x in bids],
        ),
        ("Cost to peaq", [cost(x) for x in bids]),
        ("Cost per $1k depth / yr", [per1k(x) for x in bids]),
    ]
    pending = data.rfq.get("pending", [])
    head = "".join(
        f"<th>{_e(x['bid'].get('name', '?'))}<small>{_e(x['bid'].get('kind', ''))}</small></th>"
        for x in bids
    ) + "".join(
        f"<th>{_e(p.get('name', ''))}<small>{_e(p.get('note', ''))}</small></th>" for p in pending
    )
    out = []
    if circ:
        held = [(c["counterparty"], float(c.get("outstanding") or 0)) for c in data.mm_book]
        tot = sum(q for _, q in held)
        parts = " · ".join(
            f"{_e(n)} {_m(q)} ({q / circ * 100:.2f}% circ"
            + (f" · {q / fdv * 100:.2f}% FDV" if fdv else "")
            + ")"
            for n, q in held
        )
        fdv_txt = f" · {tot / fdv * 100:.2f}% of FDV ({_m(fdv)} total supply)" if fdv else ""
        out.append(f'<p class="pricebar">Lent today <strong>{_m(tot)} PEAQ = {tot / circ * 100:.2f}%</strong>'
                   f"<span>of circulating supply ({_m(circ)}){fdv_txt} · {parts}</span></p>")  # fmt: skip
    out.append('<div class="card"><table class="grid mx compare"><thead><tr><th>today → offer</th>'
               + head + "</tr></thead><tbody>")  # fmt: skip
    for label, cells in rows:
        out.append(f'<tr><td class="pair">{_e(label)}</td>' + "".join(f"<td>{c}</td>" for c in cells)
                   + "<td></td>" * len(pending) + "</tr>")  # fmt: skip
    sig = ctx["sig"]
    out.append("</tbody></table>"
               f'<p class="asof">Depth now: last 7 days (Coinwatch). Promised depth counts only exchanges that list '
               f"PEAQ. Cost = option fair value at PEAQ {_price(ctx['spot'])} and its volatility "
               f"({sig[0] * 365**0.5 * 100:.0f}%/yr) + carry + fee, over each bid's own term. "
               "Per year and per $1k are annualised so terms compare. Lower is better.</p></div>"
               if sig else "</tbody></table></div>")  # fmt: skip
    return "".join(out)


def _variants_table(data: ReportData, ctx: dict) -> str:
    """Other structures for a bid, priced the same way (rfq.toml [[variant]]:
    overrides on the bid named in `of`), one table per bid. Each row: PEAQ
    lent, cost for the bid's term, and that cost per $1k of depth promised,
    delivered now, or of DEX TVL (`tvl_mid_usd` on the bid)."""
    from defi_tracker.analytics import bid_commitments, bid_cost

    vs = data.rfq.get("variant", [])
    if not vs:
        return ""
    by_name = {x["bid"].get("name"): x for x in ctx["bids"]}
    got = _delivered(data)
    sig = ctx["sig"]
    groups: dict[str, list[str]] = {}
    for v in vs:
        base = by_name.get(v.get("of"))
        if not base:
            continue
        b = {**base["bid"], **{k: val for k, val in v.items() if k not in ("of", "name", "note")}}
        # A variant may ask for its own depth; otherwise the bid's promise stands.
        com = bid_commitments(b, ctx["listed"]) if "depth" in v else base["com"]
        c = bid_cost(b, ctx["spot"], sig[0] if sig else None, sig[1] if sig else 0,
                     data.rfq.get("carry_rate_pct"), com["listed_d2_usd"])  # fmt: skip
        # Loan utilisation as the MMs' own sheets put it: ±2% depth (spot and
        # perp) per dollar of PEAQ lent.
        d2 = (com["listed_d2_usd"] or 0) + (com["perp_d2_usd"] or 0)
        used = f"{d2 / c['notional_usd']:.0%}" if d2 and c["notional_usd"] else "—"
        term = c["total_usd"] * c["years"] if c["total_usd"] is not None else None
        inc = _delivered_d2(got, b.get("incumbent"))
        tvl = float(b.get("tvl_mid_usd") or 0)
        if c["per_1k"] is not None:
            per = f"{_k(c['per_1k'])}<small>per $1k promised / yr</small>"
        elif inc and c["total_usd"] is not None:
            per = f"{_k(c['total_usd'] / (inc / 1000))}<small>per $1k delivered now / yr</small>"
        elif tvl and term is not None:
            per = (
                f"{_k(term / (tvl / 1000))}<small>per $1k of TVL ({_k(tvl)}), for the term</small>"
            )
        else:
            per = "—"
        groups.setdefault(v.get("of", ""), []).append(
            f'<tr><td class="pair"><strong>{_e(v.get("name", ""))}</strong><small>{_e(v.get("note", ""))}</small></td>'
            f"<td>{_m(c['loan_peaq'])}</td><td><strong>{_k(term)}</strong> for {round(c['years'] * 12)} mo</td>"
            f"<td>{per}</td><td>{used}<small>{_k(d2) if d2 else ''} ±2%</small></td></tr>"
        )  # fmt: skip
    out = []
    for name, rows in groups.items():
        out.append(f'<div class="card" style="margin-top:10px"><div class="blk">{_e(name)}: other structures</div>'
                   '<table class="grid mx"><thead><tr><th>Structure</th><th>PEAQ</th><th>Cost to peaq</th>'
                   "<th>Cost per $1k</th><th>Depth ÷ loan</th></tr></thead><tbody>"
                   + "".join(rows) + "</tbody></table></div>")  # fmt: skip
    return "".join(out)


def _rfq_section(data: ReportData) -> str:
    """The MM RFQ side by side: each bid's scope, its committed ±2% depth per
    venue beside the market's own depth today (CoinGecko at build), and a
    weighted scorecard. Bids come from content/rfq.toml."""
    from defi_tracker.analytics import rfq_scores

    q = data.rfq
    if not q:
        return ('<h2>RFQ</h2><div class="card"><p class="empty">No RFQ keyed '
                "(content/rfq.toml).</p></div>")  # fmt: skip
    if q.get("error"):
        return f'<h2>RFQ</h2><p class="empty down">RFQ file invalid: {_e(q["error"])}</p>'
    bidders = q.get("bidders", [])
    head = "".join(f"<th>{_e(b)}</th>" for b in bidders)
    out = [
        f'<h2>Market-maker seats · <span class="asof">quotes as of {_e(q.get("as_of", "—"))}</span></h2>'
    ]
    if q.get("note"):
        out.append(f'<p class="asof" style="margin:0 0 10px">{_e(q["note"])}</p>')
    ctx = _rfq_context(data)
    compare, grid = _rfq_compare(data, ctx), _bids_grid(data)
    if compare:
        out.append("<h2>1 · The RFQ: every offer side by side</h2>" + compare)
    var = _variants_table(data, ctx)
    if var:
        out.append(
            '<details class="more"><summary>Other structures and their cost</summary>'
            + var
            + "</details>"
        )
    if grid:
        out.append('<details class="more"><summary>Venue by venue: what each offer promises vs what is '
                   "delivered now</summary>" + grid + "</details>")  # fmt: skip
    out.append("<h2>2 · How today's market makers perform</h2>" + _performance_table(data, ctx))
    out.append('<details class="more"><summary>Full detail: every metric, venue and DEX pool</summary>'
               + _delivery_matrix(data) + "</details>")  # fmt: skip
    seats = _seat_cards(data, ctx)
    if seats:
        out.append("<h2>3 · Decisions</h2>" + seats)

    def cell(v: object) -> str:
        t = str(v) if v is not None else "—"
        return f'<td class="{"asof" if t in ("TBD", "—") else ""}">{_e(t)}</td>'

    if q.get("scope"):
        out.append('<details class="more"><summary>Terms of each proposal</summary><div class="card">'
                   f'<table class="grid terms"><thead><tr><th></th>{head}</tr></thead><tbody>')  # fmt: skip
        for r in q.get("scope", []):
            cells = r.get("cells", {})
            out.append(f'<tr><td class="pair">{_e(r.get("term", ""))}</td>'
                       + "".join(cell(cells.get(b)) for b in bidders) + "</tr>")  # fmt: skip
        out.append("</tbody></table></div></details>")

    crit = q.get("score", [])
    if crit:
        totals = rfq_scores(crit, bidders)
        out.append('<div class="card" style="margin-top:10px"><div class="blk">Scorecard · 1–5, '
                   "weights proposed by the desk</div>"
                   f'<table class="grid"><thead><tr><th>Criterion</th><th>Weight</th>{head}'
                   "</tr></thead><tbody>")  # fmt: skip
        for c in crit:
            sc = c.get("scores") or {}
            out.append(f'<tr><td class="pair">{_e(c.get("criterion", ""))}</td>'
                       f"<td>{_e(c.get('weight', '—'))}%</td>"
                       + "".join(f"<td>{_e(sc.get(b, '—'))}</td>" for b in bidders) + "</tr>")  # fmt: skip
        out.append("</tbody><tfoot><tr><td>Weighted score</td><td>100%</td>"
                   + "".join(f"<td>{totals[b]:.2f}</td>" if totals[b] is not None else "<td>—</td>"
                             for b in bidders) + "</tr></tfoot></table></div>")  # fmt: skip
    if q.get("footnotes"):
        out.append('<details class="more"><summary>Notes and method</summary>'
                   + "".join(f'<p class="asof">{_e(n)}</p>' for n in q["footnotes"]) + "</details>")  # fmt: skip
    return "".join(out)


def _venues_section(data: ReportData) -> str:
    """Where PEAQ trades (CoinGecko at build) against where each MM is
    contracted to quote: the coverage gap in one table."""
    from defi_tracker.analytics import venue_matrix

    if not data.venues:
        return ('<h2>Venues</h2><div class="card"><p class="empty">Markets not read for this '
                "build (CoinGecko unavailable).</p></div>")  # fmt: skip
    m = venue_matrix(data.venues, data.mm_book)
    out = [
        f'<h2>Venues <span class="asof">CoinGecko · 24h volume {_e(_usd(m["total_volume_usd"]))}</span></h2>'
    ]
    if m["uncovered_pct"] is not None:
        out.append(f'<p class="pricebar">No MM contracted on <strong>{m["uncovered_pct"]:.0f}%</strong>'
                   "<span>of PEAQ's 24h volume trades on exchanges with no MM contract</span></p>")  # fmt: skip
    out.append('<div class="card"><table class="grid"><thead><tr><th>Venue</th><th>Share</th>'
               "<th>24h volume</th><th>+2% depth</th><th>−2% depth</th><th>Spread</th>"
               + "".join(f"<th>{_e(mm)}</th>" for mm in m["mms"]) + "</tr></thead><tbody>")  # fmt: skip
    for r in m["rows"]:
        if r["share_pct"] is not None and r["share_pct"] < 0.5:
            continue
        name = r["venue"] + (" · DEX" if r["dex"] else "")
        spread = f"{r['spread_pct']:.2f}%" if r["spread_pct"] is not None else "—"
        flags = "".join(
            f"<td>{'✓' if r['mm'].get(mm) else ('' if r['dex'] else '—')}</td>" for mm in m["mms"]
        )
        gap = not r["dex"] and not any(r["mm"].values())
        out.append(
            f'<tr><td class="pair{" down" if gap else ""}">{_e(name)}</td>'
            f"<td>{r['share_pct']:.1f}%</td><td>{_e(_usd(r['volume_usd']))}</td>"
            f"<td>{_e(_usd(r['depth_up_usd']))}</td><td>{_e(_usd(r['depth_down_usd']))}</td>"
            f"<td>{_e(spread)}</td>{flags}</tr>"
        )
    out.append("</tbody></table></div>")
    out.append('<div class="card" style="margin-top:10px"><div class="blk">Contracted, but PEAQ is '
               "not listed there</div><dl class=\"kv2 terms\">")  # fmt: skip
    for mm, vs in m["unlisted"].items():
        out.append(
            f"<dt>{_e(mm)}</dt><dd>{_e(', '.join(v.upper() if len(v) <= 4 else v.title() for v in vs) or '—')}</dd>"
        )
    out.append("</dl></div>")
    out.append('<p class="asof">Red: no MM contracted there. DEX pools are not matched to MM '
               "obligations; the PancakeSwap Infinity pool is peaq's own liquidity.</p>")  # fmt: skip
    return "".join(out)


def _key_terms_grid(book: list[dict]) -> str:
    """Term-sheet grid: one row per term, one column per MM, short cells as
    keyed in the ledger (`key_terms_json`). Source documents on hover."""
    import json

    cols = []
    for c in book:
        try:
            terms = json.loads(c.get("key_terms_json") or "{}")
        except ValueError:
            terms = {}
        cols.append((c, terms))
    keys: list[str] = []
    for _, t in cols:
        keys += [k for k in t if k not in keys and not _is_exit_term(k)]
    if not keys:
        return ""
    out = ['<h2>Key terms</h2><div class="card"><table class="grid terms"><thead><tr><th></th>']
    for c, _ in cols:
        out.append(f'<th title="{_e(c["source_note"])}">{_e(c["counterparty"])}</th>')
    out.append("</tr></thead><tbody>")
    for k in keys:
        out.append(f'<tr><td class="pair">{_e(k)}</td>')
        for _, t in cols:
            v = t.get(k, "—")
            cls = "down" if str(v).lower().startswith(("not on file", "missing")) else ""
            out.append(f'<td class="{cls}">{_e(v)}</td>')
        out.append("</tr>")
    out.append("</tbody></table></div>")
    return "".join(out)


def _signed_usd(v: float) -> str:
    return ("+" if v >= 0 else "−") + _usd(abs(v))


def _actions_section(data: ReportData) -> str:
    """The action queue as plan cards — one per pool, in plain words: what
    to do, the price range in dollars, what it earns, why, and how far to
    trust it. The working (execution steps, the arithmetic) is one click
    away. Snoozed tickets are counted, not shown; unscored positions are
    listed with the reason, so a missing card never reads as "nothing to do"."""
    from defi_tracker.analytics import GROUP_ORDER, GROUP_TITLES, plan_cards

    q = data.actions
    out: list[str] = []
    a = out.append
    if q is None:
        a("<h2>Action queue</h2>")
        a('<div class="card"><p class="empty">Not computed for this page.</p></div>')
        return "".join(out)
    cards = plan_cards(q, data.snoozed_uids)
    shown = sum(len(c.steps) for c in cards)
    n_snoozed = len(q.tickets) - shown
    total = sum(c.total_usd for c in cards)
    a(
        f"<h2>Action queue · {shown} ticket{'s' if shown != 1 else ''}, "
        f"{_e(_usd(total))} over {q.horizon_days:.0f}d"
        + (f" · {n_snoozed} snoozed" if n_snoozed else "")
        + "</h2>"
    )
    from defi_tracker.analytics import backtest_verdict

    verdict = backtest_verdict(data.backtest, {t.chain for t in q.tickets})
    tone = "ok" if verdict.startswith("Backtested") else "warn"
    a(f'<div class="card verdict {tone}"><p><strong>{_e(verdict)}.</strong></p></div>')
    a(
        '<p class="intro">EV = expected fees − expected IL over 30 days vs leaving it. '
        "Propose-only: execute from the multisig.</p>"
    )
    if not cards:
        a('<div class="card"><p class="empty">No action pays over the horizon.</p></div>')

    for group in GROUP_ORDER:
        group_cards = [c for c in cards if c.group == group]
        if not group_cards:
            continue
        title, sub = GROUP_TITLES[group]
        a(f'<div class="group-hd">{_e(title)}</div><p class="group-sub">{_e(sub)}</p>')
        a('<div class="plans">')
        for c in group_cards:
            legs = f" · {len(c.steps)} legs, in order" if len(c.steps) > 1 else ""
            a(
                f'<div class="plan"><div class="plan-hd"><span class="pool">{_e(c.pair)}</span>'
                f'<span class="venue">{_e(c.venue)}{legs}</span>'
                f'<span class="ev">EV {_e(_signed_usd(c.total_usd))} / {c.horizon_days:.0f}d</span></div>'
            )
            for n, v in enumerate(c.steps, 1):
                no = f'<span class="no">{n}</span>' if len(c.steps) > 1 else ""
                a(
                    f'<div class="leg"><div class="leg-hd">{no}<span class="act">{_e(v.action)}</span>'
                    f'<span class="ref">{_e(v.ref)}</span>'
                    f'<span class="tag {v.conviction}">{v.conviction.upper()}</span>'
                    f'<span class="ev">{_e(v.ev)}<small>{_e(v.ev_pct)}</small></span></div>'
                )
                why = next((val for lab, val in v.rows if lab == "Why"), "")
                a(f'<p class="intro">{_e(why)}</p>')
                a('<details><summary>Ticket and calculation</summary><dl class="kv2">')
                for label, value in v.rows:
                    key = ' class="key"' if label == "Deploy" else ""
                    a(f"<dt{key}>{_e(label)}</dt><dd{key}>{_e(value)}</dd>")
                a('</dl><div class="lines">')
                a("".join(f"<div>{_e(x)}</div>" for x in v.details + v.math))
                a("</div></details></div>")
            a("</div>")
        a("</div>")
    if q.unscored:
        a('<div class="group-hd">Not scored</div>')
        a('<p class="group-sub">unknown, not zero — the reason is given for each</p>')
        a('<div class="card">')
        for row, why in q.unscored:
            a(
                f'<p class="empty">{_e(row["pair_label"])} '
                f"({_e(_CHAIN_LABEL.get(row['chain'], row['chain']))}, "
                f"{_e(_usd(row['current_value_usd']))}) — {_e(why)}</p>"
            )
        a("</div>")
    return "".join(out)


def _optimiser_section(data: ReportData) -> str:
    """Per pool, the band that maximises the book's expected fees − IL for
    the capital it has there, with σ's band on the width and the EV."""
    from defi_tracker.analytics import band_pct_label

    q = data.actions
    if q is None or not q.plans:
        return ""
    out: list[str] = []
    a = out.append
    a(f"<h2>Range optimiser · {q.horizon_days:.0f}-day horizon</h2>")
    a('<div class="card"><table><thead><tr><th>Pool</th><th>Capital</th><th>Fees/day</th>'
      "<th>σ/day (n)</th><th>Book's depth</th><th>Best band</th><th>EV there</th>"
      "<th>ε 0 / 1</th><th>As it sits</th></tr></thead><tbody>")  # fmt: skip
    notes: list[str] = []
    for plan in sorted(q.plans, key=lambda p: -p.capital_usd):
        ps = plan.state
        chain, _, pair = ps.key
        name = f'<td class="pair">{_e(pair)} <span class="asof">{_e(_CHAIN_LABEL.get(chain, chain))}</span></td>'
        if plan.reason or plan.best is None or plan.ev_band is None:
            a(f'<tr>{name}<td colspan="8" class="asof">{_e(plan.reason or "no answer")}</td></tr>')
            continue
        lo, hi = plan.half_pct or (0.0, 0.0)
        a(
            f"<tr>{name}<td>{_e(_usd(plan.capital_usd))}</td>"
            f"<td>{_e(_usd(ps.fees_per_day))}</td>"
            f"<td>{(ps.sigma or 0) * 100:.1f}% ({ps.n_obs})</td>"
            f"<td>{_e(_pct((ps.book_share or 0) * 100, 0))}</td>"
            f"<td>{_e(band_pct_label(lo, hi, plan.best.ticks))}</td>"
            f"<td>{_e(_usd(plan.best.ev.ev_usd))} "
            f'<span class="asof">({_e(_usd(plan.ev_band[0]))} … {_e(_usd(plan.ev_band[1]))})</span></td>'
            + (
                f"<td>{_e(_usd(plan.volume_band[0]))} / {_e(_usd(plan.volume_band[1]))}</td>"
                if plan.volume_band
                else "<td>—</td>"
            )
            + f"<td>{_e(_usd(plan.today_ev))}</td></tr>"
        )
        notes.extend(f"{pair}: {n}." for n in plan.notes)
    a("</tbody></table>")
    from defi_tracker.analytics import DEFAULT_REBALANCE_CONFIG

    _cfg = data.rebalance_config or DEFAULT_REBALANCE_CONFIG
    lo_b, hi_b = _cfg.depth_bounds
    e_lo, e_hi = _cfg.elasticity_band[0], _cfg.elasticity_band[-1]
    est = data.elasticity
    if est is not None and est.usable:
        basis = f"measured from {est.n_obs} pool-weeks of pool history, the book's own depth as the instrument"
    elif est is not None:
        basis = f"assumed — not measured: {est.reason}"
    else:
        basis = "assumed — not yet measured (`defi-tracker elasticity --sync`)"
    a(
        '<p class="empty">EV = expected fees − expected IL vs holding, over the horizon, the book\'s '
        "capital in the pool as one band about spot. Pool fees ∝ depth^ε, ε = "
        f"{_cfg.volume_elasticity:.2g} ({_e(basis)}); ε {e_lo:.2g} / {e_hi:.2g} is the same band at the "
        f"bracket's ends (0 holds volume fixed, 1 makes it proportional to depth). Bracket: σ's "
        f"95% band. Depth outside {lo_b:g}–{hi_b:g}× today's is not scored, except a close, "
        "which is flagged.</p>"
    )
    for n in dict.fromkeys(notes):
        a(f'<p class="empty">{_e(n)}</p>')
    a("</div>")
    return "".join(out)


# The desk's tabs, in order: (anchor id, label). Panels are written in the
# page's own section order; the tab bar sets the reading order.
_TABS = (
    ("overview", "Brief"),
    ("actions", "Actions"),
    ("performance", "Performance"),
    ("risk", "Risk"),
    ("mm-book", "MM book"),
    ("rfq", "RFQ"),
    ("venues", "Venues"),
    ("mandate", "Mandate"),
    ("positions", "Positions"),
)


def render_dashboard(data: ReportData, title: str = "Liquidity Desk") -> str:
    """Render the whole book as one self-contained HTML document."""
    from defi_tracker.analytics import (
        health_warnings,
        pnl_decomposition,
        position_id,
        realized_closed_pnl,
        risk_summary,
        window_yields,
    )
    from defi_tracker.report import YIELD_WINDOW_DAYS, mtd_headline

    snaps = data.snaps
    total_value = sum(r["current_value_usd"] for r in snaps)
    # MTD is period PnL with deposits and withdrawals netted out, over the
    # positions it can establish — the coverage travels with the number.
    head = mtd_headline(data.periods)

    unc_rows = [r for r in snaps if unclaimed_known(r)]
    unc_unknown = len(snaps) - len(unc_rows)

    d = pnl_decomposition(snaps, data.tx_cost_rows)
    r = risk_summary(snaps)
    closed = realized_closed_pnl(data.flow_rows)
    book_net = float(d["net"]) + float(closed["realized_usd"])
    lp_vs_hodl = float(d["il"]) + float(d["fees_lifetime"]) + float(d["unclaimed"])

    vd = data.value_delta
    # A percentage off a zero baseline is undefined, so the whole line is
    # withheld rather than rendered as +0.00%.
    show_delta = vd is not None and vd.pct is not None

    out: list[str] = []
    a = out.append

    generated = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    a('<!doctype html><html lang="en"><head><meta charset="utf-8">')
    a('<meta name="viewport" content="width=device-width,initial-scale=1">')
    a(f"<title>{_e(title)}</title><style>{_CSS}</style></head><body>")
    a('<button id="theme">Dark</button><div class="wrap">')

    # ── header + hero ────────────────────────────────────────────────
    # The book is as of the latest snapshot, which is NOT necessarily today —
    # a failed or not-yet-run pass leaves yesterday's marks standing, and a
    # page that says "generated today" over stale marks is a lie of omission.
    as_of = max((r["snapshot_date"] for r in snaps), default=None)
    from defi_tracker.analytics import PROPOSAL_URL

    a(
        f'<header><h1>{_e(title)}</h1><span class="asof">marks {_e(as_of or "—")} · '
        f"built {_e(generated)}{' · ' + _link(PROPOSAL_URL, 'Proposal ↗') if PROPOSAL_URL else ''}"
        "</span></header>"
    )

    # ── degradation banners ──────────────────────────────────────────
    warnings = health_warnings(data.health_rows)
    if warnings:
        n_warn = len(warnings)
        a(f'<details class="status"><summary>Data status: {n_warn} source'
          f'{"s" if n_warn > 1 else ""} read directly on-chain while its indexer is offline; '
          "figures are unaffected</summary>")  # fmt: skip
        for w in warnings:
            a(f"<p>{_e(w.lstrip('⚠️ '))}</p>")
        a("</details>")

    # ── tab bar: one panel per area; without JavaScript every panel shows ──
    open_panel: list[str] = []

    def panel(tab_id: str) -> None:
        """Close the open panel (if any) and open the next one."""
        if open_panel:
            a("</section>")
        label = dict(_TABS)[tab_id]
        a(f'<section class="panel" id="{tab_id}"><h2 class="no-js-label">{_e(label)}</h2>')
        open_panel[:] = [tab_id]

    a('<nav class="tabs" aria-label="Desk sections">')
    a("".join(f'<a href="#{tid}">{_e(lab)}</a>' for tid, lab in _TABS))
    a("</nav>")
    panel("overview")
    a(_brief_section(data, total_value, head, book_net))

    # ── drivers | risk ───────────────────────────────────────────────
    a('<details class="more"><summary>Drivers and concentration</summary><div class="cols">')
    a(
        '<div class="card"><div class="lbl" style="color:var(--muted);font-size:11px;'
        'text-transform:uppercase;letter-spacing:.06em;margin-bottom:8px">PnL drivers (lifetime)</div>'
    )
    for lbl, v in [
        ("Market move (open)", float(d["market_move"])),
        ("Impermanent loss (open)", float(d["il"])),
        ("Fees collected (open)", float(d["fees_lifetime"])),
        ("Unclaimed (open)", float(d["unclaimed"])),
        (f"Realized, closed ({closed['positions']} pos)", float(closed["realized_usd"])),
    ]:
        a(
            f'<div class="kv"><span>{_e(lbl)}</span>'
            f'<span class="{_sign_class(v)}">{_e(_usd(v))}</span></div>'
        )
    # Gas is realized and already paid; it belongs above the line, not in a
    # footnote. Slippage sits beside it as an admitted unknown so the reader
    # knows the cost base is incomplete rather than assuming it is complete.
    costs = d["costs"]
    # No transaction history at all means there is nothing to say about costs;
    # an empty "Gas paid —" line is noise, not honesty.
    if costs and costs["txs"]:
        gas = d["gas"]
        a(
            f'<div class="kv"><span>Gas paid{_e(_coverage(costs))}</span>'
            f'<span class="{_sign_class(-float(gas)) if gas is not None else ""}">'
            f"{_e(_usd(-float(gas), dp=2) if gas is not None else '—')}</span></div>"
        )
        slip = d["slippage"]
        a(
            '<div class="kv"><span>Slippage <em>(not measured)</em></span>'
            f"<span>{_e(_usd(-float(slip), dp=2) if slip is not None else '—')}</span></div>"
        )
    a(
        f'<div class="kv total"><span>Book net</span>'
        f'<span class="{_sign_class(book_net)}">{_e(_usd(book_net))}</span></div>'
    )
    if d["net_after_costs"] is not None:
        after = float(d["net_after_costs"]) + float(closed["realized_usd"])
        a(
            f'<div class="kv total"><span>Book net after costs</span>'
            f'<span class="{_sign_class(after)}">{_e(_usd(after))}</span></div>'
        )
    a(
        f'<div class="kv"><span>LP vs holding <em>(open only)</em></span>'
        f'<span class="{_sign_class(lp_vs_hodl)}">{_e(_usd(lp_vs_hodl))}</span></div>'
    )
    a("</div>")

    a(
        '<div class="card"><div class="lbl" style="color:var(--muted);font-size:11px;'
        'text-transform:uppercase;letter-spacing:.06em;margin-bottom:8px">Concentration</div>'
    )
    a(
        f'<div class="kv"><span>Top pair — {_e(r["top_pair"])}</span>'
        f"<span>{_e(_usd(float(r['top_pair_value'])))} · {_e(_pct(r['top_pair_pct'], 0))}</span></div>"
    )
    a(
        f'<div class="kv"><span>Largest position — {_e(r["largest_position_label"])}</span>'
        f"<span>{_e(_usd(float(r['largest_position_value'])))}</span></div>"
    )
    a(
        f'<div class="kv"><span>Out of range</span>'
        f"<span>{_e(_usd(float(r['out_of_range_value'])))} · "
        f"{_e(r['out_of_range_count'])} positions</span></div>"
    )
    a(
        f'<div class="kv"><span>Chains</span><span>'
        f"{_e(', '.join(sorted({s['chain'] for s in snaps})))}</span></div>"
    )
    a("</div></div></details>")

    panel("performance")
    a(_performance_section(data))

    # ── fees, under performance ──────────────────────────────────────
    table = data.fee_table
    if table is not None and table.rows:
        order = table.chains
        fee_shown = table.rows[-12:]
        months = [r.label for r in fee_shown]
        chart_cells = {(r.label, c): r.cells.get(c, 0) for r in fee_shown for c in order}
        a("<h2>Claimed fees by accrual month</h2>")
        a('<div class="card">')
        a(_fee_chart(months, order, chart_cells))

        def cell(frow, c) -> str:
            if c in frow.unknown:
                return '<span title="event feed not synced for this month">?</span>'
            v = float(frow.cells.get(c, 0))
            if not v:
                return "—"
            return _e(_usd(v)) + ("+" if c in frow.partial else "")

        def claimed(frow) -> str:
            if frow.status == "unswept":
                return "unswept"
            if frow.status == "rolled":
                return "in a later month"
            return _e(_usd(float(frow.claimed))) + ("+" if frow.partial or frow.unknown else "")

        def mtm(frow) -> str:
            return _e(_usd(float(frow.mtm))) if frow.mtm is not None else "—"

        a("<table><thead><tr><th>Month</th>")
        for c in order:
            a(f"<th>{_e(_CHAIN_LABEL.get(c, c))}</th>")
        a("<th>Claimed</th><th>Marked to market</th></tr></thead><tbody>")
        for frow in fee_shown:
            mark = {"split": " §"}.get(frow.status, "")
            a(f"<tr><td>{_e(frow.label)}{mark}</td>")
            a("".join(f"<td>{cell(frow, c)}</td>" for c in order))
            a(f"<td>{claimed(frow)}</td><td>{mtm(frow)}</td></tr>")
        if table.accruing:
            acc = table.accruing
            a('<tr class="sub"><td>accruing *</td>')
            a("".join(f"<td>{cell(acc, c)}</td>" for c in order))
            a(f"<td>{claimed(acc)}</td><td>{mtm(acc)}</td></tr>")
        a("</tbody><tfoot>")
        for ftot in (table.ytd, table.lifetime):
            a(f"<tr><td>{_e(ftot.label)}</td>")
            a("".join(f"<td>{cell(ftot, c)}</td>" for c in order))
            a(f"<td>{claimed(ftot)}</td><td>{mtm(ftot)}</td></tr>")
        a("</tfoot></table>")
        fee_notes = []
        if table.accruing:
            fee_notes.append(f"* {table.accruing.note}; not yet claimed.")
        fee_notes += [f"{r.label}: {r.note}." for r in fee_shown if r.status in ("split", "rolled")]
        fee_notes += [f"? {n}." for n in table.notes]
        for n in fee_notes:
            a(f'<p class="empty">{_e(n)}</p>')
        a("</div>")

    # ── action queue (Phase 5.2) and the optimiser behind it (5.1) ───
    yields = window_yields(data.window_stats, data.window_collects, window_days=YIELD_WINDOW_DAYS)
    panel("actions")
    a(_actions_section(data))
    a(_optimiser_section(data))
    panel("risk")
    a(_risk_section(data))
    panel("mm-book")
    a(_mm_book_section(data))
    panel("rfq")
    a(_rfq_section(data))
    panel("venues")
    a(_venues_section(data))
    panel("mandate")
    a(_mandate_section(data))

    # ── earning ──────────────────────────────────────────────────────
    panel("positions")
    earning = sorted(
        (row for row in snaps if row.get("in_range")),
        key=lambda row: row["current_value_usd"],
        reverse=True,
    )
    if earning:
        in_range_val = sum(row["current_value_usd"] for row in earning)
        a(f"<h2>Earning — {_e(_usd(in_range_val))} in range</h2>")
        a(
            '<div class="card"><table><thead><tr><th>Pair</th><th>Venue</th><th>Value</th>'
            f"<th>Range</th><th>{YIELD_WINDOW_DAYS}d yield</th></tr></thead><tbody>"
        )
        for row in earning:
            y = yields.get(position_id(row))
            yld = _pct(y["apy_pct"], 0) if y and y["apy_pct"] is not None else "—"
            a(
                f'<tr><td class="pair">{_e(row["pair_label"])}</td>'
                f"<td>{_e(_CHAIN_LABEL.get(row['chain'], row['chain']))}</td>"
                f"<td>{_e(_usd(row['current_value_usd']))}</td>"
                f"<td>{_e(_range_pct(row))}</td><td>{_e(yld)}</td></tr>"
            )
        a("</tbody></table></div>")

    # ── all positions ────────────────────────────────────────────────
    a("<h2>All positions</h2>")
    a(
        '<div class="card"><table><thead><tr><th>Pair</th><th>Venue</th><th>Value</th>'
        "<th>Unclaimed</th><th>IL</th><th>PnL</th><th>Status</th></tr></thead><tbody>"
    )
    for row in sorted(snaps, key=lambda x: (x["chain"], -x["current_value_usd"])):
        unc = _usd(row["unclaimed_usd"]) if unclaimed_known(row) else "—"
        il, pnl = row["il_usd"], row["pnl_usd"]
        in_r = row.get("in_range")
        chip = (
            '<span class="chip in">● in range</span>'
            if in_r
            else (
                '<span class="chip out">● out</span>'
                if in_r == 0
                else '<span class="chip">—</span>'
            )
        )
        a(
            f'<tr><td class="pair">{_e(row["pair_label"])}</td>'
            f"<td>{_e(_CHAIN_LABEL.get(row['chain'], row['chain']))}</td>"
            f"<td>{_e(_usd(row['current_value_usd']))}</td><td>{_e(unc)}</td>"
            f'<td class="{_sign_class(il)}">{_e(_usd(il))}</td>'
            f'<td class="{_sign_class(pnl)}">{_e(_usd(pnl))}</td><td>{chip}</td></tr>'
        )
    a("</tbody></table></div>")

    a("</section>")

    # ── footnotes ────────────────────────────────────────────────────
    notes: list[str] = []
    if closed["unverified_positions"]:
        notes.append(
            f"{closed['unverified_positions']} closed position(s) with no withdrawal on record "
            f"({_usd(float(closed['unverified_usd']))} deposited) are excluded from realized "
            "PnL — missing data, not losses."
        )
    unknown = sum(1 for row in snaps if row["il_usd"] is None and row["pnl_usd"] is None)
    if unknown:
        notes.append(f"IL and PnL exclude {unknown} position(s) with no event history.")
    if unc_unknown:
        notes.append(
            f"{unc_unknown} position(s) have an unreadable unclaimed balance — shown as —, "
            "never as zero."
        )
    if as_of and as_of < datetime.now(UTC).strftime("%Y-%m-%d"):
        notes.append(f"No snapshot pass has completed today — every figure here is as of {as_of}.")
    if show_delta and vd is not None:
        notes.append(
            f"The change is measured per position from the {vd.prev_date} marks to the "
            f"{vd.curr_date} marks — {vd.span_days} day(s), not necessarily one."
        )
        if vd.excluded_positions:
            notes.append(
                f"It excludes {vd.excluded_positions} position(s) worth "
                f"{_usd(vd.excluded_value)} whose own prior mark predates {vd.prev_date}: "
                "their move spans a longer window and would not belong in this one."
            )
    if costs and not costs["gas_is_complete"]:
        a_known, a_total = costs["gas_known_txs"], costs["txs"]
        notes.append(
            f"Gas is priced for {a_known} of {a_total} transaction(s), so it is a floor, not a "
            "total, and book net after costs is withheld until the cost base is complete. "
            "Run `defi-tracker backfill-gas` to fill the rest."
        )
    notes.append(
        "Slippage is shown as unknown rather than zero: a mint or a burn has none, and the swap "
        "leg of a zap is not in the event history, so there is nothing yet to measure it against."
    )
    notes.append(
        f"Yield is realized: fees earned over the trailing {YIELD_WINDOW_DAYS} days divided by "
        "average value, annualized. Marked-to-market values fees at today's prices rather than "
        "at the price when they were claimed."
    )
    a('<details class="more foot"><summary>Notes on the numbers</summary>')
    for n in notes:
        a(f"<p>{_e(n)}</p>")
    a("</details></div>")
    a(f"<script>{_JS}</script></body></html>")
    return "\n".join(out)

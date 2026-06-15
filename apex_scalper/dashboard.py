"""Dashboard v2.1.1 — toate datele live din bot vizibile intr-un singur loc.

Changelog v2.1.1 (bugfix):
  - FIX: ob.spread / bid_depth / ask_depth citite INAUNTRUL lock-ului
    (era race condition: OrderBook mutat de feed thread dupa eliberarea lock-ului)
  - FIX: ask_pct = round(ask_depth/total*100, 1) in loc de 100-bid_pct
    (round(100 - round(x)) putea da 99.9 sau 100.1 la valori .5)
  - FIX: dcc.Graph(id='g-fib') scos din pos-card dinamic intr-un Output separat
    (id fix in children dinamici genereaza duplicate-ID warnings in Dash)

Changelog v2.1.0:
  - Regime chip: TRENDING/RANGING/VOLATILE/NEUTRAL + ADX live
  - Feed latency dot (green/orange/red) din last_tick_ts
  - Spread live in header
  - OB depth bar + imbalance
  - Trailing stop line pe Fibonacci chart
  - Realized PnL metrica
  - Refresh 1.5s

Changelog v2.0.0:
  - Fibonacci chart orizontal pe scala pretului real
  - PnL bar+cumulat dual-axis
  - Streak badge, session stats, CSS polish
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any

from loguru import logger

# ── Fibonacci niveluri ─────────────────────────────────────────────────────
FIB_LEVELS = [
    (0.000, "#444c56", "Entry",  3),
    (0.236, "#d29922", "23.6%", 1.5),
    (0.382, "#cd853f", "38.2%", 1.5),
    (0.500, "#4682b4", "50.0%", 1.5),
    (0.618, "#2e8b57", "61.8%", 2),
    (0.786, "#9370db", "78.6%", 1.5),
    (1.000, "#3fb950", "TP3",   3),
]

# ── Paleta ──────────────────────────────────────────────────────────────────
_BG     = "#0d1117"
_BG2    = "#161b22"
_BG3    = "#21262d"
_BG4    = "#2d333b"
_GREEN  = "#3fb950"
_RED    = "#f85149"
_GOLD   = "#d29922"
_BLUE   = "#58a6ff"
_PURPLE = "#bc8cff"
_ORANGE = "#e3b341"
_TEXT   = "#e6edf3"
_SUB    = "#8b949e"
_BORDER = "#30363d"

_FONT = "'JetBrains Mono', 'Fira Code', 'Cascadia Code', monospace"

_REGIME_COLORS = {
    "TRENDING":  (_GREEN,  "#3fb95022"),
    "VOLATILE":  (_ORANGE, "#e3b34122"),
    "RANGING":   (_RED,    "#f8514922"),
    "NEUTRAL":   (_BLUE,   "#58a6ff22"),
    "UNKNOWN":   (_SUB,    "#8b949e22"),
}

# ── CSS global ──────────────────────────────────────────────────────────────
_CSS = f"""
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700&display=swap');

*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

body {{
    background: {_BG};
    color: {_TEXT};
    font-family: {_FONT};
    font-size: 13px;
    line-height: 1.5;
}}

::-webkit-scrollbar {{ width: 5px; height: 5px; }}
::-webkit-scrollbar-track {{ background: {_BG2}; }}
::-webkit-scrollbar-thumb {{ background: {_BG4}; border-radius: 3px; }}

.card {{
    background: {_BG2};
    border: 1px solid {_BORDER};
    border-radius: 10px;
    padding: 16px 20px;
    margin-bottom: 14px;
}}
.card-active {{
    background: {_BG2};
    border: 1px solid {_GOLD}55;
    border-radius: 10px;
    padding: 16px 20px;
    margin-bottom: 14px;
    box-shadow: 0 0 22px {_GOLD}14;
}}

.m-val {{ font-size: 22px; font-weight: 700; line-height: 1.1; }}
.m-lbl {{ font-size: 10px; color: {_SUB}; text-transform: uppercase;
          letter-spacing: 1px; margin-top: 3px; }}

.sec-lbl {{
    font-size: 10px; color: {_SUB}; text-transform: uppercase;
    letter-spacing: 1.5px; margin-bottom: 10px;
    display: flex; align-items: center; gap: 6px;
}}
.sec-lbl::after {{
    content: ''; flex: 1; height: 1px; background: {_BORDER};
}}

.badge-win  {{ display:inline-block; padding:2px 9px; border-radius:12px;
               background:{_GREEN}22; border:1px solid {_GREEN}55;
               color:{_GREEN}; font-size:11px; font-weight:700; }}
.badge-loss {{ display:inline-block; padding:2px 9px; border-radius:12px;
               background:{_RED}22; border:1px solid {_RED}55;
               color:{_RED}; font-size:11px; font-weight:700; }}
.badge-neu  {{ display:inline-block; padding:2px 9px; border-radius:12px;
               background:{_BG4}; border:1px solid {_BORDER};
               color:{_SUB}; font-size:11px; }}

.regime-chip {{
    display: inline-block; padding: 2px 10px; border-radius: 12px;
    font-size: 11px; font-weight: 700; letter-spacing: 0.5px;
}}

.trade-row {{ border-bottom: 1px solid {_BG3}; transition: background .12s; }}
.trade-row:hover {{ background: {_BG3} !important; cursor: default; }}
.trade-td {{ padding: 5px 8px; font-size: 11px; white-space: nowrap; }}

.stat-box {{
    background: {_BG3}; border-radius: 6px; padding: 8px 12px;
    flex: 1; min-width: 0;
}}
.stat-val {{ font-size: 14px; font-weight: 700; }}
.stat-lbl {{ font-size: 10px; color: {_SUB}; margin-top: 2px; }}

/* OB depth bar */
.ob-wrap {{
    height: 6px; border-radius: 3px; background: {_BG3};
    overflow: hidden; margin-top: 4px;
}}
.ob-bid {{ height: 100%; float: left; background: {_GREEN}; border-radius: 3px 0 0 3px; }}
.ob-ask {{ height: 100%; float: right; background: {_RED}; border-radius: 0 3px 3px 0; }}

/* latency dot */
.dot-ok   {{ width:7px; height:7px; border-radius:50%; display:inline-block;
             background:{_GREEN};  margin-right:5px; }}
.dot-lag  {{ width:7px; height:7px; border-radius:50%; display:inline-block;
             background:{_ORANGE}; margin-right:5px; }}
.dot-dead {{ width:7px; height:7px; border-radius:50%; display:inline-block;
             background:{_RED};    margin-right:5px; }}
"""


# ── Helpers ─────────────────────────────────────────────────────────────────
def _card(children, active: bool = False, style: dict | None = None) -> Any:
    from dash import html
    return html.Div(children,
                    className="card-active" if active else "card",
                    style=style or None)


def _sec(label: str) -> Any:
    from dash import html
    return html.Div(label, className="sec-lbl")


def _metric(val: str, lbl: str, color: str = _TEXT) -> Any:
    from dash import html
    return html.Div([
        html.Div(val, className="m-val", style={"color": color}),
        html.Div(lbl, className="m-lbl"),
    ])


def _stat(val: str, lbl: str, color: str = _TEXT) -> Any:
    from dash import html
    return html.Div([
        html.Div(val, className="stat-val", style={"color": color}),
        html.Div(lbl, className="stat-lbl"),
    ], className="stat-box")


def _empty_fig(msg: str = "Fără date") -> Any:
    import plotly.graph_objects as go
    fig = go.Figure()
    fig.add_annotation(x=0.5, y=0.5, xref="paper", yref="paper",
                       text=msg, showarrow=False,
                       font={"size": 12, "color": _SUB, "family": _FONT})
    fig.update_layout(paper_bgcolor=_BG2, plot_bgcolor=_BG2,
                      margin={"t": 0, "b": 0, "l": 0, "r": 0},
                      xaxis={"visible": False}, yaxis={"visible": False})
    return fig


def _base_layout(**extra) -> dict:
    d = dict(
        paper_bgcolor=_BG2, plot_bgcolor=_BG2,
        font={"family": _FONT, "color": _TEXT},
        xaxis=dict(color=_SUB, gridcolor=_BG3, linecolor=_BORDER, zeroline=False),
        yaxis=dict(color=_SUB, gridcolor=_BG3, linecolor=_BORDER, zeroline=False),
        hovermode="x unified",
        hoverlabel=dict(bgcolor=_BG3, bordercolor=_BORDER,
                        font={"family": _FONT, "size": 11}),
        margin={"t": 8, "b": 36, "l": 54, "r": 12},
    )
    d.update(extra)
    return d


# ── Fibonacci chart ───────────────────────────────────────────────────────────
def _build_fib_fig(is_long, entry_p, price, sl_p, tp1_p, tp3_p,
                   open_qty, pnl_live, trailing_stop: float = 0.0):
    import plotly.graph_objects as go

    total_range = abs(tp3_p - entry_p)
    progress = max(0.0, min(1.0,
        ((price - entry_p) if is_long else (entry_p - price)) / total_range
    )) if total_range > 0 else 0.0

    y_min = min(sl_p, tp3_p) * 0.9997
    y_max = max(sl_p, tp3_p) * 1.0003
    bar_color = _GREEN if pnl_live >= 0 else _RED

    fig = go.Figure()

    fig.add_hrect(y0=min(sl_p, entry_p), y1=max(sl_p, entry_p),
                  fillcolor=f"{_RED}15", line_width=0)
    fig.add_hrect(y0=min(entry_p, tp3_p), y1=max(entry_p, tp3_p),
                  fillcolor=f"{_GREEN}0b", line_width=0)

    for ratio, color, label, width in FIB_LEVELS:
        y = entry_p + (1 if is_long else -1) * ratio * total_range
        fig.add_hline(
            y=y, line_color=color, line_width=width,
            line_dash="solid" if ratio in (0.0, 1.0) else "dot",
            annotation_text=f"  {label} {y:.5f}",
            annotation_position="right",
            annotation_font_size=9, annotation_font_color=color,
        )

    if trailing_stop and trailing_stop > 0:
        fig.add_hline(
            y=trailing_stop,
            line_color=_PURPLE, line_width=1.8, line_dash="dashdot",
            annotation_text=f"  TS {trailing_stop:.5f}",
            annotation_position="right",
            annotation_font_size=9, annotation_font_color=_PURPLE,
        )

    fig.add_hline(
        y=tp1_p, line_color=_BLUE, line_width=1.5, line_dash="dashdot",
        annotation_text=f"  TP1 {tp1_p:.5f}",
        annotation_position="right",
        annotation_font_size=9, annotation_font_color=_BLUE,
    )
    fig.add_hline(
        y=price, line_color=_GOLD, line_width=2.2, line_dash="dash",
        annotation_text=f"  ● {price:.5f}",
        annotation_position="right",
        annotation_font_size=10, annotation_font_color=_GOLD,
    )
    fig.add_annotation(
        x=0.015, y=price, xref="paper", yref="y",
        text=f"{progress * 100:.0f}%",
        showarrow=False,
        font={"size": 17, "color": bar_color, "family": _FONT},
        xanchor="left",
    )

    fig.update_layout(**_base_layout(
        margin={"t": 6, "b": 6, "l": 6, "r": 130},
        xaxis={"visible": False},
        yaxis={"range": [y_min, y_max], "color": _SUB,
               "gridcolor": _BG3, "tickformat": ".5f"},
        height=210,
        showlegend=False,
    ))
    return fig, progress


# ── PnL chart ───────────────────────────────────────────────────────────────
def _build_pnl_fig(closed: list) -> Any:
    import plotly.graph_objects as go

    if not closed:
        return _empty_fig("Niciun trade închis încă...")

    pnls = [t["pnl_usdt"] for t in reversed(closed)]
    xs   = list(range(1, len(pnls) + 1))
    cum, s = [], 0.0
    for p in pnls:
        s += p
        cum.append(round(s, 6))

    bar_colors = [_GREEN if p >= 0 else _RED for p in pnls]
    line_color = _GREEN if cum[-1] >= 0 else _RED

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=xs, y=pnls,
        marker_color=bar_colors, marker_line_width=0,
        opacity=0.55, name="Trade PnL",
        hovertemplate="Trade #%{x}: %{y:+.4f} USDT<extra></extra>",
        yaxis="y2",
    ))
    fig.add_trace(go.Scatter(
        x=xs, y=cum,
        mode="lines",
        line={"color": line_color, "width": 2.5, "shape": "spline"},
        fill="tozeroy",
        fillcolor=f"{'rgba(63,185,80' if cum[-1] >= 0 else 'rgba(248,81,73'},0.06)",
        name="Cumulat",
        hovertemplate="Cumulat: %{y:+.4f} USDT<extra></extra>",
        yaxis="y",
    ))
    fig.add_hline(y=0, line_color=_BORDER, line_width=1, yref="y")
    fig.update_layout(**_base_layout(
        barmode="overlay",
        yaxis=dict(color=_SUB, gridcolor=_BG3, zeroline=False, title="Cumulat USDT"),
        yaxis2=dict(overlaying="y", side="right", color=_SUB,
                    gridcolor="rgba(0,0,0,0)", zeroline=False, title="Per trade"),
        legend=dict(orientation="h", x=0, y=1.08,
                    font={"size": 10, "color": _SUB}, bgcolor="rgba(0,0,0,0)"),
        height=220,
        margin={"t": 24, "b": 36, "l": 54, "r": 54},
    ))
    return fig


# ── Win/Loss donut ──────────────────────────────────────────────────────────
def _build_wl_fig(wins: int, losses: int) -> Any:
    import plotly.graph_objects as go

    if wins + losses == 0:
        return _empty_fig("Fără date")

    wr = wins / (wins + losses) * 100
    fig = go.Figure(go.Pie(
        labels=["Win", "Loss"], values=[wins, losses],
        hole=0.65,
        marker=dict(colors=[_GREEN, _RED], line=dict(color=_BG2, width=3)),
        textinfo="percent+value",
        textfont=dict(size=11, family=_FONT),
        direction="clockwise", sort=False,
        hovertemplate="%{label}: %{value} (%{percent})<extra></extra>",
    ))
    fig.add_annotation(x=0.5, y=0.52, text=f"<b>{wr:.0f}%</b>",
                       showarrow=False,
                       font={"size": 20,
                             "color": _GREEN if wr >= 50 else _RED,
                             "family": _FONT})
    fig.add_annotation(x=0.5, y=0.35, text="win rate",
                       showarrow=False,
                       font={"size": 10, "color": _SUB, "family": _FONT})
    fig.update_layout(
        paper_bgcolor=_BG2, margin={"t": 0, "b": 0, "l": 0, "r": 0},
        showlegend=True,
        legend=dict(font={"color": _SUB, "size": 11, "family": _FONT},
                    bgcolor="rgba(0,0,0,0)",
                    orientation="h", x=0.15, y=-0.04),
        height=185,
    )
    return fig


# ── OB depth bar ─────────────────────────────────────────────────────────────
def _build_ob_bar(bid_depth: float, ask_depth: float) -> Any:
    from dash import html
    total = bid_depth + ask_depth
    if total == 0:
        bid_pct = ask_pct = 50.0
    else:
        # FIX: calculeaza ambele din sursa, nu 100-bid (evita rounding drift)
        bid_pct = round(bid_depth / total * 100, 1)
        ask_pct = round(ask_depth / total * 100, 1)

    imb_color = _GREEN if bid_pct >= 55 else (_RED if bid_pct <= 45 else _SUB)
    imb_label = ("BID heavy" if bid_pct >= 55
                 else ("ASK heavy" if bid_pct <= 45 else "Balanced"))

    return html.Div([
        html.Div([
            html.Span("OB  ",             style={"color": _SUB,   "fontSize": "10px"}),
            html.Span(f"Bid {bid_pct:.0f}%", style={"color": _GREEN, "fontSize": "10px",
                                                     "marginRight": "6px"}),
            html.Span(f"Ask {ask_pct:.0f}%", style={"color": _RED,   "fontSize": "10px",
                                                     "marginRight": "8px"}),
            html.Span(f"▸ {imb_label}",  style={"color": imb_color, "fontSize": "10px",
                                                   "fontWeight": "700"}),
        ], style={"marginBottom": "3px"}),
        html.Div(className="ob-wrap", children=[
            html.Div(className="ob-bid", style={"width": f"{bid_pct}%"}),
            html.Div(className="ob-ask", style={"width": f"{ask_pct}%"}),
        ]),
    ], style={"marginTop": "8px"})


# ── Aplicatia Dash ──────────────────────────────────────────────────────────
def create_app():
    import dash
    from dash import dcc, html
    from dash.dependencies import Input, Output

    app = dash.Dash(
        __name__, title="⚡ Apex Scalper",
        update_title=None, suppress_callback_exceptions=True,
    )
    app.index_string = app.index_string.replace(
        "<head>", f"<head><style>{_CSS}</style>"
    )

    # ── Layout ──────────────────────────────────────────────────────────────
    app.layout = html.Div([
        dcc.Interval(id="iv", interval=1500, n_intervals=0),

        # Header
        html.Div([
            html.Div([
                html.Span("⚡", style={"color": _GOLD, "marginRight": "8px"}),
                html.Span("APEX SCALPER", style={
                    "color": _TEXT, "fontWeight": "700",
                    "fontSize": "16px", "letterSpacing": "3px",
                }),
                html.Span(id="regime-chip", style={"marginLeft": "14px"}),
            ], style={"display": "flex", "alignItems": "center"}),
            html.Div(id="hdr", style={"fontSize": "11px", "color": _SUB,
                                      "display": "flex", "gap": "18px",
                                      "alignItems": "center"}),
        ], style={
            "display": "flex", "justifyContent": "space-between",
            "alignItems": "center", "padding": "11px 24px",
            "borderBottom": f"1px solid {_BORDER}",
            "background": _BG2, "marginBottom": "14px",
        }),

        # Body
        html.Div([

            # Coloana stanga (2/3)
            html.Div([

                # Metrici top
                _card(html.Div([
                    html.Div(id="m-price",  style={"flex": "1"}),
                    html.Div(id="m-rpnl",   style={"flex": "1"}),
                    html.Div(id="m-pnl",    style={"flex": "1"}),
                    html.Div(id="m-trades", style={"flex": "1"}),
                    html.Div(id="m-wr",     style={"flex": "1"}),
                    html.Div(id="m-dd",     style={"flex": "1"}),
                    html.Div(id="m-streak", style={"flex": "1", "display": "flex",
                                                   "alignItems": "flex-start",
                                                   "flexDirection": "column"}),
                ], style={"display": "flex", "gap": "16px", "flexWrap": "wrap"})),

                # Pozitie activa (info + OB bar) ─ fara Graph inauntru
                html.Div(id="pos-card"),

                # FIX: g-fib e acum Output static separat, nu in pos-card dinamic
                # Cand nu e pozitie, figura e _empty_fig()
                _card([
                    dcc.Graph(id="g-fib", config={"displayModeBar": False},
                              style={"height": "210px"}),
                ], style={"padding": "8px 12px", "marginBottom": "14px"}),

                # PnL chart
                _card([
                    _sec("PNL CUMULAT + PER TRADE"),
                    dcc.Graph(id="g-pnl", config={"displayModeBar": False},
                              style={"height": "230px"}),
                ]),

            ], style={"flex": "2", "minWidth": "0"}),

            # Coloana dreapta (1/3)
            html.Div([

                _card([
                    _sec("SESIUNE"),
                    html.Div(id="sess",
                             style={"display": "flex", "gap": "8px",
                                    "flexWrap": "wrap"}),
                ]),

                _card([
                    _sec("WIN / LOSS"),
                    dcc.Graph(id="g-wl", config={"displayModeBar": False},
                              style={"height": "195px"}),
                ]),

                _card([
                    _sec("TRADE-URI RECENTE"),
                    html.Div(id="tbl",
                             style={"overflowY": "auto", "maxHeight": "300px"}),
                ], style={"flex": "1"}),

            ], style={"flex": "1", "minWidth": "280px",
                      "display": "flex", "flexDirection": "column"}),

        ], style={"display": "flex", "gap": "14px", "padding": "0 20px 20px"}),

    ], style={"minHeight": "100vh", "background": _BG})

    # ── Callback ────────────────────────────────────────────────────────────
    @app.callback(
        [
            Output("hdr",         "children"),
            Output("regime-chip", "children"),
            Output("m-price",     "children"),
            Output("m-rpnl",      "children"),
            Output("m-pnl",       "children"),
            Output("m-trades",    "children"),
            Output("m-wr",        "children"),
            Output("m-dd",        "children"),
            Output("m-streak",    "children"),
            Output("pos-card",    "children"),
            Output("g-fib",       "figure"),   # FIX: Output static
            Output("g-pnl",       "figure"),
            Output("g-wl",        "figure"),
            Output("sess",        "children"),
            Output("tbl",         "children"),
        ],
        Input("iv", "n_intervals"),
    )
    def refresh(_):
        from dash import html
        from .state import state
        from .persistence import db
        from .config import config
        from .risk import risk
        from .regime_filter import regime

        sym = config.symbol
        now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

        # FIX: toate datele din OrderBook citite INAUNTRUL lock-ului
        with state.lock:
            pos          = state.open_position
            open_qty     = state.open_qty
            entry_p      = state.open_entry
            price        = state.last_price
            running      = state.running
            paused       = state.paused
            trailing_stp = state.trailing_stop
            realized_pnl = state.realized_pnl
            last_tick_ts = state.last_tick_ts
            # Citim valorile OB aici, nu referinta la obiect
            spread    = state.orderbook.spread    or 0.0
            bid_depth = state.orderbook.bid_depth(10)
            ask_depth = state.orderbook.ask_depth(10)

        # Feed latency
        tick_age_ms = int((time.time() - last_tick_ts) * 1000) if last_tick_ts else 9999
        if tick_age_ms < 500:
            dot_cls, lat_color, lat_label = "dot-ok",   _GREEN,  f"{tick_age_ms}ms"
        elif tick_age_ms < 2000:
            dot_cls, lat_color, lat_label = "dot-lag",  _ORANGE, f"{tick_age_ms}ms"
        else:
            dot_cls, lat_color, lat_label = "dot-dead", _RED,    "STALE"

        status_c = _GREEN if (running and not paused) else _RED
        status_t = ("● ACTIV" if (running and not paused)
                    else ("⏸ PAUZAT" if paused else "● OPRIT"))

        hdr = [
            html.Span(status_t, style={"color": status_c, "fontWeight": "700"}),
            html.Span(sym,      style={"color": _GOLD}),
            html.Span([
                html.Span(className=dot_cls),
                html.Span(lat_label, style={"color": lat_color}),
            ], style={"display": "flex", "alignItems": "center"}),
            html.Span(f"SPR {spread:.5f}", style={"color": _SUB}),
            html.Span(now,               style={"color": _SUB}),
        ]

        rlabel = regime.label
        rc, rbg = _REGIME_COLORS.get(rlabel, (_SUB, f"{_SUB}22"))
        regime_chip = html.Span(
            f"{rlabel}  ADX {regime.adx:.1f}",
            className="regime-chip",
            style={"color": rc, "background": rbg, "border": f"1px solid {rc}44"},
        )

        # DB
        daily_pnl, total_today, wins_today = db.load_daily_pnl(sym)
        all_trades   = db.get_all_trades(sym, limit=300)
        closed       = [t for t in all_trades if t["reason"] != "OPEN"]
        losses_today = total_today - wins_today
        wins_all     = sum(1 for t in closed if t["pnl_usdt"] > 0)
        losses_all   = len(closed) - wins_all
        wr           = wins_all / len(closed) * 100 if closed else 0

        # Drawdown
        max_dd = 0.0
        if closed:
            s, peak = 0.0, 0.0
            for t in reversed(closed):
                s += t["pnl_usdt"]
                if s > peak: peak = s
                dd = peak - s
                if dd > max_dd: max_dd = dd

        # Stats
        gross_win  = sum(t["pnl_usdt"] for t in closed if t["pnl_usdt"] > 0)
        gross_loss = sum(abs(t["pnl_usdt"]) for t in closed if t["pnl_usdt"] < 0)
        pf       = (gross_win / gross_loss if gross_loss > 0
                    else (99.0 if gross_win > 0 else 0.0))
        avg_win  = gross_win  / wins_all   if wins_all   > 0 else 0
        avg_loss = gross_loss / losses_all if losses_all > 0 else 0
        best     = max((t["pnl_usdt"] for t in closed), default=0.0)
        worst    = min((t["pnl_usdt"] for t in closed), default=0.0)
        cons     = risk.consecutive_losses

        pnl_c  = _GREEN if daily_pnl    >= 0 else _RED
        rpnl_c = _GREEN if realized_pnl >= 0 else _RED

        total_ob = bid_depth + ask_depth
        imb_pct  = round(bid_depth / total_ob * 100) if total_ob > 0 else 50

        m_price  = _metric(f"{price:.5f}" if price else "—",
                           f"{sym}  OB {imb_pct}% bid", _BLUE)
        m_rpnl   = _metric(f"{realized_pnl:+.4f}", "Realized USDT", rpnl_c)
        m_pnl    = _metric(f"{daily_pnl:+.4f}",    "PnL azi USDT",  pnl_c)
        m_trades = _metric(str(total_today), f"Trades  {wins_today}W/{losses_today}L")
        m_wr     = _metric(f"{wr:.0f}%", "Win rate", _GREEN if wr >= 50 else _RED)
        m_dd     = _metric(f"-{max_dd:.4f}", "Max Drawdown",
                           _RED if max_dd > 0 else _SUB)

        # Streak
        if cons >= 3:
            s_el = html.Span(f"⚠ {cons} LOSS", className="badge-loss")
        elif cons > 0:
            s_el = html.Span(f"{cons} loss",    className="badge-neu")
        else:
            ws = 0
            for t in closed:
                if t["pnl_usdt"] > 0: ws += 1
                else: break
            s_el = (html.Span(f"🔥 {ws} WIN", className="badge-win")
                    if ws >= 3 else html.Span("—", className="badge-neu"))
        m_streak = html.Div([
            html.Div("Streak", className="m-lbl", style={"marginBottom": "5px"}),
            s_el,
        ])

        # Pozitie activa (card fara Graph)
        if pos and entry_p and price:
            prof    = config.profile(sym)
            sl_pct  = prof.get("sl_pct",  0.0020)
            tp1_pct = prof.get("tp1_pct", 0.0030)
            tp3_pct = prof.get("tp3_pct", 0.0100)
            is_long = pos == "long"
            sign    = 1 if is_long else -1
            sl_p    = entry_p * (1 - sign * sl_pct)
            tp1_p   = entry_p * (1 + sign * tp1_pct)
            tp3_p   = entry_p * (1 + sign * tp3_pct)

            pnl_live = (price - entry_p) * sign * open_qty
            pnl_c2   = _GREEN if pnl_live >= 0 else _RED
            s_icon   = "▲ LONG" if is_long else "▼ SHORT"
            s_color  = _GREEN  if is_long else _RED

            fib_fig, progress = _build_fib_fig(
                is_long, entry_p, price, sl_p, tp1_p, tp3_p,
                open_qty, pnl_live, trailing_stop=trailing_stp,
            )

            pos_card = _card([
                _sec("POZIȚIE ACTIVĂ"),
                html.Div([
                    html.Span(s_icon, style={"color": s_color, "fontWeight": "700",
                                             "fontSize": "14px", "marginRight": "10px"}),
                    html.Span(f"{open_qty:.0f} {sym.replace('USDT','')} @ {entry_p:.5f}",
                              style={"color": _SUB, "fontSize": "12px"}),
                    html.Span("│ PnL: ", style={"color": _SUB, "fontSize": "11px",
                                                  "margin": "0 6px"}),
                    html.Span(f"{pnl_live:+.5f} USDT",
                              style={"color": pnl_c2, "fontWeight": "700",
                                     "fontSize": "14px"}),
                    html.Span(f"│ {progress*100:.0f}% → TP3",
                              style={"color": _GOLD, "fontSize": "11px",
                                     "marginLeft": "8px"}),
                ], style={"marginBottom": "8px", "display": "flex",
                          "alignItems": "center", "flexWrap": "wrap", "gap": "4px"}),
                html.Div([
                    html.Span(f"SL {sl_p:.5f}",
                              style={"color": _RED,   "fontSize": "11px", "marginRight": "12px"}),
                    html.Span(f"TP1 {tp1_p:.5f}",
                              style={"color": _BLUE,  "fontSize": "11px", "marginRight": "12px"}),
                    html.Span(f"TP3 {tp3_p:.5f}",
                              style={"color": _GREEN, "fontSize": "11px", "marginRight": "12px"}),
                    *([html.Span(f"TS {trailing_stp:.5f}",
                                 style={"color": _PURPLE, "fontSize": "11px"})]
                      if trailing_stp else []),
                ], style={"marginBottom": "6px"}),
                _build_ob_bar(bid_depth, ask_depth),
            ], active=True)
        else:
            fib_fig  = _empty_fig("Aștept semnal...")
            pos_card = _card([
                _sec("POZIȚIE ACTIVĂ"),
                html.Div([
                    html.Span("○ ", style={"color": _SUB}),
                    html.Span("Aștept semnal...",
                              style={"color": _SUB, "fontSize": "12px"}),
                ]),
                _build_ob_bar(bid_depth, ask_depth),
            ])

        pnl_fig = _build_pnl_fig(closed)
        wl_fig  = _build_wl_fig(wins_all, losses_all)

        pf_c = _GREEN if pf >= 1.5 else (_GOLD if pf >= 1.0 else _RED)
        sess = [
            _stat(f"{avg_win:+.4f}",  "Avg Win",      _GREEN),
            _stat(f"-{avg_loss:.4f}", "Avg Loss",      _RED),
            _stat(f"{pf:.2f}",        "Profit Factor", pf_c),
            _stat(f"{best:+.4f}",     "Best",          _GREEN),
            _stat(f"{worst:+.4f}",    "Worst",         _RED),
        ]

        last30 = db.get_last_trades(sym, limit=30)
        if last30:
            hd = html.Thead(html.Tr([
                html.Th(h, style={"color": _SUB, "fontSize": "10px",
                                  "textTransform": "uppercase", "padding": "4px 8px",
                                  "borderBottom": f"1px solid {_BORDER}",
                                  "textAlign": "left", "fontWeight": "400"})
                for h in ["Ora", "Side", "Entry", "Exit", "PnL", "Score", "Motiv"]
            ]))
            rows = []
            for t in last30:
                pnl = t["pnl_usdt"]
                sc  = t.get("signal_score", 0) or 0
                rows.append(html.Tr([
                    html.Td(t.get("closed_at", "")[-5:],
                            className="trade-td", style={"color": _SUB}),
                    html.Td("▲ L" if t["side"] == "long" else "▼ S",
                            className="trade-td",
                            style={"color": _GREEN if t["side"] == "long" else _RED,
                                   "fontWeight": "700"}),
                    html.Td(f"{t['entry']:.5f}",            className="trade-td"),
                    html.Td(f"{t.get('exit_price', 0):.5f}", className="trade-td",
                            style={"color": _SUB}),
                    html.Td(f"{pnl:+.4f}", className="trade-td",
                            style={"color": _GREEN if pnl > 0 else _RED,
                                   "fontWeight": "700"}),
                    html.Td(f"{sc:.2f}" if sc else "—", className="trade-td",
                            style={"color": _GOLD}),
                    html.Td(t.get("reason", "")[:14], className="trade-td",
                            style={"color": _SUB, "fontSize": "10px"}),
                ], className="trade-row"))
            tbl = html.Table([hd, html.Tbody(rows)],
                             style={"width": "100%", "borderCollapse": "collapse"})
        else:
            tbl = html.Div("Niciun trade închis încă",
                           style={"color": _SUB, "fontSize": "12px", "padding": "8px 0"})

        return (hdr, regime_chip,
                m_price, m_rpnl, m_pnl, m_trades, m_wr, m_dd, m_streak,
                pos_card, fib_fig, pnl_fig, wl_fig, sess, tbl)

    return app


# ── Entry point ─────────────────────────────────────────────────────────────
def run_dashboard(host: str = "0.0.0.0", port: int = 8050) -> None:
    try:
        import dash  # noqa: F401
    except ImportError:
        logger.warning("[Dashboard] 'dash' nu e instalat — skip. "
                       "Instaleaza cu: pip install dash")
        return

    app = create_app()

    def _run():
        logger.info(f"[Dashboard] http://localhost:{port}")
        import logging
        logging.getLogger("werkzeug").setLevel(logging.ERROR)
        app.run(host=host, port=port, debug=False, use_reloader=False)

    threading.Thread(target=_run, daemon=True, name="apex-dashboard").start()

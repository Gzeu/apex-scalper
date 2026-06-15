"""Dashboard v1.0.0 — Dash GUI pentru monitorizarea bot-ului.

Pornire: ruleaza automat din main.py pe portul 8050.
Acces:   http://localhost:8050

Features:
  - Live tick price + pozitie curenta
  - Trade progress bar (0-100%) cu niveluri Fibonacci
  - Grafic PnL cumulat (toate trade-urile)
  - Win/Loss donut chart
  - Tabel ultimele 30 trade-uri cu colorare win/loss
  - Refresh automat la fiecare 2 secunde
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any

from loguru import logger

# Fibonacci retracement levels (0% = entry, 100% = TP)
FIB_LEVELS = [
    (0.236, "#b8860b", "Fib 23.6%"),
    (0.382, "#cd853f", "Fib 38.2%"),
    (0.500, "#4682b4", "Fib 50.0%"),
    (0.618, "#2e8b57", "Fib 61.8%"),
    (0.786, "#9370db", "Fib 78.6%"),
]

_BG       = "#0d1117"
_BG2      = "#161b22"
_BG3      = "#21262d"
_GREEN    = "#3fb950"
_RED      = "#f85149"
_GOLD     = "#d29922"
_TEXT     = "#e6edf3"
_SUBTEXT  = "#8b949e"
_BORDER   = "#30363d"


def _css() -> str:
    return f"""
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ background: {_BG}; color: {_TEXT}; font-family: 'JetBrains Mono', 'Fira Code', monospace; }}
    .dash-table-container .dash-spreadsheet-container .dash-spreadsheet-inner td {{
        background: {_BG2} !important; color: {_TEXT} !important;
        border-color: {_BORDER} !important; font-size: 12px;
    }}
    .dash-table-container .dash-spreadsheet-container .dash-spreadsheet-inner th {{
        background: {_BG3} !important; color: {_SUBTEXT} !important;
        border-color: {_BORDER} !important; font-size: 11px; text-transform: uppercase;
    }}
    .card {{ background: {_BG2}; border: 1px solid {_BORDER}; border-radius: 8px; padding: 16px; }}
    .metric-val {{ font-size: 28px; font-weight: 700; }}
    .metric-lbl {{ font-size: 11px; color: {_SUBTEXT}; text-transform: uppercase; letter-spacing: 1px; margin-top: 4px; }}
    .win  {{ color: {_GREEN}; }}
    .loss {{ color: {_RED}; }}
    """


def _card(children, style: dict | None = None) -> Any:
    from dash import html
    s = {"marginBottom": "16px"}
    if style:
        s.update(style)
    return html.Div(children, className="card", style=s)


def _metric(value: str, label: str, color: str = _TEXT) -> Any:
    from dash import html
    return html.Div([
        html.Div(value, className="metric-val", style={"color": color}),
        html.Div(label, className="metric-lbl"),
    ])


def create_app():
    """Creeaza si returneaza aplicatia Dash."""
    import dash
    from dash import dcc, html, dash_table
    from dash.dependencies import Input, Output
    import plotly.graph_objects as go

    app = dash.Dash(
        __name__,
        title="Apex Scalper",
        update_title=None,
        suppress_callback_exceptions=True,
    )
    app.index_string = app.index_string.replace(
        "<head>",
        f"<head><style>{_css()}</style>"
    )

    # ------------------------------------------------------------------ #
    #  Layout                                                              #
    # ------------------------------------------------------------------ #

    app.layout = html.Div([
        dcc.Interval(id="interval", interval=2000, n_intervals=0),

        # Header
        html.Div([
            html.Div("⚡ APEX SCALPER", style={
                "fontSize": "20px", "fontWeight": "700",
                "color": _GOLD, "letterSpacing": "2px"
            }),
            html.Div(id="header-status", style={"fontSize": "12px", "color": _SUBTEXT}),
        ], style={
            "display": "flex", "justifyContent": "space-between",
            "alignItems": "center", "padding": "16px 24px",
            "borderBottom": f"1px solid {_BORDER}",
            "marginBottom": "16px",
        }),

        # Body
        html.Div([

            # Coloana stanga
            html.Div([

                # Metrici rapide
                _card(html.Div([
                    html.Div(id="metric-price",    style={"flex": "1"}),
                    html.Div(id="metric-pnl-day",  style={"flex": "1"}),
                    html.Div(id="metric-trades",   style={"flex": "1"}),
                    html.Div(id="metric-winrate",  style={"flex": "1"}),
                    html.Div(id="metric-drawdown", style={"flex": "1"}),
                ], style={"display": "flex", "gap": "24px"})),

                # Trade activ + Fibonacci
                _card([
                    html.Div("POZITIE ACTIVA", style={
                        "fontSize": "11px", "color": _SUBTEXT,
                        "letterSpacing": "1px", "marginBottom": "12px"
                    }),
                    html.Div(id="active-trade-info", style={"marginBottom": "16px"}),
                    dcc.Graph(
                        id="fib-chart",
                        config={"displayModeBar": False},
                        style={"height": "160px"}
                    ),
                ]),

                # PnL cumulat
                _card([
                    html.Div("PNL CUMULAT", style={
                        "fontSize": "11px", "color": _SUBTEXT,
                        "letterSpacing": "1px", "marginBottom": "8px"
                    }),
                    dcc.Graph(
                        id="pnl-chart",
                        config={"displayModeBar": False},
                        style={"height": "220px"}
                    ),
                ]),

            ], style={"flex": "2", "minWidth": "0"}),

            # Coloana dreapta
            html.Div([

                # Win/Loss donut
                _card([
                    html.Div("WIN / LOSS", style={
                        "fontSize": "11px", "color": _SUBTEXT,
                        "letterSpacing": "1px", "marginBottom": "8px"
                    }),
                    dcc.Graph(
                        id="winloss-chart",
                        config={"displayModeBar": False},
                        style={"height": "200px"}
                    ),
                ]),

                # Tabel trade-uri
                _card([
                    html.Div("ULTIMELE TRADE-URI", style={
                        "fontSize": "11px", "color": _SUBTEXT,
                        "letterSpacing": "1px", "marginBottom": "12px"
                    }),
                    html.Div(id="trades-table"),
                ], style={"flex": "1", "overflow": "auto"}),

            ], style={"flex": "1", "minWidth": "280px", "display": "flex", "flexDirection": "column"}),

        ], style={"display": "flex", "gap": "16px", "padding": "0 24px 24px"}),

    ], style={"minHeight": "100vh", "background": _BG})

    # ------------------------------------------------------------------ #
    #  Callbacks                                                           #
    # ------------------------------------------------------------------ #

    @app.callback(
        [
            Output("header-status",   "children"),
            Output("metric-price",    "children"),
            Output("metric-pnl-day",  "children"),
            Output("metric-trades",   "children"),
            Output("metric-winrate",  "children"),
            Output("metric-drawdown", "children"),
            Output("active-trade-info", "children"),
            Output("fib-chart",       "figure"),
            Output("pnl-chart",       "figure"),
            Output("winloss-chart",   "figure"),
            Output("trades-table",    "children"),
        ],
        Input("interval", "n_intervals"),
    )
    def refresh(_):
        from dash import html
        from .state import state
        from .persistence import db
        from .config import config
        from .risk import risk
        import plotly.graph_objects as go

        sym = config.symbol
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        # Stare bot
        with state.lock:
            pos      = state.open_position
            open_qty = state.open_qty
            entry_p  = state.open_entry
            price    = state.last_price if hasattr(state, "last_price") else 0.0
            running  = state.running
            paused   = state.paused

        status_color = _GREEN if (running and not paused) else _RED
        bot_status   = "● ACTIV" if (running and not paused) else ("⏸ PAUZAT" if paused else "● OPRIT")

        header_status = html.Span([
            html.Span(bot_status, style={"color": status_color, "marginRight": "16px"}),
            html.Span(f"{sym}  |  {now}", style={"color": _SUBTEXT}),
        ])

        # Date DB
        daily_pnl, total_today, wins_today = db.load_daily_pnl(sym)
        trades_all = db.get_all_trades(sym, limit=200)
        closed = [t for t in trades_all if t["reason"] != "OPEN"]

        losses_today = total_today - wins_today
        win_rate = (wins_today / total_today * 100) if total_today > 0 else 0
        cons_losses = risk.consecutive_losses

        # Max drawdown din trade-uri inchise
        if closed:
            pnls = [t["pnl_usdt"] for t in reversed(closed)]
            cumulative = []
            s = 0.0
            peak = 0.0
            max_dd = 0.0
            for p in pnls:
                s += p
                cumulative.append(s)
                if s > peak:
                    peak = s
                dd = peak - s
                if dd > max_dd:
                    max_dd = dd
        else:
            max_dd = 0.0
            cumulative = []

        # ---------- Metrici ----------
        pnl_color = _GREEN if daily_pnl >= 0 else _RED
        m_price   = _metric(f"{price:.5f}" if price else "—", f"{sym} Price")
        m_pnl     = _metric(f"{daily_pnl:+.4f} USDT", "PnL azi", pnl_color)
        m_trades  = _metric(str(total_today), "Trade-uri azi")
        m_winrate = _metric(f"{win_rate:.0f}%", f"Win rate  {wins_today}W / {losses_today}L",
                            _GREEN if win_rate >= 50 else _RED)
        m_dd      = _metric(f"-{max_dd:.4f}", "Max Drawdown", _RED if max_dd > 0 else _SUBTEXT)

        # ---------- Trade activ + Fibonacci ----------
        if pos and entry_p and price:
            prof = __import__('apex_scalper.config', fromlist=['config']).config.profile(sym)
            sl_pct  = prof.get("sl_pct",  0.0020)
            tp1_pct = prof.get("tp1_pct", 0.0030)
            tp3_pct = prof.get("tp3_pct", 0.0100)

            is_long = pos == "long"
            sl_price  = entry_p * (1 - sl_pct  if is_long else 1 + sl_pct)
            tp1_price = entry_p * (1 + tp1_pct if is_long else 1 - tp1_pct)
            tp3_price = entry_p * (1 + tp3_pct if is_long else 1 - tp3_pct)

            total_range = abs(tp3_price - entry_p)
            current_move = (price - entry_p) if is_long else (entry_p - price)
            progress = max(0.0, min(1.0, current_move / total_range)) if total_range > 0 else 0.0
            pnl_live = (price - entry_p) * open_qty if is_long else (entry_p - price) * open_qty
            pnl_color_live = _GREEN if pnl_live >= 0 else _RED

            side_label = "🟢 LONG" if is_long else "🔴 SHORT"
            active_info = html.Div([
                html.Span(f"{side_label} ", style={"fontWeight": "700", "fontSize": "15px"}),
                html.Span(f"{open_qty:.0f} {sym.replace('USDT','')} @ {entry_p:.5f}",
                          style={"color": _SUBTEXT, "fontSize": "13px"}),
                html.Span(f"  PnL: ", style={"color": _SUBTEXT, "fontSize": "12px", "marginLeft": "16px"}),
                html.Span(f"{pnl_live:+.4f} USDT",
                          style={"color": pnl_color_live, "fontWeight": "700", "fontSize": "13px"}),
                html.Span(f"  SL: {sl_price:.5f}  TP1: {tp1_price:.5f}  TP3: {tp3_price:.5f}",
                          style={"color": _SUBTEXT, "fontSize": "11px", "marginLeft": "16px"}),
            ])

            # Fibonacci chart
            fib_fig = go.Figure()

            # Background bar (total range)
            fib_fig.add_trace(go.Bar(
                x=[1.0], y=[0.12],
                base=[-0.06],
                orientation="v",
                marker_color=_BG3,
                width=0.8,
                showlegend=False,
                hoverinfo="skip",
            ))

            # Progress bar
            bar_color = _GREEN if pnl_live >= 0 else _RED
            fib_fig.add_trace(go.Bar(
                x=[1.0], y=[progress * 0.12],
                base=[-0.06],
                orientation="v",
                marker_color=bar_color,
                width=0.8,
                opacity=0.7,
                showlegend=False,
                hoverinfo="skip",
            ))

            # Fibonacci lines
            for fib_val, fib_color, fib_name in FIB_LEVELS:
                y_pos = -0.06 + fib_val * 0.12
                fib_fig.add_hline(
                    y=y_pos,
                    line_dash="dot",
                    line_color=fib_color,
                    line_width=1.2,
                    annotation_text=f"{fib_name}",
                    annotation_position="right",
                    annotation_font_size=10,
                    annotation_font_color=fib_color,
                )

            # Progress label
            fib_fig.add_annotation(
                x=1.0, y=-0.06 + progress * 0.12 + 0.003,
                text=f"{progress*100:.0f}%",
                showarrow=False,
                font={"size": 14, "color": bar_color, "family": "monospace"},
            )

            fib_fig.update_layout(
                paper_bgcolor=_BG2, plot_bgcolor=_BG2,
                margin={"t": 0, "b": 0, "l": 0, "r": 80},
                xaxis={"visible": False},
                yaxis={"visible": False, "range": [-0.08, 0.10]},
                barmode="overlay",
                height=140,
            )
        else:
            active_info = html.Div("Nicio pozitie deschisa",
                                   style={"color": _SUBTEXT, "fontSize": "13px", "padding": "8px 0"})
            fib_fig = go.Figure()
            fib_fig.add_annotation(
                x=0.5, y=0.5, xref="paper", yref="paper",
                text="Astept semnal...",
                showarrow=False,
                font={"size": 13, "color": _SUBTEXT},
            )
            fib_fig.update_layout(
                paper_bgcolor=_BG2, plot_bgcolor=_BG2,
                margin={"t": 0, "b": 0, "l": 0, "r": 0},
                xaxis={"visible": False}, yaxis={"visible": False},
                height=140,
            )

        # ---------- PnL Chart ----------
        pnl_fig = go.Figure()
        if cumulative:
            xs = list(range(1, len(cumulative) + 1))
            colors = [_GREEN if v >= 0 else _RED for v in cumulative]
            pnl_fig.add_trace(go.Scatter(
                x=xs, y=cumulative,
                mode="lines+markers",
                line={"color": _GREEN if cumulative[-1] >= 0 else _RED, "width": 2},
                marker={"color": colors, "size": 5},
                fill="tozeroy",
                fillcolor="rgba(63,185,80,0.08)" if cumulative[-1] >= 0 else "rgba(248,81,73,0.08)",
                hovertemplate="Trade #%{x}<br>PnL cumulat: %{y:.4f} USDT<extra></extra>",
            ))
            pnl_fig.add_hline(y=0, line_color=_BORDER, line_width=1)
        else:
            pnl_fig.add_annotation(
                x=0.5, y=0.5, xref="paper", yref="paper",
                text="Fara trade-uri inca...",
                showarrow=False, font={"size": 13, "color": _SUBTEXT},
            )
        pnl_fig.update_layout(
            paper_bgcolor=_BG2, plot_bgcolor=_BG2,
            margin={"t": 10, "b": 30, "l": 50, "r": 10},
            xaxis={"color": _SUBTEXT, "gridcolor": _BG3, "title": ""},
            yaxis={"color": _SUBTEXT, "gridcolor": _BG3, "title": "USDT"},
            height=200,
        )

        # ---------- Win/Loss Donut ----------
        wl_fig = go.Figure()
        total_all = len(closed)
        wins_all  = sum(1 for t in closed if t["pnl_usdt"] > 0)
        losses_all = total_all - wins_all
        if total_all > 0:
            wl_fig.add_trace(go.Pie(
                labels=["Win", "Loss"],
                values=[wins_all, losses_all],
                hole=0.6,
                marker_colors=[_GREEN, _RED],
                textinfo="percent",
                textfont_size=12,
                hovertemplate="%{label}: %{value} (%{percent})<extra></extra>",
            ))
            wl_fig.add_annotation(
                x=0.5, y=0.5,
                text=f"<b>{wins_all/total_all*100:.0f}%</b><br><span style='font-size:10px'>win rate</span>",
                showarrow=False,
                font={"size": 14, "color": _TEXT},
            )
        else:
            wl_fig.add_annotation(
                x=0.5, y=0.5, xref="paper", yref="paper",
                text="Fara date", showarrow=False,
                font={"size": 12, "color": _SUBTEXT},
            )
        wl_fig.update_layout(
            paper_bgcolor=_BG2, plot_bgcolor=_BG2,
            margin={"t": 0, "b": 0, "l": 0, "r": 0},
            showlegend=True,
            legend={"font": {"color": _SUBTEXT, "size": 11},
                    "bgcolor": "rgba(0,0,0,0)",
                    "orientation": "h", "x": 0.25, "y": -0.05},
            height=190,
        )

        # ---------- Tabel trade-uri ----------
        last30 = db.get_last_trades(sym, limit=30)
        if last30:
            rows = []
            for t in last30:
                pnl = t["pnl_usdt"]
                side_icon = "▲" if t["side"] == "long" else "▼"
                rows.append(html.Tr([
                    html.Td(t.get("closed_at", "")[-5:],
                            style={"color": _SUBTEXT, "fontSize": "11px", "padding": "4px 6px"}),
                    html.Td(f"{side_icon} {t['side'].upper()}",
                            style={"color": _GREEN if t["side"] == "long" else _RED,
                                   "fontSize": "11px", "padding": "4px 6px"}),
                    html.Td(f"{t['entry']:.5f}",
                            style={"fontSize": "11px", "padding": "4px 6px"}),
                    html.Td(f"{pnl:+.4f}",
                            style={"color": _GREEN if pnl > 0 else _RED,
                                   "fontWeight": "700", "fontSize": "12px", "padding": "4px 6px"}),
                    html.Td(t.get("reason", "")[:12],
                            style={"color": _SUBTEXT, "fontSize": "10px", "padding": "4px 6px"}),
                ], style={"borderBottom": f"1px solid {_BG3}"})
                )
            table = html.Table([
                html.Thead(html.Tr([
                    html.Th(h, style={"color": _SUBTEXT, "fontSize": "10px",
                                     "textTransform": "uppercase", "padding": "4px 6px",
                                     "borderBottom": f"1px solid {_BORDER}"})
                    for h in ["Ora", "Side", "Entry", "PnL", "Motiv"]
                ])),
                html.Tbody(rows),
            ], style={"width": "100%", "borderCollapse": "collapse"})
        else:
            table = html.Div("Niciun trade inchis inca",
                             style={"color": _SUBTEXT, "fontSize": "12px", "padding": "8px 0"})

        return (
            header_status,
            m_price, m_pnl, m_trades, m_winrate, m_dd,
            active_info, fib_fig, pnl_fig, wl_fig, table,
        )

    return app


def run_dashboard(host: str = "0.0.0.0", port: int = 8050) -> None:
    """Porneste Dash dashboard intr-un thread separat (non-blocking)."""
    try:
        import dash  # noqa: F401
    except ImportError:
        logger.warning("[Dashboard] dash nu e instalat — skip. Ruleaza: pip install dash")
        return

    app = create_app()

    def _run():
        logger.info(f"[Dashboard] pornit pe http://localhost:{port}")
        import logging
        log = logging.getLogger("werkzeug")
        log.setLevel(logging.ERROR)
        app.run(host=host, port=port, debug=False, use_reloader=False)

    t = threading.Thread(target=_run, daemon=True, name="dashboard")
    t.start()

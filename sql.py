"""
seniority_dashboard.py
======================
Government Employee Seniority Ranking Dashboard — CORRECTED ENGINE + MySQL.

WHAT CHANGED FROM THE PREVIOUS VERSION
---------------------------------------
1) DATA SOURCE: now reads from MySQL (schema `pac_erp_care`, tables
   temp_emp_data / temp_promotion / temp_reappointment) instead of CSV files.
   Every column in those tables is VARCHAR(100) — including dates and BPS
   numbers — so dates are parsed explicitly with format="%d-%b-%Y" and
   BPS/level columns are coerced to numeric, both with errors="coerce" so a
   single malformed cell turns into NaT/NaN (and gets filtered out later)
   instead of crashing the whole load.
2) BPS DEEP DIVE HARDENING (this version): update_bps() and build_accordion()
   are now each wrapped so that a problem with ONE BPS level (a lone
   employee with no peers, an ArfNo that doesn't match `report`, a missing
   event_date, a duplicate name collision, a heatmap that can't be built)
   never blanks the whole tab / crashes the whole page. Every failure is
   printed to the console with the exact BPS level and exception, and a
   friendly placeholder is shown for that level only. Concretely:
     a) event_date rows with NaT are dropped before any .dt.strftime()/
        groupby("event_date") call, since a NaT there is what crashes those
        calls, and null dates get more common the sparser/higher a BPS grade
        is.
     b) rows whose ArfNo doesn't have a full matching row in `report` (which
        can happen silently via a how="left" merge) are dropped, with a
        console warning naming the ArfNo, instead of letting NaN leak into
        rank/label/sort logic further down.
     c) the career heatmap is keyed by "Name (ArfNo)" instead of raw Namee,
        since two employees sharing a name were silently colliding in
        set_index("Namee")/groupby("Namee") — more likely to bite at
        senior/crowded grades — and is wrapped in its own try/except with a
        placeholder fallback chart.
     d) the whole update_bps callback body runs inside a try/except so ANY
        remaining edge case at ANY BPS level degrades gracefully instead of
        crashing the Dash callback (which is what makes a whole tab go
        blank).
     e) build_accordion() (the "All BPS Levels" tab) gets the same NaT/NaN
        guards, plus a per-level try/except so one bad grade renders as an
        "⚠ render error" accordion item instead of stopping app.layout from
        building at all.
3) TIE-BREAK ENGINE ITSELF IS UNCHANGED: same-grade recursive tiebreaker
   (peak-1, peak-2, ... down to 1), only comparing employees at a grade when
   every tied member actually has a recorded date there. Falls back to
   government entry date -> date of birth -> ArfNo.

HOW TO RUN:
    pip install dash dash-bootstrap-components plotly pandas sqlalchemy pymysql
    python seniority_dashboard.py
    (or paste into a Jupyter notebook in 3 cells: imports+engine / everything
    else / load-data+app.run(mode="external", port=8050))

MYSQL CONNECTION:
    host=127.0.0.1  port=3306  user=root  schema=pac_erp_care
    Update DB_PASSWORD below before running.
"""
import os, json
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import dash
from dash import dcc, html, dash_table, Input, Output, State
import dash_bootstrap_components as dbc
from sqlalchemy import create_engine
from sqlalchemy.engine import URL

# ─────────────────────────────────────────────────────────────────────────────
# DATABASE CONNECTION
# ─────────────────────────────────────────────────────────────────────────────
DB_USER     = os.environ.get("DB_USER", "root")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "Netflix@55")   # <-- set this
DB_HOST     = os.environ.get("DB_HOST", "127.0.0.1")
DB_PORT     = int(os.environ.get("DB_PORT", "3306"))
DB_SCHEMA   = os.environ.get("DB_SCHEMA", "pac_erp_care")

# Built via URL.create() rather than an f-string: if the password contains
# an "@", ":", "/", or "%", plugging it straight into "user:pass@host:port"
# breaks the URL parser (it misreads part of the password as the hostname --
# this is exactly what caused "Can't connect to MySQL server on '55@127.0.0.1'").
# URL.create() escapes special characters automatically, no manual work needed.
db_url = URL.create(
    drivername="mysql+pymysql",
    username=DB_USER,
    password=DB_PASSWORD,
    host=DB_HOST,
    port=DB_PORT,
    database=DB_SCHEMA,
)
engine = create_engine(db_url)

# ─────────────────────────────────────────────────────────────────────────────
# COLOUR PALETTE
# ─────────────────────────────────────────────────────────────────────────────
P = {
    "navy": "#0A2342", "teal": "#1ABC9C", "gold": "#F39C12", "slate": "#2C3E50",
    "card":  "#1E2D40", "even": "#1A2535", "odd": "#223044",
    "border":"#2E4057", "text": "#ECF0F1", "muted":"#95A5A6",
}
CATEGORY_COLORS = {
    "Peak": "#2ECC71", "Grade": "#3498DB", "Entry": "#F39C12",
    "DOB": "#E74C3C", "ArfNo": "#95A5A6",
}
TIER_ICONS_BY_CAT = {"Peak": "star", "Grade": "layer-group", "Entry": "door-open",
                      "DOB": "birthday-cake", "ArfNo": "hashtag"}
CHART_BASE  = dict(
    paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
    font=dict(color=P["text"], family="Segoe UI"),
    margin=dict(l=40,r=20,t=40,b=40),
    xaxis=dict(gridcolor=P["border"], zerolinecolor=P["border"]),
    yaxis=dict(gridcolor=P["border"], zerolinecolor=P["border"]),
)
DQ_ICON = {"error":("times-circle","#E74C3C"), "warning":("exclamation-triangle","#F39C12"),
           "info":("info-circle","#3498DB"),    "success":("check-circle","#2ECC71")}

def tier_category(decision_basis: str) -> str:
    if decision_basis.startswith("Unique at BPS"):   return "Peak"
    if decision_basis.startswith("Broke at BPS-"):   return "Grade"
    if "Government Entry Date" in decision_basis:    return "Entry"
    if "Date of Birth" in decision_basis:             return "DOB"
    return "ArfNo"

def tier_label_for(decision_basis: str) -> str:
    cat = tier_category(decision_basis)
    if cat == "Grade":
        level = decision_basis.split("BPS-", 1)[1].split(" ", 1)[0]
        return f"Grade – BPS-{level}"
    return {"Peak": "Peak BPS/Date", "Entry": "Govt Entry Date",
            "DOB": "Date of Birth", "ArfNo": "ArfNo (Fallback)"}[cat]

def tier_color_for(decision_basis: str) -> str:
    return CATEGORY_COLORS[tier_category(decision_basis)]

# ─────────────────────────────────────────────────────────────────────────────
# SENIORITY ENGINE — same-grade recursive tie-break
# ─────────────────────────────────────────────────────────────────────────────
def resolve_seniority_order(tracking: pd.DataFrame, level_date: dict):
    """
    tracking needs: ArfNo, highest_bps, highest_bps_date, dateofentryingov, DateOfBirth
    level_date: {(ArfNo, bps_level): achieved_date}
    Returns (order_df, basis, trails):
      order_df — tracking reordered most- to least-senior
      basis    — {ArfNo: human-readable decision_basis string}
      trails   — {ArfNo: [step, ...]} full tier-by-tier trail for the modal/audit
    """
    basis: dict = {}
    trails: dict = {}
    def resolve(members_df, ceiling_level, trail_so_far):
        arfs = members_df["ArfNo"].tolist()
        if len(members_df) == 1:
            trails[arfs[0]] = trail_so_far
            return [members_df]
        for L in range(ceiling_level - 1, 0, -1):
            dates = {a: level_date.get((a, L)) for a in arfs}
            if not all(d is not None for d in dates.values()):
                continue  # grade not common to everyone still tied -> can't use it
            uniq_dates = sorted(set(dates.values()))
            if len(uniq_dates) == 1:
                trail_so_far = trail_so_far + [{
                    "level": L, "title": f"BPS-{L} Achievement Date", "resolved": False,
                    "note": f"All {len(arfs)} employees reached BPS-{L} on the same "
                            f"date — still tied, checking a lower grade.",
                    "compared": [{"ArfNo": a, "date": dates[a]} for a in arfs],
                }]
                continue
            blocks = []
            for d in uniq_dates:
                sub_arfs = [a for a in arfs if dates[a] == d]
                sub = members_df[members_df["ArfNo"].isin(sub_arfs)]
                step = {
                    "level": L, "title": f"BPS-{L} Achievement Date",
                    "resolved": len(sub) == 1,
                    "note": (f"Reached BPS-{L} on {d.strftime('%d-%b-%Y')} — "
                             + ("unique, rank decided here." if len(sub) == 1
                                else f"still tied with {len(sub) - 1} other(s).")),
                    "compared": [{"ArfNo": a, "date": dates[a]} for a in arfs],
                }
                new_trail = trail_so_far + [step]
                if len(sub) == 1:
                    a = sub.iloc[0]["ArfNo"]
                    basis[a] = f"Broke at BPS-{L} date ({d.strftime('%d-%b-%Y')})"
                    trails[a] = new_trail
                    blocks.append(sub)
                else:
                    blocks.extend(resolve(sub, L, new_trail))
            return blocks
        return resolve_fallback(members_df, trail_so_far)
    def resolve_fallback(members_df, trail_so_far):
        for col, label in [("dateofentryingov", "Government Entry Date"),
                            ("DateOfBirth", "Date of Birth")]:
            uniq_vals = sorted(members_df[col].dropna().unique())
            if len(uniq_vals) > 1:
                blocks = []
                for v in uniq_vals:
                    sub = members_df[members_df[col] == v]
                    step = {
                        "level": None, "title": label, "resolved": len(sub) == 1,
                        "note": (f"{label} {pd.Timestamp(v).strftime('%d-%b-%Y')} — "
                                 + ("unique, rank decided here." if len(sub) == 1 else "still tied.")),
                        "compared": [{"ArfNo": r.ArfNo, "date": getattr(r, col)}
                                     for r in members_df.itertuples()],
                    }
                    new_trail = trail_so_far + [step]
                    if len(sub) == 1:
                        a = sub.iloc[0]["ArfNo"]
                        basis[a] = f"Broke by {label} ({pd.Timestamp(v).strftime('%d-%b-%Y')})"
                        trails[a] = new_trail
                        blocks.append(sub)
                    else:
                        blocks.extend(resolve_fallback(sub, new_trail))
                return blocks
            trail_so_far = trail_so_far + [{
                "level": None, "title": label, "resolved": False,
                "note": f"All employees share the same {label.lower()} — no help.",
                "compared": [{"ArfNo": r.ArfNo, "date": getattr(r, col)} for r in members_df.itertuples()],
            }]
        sub = members_df.sort_values("ArfNo")
        blocks = []
        for r in sub.itertuples():
            a = r.ArfNo
            basis[a] = f"Broke by ArfNo ({a}) — all fields identical"
            trails[a] = trail_so_far + [{
                "level": None, "title": "ArfNo (Absolute Fallback)", "resolved": True,
                "note": f"Smallest ArfNo wins — {a}.",
                "compared": [{"ArfNo": x.ArfNo} for x in sub.itertuples()],
            }]
            blocks.append(members_df[members_df["ArfNo"] == a])
        return blocks
    tracking_sorted = tracking.sort_values(["highest_bps", "highest_bps_date"], ascending=[False, True])
    ordered_blocks = []
    for (bps, dt), grp in tracking_sorted.groupby(["highest_bps", "highest_bps_date"], sort=False):
        peak_step = {"level": int(bps), "title": f"Peak BPS {int(bps)} Achievement Date",
                     "compared": [{"ArfNo": r.ArfNo, "date": dt} for r in grp.itertuples()]}
        if len(grp) == 1:
            a = grp.iloc[0]["ArfNo"]
            peak_step["resolved"] = True
            peak_step["note"] = "Unique combination of peak BPS and achievement date."
            basis[a] = f"Unique at BPS {int(bps)} date ({pd.Timestamp(dt).strftime('%d-%b-%Y')})"
            trails[a] = [peak_step]
            ordered_blocks.append(grp)
        else:
            peak_step["resolved"] = False
            peak_step["note"] = f"{len(grp)} employees share this peak BPS and date — comparing lower grades."
            ordered_blocks.extend(resolve(grp, int(bps), [peak_step]))
    order_df = pd.concat(ordered_blocks, ignore_index=True)
    return order_df, basis, trails

# ─────────────────────────────────────────────────────────────────────────────
# DATA LOAD — from MySQL (all source columns are VARCHAR, parsed explicitly)
# ─────────────────────────────────────────────────────────────────────────────
def parse_messy_date(series, col_name, table_name):
    """Parse a VARCHAR date column that might be in any of several common
    formats. Tries each format in turn (fast path); whatever's left after all
    of them is handed to pandas' general parser as a last resort. Reports
    exactly how many values couldn't be parsed at all, instead of silently
    turning an entire mis-formatted column into NaT (which is what caused
    "No objects to concatenate" -- every row looked invalid, so nothing was
    left to build a report from).
    """
    raw = series.astype(str).str.strip()
    raw = raw.replace({"": None, "nan": None, "None": None, "NaT": None})
    parsed = pd.Series(pd.NaT, index=raw.index)
    remaining_mask = raw.notna()
    candidate_formats = ["%d-%b-%Y", "%d/%m/%Y", "%Y-%m-%d", "%m/%d/%Y",
                          "%d-%m-%Y", "%d.%m.%Y", "%Y/%m/%d"]
    for fmt in candidate_formats:
        if not remaining_mask.any():
            break
        attempt = pd.to_datetime(raw[remaining_mask], format=fmt, errors="coerce")
        newly_parsed = attempt.notna()
        idx = raw[remaining_mask][newly_parsed].index
        parsed.loc[idx] = attempt[newly_parsed]
        remaining_mask.loc[idx] = False
    if remaining_mask.any():
        attempt = pd.to_datetime(raw[remaining_mask], errors="coerce", dayfirst=True)
        newly_parsed = attempt.notna()
        idx = raw[remaining_mask][newly_parsed].index
        parsed.loc[idx] = attempt[newly_parsed]
        remaining_mask.loc[idx] = False
    n_total = len(raw)
    n_failed = int(remaining_mask.sum())
    if n_failed > 0:
        sample_bad = raw[remaining_mask].head(5).tolist()
        print(f"[date-parse warning] {table_name}.{col_name}: "
              f"{n_failed}/{n_total} values could not be parsed as a date. "
              f"Example unparsed values: {sample_bad}")
    return parsed

def build_seniority_report(engine):
    emp = pd.read_sql(
        "SELECT ArfNo, Namee, Trade, qualification, "
        "DateOfBirth, dateofentryingov, DateOfJoining, DateOfJoiningbps "
        "FROM temp_emp_data",
        engine,
    )
    emp["DateOfBirth"]      = parse_messy_date(emp["DateOfBirth"], "DateOfBirth", "temp_emp_data")
    emp["dateofentryingov"] = parse_messy_date(emp["dateofentryingov"], "dateofentryingov", "temp_emp_data")
    emp["DateOfJoining"]    = parse_messy_date(emp["DateOfJoining"], "DateOfJoining", "temp_emp_data")
    emp["DateOfJoiningbps"] = pd.to_numeric(emp["DateOfJoiningbps"], errors="coerce")

    promo = pd.read_sql(
        "SELECT ArfNo, dateofpromotion, dateofpromotionbps FROM temp_promotion",
        engine,
    )
    promo["dateofpromotion"]    = parse_messy_date(promo["dateofpromotion"], "dateofpromotion", "temp_promotion")
    promo["dateofpromotionbps"] = pd.to_numeric(promo["dateofpromotionbps"], errors="coerce")

    reapp = pd.read_sql(
        "SELECT ArfNo, dateofreappoitment, dateofreappoitmentbps FROM temp_reappointment",
        engine,
    )
    reapp["dateofreappoitment"]    = parse_messy_date(reapp["dateofreappoitment"], "dateofreappoitment", "temp_reappointment")
    reapp["dateofreappoitmentbps"] = pd.to_numeric(reapp["dateofreappoitmentbps"], errors="coerce")

    combined = emp.merge(promo, on="ArfNo", how="left").merge(reapp, on="ArfNo", how="left")

    def mk(df, date_col, bps_col, src, name_df=None):
        d = df[df[date_col].notna() & df[bps_col].notna()].copy()
        if name_df is not None:
            d = d.merge(name_df[["ArfNo","Namee"]], on="ArfNo", how="left")
        d = d[["ArfNo","Namee",date_col,bps_col]].copy()
        d.columns = ["ArfNo","Namee","event_date","bps_level"]
        d["source"] = src
        return d

    all_events = pd.concat([
        mk(emp,   "DateOfJoining",      "DateOfJoiningbps",      "Joining"),
        mk(reapp, "dateofreappoitment", "dateofreappoitmentbps", "Reappointment", emp),
        mk(promo, "dateofpromotion",    "dateofpromotionbps",    "Promotion",     emp),
    ], ignore_index=True)
    all_events["bps_level"] = all_events["bps_level"].astype(int)

    if all_events.empty:
        raise ValueError(
            "No valid career events found after parsing dates/BPS levels from "
            "temp_emp_data / temp_promotion / temp_reappointment. This almost "
            "always means the date format in those columns didn't match any "
            "of the formats parse_messy_date() tries, or DateOfJoiningbps / "
            "dateofpromotionbps / dateofreappoitmentbps aren't numeric. Check "
            "the '[date-parse warning]' lines printed above for the exact "
            "columns and example values that failed to parse."
        )

    bps_earliest = (all_events.groupby(["ArfNo","bps_level"])["event_date"]
                     .min().reset_index().rename(columns={"event_date":"achieved_date"}))
    # O(1) lookup used throughout tie-breaking — the key to keeping this fast
    # on large datasets (bounded recursion over ~22 grades, no re-filtering).
    LEVEL_DATE = {(r.ArfNo, r.bps_level): r.achieved_date for r in bps_earliest.itertuples()}

    max_bps  = all_events.groupby("ArfNo")["bps_level"].max().reset_index(name="highest_bps")
    peak_dt  = bps_earliest.merge(max_bps, left_on=["ArfNo","bps_level"], right_on=["ArfNo","highest_bps"])
    peak_dt  = peak_dt.groupby("ArfNo")["achieved_date"].min().reset_index(name="highest_bps_date")

    emp_base = combined.groupby("ArfNo").agg(
        Namee=("Namee","first"),
        dateofentryingov=("dateofentryingov","min"),
        DateOfBirth=("DateOfBirth","min"),
    ).reset_index()

    tracking = max_bps.merge(peak_dt, on="ArfNo").merge(emp_base, on="ArfNo")
    order_df, basis, trails = resolve_seniority_order(tracking, LEVEL_DATE)

    order_df["seniority_rank"]  = range(1, len(order_df) + 1)
    order_df["decision_basis"]  = order_df["ArfNo"].map(basis)
    order_df["tier_label"]      = order_df["decision_basis"].apply(tier_label_for)

    # ── Descriptive (non-decision-making) career path, purely for display ──
    disp_map, readable_map = {}, {}
    for arf, g in bps_earliest.groupby("ArfNo"):
        g = g.sort_values("achieved_date")
        peak = max_bps.loc[max_bps["ArfNo"] == arf, "highest_bps"].iloc[0]
        parts, full_parts = [], []
        for r in g.itertuples():
            year = r.achieved_date.year
            seg = f"**BPS-{r.bps_level}** _{year}_" if r.bps_level == peak else f"BPS-{r.bps_level} _{year}_"
            parts.append(seg)
            full_parts.append(f"BPS-{r.bps_level}: {r.achieved_date.strftime('%d-%b-%Y')}")
        disp_map[arf] = "  →  ".join(parts)
        readable_map[arf] = " → ".join(full_parts)
    order_df["career_path_display"]     = order_df["ArfNo"].map(disp_map)
    order_df["seniority_path_readable"] = order_df["ArfNo"].map(readable_map)

    order_df["tied_group_size"] = order_df.groupby(["highest_bps","highest_bps_date"])["ArfNo"].transform("count")
    order_df = order_df.merge(emp[["ArfNo","Trade","qualification"]], on="ArfNo", how="left")

    return order_df, all_events, emp, promo, reapp, LEVEL_DATE, trails

# ─────────────────────────────────────────────────────────────────────────────
# DATA QUALITY CHECKS  (unchanged)
# ─────────────────────────────────────────────────────────────────────────────
def run_dq(emp, promo, reapp, all_events):
    issues = []
    emp_arfs = set(emp["ArfNo"])
    checks = [
        ("warning", "Missing Date of Birth",    emp[emp["DateOfBirth"].isna()]),
        ("warning", "Missing Govt Entry Date",  emp[emp["dateofentryingov"].isna()]),
        ("info",    "Missing Qualification",    emp[emp["qualification"].isna()]),
    ]
    for level, title, bad in checks:
        if len(bad):
            issues.append((level, title, f"{len(bad)} employee(s): "+", ".join(bad["Namee"].astype(str))))
    for label, tbl in [("Promotions", promo), ("Reappointments", reapp)]:
        orphans = set(tbl["ArfNo"]) - emp_arfs
        if orphans:
            issues.append(("error", f"Orphan ArfNo in {label}", f"ArfNos {sorted(orphans)} not in master"))
    dup = promo[promo.duplicated(subset=["ArfNo","dateofpromotionbps"], keep=False)]
    if len(dup):
        issues.append(("warning","Duplicate Promotion Entries", f"{len(dup)} row(s) with same ArfNo+BPS"))
    reg = [f"{g['Namee'].iloc[0]} ({a})" for a,g in all_events.groupby("ArfNo")
           if any(g.sort_values("event_date")["bps_level"].diff().dropna()<0)]
    if reg: issues.append(("error","BPS Regression",", ".join(reg)))
    nog = [f"{g['Namee'].iloc[0]} ({a})" for a,g in all_events.groupby("ArfNo")
           if g["source"].nunique()==1 and g["source"].iloc[0]=="Joining"]
    if nog: issues.append(("info","No Progression Recorded",", ".join(nog)))
    if not issues: issues.append(("success","All checks passed","No data quality issues found."))
    return issues

# ─────────────────────────────────────────────────────────────────────────────
# LOAD
# ─────────────────────────────────────────────────────────────────────────────
report, all_events, emp_raw, promo_raw, reapp_raw, LEVEL_DATE, TRAILS = build_seniority_report(engine)
DQ = run_dq(emp_raw, promo_raw, reapp_raw, all_events)

report["dob_fmt"]      = pd.to_datetime(report["DateOfBirth"]).dt.strftime("%d-%b-%Y")
report["entry_fmt"]    = pd.to_datetime(report["dateofentryingov"]).dt.strftime("%d-%b-%Y")
report["bps_date_fmt"] = pd.to_datetime(report["highest_bps_date"]).dt.strftime("%d-%b-%Y")
report["tie_fmt"]      = report["tied_group_size"].apply(lambda n: "Unique" if n==1 else f"Tied ×{int(n)}")

BPS_WITH_DATA = sorted(all_events["bps_level"].unique())
QUAL_OPTS     = sorted(report["qualification"].dropna().unique())
TIER_LABEL_OPTIONS = sorted(report["tier_label"].unique())
TIER_LABEL_COLORS  = {t: tier_color_for(report.loc[report["tier_label"]==t, "decision_basis"].iloc[0])
                       for t in TIER_LABEL_OPTIONS}

# ─────────────────────────────────────────────────────────────────────────────
# COMPARISON MODAL RENDERER  (built from the recursive trail, not fixed tiers)
# ─────────────────────────────────────────────────────────────────────────────
def render_comparison_modal(arf):
    row = report[report["ArfNo"] == arf].iloc[0]
    trail = TRAILS.get(arf, [])
    dfmt = lambda d: pd.Timestamp(d).strftime("%d-%b-%Y") if pd.notna(d) else "N/A"
    arf_to_name = report.set_index("ArfNo")["Namee"].to_dict()
    arf_to_rank = report.set_index("ArfNo")["seniority_rank"].to_dict()
    tier_color = tier_color_for(row["decision_basis"])
    header = dbc.Card([
        dbc.CardBody(dbc.Row([
            dbc.Col([
                html.H5(row["Namee"], style={"color":P["text"],"fontWeight":"700","marginBottom":"4px"}),
                html.Div([
                    dbc.Badge(f"Rank #{row['seniority_rank']}", color="info",  className="me-2"),
                    dbc.Badge(f"BPS {row['highest_bps']}",     color="warning",className="me-2"),
                    dbc.Badge(row["qualification"] or "—",      color="secondary", className="me-2"),
                ]),
            ], md=7),
            dbc.Col([
                html.Div([
                    html.Span("Rank decided by ",style={"color":P["muted"],"fontSize":"0.85rem"}),
                    dbc.Badge(row["tier_label"], style={"backgroundColor":tier_color,"fontSize":"0.82rem"}),
                ], className="mb-1"),
                html.Div(row["decision_basis"],
                         style={"color":tier_color,"fontSize":"0.82rem","fontStyle":"italic"}),
            ], md=5),
        ])),
    ], style={"background":P["slate"],"border":f"2px solid {tier_color}","marginBottom":"16px"})
    path_strip = html.Div([
        html.Span("Career Path: ", style={"color":P["muted"],"fontSize":"0.8rem","marginRight":"8px"}),
        *[dbc.Badge(seg.strip(), color="info", className="me-1 mb-1", style={"fontSize":"0.78rem"})
          for seg in row["seniority_path_readable"].split("→")],
    ], className="mb-3")
    step_cards = []
    for i, step in enumerate(trail, start=1):
        cat = ("Peak" if step["title"].startswith("Peak") else
               "Grade" if step["level"] is not None else
               "Entry" if "Entry" in step["title"] else
               "DOB" if "Birth" in step["title"] else "ArfNo")
        tc = CATEGORY_COLORS[cat]
        icon = TIER_ICONS_BY_CAT[cat]
        peers = [c for c in step["compared"] if c["ArfNo"] != arf]
        if peers:
            emp_rows = [html.Tr([
                html.Td(f"#{arf_to_rank.get(p['ArfNo'], '?')}",
                        style={"color":P["teal"],"fontWeight":"700","width":"60px","padding":"6px 10px"}),
                html.Td(arf_to_name.get(p["ArfNo"], f"ArfNo {p['ArfNo']}"),
                        style={"color":P["text"],"fontWeight":"600","padding":"6px 10px"}),
                html.Td(dfmt(p.get("date")) if "date" in p else "—",
                        style={"color":P["muted"],"fontSize":"0.82rem","padding":"6px 10px"}),
            ], style={"borderBottom":f"1px solid {P['border']}"}) for p in peers]
            emp_table = html.Table([
                html.Thead(html.Tr([
                    html.Th("Rank", style={"color":tc,"padding":"6px 10px","width":"60px"}),
                    html.Th("Name", style={"color":tc,"padding":"6px 10px"}),
                    html.Th("Value", style={"color":tc,"padding":"6px 10px"}),
                ], style={"borderBottom":f"2px solid {tc}"})),
                html.Tbody(emp_rows),
            ], style={"width":"100%","borderCollapse":"collapse","fontSize":"0.86rem"})
        else:
            emp_table = html.Div("No other employees compared at this step.",
                                  style={"color":P["muted"],"fontSize":"0.83rem","fontStyle":"italic"})
        status = html.Div([
            html.Span("✅ RESOLVED HERE — ", style={"color":"#2ECC71","fontWeight":"700","fontSize":"0.85rem"})
            if step["resolved"] else
            html.Span("⬇ Still tied — checking next grade", style={"color":P["muted"],"fontWeight":"600","fontSize":"0.82rem"}),
            html.Span(step["note"], style={"color":P["muted"],"fontSize":"0.82rem","marginLeft":"6px"}),
        ], style={"marginTop":"8px","padding":"8px 0","borderTop":f"1px solid {P['border']}"})
        step_cards.append(dbc.Card([
            dbc.CardHeader([
                html.I(className=f"fas fa-{icon} me-2", style={"color":tc}),
                html.Span(f"Step {i}", style={"color":tc,"fontWeight":"800","marginRight":"8px"}),
                html.Span(step["title"], style={"color":P["text"],"fontWeight":"600"}),
            ], style={"background":tc+"22","borderBottom":f"1px solid {tc}"}),
            dbc.CardBody([emp_table, status]),
        ], style={
            "background":P["even"],
            "border":f"2px solid {tc}" if step["resolved"] else f"1px solid {P['border']}",
            "marginBottom":"10px",
            "boxShadow":f"0 0 12px {tc}55" if step["resolved"] else "none",
        }))
    return html.Div([header, path_strip, *step_cards])

# ─────────────────────────────────────────────────────────────────────────────
# TIE-BREAK AUDIT — walks a whole tied GROUP through the same-grade cascade
# ─────────────────────────────────────────────────────────────────────────────
def build_group_cascade(arf_list):
    """Mirrors resolve_seniority_order()'s logic but records every step for a
    whole cluster, so the audit tab can show exactly who's compared to whom."""
    df = report[report["ArfNo"].isin(arf_list)].sort_values("seniority_rank").copy()
    if df.empty:
        return []
    peak_bps = int(df.iloc[0]["highest_bps"])
    cascade = []
    current_groups = [df]
    for L in range(peak_bps - 1, 0, -1):
        splits, next_groups, used = [], [], False
        for g in current_groups:
            arfs = g["ArfNo"].tolist()
            dates = {a: LEVEL_DATE.get((a, L)) for a in arfs}
            if not all(d is not None for d in dates.values()):
                next_groups.append(g)  # grade not common — pass this cluster through untouched
                continue
            used = True
            for d in sorted(set(dates.values())):
                sub_arfs = [a for a in arfs if dates[a] == d]
                sub = g[g["ArfNo"].isin(sub_arfs)]
                splits.append({"value": d.strftime("%d-%b-%Y"), "members": sub, "resolved": len(sub) == 1})
                if len(sub) > 1:
                    next_groups.append(sub)
        if used:
            cascade.append({"level": L, "title": f"BPS-{L} Achievement Date", "splits": splits})
        current_groups = next_groups
        if not current_groups:
            return cascade
    for field, title, fmt in [("dateofentryingov", "Government Entry Date", True),
                               ("DateOfBirth", "Date of Birth", True),
                               ("ArfNo", "ArfNo (final fallback)", False)]:
        splits, next_groups = [], []
        for g in current_groups:
            for val, sub in g.groupby(field, sort=False):
                label = pd.Timestamp(val).strftime("%d-%b-%Y") if fmt else str(val)
                splits.append({"value": label, "members": sub, "resolved": len(sub) == 1})
                if len(sub) > 1:
                    next_groups.append(sub)
        cascade.append({"level": None, "title": title, "splits": splits})
        current_groups = next_groups
        if not current_groups:
            break
    return cascade

def render_step_table(step):
    all_members = pd.concat([s["members"] for s in step["splits"]]).sort_values("seniority_rank")
    value_map, counts = {}, {}
    for s in step["splits"]:
        counts[s["value"]] = counts.get(s["value"], 0) + len(s["members"])
        for a in s["members"]["ArfNo"]:
            value_map[a] = s["value"]
    header = html.Tr([
        html.Th("Rank", style={"color":P["teal"],"padding":"6px 10px"}),
        html.Th("Name", style={"color":P["teal"],"padding":"6px 10px"}),
        html.Th(step["title"], style={"color":P["teal"],"padding":"6px 10px"}),
        html.Th("Status", style={"color":P["teal"],"padding":"6px 10px"}),
    ], style={"borderBottom":f"2px solid {P['teal']}"})
    rows = []
    for r in all_members.itertuples():
        v = value_map[r.ArfNo]
        shared = counts[v] > 1
        bg = "#E74C3C22" if shared else "#2ECC7122"
        marker = "🔗 still tied" if shared else "✅ splits off here"
        rows.append(html.Tr([
            html.Td(f"#{r.seniority_rank}", style={"color":P["teal"],"fontWeight":"700","padding":"6px 10px"}),
            html.Td(r.Namee, style={"color":P["text"],"fontWeight":"600","padding":"6px 10px"}),
            html.Td(v, style={"color":P["text"],"padding":"6px 10px","background":bg}),
            html.Td(marker, style={"fontSize":"0.72rem","fontWeight":"700",
                                    "color":"#E74C3C" if shared else "#2ECC71","padding":"6px 10px"}),
        ], style={"borderBottom":f"1px solid {P['border']}"}))
    return html.Table([html.Thead(header), html.Tbody(rows)],
                       style={"width":"100%","borderCollapse":"collapse","marginBottom":"16px"})

def render_cascade(cascade, header_label=""):
    blocks = []
    if header_label:
        blocks.append(html.H6(header_label, style={"color":P["gold"],"fontWeight":"700","marginBottom":"10px"}))
    for step in cascade:
        cat = "Grade" if step["level"] is not None else ("Entry" if "Entry" in step["title"] else
              "DOB" if "Birth" in step["title"] else "ArfNo")
        tc = CATEGORY_COLORS[cat]
        still_tied = [s for s in step["splits"] if len(s["members"]) > 1]
        resolved_now = [s for s in step["splits"] if len(s["members"]) == 1]
        body = []
        if step["splits"]:
            body.append(render_step_table(step))
        if resolved_now:
            names = ", ".join(f"{s['members'].iloc[0]['Namee']} (#{int(s['members'].iloc[0]['seniority_rank'])})"
                               for s in resolved_now)
            body.append(html.Div([
                html.I(className="fas fa-check-circle me-2", style={"color":"#2ECC71"}),
                html.Span(f"Resolved at this step: {names}", style={"color":"#2ECC71","fontSize":"0.82rem"}),
            ]))
        blocks.append(dbc.Card([
            dbc.CardHeader([
                html.I(className=f"fas fa-{TIER_ICONS_BY_CAT[cat]} me-2", style={"color":tc}),
                html.Span(step["title"], style={"color":P["text"],"fontWeight":"600"}),
            ], style={"background":tc+"22","borderBottom":f"1px solid {tc}"}),
            dbc.CardBody(body if body else html.Div("Nothing to compare here.",
                         style={"color":P["muted"],"fontStyle":"italic"})),
        ], style={"background":P["card"],"border":f"1px solid {P['border']}","marginBottom":"10px"}))
        if not still_tied:
            blocks.append(html.Div([
                html.I(className="fas fa-flag-checkered me-2", style={"color":"#2ECC71"}),
                html.Span("Every employee in this group is fully resolved.",
                          style={"color":"#2ECC71","fontWeight":"600"}),
            ], className="mb-3"))
            break
    return html.Div(blocks)

def full_career_str(arf):
    evts = all_events[all_events["ArfNo"] == arf].sort_values("event_date")
    if evts.empty:
        return html.Span("No events on record", style={"color": P["muted"]})
    return html.Div([
        dbc.Badge(f"{e.source}: BPS {int(e.bps_level)}  ({e.event_date.strftime('%d-%b-%Y')})",
                  className="me-1 mb-1",
                  style={"fontSize": "0.72rem", "backgroundColor": P["slate"], "color": P["text"],
                         "border": f"1px solid {P['border']}"})
        for e in evts.itertuples()
    ])

# ─────────────────────────────────────────────────────────────────────────────
# UI HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def stat_card(title, value, icon, color):
    return dbc.Card([dbc.CardBody(html.Div([
        html.Div([
            html.P(title, className="mb-0",
                   style={"color":P["muted"],"fontSize":"0.78rem",
                          "textTransform":"uppercase","letterSpacing":"0.08em"}),
            html.H3(str(value), className="mb-0",
                    style={"color":P["text"],"fontWeight":"700"}),
        ]),
        html.I(className=f"fas fa-{icon} fa-2x", style={"color":color,"opacity":"0.8"}),
    ], style={"display":"flex","justifyContent":"space-between","alignItems":"center"}))],
    style={"background":P["card"],"border":f"1px solid {color}",
           "borderLeft":f"4px solid {color}","borderRadius":"8px"})

def dq_alert(level, title, detail):
    icon, color = DQ_ICON[level]
    return dbc.Alert([
        html.I(className=f"fas fa-{icon} me-2", style={"color":color}),
        html.B(title+": ", style={"color":color}),
        html.Span(detail, style={"color":P["text"]}),
    ], style={"background":"#1A2535","border":f"1px solid {color}"}, className="mb-2")

def raw_tbl(df, tid):
    return dash_table.DataTable(
        id=tid, data=df.astype(str).to_dict("records"),
        columns=[{"name":c,"id":c} for c in df.columns],
        page_size=10, sort_action="native", filter_action="native",
        style_table={"overflowX":"auto"},
        style_header={"backgroundColor":P["slate"],"color":P["teal"],"fontWeight":"700",
                      "border":f"1px solid {P['border']}","fontSize":"0.75rem"},
        style_cell={"backgroundColor":P["even"],"color":P["text"],
                    "border":f"1px solid {P['border']}","fontSize":"0.8rem","padding":"6px 10px"},
        style_data_conditional=[{"if":{"row_index":"odd"},"backgroundColor":P["odd"]}],
    )

# ─────────────────────────────────────────────────────────────────────────────
# COMPARISON MODAL COMPONENT
# ─────────────────────────────────────────────────────────────────────────────
comparison_modal = dbc.Modal([
    dbc.ModalHeader([
        html.I(className="fas fa-balance-scale me-2", style={"color":P["gold"]}),
        html.Span(id="modal-title",
                  style={"color":P["gold"],"fontWeight":"700","fontSize":"1.05rem"}),
    ], style={"background":P["navy"],"borderBottom":f"2px solid {P['gold']}"}),
    dbc.ModalBody(
        html.Div(id="modal-body", style={"background":P["navy"],"padding":"0"}),
        style={"background":P["navy"],"maxHeight":"78vh","overflowY":"auto"},
    ),
    dbc.ModalFooter([
        html.Small("Click any row in the Seniority List to compare a different employee.",
                   style={"color":P["muted"]}),
        dbc.Button("Close", id="modal-close", color="secondary", size="sm"),
    ], style={"background":P["navy"],"borderTop":f"1px solid {P['border']}"}),
], id="comparison-modal", size="xl", is_open=False,
   style={"--bs-modal-bg":P["navy"]})

# ─────────────────────────────────────────────────────────────────────────────
# FILTER BAR
# ─────────────────────────────────────────────────────────────────────────────
filter_bar = dbc.Card([dbc.CardBody(dbc.Row([
    dbc.Col([
        html.Label("Search Name / ArfNo",style={"color":P["muted"],"fontSize":"0.8rem"}),
        dbc.Input(id="search-input", placeholder="Type to search…", debounce=True,
                  style={"background":"#1A2535","color":P["text"],"border":f"1px solid {P['border']}"}),
    ], md=4),
    dbc.Col([
        html.Label("BPS Level",style={"color":P["muted"],"fontSize":"0.8rem"}),
        dcc.Dropdown(id="bps-filter",
            options=[{"label":f"BPS {b}"+(""if b in BPS_WITH_DATA else" (no data)"),
                      "value":b,"disabled":b not in BPS_WITH_DATA} for b in range(1,23)],
            multi=True, placeholder="All BPS levels",
            style={"background":"#1A2535","color":"#000"}),
    ], md=3),
    dbc.Col([
        html.Label("Qualification",style={"color":P["muted"],"fontSize":"0.8rem"}),
        dcc.Dropdown(id="qual-filter",
            options=[{"label":q,"value":q} for q in QUAL_OPTS],
            multi=True, placeholder="All qualifications",
            style={"background":"#1A2535","color":"#000"}),
    ], md=3),
    dbc.Col([
        html.Label("Rank Range",style={"color":P["muted"],"fontSize":"0.8rem"}),
        dcc.RangeSlider(id="rank-slider", min=1, max=len(report), step=1,
            value=[1,len(report)], marks={1:"1",len(report):str(len(report))},
            tooltip={"placement":"bottom","always_visible":False}),
    ], md=2),
]))], style={"background":P["card"],"border":f"1px solid {P['border']}","marginBottom":"16px"})

# ─────────────────────────────────────────────────────────────────────────────
# TABS
# ─────────────────────────────────────────────────────────────────────────────
tab_overview = dbc.Tab(label="📊 Overview", tab_id="tab-overview", children=[
    dbc.Row([
        dbc.Col(stat_card("Total Employees",    len(report),                          "users",          P["teal"]),  md=3),
        dbc.Col(stat_card("BPS Levels",         report["highest_bps"].nunique(),      "layer-group",    P["gold"]),  md=3),
        dbc.Col(stat_card("Qualifications",     report["qualification"].nunique(),    "graduation-cap", "#9B59B6"),  md=3),
        dbc.Col(stat_card("Resolved at Peak",   (report["tier_label"]=="Peak BPS/Date").sum(),"trophy","#2ECC71"),md=3),
    ], className="mb-4"),
    dbc.Row([
        dbc.Col(dbc.Card([
            dbc.CardHeader("Employees per BPS Level",style={"background":P["slate"],"color":P["text"]}),
            dbc.CardBody(dcc.Graph(id="chart-bps-dist",config={"displayModeBar":False})),
        ], style={"background":P["card"],"border":f"1px solid {P['border']}"}), md=6),
        dbc.Col(dbc.Card([
            dbc.CardHeader("Tiebreaker Step Distribution",style={"background":P["slate"],"color":P["text"]}),
            dbc.CardBody(dcc.Graph(id="chart-tier-pie",config={"displayModeBar":False})),
        ], style={"background":P["card"],"border":f"1px solid {P['border']}"}), md=6),
    ], className="mb-4"),
    dbc.Row([
        dbc.Col(dbc.Card([
            dbc.CardHeader("Qualification by BPS",style={"background":P["slate"],"color":P["text"]}),
            dbc.CardBody(dcc.Graph(id="chart-qual-bps",config={"displayModeBar":False})),
        ], style={"background":P["card"],"border":f"1px solid {P['border']}"}), md=8),
        dbc.Col(dbc.Card([
            dbc.CardHeader("Joining Year",style={"background":P["slate"],"color":P["text"]}),
            dbc.CardBody(dcc.Graph(id="chart-join-year",config={"displayModeBar":False})),
        ], style={"background":P["card"],"border":f"1px solid {P['border']}"}), md=4),
    ]),
])

tab_list = dbc.Tab(label="📋 Seniority List", tab_id="tab-list", children=[
    dbc.Alert([
        html.I(className="fas fa-mouse-pointer me-2",style={"color":P["teal"]}),
        html.Span("Click any row to open a ", style={"color":P["text"]}),
        html.B("Tier-by-Tier Comparison popup",  style={"color":P["teal"]}),
        html.Span(" showing exactly who each employee is compared against — always on the same BPS grade — and why their rank was decided.",
                  style={"color":P["text"]}),
    ], style={"background":"#1A2535","border":f"1px solid {P['teal']}","marginBottom":"12px"}),
    html.Div(id="table-info",style={"color":P["muted"],"fontSize":"0.85rem","marginBottom":"8px"}),
    dash_table.DataTable(
        id="seniority-table",
        columns=[
            {"name":"Rank",         "id":"seniority_rank"},
            {"name":"ArfNo",        "id":"ArfNo"},
            {"name":"Name",         "id":"Namee"},
            {"name":"Qualification","id":"qualification"},
            {"name":"Peak BPS",     "id":"highest_bps"},
            {"name":"BPS Date",     "id":"bps_date_fmt"},
            {"name":"Resolved By",  "id":"tier_label"},
            {"name":"Govt Entry",   "id":"entry_fmt"},
            {"name":"DOB",          "id":"dob_fmt"},
            {"name":"Decision",     "id":"decision_basis"},
            {"name":"Career Path",  "id":"career_path_display", "presentation":"markdown"},
        ],
        markdown_options={"html": False},
        page_size=15, sort_action="native", row_selectable="single",
        style_table={"overflowX":"auto"},
        style_header={"backgroundColor":P["slate"],"color":P["teal"],"fontWeight":"700",
                      "border":f"1px solid {P['border']}","textTransform":"uppercase","fontSize":"0.75rem"},
        style_cell={"backgroundColor":P["even"],"color":P["text"],"border":f"1px solid {P['border']}",
                    "fontSize":"0.84rem","padding":"9px 12px",
                    "maxWidth":"240px","overflow":"hidden","textOverflow":"ellipsis","whiteSpace":"nowrap"},
        style_cell_conditional=[
            {"if":{"column_id":"career_path_display"},
             "whiteSpace":"normal","height":"auto","textAlign":"left",
             "minWidth":"300px","maxWidth":"440px","textOverflow":"unset","overflow":"visible"},
        ],
        style_data_conditional=[
            {"if":{"row_index":"odd"},"backgroundColor":P["odd"]},
            {"if":{"column_id":"seniority_rank"},"fontWeight":"700","color":P["teal"],"textAlign":"center"},
            {"if":{"column_id":"highest_bps"},   "fontWeight":"700","color":P["gold"],"textAlign":"center"},
            {"if":{"state":"selected"},"backgroundColor":"#2E4057","border":f"1px solid {P['teal']}"},
            *[{"if":{"filter_query":f'{{tier_label}} = "{t}"',"column_id":"tier_label"},
               "color":c,"fontWeight":"600"} for t,c in TIER_LABEL_COLORS.items()],
        ],
        tooltip_data=[], tooltip_delay=0, tooltip_duration=None,
    ),
])

tab_employee = dbc.Tab(label="🔍 Employee Profile", tab_id="tab-employee", children=[
    dbc.Row([
        dbc.Col([
            html.Label("Select Employee",style={"color":P["muted"],"fontSize":"0.8rem"}),
            dcc.Dropdown(id="emp-selector",
                options=[{"label":f"[{r.seniority_rank}] {r.Namee} (ArfNo {r.ArfNo})","value":r.ArfNo}
                         for r in report.sort_values("seniority_rank").itertuples()],
                placeholder="Choose an employee…",style={"color":"#000"}),
        ], md=6),
        dbc.Col(html.Div(id="emp-rank-badge",className="mt-4"), md=6),
    ], className="mb-4"),
    html.Div(id="emp-profile-cards",className="mb-4"),
    dbc.Row([
        dbc.Col(dcc.Graph(id="emp-career-timeline",config={"displayModeBar":False}),md=8),
        dbc.Col(dcc.Graph(id="emp-bps-gauge",       config={"displayModeBar":False}),md=4),
    ]),
    dbc.Card([
        dbc.CardHeader("Seniority Decision",
                       style={"background":P["slate"],"color":P["text"]}),
        dbc.CardBody(html.Div(id="emp-decision-text")),
    ], style={"background":P["card"],"border":f"1px solid {P['teal']}","marginTop":"16px"}),
    dbc.Card([
        dbc.CardHeader([
            html.I(className="fas fa-balance-scale me-2",style={"color":P["gold"]}),
            html.Span("Tier-by-Tier Comparison Breakdown",
                      style={"color":P["gold"],"fontWeight":"700"}),
        ], style={"background":P["slate"]}),
        dbc.CardBody(html.Div(id="emp-comparison-body"),
                     style={"background":P["navy"],"padding":"12px"}),
    ], style={"background":P["card"],"border":f"2px solid {P['gold']}","marginTop":"16px"}),
])

tab_bps = dbc.Tab(label="📈 BPS Deep Dive", tab_id="tab-bps", children=[
    dbc.Card(dbc.CardBody(dbc.Row([
        dbc.Col([
            html.Label("Select BPS Level(s) — defaults to ALL levels with data",
                       style={"color":P["muted"],"fontSize":"0.8rem"}),
            dcc.Dropdown(id="bps-group-selector",
                options=[{"label":f"BPS {b}"+(""if b in BPS_WITH_DATA else" (no data)"),
                          "value":b,"disabled":b not in BPS_WITH_DATA} for b in range(1,23)],
                value=BPS_WITH_DATA, multi=True, clearable=True,
                placeholder="Select one or more BPS levels…", style={"color":"#000"}),
            html.Div([
                dbc.Button("Select All", id="bps-select-all-btn", size="sm", color="info",
                           outline=True, className="me-2 mt-2",
                           style={"borderColor":P["teal"],"color":P["teal"]}),
                dbc.Button("Clear", id="bps-clear-btn", size="sm", color="secondary",
                           outline=True, className="mt-2"),
            ]),
        ], md=4),
        dbc.Col(html.Div(id="bps-summary-badges"),md=8,
                style={"display":"flex","alignItems":"center","paddingTop":"10px","flexWrap":"wrap"}),
    ])), style={"background":P["card"],"border":f"1px solid {P['border']}","marginBottom":"16px"}),
    dbc.Row([
        dbc.Col(dbc.Card([
            dbc.CardHeader("Ranked Members",style={"background":P["slate"],"color":P["text"]}),
            dbc.CardBody(dcc.Graph(id="bps-group-bar",config={"displayModeBar":False})),
        ], style={"background":P["card"],"border":f"1px solid {P['border']}"}), md=7),
        dbc.Col(dbc.Card([
            dbc.CardHeader("Achievement Dates",style={"background":P["slate"],"color":P["text"]}),
            dbc.CardBody(dcc.Graph(id="bps-group-timeline",config={"displayModeBar":False})),
        ], style={"background":P["card"],"border":f"1px solid {P['border']}"}), md=5),
    ], className="mb-4"),
    dbc.Card([
        dbc.CardHeader(dbc.Row([
            dbc.Col("🔗 Tie Groups at this BPS Level",style={"color":P["teal"],"fontWeight":"700"},md=8),
            dbc.Col(html.Div(id="tie-count-badge"),md=4,style={"textAlign":"right"}),
        ]), style={"background":P["slate"]}),
        dbc.CardBody(html.Div(id="tie-groups-panel")),
    ], style={"background":P["card"],"border":f"2px solid {P['teal']}","marginBottom":"16px"}),
    dbc.Card([
        dbc.CardHeader("📊 Career Path Heatmap",style={"background":P["slate"],"color":P["text"]}),
        dbc.CardBody(dcc.Graph(id="bps-career-compare",config={"displayModeBar":False})),
    ], style={"background":P["card"],"border":f"1px solid {P['border']}","marginBottom":"16px"}),
    dbc.Card([
        dbc.CardHeader("📋 Full Ranked Table",style={"background":P["slate"],"color":P["text"]}),
        dbc.CardBody(html.Div(id="bps-group-table")),
    ], style={"background":P["card"],"border":f"1px solid {P['border']}"}),
])

def _empty_bps_response(msg):
    """Shared placeholder response used whenever the current BPS selection has
    no usable data, or (in update_bps) whenever rendering it raised an
    exception. Keeps every Output filled with something renderable so a bad
    selection can never leave any part of the tab blank."""
    ef = go.Figure(); ef.update_layout(**CHART_BASE, height=320,
        annotations=[dict(text=msg, showarrow=False, font=dict(color=P["muted"], size=14))])
    placeholder = html.Div(msg, style={"color": P["muted"], "padding": "12px", "fontStyle": "italic"})
    badges = [dbc.Badge(msg, color="secondary", className="me-2 fs-6")]
    tie_badge = dbc.Badge("No data", color="secondary", className="fs-6")
    return ef, ef, badges, tie_badge, placeholder, ef, placeholder

def build_all_bps_overview_data():
    """Aggregate, per BPS level: how many employees ever reached it, how many
    eventually peaked there vs moved on to a higher grade, and how many
    tie groups exist at that level. Mirrors the per-level logic used by
    build_accordion()/update_bps(), but only keeps the counts -- used to
    drive the summary chart at the top of the 'All BPS Levels' tab so the
    person gets an at-a-glance view before opening any accordion item.
    """
    rows = []
    for bv in range(1, 23):
        le = all_events[(all_events["bps_level"] == bv) & (all_events["event_date"].notna())]
        if le.empty:
            continue
        le2 = le.groupby("ArfNo")["event_date"].min().reset_index(name="level_date")
        merged = le2.merge(report[["ArfNo", "highest_bps"]], on="ArfNo", how="left")
        merged = merged[merged["highest_bps"].notna()]  # drop true orphan ArfNos, same rule as elsewhere
        if merged.empty:
            continue
        reached = len(merged)
        peaked = int((merged["highest_bps"] == bv).sum())
        ld_fmt = merged["level_date"].dt.strftime("%d-%b-%Y")
        tie_groups = int((ld_fmt.value_counts() > 1).sum())
        rows.append({"bps": bv, "reached": reached, "peaked": peaked,
                     "moved_on": reached - peaked, "tie_groups": tie_groups})
    return pd.DataFrame(rows)

def build_all_bps_overview_chart():
    """Stacked bar: employees reached per BPS level, split into 'peaked here'
    vs 'moved on to a higher grade', with a small marker for how many tie
    groups exist at that level. Built once at load time (not filter-reactive,
    same as the accordion itself) since it's a structural overview, not a
    per-search-term view."""
    df_ov = build_all_bps_overview_data()
    if df_ov.empty:
        fig = go.Figure(); fig.update_layout(**CHART_BASE, height=320,
            annotations=[dict(text="No BPS data available.", showarrow=False,
                               font=dict(color=P["muted"], size=14))])
        return fig
    melted = df_ov.melt(id_vars=["bps", "tie_groups", "reached"],
                         value_vars=["peaked", "moved_on"],
                         var_name="status", value_name="count")
    melted["status_label"] = melted["status"].map(
        {"peaked": "Peaked here", "moved_on": "Moved on to a higher grade"})
    fig = px.bar(melted.sort_values("bps"), x="bps", y="count", color="status_label",
                 color_discrete_map={"Peaked here": P["gold"],
                                      "Moved on to a higher grade": P["teal"]},
                 labels={"bps": "BPS Level", "count": "Employees", "status_label": ""},
                 title="Employees Reaching Each BPS Level  ·  🔗 marks levels with tie groups")
    for _, r in df_ov.iterrows():
        if r["tie_groups"] > 0:
            fig.add_annotation(x=r["bps"], y=r["reached"], yshift=14, showarrow=False,
                                text=f"🔗{int(r['tie_groups'])}",
                                font=dict(color=P["gold"], size=11))
    fig.update_layout(**CHART_BASE, height=380, legend=dict(orientation="h", y=-0.25))
    fig.update_xaxes(dtick=1, gridcolor=P["border"])
    fig.update_traces(marker_line_width=0)
    return fig

def build_accordion():
    items = []
    # Senior grades first: for a seniority dashboard, the higher/crowded
    # grades (where ties actually matter most) are more relevant to open
    # first, rather than starting at BPS 1.
    for bv in range(22, 0, -1):
        try:
            le = all_events[all_events["bps_level"] == bv]
            if le.empty:
                continue
            # Drop rows with no usable event_date up front -- a NaT here is
            # what crashes .dt.strftime()/groupby("event_date") below, and
            # null dates get more common the sparser/higher a BPS grade is.
            le = le[le["event_date"].notna()]
            if le.empty:
                continue
            le2 = le.groupby("ArfNo")["event_date"].min().reset_index(name="level_date")
            df = le2.merge(report, on="ArfNo", how="left").sort_values("seniority_rank")
            # Guard against a how="left" merge silently introducing NaN for
            # any ArfNo that doesn't have a matching row in `report` at all
            # (a true orphan). IMPORTANT: only "seniority_rank" is checked --
            # every legitimate employee in `report` has one, so if it's NaN
            # the merge genuinely failed to find that ArfNo. Fields like
            # "qualification" are legitimately blank for some real employees
            # (missing data entry, not a merge failure) and must NOT be used
            # here, or real employees get incorrectly dropped as "orphans".
            missing = df["seniority_rank"].isna()
            if missing.any():
                print(f"[All BPS Levels] BPS {bv}: dropping {missing.sum()} row(s) "
                      f"with no matching report data (true orphan ArfNo): "
                      f"{df.loc[missing,'ArfNo'].tolist()}")
                df = df[~missing]
            if df.empty:
                continue
            df["qualification"] = df["qualification"].fillna("Not specified")
            df["ld_fmt"] = df["level_date"].dt.strftime("%d-%b-%Y")
            df["is_peak"] = df["highest_bps"] == bv
            tg = {d: g for d, g in df.groupby("ld_fmt") if len(g) > 1}
            rows = [html.Tr([
                html.Td(f"#{r.seniority_rank}", style={"color": P["teal"], "fontWeight": "700"}),
                html.Td(r.Namee, style={"color": P["text"]}),
                html.Td(r.qualification, style={"color": P["muted"], "fontSize": "0.8rem"}),
                html.Td(r.ld_fmt, style={"color": P["text"]}),
                html.Td(dbc.Badge("Peak" if r.is_peak else f"→ BPS {r.highest_bps}",
                                   style={"backgroundColor": P["gold"] if r.is_peak else P["slate"],
                                          "color": "#000" if r.is_peak else P["text"], "fontSize": "0.72rem"})),
                html.Td(r.tier_label, style={"color": TIER_LABEL_COLORS.get(r.tier_label, P["muted"]),
                                              "fontSize": "0.78rem", "fontWeight": "600"}),
                html.Td(r.decision_basis, style={"color": P["muted"], "fontSize": "0.76rem"}),
            ], style={"borderBottom": f"1px solid {P['border']}"}) for r in df.itertuples()]
            body = html.Div([
                dbc.Row([
                    dbc.Col(dbc.Badge(f"{len(df)} reached", color="info", className="me-2 fs-6"), width="auto"),
                    dbc.Col(dbc.Badge(f"{int(df['is_peak'].sum())} peaked", color="primary", className="me-2 fs-6"), width="auto"),
                    dbc.Col(dbc.Badge(f"{len(tg)} tie group{'s' if len(tg) != 1 else ''}",
                                      color="warning" if tg else "success", className="me-2 fs-6"), width="auto"),
                ], className="mb-3"),
                html.Table([
                    html.Thead(html.Tr([html.Th(h, style={"color": P["teal"]}) for h in
                        ["Rank", "Name", "Qualification", "Date Reached", "Status", "Tier", "Decision"]],
                        style={"borderBottom": f"2px solid {P['teal']}"})),
                    html.Tbody(rows),
                ], style={"width": "100%", "borderCollapse": "collapse", "fontSize": "0.84rem"}),
            ])
            items.append(dbc.AccordionItem(body, item_id=f"bps-{bv}",
                title=f"BPS {bv}  ·  {len(df)} employees  ·  {int(df['is_peak'].sum())} peaked  ·  {len(tg)} tie group(s)"))
        except Exception as e:
            # A single bad BPS level (odd ArfNo format, unexpected NaN, etc.)
            # must never stop the accordion -- and therefore app.layout --
            # from building at all. Log it and render a visible error item
            # for just that level instead.
            print(f"[All BPS Levels] BPS {bv} skipped due to error: {type(e).__name__}: {e}")
            items.append(dbc.AccordionItem(
                html.Div(f"Could not render BPS {bv} ({type(e).__name__}). See console for details.",
                          style={"color": P["muted"], "fontStyle": "italic", "padding": "12px"}),
                item_id=f"bps-{bv}", title=f"BPS {bv}  ·  ⚠ render error"))
    return items

tab_all_bps = dbc.Tab(label="🗂️ All BPS Levels", tab_id="tab-all-bps", children=[
    html.P("All BPS tiers at once. Click any row in the Seniority List tab for a full comparison popup.",
           style={"color":P["muted"],"fontSize":"0.85rem","margin":"12px 0"}),
    dbc.Card([
        dbc.CardHeader("📊 Overview — Employees per BPS Level",
                       style={"background":P["slate"],"color":P["text"]}),
        dbc.CardBody(dcc.Graph(figure=build_all_bps_overview_chart(), config={"displayModeBar":False})),
    ], style={"background":P["card"],"border":f"1px solid {P['border']}","marginBottom":"16px"}),
    html.P("Detail by grade (senior grades first — click to expand):",
           style={"color":P["muted"],"fontSize":"0.82rem","margin":"4px 0 8px"}),
    dbc.Accordion(build_accordion(), start_collapsed=True, always_open=True, id="all-bps-accordion"),
])

tab_career = dbc.Tab(label="🗺️ Career Paths", tab_id="tab-career", children=[
    dbc.Row([dbc.Col(dcc.Graph(id="career-sankey",  config={"displayModeBar":False}))],className="mb-4"),
    dbc.Row([dbc.Col(dcc.Graph(id="career-scatter", config={"displayModeBar":False}))],className="mb-4"),
    dbc.Card([
        dbc.CardHeader("Full BPS Milestone History",style={"background":P["slate"],"color":P["text"]}),
        dbc.CardBody(html.Div(id="career-milestone-table")),
    ], style={"background":P["card"],"border":f"1px solid {P['border']}","marginBottom":"16px"}),
    dbc.Card([
        dbc.CardHeader("First to Reach Each BPS Level",style={"background":P["slate"],"color":P["text"]}),
        dbc.CardBody(html.Div(id="career-first-to-reach")),
    ], style={"background":P["card"],"border":f"1px solid {P['border']}"}),
])

tab_dataset = dbc.Tab(label="🧬 Dataset Explorer", tab_id="tab-dataset", children=[
    dbc.Row([
        dbc.Col(stat_card("Employee Records",    len(emp_raw),   "id-badge",    P["teal"]),md=3),
        dbc.Col(stat_card("Promotion Records",   len(promo_raw), "arrow-up",    P["gold"]),md=3),
        dbc.Col(stat_card("Reappointment Records",len(reapp_raw),"redo",        "#9B59B6"),md=3),
        dbc.Col(stat_card("Total Career Events", len(all_events),"calendar-alt","#3498DB"),md=3),
    ], className="mb-4"),
    dbc.Card([
        dbc.CardHeader("Data Quality Checks",style={"background":P["slate"],"color":P["text"]}),
        dbc.CardBody([dq_alert(lv,ti,de) for lv,ti,de in DQ]),
    ], style={"background":P["card"],"border":f"1px solid {P['border']}","marginBottom":"20px"}),
    dbc.Tabs([
        dbc.Tab(label="Employee Master",   children=[html.Div(raw_tbl(emp_raw,  "raw-emp"),  className="mt-3")]),
        dbc.Tab(label="Promotions",        children=[html.Div(raw_tbl(promo_raw,"raw-promo"),className="mt-3")]),
        dbc.Tab(label="Reappointments",    children=[html.Div(raw_tbl(reapp_raw,"raw-reapp"),className="mt-3")]),
        dbc.Tab(label="All Career Events", children=[html.Div(raw_tbl(
            all_events.assign(event_date=all_events["event_date"].dt.strftime("%d-%b-%Y"))
                      .sort_values(["ArfNo","event_date"]), "raw-events"), className="mt-3")]),
    ], className="mt-2"),
])

_tie_groups_df = (report[report["tied_group_size"] > 1]
                   .groupby(["highest_bps", "bps_date_fmt"])["ArfNo"]
                   .apply(list).reset_index()
                   .sort_values("highest_bps", ascending=False))
AUDIT_GROUP_OPTIONS = [
    {"label": f"BPS {r.highest_bps}  ·  {r.bps_date_fmt}  ·  {len(r.ArfNo)} employees tied",
     "value": f"{r.highest_bps}|{r.bps_date_fmt}"}
    for r in _tie_groups_df.itertuples()
]

tab_audit = dbc.Tab(label="🥊 Tie-Break Audit", tab_id="tab-audit", children=[
    dbc.Alert([
        html.I(className="fas fa-sitemap me-2", style={"color": P["teal"]}),
        html.Span("Pick a tied group below to see the ", style={"color": P["text"]}),
        html.B("full cascade", style={"color": P["teal"]}),
        html.Span(": every employee who started tied, and exactly which BPS grade — "
                  "compared only against the same grade — splits them apart, step by step.",
                  style={"color": P["text"]}),
    ], style={"background": "#1A2535", "border": f"1px solid {P['teal']}", "marginBottom": "12px"}),
    dbc.Row([
        dbc.Col([
            html.Label("Select a tied group (same Peak BPS + same Peak Date)",
                       style={"color": P["muted"], "fontSize": "0.8rem"}),
            dcc.Dropdown(id="audit-group-selector", options=AUDIT_GROUP_OPTIONS,
                         placeholder=f"{len(AUDIT_GROUP_OPTIONS)} tied group(s) found — choose one…",
                         style={"color": "#000"}),
        ], md=8),
        dbc.Col(html.Div(id="audit-group-badge", className="mt-4"), md=4),
    ], className="mb-3"),
    html.Div(id="audit-group-body"),
])

# ─────────────────────────────────────────────────────────────────────────────
# APP LAYOUT
# ─────────────────────────────────────────────────────────────────────────────
app = dash.Dash(__name__,
    external_stylesheets=[dbc.themes.CYBORG,
        "https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css"],
    title="Seniority Dashboard")
server = app.server

app.layout = html.Div([
    html.Div([
        html.Div([
            html.H4("🏛️  Government Employee Seniority Dashboard",
                    style={"color":P["text"],"fontWeight":"700","marginBottom":"2px"}),
            html.P(f"{len(report)} employees  ·  same-grade recursive tiebreaker  ·  Click any row for comparison popup",
                   style={"color":P["muted"],"fontSize":"0.85rem","margin":"0"}),
        ]),
        dbc.Button("↻ Refresh", id="refresh-btn", color="info", size="sm", outline=True,
                   style={"borderColor":P["teal"],"color":P["teal"]}),
    ], style={"background":P["navy"],"padding":"16px 24px","display":"flex",
              "justifyContent":"space-between","alignItems":"center",
              "borderBottom":f"2px solid {P['teal']}","marginBottom":"20px"}),
    dbc.Container([
        filter_bar,
        dbc.Tabs([tab_overview, tab_list, tab_employee,
                  tab_bps, tab_all_bps, tab_career, tab_audit, tab_dataset],
                 id="main-tabs", active_tab="tab-overview"),
    ], fluid=True),
    comparison_modal,
    dcc.Store(id="selected-arf"),
], style={"background":"#111B27","minHeight":"100vh","fontFamily":"'Segoe UI', sans-serif"})

# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def apply_filters(search, bps_vals, qual_vals, rank_range):
    df = report.copy()
    if search:
        s = search.strip().lower()
        df = df[df["Namee"].str.lower().str.contains(s)|df["ArfNo"].astype(str).str.contains(s)]
    if bps_vals:  df = df[df["highest_bps"].isin(bps_vals)]
    if qual_vals: df = df[df["qualification"].isin(qual_vals)]
    if rank_range:
        df = df[(df["seniority_rank"]>=rank_range[0])&(df["seniority_rank"]<=rank_range[1])]
    return df

# ─────────────────────────────────────────────────────────────────────────────
# CALLBACKS — Overview
# ─────────────────────────────────────────────────────────────────────────────
@app.callback(
    Output("chart-bps-dist","figure"), Output("chart-tier-pie","figure"),
    Output("chart-qual-bps","figure"), Output("chart-join-year","figure"),
    Input("search-input","value"), Input("bps-filter","value"),
    Input("qual-filter","value"),  Input("rank-slider","value"),
)
def update_overview(search, bps_vals, qual_vals, rank_range):
    df = apply_filters(search, bps_vals, qual_vals, rank_range)
    bc = df.groupby("highest_bps").size().reset_index(name="count")
    f1 = px.bar(bc, x="highest_bps", y="count", color="count",
                 color_continuous_scale="Teal",
                 labels={"highest_bps":"BPS Level","count":"Employees"})
    f1.update_layout(**CHART_BASE, coloraxis_showscale=False)
    f1.update_traces(marker_line_width=0)
    tc = df.groupby("tier_label").size().reset_index(name="count")
    f2 = px.pie(tc, names="tier_label", values="count", color="tier_label",
                 color_discrete_map=TIER_LABEL_COLORS, hole=0.45)
    f2.update_layout(**CHART_BASE)
    f2.update_traces(textfont_color="#fff")
    qc = df.groupby(["qualification","highest_bps"]).size().reset_index(name="count")
    f3 = px.bar(qc, x="qualification", y="count", color="highest_bps", barmode="stack",
                 color_continuous_scale="Viridis",
                 labels={"count":"Employees","highest_bps":"BPS"})
    f3.update_layout(**CHART_BASE, coloraxis_showscale=True)
    df2 = df.copy()
    df2["join_year"] = pd.to_datetime(emp_raw.set_index("ArfNo").reindex(df2["ArfNo"])["DateOfJoining"].values).year
    f4 = px.histogram(df2, x="join_year", nbins=20,
                       color_discrete_sequence=[P["teal"]], labels={"join_year":"Year"})
    f4.update_layout(**CHART_BASE)
    f4.update_traces(marker_line_width=0)
    return f1, f2, f3, f4

# ─────────────────────────────────────────────────────────────────────────────
# CALLBACKS — Seniority Table
# ─────────────────────────────────────────────────────────────────────────────
@app.callback(
    Output("seniority-table","data"), Output("seniority-table","tooltip_data"),
    Output("table-info","children"),
    Input("search-input","value"), Input("bps-filter","value"),
    Input("qual-filter","value"),  Input("rank-slider","value"),
)
def update_table(search, bps_vals, qual_vals, rank_range):
    df = apply_filters(search, bps_vals, qual_vals, rank_range)
    cols = ["seniority_rank","ArfNo","Namee","qualification","highest_bps",
            "bps_date_fmt","tier_label","entry_fmt","dob_fmt",
            "decision_basis","seniority_path_readable","career_path_display","tied_group_size"]
    recs = df[cols].to_dict("records")
    tips = [{
        "decision_basis":       {"value":r["decision_basis"],          "type":"text"},
        "career_path_display":  {"value":r["seniority_path_readable"], "type":"text"},
        "tier_label":           {"value":"Click this row for full comparison breakdown","type":"text"},
    } for r in recs]
    return recs, tips, f"Showing {len(df)} of {len(report)} employees  ·  Click any row to compare"

# ─────────────────────────────────────────────────────────────────────────────
# CALLBACKS — Row click → store ArfNo → open modal
# ─────────────────────────────────────────────────────────────────────────────
@app.callback(
    Output("selected-arf","data"),
    Input("seniority-table","selected_rows"),
    State("seniority-table","data"),
    prevent_initial_call=True,
)
def store_selected(sel_rows, data):
    if not sel_rows: return dash.no_update
    return data[sel_rows[0]]["ArfNo"]

@app.callback(
    Output("comparison-modal","is_open"),
    Output("modal-title","children"),
    Output("modal-body","children"),
    Input("selected-arf","data"),
    Input("modal-close","n_clicks"),
    State("comparison-modal","is_open"),
    prevent_initial_call=True,
)
def toggle_modal(arf, close_clicks, is_open):
    ctx = dash.callback_context.triggered[0]["prop_id"]
    if "modal-close" in ctx:
        return False, dash.no_update, dash.no_update
    if arf is None:
        return False, dash.no_update, dash.no_update
    row   = report[report["ArfNo"]==arf].iloc[0]
    title = f"Comparison: {row['Namee']}  (Rank #{row['seniority_rank']}  ·  ArfNo {arf})"
    body  = render_comparison_modal(arf)
    return True, title, body

# ─────────────────────────────────────────────────────────────────────────────
# CALLBACKS — Employee Profile
# ─────────────────────────────────────────────────────────────────────────────
@app.callback(
    Output("emp-selector","value"), Output("main-tabs","active_tab"),
    Input("seniority-table","selected_rows"), State("seniority-table","data"),
    prevent_initial_call=True,
)
def row_to_profile(sel, data):
    if not sel: return dash.no_update, dash.no_update
    return data[sel[0]]["ArfNo"], "tab-employee"

@app.callback(
    Output("emp-rank-badge","children"), Output("emp-profile-cards","children"),
    Output("emp-career-timeline","figure"), Output("emp-bps-gauge","figure"),
    Output("emp-decision-text","children"), Output("emp-comparison-body","children"),
    Input("emp-selector","value"),
)
def update_employee(arf):
    ef = go.Figure(); ef.update_layout(**CHART_BASE)
    if arf is None: return "", [], ef, ef, "Select an employee above.", ""
    row  = report[report["ArfNo"]==arf].iloc[0]
    evts = all_events[all_events["ArfNo"]==arf].sort_values("event_date")
    pct   = 1-(row["seniority_rank"]-1)/max(len(report)-1,1)
    bcol  = "#2ECC71" if pct>=0.75 else "#F39C12" if pct>=0.40 else "#E74C3C"
    badge = html.Div([
        html.Span(f"Rank #{row['seniority_rank']} of {len(report)}",
                  style={"fontSize":"1.6rem","fontWeight":"700","color":bcol}),
        html.Span(f"  ·  Top {100*(1-pct):.0f}%",
                  style={"color":P["muted"],"fontSize":"0.9rem"}),
    ])
    cards = dbc.Row([
        dbc.Col(stat_card("BPS",        row["highest_bps"],   "star",           P["gold"]),  md=3),
        dbc.Col(stat_card("BPS Date",   row["bps_date_fmt"],  "calendar-check", P["teal"]),  md=3),
        dbc.Col(stat_card("Govt Entry", row["entry_fmt"],     "door-open",      "#9B59B6"),  md=3),
        dbc.Col(stat_card("Qual",       row["qualification"], "graduation-cap", "#3498DB"),  md=3),
    ])
    src_c = {"Joining":"#3498DB","Reappointment":"#2ECC71","Promotion":"#F39C12"}
    fig_tl = go.Figure()
    for _,e in evts.iterrows():
        fig_tl.add_trace(go.Scatter(x=[e["event_date"]],y=[e["bps_level"]],mode="markers+text",
            marker=dict(size=18,color=src_c.get(e["source"],"grey"),line=dict(width=2,color="#fff")),
            text=[f"BPS {int(e['bps_level'])}"],textposition="top center",
            name=e["source"],showlegend=True,
            hovertemplate=f"<b>{e['source']}</b><br>BPS: {int(e['bps_level'])}<br>"
                          f"Date: {e['event_date'].strftime('%d-%b-%Y')}<extra></extra>"))
    if len(evts)>1:
        fig_tl.add_trace(go.Scatter(x=evts["event_date"],y=evts["bps_level"],mode="lines",
            line=dict(color=P["border"],dash="dot"),showlegend=False))
    fig_tl.update_layout(**CHART_BASE,title=f"Career Timeline — {row['Namee']}",
        xaxis_title="Date",yaxis_title="BPS Level",legend=dict(orientation="h",y=-0.2))
    mbps = int(report["highest_bps"].max())
    fig_g = go.Figure(go.Indicator(mode="gauge+number",value=int(row["highest_bps"]),
        domain={"x":[0,1],"y":[0,1]},
        title={"text":"Current BPS","font":{"color":P["text"]}},
        gauge={"axis":{"range":[1,mbps],"tickcolor":P["text"],"tickfont":{"color":P["text"]}},
               "bar":{"color":P["teal"]},"bgcolor":"rgba(0,0,0,0)","borderwidth":1,
               "bordercolor":P["border"],
               "steps":[{"range":[1,7],"color":"#1A2535"},{"range":[7,14],"color":"#1E2D40"},
                         {"range":[14,mbps],"color":"#223044"}],
               "threshold":{"line":{"color":P["gold"],"width":4},"thickness":0.75,
                             "value":row["highest_bps"]}},
        number={"font":{"color":P["text"]}}))
    fig_g.update_layout(**CHART_BASE,height=280)
    tc  = tier_color_for(row["decision_basis"])
    dec = html.Div([
        html.Div([html.Span("Tier: ",style={"color":P["muted"]}),
                  html.Span(row["tier_label"],style={"color":tc,"fontWeight":"700"})],className="mb-2"),
        html.Div([html.Span("Decision: ",style={"color":P["muted"]}),
                  html.Span(row["decision_basis"],style={"color":P["text"]})],className="mb-3"),
        html.Hr(style={"borderColor":P["border"]}),
        html.P("Full Career Path:",style={"color":P["muted"],"marginBottom":"6px"}),
        html.Div([dbc.Badge(s.strip(),color="info",className="me-2 mb-1",style={"fontSize":"0.8rem"})
                  for s in row["seniority_path_readable"].split("→")]),
    ])
    comp_body = render_comparison_modal(arf)
    return badge, cards, fig_tl, fig_g, dec, comp_body

# ─────────────────────────────────────────────────────────────────────────────
# CALLBACKS — BPS Deep Dive (hardened: NaT-safe, duplicate-name-safe, and the
# whole callback is wrapped so ANY unexpected error at ANY BPS level shows a
# friendly placeholder for that level instead of blanking the whole tab)
# ─────────────────────────────────────────────────────────────────────────────
@app.callback(
    Output("bps-group-bar","figure"),   Output("bps-group-timeline","figure"),
    Output("bps-summary-badges","children"), Output("tie-count-badge","children"),
    Output("tie-groups-panel","children"),   Output("bps-career-compare","figure"),
    Output("bps-group-table","children"),
    Input("bps-group-selector","value"),
)
def update_bps(bps_vals):
    try:
        return _update_bps_inner(bps_vals)
    except Exception as e:
        # Never let a bad selection or an edge case at any one level (odd
        # ArfNo format, an unexpected NaN, a lone employee with no peers,
        # etc.) blank the whole tab. Log it so it's visible in the console,
        # but always return something renderable.
        print(f"[BPS Deep Dive] selection {bps_vals} failed to render: {type(e).__name__}: {e}")
        return _empty_bps_response(
            f"Could not render this selection ({type(e).__name__}). See console for details.")

def _update_bps_inner(bps_vals):
    # dcc.Dropdown(multi=True) gives a list; normalize + dedupe + sort with
    # senior grades first so every downstream table/chart reads top-down by
    # seniority, matching the rest of the dashboard.
    if not bps_vals:
        return _empty_bps_response("Select at least one BPS level above to see its employees.")
    bps_vals = sorted({int(b) for b in bps_vals}, reverse=True)

    le = all_events[all_events["bps_level"].isin(bps_vals)].copy()
    # Drop rows with no usable event_date up front -- a NaT here is what
    # crashes every .dt.strftime()/groupby("event_date") call further down,
    # and null dates get more common the higher/sparser the BPS grade is.
    le = le[le["event_date"].notna()]
    if le.empty:
        return _empty_bps_response(
            f"No data for BPS level(s): {', '.join(str(b) for b in bps_vals)}")

    # One row per (employee, BPS level reached) -- this is what lets a
    # single employee who progressed through several selected grades show up
    # once per grade, i.e. "employees on every BPS" rather than just one
    # level at a time.
    le2 = le.groupby(["ArfNo", "bps_level"])["event_date"].min().reset_index(name="level_date")
    df  = le2.merge(report, on="ArfNo", how="left") \
             .sort_values(["bps_level", "seniority_rank"], ascending=[False, True]).copy()

    # Guard: a how="left" merge can silently introduce NaN for any ArfNo that
    # doesn't have a matching row in `report` at all (a true orphan). Only
    # "seniority_rank" is checked -- every legitimate employee in `report`
    # has one, so NaN there means the merge genuinely found nothing for that
    # ArfNo. Other fields like "qualification" are legitimately blank for
    # some real employees (a data-entry gap, not a merge failure), so they
    # must NOT be used for this check, or real employees get incorrectly
    # dropped and disappear from levels they actually belong to.
    missing_report_data = df["seniority_rank"].isna()
    if missing_report_data.any():
        print(f"[BPS Deep Dive] selection {bps_vals}: dropping {missing_report_data.sum()} "
              f"row(s) with no matching report data (true orphan ArfNo): "
              f"{df.loc[missing_report_data,'ArfNo'].tolist()}")
        df = df[~missing_report_data]
    if df.empty:
        return _empty_bps_response("No usable data for the selected BPS level(s) after cleaning.")

    df["qualification"] = df["qualification"].fillna("Not specified")
    df["ld_fmt"]  = df["level_date"].dt.strftime("%d-%b-%Y")
    df["is_peak"] = df["highest_bps"] == df["bps_level"]
    # Tie status is scoped to (bps_level, date) -- two people sharing a date
    # at BPS 14 are tied there; that says nothing about BPS 15.
    df["is_tied"] = df.groupby(["bps_level","ld_fmt"])["ArfNo"].transform("count") > 1

    evts = le.merge(df[["ArfNo","bps_level","seniority_rank"]], on=["ArfNo","bps_level"])
    evts["row_label"] = "BPS " + evts["bps_level"].astype(str) + " — " + evts["Namee"].astype(str)

    total_members = df["ArfNo"].nunique()
    pc = int(df["is_peak"].sum())
    tie_group_count = df[df["is_tied"]].groupby(["bps_level","ld_fmt"]).ngroups
    levels_txt = ", ".join(str(b) for b in bps_vals)
    badges = [
        dbc.Badge(f"{total_members} unique employees across {len(bps_vals)} BPS level(s)",
                  color="info", className="me-2 fs-6"),
        dbc.Badge(f"{len(df)} level-reach record(s)", color="secondary", className="me-2 fs-6"),
        dbc.Badge(f"{pc} peaked in selection",  color="primary",  className="me-2 fs-6"),
        dbc.Badge(f"{tie_group_count} tie group{'s'if tie_group_count!=1 else''}",
                  color="warning"if tie_group_count else"success",className="me-2 fs-6"),
    ]

    df["label"] = "BPS " + df["bps_level"].astype(str) + " — " + df["Namee"] + "  (#" + df["seniority_rank"].astype(str) + ")"
    label_order = df.sort_values(["bps_level","seniority_rank"], ascending=[False, True])["label"].tolist()

    # Tie status per (employee, level) row is a separate axis from "peaked
    # here" -- someone can be tied at a grade but go on to peak higher, or be
    # the sole peak with a unique date. Coloring both together makes tie
    # clusters visible directly in the bar, not only in the panel below.
    def _status_cat(r):
        if r["is_peak"] and r["is_tied"]:   return "Peak & Tied"
        if r["is_peak"]:                     return "Peak (unique date)"
        if r["is_tied"]:                      return "Tied (not peak)"
        return "Moved on (unique date)"
    df["status_cat"] = df.apply(_status_cat, axis=1)
    STATUS_COLORS = {
        "Peak & Tied":          "#E74C3C",  # red — the cases that actually needed tie-breaking at the peak
        "Peak (unique date)":   P["gold"],
        "Tied (not peak)":      "#3498DB",
        "Moved on (unique date)": P["teal"],
    }
    f_bar = px.bar(df, x="seniority_rank", y="label",
        orientation="h", color="status_cat",
        category_orders={"label": label_order, "status_cat": list(STATUS_COLORS.keys())},
        color_discrete_map=STATUS_COLORS,
        labels={"seniority_rank":"Overall Rank","label":"","status_cat":""},
        text="seniority_rank",
        hover_data={"decision_basis":True,"qualification":True,"ld_fmt":True,"bps_level":True})
    f_bar.update_layout(**CHART_BASE,
        title=f"Members across BPS {levels_txt}  ·  red = tied at peak",
        showlegend=True, legend=dict(orientation="h",y=-0.15), height=max(340,len(df)*26))
    f_bar.update_traces(textposition="outside",cliponaxis=False)

    f_tl = px.scatter(evts,x="event_date",y="row_label",color="source",symbol="source",
        color_discrete_map={"Joining":"#3498DB","Reappointment":"#2ECC71","Promotion":"#F39C12"},
        category_orders={"row_label": label_order},
        labels={"event_date":"Date","row_label":""},
        title=f"Dates Reached — BPS {levels_txt}",
        hover_data={"seniority_rank":True,"bps_level":True})
    for (bv,dt),cnt in evts.groupby(["bps_level","event_date"])["ArfNo"].nunique().items():
        if cnt>1:
            # add_vline() internally averages the two x-anchor points with
            # plain sum()/len() math, which pandas Timestamps no longer
            # support (raises "Addition/subtraction of integers ... is no
            # longer supported"). add_shape()+add_annotation() draw the same
            # dashed line + label without going through that buggy path.
            f_tl.add_shape(type="line", x0=dt, x1=dt, y0=0, y1=1, yref="paper",
                            line=dict(color=P["gold"], dash="dash"))
            f_tl.add_annotation(x=dt, y=1.02, yref="paper", showarrow=False,
                                 text=f"BPS{bv} TIE ({cnt})",
                                 font=dict(color=P["gold"], size=9))
    f_tl.update_layout(**CHART_BASE,height=max(320,len(label_order)*26),legend=dict(orientation="h",y=-0.15))

    # Tie Groups panel -- one section per selected BPS level (senior first),
    # each listing which employees share a date there and which have unique
    # dates. This is the multi-level analogue of the old single-level panel.
    panels = []
    any_ties = False
    for bv in bps_vals:
        sub = df[df["bps_level"]==bv]
        if sub.empty:
            continue
        tg = {d:g for d,g in sub.groupby("ld_fmt") if len(g)>1}
        sg = {d:g for d,g in sub.groupby("ld_fmt") if len(g)==1}
        panels.append(html.H6(
            f"BPS {bv}  ·  {len(sub)} member(s)  ·  {len(tg)} tie group(s)",
            style={"color":P["gold"],"fontWeight":"700","marginTop":"16px","marginBottom":"8px"}))
        if not tg:
            panels.append(html.Div([html.I(className="fas fa-check-circle me-2",style={"color":"#2ECC71"}),
                html.Span(f"All unique dates at BPS {bv}.",style={"color":P["text"]})],className="p-2 mb-2"))
        else:
            any_ties = True
            for ds,grp in sorted(tg.items()):
                grp = grp.sort_values("seniority_rank")
                rows=[html.Tr([
                    html.Td(f"#{r.seniority_rank}",style={"color":P["teal"],"fontWeight":"700","width":"60px"}),
                    html.Td(r.Namee,style={"color":P["text"],"fontWeight":"600"}),
                    html.Td(r.qualification,style={"color":P["muted"],"fontSize":"0.8rem"}),
                    html.Td(f"BPS {r.highest_bps}"+(" (peak)"if r.is_peak else""),
                            style={"color":P["gold"]if r.is_peak else P["muted"],"fontSize":"0.8rem"}),
                    html.Td(dbc.Badge(r.tier_label,
                                      style={"backgroundColor":TIER_LABEL_COLORS.get(r.tier_label,"grey"),
                                             "fontSize":"0.72rem"})),
                    html.Td(r.decision_basis,style={"color":P["muted"],"fontSize":"0.76rem"}),
                ],style={"borderBottom":f"1px solid {P['border']}"}) for r in grp.itertuples()]
                panels.append(dbc.Card([
                    dbc.CardHeader([
                        html.I(className="fas fa-link me-2",style={"color":P["gold"]}),
                        html.Span(f"Reached BPS {bv} on {ds}  ·  {len(grp)} employees tied",
                                  style={"color":P["gold"],"fontWeight":"700"}),
                    ],style={"background":"#1A2535"}),
                    dbc.CardBody(html.Table([
                        html.Thead(html.Tr([html.Th(h,style={"color":P["teal"]}) for h in
                            ["Rank","Name","Qual","Eventual Peak","Tier","Decision"]],
                            style={"borderBottom":f"2px solid {P['teal']}"})),
                        html.Tbody(rows),
                    ],style={"width":"100%","borderCollapse":"collapse","fontSize":"0.83rem"})),
                ],style={"background":P["card"],"border":f"1px solid {P['gold']}","marginBottom":"12px"}))
    if not panels:
        panels = [html.Div("No records for the selected BPS level(s).",
                            style={"color":P["muted"],"padding":"12px","fontStyle":"italic"})]
    tb = dbc.Badge(f"{tie_group_count} tie group{'s'if tie_group_count!=1 else''} total",
                   color="warning"if any_ties else"success",className="fs-6")

    # Career heatmap — keyed by "Name (ArfNo)" as the row key, not raw
    # Namee, since two employees can share a name; once that happens
    # set_index("Namee")/groupby("Namee") silently collide (one employee's
    # rows overwrite the other's), which tends to only show up once a grade
    # has enough people in it -- i.e. more often at crowded senior grades.
    unique_arfs = df.drop_duplicates("ArfNo")
    ae2 = all_events[all_events["ArfNo"].isin(unique_arfs["ArfNo"])].copy()
    ae2 = ae2[ae2["event_date"].notna()]
    label_series = (unique_arfs["Namee"].astype(str) + " (" + unique_arfs["ArfNo"].astype(str) + ")").reset_index(drop=True)
    label_map = dict(zip(unique_arfs["ArfNo"].reset_index(drop=True), label_series))
    ae2["hm_label"] = ae2["ArfNo"].map(label_map)
    rm = unique_arfs.set_index("ArfNo")["seniority_rank"].to_dict()
    # Map hm_label -> seniority_rank directly from the ArfNo we already have,
    # instead of re-parsing the ArfNo back out of the "Name (ArfNo)" string.
    # That parse only works when ArfNo is purely numeric -- it would throw
    # the moment an ArfNo uses any non-numeric format (letters, dashes, a
    # prefix like an "SPS-" senior/management series code, etc), and that
    # kind of format tends to show up specifically among senior-grade
    # employees, which is exactly why this used to go blank for BPS 11+
    # while junior grades rendered fine.
    rank_by_label = {label_map[arf]: rank for arf, rank in rm.items() if arf in label_map}
    if ae2.empty or ae2["hm_label"].nunique() < 1:
        f_cm = go.Figure(); f_cm.update_layout(**CHART_BASE, height=320,
            annotations=[dict(text="Not enough career history to chart here.",
                               showarrow=False, font=dict(color=P["muted"], size=13))])
    else:
        try:
            pm = ae2.groupby(["hm_label","bps_level"])["event_date"].min().reset_index()
            pm["year"] = pm["event_date"].dt.year
            pm["nr"]   = pm["hm_label"].map(lambda lbl: rank_by_label.get(lbl, 0))
            pm = pm.sort_values("nr")
            f_cm = px.density_heatmap(pm, x="bps_level", y="hm_label", z="year",
                color_continuous_scale="Teal", histfunc="avg",
                labels={"bps_level":"BPS Level","hm_label":"","year":"Year"},
                title=f"Full Career History — everyone in this selection",
                category_orders={"hm_label": pm["hm_label"].drop_duplicates().tolist()})
            f_cm.update_layout(**CHART_BASE, height=max(320, len(unique_arfs)*30))
            f_cm.update_xaxes(tickmode="linear", dtick=1, gridcolor=P["border"])
        except Exception as e:
            print(f"[BPS Deep Dive] Career heatmap failed for selection {bps_vals}: {type(e).__name__}: {e}")
            f_cm = go.Figure(); f_cm.update_layout(**CHART_BASE, height=320,
                annotations=[dict(text="Could not render career heatmap for this selection.",
                                   showarrow=False, font=dict(color=P["muted"], size=13))])

    tbl_df = df[["bps_level","seniority_rank","ArfNo","Namee","qualification","ld_fmt","is_peak",
                 "highest_bps","bps_date_fmt","entry_fmt","dob_fmt",
                 "tier_label","decision_basis","seniority_path_readable"]].copy()
    tbl_df["is_peak"] = tbl_df["is_peak"].map({True:"Yes (peak)",False:"No — moved on"})
    tbl = dash_table.DataTable(
        data=tbl_df.to_dict("records"),
        columns=[
            {"name":"BPS Level","id":"bps_level"},
            {"name":"Rank","id":"seniority_rank"}, {"name":"ArfNo","id":"ArfNo"},
            {"name":"Name","id":"Namee"},           {"name":"Qual","id":"qualification"},
            {"name":"Date @ Level","id":"ld_fmt"},
            {"name":"Peaked?","id":"is_peak"},      {"name":"Peak BPS","id":"highest_bps"},
            {"name":"Peak Date","id":"bps_date_fmt"},{"name":"Entry","id":"entry_fmt"},
            {"name":"DOB","id":"dob_fmt"},          {"name":"Tier","id":"tier_label"},
            {"name":"Decision","id":"decision_basis"},
            {"name":"Career Path","id":"seniority_path_readable"},
        ],
        sort_action="native",filter_action="native",page_size=20,style_table={"overflowX":"auto"},
        style_header={"backgroundColor":P["slate"],"color":P["teal"],"fontWeight":"700",
                      "border":f"1px solid {P['border']}","fontSize":"0.75rem","textTransform":"uppercase"},
        style_cell={"backgroundColor":P["even"],"color":P["text"],"border":f"1px solid {P['border']}",
                    "fontSize":"0.82rem","padding":"7px 10px",
                    "maxWidth":"220px","overflow":"hidden","textOverflow":"ellipsis","whiteSpace":"nowrap"},
        style_data_conditional=[
            {"if":{"row_index":"odd"},"backgroundColor":P["odd"]},
            {"if":{"column_id":"bps_level"},     "color":P["gold"],"fontWeight":"700","textAlign":"center"},
            {"if":{"column_id":"seniority_rank"},"color":P["teal"],"fontWeight":"700","textAlign":"center"},
            {"if":{"column_id":"highest_bps"},   "color":P["gold"],"fontWeight":"700","textAlign":"center"},
            *[{"if":{"filter_query":f'{{tier_label}} = "{t}"',"column_id":"tier_label"},
               "color":c,"fontWeight":"600"} for t,c in TIER_LABEL_COLORS.items()],
        ],
        tooltip_data=[{"decision_basis":{"value":r["decision_basis"],"type":"text"},
                       "seniority_path_readable":{"value":r["seniority_path_readable"],"type":"text"}}
                      for r in tbl_df.to_dict("records")],
        tooltip_delay=0, tooltip_duration=None,
    )
    return f_bar, f_tl, badges, tb, panels, f_cm, tbl

# ─────────────────────────────────────────────────────────────────────────────
# CALLBACKS — BPS Deep Dive quick Select All / Clear buttons
# ─────────────────────────────────────────────────────────────────────────────
@app.callback(
    Output("bps-group-selector","value"),
    Input("bps-select-all-btn","n_clicks"), Input("bps-clear-btn","n_clicks"),
    prevent_initial_call=True,
)
def bps_select_all_or_clear(select_clicks, clear_clicks):
    ctx = dash.callback_context.triggered[0]["prop_id"]
    if "bps-select-all-btn" in ctx:
        return BPS_WITH_DATA
    if "bps-clear-btn" in ctx:
        return []
    return dash.no_update

# ─────────────────────────────────────────────────────────────────────────────
# CALLBACKS — Career Paths
# ─────────────────────────────────────────────────────────────────────────────
@app.callback(
    Output("career-sankey","figure"), Output("career-scatter","figure"),
    Input("search-input","value"), Input("bps-filter","value"),
    Input("qual-filter","value"),  Input("rank-slider","value"),
)
def update_career(search, bps_vals, qual_vals, rank_range):
    df  = apply_filters(search, bps_vals, qual_vals, rank_range)
    ev  = all_events[all_events["ArfNo"].isin(df["ArfNo"])]
    jb  = ev[ev["source"]=="Joining"].groupby("ArfNo")["bps_level"].first().reset_index(name="join_bps")
    mg  = df[["ArfNo","highest_bps"]].merge(jb,on="ArfNo",how="left")
    fl  = mg.groupby(["join_bps","highest_bps"]).size().reset_index(name="count")
    fl  = fl[fl["join_bps"].notna()&fl["highest_bps"].notna()]
    an  = sorted(set(fl["join_bps"].tolist()+fl["highest_bps"].tolist()))
    ni  = {v:i for i,v in enumerate(an)}
    fs  = go.Figure(go.Sankey(
        node=dict(label=[f"BPS {int(n)}" for n in an],color=[P["teal"]]*len(an),pad=20,thickness=18),
        link=dict(source=[ni[r["join_bps"]] for _,r in fl.iterrows()],
                  target=[ni[r["highest_bps"]] for _,r in fl.iterrows()],
                  value=fl["count"].tolist(),color="rgba(26,188,156,0.25)")))
    fs.update_layout(**CHART_BASE,title="Career Flow: Joining BPS → Peak BPS",height=420)
    sc = df.copy(); sc["entry_year"]=pd.to_datetime(sc["dateofentryingov"]).dt.year
    fsc=px.scatter(sc,x="entry_year",y="highest_bps",color="qualification",size_max=12,
        hover_data={"Namee":True,"seniority_rank":True,"decision_basis":True},
        labels={"entry_year":"Govt Entry Year","highest_bps":"Peak BPS"},
        title="Govt Entry Year vs Peak BPS Level")
    fsc.update_layout(**CHART_BASE,height=380)
    return fs,fsc

@app.callback(
    Output("career-milestone-table","children"), Output("career-first-to-reach","children"),
    Input("search-input","value"), Input("bps-filter","value"),
    Input("qual-filter","value"),  Input("rank-slider","value"),
)
def update_career_tables(search, bps_vals, qual_vals, rank_range):
    df  = apply_filters(search, bps_vals, qual_vals, rank_range)
    ev  = all_events[all_events["ArfNo"].isin(df["ArfNo"])].copy()
    ev  = ev.merge(df[["ArfNo","seniority_rank"]],on="ArfNo").sort_values(["seniority_rank","event_date"])
    ev["ed_fmt"] = ev["event_date"].dt.strftime("%d-%b-%Y")
    sc  = {"Joining":"#3498DB","Reappointment":"#2ECC71","Promotion":"#F39C12"}
    mr  = [html.Tr([
        html.Td(f"#{r['seniority_rank']}",style={"color":P["teal"],"fontWeight":"700"}),
        html.Td(r["Namee"],style={"color":P["text"]}),
        html.Td(dbc.Badge(r["source"],style={"backgroundColor":sc.get(r["source"],"grey"),"fontSize":"0.72rem"})),
        html.Td(f"BPS {int(r['bps_level'])}",style={"color":P["gold"],"fontWeight":"700"}),
        html.Td(r["ed_fmt"],style={"color":P["text"]}),
    ],style={"borderBottom":f"1px solid {P['border']}"}) for _,r in ev.iterrows()]
    mt  = html.Table([
        html.Thead(html.Tr([html.Th(h,style={"color":P["teal"]}) for h in
            ["Rank","Name","Event","BPS","Date"]],style={"borderBottom":f"2px solid {P['teal']}"})),
        html.Tbody(mr),
    ],style={"width":"100%","borderCollapse":"collapse","fontSize":"0.85rem"})
    bf  = ev.groupby("bps_level").apply(lambda g: g.loc[g["event_date"].idxmin()],include_groups=False).reset_index()
    fr  = [html.Tr([
        html.Td(f"BPS {int(r['bps_level'])}",style={"color":P["gold"],"fontWeight":"700"}),
        html.Td(r["Namee"],style={"color":P["text"],"fontWeight":"600"}),
        html.Td(f"#{int(r['seniority_rank'])}",style={"color":P["teal"]}),
        html.Td(r["event_date"].strftime("%d-%b-%Y"),style={"color":P["text"]}),
        html.Td(dbc.Badge(r["source"],style={"backgroundColor":sc.get(r["source"],"grey"),"fontSize":"0.72rem"})),
    ],style={"borderBottom":f"1px solid {P['border']}"}) for _,r in bf.sort_values("bps_level",ascending=False).iterrows()]
    ft  = html.Table([
        html.Thead(html.Tr([html.Th(h,style={"color":P["teal"]}) for h in
            ["BPS","Earliest Achiever","Rank","Date","Via"]],style={"borderBottom":f"2px solid {P['teal']}"})),
        html.Tbody(fr),
    ],style={"width":"100%","borderCollapse":"collapse","fontSize":"0.85rem"})
    return mt, ft

# ─────────────────────────────────────────────────────────────────────────────
# CALLBACKS — Tie-Break Audit
# ─────────────────────────────────────────────────────────────────────────────
@app.callback(
    Output("audit-group-badge", "children"),
    Output("audit-group-body", "children"),
    Input("audit-group-selector", "value"),
)
def update_audit(group_key):
    if not group_key:
        return "", html.Div("Select a tied group above to see the full comparison cascade.",
                             style={"color": P["muted"], "padding": "12px", "fontStyle": "italic"})
    bps_str, date_str = group_key.split("|", 1)
    bps = int(bps_str)
    arfs = report[(report["highest_bps"] == bps) & (report["bps_date_fmt"] == date_str)]["ArfNo"].tolist()
    badge = dbc.Badge(f"{len(arfs)} employees start tied at BPS {bps}  ·  {date_str}",
                       color="warning", className="fs-6")
    cascade = build_group_cascade(arfs)
    body = render_cascade(cascade)
    return badge, body

# ─────────────────────────────────────────────────────────────────────────────
if __name__=="__main__":
    print("\n"+"="*60)
    print("  Seniority Dashboard — http://127.0.0.1:8050")
    print("="*60+"\n")
    app.run(debug=True, port=8050)

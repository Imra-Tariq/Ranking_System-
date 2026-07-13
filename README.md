# Ranking_System-
# Government Employee Seniority Ranking Dashboard

An interactive **Dash / Plotly** web application that computes and visualizes
employee seniority rankings from MySQL-backed HR data, using a recursive
same-grade tiebreaker algorithm with a full audit trail for every ranking
decision.

---

## Table of Contents

- [Overview](#overview)
- [Tech Stack](#tech-stack)
- [Core Logic](#core-logic)
  - [1. Seniority Resolution Engine](#1-seniority-resolution-engine)
  - [2. Messy Date Parsing](#2-messy-date-parsing)
  - [3. Data Quality Checks](#3-data-quality-checks)
  - [4. Tie-Break Audit / Cascade Builder](#4-tie-break-audit--cascade-builder)
  - [5. Display vs. Decision Logic Separation](#5-display-vs-decision-logic-separation)
- [Application Structure](#application-structure)
- [Dashboard Tabs](#dashboard-tabs)
- [Setup](#setup)
- [Environment Variables](#environment-variables)
- [Running the App](#running-the-app)
- [Known Fixes / Design Notes](#known-fixes--design-notes)

---

## Overview

The dashboard reads employee, promotion, and reappointment records from a
MySQL database, reconstructs each employee's full BPS (grade) career
history, and ranks all employees by seniority using a peak-grade-first,
same-grade-only tiebreaking rule. Every ranking decision is fully
explainable — the app builds and exposes the exact step-by-step comparison
trail behind each rank via interactive modals and audit views.

## Tech Stack

| Layer | Tools |
|---|---|
| Data wrangling | **pandas** |
| Database | **MySQL**, **SQLAlchemy** (`create_engine`, `URL.create()`), **PyMySQL** driver |
| Config / secrets | **python-dotenv** (`.env` file, never committed) |
| Web framework | **Dash** (`dash`, `dcc`, `html`, `dash_table`, callbacks) |
| UI components | **dash-bootstrap-components** (Cards, Tabs, Modals, Alerts, Badges, Accordion, Dropdowns, RangeSlider) — CYBORG theme |
| Charts | **Plotly Express** (bar, pie, histogram, scatter, density heatmap) and **Plotly Graph Objects** (Sankey diagram, gauge/indicator, custom timelines) |
| Icons | Font Awesome (via CDN) |

## Core Logic

### 1. Seniority Resolution Engine

`resolve_seniority_order()` implements the primary ranking algorithm:

1. Employees are sorted and grouped by **peak BPS grade reached** and the
   **date they reached it** (`highest_bps`, `highest_bps_date`).
2. Ties within a group are broken by walking *downward* through lower BPS
   grades (from `ceiling - 1` to `1`), comparing achievement dates **only
   among employees still tied** at each step — comparisons never cross
   grades.
3. If grade-level comparisons are exhausted without a unique resolution,
   the algorithm falls through a **fallback chain**:
   `Government Entry Date` → `Date of Birth` → `ArfNo` (lexicographically
   smallest, guaranteed unique).
4. The engine is implemented with mutual recursion (`resolve()` ⇄
   `resolve_fallback()`) that, alongside the final `decision_basis` string,
   builds a full **audit trail** (ordered list of comparison steps) per
   employee. This trail powers the comparison modal UI.

**Performance:** `LEVEL_DATE` is a precomputed
`{(ArfNo, bps_level): date}` dictionary, giving O(1) lookups during
recursion instead of repeatedly filtering the DataFrame — keeps the
recursive tiebreak fast across ~22 grades even on larger datasets.

### 2. Messy Date Parsing

`parse_messy_date()` handles inconsistently formatted VARCHAR date columns:

- Tries a sequence of candidate formats (`%d-%b-%Y`, `%d/%m/%Y`, `%Y-%m-%d`,
  etc.) against the column.
- Anything left unparsed is handed to pandas' flexible parser
  (`dayfirst=True`) as a last resort.
- Logs exactly how many values (and sample examples) failed to parse,
  instead of silently turning a mis-formatted column into all-`NaT`.

### 3. Data Quality Checks

`run_dq()` surfaces issues as color-coded alerts (error / warning / info /
success):

- Missing Date of Birth, Government Entry Date, or Qualification
- Orphan `ArfNo`s in the promotion/reappointment tables not present in the
  employee master
- Duplicate promotion entries
- BPS regressions (grade decreased over time — data integrity flag)
- Employees with no recorded progression (joining event only)

### 4. Tie-Break Audit / Cascade Builder

`build_group_cascade()` and `render_cascade()` mirror the main engine's
logic but operate on an entire tied cluster at once, recording *every*
split at *every* grade. This powers the **Tie-Break Audit** tab, letting a
user pick any tied group and see the full step-by-step cascade
independent of any single employee's individual trail.

### 5. Display vs. Decision Logic Separation

`career_path_display` and `seniority_path_readable` are human-readable
"BPS-X → BPS-Y" strings built purely for the UI. They are kept
intentionally separate from the fields that actually drive ranking
(`highest_bps`, `highest_bps_date`, the `LEVEL_DATE` lookup table) so
display formatting can never accidentally influence ranking decisions.

## Application Structure

> Originally a single ~1,300-line script; refactored into a modular
> package.

```
├── engine/       # seniority resolution + tiebreak algorithm
├── data/         # DB access, date parsing, data quality checks
├── components/   # reusable UI building blocks (cards, modals, tables)
├── tabs/         # one module per dashboard tab
├── callbacks/    # Dash callback wiring
├── app.py        # entry point
├── requirements.txt
├── .env.example
└── README.md
```

## Dashboard Tabs

| Tab | Purpose |
|---|---|
| 📊 Overview | Headline stats, BPS distribution, tiebreak-step distribution, qualification breakdown |
| 📋 Seniority List | Full ranked, filterable, sortable table — click any row for a comparison modal |
| 🔍 Employee Profile | Per-employee career timeline, BPS gauge, and full decision breakdown |
| 📈 BPS Deep Dive | Ranked members and tie groups filtered to selected BPS level(s) |
| 🗂️ All BPS Levels | Accordion view of every grade at once |
| 🗺️ Career Paths | Sankey flow (joining grade → peak grade), entry-year vs. peak-grade scatter |
| 🥊 Tie-Break Audit | Pick a tied group and see the full comparison cascade, step by step |
| 🧬 Dataset Explorer | Raw table browser + data quality report |

## Setup

```bash
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env           # then fill in your MySQL credentials
```

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `DB_USER` | `root` | MySQL username |
| `DB_PASSWORD` | — | MySQL password (required) |
| `DB_HOST` | `127.0.0.1` | MySQL host |
| `DB_PORT` | `3306` | MySQL port |
| `DB_SCHEMA` | `pac_erp_care` | Database/schema name |
| `DASH_DEBUG` | `true` | Enable Dash debug mode |
| `DASH_PORT` | `8050` | Port the app runs on |

> `.env` is never committed — only `.env.example` should be tracked.

## Running the App

```bash
python app.py
```

Then open **http://127.0.0.1:8050**.

## Known Fixes / Design Notes

- **MySQL URL parsing with special-character passwords** — building the
  connection string with an f-string broke when the password contained
  `@`, `:`, `/`, or `%` (misread as part of the hostname). Fixed by
  constructing the URL with SQLAlchemy's `URL.create()`, which escapes
  special characters automatically.
- **`pd.Timestamp` arithmetic in Plotly's `add_vline()`** — worked around
  by using `add_shape()` + `add_annotation()` instead.
- **Orphan `ArfNo` merges** — promotion/reappointment records referencing
  an `ArfNo` not present in the employee master could produce invalid
  JSON in callback outputs; these rows are now explicitly detected,
  logged, and dropped before rendering.
- **`int()` cast bug** in the BPS Deep Dive tab that affected senior-grade
  employees — resolved by correcting the type coercion on the BPS level
  column before comparison.

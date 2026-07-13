# Ranking_System-
A single-file Python Dash web app that ranks  employees by seniority and, crucially, shows exactly why each person got the rank they did — including a full breakdown when two employees are tied.


The core problem it solves
Seniority isn't just "who joined first." When two employees are dead-even on paper, you need a fair, auditable tiebreaker chain. This app implements a 6-tier cascading tiebreaker:

Peak BPS (highest pay/grade level ever reached)
Date that peak BPS was achieved
Full career path (every BPS level + date, not just the peak)
Government entry date
Date of birth
ArfNo (employee ID — absolute last-resort fallback, guarantees no two people can ever truly tie)

If tier 1 doesn't separate two people, it falls to tier 2, then 3, and so on until someone wins.
The engine (build_seniority_report)

Reads three CSVs: employee master data, promotions, reappointments
Merges every BPS-changing event (joining, promotion, reappointment) into one timeline per employee
Builds a sortable "seniority path" string per employee by encoding BPS level + days-until-a-far-future-date, so a simple string sort produces the correct rank order
Walks each employee through the 6 tiers to generate a human-readable decision_basis (e.g. "Broke at L1 BPS 17 date (04-Mar-2010)")
Also pre-builds a structured JSON blob per employee (comparison_json) listing, tier by tier, exactly who else was in the running and why they got filtered out — this powers the UI

Data quality checks (run_dq)
Before trusting the rankings, it flags: missing DOB/entry dates, orphaned promotion/reappointment records (referencing an ArfNo not in the master list), duplicate promotion entries, and any BPS "regression" (someone's grade going down, which shouldn't happen).

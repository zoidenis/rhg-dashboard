"""Connection and readiness check for the Finance Control Tower.

Run:  check_connection.bat      (or: python check_bc.py)

It confirms the login, lists the companies, and tests each source the
application relies on, so a problem is visible before the dashboard opens.
"""
from datetime import date, timedelta

import config
from bc_client import BCClient, BCError


def line(label, ok, detail=""):
    mark = "OK  " if ok else "FAIL"
    print(f"[{mark}] {label}" + (f" - {detail}" if detail else ""))


def main():
    print(f"Business Central: {config.BC_BASE_URL}")
    print(f"User: '{config.BC_USERNAME}'  mode: {config.BC_AUTH_MODE}  company: {config.BC_COMPANY}\n")
    c = BCClient()
    today = date.today()
    first = today.replace(day=1)

    try:
        companies = c.companies()
        line("Login and company list", True, ", ".join(companies))
    except BCError as exc:
        line("Login and company list", False, str(exc))
        print("\nNothing else can be tested until the login works. Check BC_USERNAME, BC_PASSWORD "
              "and BC_AUTH_MODE in .env.")
        input("\nPress Enter to close...")
        return

    company = config.BC_COMPANY
    checks = [
        ("Chart of accounts with period figures",
         lambda: c.accounts(company, first.isoformat(), today.isoformat(), None, 0),
         lambda rows: f"{len(rows)} accounts, "
                      f"{sum(1 for r in rows if r.get('Income_Balance') == 'Income Statement')} in the P&L"),
        ("Open supplier entries (payables)",
         lambda: c.open_vendor_entries(company, 0), lambda rows: f"{len(rows)} open entries"),
        ("Open customer entries (receivables)",
         lambda: c.open_customer_entries(company, 0), lambda rows: f"{len(rows)} open entries"),
        ("Bank ledger entries this month",
         lambda: c.bank_entries(company, first.isoformat(), today.isoformat(), 0),
         lambda rows: f"{len(rows)} entries"),
        ("Material general ledger entries",
         lambda: c.material_gl_entries(company, first.isoformat(), today.isoformat(), 100000, 50, 0),
         lambda rows: f"{len(rows)} entries at or above 100,000"),
        ("Budget entries",
         lambda: c.budget(company, first.isoformat(), today.isoformat(), "", 0),
         lambda rows: f"{len(rows)} rows"
                      + (", budgets: " + ", ".join(sorted({r.get('Budget_Name') for r in rows if r.get('Budget_Name')}))
                         if rows else " (no budget for this period)")),
        ("Department dimension values",
         lambda: c.dimension_values(company), lambda rows: f"{len(rows)} values"),
    ]

    for label, call, describe in checks:
        try:
            rows = call()
            line(label, True, describe(rows))
        except BCError as exc:
            line(label, False, str(exc)[:200])
        except Exception as exc:  # noqa: BLE001
            line(label, False, f"{type(exc).__name__}: {exc}"[:200])

    print("\nOptional sources (the application shows 'data unavailable' where these are missing):")
    for label, entity in [("Financial report KPIs (pbfinance)", config.E_FINREPORT)]:
        try:
            rows = c.financial_report_kpis(company, 0)
            line(label, True, f"{len(rows)} rows")
        except BCError as exc:
            line(label, False, str(exc)[:160])

    print("\nIf every required line says OK, start the application with start.bat "
          f"and open http://127.0.0.1:{config.APP_PORT}")
    input("\nPress Enter to close...")


if __name__ == "__main__":
    main()

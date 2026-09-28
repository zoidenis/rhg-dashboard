"""Quick connection test: python check_bc.py"""
from datetime import date, timedelta

import config
from bc_client import BCClient, BCError

print(f"Connecting to {config.BC_BASE_URL} as '{config.BC_USERNAME}' ({config.BC_AUTH_MODE})...")
c = BCClient()
try:
    print("Companies:", ", ".join(c.list_companies()))
    today = date.today()
    rows = c.purchase_entries(config.BC_COMPANY, (today - timedelta(days=2)).isoformat(), today.isoformat())
    print(f"OK - {len(rows)} purchase entries for {config.BC_COMPANY} in the last 3 days.")
    if rows:
        r = rows[-1]
        print("Latest:", r.get("Posting_Date"), r.get("Item_Description"), r.get("Location_Code"), r.get("Cost_Amount_Actual"))
except BCError as e:
    print("FAILED:", e)
input("\nPress Enter to close...")

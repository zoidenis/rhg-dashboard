"""Read-only Business Central access for the finance application.

The general ledger holds tens of thousands of entries a day, so nothing here
scans it. Period figures come from the Chart of Accounts flow fields, which BC
calculates server side when a date range (and optionally a dimension) is passed
as a filter. Ledger entries are read only where the volume is manageable:
open vendor and customer entries, bank entries of the period, and material
journal entries above the configured threshold.
"""
import re
import threading
import time
from urllib.parse import quote

import requests

import config
import localdb

ACCOUNT_FIELDS = ["No", "Name", "Income_Balance", "Account_Category", "Account_Subcategory_Descript",
                  "Account_Type", "Direct_Posting", "Net_Change", "Balance_at_Date", "Debit_Amount",
                  "Credit_Amount"]
VENDOR_FIELDS = ["Entry_No", "Vendor_No", "Vendor_Name", "Posting_Date", "Due_Date", "Document_Date",
                 "Document_Type", "Document_No", "Currency_Code", "Amount_LCY", "Remaining_Amt_LCY",
                 "Original_Amt_LCY", "Open", "Source_Code", "Dimension_Set_ID"]
CUSTOMER_FIELDS = ["Entry_No", "Customer_No", "Customer_Name", "Posting_Date", "Due_Date", "Document_Date",
                   "Document_Type", "Document_No", "Currency_Code", "Amount_LCY", "Remaining_Amt_LCY",
                   "Original_Amt_LCY", "Open", "Source_Code", "Dimension_Set_ID"]
BANK_FIELDS = ["Entry_No", "Bank_Account_No", "Bank_Account_Name", "Posting_Date", "Document_Type",
               "Document_No", "Currency_Code", "Amount", "Amount_LCY", "Remaining_Amount", "Open",
               "Source_Code"]
GL_FIELDS = ["Entry_No", "G_L_Account_No", "G_L_Account_Name", "Posting_Date", "Document_Date",
             "Document_Type", "Document_No", "Source_Code", "Reason_Code", "Amount", "Debit_Amount",
             "Credit_Amount", "Dimension_Set_ID", "Business_Unit_Code"]


class BCError(Exception):
    pass


def _session():
    s = requests.Session()
    if config.BC_AUTH_MODE == "ntlm":
        try:
            from requests_ntlm import HttpNtlmAuth
        except ImportError as exc:
            raise BCError("BC_AUTH_MODE=ntlm needs the requests_ntlm package.") from exc
        s.auth = HttpNtlmAuth(config.BC_USERNAME, config.BC_PASSWORD)
    elif config.BC_USERNAME:
        s.auth = (config.BC_USERNAME, config.BC_PASSWORD)
    s.headers["Accept"] = "application/json"
    s.verify = config.BC_VERIFY_SSL
    return s


class BCClient:
    def __init__(self):
        self.session = _session()
        self._cache = {}
        self._lock = threading.Lock()

    # ---- plumbing ----
    def _url(self, company, entity):
        return f"{config.BC_BASE_URL}/Company('{quote(company, safe='')}')/{entity}"

    MISSING_FIELD = re.compile(r"Could not find a property named '([^']+)'")

    def _get_all(self, url, params=None, limit=None, _dropped=None):
        """Reads every page. If a company publishes a page without one of the selected
        columns, BC answers 400 naming that column; the field is dropped and the call is
        retried, so one missing column never takes the whole screen down."""
        rows, next_url, next_params = [], url, params
        while next_url:
            try:
                resp = self.session.get(next_url, params=next_params, timeout=config.BC_TIMEOUT)
            except requests.RequestException as exc:
                raise BCError(f"Could not reach Business Central at {config.BC_BASE_URL}: {exc}") from exc
            if resp.status_code == 401:
                raise BCError("Business Central rejected the login (HTTP 401). Check the credentials in .env.")
            if resp.status_code == 400 and params and params.get("$select"):
                missing = self.MISSING_FIELD.search(resp.text or "")
                if missing:
                    field = missing.group(1)
                    kept = [f for f in params["$select"].split(",") if f != field]
                    if kept and len(kept) < len(params["$select"].split(",")):
                        retry = dict(params, **{"$select": ",".join(kept)})
                        dropped = (_dropped or []) + [field]
                        print(f"Business Central does not publish '{field}' on this page; continuing without it.")
                        return self._get_all(url, retry, limit, dropped)
            if resp.status_code >= 400:
                raise BCError(f"Business Central returned HTTP {resp.status_code}: {resp.text[:300]}")
            data = resp.json()
            rows.extend(data.get("value", []))
            if limit and len(rows) >= limit:
                return rows[:limit]
            next_url, next_params = data.get("@odata.nextLink"), None
        return rows

    def _cached_immutable(self, kind, company, date_from, date_to, producer):
        """For entries that cannot change once posted: memory, then the local file, then BC.

        Backdated postings do arrive, so a period already held is still topped up with
        anything newer than the highest entry number it holds.
        """
        key = (kind, company, date_from, date_to)
        with self._lock:
            hit = self._cache.get(key)
            if hit and time.time() - hit[0] < 300:
                return hit[1]
        stored = localdb.rows(kind, company, date_from, date_to)
        marker = localdb.period_loaded(kind, company, date_from, date_to)
        if stored and marker:
            highest = max((r.get("Entry_No") or 0 for r in stored), default=0)
            try:
                newer = [r for r in producer(highest) if (r.get("Entry_No") or 0) > highest]
            except BCError:
                newer = []
            if newer:
                localdb.store(kind, company, newer)
                stored.extend(newer)
            localdb.mark_period(kind, company, date_from, date_to, len(stored))
            rows = stored
        else:
            rows = producer(None)
            localdb.store(kind, company, rows)
            localdb.mark_period(kind, company, date_from, date_to, len(rows))
        with self._lock:
            self._cache[key] = (time.time(), rows)
        return rows

    def _cached(self, key, max_age, producer):
        with self._lock:
            hit = self._cache.get(key)
            if hit and time.time() - hit[0] < max_age:
                return hit[1]
        value = producer()
        with self._lock:
            self._cache[key] = (time.time(), value)
            # a month of ledger entries is tens of megabytes in memory, so the ceiling is
            # low on purpose; the immutable reads are also on disk and cheap to redo
            if len(self._cache) > 24:
                for k, _ in sorted(self._cache.items(), key=lambda kv: kv[1][0])[:8]:
                    self._cache.pop(k, None)
        return value

    # ---- master data ----
    def companies(self):
        return self._cached(("companies",), 3600, lambda: [
            r.get("Name") for r in self._get_all(f"{config.BC_BASE_URL}/Company")])

    def dimension_values(self, company, code="DEPARTMENT"):
        def go():
            rows = self._get_all(self._url(company, config.E_DIMSET),
                                 {"$select": "Dimension_Code,Dimension_Value_Code,Dimension_Value_Name"})
            seen = {}
            for r in rows:
                if (r.get("Dimension_Code") or "").upper() == code.upper() and r.get("Dimension_Value_Code"):
                    seen[r["Dimension_Value_Code"]] = r.get("Dimension_Value_Name") or r["Dimension_Value_Code"]
            return sorted(seen.items())
        return self._cached(("dims", company, code), 3600, go)

    # ---- period figures, calculated by BC ----
    def accounts(self, company, date_from, date_to, dimension=None, max_age=300):
        """Chart of accounts with Net_Change and Balance_at_Date for the period.
        BC evaluates the flow fields; nothing is aggregated here."""
        def go():
            flt = f"Date_Filter eq '{date_from}..{date_to}'"
            if dimension:
                flt += f" and Global_Dimension_1_Filter eq '{dimension}'"
            return self._get_all(self._url(company, config.E_ACCOUNTS),
                                 {"$filter": flt, "$select": ",".join(ACCOUNT_FIELDS)})
        return self._cached(("acc", company, date_from, date_to, dimension or ""), max_age, go)

    def budget(self, company, date_from, date_to, budget_name="", max_age=900):
        def go():
            flt = f"Date ge {date_from} and Date le {date_to}"
            if budget_name:
                flt += f" and Budget_Name eq '{budget_name}'"
            return self._get_all(self._url(company, config.E_BUDGET),
                                 {"$filter": flt, "$select": "Entry_No,Budget_Name,G_L_Account_No,Date,Amount"})
        return self._cached(("bud", company, date_from, date_to, budget_name), max_age, go)

    # ---- ledgers ----
    def open_vendor_entries(self, company, max_age=300):
        return self._cached(("ap", company), max_age, lambda: self._get_all(
            self._url(company, config.E_VENDOR),
            {"$filter": "Open eq true", "$select": ",".join(VENDOR_FIELDS)}))

    def open_customer_entries(self, company, max_age=300):
        return self._cached(("ar", company), max_age, lambda: self._get_all(
            self._url(company, config.E_CUSTOMER),
            {"$filter": "Open eq true", "$select": ",".join(CUSTOMER_FIELDS)}))

    def vendor_entries(self, company, date_from, date_to, max_age=300):
        return self._cached(("apmov", company, date_from, date_to), max_age, lambda: self._get_all(
            self._url(company, config.E_VENDOR),
            {"$filter": f"Posting_Date ge {date_from} and Posting_Date le {date_to}",
             "$select": ",".join(VENDOR_FIELDS)}))

    def bank_entries(self, company, date_from, date_to, max_age=300):
        """Bank movements. Posted entries never change, so they are kept in the local file
        and only newer entry numbers are fetched."""
        def go(after):
            flt = f"Posting_Date ge {date_from} and Posting_Date le {date_to}"
            if after:
                flt += f" and Entry_No gt {after}"
            return self._get_all(self._url(company, config.E_BANK),
                                 {"$filter": flt, "$select": ",".join(BANK_FIELDS)})
        return self._cached_immutable("bank", company, date_from, date_to, go)

    def material_gl_entries(self, company, date_from, date_to, threshold, limit=400, max_age=300):
        """Only entries at or above the materiality threshold: the ledger itself is far too
        large to read in full (tens of thousands of entries a day)."""
        def go():
            flt = (f"Posting_Date ge {date_from} and Posting_Date le {date_to} "
                   f"and (Amount ge {threshold} or Amount le -{threshold})")
            return self._get_all(self._url(company, config.E_GL),
                                 {"$filter": flt, "$select": ",".join(GL_FIELDS), "$top": str(limit)},
                                 limit=limit)
        return self._cached(("gl", company, date_from, date_to, threshold), max_age, go)

    def gl_for_account(self, company, account, date_from, date_to, limit=300):
        flt = (f"G_L_Account_No eq '{account}' and Posting_Date ge {date_from} "
               f"and Posting_Date le {date_to}")
        return self._get_all(self._url(company, config.E_GL),
                             {"$filter": flt, "$select": ",".join(GL_FIELDS), "$top": str(limit)},
                             limit=limit)

    FA_FIELDS = ["Entry_No", "FA_No", "FA_Description", "FA_Class_Code", "FA_Subclass_Code",
                 "FA_Posting_Type", "FA_Posting_Date", "FA_Location_Code", "Location_Code",
                 "Depreciation_Book_Code", "Posting_Date", "Document_No", "Document_Type",
                 "Source_Code", "Amount_LCY"]

    def item_ledger(self, company, date_from, date_to):
        """Posted stock movements of a period. Entries never change once posted, so they
        are kept in the local file and only newer entry numbers are fetched."""
        def go(after=None):
            flt = f"Posting_Date ge {date_from} and Posting_Date le {date_to}"
            if after:
                flt += f" and Entry_No gt {after}"
            return self._get_all(self._url(company, config.E_ILE),
                                 {"$filter": flt,
                                  "$select": "Entry_No,Entry_Type,Item_No,Location_Code,Posting_Date,"
                                             "Quantity,Cost_Amount_Actual,Item_Category_Code,"
                                             "Unit_of_Measure_Code"})
        return self._cached_immutable("ile", company, date_from, date_to, go)

    def fa_entries(self, company, date_from, date_to, max_age=900):
        """Fixed-asset movements of the period only: the FA ledger holds hundreds of
        thousands of entries across the years."""
        def go(after=None):
            flt = f"Posting_Date ge {date_from} and Posting_Date le {date_to}"
            if after:
                flt += f" and Entry_No gt {after}"
            return self._get_all(self._url(company, config.E_FA),
                                 {"$filter": flt, "$select": ",".join(self.FA_FIELDS)})
        return self._cached_immutable("fa", company, date_from, date_to, go)

    ITEM_FIELDS = ["No", "Description", "InventorybyDate", "InventoryField", "Unit_Cost", "Last_Direct_Cost",
                   "Item_Category_Code", "Base_Unit_of_Measure", "Costing_Method", "Blocked", "Vendor_No"]

    def item_snapshot(self, company, as_of, location=None, max_age=900):
        """Stock on hand per item at a date, calculated by BC through the item card's
        Inventory flow field. 15,000 item cards, instead of 290,000 ledger entries a month."""
        def go():
            flt = f"Date_Filter eq '..{as_of}'"
            if location:
                flt += f" and Location_Filter eq '{location}'"
            return self._get_all(self._url(company, config.E_ITEMS),
                                 {"$filter": flt, "$select": ",".join(self.ITEM_FIELDS)})
        return self._cached(("items", company, as_of, location or ""), max_age, go)

    def financial_report_kpis(self, company, max_age=900):
        """pbfinance: account-schedule KPIs. Its own date filter is ignored by the service,
        so the whole (small) table is read and filtered here."""
        return self._cached(("finrep", company), max_age, lambda: self._get_all(
            self._url(company, config.E_FINREPORT)))

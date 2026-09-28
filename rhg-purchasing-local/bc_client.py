"""Reads purchase value entries, vendor ledger entries, purchase orders and item
registers from Business Central OData V4. Read-only.

Ledger entries in BC are append-only, so after the first full load of a period
we only ask BC for entries with a higher Entry_No. That keeps the 30-second
refresh light on the BC server.
"""
from datetime import date, timedelta
import threading
import time
from urllib.parse import quote

import requests

import config
import localdb

SELECT_FIELDS = [
    "Entry_No", "Item_No", "Item_Description", "Posting_Date", "Location_Code",
    "Document_No", "Item_Ledger_Entry_Quantity", "Cost_Amount_Actual",
    "Cost_Amount_Expected", "Cost_per_Unit", "Gen_Prod_Posting_Group",
    "Document_Type", "Document_Date",
]

VENDOR_FIELDS = [
    "Entry_No", "Vendor_No", "Vendor_Name", "Posting_Date", "Document_Date", "Document_Type",
    "Document_No", "Source_Code", "Amount_LCY", "Due_Date", "Open", "Remaining_Amt_LCY",
]

PO_FIELDS = [
    "No", "Document_Type", "Buy_from_Vendor_No", "Buy_from_Vendor_Name", "Posting_Date", "Document_Date",
    "Location_Code", "Status", "Purchaser_Code", "Assigned_User_ID", "Currency_Code", "Amount",
    "Amount_Including_VAT", "Amount_Received_Not_Invoiced_excl_VAT_LCY", "Requested_Receipt_Date",
]


def _norm(name):
    return "".join(ch for ch in name.lower() if ch.isalnum())


class BCError(Exception):
    pass


def _session():
    s = requests.Session()
    if config.BC_AUTH_MODE == "ntlm":
        try:
            from requests_ntlm import HttpNtlmAuth
        except ImportError as exc:
            raise BCError("BC_AUTH_MODE=ntlm needs the requests_ntlm package (pip install requests_ntlm)") from exc
        s.auth = HttpNtlmAuth(config.BC_USERNAME, config.BC_PASSWORD)
    elif config.BC_USERNAME:
        s.auth = (config.BC_USERNAME, config.BC_PASSWORD)
    s.headers["Accept"] = "application/json"
    s.verify = config.BC_VERIFY_SSL
    return s


class BCClient:
    def __init__(self):
        self.session = _session()

    def _url(self, company, entity):
        return f"{config.BC_BASE_URL}/Company('{quote(company, safe='')}')/{entity}"

    def _get_all(self, url, params=None):
        rows = []
        next_url, next_params = url, params
        while next_url:
            try:
                resp = self.session.get(next_url, params=next_params, timeout=config.BC_TIMEOUT)
            except requests.RequestException as exc:
                raise BCError(f"Could not reach Business Central at {config.BC_BASE_URL}: {exc}") from exc
            if resp.status_code == 401:
                raise BCError("Business Central rejected the login (HTTP 401). Check BC_USERNAME / BC_PASSWORD / BC_AUTH_MODE in .env.")
            if resp.status_code >= 400:
                raise BCError(f"Business Central returned HTTP {resp.status_code}: {resp.text[:400]}")
            data = resp.json()
            rows.extend(data.get("value", []))
            next_url = data.get("@odata.nextLink")
            next_params = None  # nextLink already carries the query
        return rows

    def count(self, company, entity, flt=None):
        """How many rows BC says match, asked server side with $count.

        Every large read is checked against this: if fewer rows arrive than BC counts,
        the screen must say so rather than quietly show a smaller number. Returns None
        when the published service does not support $count.
        """
        params = {"$count": "true", "$top": "0"}
        if flt:
            params["$filter"] = flt
        try:
            resp = self.session.get(self._url(company, entity), params=params, timeout=config.BC_TIMEOUT)
            if resp.status_code >= 400:
                return None
            return int(resp.json().get("@odata.count"))
        except (requests.RequestException, ValueError, TypeError):
            return None

    def list_companies(self):
        rows = self._get_all(f"{config.BC_BASE_URL}/Company")
        return [r.get("Name") for r in rows]

    def purchase_entries(self, company, date_from, date_to, after_entry_no=None):
        flt = f"Posting_Date ge {date_from} and Posting_Date le {date_to}"
        if config.BC_PURCHASE_FILTER:
            flt += f" and {config.BC_PURCHASE_FILTER}"
        if after_entry_no is not None:
            flt += f" and Entry_No gt {after_entry_no}"
        params = {"$filter": flt, "$select": ",".join(SELECT_FIELDS)}
        return self._get_all(self._url(company, config.BC_ENTITY), params)

    def vendor_entries(self, company, date_from, date_to, after_entry_no=None):
        flt = f"Posting_Date ge {date_from} and Posting_Date le {date_to}"
        if after_entry_no is not None:
            flt += f" and Entry_No gt {after_entry_no}"
        params = {"$filter": flt, "$select": ",".join(VENDOR_FIELDS)}
        return self._get_all(self._url(company, config.BC_VENDOR_ENTITY), params)

    def fetch(self, kind, company, date_from, date_to, after_entry_no=None):
        if kind == "vendor":
            return self.vendor_entries(company, date_from, date_to, after_entry_no)
        return self.purchase_entries(company, date_from, date_to, after_entry_no)

    # ---- Purchase orders (open documents) ----
    _po_cache = {}

    def purchase_order_list(self, company, max_age=300):
        cached = self._po_cache.get(company)
        if cached and time.time() - cached[0] < max_age:
            return cached[1]
        rows = self._get_all(self._url(company, config.BC_PO_ENTITY), {"$select": ",".join(PO_FIELDS)})
        self._po_cache[company] = (time.time(), rows)
        return rows

    # ---- Budget and general ledger (for the budget benchmark) ----
    def budget_entries(self, company, date_from, date_to, accounts, budget_name=""):
        flt = f"Date ge {date_from} and Date le {date_to}"
        if budget_name:
            flt += f" and Budget_Name eq '{budget_name}'"
        if accounts:
            flt += " and (" + " or ".join(f"G_L_Account_No eq '{a}'" for a in accounts) + ")"
        params = {"$filter": flt, "$select": "Entry_No,Budget_Name,G_L_Account_No,Date,Amount"}
        return self._get_all(self._url(company, config.BC_BUDGET_ENTITY), params)

    def gl_entries(self, company, date_from, date_to, accounts):
        flt = f"Posting_Date ge {date_from} and Posting_Date le {date_to}"
        if accounts:
            flt += " and (" + " or ".join(f"G_L_Account_No eq '{a}'" for a in accounts) + ")"
        params = {"$filter": flt, "$select": "Entry_No,G_L_Account_No,G_L_Account_Name,Posting_Date,Amount,Document_No"}
        return self._get_all(self._url(company, config.BC_GL_ENTITY), params)

    # ---- Open purchase lines (used for the free-text check) ----
    _line_cache = {}

    def purchase_lines(self, company, max_age=600):
        cached = self._line_cache.get(company)
        if cached and time.time() - cached[0] < max_age:
            return cached[1]
        try:
            rows = self._get_all(self._url(company, config.BC_LINE_ENTITY))
        except BCError:
            rows = []
        self._line_cache[company] = (time.time(), rows)
        return rows

    # ---- Contracted purchase prices ----
    _price_fields = {}

    # ---- Item catalogue, chart of accounts and non-item purchase postings ----
    def item_catalog(self, company, max_age=3600):
        """Item cards with the five category groups, for purchase-type classification."""
        def go():
            rows = self._get_all(self._url(company, config.BC_ITEMS_ENTITY),
                                 {"$select": "No,Description,Item_Category_Code,Item_Category_Group_1,"
                                             "Item_Category_Group_2,Item_Category_Group_3,"
                                             "Item_Category_Group_4,Item_Category_Group_5,"
                                             "Base_Unit_of_Measure,Purch_Unit_of_Measure,Vendor_No,"
                                             "Gen_Prod_Posting_Group,Blocked"})
            return {str(r.get("No")): {
                "desc": (r.get("Description") or "").strip(),
                "code": (r.get("Item_Category_Code") or "").strip(),
                "g1": (r.get("Item_Category_Group_1") or "").strip(),
                "g2": (r.get("Item_Category_Group_2") or "").strip(),
                "g3": (r.get("Item_Category_Group_3") or "").strip(),
                "g4": (r.get("Item_Category_Group_4") or "").strip(),
                "uom": (r.get("Base_Unit_of_Measure") or "").strip(),
                "purch_uom": (r.get("Purch_Unit_of_Measure") or "").strip(),
                "vendor": (r.get("Vendor_No") or "").strip(),
                "posting_group": (r.get("Gen_Prod_Posting_Group") or "").strip(),
            } for r in rows}
        return self._cached_value(("catalog", company), max_age, go)

    def chart_of_accounts(self, company, max_age=3600):
        def go():
            rows = self._get_all(self._url(company, config.BC_ACCOUNTS_ENTITY),
                                 {"$select": "No,Name,Income_Balance,Account_Category,"
                                             "Account_Subcategory_Descript,Account_Type"})
            return {str(r.get("No")): r for r in rows}
        return self._cached_value(("coa", company), max_age, go)

    def purchase_gl_entries(self, company, date_from, date_to, accounts=None, max_age=300):
        """Purchase postings in the general ledger, read account by account.

        A single sweep of the ledger by date and source code does not come back
        complete: the published G/L query returns only part of a large result, so
        postings quietly went missing. Asking for a known list of accounts, in small
        batches, returns everything and is far lighter on BC as well. The account list
        comes from the chart of accounts, so nothing is assumed about what exists.
        """
        accounts = sorted({str(a).strip() for a in (accounts or []) if str(a).strip()})
        if not accounts:
            return []
        key = ("purchgl", company, date_from, date_to, ",".join(accounts))

        def go():
            rows, batch_size = [], 20
            for i in range(0, len(accounts), batch_size):
                batch = accounts[i:i + batch_size]
                flt = (f"Posting_Date ge {date_from} and Posting_Date le {date_to} "
                       f"and Source_Code eq '{config.BC_PURCHASE_SOURCE_CODE}' and ("
                       + " or ".join(f"G_L_Account_No eq '{a}'" for a in batch) + ")")
                rows.extend(self._get_all(self._url(company, config.BC_GL_ENTITY),
                                          {"$filter": flt,
                                           "$select": "Entry_No,G_L_Account_No,G_L_Account_Name,"
                                                      "Posting_Date,Document_Date,Document_No,"
                                                      "Document_Type,Source_Code,Amount,Dimension_Set_ID"}))
            return rows
        return self._cached_value(key, max_age, go)

    _value_cache = {}

    def _cached_value(self, key, max_age, producer):
        hit = self._value_cache.get(key)
        if hit and time.time() - hit[0] < max_age:
            return hit[1]
        value = producer()
        self._value_cache[key] = (time.time(), value)
        # small ceiling on purpose: the heavy readings (item cards, SKU cards, a month of
        # ledger entries) are each tens of megabytes in memory, and the local file makes
        # holding them cheap to re-read
        if len(self._value_cache) > 8:
            for k, _ in sorted(self._value_cache.items(), key=lambda kv: kv[1][0])[:4]:
                self._value_cache.pop(k, None)
        return value

    def price_list(self, company):
        """Contracted purchase prices per item.

        Business Central has two generations of pricing. The old Purchase Price table is
        empty once a tenant moves to price lists, and the page still answers, with no
        rows, so an empty answer must not be read as "no agreed prices". Each published
        service name is tried in turn and the first that returns rows is used; the source
        is reported so the screen can say where the prices came from.

        Only purchase lines that are active today are kept: a price list line carries a
        price type, a status, validity dates, a minimum quantity and a currency, and a
        draft or expired line is not an agreed price.
        """
        cached = self._price_fields.get(company)
        if cached and time.time() - cached[0] < 600:
            return cached[1], cached[2]
        names = [n for n in (config.BC_PRICE_ENTITIES or []) if n] or [config.BC_PRICE_ENTITY]
        attempts, prices, status = [], None, None
        for entity in names:
            try:
                rows = self._get_all(self._url(company, entity))
            except BCError as exc:
                attempts.append({"entity": entity, "rows": 0, "error": str(exc)[:200]})
                continue
            attempts.append({"entity": entity, "rows": len(rows)})
            if not rows:
                continue
            prices, status = self._parse_price_rows(rows, entity)
            status["attempts"] = attempts
            break
        if prices is None:
            status = {"available": False, "attempts": attempts,
                      "reason": ("The published price services answered but hold no lines: "
                                 + ", ".join(f"{a['entity']} {a.get('error') or str(a['rows']) + ' rows'}"
                                             for a in attempts)
                                 + ". In current Business Central the agreed prices live in the price "
                                   "lists, so the 'Price List Lines' page has to be the one published.")}
        self._price_fields[company] = (time.time(), prices, status)
        return prices, status

    def _parse_price_rows(self, rows, entity):
        keys = [k for k in rows[0].keys() if not k.startswith("@")]
        normed = {_norm(k): k for k in keys}
        want = {"item": ["assetno", "productno", "itemno", "no", "code"],
                "price": ["directunitcost", "unitcost", "unitprice", "amount"],
                "vendor": ["sourceno", "vendorno", "parentsourceno", "assigntono"],
                "uom": ["unitofmeasurecode", "unitofmeasure"],
                "currency": ["currencycode"],
                "starting": ["startingdate"], "ending": ["endingdate"],
                "min_qty": ["minimumquantity", "minqty"],
                "asset_type": ["assettype", "producttype"],
                "price_type": ["pricetype"], "status": ["status"],
                "amount_type": ["amounttype"], "list_code": ["pricelistcode"],
                "variant": ["variantcode"]}
        fmap = {role: next((normed[c] for c in cands if c in normed), None) for role, cands in want.items()}
        if not (fmap["item"] and fmap["price"]):
            return None, {"available": False,
                          "reason": f"{entity} has no item/price columns. Found: " + ", ".join(keys)}
        today = date.today().isoformat()
        candidates = {}
        skipped = {"expired": 0, "not_started": 0, "no_price": 0, "not_item": 0,
                   "not_purchase": 0, "not_active": 0}
        for r in rows:
            def val(role):
                return r.get(fmap[role]) if fmap[role] else None
            if fmap["price_type"] and str(val("price_type") or "").strip().lower() not in ("", "purchase"):
                skipped["not_purchase"] += 1
                continue
            if fmap["status"] and str(val("status") or "").strip().lower() not in ("", "active"):
                skipped["not_active"] += 1
                continue
            if fmap["asset_type"] and str(val("asset_type") or "").strip().lower() not in ("", "item"):
                skipped["not_item"] += 1
                continue
            item, price = val("item"), val("price")
            if not item or price in (None, "", 0):
                skipped["no_price"] += 1
                continue
            start = str(val("starting") or "")[:10]
            end = str(val("ending") or "")[:10]
            if start and start[:4] > "0001" and start > today:
                skipped["not_started"] += 1
                continue
            if end and end[:4] > "0001" and end < today:
                skipped["expired"] += 1
                continue
            candidates.setdefault(str(item), []).append({
                "price": float(price),
                "vendor": (val("vendor") or "") if fmap["vendor"] else "",
                "uom": (val("uom") or "") if fmap["uom"] else "",
                "currency": (val("currency") or "") if fmap["currency"] else "",
                "variant": (val("variant") or "") if fmap["variant"] else "",
                "list": (val("list_code") or "") if fmap["list_code"] else "",
                "starting": start, "ending": end,
                "min_qty": float(val("min_qty") or 0) if fmap["min_qty"] else 0,
            })
        prices = {}
        for item, rows_for_item in candidates.items():
            rows_for_item.sort(key=lambda x: (x["currency"] != "", x["min_qty"], x["starting"]))
            chosen = rows_for_item[-1] if len(rows_for_item) == 1 else rows_for_item[0]
            spread = max(x["price"] for x in rows_for_item) - min(x["price"] for x in rows_for_item)
            prices[item] = {**chosen, "alternatives": len(rows_for_item) - 1, "price_spread": spread}
        return prices, {"available": True, "entity": entity, "count": len(prices), "fields": keys,
                        "rows_read": len(rows), "skipped": skipped,
                        "multi_price_items": sum(1 for p in prices.values() if p["alternatives"]),
                        "note": f"Read from {entity}. Only purchase lines that are active today are used; "
                                f"where an item has several valid prices the local-currency, lowest-minimum "
                                f"line is taken and the others are counted."}

    # ---- Stock: balances at a date and the movements of a period ----
    def inventory_at(self, company, location, as_of, max_age=86400):
        """Quantity on hand per item at one location on one date.

        Uses the item card's InventorybyDate flowfield with a Date_Filter of "..date".
        InventoryField ignores the date filter and always answers with today's stock, so
        it must not be used for a historical balance.
        """
        def go():
            params = {"$select": "No,Description,InventorybyDate,Unit_Cost,Base_Unit_of_Measure,"
                                 "Item_Category_Code,Item_Category_Group_2,Item_Category_Group_3,"
                                 "Item_Category_Group_4,Vendor_No",
                      "$filter": f"Date_Filter eq '..{as_of}'"
                                 + (f" and Location_Filter eq '{location}'" if location else "")}
            rows = self._get_all(self._url(company, config.BC_ITEMS_ENTITY), params)
            balances, cards = {}, {}
            for r in rows:
                no = str(r.get("No"))
                qty = float(r.get("InventorybyDate") or 0)
                cards[no] = {"desc": (r.get("Description") or "").strip(),
                             "uom": (r.get("Base_Unit_of_Measure") or "").strip(),
                             "unit_cost": float(r.get("Unit_Cost") or 0),
                             "category": (r.get("Item_Category_Code") or "").strip(),
                             "g2": (r.get("Item_Category_Group_2") or "").strip(),
                             "g3": (r.get("Item_Category_Group_3") or "").strip(),
                             "g4": (r.get("Item_Category_Group_4") or "").strip(),
                             "vendor": (r.get("Vendor_No") or "").strip()}
                if qty:
                    balances[no] = qty
            return balances, cards
        return self._cached_value(("inv", company, location or "", as_of), max_age, go)

    def item_ledger(self, company, location, date_from, date_to, max_age=86400):
        """Every posted stock movement of one location in one period."""
        def go():
            flt = f"Posting_Date ge {date_from} and Posting_Date le {date_to}"
            if location:
                flt += f" and Location_Code eq '{location}'"
            return self._get_all(self._url(company, config.BC_ILE_ENTITY),
                                 {"$filter": flt,
                                  "$select": "Entry_No,Entry_Type,Item_No,Item_Description,Posting_Date,"
                                             "Document_No,Document_Type,Location_Code,Quantity,"
                                             "Unit_of_Measure_Code,Remaining_Quantity,Invoiced_Quantity,"
                                             "Lot_No,Expiration_Date,Item_Category_Code,"
                                             "Cost_Amount_Actual,Cost_Amount_Expected"})
        return self._cached_value(("ile", company, location or "", date_from, date_to), max_age, go)

    def stockkeeping_units(self, company, location=None, max_age=3600):
        """Planning parameters per item and location: reorder point, maximum inventory,
        reorder quantity. Returns ({(item, location): params}, status). A card can exist
        with every parameter at zero, which means nobody has set a limit, so the status
        reports how many actually carry one."""
        if not config.BC_SKU_ENTITY:
            return {}, {"available": False, "reason": "BC_SKU_ENTITY is empty in .env."}

        def go():
            params = {"$select": "Item_No,Location_Code,Variant_Code,Description,Inventory,"
                                 "Reorder_Point,Reorder_Quantity,Maximum_Inventory,Replenishment_System"}
            if location:
                params["$filter"] = f"Location_Code eq '{location}'"
            rows = self._get_all(self._url(company, config.BC_SKU_ENTITY), params)
            out = {}
            with_max = with_point = 0
            for r in rows:
                key = (str(r.get("Item_No")), str(r.get("Location_Code")))
                reorder = float(r.get("Reorder_Point") or 0)
                maximum = float(r.get("Maximum_Inventory") or 0)
                with_max += maximum > 0
                with_point += reorder > 0
                out[key] = {"reorder_point": reorder, "maximum": maximum,
                            "reorder_qty": float(r.get("Reorder_Quantity") or 0),
                            "variant": (r.get("Variant_Code") or "").strip(),
                            "replenishment": (r.get("Replenishment_System") or "").strip()}
            return out, {"available": True, "cards": len(rows), "with_maximum": with_max,
                         "with_reorder_point": with_point}
        try:
            return self._cached_value(("sku", company, location or ""), max_age, go)
        except BCError as exc:
            return {}, {"available": False, "reason": str(exc)[:300]}

    def posted_invoices(self, company, date_from, date_to, max_age=900):
        """Posted purchase invoices of a period, with the order number behind each one."""
        def go():
            return self._get_all(self._url(company, config.BC_PINV_ENTITY),
                                 {"$filter": f"Posting_Date ge {date_from} and Posting_Date le {date_to}",
                                  "$select": "No,Order_No,Vendor_Invoice_No,Buy_from_Vendor_No,"
                                             "Buy_from_Vendor_Name,Posting_Date,Document_Date,Due_Date,"
                                             "Amount,Amount_Including_VAT,Currency_Code,Location_Code,"
                                             "Shortcut_Dimension_1_Code,Shortcut_Dimension_2_Code,"
                                             "Purchaser_Code,Cancelled,Corrective,Closed"})
        return self._cached_value(("pinv", company, date_from, date_to), max_age, go)

    def invoice_lines_without_order(self, company, first_doc, last_doc, max_age=900):
        """Invoice lines carrying no order number, within a range of document numbers.

        Reading every line of a month would be tens of thousands of rows for no purpose:
        only the lines without an order number matter, and the document numbers of a
        period run in a block, so the range keeps the read small. A line found here whose
        invoice header does carry an order number is a partially linked invoice.
        """
        def go():
            return self._get_all(self._url(company, config.BC_PINV_LINE_ENTITY),
                                 {"$filter": f"Order_No eq '' and Document_No ge '{first_doc}' "
                                             f"and Document_No le '{last_doc}'",
                                  "$select": "Document_No,Line_No,Type,No,Description,Quantity,"
                                             "Unit_of_Measure_Code,Direct_Unit_Cost,Amount,"
                                             "Amount_Including_VAT,Buy_from_Vendor_No,"
                                             "Buy_from_Vendor_Name,Shortcut_Dimension_1_Code,"
                                             "Shortcut_Dimension_2_Code,Order_No"})
        return self._cached_value(("pinvline", company, first_doc, last_doc), max_age, go)

    def locations(self, company, max_age=3600):
        """The location codes as BC holds them. The published page carries the code only,
        so no name is asked for: selecting a field that does not exist fails the whole
        request and leaves the screen with an empty list."""
        def go():
            rows = self._get_all(self._url(company, config.BC_LOCATIONS_ENTITY), {"$select": "Code"})
            return [{"code": str(r.get("Code")).strip(), "name": str(r.get("Code")).strip()}
                    for r in rows if r.get("Code")]
        return self._cached_value(("locs", company), max_age, go)

    # ---- Requisition worksheet lines ----
    _req_fields = {}

    def requisition_fields(self, company):
        """Field map for the published requisition worksheet, or {'error': ...} if it is not there."""
        cached = self._req_fields.get(company)
        if cached and time.time() - cached[0] < 300:
            return cached[1]
        fmap = None
        if config.BC_REQ_ENTITY:
            try:
                rows = self._get_all(self._url(company, config.BC_REQ_ENTITY), {"$top": "1"})
                keys = [k for k in (rows[0].keys() if rows else []) if not k.startswith("@")]
                normed = {_norm(k): k for k in keys}
                want = {
                    "template": ["worksheettemplatename", "templatename", "reqwkshtemplatename"],
                    "batch": ["journalbatchname", "batchname"],
                    "line": ["lineno", "linenumber"],
                    "item": ["no", "itemno", "number"],
                    "description": ["description"],
                    "quantity": ["quantity"],
                    "location": ["locationcode", "location"],
                    "vendor": ["vendorno", "buyfromvendorno"],
                    "vendor_name": ["vendorname", "buyfromvendorname"],
                    "unit_cost": ["directunitcost", "unitcost", "unitcostlcy"],
                    "due_date": ["duedate"],
                    "order_date": ["orderdate"],
                    "user": ["userid", "assigneduserid", "purchasercode"],
                    "action": ["actionmessage", "acceptactionmessage"],
                    "uom": ["unitofmeasurecode", "unitofmeasure"],
                    "ref_order": ["reforderno", "referenceorderno"],
                    "ref_type": ["refordertype"],
                    "accepted": ["acceptactionmessage"],
                    "requester": ["requesterid"],
                    "purchaser": ["purchasercode"],
                    "original_qty": ["originalquantity"],
                }
                fmap = {role: next((normed[c] for c in cands if c in normed), None) for role, cands in want.items()}
                fmap["_empty"] = not rows
                fmap["_fields"] = keys
                if not rows:
                    fmap["_note"] = "The web service is published but returned no lines."
            except BCError as exc:
                fmap = {"error": str(exc)}
        else:
            fmap = {"error": "BC_REQ_ENTITY is empty in .env, so no requisition service is queried."}
        self._req_fields[company] = (time.time(), fmap)
        return fmap

    def requisition_lines(self, company):
        fmap = self.requisition_fields(company)
        if not fmap or "error" in fmap:
            return None, fmap
        rows = self._get_all(self._url(company, config.BC_REQ_ENTITY))
        out = []
        for r in rows:
            def g(role):
                key = fmap.get(role)
                return r.get(key) if key else None
            item = g("item")
            if not item and not g("description"):
                continue
            key_parts = [str(g("template") or ""), str(g("batch") or ""), str(g("line") or ""), str(item or "")]
            out.append({
                "key": "/".join(p for p in key_parts if p),
                "template": g("template") or "", "batch": g("batch") or "",
                "item": item or "", "description": (g("description") or "").strip(),
                "quantity": g("quantity") or 0, "location": g("location") or "",
                "vendor": g("vendor_name") or g("vendor") or "", "unit_cost": g("unit_cost") or 0,
                "due_date": g("due_date") or "", "order_date": g("order_date") or "",
                "user": g("user") or "", "action": g("action") or "", "uom": g("uom") or "",
                "ref_order": str(g("ref_order") or "").strip(),
                "ref_type": str(g("ref_type") or "").strip(),
                "accepted": bool(g("accepted")),
                "requester": str(g("requester") or "").strip(),
                "purchaser": str(g("purchaser") or "").strip(),
                "vendor_no": str(g("vendor") or "").strip(),
                "line_no": g("line") or 0,
            })
        return out, fmap

    # ---- Item Registers (who posted what, and when) ----
    _reg_fields = {}

    def register_fields(self, company):
        """Returns a field map for the Item Registers web service, or None if it isn't published."""
        cached = self._reg_fields.get(company)
        if cached and time.time() - cached[0] < 600:
            return cached[1]
        fmap = None
        if config.BC_REGISTER_ENTITY:
            try:
                rows = self._get_all(self._url(company, config.BC_REGISTER_ENTITY), {"$top": "1"})
                keys = list(rows[0].keys()) if rows else []
                want = {"no": ["no", "entryno"], "user": ["userid"], "from": ["fromvalueentryno"],
                        "to": ["tovalueentryno"], "date": ["creationdate"], "time": ["creationtime"],
                        "created_at": ["systemcreatedat"], "source": ["sourcecode"], "batch": ["journalbatchname"]}
                normed = {_norm(k): k for k in keys if not k.startswith("@")}
                fmap = {role: next((normed[c] for c in cands if c in normed), None) for role, cands in want.items()}
                if not (fmap["user"] and fmap["from"] and fmap["to"]):
                    fmap = {"error": "Item Registers is published but lacks User ID / From-To Value Entry No. fields. Found: " + ", ".join(keys)}
            except BCError as exc:
                fmap = {"error": str(exc)}
        self._reg_fields[company] = (time.time(), fmap)
        return fmap

    def registers_for(self, company, min_entry, max_entry):
        fmap = self.register_fields(company)
        if not fmap or "error" in fmap:
            return None, fmap
        flt = f"{fmap['to']} ge {min_entry} and {fmap['from']} le {max_entry} and {fmap['from']} gt 0"
        rows = self._get_all(self._url(company, config.BC_REGISTER_ENTITY), {"$filter": flt})
        out = []
        for r in rows:
            created = r.get(fmap["created_at"]) if fmap.get("created_at") else None
            date_s = r.get(fmap["date"]) if fmap.get("date") else (created or "")[:10]
            time_s = r.get(fmap["time"]) if fmap.get("time") else (created or "")[11:19]
            out.append({"user": (r.get(fmap["user"]) or "?").strip() or "?",
                        "from": int(r.get(fmap["from"]) or 0), "to": int(r.get(fmap["to"]) or 0),
                        "date": date_s or "", "time": time_s or "",
                        "source": r.get(fmap["source"]) if fmap.get("source") else ""})
        out.sort(key=lambda x: x["from"])
        return out, fmap


class PeriodCache:
    """The rows of one (kind, company, date range), held in memory and on disk.

    Three layers, cheapest first. Memory answers a repeated request inside the refresh
    window. The local SQLite file answers everything already read at any time in the
    past, including after a restart. Business Central is asked only for entries newer
    than the highest one already held, because a posted ledger entry never changes.
    """

    def __init__(self, client):
        self.client = client
        self.lock = threading.Lock()          # guards the lock registry and the store index
        self.key_locks = {}
        self.store = {}  # key -> dict(rows, max_entry, loaded_at, checked_at)

    def _lock_for(self, key):
        with self.lock:
            return self.key_locks.setdefault(key, threading.Lock())

    def get(self, company, date_from, date_to, max_age, kind="value"):
        key = (kind, company, date_from, date_to)
        with self._lock_for(key):
            item = self.store.get(key)
            now = time.time()
            if item and now - item["checked_at"] < max_age:
                return item
            source = "memory"
            if item is None:
                stored = localdb.rows(kind, company, date_from, date_to)
                marker = localdb.period_loaded(kind, company, date_from, date_to)
                if stored and marker:
                    item = {"rows": stored, "loaded_at": marker["loaded_at"]}
                    source = "local file"
                else:
                    rows = self.client.fetch(kind, company, date_from, date_to)
                    localdb.store(kind, company, rows)
                    localdb.mark_period(kind, company, date_from, date_to, len(rows))
                    item = {"rows": rows, "loaded_at": now}
                    source = "Business Central, full read"
                self.store[key] = item
                item["max_entry"] = max((r.get("Entry_No") or 0 for r in item["rows"]), default=0)
                item["checked_at"] = now if source.startswith("Business") else 0
                item["new_since_last"] = len(item["rows"]) if source.startswith("Business") else 0
                item["source"] = source
                if source.startswith("Business"):
                    self._trim()
                    return item
            # top up: only entries posted in the period and newer than the highest held
            try:
                new_rows = self.client.fetch(kind, company, date_from, date_to, item["max_entry"])
            except BCError:
                # some BC query objects refuse an Entry_No filter; fall back to a full reload
                new_rows = None
            if new_rows is None:
                item["rows"] = self.client.fetch(kind, company, date_from, date_to)
                localdb.store(kind, company, item["rows"])
                localdb.mark_period(kind, company, date_from, date_to, len(item["rows"]))
                new_count = 0
                source = "Business Central, full reload"
            else:
                known = {r.get("Entry_No") for r in item["rows"]}
                new_rows = [r for r in new_rows if r.get("Entry_No") not in known]
                item["rows"].extend(new_rows)
                new_count = len(new_rows)
                if new_rows:
                    localdb.store(kind, company, new_rows)
                    localdb.mark_period(kind, company, date_from, date_to, len(item["rows"]))
                elif source == "local file":
                    localdb.mark_period(kind, company, date_from, date_to, len(item["rows"]))
                source = source if source == "local file" else "Business Central, top-up"
            item["max_entry"] = max((r.get("Entry_No") or 0 for r in item["rows"]), default=0)
            item["checked_at"] = now
            item["new_since_last"] = new_count
            item["source"] = source
            self._trim()
            return item

    def _trim(self, keep=8):
        if len(self.store) > keep:
            oldest = sorted(self.store.items(), key=lambda kv: kv[1]["checked_at"])[: len(self.store) - keep]
            for key, _ in oldest:
                self.store.pop(key, None)

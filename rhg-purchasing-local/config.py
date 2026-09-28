"""Loads settings from the .env file next to this script (no extra packages needed)."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _load_env():
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


_load_env()


def get(key, default=None):
    return os.environ.get(key, default)


BC_BASE_URL = get("BC_BASE_URL", "http://92.205.182.52:8348/RHG/ODataV4").rstrip("/")
BC_USERNAME = get("BC_USERNAME", "")
BC_PASSWORD = get("BC_PASSWORD", "")          # web service access key (Basic) or Windows password (NTLM)
BC_AUTH_MODE = get("BC_AUTH_MODE", "basic").lower()   # basic | ntlm
BC_COMPANY = get("BC_COMPANY", "SALT")
BC_ENTITY = get("BC_ENTITY", "ValueEntries")
BC_VENDOR_ENTITY = get("BC_VENDOR_ENTITY", "VendorLedgerEntries")
BC_PO_ENTITY = get("BC_PO_ENTITY", "PurchaseOrderList")
# Requisition Worksheet published as a web service (see README); blank disables the feature
BC_REQ_ENTITY = get("BC_REQ_ENTITY", "RequisitionLines")
BC_LINE_ENTITY = get("BC_LINE_ENTITY", "PurchaseLineBI")
BC_BUDGET_ENTITY = get("BC_BUDGET_ENTITY", "G_LBudgetEntries")
BC_GL_ENTITY = get("BC_GL_ENTITY", "G_LEntries")
# Purchase price list published as a web service (Purchase Prices / Price List Lines); blank disables the check
# Contracted purchase prices. Newer Business Central keeps them in price lists, so the
# app tries each of these service names in turn and uses the first that returns rows.
BC_PRICE_ENTITY = get("BC_PRICE_ENTITY", "PurchasePrices")
BC_PRICE_ENTITIES = [e.strip() for e in get(
    "BC_PRICE_ENTITIES", "PriceListLines,PurchasePrices").split(",") if e.strip()]
BC_ITEMS_ENTITY = get("BC_ITEMS_ENTITY", "PbItems")
BC_ILE_ENTITY = get("BC_ILE_ENTITY", "ItemLedgerEntries")
BC_LOCATIONS_ENTITY = get("BC_LOCATIONS_ENTITY", "NavLocations")
# Stockkeeping Unit List (page 5701) published as a web service; blank disables the checks
BC_SKU_ENTITY = get("BC_SKU_ENTITY", "StockkeepingUnits")
# Posted purchase invoices and their lines: the only place BC publishes the order
# number behind an invoice, which is what proves a purchase had an order first.
BC_PINV_ENTITY = get("BC_PINV_ENTITY", "PostedPurchaseInvoices")
BC_PINV_LINE_ENTITY = get("BC_PINV_LINE_ENTITY", "PostedPurchaseInvoiceLines")
BC_ACCOUNTS_ENTITY = get("BC_ACCOUNTS_ENTITY", "Chart_of_Accounts")
BC_GL_ENTITY = get("BC_GL_ENTITY", "G_LEntries")
BC_PURCHASE_SOURCE_CODE = get("BC_PURCHASE_SOURCE_CODE", "PURCHASES")
# Standard page 117 "Item Registers" published as a web service; gives the user who posted each document
BC_REGISTER_ENTITY = get("BC_REGISTER_ENTITY", "ItemRegisters")
BC_PURCHASE_FILTER = get("BC_PURCHASE_FILTER", "Item_Ledger_Entry_Type eq 'Purchase'")
BC_TIMEOUT = int(get("BC_TIMEOUT", "180"))
BC_VERIFY_SSL = get("BC_VERIFY_SSL", "true").lower() != "false"

# Background warm-up: reads the current and previous month before anyone asks, so the
# first person in the morning does not wait for them. "" disables it.
WARM_AT = get("WARM_AT", "05:00")
WARM_ON_START = get("WARM_ON_START", "true").lower() != "false"
# Serve the figures already held while the new ones are being read, instead of making
# the screen wait. The screen says when what it shows is being refreshed.
SERVE_WHILE_REFRESHING = get("SERVE_WHILE_REFRESHING", "true").lower() != "false"

APP_HOST = get("APP_HOST", "127.0.0.1")      # 0.0.0.0 to share on the office network
APP_PORT = int(get("APP_PORT", "8765"))
REFRESH_SECONDS = int(get("REFRESH_SECONDS", "30"))

# Thresholds used to decide what counts as a price mover / exception (ALL, ex-VAT).
# Service-level thresholds used by the exceptions engine live in data/settings.json
# and are editable from the Settings page.
MIN_SPEND_FOR_MOVER = float(get("MIN_SPEND_FOR_MOVER", "5000"))
MIN_PCT_FOR_MOVER = float(get("MIN_PCT_FOR_MOVER", "3"))
PRICE_ALERT_PCT = float(get("PRICE_ALERT_PCT", "25"))
EXCEPTION_SPEND = float(get("EXCEPTION_SPEND", "100000"))

# Count received-but-not-yet-invoiced goods at their expected cost (recommended for a live view)
INCLUDE_EXPECTED_COST = get("INCLUDE_EXPECTED_COST", "true").lower() != "false"
LOCATION_SPREAD_PCT = float(get("LOCATION_SPREAD_PCT", "20"))

LATE_POSTING_DAYS = int(get("LATE_POSTING_DAYS", "7"))

"""Settings for the Finance & Inventory Control Tower (read from .env next to this file)."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _load_env():
    env = BASE_DIR / ".env"
    if not env.exists():
        return
    for raw in env.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()


def get(key, default=None):
    return os.environ.get(key, default)


BC_BASE_URL = get("BC_BASE_URL", "http://92.205.182.52:8348/RHG/ODataV4").rstrip("/")
BC_USERNAME = get("BC_USERNAME", "")
BC_PASSWORD = get("BC_PASSWORD", "")
BC_AUTH_MODE = get("BC_AUTH_MODE", "basic").lower()
BC_COMPANY = get("BC_COMPANY", "SALT")
BC_TIMEOUT = int(get("BC_TIMEOUT", "180"))
BC_VERIFY_SSL = get("BC_VERIFY_SSL", "true").lower() != "false"

# entity names as published in BC
E_ACCOUNTS = get("E_ACCOUNTS", "Chart_of_Accounts")
E_GL = get("E_GL", "G_LEntries")
E_BUDGET = get("E_BUDGET", "G_LBudgetEntries")
E_VENDOR = get("E_VENDOR", "VendorLedgerEntries")
E_CUSTOMER = get("E_CUSTOMER", "Cust_LedgerEntries")
E_BANK = get("E_BANK", "BankAccountLedgerEntries")
E_ITEM_LEDGER = get("E_ITEM_LEDGER", "ItemLedgerEntries")
E_FA = get("E_FA", "FALedgerEntries")
E_DIMSET = get("E_DIMSET", "DimensionSetEntries")
E_FINREPORT = get("E_FINREPORT", "pbfinance")
E_ITEMS = get("E_ITEMS", "PbItems")
# Posted stock movements. The snapshots say what the balance was; only the ledger says
# whether anything actually moved.
E_ILE = get("E_ILE", "ItemLedgerEntries")
E_REGISTERS = get("E_REGISTERS", "ItemRegisters")
E_LOCATIONS = get("E_LOCATIONS", "NavLocations")

# Background warm-up and instant serving, as in the purchasing tower.
WARM_AT = get("WARM_AT", "05:15")
WARM_ON_START = get("WARM_ON_START", "true").lower() != "false"
SERVE_WHILE_REFRESHING = get("SERVE_WHILE_REFRESHING", "true").lower() != "false"

APP_HOST = get("APP_HOST", "127.0.0.1")
APP_PORT = int(get("APP_PORT", "8766"))
REFRESH_SECONDS = int(get("REFRESH_SECONDS", "120"))
CURRENCY = get("CURRENCY", "ALL")

# The general ledger holds ~38,000 entries a day, and filtering it by amount makes BC scan
# the whole table. The G/L control screen therefore looks at a short window and caps the rows.
GL_WINDOW_DAYS = int(get("GL_WINDOW_DAYS", "7"))
GL_MAX_ROWS = int(get("GL_MAX_ROWS", "300"))

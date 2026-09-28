"""The permission catalogue for every RHG application.

A permission is "<app>.<area>.<action>". Roles are named bundles of them, so a
new report is added by listing its permissions here - no change to the apps.

The two control towers used to carry their own role tables, and they disagreed:
Finance had a "closing" right Purchasing never had. Those tables are merged here,
and the old role names keep working so existing accounts map across cleanly.
"""

# ---------------------------------------------------------------- catalogue
PERMISSIONS = {
    # --- Finance & Inventory
    "finance.view":            "Open the Finance control tower",
    "finance.pl.view":         "Profit and loss",
    "finance.bs.view":         "Balance sheet",
    "finance.ap.view":         "Payables and ageing",
    "finance.ar.view":         "Receivables and ageing",
    "finance.cash.view":       "Cash and bank",
    "finance.treasury.view":   "Treasury",
    "finance.assets.view":     "Fixed assets",
    "finance.inventory.view":  "Inventory and stock",
    "finance.gl.view":         "General ledger detail",
    "finance.actions.manage":  "Create and update finance actions",
    "finance.closing.manage":  "Tick and sign off closing tasks",
    "finance.settings.manage": "Change thresholds and owners",
    "finance.export":          "Export finance data",

    # --- Purchasing
    "purchasing.view":            "Open the Purchasing control tower",
    "purchasing.spend.view":      "Spend and suppliers",
    "purchasing.prices.view":     "Price movements",
    "purchasing.orders.view":     "Purchase orders",
    "purchasing.invoices.view":   "Invoices",
    "purchasing.quality.view":    "Quality and compliance",
    "purchasing.actions.manage":  "Create and update purchasing actions",
    "purchasing.settings.manage": "Change purchasing thresholds",
    "purchasing.export":          "Export purchasing data",

    # --- Platform
    "admin.users.manage":    "Create, edit and disable accounts",
    "admin.roles.manage":    "Change what roles are allowed to do",
    "admin.audit.view":      "Read the audit trail",
    "admin.sessions.manage": "See and revoke active sessions",
}

_FINANCE_ALL = [p for p in PERMISSIONS if p.startswith("finance.")]
_PURCHASING_ALL = [p for p in PERMISSIONS if p.startswith("purchasing.")]
_FINANCE_READ = [p for p in _FINANCE_ALL if p.endswith(".view") or p == "finance.export"]
_PURCHASING_READ = [p for p in _PURCHASING_ALL if p.endswith(".view") or p == "purchasing.export"]

# ---------------------------------------------------------------- roles
# system=True marks a role the user manager will not let anyone delete.
DEFAULT_ROLES = {
    "Administrator": {
        "description": "Everything, including accounts, roles and the audit trail.",
        "permissions": list(PERMISSIONS),
        "system": True,
    },
    "CFO": {
        "description": "Both towers in full, closing sign-off and thresholds; cannot manage accounts.",
        "permissions": _FINANCE_ALL + _PURCHASING_READ + ["admin.audit.view"],
        "system": False,
    },
    "Finance director": {
        "description": "Finance in full, including closing and thresholds.",
        "permissions": _FINANCE_ALL,
        "system": False,
    },
    "Chief accountant": {
        "description": "Finance screens, actions and closing; no thresholds.",
        "permissions": [p for p in _FINANCE_ALL if p != "finance.settings.manage"],
        "system": False,
    },
    "Accountant": {
        "description": "Finance screens and actions; no closing, no thresholds.",
        "permissions": [p for p in _FINANCE_ALL
                        if p not in ("finance.settings.manage", "finance.closing.manage")],
        "system": False,
    },
    "Purchasing director": {
        "description": "Purchasing in full, including thresholds.",
        "permissions": _PURCHASING_ALL,
        "system": False,
    },
    "Buyer": {
        "description": "Purchasing screens and actions; no thresholds.",
        "permissions": [p for p in _PURCHASING_ALL if p != "purchasing.settings.manage"],
        "system": False,
    },
    "Viewer": {
        "description": "Read only, both towers, including exports.",
        "permissions": _FINANCE_READ + _PURCHASING_READ,
        "system": False,
    },
}

# Old per-app role names -> the merged role they become on import.
LEGACY_ROLE_MAP = {
    "Administrator": "Administrator",
    "CFO": "CFO",
    "Finance director": "Finance director",
    "Chief accountant": "Chief accountant",
    "Accountant": "Accountant",
    "Purchasing director": "Purchasing director",
    "Buyer": "Buyer",
    "Viewer": "Viewer",
}


def app_of(permission):
    return permission.split(".", 1)[0]


def valid(permission):
    return permission in PERMISSIONS

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

    # --- Layout of the towers (drag and drop, hiding sections)
    "layout.personal":       "Rearrange and hide sections for yourself",
    "layout.roles.manage":   "Set the sections each role sees, and their order",

    # --- Unicorn, the business analyst (morning brief and chat; for management)
    "unicorn.use":           "Talk to Unicorn, the business analyst: the morning brief and the chat",

    # --- Platform
    "admin.users.manage":    "Create, edit and disable accounts",
    "admin.roles.manage":    "Change what roles are allowed to do",
    "admin.audit.view":      "Read the audit trail",
    "admin.sessions.manage": "See and revoke active sessions",
}

# ---------------------------------------------------------------- screens and actions
# Roles are built from SCREENS (what can be opened: tower → menu item → tab, screens.py) and ACTIONS (what can be
# done). The data rights above ("finance.ap.view", "finance.view" ...) are what the towers' servers check; they
# follow from the screens a role holds (screens.grants) and are no longer ticked by hand.
import screens  # noqa: E402

DATA_RIGHTS = [p for p in PERMISSIONS if p.endswith(".view") and not p.startswith("admin.")]
ACTIONS = {
    "finance.export": "Finance: export (CSV, Excel, PDF)",
    "finance.actions.manage": "Finance: create and update actions",
    "finance.closing.manage": "Finance: tick and sign off closing tasks",
    "finance.settings.manage": "Finance: change thresholds, owners and email settings",
    "purchasing.export": "Purchasing: export (CSV, Excel, PDF)",
    "purchasing.actions.manage": "Purchasing: create and update actions",
    "purchasing.settings.manage": "Purchasing: change thresholds and email settings",
    "unicorn.use": "Unicorn, the business analyst: the morning brief and the chat",
    "layout.personal": "Rearrange and hide sections for yourself",
    "layout.roles.manage": "Set the sections each role sees, and their order",
    "admin.users.manage": "Administration: create, edit and disable accounts, and their access",
    "admin.roles.manage": "Administration: roles and permissions",
    "admin.audit.view": "Administration: read the audit trail",
    "admin.sessions.manage": "Administration: see and revoke active sessions",
}
for _p, (_label, _kind, _g) in screens.SCREENS.items():
    PERMISSIONS[_p] = _label


def effective(perms):
    """What a role really holds: its screens and actions, and the data rights its screens bring."""
    held = set(perms or [])
    return sorted(held | screens.grants(held))


def screens_for_rights(rights):
    """For a role stored before screens existed: every screen whose data rights it already held."""
    rights = set(rights)
    out = []
    for p, (_l, _k, g) in screens.SCREENS.items():
        app = p.split(".", 1)[0]
        need = set(g) | ({f"{app}.view"} if app in ("finance", "purchasing") else set())
        if p == "platform.s.control":                       # it always showed whichever towers the role had
            if rights & {"finance.view", "purchasing.view"}:
                out.append(p)
            continue
        if app == "platform":
            need = set(g) or {"finance.view"}
        if need and need <= rights:
            out.append(p)
    return out


def _s(*prefixes, but=()):
    """The screens under the given prefixes (finance.s.kartela, purchasing.s ...), less those listed in but."""
    return [p for p in screens.SCREENS if any(p == x or p.startswith(x + ".") for x in prefixes)
            and not any(p == y or p.startswith(y + ".") for y in but)]


_FIN_ALL = _s("finance.s")
_PUR_ALL = _s("purchasing.s")
_EXEC = _s("platform.s.executive")
_FIN_ACT = ["finance.export", "finance.actions.manage", "finance.closing.manage", "finance.settings.manage"]
_PUR_ACT = ["purchasing.export", "purchasing.actions.manage", "purchasing.settings.manage"]

# ---------------------------------------------------------------- roles
# The administration of RHG by department (Purchasing, Sales, Finance, HR), plus management. Each can be changed
# in Admin → Roles & permissions; a role missing here is added on start, one already stored is left as it is.
# system=True marks a role the user manager will not let anyone delete.
DEFAULT_ROLES = {
    "Administrator": {"department": "Management", "system": True,
                      "description": "Everything, including accounts, roles and the audit trail.",
                      "permissions": list(PERMISSIONS)},
    # --- management
    "CEO": {"department": "Management", "system": False,
            "description": "Every screen of both towers and the executive dashboard; Unicorn; read only.",
            "permissions": _FIN_ALL + _PUR_ALL + _s("platform.s") + ["finance.export", "purchasing.export",
                                                                     "unicorn.use", "layout.personal"]},
    "COO": {"department": "Management", "system": False,
            "description": "Operations: sales, inventory, purchasing, the executive dashboard; Unicorn.",
            "permissions": _s("finance.s.overview", "finance.s.sales", "finance.s.inventory", "finance.s.budget")
                           + _PUR_ALL + _s("platform.s") + ["finance.export", "purchasing.export", "unicorn.use",
                                                             "layout.personal"]},
    # --- finance
    "CFO": {"department": "Finance", "system": False,
            "description": "Finance in full with closing and thresholds, Purchasing to read, the executive "
                           "dashboard; Unicorn.",
            "permissions": _FIN_ALL + _PUR_ALL + _s("platform.s") + _FIN_ACT + ["purchasing.export",
                                                                                "admin.audit.view", "unicorn.use",
                                                                                "layout.personal"]},
    "Finance director": {"department": "Finance", "system": False,
                         "description": "Finance in full, including closing and thresholds.",
                         "permissions": _FIN_ALL + _EXEC + _FIN_ACT + ["layout.personal"]},
    "Chief accountant": {"department": "Finance", "system": False,
                         "description": "Statements, working capital, fiscal, fixed assets and closing; no "
                                        "thresholds.",
                         "permissions": _s("finance.s", but=("finance.s.settings",))
                                        + ["finance.export", "finance.actions.manage", "finance.closing.manage"]},
    "Accountant": {"department": "Finance", "system": False,
                   "description": "Statements, G/L, Kartela, fiscal and fixed assets; actions; no closing.",
                   "permissions": _s("finance.s.pl", "finance.s.gl", "finance.s.kartela", "finance.s.fiscal",
                                     "finance.s.assets", "finance.s.actions", "finance.s.exceptions", "finance.s.gaps")
                                  + ["finance.export", "finance.actions.manage"]},
    "AP & AR accountant": {"department": "Finance", "system": False,
                           "description": "Payables and receivables: Kartela AP and AR, suppliers & stock, fiscal "
                                          "purchases and sales, invoices without PO, sales.",
                           "permissions": ["finance.s.kartela.ap", "finance.s.kartela.ar", "finance.s.kartela.sup",
                                           "finance.s.kartela.loc", "finance.s.kartela.today",
                                           "finance.s.fiscal.fiscal", "finance.s.fiscal.fsales",
                                           "finance.s.sales.main", "finance.s.sales.loc", "finance.s.actions",
                                           "purchasing.s.compliance", "purchasing.s.reqs.invoices",
                                           "finance.export", "finance.actions.manage"]},
    "Treasury": {"department": "Finance", "system": False,
                 "description": "Cash & bank, cash flow and treasury, payables and receivables to read.",
                 "permissions": _s("finance.s.cash") + ["finance.s.pl.treasury", "finance.s.kartela.ap",
                                                        "finance.s.kartela.ar", "finance.export"]},
    # --- purchasing
    "Purchasing director": {"department": "Purchasing", "system": False,
                            "description": "Purchasing in full, including thresholds; inventory to read.",
                            "permissions": _PUR_ALL + _s("finance.s.inventory") + _PUR_ACT
                                           + ["layout.personal"]},
    "Purchasing manager": {"department": "Purchasing", "system": False,
                           "description": "Purchasing screens and actions, savings and contracts; no thresholds.",
                           "permissions": _s("purchasing.s", but=("purchasing.s.settings",))
                                          + _s("finance.s.inventory.sku", "finance.s.inventory.loc")
                                          + ["purchasing.export", "purchasing.actions.manage"]},
    "Purchasing coordinator": {"department": "Purchasing", "system": False,
                               "description": "Requisitions, orders, invoices without PO, exceptions.",
                               "permissions": _s("purchasing.s.overview", "purchasing.s.reqs",
                                                 "purchasing.s.exceptions", "purchasing.s.compliance",
                                                 "purchasing.s.feed", "purchasing.s.actions")
                                              + ["purchasing.export", "purchasing.actions.manage"]},
    "Buyer": {"department": "Purchasing", "system": False,
              "description": "Buying, prices and contracts, supplier performance; actions.",
              "permissions": _s("purchasing.s.overview", "purchasing.s.reqs", "purchasing.s.compare",
                                "purchasing.s.suppliers", "purchasing.s.actions")
                             + ["purchasing.export", "purchasing.actions.manage"]},
    "Cost controller": {"department": "Purchasing", "system": False,
                        "description": "Prices, savings, inventory, recipes, movement and SKU planning.",
                        "permissions": _s("purchasing.s.compare", "purchasing.s.overview", "purchasing.s.exceptions")
                                       + _s("finance.s.inventory") + ["finance.s.budget.budget",
                                                                       "platform.s.executive.food"]
                                       + ["finance.export", "purchasing.export"]},
    "Stock manager": {"department": "Purchasing", "system": False,
                      "description": "Inventory, movement, SKU planning, requisitions and orders at the locations.",
                      "permissions": _s("finance.s.inventory", but=("finance.s.inventory.stockwc",))
                                     + _s("purchasing.s.reqs.reqs", "purchasing.s.reqs.po") + ["finance.export"]},
    "Inputer": {"department": "Purchasing", "system": False,
                "description": "Invoice entry in BC: invoices, invoices without PO, posting follow-up, fiscal "
                               "purchases.",
                "permissions": ["purchasing.s.reqs.invoices", "purchasing.s.compliance", "purchasing.s.feed",
                                "finance.s.fiscal.fiscal"]},
    # --- sales
    "Sales & operations director": {"department": "Sales", "system": False,
                                    "description": "Sales of every restaurant, location intelligence, budget, "
                                                   "the executive dashboard.",
                                    "permissions": _s("finance.s.sales", "finance.s.overview.loc",
                                                      "finance.s.overview.today", "finance.s.budget")
                                                   + _EXEC + ["finance.export", "layout.personal"]},
    "Unit manager": {"department": "Sales", "system": False,
                     "description": "One restaurant: its sales, today, stock and SKU planning (give the account "
                                    "its locations).",
                     "permissions": ["finance.s.sales.loc", "finance.s.sales.today", "finance.s.overview.today",
                                     "finance.s.inventory.loc", "finance.s.inventory.sku",
                                     "finance.s.inventory.today", "purchasing.s.overview.overview",
                                     "purchasing.s.reqs.reqs", "purchasing.s.reqs.po"]},
    "Sales analyst": {"department": "Sales", "system": False,
                      "description": "Sales, location intelligence and the sales tabs of the executive dashboard.",
                      "permissions": _s("finance.s.sales", "finance.s.overview.loc")
                                     + ["platform.s.executive.sales", "platform.s.executive.locations",
                                        "finance.export"]},
    # --- HR
    "HR manager": {"department": "HR", "system": False,
                   "description": "Labour cost of every restaurant (executive dashboard); payroll is not in pulse.",
                   "permissions": ["platform.s.executive.labor", "platform.s.executive.locations"]},
    "Payroll specialist": {"department": "HR", "system": False,
                           "description": "Labour cost (executive dashboard).",
                           "permissions": ["platform.s.executive.labor"]},
    # --- read only
    "Viewer": {"department": "Management", "system": False,
               "description": "Read only, both towers, including exports.",
               "permissions": _FIN_ALL + _PUR_ALL + ["finance.export", "purchasing.export"]},
}
# Screens added after the roles were stored: {new screen: the screen whose holders get it once}.
ADDED_SCREENS = {"purchasing.s.purchasers": "purchasing.s.users"}

DEPARTMENTS = ["Management", "Finance", "Purchasing", "Sales", "HR", "IT", "Other"]

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

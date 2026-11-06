"""The screens of pulse, as a tree the roles are built from: tower → menu item → tabs.

A role holds screen permissions ("finance.s.kartela.ap", "purchasing.s.compare.savings", "platform.s.executive.sales");
the data rights the towers' servers check ("finance.ap.view", "finance.view" ...) follow from the screens a role
holds (GRANTS below), so a role is built by ticking screens and actions, never data rights.

Every screen also says whether it shows the whole company ("company") or can be cut to locations ("location").
A person whose access is limited to some locations of a company (Admin → Accounts → access) does not get the
company screens of that company: they are hidden in the menus and refused by the servers.

The tab keys are the data-t of the tower's tab bars; menu keys are the data-view of the menu buttons; layout.js uses
them to hide what a role does not hold.
"""

# (menu key, label, menu group, tab bar id or None, [(tab key, label, kind, grants)], kind, grants)
# kind: "company" or "location"; grants: data rights the screen needs on the server (besides the tower's view).
FINANCE = [
    ("overview", "Executive overview", "Overview", "ovFTabs", [
        ("overview", "Overview", "company", ["finance.pl.view", "finance.bs.view", "finance.cash.view",
                                             "finance.ap.view", "finance.ar.view"]),
        ("loc", "Location intelligence", "location", ["finance.pl.view"]),
        ("today", "Today", "location", [])]),
    ("group", "Group view", "Overview", None, [], "company", ["finance.pl.view", "finance.bs.view"]),
    ("sales", "Sales", "Overview", "slTabs", [
        ("main", "Sales", "location", []),
        ("loc", "Location intelligence", "location", []),
        ("prices", "Menu prices", "location", []),
        ("menu", "Menu engineering", "location", []),
        ("chanprofit", "Channel profitability", "location", []),
        ("today", "Today", "location", [])]),
    ("pl", "Financial statements", "Statements", "stTabs", [
        ("pl", "Profit & loss", "company", ["finance.pl.view"]),
        ("bs", "Balance sheet", "company", ["finance.bs.view"]),
        ("liquidity", "Liquidity & capital", "company", ["finance.bs.view"]),
        ("treasury", "Cash flow & treasury", "company", ["finance.treasury.view", "finance.cash.view"]),
        ("charts", "P&L charts", "company", ["finance.pl.view"])]),
    ("gl", "G/L entries", "Statements", None, [], "company", ["finance.gl.view"]),
    ("cash", "Cash & bank", "Working capital", "clTabs", [
        ("main", "Cash & bank", "company", ["finance.cash.view"]),
        ("loc", "Location intelligence", "location", ["finance.cash.view"]),
        ("today", "Today", "location", ["finance.cash.view"])]),
    ("kartela", "Kartela", "Working capital", "kaTabs", [
        ("ap", "Accounts payable", "company", ["finance.ap.view"]),
        ("ar", "Accounts receivable", "company", ["finance.ar.view"]),
        ("loc", "Location intelligence", "location", ["finance.ap.view", "finance.ar.view"]),
        ("sup", "Suppliers & stock", "company", ["finance.ap.view"]),
        ("today", "Today", "location", ["finance.ap.view", "finance.ar.view"])]),
    ("fiscal", "Fiscal vs BC", "Working capital", "fbTabs", [
        ("fiscal", "Fiscal purchases vs BC", "company", ["finance.ap.view"]),
        ("fsales", "Fiscal sales vs BC", "company", ["finance.ar.view"])]),
    ("inventory", "Inventory", "Working capital", "invTabs", [
        ("overview", "Overview", "company", ["finance.inventory.view"]),
        ("loc", "Location intelligence", "location", ["finance.inventory.view"]),
        ("sku", "SKU planning", "location", ["finance.inventory.view"]),
        ("typology", "Typology & turnover", "location", ["finance.inventory.view"]),
        ("recipes", "Recipe integrity", "company", ["finance.inventory.view"]),
        ("discharge", "Received vs discharged", "location", ["finance.inventory.view"]),
        ("itemmaster", "Item master control", "company", ["finance.inventory.view"]),
        ("movement", "Movement & dead stock", "location", ["finance.inventory.view"]),
        ("stockcount", "Stock count analysis", "location", ["finance.inventory.view"]),
        ("stockwc", "Stock & working capital", "company", ["finance.inventory.view", "purchasing.view"]),
        ("invcharts", "Inventory charts", "location", ["finance.inventory.view"]),
        ("today", "Today", "location", ["finance.inventory.view"])]),
    ("budget", "Budget & forecast", "Planning", "bgTabs", [
        ("budget", "Budget & forecast", "company", ["finance.pl.view"]),
        ("loc", "Location intelligence", "location", ["finance.pl.view"]),
        ("today", "Today", "location", ["finance.pl.view"])]),
    ("assets", "Fixed assets", "Planning", "faTabs", [
        ("main", "Fixed assets", "company", ["finance.assets.view"]),
        ("loc", "Location intelligence", "location", ["finance.assets.view"]),
        ("today", "Today", "location", ["finance.assets.view"])]),
    ("closing", "Month-end close", "Control", None, [], "company", []),
    ("actions", "Action center", "Control", None, [], "company", []),
    ("exceptions", "Exceptions", "Control", None, [], "company", []),
    ("register", "Exceptions register", "Control", None, [], "location", []),
    ("salesrec", "Simphony ↔ BC", "Control", None, [], "location", []),
    ("receipts", "Takings POS ↔ BC", "Control", None, [], "location", ["finance.cash.view"]),
    ("gaps", "Data quality & reconciliation", "System", None, [], "company", []),
    ("settings", "Settings", "System", None, [], "company", []),
]

PURCHASING = [
    ("overview", "Overview", "Overview", "ovTabs", [
        ("overview", "Overview", "location", ["purchasing.spend.view"]),
        ("loc", "Location intelligence", "location", ["purchasing.spend.view"]),
        ("today", "Today", "location", ["purchasing.spend.view"])]),
    ("live", "Live operations", "Overview", None, [], "location", ["purchasing.spend.view"]),
    ("reqs", "Buying", "Buying & prices", "byTabs", [
        ("reqs", "Requisitions", "location", ["purchasing.orders.view"]),
        ("po", "Purchase orders", "location", ["purchasing.orders.view"]),
        ("invoices", "Invoices", "location", ["purchasing.invoices.view"]),
        ("streams", "Purchase types", "location", ["purchasing.spend.view"]),
        ("services", "Services & CAPEX", "location", ["purchasing.spend.view"]),
        ("buyloc", "Location intelligence", "location", ["purchasing.spend.view"])]),
    ("compare", "Prices & contracts", "Buying & prices", "pcTabs", [
        ("compare", "Comparisons", "location", ["purchasing.prices.view"]),
        ("contracts", "Contract prices", "location", ["purchasing.prices.view"]),
        ("savings", "Price savings", "location", ["purchasing.prices.view"]),
        ("prices", "Price changes", "location", ["purchasing.prices.view"]),
        ("locations", "Location prices", "location", ["purchasing.prices.view"])]),
    ("actions", "Action center", "Control", None, [], "location", []),
    ("exceptions", "Exceptions", "Control", "exTabs", [
        ("list", "Exceptions", "location", ["purchasing.quality.view"]),
        ("loc", "Location intelligence", "location", ["purchasing.quality.view"]),
        ("today", "Today", "location", ["purchasing.quality.view"])]),
    ("compliance", "Invoices without PO", "Control", None, [], "company", ["purchasing.invoices.view"]),
    ("feed", "Posting follow-up", "Control", None, [], "location", ["purchasing.invoices.view"]),
    ("suppliers", "Supplier performance", "Performance", "spTabs", [
        ("suppliers", "Supplier performance", "location", ["purchasing.spend.view"]),
        ("suploc", "Location intelligence", "location", ["purchasing.spend.view"]),
        ("supco", "Whole company", "location", ["purchasing.spend.view"])]),
    ("purchasers", "Purchaser performance", "Performance", None, [], "company", ["purchasing.spend.view"]),
    ("users", "User performance", "Performance", None, [], "company", []),
    ("reports", "Reports", "Performance", None, [], "location", []),
    ("quality", "Data quality", "System", None, [], "location", ["purchasing.quality.view"]),
    ("gaps", "Data availability", "System", None, [], "company", []),
    ("settings", "Settings", "System", None, [], "company", []),
]

PLATFORM = [
    ("control", "Control center", "Platform", None, [], "company", []),
    ("executive", "Executive dashboard", "Platform", "tabs", [
        ("overview", "Executive KPIs", "company", ["finance.view", "finance.pl.view", "finance.bs.view",
                                                   "purchasing.view"]),
        ("sales", "Sales", "company", ["finance.view", "finance.pl.view"]),
        ("labor", "Labour cost", "company", ["finance.view", "finance.pl.view"]),
        ("food", "Food cost", "company", ["finance.view", "finance.pl.view"]),
        ("budget", "Budget", "company", ["finance.view", "finance.pl.view"]),
        ("balance", "Balance & capital", "company", ["finance.view", "finance.bs.view"]),
        ("purchasing", "Purchasing", "company", ["purchasing.view"]),
        ("locations", "Location intelligence", "location", ["finance.view"])]),
]

TOWERS = (("finance", "Finance & Inventory", FINANCE), ("purchasing", "Purchasing", PURCHASING),
          ("platform", "Platform", PLATFORM))


def catalogue():
    """[{app, label, menus: [{key, label, group, bar, kind, tabs: [{key, label, kind}]}]}] for the admin and layout.js."""
    out = []
    for app, label, menus in TOWERS:
        ms = []
        for key, mlabel, group, bar, tabs, *rest in menus:
            m = {"key": f"{app}.s.{key}", "view": key, "label": mlabel, "group": group, "bar": bar,
                 "kind": rest[0] if rest else None,
                 "tabs": [{"key": f"{app}.s.{key}.{t[0]}", "tab": t[0], "label": t[1], "kind": t[2]} for t in tabs]}
            ms.append(m)
        out.append({"app": app, "label": label, "menus": ms})
    return out


def _all():
    """{permission: (label, kind, grants)} of every screen that can be ticked (menus without tabs, and tabs)."""
    out = {}
    for app, label, menus in TOWERS:
        for key, mlabel, group, bar, tabs, *rest in menus:
            if tabs:
                for t in tabs:
                    out[f"{app}.s.{key}.{t[0]}"] = (f"{label} › {mlabel} › {t[1]}", t[2], list(t[3]))
            else:
                out[f"{app}.s.{key}"] = (f"{label} › {mlabel}", rest[0], list(rest[1]))
    return out


SCREENS = _all()


def grants(perms):
    """The data rights that follow from the screens held: the tower's view right for each tower with a screen, and
    the screens' own grants."""
    out = set()
    for p in perms:
        s = SCREENS.get(p)
        if not s:
            continue
        app = p.split(".", 1)[0]
        if app in ("finance", "purchasing"):
            out.add(f"{app}.view")
        out.update(s[2])
    return out


def company_screens():
    return {p for p, s in SCREENS.items() if s[1] == "company"}

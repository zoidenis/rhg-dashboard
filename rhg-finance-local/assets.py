"""Fixed assets and CAPEX for the period.

The FA ledger holds hundreds of thousands of entries across the years, so only
the selected period is read. Net book value per asset needs the full history and
the asset card, neither of which is published; that stays under Data availability.
"""
from collections import defaultdict

ACQUISITION = "Acquisition Cost"
DEPRECIATION = "Depreciation"
DISPOSAL = "Proceeds on Disposal"


def _f(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def build(rows, settings, period):
    by_type = defaultdict(float)
    by_asset = defaultdict(lambda: {"description": "", "class": "", "acquisition": 0.0,
                                    "depreciation": 0.0, "other": 0.0, "entries": 0, "location": ""})
    by_class = defaultdict(lambda: {"acquisition": 0.0, "depreciation": 0.0, "assets": set()})
    entries = []
    for r in rows or []:
        amount = _f(r.get("Amount_LCY"))
        ptype = (r.get("FA_Posting_Type") or "").strip() or "(none)"
        by_type[ptype] += amount
        a = by_asset[r.get("FA_No")]
        a["description"] = r.get("FA_Description") or r.get("FA_No")
        a["class"] = r.get("FA_Class_Code") or ""
        a["location"] = r.get("FA_Location_Code") or r.get("Location_Code") or ""
        a["entries"] += 1
        if ptype == ACQUISITION:
            a["acquisition"] += amount
        elif ptype == DEPRECIATION:
            a["depreciation"] += amount
        else:
            a["other"] += amount
        c = by_class[r.get("FA_Class_Code") or "(no class)"]
        c["assets"].add(r.get("FA_No"))
        if ptype == ACQUISITION:
            c["acquisition"] += amount
        elif ptype == DEPRECIATION:
            c["depreciation"] += amount
        entries.append({"asset": r.get("FA_No"), "description": r.get("FA_Description"),
                        "class": r.get("FA_Class_Code"), "type": ptype, "posting": r.get("Posting_Date"),
                        "document": r.get("Document_No"), "book": r.get("Depreciation_Book_Code"),
                        "location": r.get("FA_Location_Code") or r.get("Location_Code") or "",
                        "amount": amount})
    entries.sort(key=lambda e: -abs(e["amount"]))
    assets = sorted(({"asset": k, **v} for k, v in by_asset.items()),
                    key=lambda a: -(abs(a["acquisition"]) + abs(a["depreciation"])))
    classes = sorted(({"class": k, "acquisition": v["acquisition"], "depreciation": v["depreciation"],
                       "assets": len(v["assets"])} for k, v in by_class.items()),
                     key=lambda c: -abs(c["acquisition"]))
    additions = by_type.get(ACQUISITION, 0.0)
    depreciation = by_type.get(DEPRECIATION, 0.0)

    exceptions = []
    th = settings["thresholds"]
    if rows and depreciation == 0:
        exceptions.append({"code": f"fa-nodep-{period}", "severity": "warning", "area": "Fixed assets",
                           "rule": "No depreciation posted in the period",
                           "title": "Fixed assets moved but no depreciation was posted",
                           "detail": "Acquisitions or other entries exist for this period while the "
                                     "depreciation run has not been posted.",
                           "impact": abs(additions), "document": "",
                           "next_action": "Run and post depreciation before the period is closed.",
                           "drill": {"kind": "assets", "key": ""}})
    no_location = [a for a in assets if not a["location"] and a["acquisition"] >= th["materiality"]]
    if no_location:
        exceptions.append({"code": f"fa-loc-{period}", "severity": "info", "area": "Fixed assets",
                           "rule": "Asset acquired without a location",
                           "title": f"{len(no_location)} acquisitions carry no FA location",
                           "detail": "Without a location an asset cannot be traced to a venue or a custodian.",
                           "impact": sum(a["acquisition"] for a in no_location), "document": "",
                           "next_action": "Set the FA location on the asset card.",
                           "drill": {"kind": "assets", "key": ""}})
    return {
        "totals": {"additions": additions, "depreciation": depreciation,
                   "other": sum(v for k, v in by_type.items() if k not in (ACQUISITION, DEPRECIATION)),
                   "assets_touched": len(by_asset)},
        "by_type": sorted(({"type": k, "amount": v} for k, v in by_type.items()), key=lambda x: -abs(x["amount"])),
        "classes": classes[:25], "assets": assets[:60], "entries": entries[:300],
        "exceptions": exceptions,
        "note": "Only the selected period is read from the FA ledger. Net book value, useful life and "
                "custodian need the asset card, which is not published; see Data availability.",
    }

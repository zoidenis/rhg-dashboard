"""EMC audit: a read of the Simphony configuration, kept as one file for review.

Every get call of the Configuration and Content API (CCAPI) an audit needs, read page by page through the
account of Control → EMC privileges (emcpriv.py), with the BI API's dimensions beside them. Only get calls:
nothing is added, updated or deleted in Simphony. Employees keep only the fields of EMC privileges (no PIN, no
IDs, no payroll); the audit trail keeps who changed what, when, from and to.

The read runs in the background (several minutes on a large enterprise) and is written to
data/emcaudit_raw.json.gz, which a person holding Settings downloads. The checks are made on that file.
"""
import gzip
import json
import threading
import time
import traceback
from datetime import datetime, timedelta

import emcpriv
import simphony
import store

FILE = store.DATA_DIR / "emcaudit_raw.json.gz"
TRAIL_DAYS = 180
TRAIL_MAX = 30000

# (name, [paths, the first that answers], includeAll or None)
SETS = [
    ("locations", ["v2/hierarchy/getLocations", "v1/hierarchy/getLocations"], "detailed"),
    ("zones", ["v2/hierarchy/getZones", "v1/hierarchy/getZones"], None),
    ("revenue_centers", ["v2/hierarchy/getRevenueCenters", "v1/hierarchy/getRevenueCenters"], "detailed"),
    ("rvc_parameters", ["v2/hierarchy/getRevenueCenterParameters", "v1/hierarchy/getRevenueCenterParameters"], "detailed"),
    ("location_parameters", ["v2/hierarchy/getLocationParameters", "v1/hierarchy/getLocationParameters"], "detailed"),
    ("workstations", ["v2/devices/getWorkstations", "v1/devices/getWorkstations"], "detailed"),
    ("printers", ["v2/devices/getPrinters", "v1/devices/getprinters"], "basic"),
    ("order_devices", ["v2/devices/getOrderDevices", "v1/devices/getOrderDevices"], "basic"),
    ("kitchen_displays", ["v2/devices/getKitchenDisplays", "v1/devices/getKitchenDisplays"], "basic"),
    ("major_groups", ["v2/menuItems/getMajorGroups", "v1/menuItems/getMajorGroups"], "detailed"),
    ("family_groups", ["v2/menuItems/getFamilyGroups", "v1/menuItems/getFamilyGroups"], "detailed"),
    ("menu_item_classes", ["v2/menuItems/getMenuItemClasses", "v1/menuItems/getMenuItemClasses"], "detailed"),
    ("menu_item_masters", ["v2/menuItems/getMenuItemMasters", "v1/menuItems/getMenuItemMasters"], "basic"),
    ("menu_item_definitions", ["v2/menuItems/getMenuItemDefinitions", "v1/menuItems/getMenuItemDefinitions"], "basic"),
    ("menu_item_prices", ["v2/menuItems/getMenuItemPrices", "v1/menuItems/getMenuItemPrices"], "basic"),
    ("combo_meals", ["v2/menuItems/getComboMealsLite", "v1/menuitems/getcombomeals"], None),
    ("discounts", ["v2/sales/getDiscounts", "v1/sales/getDiscounts"], "detailed"),
    ("service_charges", ["v2/sales/getServiceCharges", "v1/sales/getServicecharges"], "detailed"),
    ("tender_media", ["v2/sales/getTenderMedia", "v1/sales/getTenderMedia"], "detailed"),
    ("tax_rates", ["v2/sales/getTaxRates", "v1/sales/gettaxrates"], "detailed"),
    ("tax_classes", ["v2/sales/getTaxClasses", "v1/sales/gettaxclasses"], "detailed"),
    ("tax_parameters", ["v2/sales/getTaxParameters", "v1/sales/gettaxparameters"], "detailed"),
    ("discount_itemizers", ["v2/sales/getDiscountItemizers", "v1/sales/getdiscountitemizers"], None),
    ("sales_itemizers", ["v2/setup/getSalesItemizers", "v1/setup/getsalesitemizers"], None),
    ("order_type_names", ["v2/setup/getOrderTypeNames", "v1/setup/getordertypenames"], None),
    ("order_type_parameters", ["v2/setup/getOrderTypeParameters", "v1/setup/getOrderTypeParameters"], "detailed"),
    ("order_channels", ["v2/setup/getOrderChannels", "v1/setup/getorderchannels"], None),
    ("order_channel_parameters", ["v2/setup/getOrderChannelParameters", "v1/setup/getorderchannelparameters"], "detailed"),
    ("menu_levels", ["v2/setup/getMenuLevels", "v1/setup/getmenulevels"], None),
    ("serving_periods", ["v2/setup/getServingPeriods"], None),
    ("reason_codes", ["v2/setup/getReasonCodes"], None),
    ("currencies", ["v2/setup/getCurrencies", "v1/setup/getCurrencies"], "detailed"),
    ("tender_parameters", ["v2/setup/getTenderParameters"], "detailed"),
    ("control_parameters", ["v2/setup/getControlParameters"], "detailed"),
    ("effectivity_groups", ["v2/setup/getEffectivityGroups", "v1/setup/geteffectivitygroups"], None),
    ("employee_classes", ["v2/employees/getClasses", "v1/employees/getClasses"], "detailed"),
    ("job_codes", ["v2/employees/properties/getJobCodes", "v1/employees/properties/getJobCodes"], None),
    ("roles", ["v1/employees/getRoles", "v2/employees/getRoles"], "detail"),
]

_state = {"running": False, "step": "", "started": "", "finished": "", "error": "", "summary": None}
_lock = threading.Lock()


def _read_set(paths, inc, progress, name):
    body = {"includeAll": inc} if inc else {}
    try:
        return emcpriv._all(paths, body, progress, name)
    except emcpriv.Error as exc:
        if inc and " answered 400" in str(exc):              # this release does not take includeAll here
            return emcpriv._all(paths, {}, progress, name)
        raise


def _trail(progress):
    """The audit trail of the last TRAIL_DAYS days, newest first, until older or TRAIL_MAX rows."""
    since = (datetime.utcnow() - timedelta(days=TRAIL_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
    out, offset = [], 0
    while len(out) < TRAIL_MAX:
        j = emcpriv._post("v2/getAuditTrail", {"orderBy": "auditTime:desc", "offset": offset, "limit": emcpriv.PAGE})
        items = [x for x in ((j or {}).get("items") or []) if isinstance(x, dict)]
        keep = [x for x in items if str(x.get("auditTime") or "") >= since]
        out += keep
        progress(f"audit trail: {len(out):,}")
        if len(keep) < len(items) or not (j or {}).get("hasMore") or not items:
            break
        offset += len(items)
    return out


def collect(progress):
    data, meta = {}, {}
    for name, paths, inc in SETS:
        t0 = time.time()
        try:
            rows = _read_set(paths, inc, progress, name)
            data[name] = rows
            meta[name] = {"rows": len(rows), "seconds": round(time.time() - t0, 1)}
        except Exception as exc:                                 # noqa: BLE001
            meta[name] = {"error": str(exc)[:300]}
    try:
        data["employees"] = emcpriv._all(["v1/employees/getEmployees", "v2/employees/getEmployees2"],
                                         {"include": ",".join(emcpriv.EMP_KEEP)}, progress, "employees", emcpriv._emp)
        meta["employees"] = {"rows": len(data["employees"])}
    except Exception as exc:                                     # noqa: BLE001
        meta["employees"] = {"error": str(exc)[:300]}
    try:
        data["audit_trail"] = _trail(progress)
        meta["audit_trail"] = {"rows": len(data["audit_trail"]), "days": TRAIL_DAYS}
    except Exception as exc:                                     # noqa: BLE001
        meta["audit_trail"] = {"error": str(exc)[:300]}
    # the BI API's view of each location: order types, channels, revenue centres, menu items (sales flags)
    bi = {}
    try:
        for l in simphony.locations():
            ref = l["locRef"]
            progress(f"BI dimensions: {ref}")
            one = {"location": l}
            for api, key in (("getOrderTypeDimensions", "orderTypes"), ("getOrderChannelDimensions", "orderChannels"),
                             ("getRevenueCenterDimensions", "revenueCenters"), ("getMenuItemDimensions", "menuItems"),
                             ("getDiscountDimensions", "discounts"), ("getTenderMediaDimensions", "tenderMedias"),
                             ("getServiceChargeDimensions", "serviceCharges"), ("getTaxDimensions", "taxes")):
                try:
                    one[key] = (simphony.call(api, {"locRef": ref}) or {}).get(key) or []
                except Exception as exc:                         # noqa: BLE001
                    one[key] = {"error": str(exc)[:200]}
            bi[ref] = one
        meta["bi"] = {"locations": len(bi)}
    except Exception as exc:                                     # noqa: BLE001
        meta["bi"] = {"error": str(exc)[:300]}
    data["bi"] = bi
    return data, meta


def _run(actor):
    def progress(s):
        with _lock:
            _state["step"] = s
    try:
        data, meta = collect(progress)
        progress("writing the file")
        tmp = FILE.with_suffix(".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            json.dump({"read_at": datetime.now().isoformat(timespec="seconds"), "by": actor, "meta": meta, "data": data},
                      f, ensure_ascii=False, default=str)
        tmp.replace(FILE)
        with _lock:
            _state.update(running=False, step="done", finished=datetime.now().isoformat(timespec="seconds"),
                          summary=meta, error="")
    except Exception as exc:                                     # noqa: BLE001
        traceback.print_exc()
        with _lock:
            _state.update(running=False, step="failed", error=str(exc)[:400])


def start(actor):
    if not emcpriv.info()["configured"]:
        return {"error": "The Configuration API account is not set yet (Control → EMC privileges → Connection)."}
    with _lock:
        if _state["running"]:
            return dict(_state)
        _state.update(running=True, step="starting", started=datetime.now().isoformat(timespec="seconds"),
                      finished="", error="", summary=None)
    threading.Thread(target=_run, args=(actor,), daemon=True).start()
    return status()


def status():
    with _lock:
        s = dict(_state)
    s["file"] = FILE.exists()
    if FILE.exists():
        st = FILE.stat()
        s["file_bytes"] = st.st_size
        s["file_at"] = datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")
    return s

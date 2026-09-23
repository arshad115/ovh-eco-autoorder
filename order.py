#!/usr/bin/python3

import json
import os
import re
import sys
import time
import fcntl
import logging
import threading
from datetime import datetime, timezone
from fetcher import atomic_write_text, fetch_catalog

logging.basicConfig(format='%(asctime)s - %(message)s', level=logging.DEBUG)

user_preferences = {}
preferences_ready = False
preferences_lock = threading.RLock()
all_dc = []
order_client = None
availability_client = None
instance_lock_handle = None
ovh = None

# unknown, comingSoon, and unavailable are not orderable.
# Datacenter array order is the preference. Delivery time is only a maximum.
_HOUR_DELAY = re.compile(r"^(\d+)H$")
DEFAULT_MAX_DELIVERY_HOURS = 72


def availability_hours(status):
    if status in ("1H-high", "1H-low"):
        return 1
    if isinstance(status, str):
        match = _HOUR_DELAY.fullmatch(status)
        if match:
            hours = int(match.group(1))
            if hours > 0:
                return hours
    return None


def load_preferences():
    global user_preferences, preferences_ready
    try:
        with open("preferences.json") as ff:
            loaded = json.load(ff)
    except Exception:
        logging.exception("Error opening preferences.json. Refusing to start so the file is not overwritten.")
        sys.exit(1)
    if not isinstance(loaded, dict) or "user_servers" not in loaded or "subsidiary" not in loaded:
        logging.error("preferences.json must contain subsidiary and user_servers. Refusing to start.")
        sys.exit(1)
    user_preferences = loaded
    preferences_ready = True
    logging.debug("Success opening preferences.json")


def save_preferences():
    global user_preferences
    if not preferences_ready:
        logging.error("Refusing to save preferences because they were not loaded.")
        return
    with preferences_lock:
        text = json.dumps(user_preferences, default=str, indent=2)
        atomic_write_text("preferences.json", text)
    logging.debug("Saved settings file.")


def acquire_instance_lock():
    global instance_lock_handle
    path = "preferences.lock"
    try:
        handle = open(path, "a+")
    except PermissionError:
        handle = open(path, "r")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logging.error("Another autoorder process holds preferences.lock. Refusing to start.")
        sys.exit(1)
    instance_lock_handle = handle
    return handle


def fetch_dcs():
    global all_dc, availability_client
    while True:
        try:
            with preferences_lock:
                plan_codes = []
                for server in user_preferences.get("user_servers", []):
                    code = server.get("planCode")
                    if code and code not in plan_codes:
                        plan_codes.append(code)
            merged = []
            for code in plan_codes:
                part = availability_client.get("/dedicated/server/datacenter/availabilities", planCode=code)
                if isinstance(part, list):
                    merged.extend(part)
            all_dc = merged
            logging.debug("Fetched availabilities: %s", len(all_dc))
        except Exception as ex:
            logging.warning("Datacenter fetching failed: %s", ex)
        time.sleep(3)


def datacenter_status(all_avail, desired, fqn):
    logging.debug("Check availability for FQN %s in %s", fqn, desired)
    if not all_avail:
        return None
    for entry in all_avail:
        if entry.get("fqn") != fqn:
            continue
        logging.debug("FQN found in availabilities.")
        for dc in entry.get("datacenters", []):
            if dc.get("datacenter") == desired:
                status = dc.get("availability")
                logging.debug("Datacenter %s availability is %s.", desired, status)
                return status
        return None
    return None


def next_cart_expiration_date():
    current_time = datetime.now()
    if current_time.month == 12:
        one_month_later = current_time.replace(month=1)
        one_month_later = one_month_later.replace(year = current_time.year + 1)
    else:
        one_month_later = current_time.replace(day=27)
        one_month_later = one_month_later.replace(month=current_time.month + 1)
    print(one_month_later)
    out_string = one_month_later.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
    logging.debug("New expiration generated "+out_string)
    return out_string

def init_cart(client):
    logging.debug("Creating a new cart.")
    global user_preferences
    result = client.post("/order/cart",
        expire = next_cart_expiration_date(), # Time of expiration of the cart (type: string)
        ovhSubsidiary = user_preferences["subsidiary"], # OVH Subsidiary where you want to order (type: nichandle.OvhSubsidiaryEnum)
        description = "Cart set up by OVH Eco Autoorder", # Description of your cart (type: string)
    )
    time.sleep(1)
    res2 = client.post("/order/cart/"+result["cartId"]+"/assign")
    logging.debug("Cart created and assigned to your OVH account.")
    return result

def validate_cart(client, cartId, item_ids):
    logging.debug("Validating cart "+cartId)
    logging.debug("Local items in cart "+str(len(item_ids)))
    result={}
    try:
        result = client.get("/order/cart/"+cartId)
        logging.debug("Online items in cart "+str(len(result["items"])))
    except ovh.exceptions.BadParametersError as ex:
        logging.debug("Bad cart ID...")
        return False
    except ovh.exceptions.ResourceNotFoundError as ex:
        logging.debug(ex.args[0])
        return False
    if sorted(result["items"]) != sorted(item_ids):
        logging.debug("Local and online carts are not match!")
        return False
    logging.debug("Local and online carts matching.")
    return True

def is_cart_expired(expiration):
    exp_date = datetime.fromisoformat(str(expiration).replace("Z", "+00:00"))
    if exp_date.tzinfo is None:
        exp_date = exp_date.replace(tzinfo=timezone.utc)
    difference_seconds = (exp_date - datetime.now(timezone.utc)).total_seconds()
    return difference_seconds <= 120

def fill_cart(client, item, dc):
    result = None
    dedicated_datacenter=dc["dedicated_datacenter"]
    region=dc["region"]
    logging.info("Starting fill cart. The plancode is "+item["planCode"]+" fqn is "+item["fqn"]+" and datacenter is "+dedicated_datacenter)
    try:
        result = client.post("/order/cart/"+item["dc_carts"][dedicated_datacenter]["cartId"]+"/eco",
            planCode = item["planCode"], # Identifier of the offer (type: string)
            pricingMode = "default", # Pricing mode selected for the purchase of the product (type: string)
            quantity = item["qty"], # Quantity of product desired (type: integer)
            duration = "P1M", # Duration selected for the purchase of the product (type: string)
        )
        logging.debug("Server planCode placement ok "+item["planCode"])
    except ovh.exceptions.BadParametersError as ex:
        logging.info("Bad parameter when placing server planCode %s: %s ",item["planCode"],ex,)
        return False
    itemId=result["itemId"]
    server_itemId = result["itemId"]
    logging.debug("Server item ID in cart is "+str(server_itemId))
    item["dc_carts"][dedicated_datacenter]["itemIds"]=[]
    item["dc_carts"][dedicated_datacenter]["itemIds"].append(itemId)
    tmplabels=dict(item["labels"])
    tmplabels["dedicated_datacenter"]=dedicated_datacenter
    tmplabels["region"]=region
    logging.debug("Placing items.")
    for i in tmplabels:
        logging.debug(i+" : "+tmplabels[i])
        result = client.post("/order/cart/"+item["dc_carts"][dedicated_datacenter]["cartId"]+"/item/"+str(itemId)+"/configuration",
            label = i, # Label for your configuration item (type: string)
            value = tmplabels[i], # Value or resource URL on API.OVH.COM of your configuration item (type: string)
        )

    logging.debug("Placing addon planCodes to the item "+str(server_itemId))
    for i in item["addon_planCodes"]:
    # Request body type: order.cart.GenericOptionCreation
        result = client.post("/order/cart/"+item["dc_carts"][dedicated_datacenter]["cartId"]+"/eco/options",
            duration = "P1M", # Duration selected for the purchase of the product (type: string)
            itemId = server_itemId, # Cart item to be linked (type: integer)
            planCode = i, # Identifier of the option offer (type: string)
            pricingMode = "default", # Pricing mode selected for the purchase of the product (type: string)
            quantity = item["qty"], # Quantity of product desired (type: integer)
        )
        itemId=result["itemId"]
        logging.debug("Added planCode "+i+" with ID "+str(itemId))
        item["dc_carts"][dedicated_datacenter]["itemIds"].append(itemId)

    logging.debug("Adding coupons (if any) to the cart "+str(item["dc_carts"][dedicated_datacenter]["cartId"]))
    for i in item["coupons"]:
        logging.debug("Adding coupon to cart "+i)
        result = client.post("/order/cart/"+item["dc_carts"][dedicated_datacenter]["cartId"]+"/coupon",
            coupon = i # Coupon identifier (type: string)
        )
    logging.info("OK created cart.")
    return True

def place_order(client, item, dc):
    logging.info("Running validation and order process (depend on your settings).")
    dedicated_datacenter=dc["dedicated_datacenter"]
    if item.get("order_attempted", False):
        logging.error(
            "Checkout already attempted in %s. Refusing another automatic checkout.",
            item.get("order_attempted_in"),
        )
        item["qty"] = 0
        save_preferences()
        return "latched"
    result={}
    if "skip_validate" not in item or item["skip_validate"] == False:
        logging.info("Validating the order. Check for cartId in your settings file "+item["dc_carts"][dedicated_datacenter]["cartId"]+" for more info")
        try:
            logging.info("Fetch the cart.")
            result = client.get("/order/cart/"+item["dc_carts"][dedicated_datacenter]["cartId"]+"/summary")
        except Exception:
            logging.info("Can not fetch cart!")
            return "unavailable_cart"
        logging.info("Iterating cart...")
        for i in result["details"]:
            logging.info(i["description"]+" "+i["detailType"])
            logging.info(str(i["unitPrice"]["value"])+" "+i["unitPrice"]["currencyCode"])
        logging.info("Total: "+str(result["prices"]["withoutTax"]["value"])+" "+str(result["prices"]["withoutTax"]["currencyCode"]))
        item["dc_carts"][dedicated_datacenter]["raw_cart"] = result
        if (result["prices"]["withoutTax"]["value"] > item["ceiling_price"]):
            logging.info("Too expensive in %s. Trying another datacenter if one is orderable.", dedicated_datacenter)
            return "too_expensive"
    if "place_order" in item and item["place_order"] == True:
        logging.info("placing an order with this cart.")
        # Persist the latch before POST so a crash after OVH accepts
        # cannot leave qty=1 and trigger a second autopay checkout.
        item["order_attempted"] = True
        item["order_attempted_in"] = dedicated_datacenter
        item["qty"] = 0
        save_preferences()

        order_result={}
        try:
            order_result = client.post("/order/cart/"+item["dc_carts"][dedicated_datacenter]["cartId"]+"/checkout",
                autoPayWithPreferredPaymentMethod = item["autopay"], # Indicates that order will be automatically paid with preferred payment method (type: boolean)
                waiveRetractationPeriod = False, # Indicates that order will be processed with waiving retractation period (type: boolean)
            )
            logging.info("Success! Order placed. Check the raw order in the cart for more info!")
        except ovh.exceptions.BadParametersError as ex:
            item["order_error"] = str(ex)
            logging.info(ex)
            save_preferences()
            return "latched"
        except Exception as ex:
            item["order_error"] = str(ex)
            logging.info(ex)
            save_preferences()
            return "latched"
        logging.debug("Setting additional vars.")
        item["ordered_at"]=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        item["ordered_in"]=dedicated_datacenter
        item["dc_carts"][dedicated_datacenter]["raw_order"] = order_result
        save_preferences()
        return "ordered"
    else:
        item["qty"]-=item["qty"]
    return "validated"

def cart_needs_rebuild(client, item, dedicated_datacenter):
    cart = item["dc_carts"].get(dedicated_datacenter)
    if not isinstance(cart, dict):
        return True
    item_ids = cart.get("itemIds")
    if "cartId" not in cart or "cartExpire" not in cart or not item_ids:
        return True
    try:
        if is_cart_expired(cart["cartExpire"]):
            return True
    except Exception:
        logging.exception("Could not parse cart expiry for %s.", dedicated_datacenter)
        return True
    return not validate_cart(client, cart["cartId"], item_ids)

def ensure_cart(client, item, dc):
    dedicated_datacenter = dc["dedicated_datacenter"]
    if not cart_needs_rebuild(client, item, dedicated_datacenter):
        return True
    try:
        cart = init_cart(client)
        item["dc_carts"][dedicated_datacenter] = {
            "cartId": cart["cartId"],
            "cartExpire": cart["expire"],
        }
        if not fill_cart(client, item, dc):
            item["dc_carts"].pop(dedicated_datacenter, None)
            return False
        return True
    except Exception:
        logging.exception("Cart setup failed for %s.", dedicated_datacenter)
        item["dc_carts"].pop(dedicated_datacenter, None)
        return False

def apply_catalog_addons(catalog):
    if not isinstance(catalog, dict):
        return
    updated = False
    for server in user_preferences.get("user_servers", []):
        if len(server.get("addon_planCodes") or []) >= 3:
            if server.get("fetch_catalog"):
                server["fetch_catalog"] = {}
                updated = True
            continue
        pending = server.get("fetch_catalog") or {}
        if not pending:
            continue
        catalog_item = catalog.get(server.get("fqn"))
        if not catalog_item or not catalog_item.get("catalog"):
            logging.info("FQN %s not present in catalog yet.", server.get("fqn"))
            continue
        addons = catalog_item["catalog"].get("addons") or {}
        additions = []
        complete = True
        for key, preset in pending.items():
            if preset:
                additions.append(preset)
                continue
            addon = addons.get(key)
            if isinstance(addon, dict) and addon.get("planCode"):
                additions.append(addon["planCode"])
            elif isinstance(addon, dict) and addon.get("default"):
                additions.append(addon["default"])
            else:
                complete = False
        if not complete or not additions:
            logging.info("Catalog for %s is still missing addon plan codes.", server.get("fqn"))
            continue
        server.setdefault("addon_planCodes", [])
        server["addon_planCodes"].extend(additions)
        server["fetch_catalog"] = {}
        updated = True
        logging.info("Filled addon plan codes for %s.", server.get("fqn"))
    if updated:
        save_preferences()

def availability_record_from_server(server):
    fqn = server.get("fqn") or ""
    parts = fqn.split(".")
    plan = server.get("planCode")
    if not plan or len(parts) < 3:
        return None
    return {
        "planCode": plan,
        "fqn": fqn,
        "memory": parts[1],
        "storage": parts[-1],
    }

def servers_needing_catalog(servers):
    needed = []
    for server in servers:
        if len(server.get("addon_planCodes") or []) >= 3:
            continue
        if not (server.get("fetch_catalog") or {}):
            continue
        needed.append(server)
    return needed

def add_addons_to_servers():
    global all_dc
    while True:
        try:
            with preferences_lock:
                subsidiary = user_preferences["subsidiary"]
                servers = user_preferences.get("user_servers", [])
                needed = servers_needing_catalog(servers)
                snapshot = list(all_dc)
                if not snapshot:
                    snapshot = []
                    for server in needed:
                        record = availability_record_from_server(server)
                        if record:
                            snapshot.append(record)
            if not needed:
                time.sleep(30)
                continue
            if not snapshot:
                time.sleep(3)
                continue
            catalog = fetch_catalog(snapshot, subsidiary)
            if catalog:
                with preferences_lock:
                    apply_catalog_addons(catalog)
        except Exception:
            logging.exception("Catalog fetcher failed.")
        time.sleep(30)

def orderable_candidates(availabilities, datacenters, fqn, max_hours=DEFAULT_MAX_DELIVERY_HOURS):
    candidates = []
    try:
        max_hours = float(max_hours)
    except (TypeError, ValueError):
        max_hours = DEFAULT_MAX_DELIVERY_HOURS
    for index, dc in enumerate(datacenters):
        name = dc["dedicated_datacenter"]
        status = datacenter_status(availabilities, name, fqn)
        hours = availability_hours(status)
        if hours is None:
            if status not in (None, "unavailable"):
                logging.info(
                    "Skipping %s in %s. Availability %s is not orderable.",
                    fqn,
                    name,
                    status,
                )
            continue
        if hours > max_hours:
            logging.info(
                "Skipping %s in %s: delivery %s exceeds max %sh.",
                fqn,
                name,
                status,
                max_hours,
            )
            continue
        candidates.append((hours, index, dc, status))
    return candidates

def iterate_on():
    global user_preferences, all_dc, order_client
    availabilities = all_dc
    for item in user_preferences["user_servers"]:
        if item.get("qty", 0) < 1 or len(item.get("addon_planCodes", [])) < 3:
            continue
        if item.get("order_attempted", False):
            logging.error(
                "Checkout already attempted in %s. Refusing another automatic checkout.",
                item.get("order_attempted_in"),
            )
            item["qty"] = 0
            save_preferences()
            continue
        if not isinstance(item.get("dc_carts"), dict):
            item["dc_carts"] = {}
        candidates = orderable_candidates(
            availabilities,
            item["datacenters"],
            item["fqn"],
            item.get("max_delivery_hours", DEFAULT_MAX_DELIVERY_HOURS),
        )
        if not candidates:
            continue
        outcomes = []
        for hours, index, dc, status in candidates:
            if not ensure_cart(order_client, item, dc):
                continue
            logging.info(
                "Selected %s for checkout. Availability %s.",
                dc["dedicated_datacenter"],
                status,
            )
            outcome = place_order(order_client, item, dc)
            outcomes.append(outcome)
            if outcome in ("ordered", "latched", "validated"):
                break
        if outcomes and all(outcome == "too_expensive" for outcome in outcomes):
            logging.info("Every orderable datacenter is over the ceiling. Dropping the server.")
            item["ceiling_price"] = 0.0
            item["qty"] = 0

def main():
    global ovh, order_client, availability_client
    import ovh as ovh_sdk
    import dotenv
    ovh = ovh_sdk
    dotenv.load_dotenv("./.env")
    load_preferences()
    acquire_instance_lock()
    order_client = ovh.Client()
    availability_client = ovh.Client()

    logging.info("Start thread: availability fetcher")
    dc_pull_thread = threading.Thread(target=fetch_dcs)
    dc_pull_thread.daemon = True
    dc_pull_thread.start()

    time.sleep(3)
    logging.info("Start thread: catalog fetcher")
    catalog_pull_thread = threading.Thread(target=add_addons_to_servers)
    catalog_pull_thread.daemon = True
    catalog_pull_thread.start()

    while True:
        logging.info("New round.")
        try:
            with preferences_lock:
                iterate_on()
        except Exception:
            logging.exception("Order round failed.")
        with preferences_lock:
            save_preferences()
        time.sleep(3)

if __name__ == "__main__":
    main()

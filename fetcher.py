#!/usr/bin/python3

import urllib.request
import urllib.error
import errno
import os
import tempfile
import time
import json
import datetime
import re

# OVH eco hunter (catalog fetcher)

server_catalog = {}
server_availabilities={}
offers = {}
try:
    with open("offers.json") as ff:
        offers = json.load(ff)
except Exception:
    print("Error opening offers.json. However creating it.")

def _rewrite_existing_file(path, text):
    with open(path, "r+") as dest:
        dest.seek(0)
        dest.write(text)
        dest.truncate()
        dest.flush()
        os.fsync(dest.fileno())

def atomic_write_text(path, text):
    # Docker bind-mounts the preferences file into /app and runs as uid 1000.
    # Creating preferences.json.tmp in /app is denied, and renaming over the
    # mount point fails, so fall back to rewriting the mounted file.
    tmp = path + ".tmp"
    handle = None
    try:
        handle = open(tmp, "w")
    except PermissionError:
        fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", dir=tempfile.gettempdir())
        handle = os.fdopen(fd, "w")
    try:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    finally:
        handle.close()
    try:
        os.replace(tmp, path)
    except OSError:
        _rewrite_existing_file(path, text)
        try:
            os.remove(tmp)
        except OSError:
            pass
        return
    directory = os.path.dirname(os.path.abspath(path)) or "."
    try:
        dirfd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dirfd)
    except OSError:
        pass
    finally:
        os.close(dirfd)

def save_file():
    global offers
    atomic_write_text("offers.json", json.dumps(offers, default=str, indent=2))

def pricing_major(pricings):
    if not pricings:
        return 0.0
    chosen = pricings[1] if len(pricings) > 1 else pricings[0]
    try:
        return chosen["price"] / 100000000
    except (KeyError, TypeError, IndexError):
        return 0.0

def search_addon(planCode):
    global server_availabilities, server_catalog
    data={}
    for addon in server_catalog['addons']:
        if planCode == addon['planCode']:
            data['planCode']=addon["planCode"]
            data['invoiceName']=addon["invoiceName"]
            data['price']=pricing_major(addon.get("pricings"))
            return data
    return "unknown"

def search_cpu(planCode, invoiceName):
    global server_availabilities, server_catalog
    alter_cpu = "" # invoiceName.split("|")[1].strip()
    for k in server_catalog['products']:
        if k['name'] == planCode:
            cpuinfo = k['blobs']['technical']['server']['cpu']
            cpu_full_name = cpuinfo['brand']+" "+cpuinfo['model']
            return cpu_full_name
    return alter_cpu

def get_labels(configurations):
    labels={}
    for i in configurations:
        labels[i["name"]]=i["values"]
    return labels

def get_addons(addonFamilies, memory_code, storage_code):
    ret_addons={}
    ret_addons["price"]=0.0
    for i in addonFamilies:
        name = i["name"]
        if "mandatory" in i and i["mandatory"] == True:
            if name == "storage":
                in_list=i["addons"]
                out_list=[]
                if len(in_list) > 1:
                    r = re.compile(storage_code+".*")
                    out_list=list(filter(r.match, in_list))
                elif len(in_list) == 1:
                    out_list = [in_list[0]]
                if len(out_list) > 0:
                    found=search_addon(out_list[0])
                    if isinstance(found, dict):
                        ret_addons["storage"]=found
                        ret_addons["price"]+=found.get("price", 0.0)
            elif name == "memory":
                in_list=i["addons"]
                out_list=[]
                if len(in_list) > 1:
                    r = re.compile(memory_code+".*")
                    out_list=list(filter(r.match, in_list))
                elif len(in_list) == 1:
                    out_list = [in_list[0]]
                if len(out_list) > 0:
                    found=search_addon(out_list[0])
                    if isinstance(found, dict):
                        ret_addons["memory"]=found
                        ret_addons["price"]+=found.get("price", 0.0)
            else:
                ret_addons[name]={}
                ret_addons[name]["mandatory"]=i["mandatory"]
                ret_addons[name]["default"]=i["default"]
                ret_addons[name]["exclusive"]=i["exclusive"]
                ret_addons[name]["defaultAddon"]=search_addon(i["default"])
                tmp = []
                for k in i["addons"]:
                    tmp.append(search_addon(k))
                ret_addons[name]["items"]=tmp
    return ret_addons

def get_range(planCode):
    ranges={
        "sk":"kimsufi",
        "sys":"soyoustart",
        "rise":"rise",
    }
    for i in ranges:
        pattern = re.compile(".*"+i+".*")
        if pattern.match(planCode):
            return ranges[i]
    return "unkown"

def search_server(planCode, memory_code, storage_code):
    global server_availabilities, server_catalog
    server = {}
    for product in server_catalog['plans']:
        if product['planCode'] == planCode:
            server['invoiceName']=product['invoiceName']
            server['addons'] = {}
            server['labels'] = {}
            server["sum_price"]=0.0
            server['slug']=product['invoiceName'].split("|")[0].lower()
            if "range" in product["blobs"]["commercial"]:
                server["range"]=product["blobs"]["commercial"]["range"]
            else:
                server["range"]=get_range(planCode)
            server['price']=pricing_major(product.get("pricings"))
            server["sum_price"]+=server['price']
            server['planCode']=product['planCode']
            server["cpu"]=search_cpu(product['planCode'], server['invoiceName'])
            server["labels"]=get_labels(product["configurations"])
            server["addons"] = get_addons(product["addonFamilies"], memory_code, storage_code)
            server["sum_price"]+=server["addons"]["price"]
    return server

def catalog_url(subsidiary):
    sub = (subsidiary or "").upper()
    if sub in {"CA", "QC", "WE", "WS"}:
        host = "https://ca.api.ovh.com/v1"
    elif sub == "US":
        host = "https://api.us.ovhcloud.com/v1"
    else:
        host = "https://eu.api.ovh.com/v1"
    return host + "/order/catalog/public/eco?ovhSubsidiary=" + sub

def fetch_offers_and_servers(subsidiary):
    global server_availabilities, server_catalog
    headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:130.0) Gecko/20100101 Firefox/130.0'}
    try:
        req = urllib.request.Request(
            url=catalog_url(subsidiary),
            data=None,
            headers=headers
        )
        with urllib.request.urlopen(req,timeout=10) as response:
            server_catalog =json.loads(response.read().decode("utf-8"))
    except Exception as e:
        print("error in fetch")
        print(e)
        return False
    return True

def iterate_availabilities(server_availabilities):
    global server_catalog, offers
    for i in server_availabilities:
        planCode=i["planCode"]
        fqn=i["fqn"]
        memory_code=i["memory"]
        storage_code=i["storage"]
        offers[fqn]={}
        offers[fqn]["fqn"]=fqn
        offers[fqn]["planCode"]=planCode
        offers[fqn]["memory"]=memory_code
        offers[fqn]["storage"]=storage_code
        offers[fqn]["catalog"]=search_server(planCode, memory_code, storage_code)

def fetch_catalog(availabilities, subsidiary):
    if not fetch_offers_and_servers(subsidiary):
        return None
    iterate_availabilities(availabilities)
    save_file()
    return offers


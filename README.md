# OVH ECO auto order

## Purpose of this program

OVHcloud changed the ECO line in 2024 September. The new line offers a very insane price-performance ratio so it is easy to know that stocks limited and runs out very fast.

In this program you can place orders VIA the OVHcloud API and start it as a script or in a container and place the order when it is available!

> You are not the only one who use this so your success is not guaranteed

> The API can be changed, removed or software can make any mistake that affects your order so use it on your own risk!

> As this script fetches the OVHcloud server availability API and if required, the catalog API, requires a higher amount of data.

## What this fork changes

This repository is a fork of [adns44/ovh-eco-autoorder](https://github.com/adns44/ovh-eco-autoorder). The OVH cart and checkout calls are the same. The changes are there so an unattended bot with autopay makes one purchase and then stops.

The original project was exercised on subsidiary IE. The sample in this fork is KS-STOR on subsidiary DE. Copy `preferences.sample.json` to `preferences.json` and edit it.

### One checkout

Upstream sets `qty` to 0 only after OVH accepts the checkout, and `preferences.json` is written later by the main loop. If the process dies in that gap, the next start still has `qty` 1 and can buy a second server.

This fork saves a latch before `POST /checkout`:

1. The first invoice, excluding VAT, is compared with `ceiling_price`. A total equal to the ceiling is allowed. A higher total tries the next preferred datacenter. If every orderable datacenter is over the ceiling, `qty` and `ceiling_price` are set to 0 and nothing is bought.
2. `order_attempted` is set to true, `order_attempted_in` records the datacenter, and `qty` is set to 0.
3. `preferences.json` is written to a temporary file, fsynced, and then moved into place.
4. The checkout POST runs, with `waiveRetractationPeriod` left false.
5. A successful response stores `ordered_at`, `ordered_in`, and the raw order, then saves again. A failed, rejected, or timed-out checkout stays latched.

Check the OVH order manager before you try again. Set `order_attempted` back to false and `qty` back to 1 yourself. The script will not do that.

### Which datacenter it buys

Upstream treats every availability value other than `unavailable` as in stock. That includes `unknown`, `comingSoon`, and long delays such as `240H`, `480H`, `720H`, `1440H`, and `2160H`. It also creates a cart for every configured datacenter before it asks whether any of them can be ordered, then checks out the first site in the list that is not `unavailable`.

This fork:

- Requests `/dedicated/server/datacenter/availabilities` for each `planCode` in `preferences.json`.
- Treats `1H-high` and `1H-low` as 1 hour. Other orderable values are delivery windows such as `24H` and `72H`.
- Skips `unavailable`, `unknown`, `comingSoon`, and any window longer than `max_delivery_hours` (default 72). Set that to 24 if you only want near-immediate stock.
- Uses the `datacenters` array as preference order. With FRA first, FRA is bought whenever FRA is within the cap, even when a later site is `1H-low`. The next site is used when the earlier one is outside the cap, over the ceiling, or its cart cannot be created.
- Creates a cart for the first preferred datacenter that passed that filter, then checks the price and checks out. The next datacenter is prepared only if that cart fails or the price is over the ceiling.

### Preferences file and a second process

- A crash while saving leaves the previous `preferences.json` in place. The original code opens the file for writing, which truncates it first.
- If `preferences.json` cannot be parsed, the process exits. It does not save an empty document over the real file.
- The process holds `preferences.lock` until it exits. A second `order.py` refuses to start. Mount that file in Docker and run only one instance.
- Availability polling and ordering use two OVH clients. Updates to `preferences.json` are serialized, so the catalog thread and the order loop do not save at the same time.
- Cart expiry is compared in UTC. The original comparison used `datetime.utcnow()` against an offset-aware OVH timestamp.

### Catalog

Upstream downloads the public eco catalog with `ovhSubsidiary=IE`.

This fork uses the `subsidiary` in `preferences.json`. DE uses the EU API. CA and US use their own API hosts.

`preferences.sample.json` already contains the KS-STOR add-on plan codes, so ordering does not wait for a catalog download:

- `ram-16g-24skstor01`
- `hybridsoftraid-4x4000sa-1x500nvme-24skstor`
- `bandwidth-500-24sk`

Leave `fetch_catalog` empty when those codes are filled in. If `fetch_catalog` is set and fewer than three add-on codes are present, the catalog thread can resolve them from the FQN even while the availability feed is empty. It does not append more codes once three are already stored. An error in that thread is logged and the loop continues. A cart entry that is missing `itemIds` is rebuilt instead of aborting the round.

### Tests

```
python3 -m unittest tests/test_checkout.py
```

The tests cover the latch, the ceiling, long delivery, Frankfurt-first selection, a failed checkout, a preferences file that cannot be parsed, a missing catalog, and a failed cart.

## Set it up

The original project was tested on subsidiary IE. This fork's sample config uses subsidiary DE.
Project based on [OVHcloud Python wrapper](https://github.com/ovh/python-ovh), consult with this about subsidiary settings and more.

### Set up the API

Create an API key [here](https://api.ovh.com/createToken/index.cgi?GET=/*&PUT=/*&POST=/*&DELETE=/*) and write down its information.
> Note: Select the proper expiration. Best option is 30 days, avoid to use unlimited access, it is more secure if you recreate it every 30 days.
Create a `.env` file to allow dotenv to read it and place the settings in here.
```
OVH_ENDPOINT='ovh-eu'
OVH_APPLICATION_KEY='XXX'
OVH_APPLICATION_SECRET='XXX'
OVH_CONSUMER_KEY='XXX'
```
> Endpoints available on the uper link, use it that matches with your account.
> I suggest to use VsCode to create the files and manage them.

### Set up your desired orders

you need to create the `preferences.json` file.
`preferences.sample.json` is a ready-to-copy config (KS-STOR, subsidiary DE). Copy it to `preferences.json` and edit it.
Here is an example JSON contents for it.

#### RAW JSON
```
{
  "subsidiary": "IE",
  "user_servers": [
    {
      "planCode": "25skleb01",
      "fqn": "25skleb01.ram-32g-ecc-2400.softraid-2x450nvme",
      "skip_validate": false,
      "place_order": false,
      "autopay": false,
      "max_delivery_hours": 72,
      "order_attempted": false,
      "coupons": ["MONDAY"],
      "datacenters": [
        {
          "region": "europe",
          "dedicated_datacenter": "fra"
        },
        {
          "region": "europe",
          "dedicated_datacenter": "gra"
        }
      ],
      "labels": {
        "dedicated_os": "none_64.en"
      },
      "addon_planCodes": [
        "softraid-2x450nvme-25skle",
        "bandwidth-300-25skle",
        "ram-32g-ecc-2400-25skle"
      ],
      "qty": 1,
      "ceiling_price": 20.0,
      "dc_carts": {}
    }
  ]
}
```

#### Explanation

The JSON contains the order placement subsidiary (IE), and the user servers.

User servers is an array which contains the services. You need to fill an array item by this example but it is important that the script will edit it, store the product-specific cart information and other things. So always write this minimal array item (expand it if you need more than 2 servers) but keep this structure and let the script to use.

A server contains a few important information
- The FQN, planCode, labels, addon planCodes, datacenters.
> These informations  specified on [OVH API](https://eu.api.ovh.com/console/). See order and dedicated server sections.
- QTY and ceiling price are two important variables. Sets the wanted quantity on a cart and sets the maximum 1st invoice price (excl. VAT). If price higher this the client do not order and set the qty, price to 0 to remove it from future checks. The first invoice can include the setup fees, so E.G. if you want to order KS-LE-B with 9.9 monthly and 9.9 euros setup price, you need to set a 20 eur ceiling price.
- From the labels only one needed. Set dedicated_os.
- The dedicated_datacenter is a label too. The datacenters array is preference order. The first site that is orderable within `max_delivery_hours` is the one checked out. FRA first means FRA is bought whenever FRA is orderable, even if a later site has a shorter delivery. A later site is used when an earlier one is outside the delivery cap, over the ceiling, or its cart cannot be created.
> If you want to place an order both in gra and fra, simply create another item and modify the first to fra-only and second to gra-only. so datacenter array should contains only one element in both servers.
- To the addons you need to place all mandatory addon planCodes. If you do not do it and only one is missing the order fails.
- skip_validate: Set it to true if you want to skip order validation
> If order validation skiped, ceiling price omitted and product ordered on any price. It makes the speed faster.
- place_order: Set it explicitly to true. If you do not do it, only an order plan will be created.
> Keep in mind that if this set to false, the quantity set to 0 after the validation. So if you create an order and only validate it, to order it, close the APP, set quantity and restart it. This behaviour fixes the issue that multiple rechecks increases network load and time consumed by the script.
- autopay: Controls that after the order, payment processed automaticaly or not
> If you set this to false, until you do not pay the order it is not placed so you can lose your chance to get the server.
- max_delivery_hours: Longest delivery the script will buy. Default 72. `1H-high` and `1H-low` count as 1 hour. `240H`, `480H`, and longer windows are skipped when they are above this value. Use 24 if you only want near-immediate stock.
- order_attempted: Fail-closed latch, default false. Immediately before checkout the script sets this to true, records `order_attempted_in`, sets qty to 0, and saves `preferences.json`. Carts are created only after a datacenter is orderable within `max_delivery_hours`. `unknown`, `comingSoon`, and `unavailable` are skipped. If the latch is already true, another automatic checkout is refused. A failed or ambiguous checkout stays latched: check the OVH order manager, then set `order_attempted` back to false and `qty` back to 1 yourself before the script will try again. A second process exits if `preferences.lock` is already held.
- coupons: An array which contains the coupon codes for the order
- dc_carts: Empty, it stores the datacenter-specific cart informations. By default do not need manual modification.

So when you first start the script after a personalised configuration, it will fill up the cart information(s) based on your needs.
When it expires (after a month) the script creates a new one.
Only servers checked and planned that have higher qty than zero.

Subsidiary: Depends on your API endpoint. Used at creating orders. You can fetch subsidiary list and select yours, depending on your account.
- [CA](https://ca.api.ovh.com/console/?section=%2Fdedicated%2Fserver&branch=v1#get-/dedicated/server/availabilities)
- [EU](https://eu.api.ovh.com/console/?section=%2Fdedicated%2Fserver&branch=v1#get-/dedicated/server/availabilities)
- [US](https://api.us.ovhcloud.com/console/?section=%2Forder&branch=v1#get-/order/catalog/public/eco)
> always use capitalized letters.

### Autofill from catalog

> This feature is very very experimental. Use it on your own risk!!!

> This feature fetches the public eco catalog for the subsidiary in `preferences.json`. DE uses the EU API. CA and US use their own API hosts.

> This feature consumes a high amount of data. Use it on your own risk!

You can Instruct the API to look the server catalog and fetch the data from it when available.
This feature helps you if you want to grab a server which shown on availability API but not available on the catalog yet. For example KS-LE-2 appeared in October as 25skle02 with low stock in rbx. If you set it you can order it after OVH fills it up into the cart.
If you set up the fetcher, it fetches the data from the catalog and if you set, orders it with your needs.

Here is the raw JSON for this. You can combine multiple servers with multiple options in the array. So for example you can simply insert KS-LE-B and 25skle02.
> Warning: Until one or more than one server requires catalog, it downloaded periodicaly that increases network traffic drasticaly. Use it on your own risk!

```
{
  "subsidiary": "IE",
  "user_servers": [
    {
      "planCode": "24sk50",
      "fqn": "24sk50.ram-32g-ecc-2400.softraid-2x2000sa",
      "skip_validate": false,
      "place_order": false,
      "autopay": false,
      "max_delivery_hours": 72,
      "order_attempted": false,
      "coupons": ["MONDAY"],
      "fetch_catalog": {
        "storage": "",
        "memory": "",
        "bandwidth": ""
      },
      "datacenters": [
        {
          "region": "europe",
          "dedicated_datacenter": "gra"
        },
        {
          "region": "europe",
          "dedicated_datacenter": "fra"
        }
      ],
      "labels": {
        "dedicated_os": "none_64.en"
      },
      "addon_planCodes": [],
      "qty": 1,
      "ceiling_price": 50.0,
      "dc_carts": {}
    }
  ]
}
```

Explanation:
- The addon_planCodes is empty, it will be filled up when config available on catalog.
- Set in the `fetch_catalog` the parts that you want to fetch. In Kimsufi, these are bandwidth, memory and storage. These are empty strings but if you prefer, you can prefill it up and in this case it only copied to addons. If you ommit any mandatory field, server probably not ordered.
- After filled up the data from catalog, the process is normal. Based on your needs, it creates the raw data and adds it to the json file.
> In rare situations it is possible that FQN and catalog are not consistent, especialy on 500/512 GB NVMe and some special RAM server modells. If only one RAM and storage option possible, it is not a problem as the script uses the default one.

## Run it

run it as a simple Python script
```
pip install -r requirements.txt
python3 order.py
```

Or in Docker (see packages).

If you run it in Docker, bind the `preferences.json` and `preferences.lock` to `/app` and bind the `.env` to the `/app` directory. Optionaly bind the `offers.json` if you want to access catalog from the host system. Run only one instance. A second process exits while `preferences.lock` is held.

## Run it in Docker

Clone this repo and run 
`docker compose build`
`docker compose up -d`.

# Remarks

This script uses the OVHcloud API. The ECO and other names are owned by the OVHcloud.

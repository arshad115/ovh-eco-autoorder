import builtins
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import order


class BadParametersError(Exception):
    pass


class ResourceNotFoundError(Exception):
    pass


class _Exceptions:
    BadParametersError = BadParametersError
    ResourceNotFoundError = ResourceNotFoundError


class _Ovh:
    exceptions = _Exceptions()


class FakeClient:
    def __init__(self, total=52.0, checkout_error=None):
        self.total = total
        self.checkout_error = checkout_error
        self.posts = []

    def get(self, path):
        if path.endswith("/summary"):
            return {
                "details": [],
                "prices": {"withoutTax": {"value": self.total, "currencyCode": "EUR"}},
            }
        raise AssertionError(path)

    def post(self, path, **kwargs):
        self.posts.append(path)
        if path.endswith("/checkout"):
            with open("preferences.json") as fh:
                saved = json.load(fh)
            server = saved["user_servers"][0]
            if not server.get("order_attempted") or server.get("qty") != 0:
                raise AssertionError("latch was not saved before checkout")
            if self.checkout_error:
                raise self.checkout_error
            return {"orderId": 1}
        return {"cartId": "cart", "expire": "2099-01-01T00:00:00+00:00", "itemId": 1}


def avail(fqn, statuses):
    return [{
        "fqn": fqn,
        "datacenters": [
            {"datacenter": name, "availability": status}
            for name, status in statuses.items()
        ],
    }]


def datacenters(*names):
    return [{"region": "europe", "dedicated_datacenter": name} for name in names]


class CheckoutTests(unittest.TestCase):
    def setUp(self):
        self._cwd = os.getcwd()
        self._tmpdir = tempfile.TemporaryDirectory()
        os.chdir(self._tmpdir.name)
        order.ovh = _Ovh()
        order.preferences_ready = False
        order.user_preferences = {}
        order.all_dc = []
        order.order_client = None
        self._ensure = order.ensure_cart

    def tearDown(self):
        order.ensure_cart = self._ensure
        os.chdir(self._cwd)
        self._tmpdir.cleanup()

    def arm(self, item):
        order.preferences_ready = True
        order.user_preferences = {"subsidiary": "DE", "user_servers": [item]}
        with open("preferences.json", "w") as fh:
            json.dump(order.user_preferences, fh)
        return item

    def server(self, **extra):
        item = {
            "planCode": "24skstor012-v1",
            "fqn": "24skstor012-v1.ram-16g-ecc-2133.hybridsoftraid-4x4000sa-1x500nvme",
            "skip_validate": False,
            "place_order": True,
            "autopay": True,
            "qty": 1,
            "ceiling_price": 52.0,
            "max_delivery_hours": 72,
            "addon_planCodes": ["ram", "disk", "bandwidth"],
            "datacenters": datacenters("fra", "sbg", "gra"),
            "dc_carts": {"fra": {"cartId": "fra-cart"}},
            "coupons": [],
            "labels": {"dedicated_os": "none_64.en"},
        }
        item.update(extra)
        return self.arm(item)

    def test_availability_hours_and_delivery_cap(self):
        self.assertEqual(order.availability_hours("1H-high"), 1)
        self.assertEqual(order.availability_hours("1H-low"), 1)
        self.assertEqual(order.availability_hours("72H"), 72)
        self.assertEqual(order.availability_hours("2160H"), 2160)
        for blocked in ("unavailable", "unknown", "comingSoon", None, ""):
            self.assertIsNone(order.availability_hours(blocked))

        statuses = avail("srv", {"fra": "240H", "sbg": "72H", "gra": "1H-low"})
        dcs = datacenters("fra", "sbg", "gra")
        picked = order.orderable_candidates(statuses, dcs, "srv", 72)
        self.assertEqual([row[2]["dedicated_datacenter"] for row in picked], ["sbg", "gra"])

    def test_datacenter_preference_beats_shorter_delivery(self):
        statuses = avail("srv", {"fra": "24H", "sbg": "1H-low"})
        picked = order.orderable_candidates(statuses, datacenters("fra", "sbg"), "srv", 72)
        self.assertEqual([row[2]["dedicated_datacenter"] for row in picked], ["fra", "sbg"])

    def test_latch_is_saved_before_checkout_and_blocks_a_second_post(self):
        item = self.server()
        client = FakeClient(total=52.0)
        self.assertEqual(order.place_order(client, item, {"dedicated_datacenter": "fra"}), "ordered")
        self.assertEqual(client.posts, ["/order/cart/fra-cart/checkout"])
        self.assertTrue(item["order_attempted"])
        self.assertEqual(item["order_attempted_in"], "fra")
        self.assertEqual(item["qty"], 0)
        self.assertEqual(order.place_order(client, item, {"dedicated_datacenter": "sbg"}), "latched")
        self.assertEqual(client.posts, ["/order/cart/fra-cart/checkout"])

    def test_ceiling_allows_equal_price_and_does_not_latch_when_over(self):
        item = self.server()
        client = FakeClient(total=52.0)
        self.assertEqual(order.place_order(client, item, {"dedicated_datacenter": "fra"}), "ordered")

        other = self.server(dc_carts={"sbg": {"cartId": "sbg-cart"}})
        client.total = 52.01
        self.assertEqual(order.place_order(client, other, {"dedicated_datacenter": "sbg"}), "too_expensive")
        self.assertFalse(other.get("order_attempted", False))
        self.assertEqual(other["qty"], 1)
        self.assertEqual(client.posts, ["/order/cart/fra-cart/checkout"])

    def test_failed_checkout_stays_latched(self):
        item = self.server()
        client = FakeClient(checkout_error=BadParametersError("rejected"))
        self.assertEqual(order.place_order(client, item, {"dedicated_datacenter": "fra"}), "latched")
        self.assertTrue(item["order_attempted"])
        self.assertEqual(item["qty"], 0)
        self.assertIn("rejected", item["order_error"])
        self.assertEqual(order.place_order(client, item, {"dedicated_datacenter": "fra"}), "latched")
        self.assertEqual(len(client.posts), 1)

    def test_save_rewrites_mounted_file_when_tmp_cannot_be_created(self):
        from fetcher import atomic_write_text
        with open("preferences.json", "w") as fh:
            fh.write('{"old": 1}')
        real_open = builtins.open
        real_replace = os.replace

        def guarded(file, mode="r", *args, **kwargs):
            name = os.fspath(file)
            if name.endswith("preferences.json.tmp") and "w" in str(mode):
                raise PermissionError(13, "Permission denied")
            return real_open(file, mode, *args, **kwargs)

        def deny_replace(src, dst):
            raise OSError("Device or resource busy")

        try:
            builtins.open = guarded
            os.replace = deny_replace
            atomic_write_text("preferences.json", '{"new": 2}')
        finally:
            builtins.open = real_open
            os.replace = real_replace
        with open("preferences.json") as fh:
            self.assertEqual(fh.read(), '{"new": 2}')

    def test_invalid_preferences_are_not_overwritten(self):
        with open("preferences.json", "w") as fh:
            fh.write("{")
        order.preferences_ready = False
        with self.assertRaises(SystemExit):
            order.load_preferences()
        order.save_preferences()
        with open("preferences.json") as fh:
            self.assertEqual(fh.read(), "{")

    def test_missing_catalog_does_not_drop_known_addons(self):
        item = self.server(
            fetch_catalog={"storage": "", "memory": "", "bandwidth": ""},
            addon_planCodes=["ram-16g-24skstor01", "hybridsoftraid-4x4000sa-1x500nvme-24skstor", "bandwidth-500-24sk"],
        )
        order.apply_catalog_addons({})
        self.assertEqual(item["addon_planCodes"], [
            "ram-16g-24skstor01",
            "hybridsoftraid-4x4000sa-1x500nvme-24skstor",
            "bandwidth-500-24sk",
        ])
        self.assertEqual(item["fetch_catalog"], {})

        empty = self.server(fqn="missing.ram.disk", fetch_catalog={"storage": ""}, addon_planCodes=[])
        order.apply_catalog_addons({})
        self.assertEqual(empty["addon_planCodes"], [])
        self.assertEqual(empty["fetch_catalog"], {"storage": ""})

    def test_carts_are_built_only_for_orderable_sites_and_preference_wins(self):
        built = []

        def ensure(client, item, dc):
            name = dc["dedicated_datacenter"]
            built.append(name)
            item["dc_carts"][name] = {
                "cartId": name,
                "itemIds": [1],
                "cartExpire": "2099-01-01T00:00:00+00:00",
            }
            return True

        order.ensure_cart = ensure
        item = self.server(dc_carts={})
        order.all_dc = avail(item["fqn"], {"fra": "24H", "sbg": "1H-low", "gra": "unavailable"})
        order.order_client = FakeClient()
        order.iterate_on()
        self.assertEqual(built, ["fra"])
        self.assertEqual(order.order_client.posts, ["/order/cart/fra/checkout"])
        self.assertEqual(item["ordered_in"], "fra")

    def test_long_delivery_is_skipped_before_a_cart_is_created(self):
        built = []

        def ensure(client, item, dc):
            built.append(dc["dedicated_datacenter"])
            name = dc["dedicated_datacenter"]
            item["dc_carts"][name] = {
                "cartId": name,
                "itemIds": [1],
                "cartExpire": "2099-01-01T00:00:00+00:00",
            }
            return True

        order.ensure_cart = ensure
        item = self.server(dc_carts={})
        order.all_dc = avail(item["fqn"], {"fra": "240H", "sbg": "480H", "gra": "1H-high"})
        order.order_client = FakeClient()
        order.iterate_on()
        self.assertEqual(built, ["gra"])
        self.assertEqual(item["ordered_in"], "gra")

    def test_failed_cart_creation_tries_the_next_preferred_datacenter(self):
        built = []

        def ensure(client, item, dc):
            name = dc["dedicated_datacenter"]
            built.append(name)
            if name == "fra":
                return False
            item["dc_carts"][name] = {
                "cartId": name,
                "itemIds": [1],
                "cartExpire": "2099-01-01T00:00:00+00:00",
            }
            return True

        order.ensure_cart = ensure
        item = self.server(dc_carts={})
        order.all_dc = avail(item["fqn"], {"fra": "1H-high", "sbg": "24H", "gra": "unavailable"})
        order.order_client = FakeClient()
        order.iterate_on()
        self.assertEqual(built, ["fra", "sbg"])
        self.assertEqual(order.order_client.posts, ["/order/cart/sbg/checkout"])
        self.assertEqual(item["ordered_in"], "sbg")

    def test_cart_expiry_uses_aware_utc(self):
        future = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        self.assertFalse(order.is_cart_expired(future))
        self.assertTrue(order.is_cart_expired(past))

    def test_catalog_record_can_be_built_without_availability(self):
        record = order.availability_record_from_server({
            "planCode": "24skstor012-v1",
            "fqn": "24skstor012-v1.ram-16g-ecc-2133.hybridsoftraid-4x4000sa-1x500nvme",
        })
        self.assertEqual(record["memory"], "ram-16g-ecc-2133")
        self.assertEqual(record["storage"], "hybridsoftraid-4x4000sa-1x500nvme")
        self.assertEqual(order.servers_needing_catalog([{
            "addon_planCodes": ["a", "b", "c"],
            "fetch_catalog": {"storage": ""},
        }]), [])


if __name__ == "__main__":
    unittest.main()

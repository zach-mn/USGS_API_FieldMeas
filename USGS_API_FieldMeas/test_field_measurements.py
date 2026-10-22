"""Offline tests for field_measurements.py (HTTP is mocked)."""

import os
import tempfile
import unittest
from datetime import date
from unittest import mock

import field_measurements as fm


def feature(site, visit, code, time_str):
    return {
        "type": "Feature",
        "id": "%s-%s-%s" % (site, visit, code),
        "geometry": None,
        "properties": {
            "monitoring_location_id": site,
            "field_visit_id": visit,
            "parameter_code": code,
            "time": time_str,
            "value": "1.0",
        },
    }


class FakeResponse(object):
    def __init__(self, payload, status=200, headers=None, url="http://x"):
        self._payload = payload
        self.status_code = status
        self.headers = headers or {}
        self.text = str(payload)
        self.url = url

    def json(self):
        return self._payload


class FakeSession(object):
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.headers = {}

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "headers": headers})
        return self.responses.pop(0)


class ParsingTests(unittest.TestCase):
    def test_parse_date_formats(self):
        self.assertEqual(fm.parse_date("1/1/2025"), date(2025, 1, 1))
        self.assertEqual(fm.parse_date("12/31/2025"), date(2025, 12, 31))
        self.assertEqual(fm.parse_date("2025-06-15"), date(2025, 6, 15))
        with self.assertRaises(ValueError):
            fm.parse_date("31/12/2025")

    def test_interval_date_only_inclusive(self):
        self.assertEqual(
            fm.build_time_interval(date(2025, 1, 1), date(2025, 12, 31)),
            "2025-01-01/2025-12-31",
        )

    def test_interval_rejects_reversed(self):
        with self.assertRaises(ValueError):
            fm.build_time_interval(date(2025, 2, 1), date(2025, 1, 1))

    def test_normalize_sites(self):
        self.assertEqual(fm.normalize_site("05331000"), "USGS-05331000")
        self.assertEqual(fm.normalize_site("usgs-05331000"), "USGS-05331000")
        self.assertEqual(fm.normalize_site("5331000"), "USGS-05331000")
        self.assertEqual(fm.normalize_site("USGS-4015330"), "USGS-04015330")
        self.assertEqual(fm.normalize_site("444306122144600"), "USGS-444306122144600")
        self.assertEqual(
            fm.unique_sites(["05331000", "USGS-05331000", " 05330000 ", ""]),
            ["USGS-05331000", "USGS-05330000"],
        )

    def test_read_sites_file_keeps_leading_zeros(self):
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as fh:
            fh.write("site_no\n05331000\n# comment\n05330000,05288500\n")
            path = fh.name
        try:
            self.assertEqual(fm.read_sites_file(path), ["05331000", "05330000", "05288500"])
        finally:
            os.remove(path)

    def test_dotenv_loading(self):
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as fh:
            fh.write("# comment\nAPI_USGS_PAT='abc123'\n")
            path = fh.name
        try:
            with mock.patch.dict(os.environ, {}, clear=True):
                fm.load_dotenv_file(path)
                self.assertEqual(os.environ["API_USGS_PAT"], "abc123")
        finally:
            os.remove(path)


class ClientTests(unittest.TestCase):
    def test_follows_next_links_and_sends_key(self):
        page1 = {
            "features": [feature("USGS-1", "v1", "00060", "2025-03-01T15:00:00+00:00")],
            "numberReturned": 1,
            "numberMatched": 2,
            "links": [{"rel": "next", "href": "https://api.waterdata.usgs.gov/next?page=2"}],
        }
        page2 = {
            "features": [feature("USGS-1", "v1", "00065", "2025-03-01T15:00:00+00:00")],
            "numberReturned": 1,
            "numberMatched": 2,
            "links": [],
        }
        session = FakeSession([FakeResponse(page1), FakeResponse(page2)])
        client = fm.FieldMeasurementClient(api_key="KEY", session=session, verbose=False)
        recs = client.get_field_measurements(
            ["USGS-1", "USGS-2"], "2025-01-01T00:00:00Z/2025-12-31T23:59:59Z", ["00060"])

        self.assertEqual(len(recs), 2)
        first = session.calls[0]
        self.assertTrue(first["url"].endswith("/collections/field-measurements/items"))
        self.assertEqual(first["params"]["monitoring_location_id"], "USGS-1,USGS-2")
        self.assertEqual(first["params"]["time"], "2025-01-01T00:00:00Z/2025-12-31T23:59:59Z")
        self.assertEqual(first["params"]["parameter_code"], "00060")
        self.assertEqual(first["headers"], {"X-Api-Key": "KEY"})
        self.assertEqual(session.calls[1]["url"], "https://api.waterdata.usgs.gov/next?page=2")
        self.assertIsNone(session.calls[1]["params"])

    def test_offset_paging_fallback(self):
        page1 = {"features": [feature("USGS-1", "v1", "00060", "t")],
                 "numberReturned": 1, "numberMatched": 2, "links": []}
        page2 = {"features": [feature("USGS-1", "v2", "00060", "t")],
                 "numberReturned": 1, "numberMatched": 2, "links": []}
        session = FakeSession([FakeResponse(page1), FakeResponse(page2)])
        client = fm.FieldMeasurementClient(api_key="KEY", session=session, verbose=False)
        recs = list(client.get_items("field-measurements", {"time": "x"}))
        self.assertEqual(len(recs), 2)
        self.assertEqual(session.calls[1]["params"]["offset"], 1)

    def test_stops_on_empty_cursor(self):
        # Observed live: the last page links to cursor= (empty), which restarts at page 1.
        base = "https://api.waterdata.usgs.gov/ogcapi/v1/collections/field-measurements/items"
        page1 = {"features": [feature("USGS-1", "v1", "00060", "t")],
                 "links": [{"rel": "next", "href": base + "?cursor=abc&limit=1"}]}
        page2 = {"features": [feature("USGS-1", "v2", "00060", "t")],
                 "links": [{"rel": "next", "href": base + "?cursor=&limit=1"}]}
        session = FakeSession([FakeResponse(page1), FakeResponse(page2)])
        client = fm.FieldMeasurementClient(session=session, verbose=False)
        self.assertEqual(len(list(client.get_items("field-measurements", {}))), 2)
        self.assertEqual(len(session.calls), 2)

    def test_stops_when_page_repeats(self):
        base = "https://api.waterdata.usgs.gov/next"
        page = {"features": [feature("USGS-1", "v1", "00060", "t")],
                "links": [{"rel": "next", "href": base + "?cursor=abc"}]}
        session = FakeSession([FakeResponse(page), FakeResponse(page)])
        client = fm.FieldMeasurementClient(session=session, verbose=False)
        self.assertEqual(len(list(client.get_items("field-measurements", {}))), 1)
        self.assertEqual(len(session.calls), 2)

    def test_key_not_sent_to_other_hosts(self):
        client = fm.FieldMeasurementClient(api_key="KEY", session=FakeSession([]), verbose=False)
        self.assertEqual(client._headers_for("https://example.com/x"), {})

    def test_site_batching(self):
        sites = ["USGS-%d" % i for i in range(fm.SITE_BATCH_SIZE + 1)]
        empty = {"features": [], "numberReturned": 0, "numberMatched": 0, "links": []}
        session = FakeSession([FakeResponse(empty), FakeResponse(empty)])
        client = fm.FieldMeasurementClient(session=session, verbose=False)
        client.get_field_measurements(sites, "a/b")
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(session.calls[1]["params"]["monitoring_location_id"],
                         "USGS-%d" % fm.SITE_BATCH_SIZE)

    def test_retry_on_429(self):
        ok = {"features": [], "numberReturned": 0, "links": []}
        session = FakeSession([FakeResponse({}, status=429, headers={"Retry-After": "0"}),
                               FakeResponse(ok)])
        client = fm.FieldMeasurementClient(session=session, verbose=False)
        with mock.patch.object(fm.time, "sleep"):
            self.assertEqual(list(client.get_items("c", {})), [])
        self.assertEqual(len(session.calls), 2)

    def test_403_raises(self):
        session = FakeSession([FakeResponse({"code": "API_KEY_MISSING"}, status=403)])
        client = fm.FieldMeasurementClient(session=session, verbose=False)
        with self.assertRaises(fm.UsgsApiError):
            list(client.get_items("c", {}))


class SummaryTests(unittest.TestCase):
    def test_counts(self):
        recs = [
            feature("USGS-1", "v1", "00060", "2025-03-01")["properties"],
            feature("USGS-1", "v1", "00065", "2025-03-01")["properties"],
            feature("USGS-1", "v2", "00060", "2025-06-01")["properties"],
        ]
        rows, codes = fm.summarize(recs, ["USGS-1", "USGS-2"])
        self.assertEqual(codes, ["00060", "00065"])
        self.assertEqual(rows[0]["measurement_count"], 3)
        self.assertEqual(rows[0]["field_visit_count"], 2)
        self.assertEqual(rows[0]["param_00060"], 2)
        self.assertEqual(rows[0]["first_time"], "2025-03-01")
        self.assertEqual(rows[0]["last_time"], "2025-06-01")
        self.assertEqual(rows[1]["measurement_count"], 0)
        self.assertEqual(rows[1]["param_00065"], 0)


if __name__ == "__main__":
    unittest.main()

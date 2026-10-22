#!/usr/bin/env python3
"""Retrieve and count USGS field measurements for a site list and date range.

Uses the USGS Water Data OGC API `field-measurements` collection:
    https://api.waterdata.usgs.gov/ogcapi/v1/collections/field-measurements/items

The API key is read from the API_USGS_PAT environment variable (the same name
the `dataretrieval` library uses). A `.env` file in the current directory or
next to this script is loaded automatically if present.

Examples
--------
    python field_measurements.py --start 1/1/2025 --end 12/31/2025 \
        --sites 05331000 05330000

    python field_measurements.py --start 2025-01-01 --end 2025-12-31 \
        --sites-file sites.txt --out-dir output
"""

import argparse
import csv
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from urllib.parse import parse_qs, urlparse

import requests

BASE_URL = "https://api.waterdata.usgs.gov/ogcapi/v1"
COLLECTION = "field-measurements"
API_KEY_ENV = "API_USGS_PAT"
API_KEY_HOST = "api.waterdata.usgs.gov"

# Max features per page. The OGC docs state a max of 10000.
PAGE_LIMIT = 10000
# Sites per request, to keep the GET URL a reasonable length.
SITE_BATCH_SIZE = 50
MAX_RETRIES = 5
TIMEOUT_SECONDS = 120

DATE_FORMATS = ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%Y/%m/%d")


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------

def load_dotenv_file(path):
    """Load KEY=VALUE lines from a .env file into os.environ.

    Existing environment variables are not overwritten. Comment lines and
    blank lines are ignored; surrounding quotes on values are stripped.
    """
    if not os.path.isfile(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export "):]
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            os.environ.setdefault(key, value)


def get_api_key():
    """Return the API key, loading .env from the cwd or script dir if needed."""
    if not os.environ.get(API_KEY_ENV):
        load_dotenv_file(os.path.join(os.getcwd(), ".env"))
        load_dotenv_file(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    return os.environ.get(API_KEY_ENV) or None


# ---------------------------------------------------------------------------
# Input parsing
# ---------------------------------------------------------------------------

def parse_date(text):
    """Parse a date such as '1/1/2025' or '2025-01-01' into a date object."""
    text = text.strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    raise ValueError(
        "Unrecognized date '%s'. Use M/D/YYYY or YYYY-MM-DD." % text
    )


def build_time_interval(start_date, end_date):
    """Build the interval for the `time` query parameter.

    Both dates are inclusive. The `time` property of a field measurement is a
    date (YYYY-MM-DD) with the clock time held separately in `time_of_day`,
    and the server matches whole days on it, so a date-only interval is used.
    """
    if end_date < start_date:
        raise ValueError("End date %s is before start date %s." % (end_date, start_date))
    return "%s/%s" % (start_date.isoformat(), end_date.isoformat())


def normalize_site(site):
    """Convert '05331000' to 'USGS-05331000'; leave 'AGENCY-xxxx' IDs as is.

    USGS site numbers are at least 8 digits. All-digit numbers shorter than
    that (usually a leading zero lost in Excel) are zero-padded to 8 digits.
    """
    site = site.strip().strip("'\"")
    if not site:
        return None
    if "-" in site:
        agency, number = site.split("-", 1)
        agency = agency.upper()
    else:
        agency, number = "USGS", site
    if agency == "USGS" and number.isdigit() and len(number) < 8:
        number = number.zfill(8)
    return "%s-%s" % (agency, number)


def read_sites_file(path):
    """Read site IDs from a text or CSV file.

    One site per line, or comma-separated. A column header named 'site_no' or
    'monitoring_location_id' is skipped, as are lines starting with '#'.
    Site numbers are read as text so leading zeros are kept.
    """
    sites = []
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.reader(fh):
            for cell in row:
                cell = cell.strip()
                if not cell or cell.startswith("#"):
                    continue
                if cell.lower() in ("site_no", "site", "monitoring_location_id"):
                    continue
                sites.append(cell)
    return sites


def unique_sites(raw_sites):
    """Normalize site IDs and drop duplicates, keeping input order."""
    seen = set()
    result = []
    for raw in raw_sites:
        site = normalize_site(raw)
        if site and site not in seen:
            seen.add(site)
            result.append(site)
    return result


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------

class UsgsApiError(RuntimeError):
    pass


def _has_empty_cursor(url):
    """True if the URL has a `cursor` query parameter with no value."""
    query = parse_qs(urlparse(url).query, keep_blank_values=True)
    return "cursor" in query and not any(query["cursor"])


class FieldMeasurementClient(object):
    """Minimal client for the field-measurements collection."""

    def __init__(self, api_key=None, base_url=BASE_URL, session=None, verbose=True):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.verbose = verbose
        self.session.headers.update({
            "Accept": "application/geo+json, application/json",
            "User-Agent": "usgs-field-measurements-script/1.0",
        })

    def _log(self, msg):
        if self.verbose:
            print(msg, file=sys.stderr)

    def _headers_for(self, url):
        # Only send the key to the USGS Water Data API host.
        host = urlparse(url).hostname
        if self.api_key and host == API_KEY_HOST:
            return {"X-Api-Key": self.api_key}
        return {}

    def _get(self, url, params=None):
        """GET with retry on 429 / 5xx / connection errors."""
        delay = 2.0
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = self.session.get(
                    url, params=params, headers=self._headers_for(url),
                    timeout=TIMEOUT_SECONDS,
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                if attempt == MAX_RETRIES:
                    raise UsgsApiError("Request failed: %s" % exc)
                self._log("  network error (%s); retrying in %.0fs" % (exc, delay))
                time.sleep(delay)
                delay *= 2
                continue

            if resp.status_code == 200:
                remaining = resp.headers.get("x-ratelimit-remaining")
                if remaining is not None:
                    self._log("  rate limit remaining: %s" % remaining)
                return resp.json()

            if resp.status_code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES:
                wait = delay
                retry_after = resp.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    wait = max(wait, float(retry_after))
                self._log("  HTTP %d; retrying in %.0fs" % (resp.status_code, wait))
                time.sleep(wait)
                delay *= 2
                continue

            hint = ""
            if resp.status_code in (401, 403):
                hint = (" Check that %s is set to a valid key "
                        "(https://api.waterdata.usgs.gov/signup/)." % API_KEY_ENV)
            raise UsgsApiError(
                "HTTP %d from %s: %s%s" % (resp.status_code, resp.url, resp.text[:500], hint)
            )
        raise UsgsApiError("Exhausted retries for %s" % url)

    def get_items(self, collection, params):
        """Yield every feature from a collection, following pagination.

        Follows the `next` link when the server provides one; otherwise falls
        back to limit/offset paging using numberMatched / numberReturned.

        The API's last page can carry a `next` link with an empty `cursor`,
        which restarts from page 1. Paging therefore stops on an empty cursor
        or on a page with no unseen feature ids, and repeats are dropped.
        """
        url = "%s/collections/%s/items" % (self.base_url, collection)
        params = dict(params)
        params.setdefault("limit", PAGE_LIMIT)
        params.setdefault("f", "json")
        offset = 0
        page = 0
        following_links = False
        seen_ids = set()
        while True:
            page += 1
            data = self._get(url, params)
            features = data.get("features") or []
            new_count = 0
            for feature in features:
                fid = feature.get("id")
                if fid is not None:
                    if fid in seen_ids:
                        continue
                    seen_ids.add(fid)
                new_count += 1
                yield feature
            n_returned = data.get("numberReturned", len(features))
            n_matched = data.get("numberMatched")
            self._log("  page %d: %d features (matched: %s)" % (page, n_returned, n_matched))

            next_url = None
            for link in data.get("links") or []:
                if link.get("rel") == "next" and link.get("href"):
                    next_url = link["href"]
                    break
            if next_url and _has_empty_cursor(next_url):
                next_url = None

            if not features or new_count == 0:
                return
            if next_url:
                # The next link already carries every query parameter.
                url, params = next_url, None
                following_links = True
                continue
            if following_links:
                return
            offset += n_returned
            if n_matched is not None and offset < n_matched:
                params["offset"] = offset
                continue
            return

    def get_field_measurements(self, sites, time_interval, parameter_codes=None):
        """Return a list of property dicts for all matching field measurements."""
        records = []
        for i in range(0, len(sites), SITE_BATCH_SIZE):
            batch = sites[i:i + SITE_BATCH_SIZE]
            self._log("Requesting sites %d-%d of %d" % (i + 1, i + len(batch), len(sites)))
            params = {
                "monitoring_location_id": ",".join(batch),
                "time": time_interval,
                "skipGeometry": "true",
            }
            if parameter_codes:
                params["parameter_code"] = ",".join(parameter_codes)
            for feature in self.get_items(COLLECTION, params):
                props = dict(feature.get("properties") or {})
                props.setdefault("id", feature.get("id"))
                records.append(props)
        return records


# ---------------------------------------------------------------------------
# Counting and output
# ---------------------------------------------------------------------------

def summarize(records, sites):
    """Count measurements per site.

    Returns a list of dict rows, one per requested site (sites with no
    measurements get zero counts), with:
      - measurement_count: number of measurement records
      - field_visit_count: number of distinct field visits
      - one column per parameter code: records for that parameter
    """
    by_site = defaultdict(list)
    for rec in records:
        by_site[rec.get("monitoring_location_id")].append(rec)

    param_codes = sorted({str(r.get("parameter_code")) for r in records
                          if r.get("parameter_code") is not None})
    ordered_sites = list(sites) + sorted(s for s in by_site if s not in set(sites))

    rows = []
    for site in ordered_sites:
        recs = by_site.get(site, [])
        param_counts = Counter(str(r.get("parameter_code")) for r in recs)
        visits = {r.get("field_visit_id") for r in recs if r.get("field_visit_id")}
        times = sorted(str(r.get("time")) for r in recs if r.get("time"))
        row = {
            "monitoring_location_id": site,
            "measurement_count": len(recs),
            "field_visit_count": len(visits),
            "first_time": times[0] if times else "",
            "last_time": times[-1] if times else "",
        }
        for code in param_codes:
            row["param_" + code] = param_counts.get(code, 0)
        rows.append(row)
    return rows, param_codes


def write_csv(path, rows, fieldnames=None):
    if fieldnames is None:
        names = []
        for row in rows:
            for key in row:
                if key not in names:
                    names.append(key)
        fieldnames = names
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def print_summary(rows, param_codes, records, time_interval):
    print("")
    print("Date range (inclusive, by measurement date): %s" % time_interval)
    header = ["site", "measurements", "visits"] + ["p" + c for c in param_codes]
    widths = [max(16, max([len(r["monitoring_location_id"]) for r in rows] or [0]))]
    widths += [max(12, len(h)) for h in header[1:]]
    print("  ".join(h.ljust(w) for h, w in zip(header, widths)))
    print("  ".join("-" * w for w in widths))
    for r in rows:
        vals = [r["monitoring_location_id"], r["measurement_count"], r["field_visit_count"]]
        vals += [r["param_" + c] for c in param_codes]
        print("  ".join(str(v).ljust(w) for v, w in zip(vals, widths)))
    print("")
    sites_with_data = sum(1 for r in rows if r["measurement_count"] > 0)
    print("Total measurements: %d" % len(records))
    print("Total field visits: %d" % len({r.get("field_visit_id") for r in records
                                          if r.get("field_visit_id")}))
    print("Sites with measurements: %d of %d" % (sites_with_data, len(rows)))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(
        description="Retrieve and count USGS field measurements for sites and a date range.")
    p.add_argument("--start", required=True, help="Start date, inclusive (e.g. 1/1/2025)")
    p.add_argument("--end", required=True, help="End date, inclusive (e.g. 12/31/2025)")
    p.add_argument("--sites", nargs="*", default=[],
                   help="Site numbers (05331000) or IDs (USGS-05331000). Commas also OK.")
    p.add_argument("--sites-file", help="Text/CSV file of site numbers")
    p.add_argument("--parameter-code", nargs="*", default=[],
                   help="Optional parameter code filter, e.g. 00060 (discharge) 00065 (gage height)")
    p.add_argument("--out-dir", default=".",
                   help="Directory for the output CSV files (default: current dir)")
    p.add_argument("--no-files", action="store_true", help="Do not write CSV files")
    p.add_argument("--quiet", action="store_true", help="Suppress progress messages")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)

    raw_sites = []
    for item in args.sites:
        raw_sites.extend(item.split(","))
    if args.sites_file:
        raw_sites.extend(read_sites_file(args.sites_file))
    sites = unique_sites(raw_sites)
    padded = [s.strip() for s in raw_sites
              if s.strip().isdigit() and len(s.strip()) < 8]
    if padded:
        print("Note: zero-padded %d site number(s) to 8 digits (e.g. %s -> %s)."
              % (len(padded), padded[0], normalize_site(padded[0])), file=sys.stderr)
    if not sites:
        print("No sites given. Use --sites and/or --sites-file.", file=sys.stderr)
        return 2

    try:
        start_date = parse_date(args.start)
        end_date = parse_date(args.end)
        time_interval = build_time_interval(start_date, end_date)
    except Exception as exc:
        print("Error: %s" % exc, file=sys.stderr)
        return 2

    param_codes = []
    for item in args.parameter_code:
        param_codes.extend(c.strip() for c in item.split(",") if c.strip())

    api_key = get_api_key()
    if not api_key:
        print("Warning: %s is not set; using the lower anonymous rate limit." % API_KEY_ENV,
              file=sys.stderr)

    client = FieldMeasurementClient(api_key=api_key, verbose=not args.quiet)
    try:
        records = client.get_field_measurements(sites, time_interval, param_codes)
    except UsgsApiError as exc:
        print("Error: %s" % exc, file=sys.stderr)
        return 1

    rows, codes = summarize(records, sites)
    print_summary(rows, codes, records, time_interval)

    if not args.no_files:
        os.makedirs(args.out_dir, exist_ok=True)
        tag = "%s_%s" % (start_date.strftime("%Y%m%d"), end_date.strftime("%Y%m%d"))
        meas_path = os.path.join(args.out_dir, "field_measurements_%s.csv" % tag)
        summ_path = os.path.join(args.out_dir, "field_measurement_counts_%s.csv" % tag)
        records.sort(key=lambda r: (str(r.get("monitoring_location_id")), str(r.get("time"))))
        write_csv(meas_path, records)
        write_csv(summ_path, rows)
        print("")
        print("Wrote %s" % meas_path)
        print("Wrote %s" % summ_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())

# USGS Field Measurements Retriever

A command-line Python tool that downloads USGS **field measurements** (discharge,
gage height, and related readings collected on site visits) for a list of
monitoring locations and a date range, then counts them per site.

It uses the USGS Water Data OGC API, the modern replacement for the legacy NWIS
web services:

```
https://api.waterdata.usgs.gov/ogcapi/v1/collections/field-measurements/items
```

Example: "How many discharge measurements were made at these 30 Minnesota
gages between July 1, 2025 and June 30, 2026?"

```
python field_measurements.py --start 7/1/2025 --end 6/30/2026 --parameter-code 00060 \
    --sites-file my_sites.txt
```

---

## Contents

- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [API key setup](#api-key-setup)
- [Usage](#usage)
- [Command-line options](#command-line-options)
- [Specifying sites](#specifying-sites)
- [Output](#output)
- [How counting works](#how-counting-works)
- [Date handling](#date-handling)
- [Common parameter codes](#common-parameter-codes)
- [Using it from Python](#using-it-from-python)
- [Running the tests](#running-the-tests)
- [Known API behaviors](#known-api-behaviors)
- [Troubleshooting](#troubleshooting)
- [Project structure](#project-structure)
- [References](#references)

---

## Features

- Inclusive date range input in `M/D/YYYY` or `YYYY-MM-DD` format.
- Sites given on the command line, in a text/CSV file, or both.
- Accepts site numbers with or without the `USGS-` prefix, and repairs site
  numbers that lost their leading zero in Excel (`5331000` -> `05331000`).
- Optional filter by parameter code (for example, discharge only).
- Handles pagination, retries on rate limiting (HTTP 429) and server errors,
  and batches large site lists into multiple requests.
- Prints a per-site summary table and writes two CSV files: every measurement
  record, and the per-site counts.
- Reads the API key from a `.env` file, and sends it only to the USGS API host.
- Depends only on the Python standard library plus `requests`.

## Requirements

- Python 3.7 or newer (tested on Python 3.13)
- `requests`
- A USGS Water Data API key (recommended, see below)

## Installation

```
git clone https://github.com/<your-account>/USGS_API_FieldMeas.git
cd USGS_API_FieldMeas
python -m venv .venv
# Windows:      .venv\Scripts\activate
# macOS/Linux:  source .venv/bin/activate
pip install -r requirements.txt
```

## API key setup

1. Request a free key at <https://api.waterdata.usgs.gov/signup/>.
2. Copy `.env.example` to `.env` and paste in your key:

   ```
   API_USGS_PAT=your-key-here
   ```

The script looks for `.env` in the current working directory and in the folder
containing the script. A value already set in your environment takes
precedence. `API_USGS_PAT` is the same variable name used by the USGS
[`dataretrieval`](https://github.com/DOI-USGS/dataretrieval-python) library,
so one `.env` serves both.

The key is sent in the `X-Api-Key` HTTP header, and only to
`api.waterdata.usgs.gov`.

**Is a key required?** No. Requests without a key are accepted but share a much
lower anonymous rate limit, so larger queries may pause for several minutes
while the script waits out an HTTP 429. With a key, the same queries run
without those pauses. The script prints a warning when no key is found.

**Never commit `.env`.** It is listed in `.gitignore`.

## Usage

Count all field measurements at two sites for calendar year 2025:

```
python field_measurements.py --start 1/1/2025 --end 12/31/2025 --sites 05331000 05330000
```

Discharge measurements only, for a water year, from a site file:

```
python field_measurements.py --start 2024-10-01 --end 2025-09-30 \
    --parameter-code 00060 --sites-file sites_example.txt
```

Write the output CSVs to a folder and suppress progress messages:

```
python field_measurements.py --start 7/1/2025 --end 6/30/2026 --parameter-code 00060 \
    --sites 4015330,5062000,5075000 --out-dir output --quiet
```

Print the summary only, without writing files:

```
python field_measurements.py --start 1/1/2025 --end 12/31/2025 --sites 05331000 --no-files
```

On Windows, put a command on one line or replace the trailing `\` with `^`.

## Command-line options

| Option | Required | Description |
|---|---|---|
| `--start` | yes | Start date, inclusive. `M/D/YYYY`, `YYYY-MM-DD`, `M-D-YYYY`, or `YYYY/MM/DD`. |
| `--end` | yes | End date, inclusive. Same formats. |
| `--sites` | one of `--sites` / `--sites-file` | Site numbers or IDs, separated by spaces and/or commas. |
| `--sites-file` | one of `--sites` / `--sites-file` | Path to a text or CSV file of sites (see below). |
| `--parameter-code` | no | One or more parameter codes to keep, e.g. `00060`. Default: all. |
| `--out-dir` | no | Folder for the output CSV files. Created if missing. Default: current folder. |
| `--no-files` | no | Print the summary only. |
| `--quiet` | no | Hide per-request progress messages. |

Exit codes: `0` success, `1` API error, `2` invalid input.

## Specifying sites

All of these refer to the same site:

```
05331000      USGS-05331000      usgs-05331000      5331000
```

- A bare number gets the `USGS-` prefix.
- All-digit numbers shorter than 8 digits are zero-padded to 8 digits, because
  USGS site numbers are at least 8 digits and spreadsheets often drop the
  leading zero. The script prints a note when it does this. Longer site numbers
  (for example 15-digit well IDs) are left alone.
- Duplicates are removed; input order is kept in the output.

**Site file format.** One site per line, or several per line separated by
commas. Blank lines and lines starting with `#` are skipped, and a header cell
named `site_no`, `site`, or `monitoring_location_id` is ignored, so a
single-column CSV exported from Excel works as is:

```
# Twin Cities gages
site_no
05331000
05330000, 05288500
```

## Output

### Console summary

```
Date range (inclusive, by measurement date): 2025-01-01/2025-12-31
site              measurements  visits        p00060        p00065        p72254        p72255
----------------  ------------  ------------  ------------  ------------  ------------  ------------
USGS-05331000     31            6             5             16            5             5
USGS-05330000     32            8             8             24            0             0

Total measurements: 63
Total field visits: 14
Sites with measurements: 2 of 2
```

Every requested site gets a row, including sites with zero measurements.

### CSV files

Both file names include the date range, e.g. `20250101_20251231`.

**`field_measurement_counts_<start>_<end>.csv`**: one row per site.

| Column | Meaning |
|---|---|
| `monitoring_location_id` | Site ID, e.g. `USGS-05331000` |
| `measurement_count` | Number of measurement records in the range |
| `field_visit_count` | Number of distinct field visits (`field_visit_id`) |
| `first_time`, `last_time` | Earliest and latest measurement date in the range |
| `param_<code>` | Records for each parameter code present in the results |

**`field_measurements_<start>_<end>.csv`**: every record returned by the API,
sorted by site and date, with all API fields. The most useful ones:

| Field | Example | Meaning |
|---|---|---|
| `monitoring_location_id` | `USGS-05331000` | Site |
| `time` | `2025-11-17` | Measurement date (UTC, see [Date handling](#date-handling)) |
| `time_of_day` | `17:39:03+00:00` | Measurement time, UTC |
| `parameter_code` | `00060` | What was measured |
| `value`, `unit_of_measure` | `7740`, `ft^3/s` | The reading |
| `reading_type` | `Discharge` | Kind of reading |
| `observing_procedure` | `Acoustic Doppler Current Profiler` | Method |
| `measurement_rated` | `Fair` | Quality rating |
| `approval_status` | `Approved` / `Provisional` | Review status |
| `control_condition` | `Clear` | Channel control condition |
| `field_visit_id` | UUID | Groups records from one site visit |
| `id` | UUID | Unique record ID |

The full field list is in the API's
[queryables](https://api.waterdata.usgs.gov/ogcapi/v1/collections/field-measurements/queryables).

## How counting works

- **`measurement_count`** counts records. One field visit usually produces
  several records: for example a discharge measurement plus one or more gage
  height readings. Use `--parameter-code 00060` to count discharge
  measurements only.
- **`field_visit_count`** counts distinct `field_visit_id` values, i.e. trips
  to the site.
- A visit can contain more than one record of the *same* parameter. For
  discharge, this is usually a repeat or check measurement made later in the
  same visit, often by a second method (for example ADCP and mid-section).
  These are separate measurements with their own times and values, not
  duplicates, so they are counted individually. If you want one per trip, use
  `field_visit_count`.
- Provisional and approved records are both included; see `approval_status`
  in the detail CSV to separate them.

## Date handling

- Both `--start` and `--end` are **inclusive**.
- The API stores each measurement's `time` as a **date** (`YYYY-MM-DD`), with
  the clock time in a separate `time_of_day` field in UTC. The script sends a
  date-only range and the API matches whole days, so a measurement counts if
  its `time` date falls in the range.
- That date is the **UTC date**. A measurement taken at 8:02 pm Central on
  August 13 (01:02 UTC August 14) is dated August 14. In practice this only
  matters for evening measurements on the first or last day of a range; check
  `time_of_day` in the detail CSV if that edge case matters to you.
- Some very old records have an unknown month or day (`month`/`day` are null in
  the API). This does not affect modern date ranges.

## Common parameter codes

| Code | Description |
|---|---|
| `00060` | Discharge (streamflow), ft^3/s |
| `00065` | Gage height, ft |
| `72254` | Sensor velocity, ft/s (index-velocity stations) |
| `72255` | Mean water velocity, ft/s |
| `72019` | Water level, depth below land surface, ft (groundwater) |

Other codes can be looked up in the API's
[parameter-codes collection](https://api.waterdata.usgs.gov/ogcapi/v1/collections/parameter-codes/items),
e.g. `.../parameter-codes/items?id=00060`.

## Using it from Python

The script can also be imported:

```python
import field_measurements as fm

sites = fm.unique_sites(["05331000", "5330000"])          # -> USGS-05331000, USGS-05330000
interval = fm.build_time_interval(fm.parse_date("7/1/2025"),
                                  fm.parse_date("6/30/2026"))

client = fm.FieldMeasurementClient(api_key=fm.get_api_key(), verbose=False)
records = client.get_field_measurements(sites, interval, parameter_codes=["00060"])

rows, codes = fm.summarize(records, sites)
for row in rows:
    print(row["monitoring_location_id"], row["measurement_count"], row["field_visit_count"])
```

`records` is a list of dicts (the API's `properties` for each measurement), so
it converts directly to a pandas DataFrame with `pandas.DataFrame(records)` if
you use pandas.

## Running the tests

The tests mock all HTTP calls, so they run offline and need no API key:

```
python -m unittest -v test_field_measurements
```

They cover date parsing, site normalization and zero-padding, `.env` loading,
pagination (including the API quirk below), retries, site batching, the
API-key host check, and the summary counts.

## Known API behaviors

Found while testing against the live API; the script handles each of them.

- **Pagination loops back to the start.** Results are paged with a `cursor` in
  the `next` link. On the last page the `next` link can contain an empty
  cursor (`cursor=&...`), and following it restarts from page 1. The script
  stops on an empty cursor, and also stops if a page contains no records it
  has not already seen.
- **No total count.** Responses do not include `numberMatched`, so progress
  messages show `matched: None`. That is expected.
- **Whole-day time filtering.** The `time` filter compares dates only; a time
  of day in the query is ignored.
- **Page size.** The script requests up to 10,000 records per page and up to
  50 sites per request (`PAGE_LIMIT` and `SITE_BATCH_SIZE` near the top of the
  script).

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Warning: API_USGS_PAT is not set` | No key found. Create `.env` next to the script or in the folder you run from. |
| `HTTP 401` / `HTTP 403` | Key missing or invalid. Check `.env` for typos or extra spaces; request a new key if needed. |
| `HTTP 429; retrying in Ns` | Rate limit reached. The script waits and retries automatically. Using a key avoids most of these. |
| A site shows 0 measurements | Check the site number, and that the site had field visits in the range. Look the site up at `https://waterdata.usgs.gov/monitoring-location/USGS-<site_no>/`. |
| `Unrecognized date` | Use `M/D/YYYY` or `YYYY-MM-DD`. Day-first dates (`31/12/2025`) are not accepted. |
| SSL or proxy errors | Corporate networks may need `REQUESTS_CA_BUNDLE` or `HTTPS_PROXY` set; `requests` reads both. |

## Project structure

```
USGS_API_FieldMeas/
|-- field_measurements.py        Script and importable functions
|-- test_field_measurements.py   Offline unit tests
|-- requirements.txt             Python dependencies
|-- sites_example.txt            Example site list
|-- .env.example                 Template for the API key file
|-- .gitignore                   Excludes .env, caches, and output CSVs
`-- README.md
```

Output CSVs (`output/`, `field_measurements_*.csv`,
`field_measurement_counts_*.csv`) are ignored by git. Remove those lines from
`.gitignore` if you want to commit results.

## References

- API landing page and key sign-up: <https://api.waterdata.usgs.gov/>,
  <https://api.waterdata.usgs.gov/signup/>
- OpenAPI documentation: <https://api.waterdata.usgs.gov/ogcapi/v1/openapi?f=html>
- Field measurements collection:
  <https://api.waterdata.usgs.gov/ogcapi/v1/collections/field-measurements>
- USGS `dataretrieval` Python package:
  <https://github.com/DOI-USGS/dataretrieval-python>

Data are provided by the U.S. Geological Survey. Provisional data are subject
to revision.

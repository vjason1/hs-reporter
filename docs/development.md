# Development

## Project layout

```
app/
  main.py        FastAPI app: API routes, sign-in, startup
  reports.py     report catalog, HammerScript builder, runner, display and exports
  hsparse.py     parser for hs output (text and JSON forms), unit conversion
  shares.py      NFS/SMB mounting, stale-mount detection, folder browsing
  scheduler.py   cron schedules (APScheduler)
  store.py       JSON-file storage under HSR_DATA_DIR
  hs2csv.py      command-line converter from hs output to CSV
  hsvalue.py     parser for HammerScript text values (ITEM_TABLE{...}, LOCAL_TIME(...), 33 SECONDS)
  clusterinfo.py parsers for volume-list, object-volume-list, volume-group-list, objective-list
  objexpr.py     objective conditions: parse, validate, evaluate
  objplan.py     objective planning: scan, placement model, space per volume, commands
  static/        the single-page web GUI (index.html)
tests/
  fake_hs.py     stand-in for the hs command, answering like Hammerspace
  fixtures/      real CLI exports and a full file metadata dump, for the parsers
  test_*.py      parser, builder, planning and API tests
docs/            guides and screenshots
```

## Running locally without a cluster

`tests/fake_hs.py` answers `hs` commands with output shaped like Hammerspace's, so the whole
app runs without a cluster:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
mkdir -p /tmp/hsr/share/projects/{a,b}
HSR_DATA_DIR=/tmp/hsr/data HSR_LOCAL_ROOT=/tmp/hsr/share HSR_HS_BIN=$PWD/tests/fake_hs.py \
  uvicorn app.main:app --reload --port 8080
```

Add a share of type **Already mounted** with the path `/tmp/hsr/share`.

## Tests

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest
```

The GitHub Actions workflow runs the tests and builds the Docker image on every push and
pull request.

## Hammerspace output formats

`hs -j sum` returns tables as JSON:

```json
{"SUMS_TABLE": [{"KEY": {"HAMMERSCRIPT": "STORAGE_VOLUME('dsx-1::/hsvol0')"},
                 "VALUE": [[400, 174390123, {"TOP10_TABLE": [{"KEY": [["./big.iso", 7278611]]}]}]]}]}
```

Without `-j`, the same data comes as tab-separated lines with unit-tagged sizes such as
`{"MBYTES":40.354}` (decimal units). `hsparse.py` handles both, unwraps keys
(`STORAGE_VOLUME('…')`, `OWNER('alice|1001')` → `alice`), turns `#EMPTY` into zeros, and
converts sizes to bytes. Reports ask for `/files` and `/bytes` so values arrive as exact
numbers. Runs keep their raw output, and are re-parsed from it when the parser changes
(`PARSER_VERSION` in `reports.py`).

`hs -n` (dry run) fails in hstk 4.6.6.1, so the designer builds its command preview itself.

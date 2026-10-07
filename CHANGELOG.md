# Changelog

## 1.2.0

- **Import shares from the cluster**: paste or upload `share-list` output, enter the cluster's
  IP or FQDN, choose NFS, SMB or both, and tick the shares to add (the root share is left
  out; existing shares are skipped). Shares whose exports only allow privileged ports are
  flagged, with a reminder to enable insecure ports on the export or add an export rule for
  this machine; failed NFS mounts with "access denied" repeat it.
- **Settings page** (gear icon) for global configuration: crawl speed (Normal, Gentle,
  Slowest, Custom) for all reports, schedules and objective-planning scans; limits (hs
  commands at once across everything, reports and scans at once, timeouts, output, folder
  and scan sizes); and default NFS and SMB mount options. Changes apply without a restart.
- Crawl speed is no longer set per report or per plan; the designer and plan pages show the
  speed in effect with a link to Settings.
- Environment variables still work, as initial values for anything not changed on the page.

## 1.1.1

- A run or scan whose `hs` command can't be started (not installed, not executable, wrong
  `HSR_HS_BIN`) now fails at once with a clear message instead of staying "running".
- Tests make the stand-in `hs` executable themselves; CI uses actions/checkout@v5 and
  actions/setup-python@v6, pins Ubuntu 24.04, and has job time limits.

## 1.1.0

- **Objective planning** (new Objectives tab): scan a share for only the metadata fields the
  objective conditions need (one recursive `hs eval`, with a batched per-file fallback),
  load `volume-list`, `object-volume-list`, `volume-group-list` and `objective-list` exports,
  build share objectives with scopes (share, folder, file) and conditions (dropdown builder
  or typed, validated as you type with match counts), and calculate space per volume.
- Placement model: place-on instances, availability and durability copies on separate
  failure domains, confine/exclude, keep-online, optimize-for-capacity, read-only volumes
  excluded, small files kept in metadata on object volumes.
- Space results per placement target: a targeted volume group is one row with free space
  totalled across its volumes (members on request); **Calculate space** moved between the
  assumptions and the results.
- Condition builder: one **Add condition** button; the AND / OR choice appears only once
  there's a condition to combine with; the builder resets after each part; **Clear condition**.
- Syntax highlighting no longer garbles single-quoted text such as `FNMATCH('*.log', NAME)`.
- File name and path matching with `FNMATCH('*.log', NAME)?TRUE` and
  `!FNMATCH('*/scratch/*', PATH)?TRUE`, in the builder and typed conditions; C-style
  `cond ? a : b` conditions.
- Coverage warnings with a suggested catch-all condition, unsatisfiable-objective and
  capacity warnings, and generated `share-objective-add` commands: `--path` for folder and
  file scopes, and conditions as `--applicability` statements.

## 1.0.0

First release.

- Web GUI styled after the Hammerspace console, with the Hammerspace logo.
- Default reports: capacity per folder and per full directory walk, per storage volume, per
  user per volume, totals with the top 10 files, top 10 files per volume not accessed in 2+
  days, owner, MIME type, alignment, active objective, virus scan state, open files, file
  listing and volume status.
- Report designer with groupings, metrics, filters (size, access age, open, online, errors,
  custom), per-folder breakdown to any depth, and subfolder selection by path or browsing.
- Editable HammerScript before and after a run, with live command preview, saved edits and
  reset to the generated expression.
- One display unit for sizes (bytes to TB) across the table and downloads.
- Downloads: analysis CSV with options and live preview, largest-files CSV, table as shown,
  JSON, Prometheus metrics and raw output; `hs2csv` for hs output run by hand.
- Schedules with retention, CSV exports and history files.
- NFS v3 and SMB mounting from the container, host-mounted shares, stale-mount detection with
  automatic remount and retry.
- Crawl speed per report (Normal, Gentle, Slowest, Custom) and a server-wide cap on
  concurrent `hs` commands.
- Optional sign-in, JSON API, tests with a stand-in `hs`, and CI.

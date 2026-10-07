# API

Everything the GUI does is available as JSON over HTTP. With sign-in turned on, send HTTP
Basic credentials (`curl -u user:password ...`).

## Shares

| Method and path | Purpose |
|---|---|
| `GET /api/shares` | List shares with mount status |
| `POST /api/shares` | Add a share |
| `PUT /api/shares/{id}` / `DELETE /api/shares/{id}` | Change or remove a share |
| `POST /api/shares/{id}/mount` / `unmount` / `remount` | Mount control |
| `GET /api/shares/{id}/browse?path=/projects` | List the folders in a path |
| `POST /api/shares/import/parse` | Read `share-list` output: `{"text": "..."}` → the shares, without the root share |
| `POST /api/shares/import` | Add shares: `server`, `protocols` (`["nfs"]`, `["smb"]` or both), `shares` (`[{"name","path"}]`), SMB `username`/`password`/`domain`, `mount`, `auto_mount` |

```bash
curl -u admin:pw -X POST localhost:8080/api/shares -H 'content-type: application/json' \
  -d '{"name":"projects","kind":"nfs","server":"anvil.example.com","export":"/projects","auto_mount":true}'
```

`kind` is `nfs`, `smb` (add `username`, `password`, `domain`) or `local` (with `local_path`).

## Reports

| Method and path | Purpose |
|---|---|
| `GET /api/catalog` | Groupings, metrics, fields, default reports and other choices |
| `POST /api/preview` | The expression, command and columns for a report design |
| `GET /api/definitions` / `POST /api/definitions` | List or save reports |
| `GET/PUT/DELETE /api/definitions/{id}` | Read, change or delete a report |
| `POST /api/definitions/{id}/run` | Run a report; returns `{"run_id": ...}` |
| `POST /api/run` | Run an unsaved report design |

A report design, for example capacity per owner and volume for one folder, shown in GB:

```json
{
  "name": "Usage per user per volume",
  "share_id": "a1b2c3d4e5f6",
  "paths": ["/projects/2026"],
  "mode": "sum",
  "sum": {"group_by": ["owner", "group", "volume"], "metrics": ["file_count", "space_used"], "top_n": 10},
  "filters": {"access_age_days": 30},
  "folders": {"enabled": false},
  "display": {"size_unit": "gb", "sort_by": "Space used"}
}
```

`mode` is `sum`, `eval` or `custom`. Crawl speed is a global setting (see Settings below).

```bash
curl -u admin:pw -X POST localhost:8080/api/definitions/$ID/run -H 'content-type: application/json' \
  -d '{"expression":"IS_FILE?SUMS_TABLE{|KEY=OWNER,|VALUE={1FILE/files,SPACE_USED/bytes}}"}'
```

Set `expression_override` on a report to save an edited expression.

## Results

| Method and path | Purpose |
|---|---|
| `GET /api/runs` | Recent runs (filter with `definition_id` or `schedule_id`) |
| `GET /api/runs/{id}` | A run with its table; `fields`, `sort_by`, `sort_desc`, `limit`, `size_unit` adjust it |
| `GET /api/runs/{id}/export?format=...` | Downloads (below) |
| `DELETE /api/runs/{id}` | Delete a run |

Export formats:

| `format` | Options |
|---|---|
| `analysis` | `unit` (`bytes`, `kb`, `mb`, `gb`, `tb`), `decimals`, `meta` (`true`/`false`), `paths` (`full`/`relative`) |
| `files` | same as `analysis`; one row per largest file |
| `csv`, `json` | table as shown: `fields`, `sort_by`, `sort_desc`, `size_unit` |
| `prometheus` | per-folder reports |
| `raw` | raw `hs` output |

```bash
curl -u admin:pw "localhost:8080/api/runs/$RUN/export?format=analysis&unit=gb&decimals=2&meta=false" -o usage.csv
```

## Objective plans

| Method and path | Purpose |
|---|---|
| `GET /api/plan-catalog` | Fields offered for conditions, with types |
| `GET /api/plans` / `POST /api/plans` | List or create (`name`, `share_id`, `root`) |
| `GET/PUT/DELETE /api/plans/{id}` | Read, change (`fields`, `metas`, `rows`, `settings`, `cluster_share`) or delete |
| `POST /api/plans/{id}/scan` | Start a scan; progress is in the plan's `scan` |
| `POST /api/plans/{id}/cluster` | Load a CLI export: `{"text": "..."}`; the kind is detected |
| `POST /api/plans/{id}/check` | Check a condition: `{"condition": "...", "scope": {...}}` → errors or match count |
| `GET /api/plans/{id}/folders?path=/` | Scanned folders, for choosing scopes |
| `POST /api/plans/{id}/calculate` | Calculate space per volume |
| `GET /api/plans/{id}/commands` | The commands to apply the plan, as text |

A row: `{"objective": "place-on-object-volumes", "scope": {"type": "folder", "path": "/projects"},
"condition": "MODIFY_AGE>90*DAYS"}`. Scope types are `share`, `folder` and `file` (with `name`).

## Settings

| Method and path | Purpose |
|---|---|
| `GET /api/settings` | Current values, defaults, crawl speed presets and allowed ranges |
| `PUT /api/settings` | Change any of them; invalid values are rejected with a message |
| `POST /api/settings/reset` | Back to the defaults |

```bash
curl -u admin:pw -X PUT localhost:8080/api/settings -H 'content-type: application/json' \
  -d '{"crawl_preset":"custom","crawl_concurrency":1,"crawl_pause":2,"crawl_list_rate":10,"max_hs_processes":2}'
```

Keys: `crawl_preset` (`normal`, `gentle`, `slowest`, `custom`), `crawl_concurrency`,
`crawl_pause`, `crawl_list_rate`, `max_hs_processes`, `max_concurrent`, `run_timeout`,
`max_output_mb`, `max_folders`, `plan_max_files`, `plan_batch`, `nfs_options`, `smb_options`.

## Sign-in

| Method and path | Purpose |
|---|---|
| `GET /api/auth` | Whether sign-in is on, the username, and where the password comes from |
| `POST /api/auth/password` | `{"current", "new", "confirm"}`: change the password |
| `POST /api/auth/enable` | `{"username", "password", "confirm"}`: turn sign-in on when it's off |

## Schedules

| Method and path | Purpose |
|---|---|
| `GET /api/schedules` / `POST /api/schedules` | List or create (`definition_id`, `cron`, `keep_last`, `export_csv`) |
| `PUT/DELETE /api/schedules/{id}` | Change (including `enabled`) or delete |
| `POST /api/schedules/{id}/run` | Run now |
| `GET /api/exports` | List exported CSV files |

`GET /api/health` reports whether the service is up and is never behind sign-in.

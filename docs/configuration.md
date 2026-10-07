# Configuration

Settings are environment variables, set in `docker-compose.yml`.

| Variable | Default | Purpose |
|---|---|---|
| `HSR_USER`, `HSR_PASSWORD` | unset | Sign-in for the GUI and API (HTTP Basic). Unset turns sign-in off |
| `TZ` | `UTC` | Time zone for schedules |
| `HSR_NFS_OPTIONS` | `vers=3,nolock` | Default NFS mount options |
| `HSR_SMB_OPTIONS` | `vers=3.0,noserverino,cache=none,actimeo=0` | Default SMB mount options |
| `HSR_MAX_CONCURRENT` | `2` | Reports allowed to run at once; others queue |
| `HSR_MAX_HS_PROCESSES` | `4` | `hs` commands allowed at once across all reports |
| `HSR_FOLDER_CONCURRENCY` | `4` | `hs` commands at once for the **Normal** crawl speed |
| `HSR_COMMAND_PAUSE` | `0` | Seconds between `hs` commands for the **Normal** crawl speed |
| `HSR_LIST_RATE` | `0` | Folder listings per second for the **Normal** crawl speed (0 = no limit) |
| `HSR_MAX_FOLDERS` | `2000` | Most folders one per-folder report scans |
| `HSR_RUN_TIMEOUT` | `3600` | Seconds before an `hs` command is stopped |
| `HSR_MAX_OUTPUT_MB` | `50` | Most output kept per `hs` command |
| `HSR_DATA_DIR` | `/data` | Where shares, reports, schedules, results and exports are stored |
| `HSR_MOUNT_ROOT` | `/mnt/hs` | Where the container mounts NFS and SMB shares |
| `HSR_LOCAL_ROOT` | `/mnt/external` | The only place "Already mounted" shares may point |
| `HSR_HS_BIN` | `hs` | The hstk command |
| `HSR_PLAN_MAX_FILES` | `1000000` | Most entries an objective-planning scan records |
| `HSR_PLAN_BATCH` | `100` | Files per `hs eval` call when a scan gathers metadata file by file |

## Protecting the cluster

To make the service gentler for everyone, lower `HSR_MAX_HS_PROCESSES` (for example to `1`)
and `HSR_MAX_CONCURRENT`, and give **Normal** a pause or listing limit with
`HSR_COMMAND_PAUSE` and `HSR_LIST_RATE`. Individual reports can go slower still with their
crawl speed setting (see the [user guide](user-guide.md#crawl-speed)).

## Sign-in

With `HSR_USER` and `HSR_PASSWORD` set, the browser asks for them, and API calls need HTTP
Basic credentials. `/api/health` stays open for health checks. Put the service behind HTTPS
(for example a reverse proxy) if it's reachable beyond a trusted network.

## hstk version

The image installs hstk from PyPI. To pin a version or build from source:

```bash
docker compose build --build-arg HSTK_SPEC="hstk==4.6.6.1"
docker compose build --build-arg HSTK_SPEC="git+https://github.com/hammer-space/hstk.git@master"
```

hstk 4.6.6 and later need Hammerspace 4.6.5 or later.

## Container privileges

Mounting NFS or SMB inside the container needs the `SYS_ADMIN` and `DAC_READ_SEARCH`
capabilities and AppArmor unconfined, as in the provided compose file. If mounts still fail
with "permission denied", use `privileged: true`. To avoid mount privileges entirely, mount
shares on the host instead ([Shares and mounting](shares.md#shares-mounted-on-the-host)).

## Data and backups

Everything the service keeps is under `/data` (the `hsr-data` volume):

| Path | Contents |
|---|---|
| `shares.json` | Share definitions, including SMB passwords (file mode 0600) |
| `creds/` | SMB credentials files (0600) |
| `definitions.json` | Saved reports |
| `schedules.json` | Schedules |
| `runs/` | One file per result, including the raw `hs` output |
| `exports/` | Scheduled CSV exports and history files |

Back up the volume to keep reports and history; protect it, since it holds SMB passwords.

## Scaling

The scheduler and run queue live in the one process, so run a single container with one
worker. Don't run several replicas against the same `/data`.

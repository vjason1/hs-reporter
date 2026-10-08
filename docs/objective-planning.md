# Objective planning

The **Objectives** tab models a share's objectives before you apply them: which files each
objective would apply to, where the data would be placed, and how much space each volume
would need. It uses the same shares as reports.

![Objective plan](screenshots/objectives-plan.png)

## Workflow

1. **Share and folder.** Pick a share and the folder to model (`/` for the whole share), and
   the share's name on the cluster, used as `--name` in the generated commands (filled in from
   the cluster's share list for imported shares). The plan's cluster is shown in its header.
2. **Fields to gather, and scan.** Choose the metadata your conditions use. Most designs need
   one to five fields; the scan asks Hammerspace for those and nothing else.
3. **Cluster information.** **Load all from** the plan's cluster (the share's cluster, or one
   you choose when the share isn't linked; see [Clusters](shares.md#clusters)), or upload or
   paste the output of
   `volume-list --full`, `object-volume-list --full`, `volume-group-list --full` and
   `objective-list --full`. Each card says where its data came from, including which cluster.
4. **Share objectives.** Add objectives, each with a scope and an optional condition.
5. **Placement assumptions.** Adjust the default placement and size rules if needed.
6. **Calculate space.** See the space needed per volume group or volume, warnings, and the
   commands to apply the plan.

## Scanning

The scan walks the folder itself (file names, paths and folder names come from the walk, so
folders that scopes and paths refer to are always known) and then gathers the chosen fields
with `hs eval`, using a tuple that starts with `DPATH` so every result identifies its file:

```
hs eval -r -e '{DPATH,SIZE,SPACE_USED,MODIFY_AGE,GET_TAG("project")}' /mnt/hs/<share>/<folder>
```

It first tries one recursive evaluation (`-r`, a single gateway call for the whole tree). Any
files that call doesn't return are gathered with batched per-file calls
(`hs eval -e ... file1 file2 ...`, read using the `##### path` headers hstk prints). The scan
status shows which method was used. The global crawl speed
([Settings](configuration.md#crawl-speed)) paces both the folder walk and
the per-file calls.

**Fields offered** are the ones most used in objective conditions: sizes (`SIZE`,
`SPACE_USED`), ages (`MODIFY_AGE`, `ACCESS_AGE`, `LAST_USE_AGE`, `ACTUAL_*_AGE`, ...), times
(`MODIFY_TIME`, ...), ownership (`OWNER`, `OWNER_GROUP`), `TYPE`, `PARENT_SHARE`, labels
(`ALL_LABELS`), and flags (`IS_ONLINE`, `IS_OPEN`, `DATA_ORIGIN_LOCAL`, `IS_SNAP`, ...).
`SIZE` and `SPACE_USED` are always gathered because the space calculation needs them.

**Tags, labels, keywords and attributes** aren't in a file's standard metadata. Add them by
name as `GET_TAG("name")`, `HAS_TAG("name")`, `HAS_LABEL("name")`, `HAS_KEYWORD("name")`,
`GET_ATTRIBUTE("name")` or `HAS_ATTRIBUTE("name")`; the scan evaluates each per file, the
same way hstk's `hs tag get` and similar commands do.

If a condition uses a field the scan didn't gather, the plan says so, offers to add it, and
won't calculate until you scan again.

## Objectives, scopes and conditions

![Share objectives](screenshots/objectives-rows.png)

Each row is one objective from the uploaded objective list, with:

- **Applies to**: the whole share, a folder (everything below it), or one file. These map to
  `share-objective-add`'s `--path` option: omitted for the whole share, or the folder's or
  file's path within the share.
- **Condition** (optional): a HammerScript condition, typed or built with the builder below
  it. It's checked as you type, with the number of scanned files it matches and examples.

**Building a condition.** Choose a field, an operator and a value, and click **Add condition**
(or press Enter). The builder then resets for the next part. Once there's a condition, an
**AND / OR** choice appears at the start of the builder to say how the next part combines
with it, and the button reads **Add**. **Clear condition** starts over. You can also type or
edit the condition directly; the builder and the checks follow along.

Condition syntax follows Hammerspace's: `MODIFY_AGE>90*DAYS`, `ACCESS_AGE>=2DAYS`,
`SIZE<2*MBYTES AND IS_ONLINE`, `OWNER==USER('alice@example.com')`,
`MODIFY_TIME<LOCAL_TIME('2026-01-01')`, `MODIFY_TIME<NOW-30*DAYS`, `GET_TAG("tier")=="cold"`,
`NOT (...)`, `OR`. Units: `BYTES` to `PBYTES` (decimal) and `SECONDS` to `YEARS`. Mistakes are
reported with suggestions ("Unknown unit 'DAYZ'; did you mean DAYS?").

**File names and paths** are matched with `FNMATCH`, and negated with `!`:

| Condition | Matches |
|---|---|
| `FNMATCH('*.log', NAME)?TRUE` | files named `*.log` |
| `!FNMATCH('*.log', NAME)?TRUE` | files not named `*.log` |
| `FNMATCH('*/scratch/*', PATH)?TRUE` | files anywhere below a `scratch` folder |
| `!FNMATCH('*/scratch/*', PATH)?TRUE` | files not below a `scratch` folder |

Patterns use shell wildcards (`*`, `?`, `[abc]`), are case-sensitive, and `*` also matches
`/`. `PATH` is the path from the share's root as Hammerspace reports it
(`./projects/scratch/run.log`), whichever folder the plan models. In the builder, choose
`NAME` or `PATH` with **matches** or **doesn't match**. When combined with other conditions,
each `FNMATCH(...)?TRUE` is kept in parentheses:
`MODIFY_AGE>90*DAYS AND (!FNMATCH('*/scratch/*', PATH)?TRUE)`. The C-style `cond ? a : b`
form is supported generally.

## How space is calculated

This is a model of Hammerspace's placement, not the cluster's own engine:

- **Instances.** Each place-on objective that applies to a file needs one instance in its
  target; a volume group means any one of its volumes. Several place-on objectives mean
  several instances.
- **Availability and durability.** Objectives such as `availability-3-nines` add instances,
  each in a different failure domain (storage system), until the combined value
  `1 - Π(1 - vᵢ)` reaches the target. With volumes at 99% availability, `availability-3-nines`
  needs two instances.
- **Limits.** `confine-to` keeps every instance in its target and `exclude-from` keeps
  instances out of it. `keep-online` needs an instance on a volume with no online delay;
  performance objectives need one on a volume with enough IOPS.
- **Local copies.** Unless `optimize-for-capacity` applies, a local (storage volume) instance
  is kept alongside object-volume instances. You can turn this off under placement assumptions.
- **Default placement.** Files that no placement objective covers get one instance on the
  default placement (any writable storage volume, or a group or volume you choose).
- **Read-only volumes** are never targets: their data is assumed to move to the volumes the
  objectives choose.
- **Small files on object volumes.** Files smaller than 40 bytes (adjustable) are kept in the
  cluster metadata instead and use no object-volume space.
- **Space per instance.** On storage volumes, the size rounded up to 4 KiB blocks (or
  `SPACE_USED`, if you choose); on object volumes, the size.
- **Spreading.** Within a group, each instance goes to the eligible volume with the most free
  space left.
- `do-not-move` files are counted as staying where they are. Expression-based objectives
  (such as `virus-scan-operation`) and objectives without a placement effect (deny, block,
  versioning, undelete, ...) don't change the calculation.

![Space by volume](screenshots/objectives-space.png)

**Calculate space**, between the placement assumptions and the results, runs the model. The
result shows the space this share's data would need where it's placed, and its share of the
free space there:

- A **volume group** that a place-on objective (or the default placement) targets is one row,
  with free space totalled across its writable volumes. **Show volumes** lists the members.
  Copies kept for other reasons, such as a local copy or an extra availability copy, count
  toward the targeted group their volume is in.
- Volumes outside those groups have a row each. Read-only volumes are listed as not used.

It also shows instance counts, small files kept in metadata, and how many files each
objective applies to.

## Warnings

- **Coverage.** If some files aren't targeted by any placement objective (for example, when
  conditions cover files older than 90 days and newer than 7 days but not those in between),
  the plan lists them and suggests a catch-all condition such as
  `NOT ((MODIFY_AGE>90*DAYS) OR (MODIFY_AGE<7*DAYS))`, which you can add as an objective in
  one click.
- **Unsatisfiable objectives**: a target group with no writable volumes (such as an empty
  `virus-scanners` group), a group missing from the uploaded information, confine-to and
  exclude-from leaving nowhere to go, or not enough separate storage systems for an
  availability or durability target.
- **Capacity**: a volume, or a volume group as a whole, that would need more than its free space.

## Applying the plan

The plan generates one `share-objective-add` command per objective, in the CLI's syntax:
`--name` is the share, `--path` is the folder or file within it (left out for the whole
share), and the condition is the `--applicability` statement (TRUE when left out):

```
share-objective-add --name "home" --objective "place-on-object-volumes" --applicability "MODIFY_AGE>90*DAYS"
share-objective-add --name "home" --objective "availability-3-nines" --path "/home/projects"
share-objective-add --name "home" --objective "keep-online" --path "/home/projects/a.txt" --applicability "FNMATCH('*.log', NAME)?TRUE"
```

Paths are written from the share's root, including the folder the plan models. Copy or
download the commands from the results and review them before running.

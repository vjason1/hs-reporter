"""Convert raw `hs sum` / `hs eval` output into analysis-ready CSV.

    hs -j sum -e 'IS_FILE?SUMS_TABLE{...}' /mnt/share/proj > out.txt
    python3 hs2csv.py out.txt --path /mnt/share/proj \\
        --keys storage_volume --values files,space_used_bytes,largest_files \\
        --files-out largest-files.csv > summary.csv

Inside the container:  docker exec -i hs-reporter python -m app.hs2csv --path /proj < out.txt

--keys / --values name the key parts and VALUE tuple elements in order. Without them, columns
are named key, files, bytes, bytes_2, top10_table... Sizes are always integer bytes. Largest-files
lists go to --files-out (one row per file) and are left out of the summary.
"""
import argparse
import csv
import sys

try:
    from . import hsparse
except ImportError:  # run as a plain script next to hsparse.py
    import os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import hsparse


def _split(s):
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def main(argv=None):
    ap = argparse.ArgumentParser(description="Convert hs sum/eval output to CSV")
    ap.add_argument("input", nargs="?", help="file with hs output (default: stdin)")
    ap.add_argument("--path", default=".", help="path hs was run on; used to build full file paths")
    ap.add_argument("--keys", help="comma-separated names for the key parts, e.g. owner,group")
    ap.add_argument("--values", help="comma-separated names for the VALUE elements, in order")
    ap.add_argument("--files-out", help="write largest-files rows to this CSV")
    a = ap.parse_args(argv)

    text = open(a.input).read() if a.input else sys.stdin.read()
    records = hsparse.parse_lines(text)
    keys = _split(a.keys) or (["key"] if any(r["key"] for r in records) else [])
    values = _split(a.values)
    rows = hsparse.rows_from_text(text, keys, values, {})

    top_cols = [c for c in {k for r in rows for k in r}
                if any(isinstance(r.get(c), list) and r.get(c) and isinstance(r[c][0], dict)
                       and "path" in r[c][0] for r in rows)]
    cols = []
    for r in rows:
        for c in r:
            if c not in cols and c not in top_cols:
                cols.append(c)

    w = csv.writer(sys.stdout, lineterminator="\n")
    w.writerow(cols)
    for r in rows:
        w.writerow(["" if r.get(c) is None else r.get(c) for c in cols])

    if a.files_out:
        with open(a.files_out, "w", newline="") as f:
            fw = csv.writer(f, lineterminator="\n")
            fw.writerow(keys + (["list"] if len(top_cols) > 1 else []) +
                        ["rank", "file_path", "space_used_bytes"])
            for r in rows:
                for tc in top_cols:
                    for rank, item in enumerate(r.get(tc) or [], 1):
                        fw.writerow([r.get(k) for k in keys] + ([tc] if len(top_cols) > 1 else []) +
                                    [rank, hsparse.join_path(a.path, item.get("path")),
                                     item.get("space_used")])


if __name__ == "__main__":
    import signal
    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)  # quiet exit when piped into head
    main()

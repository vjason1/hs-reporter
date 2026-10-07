#!/usr/bin/env python3
"""A stand-in for the hstk `hs` CLI, for tests and local development without a cluster.

It answers `hs -j sum|eval ... -e EXPRESSION PATH` with output shaped like Hammerspace's:
{"SUMS_TABLE": [{"KEY": ..., "VALUE": [[...]]}]} for tables, [[...]] for totals, and
"#EMPTY" for folders named "empty".
"""
import json
import sys


def _field_text(field, path, root):
    """One metadata value in Hammerspace's text form, from the real file."""
    import os
    import time
    st = os.lstat(path)
    f = field.upper()
    if f == "DPATH":
        rel = os.path.relpath(path, root)
        return '"./' + ("" if rel == "." else rel) + '"'
    if f in ("SIZE", "SPACE_USED"):
        return f"{st.st_size} BYTES"
    if f.endswith("_AGE"):
        return f"{max(0.0, time.time() - st.st_mtime):.4f} SECONDS"
    if f.endswith("_TIME"):
        return time.strftime("LOCAL_TIME('%Y-%m-%d %H:%M:%S')", time.gmtime(st.st_mtime))
    if f == "OWNER":
        return "USER('root@localdomain')"
    if f == "OWNER_GROUP":
        return "GROUP('root@localdomain')"
    if f == "TYPE":
        return "ITEM_TYPE('FILE')"
    if f == "ALL_LABELS":
        return "LABELS_TABLE{}"
    if f.startswith("GET_TAG("):
        return '"cold"' if "archive" in path else "#EMPTY"
    if f.startswith(("HAS_", "IS_")) or f in ("DIRECTORY", "ARCHIVE", "DATA_ORIGIN_LOCAL"):
        return "TRUE" if f in ("IS_ONLINE", "IS_LIVE", "IS_AVAILABLE", "HAS_ONLINE_INSTANCE", "DATA_ORIGIN_LOCAL") else "FALSE"
    return "#EMPTY"


def _tuple_fields(exp):
    """'{DPATH,SIZE,GET_TAG("x")}' -> ['DPATH', 'SIZE', 'GET_TAG("x")']"""
    import re
    return re.findall(r'[A-Z_]+\("[^"]*"\)|[A-Z_][A-Z0-9_.]*', exp.strip()[1:-1])


def eval_metadata(args, exp):
    """Answer hs eval {DPATH,...}: -r walks the folder (one line per file); otherwise one value per
    path, with '##### path' headers when several paths are given, as hstk prints them."""
    import os
    fields = _tuple_fields(exp)
    paths = [a for a in args[args.index("-e") + 2:]]
    if "-r" in args:
        if os.environ.get("FAKE_HS_NO_RECURSIVE"):
            print("recursive evaluation unavailable", file=sys.stderr)
            sys.exit(1)
        root = paths[0]
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for name in sorted(files):
                full = os.path.join(dirpath, name)
                print("{" + ", ".join(_field_text(f, full, root) for f in fields) + "}")
        return
    for pth in paths:
        if len(paths) > 1:
            print(f"##### {pth}")
        root = os.path.dirname(pth)
        print("{" + ", ".join(_field_text(f, pth, root) for f in fields) + "}")


def main():
    args = sys.argv[1:]
    exp = args[args.index("-e") + 1]
    path = args[-1]
    if "eval" in args and exp.startswith("{DPATH"):
        eval_metadata(args, exp)
        return
    top = {"TOP10_TABLE": [{"KEY": [["./big.iso", 7278611]]}, {"KEY": [["./a/small.gz", 413700]]}]}

    if "INSTANCES[PARENT.ROW].VOLUME" in exp:
        print(json.dumps({"SUMS_TABLE": [
            {"KEY": [[{"HAMMERSCRIPT": "OWNER('alice|1001')"}, {"HAMMERSCRIPT": "GROUP('eng|500')"},
                      {"HAMMERSCRIPT": "STORAGE_VOLUME('dsx-1::/hsvol0')"}]], "VALUE": [[12, 48000000]]},
            {"KEY": [[{"HAMMERSCRIPT": "OWNER('bob|1002')"}, {"HAMMERSCRIPT": "GROUP('ops|501')"},
                      {"HAMMERSCRIPT": "STORAGE_VOLUME('dsx-2::/hsvol0')"}]], "VALUE": [[3, 1500]]}]}))
    elif "SUMS_TABLE" in exp:
        value = lambda n, b: [[n, b, top]] if "TOP10_TABLE" in exp else [[n, b]]
        print(json.dumps({"SUMS_TABLE": [
            {"KEY": {"HAMMERSCRIPT": "STORAGE_VOLUME('vol-a::/export')"}, "VALUE": value(400, 174390123)},
            {"KEY": {"HAMMERSCRIPT": "STORAGE_VOLUME('vol-b::/hsvol0')"}, "VALUE": value(497, 88569456)}]}))
    elif path.rstrip("/").endswith("empty"):
        print('"#EMPTY"')
    elif "TOP10_TABLE" in exp:
        print(json.dumps([[10, 2048000, top]]))
    else:
        n = len(path)
        print(json.dumps([[n, n * 1000003]]))


if __name__ == "__main__":
    main()

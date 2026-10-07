import json

from app import hsparse

COLS = ["Files", "Space used", "Largest files"]
UNITS = {"Files": "count", "Space used": "bytes", "Largest files": "files"}


def test_tab_separated_text_with_unit_tagged_values():
    key = json.dumps({"HAMMERSCRIPT": "STORAGE_VOLUME('dsx-2.example::/hsvol0')"})
    val = json.dumps([{"FILES": 400}, {"MBYTES": 40.354},
                      {"TOP10_TABLE": [{"KEY": [[{"MBYTES": 15.38}, "./lib.so"]]}]}])
    rows = hsparse.rows_from_text(f"{key}\t{val}\t\t[]\n", ["Storage volume"], COLS, UNITS)
    assert rows == [{"Storage volume": "dsx-2.example::/hsvol0", "Files": 400, "Space used": 40354000,
                     "Largest files": [{"space_used": 15380000, "path": "./lib.so"}]}]


def test_sums_table_json_wrapper():
    obj = {"SUMS_TABLE": [{"KEY": {"HAMMERSCRIPT": "STORAGE_VOLUME('v1')"},
                           "VALUE": [[3, 1200, {"TOP10_TABLE": [{"KEY": [["./x", 900]]}]}]]}]}
    recs = hsparse.records_from_json(obj, "table")
    rows = hsparse.rows_from_records(recs, ["Storage volume"], COLS, UNITS)
    assert rows[0]["Storage volume"] == "v1"
    assert rows[0]["Files"] == 3 and rows[0]["Space used"] == 1200
    assert rows[0]["Largest files"] == [{"space_used": 900, "path": "./x"}]


def test_owner_uid_is_trimmed_and_tuple_keys_split():
    key = [[{"HAMMERSCRIPT": "OWNER('alice|1001')"}, {"HAMMERSCRIPT": "GROUP('eng|500')"}]]
    assert hsparse.key_parts(key) == ["alice", "eng"]


def test_empty_result_reports_zeros():
    rows = hsparse.rows_from_records([{"key": [], "items": hsparse.value_items("#EMPTY"), "extra": []}],
                                     [], COLS, UNITS)
    assert rows == [{"Files": 0, "Space used": 0, "Largest files": []}]


def test_decimal_units():
    assert hsparse.to_bytes("KBYTES", 4.096) == 4096
    assert hsparse.to_bytes("GBYTES", 1.5) == 1_500_000_000


def test_join_path():
    assert hsparse.join_path("/proj", "./a/b.txt") == "/proj/a/b.txt"
    assert hsparse.join_path("/", "./a") == "/a"

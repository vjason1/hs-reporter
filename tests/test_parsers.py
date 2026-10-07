from pathlib import Path

from app import clusterinfo, hsvalue

FIX = Path(__file__).parent / "fixtures"


def test_full_metadata_dump():
    d = hsvalue.parse((FIX / "file-metadata.txt").read_text())
    assert len(d) == 128
    assert d["NAME"] == "file.txt" and d["DPATH"] == "./file.txt"
    assert d["OWNER"] == "root@localdomain" and d["OWNER"].kind == "USER"
    assert d["SIZE"] == 0 and d["IS_FILE"] is True and d["SYMLINK"] is None
    assert d["ACCESS_AGE"] == 33.4566
    assert d["ACTUAL_LAST_USE_AGE"] == (56 * 365 + 292) * 86400
    assert d["INSTANCES"]["VOLUME"] == "jv-t4-dsx-1.asgard.local::/hsvol0"
    assert len(d["SWEEP_DETAILS"]) == 2                      # rows split by ';'
    assert d["ATTRIBUTES"]["RECENTLY_USED_DURATION"] == 300  # 00:05:00


def test_values_and_streams():
    assert hsvalue.parse("1.234 MBYTES") == 1_234_000
    assert hsvalue.parse("100%") == 1.0
    assert hsvalue.parse('{"./a", 4 BYTES, TRUE}') == ["./a", 4, True]
    out = list(hsvalue.parse_stream('##### /s/a\n{"./a", 4 BYTES}\n##### /s/b\n{"./b", 9 BYTES}\n'))
    assert out == [("/s/a", ["./a", 4]), ("/s/b", ["./b", 9])]


def test_exports():
    kind, vols = clusterinfo.parse_any((FIX / "volume-list.txt").read_text())
    assert kind == "volumes" and [v["name"] for v in vols] == ["dsx2-1", "dsx1-1"]
    v = vols[0]
    assert v["node"] == "jv-a-dsx-2.asgard.local" and not v["read_only"] and v["free"] == 42_300_000_000
    assert "jv-a-dsx-2.asgard.local::/hsvol0" in v["aliases"] and round(v["availability"], 4) == 0.99
    kind, ovs = clusterinfo.parse_any((FIX / "object-volume-list.txt").read_text())
    assert kind == "object_volumes" and ovs[0]["total"] is None and ovs[0]["online_delay"] == 300
    kind, groups = clusterinfo.parse_any((FIX / "volume-group-list.txt").read_text())
    assert {g["name"]: g["volumes"] for g in groups}["DSX"] == ["dsx2-1", "dsx1-1"]
    kind, objs = clusterinfo.parse_any((FIX / "objective-list.txt").read_text())
    by = {o["name"]: o for o in objs}
    assert len(objs) == 63
    assert by["place-on-object-volumes"]["place_on"] == [("group", "object-volumes")]
    assert by["confine-to-object-volumes"]["effects"] == ["confine"]
    assert by["keep-online"]["online_delay"] == 0
    assert by["virus-scan-operation"]["effects"] == ["expression"]


def test_read_only_volume_detected():
    text = (FIX / "volume-list.txt").read_text().replace("Access type:             Read Write",
                                                          "Access type:             Read Only", 1)
    _, vols = clusterinfo.parse_any(text)
    assert vols[0]["read_only"] and not vols[1]["read_only"]

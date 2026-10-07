import pytest

from app import reports


def preset(pid):
    return next(p for p in reports.PRESETS if p["id"] == pid)


def test_every_preset_builds():
    for p in reports.PRESETS:
        b = reports.build(p)
        assert b["expression"]


def test_volume_with_owner_and_group():
    b = reports.build(preset("user_vol"))
    assert b["expression"] == ("IS_FILE?SUMS_TABLE{|::KEY={OWNER,OWNER_GROUP,INSTANCES[PARENT.ROW].VOLUME},"
                               "|::VALUE={1FILE/files,SPACE_USED/bytes}}[ROWS(INSTANCES)]")
    assert b["key_columns"] == ["Owner", "Group", "Storage volume"]


def test_filters_chain_as_ternaries():
    b = reports.build({"mode": "sum", "sum": {"group_by": [], "metrics": ["file_count"]},
                       "filters": {"min_size": 1000, "access_age_days": 2}})
    assert b["expression"] == "IS_FILE?(SPACE_USED>=1000)?(ACCESS_AGE>=2DAYS)?1FILE/files"


def test_objective_cannot_combine():
    with pytest.raises(reports.DefinitionError):
        reports.build({"mode": "sum", "sum": {"group_by": ["objective", "owner"], "metrics": ["file_count"]}})


def test_per_folder_settings():
    b = reports.build(preset("dir_walk"))
    assert b["per_folder"] == {"depth": "all", "include_hidden": False}


def test_expression_override_and_unit_scale():
    d = {**preset("owner_usage"),
         "expression_override": "IS_FILE?SUMS_TABLE{|KEY=OWNER,|VALUE={1FILE/files,SPACE_USED/gbytes}}"}
    b = reports.build(d)
    assert b["edited"] and b["byte_scale"] == 10**9
    assert "SPACE_USED/bytes" in b["generated_expression"]


def test_crawl_speed_presets_and_bounds():
    base = preset("folder_usage")
    assert reports.build({**base, "throttle": {"preset": "slowest"}})["throttle"] == \
        {"preset": "slowest", "concurrency": 1, "pause": 5.0, "list_rate": 5}
    custom = reports.build({**base, "throttle": {"preset": "custom", "concurrency": 2, "pause": 0.5, "list_rate": 10}})
    assert custom["throttle"]["concurrency"] == 2 and custom["throttle"]["pause"] == 0.5
    for bad in ({"concurrency": 0}, {"concurrency": 99}, {"pause": -1}, {"list_rate": "fast"}):
        with pytest.raises(reports.DefinitionError):
            reports.build({**base, "throttle": {"preset": "custom", **bad}})


def test_fnmatch_and_ternary():
    from app import objexpr as ox
    item = {"NAME": "run.log", "PATH": "./home/scratch/run.log", "MODIFY_AGE": 100 * 86400}
    expect = {
        "FNMATCH('*.log', NAME)?TRUE": True,
        "!FNMATCH('*.log', NAME)?TRUE": False,
        "FNMATCH('*/scratch/*', PATH)?TRUE": True,
        "!FNMATCH('*/scratch/*', PATH)?TRUE": False,
        "FNMATCH('*.LOG', NAME)?TRUE": False,                      # case-sensitive
        "FNMATCH('run.???', NAME)?TRUE": True,
        "FNMATCH('*/scratch/*', PATH)?TRUE AND MODIFY_AGE>90*DAYS": True,
        "MODIFY_AGE>90*DAYS AND (FNMATCH('*.tmp', NAME)?TRUE)": False,
        "MODIFY_AGE<1*DAYS ? TRUE : FALSE": False,
    }
    for text, want in expect.items():
        assert ox.Condition(text).evaluate(item, 0) is want or bool(ox.Condition(text).evaluate(item, 0)) == want, text
    assert "quoted pattern" in ox.check("FNMATCH(NAME, '*.log')?TRUE")["error"]
    assert "NAME or PATH" in ox.check("FNMATCH('*.log', SIZE)?TRUE")["error"]

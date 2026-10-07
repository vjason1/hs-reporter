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


def test_crawl_speed_is_global(tmp_path):
    from app import settings
    try:
        settings.update({"crawl_preset": "slowest"})
        assert settings.crawl() == {"preset": "slowest", "concurrency": 1, "pause": 5.0, "list_rate": 5.0}
        assert reports.build(preset("folder_usage"))["throttle"]["preset"] == "slowest"   # every report
        settings.update({"crawl_preset": "custom", "crawl_concurrency": 2, "crawl_pause": 0.5, "crawl_list_rate": 10})
        assert settings.crawl() == {"preset": "custom", "concurrency": 2, "pause": 0.5, "list_rate": 10.0}
        for bad in ({"crawl_concurrency": 0}, {"crawl_concurrency": 99}, {"crawl_pause": -1},
                    {"crawl_list_rate": "fast"}, {"crawl_preset": "turbo"}, {"nfs_options": "vers=3, nolock"}):
            with pytest.raises(settings.SettingsError):
                settings.update(bad)
    finally:
        settings.reset()
    assert settings.crawl()["preset"] == "normal"


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


def test_limiter_follows_settings_without_restart():
    import asyncio
    import time
    from app import settings

    async def scenario():
        lim = settings.Limiter("max_hs_processes")
        settings.update({"max_hs_processes": 1})
        await lim.__aenter__()                      # first slot taken
        waiter = asyncio.create_task(lim.__aenter__())
        await asyncio.sleep(0.3)
        assert not waiter.done()                    # limit 1: the second waits
        t0 = time.time()
        settings.update({"max_hs_processes": 2})    # raised on the Settings page
        await asyncio.wait_for(waiter, 3)
        assert time.time() - t0 < 2.5 and lim.active == 2

    try:
        asyncio.run(scenario())
    finally:
        settings.reset()


def test_settings_store_only_changes(monkeypatch):
    from app import settings
    try:
        settings.update({"max_folders": 2000, "run_timeout": 600})   # 2000 is the default
        import json
        assert json.loads(settings.PATH.read_text()) == {"run_timeout": 600}
        monkeypatch.setenv("HSR_MAX_FOLDERS", "500")                  # env still sets unchanged values
        assert settings.get()["max_folders"] == 500 and settings.get()["run_timeout"] == 600
        msg = ""
        try:
            settings.update({"max_hs_processes": 99})
        except settings.SettingsError as e:
            msg = str(e)
        assert msg == "hs commands at once, across everything must be between 1 and 32"
    finally:
        settings.reset()

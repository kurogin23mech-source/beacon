"""ms-108 e-5217 — the cloud-config path has ONE namespace-free patch seam.

``_get_cloud_config_path`` is from-imported into several module globals
(``commands`` / ``cmd_trigger`` / ``cmd_claim`` / ``cmd_note`` / ``cmd_project``).
Monkeypatching one module's copy silently misses the others — the op-1 leak's真因
(a test faked ``commands._get_cloud_config_path`` but the caller had moved to
``cmd_trigger``). The fix is not a rule ("remember to patch every namespace") but a
structural one: fake the cloud config via ``BEACON_PROJECT_FILE`` (the ``fake_cloud_config``
conftest fixture), which every copy resolves through — so no namespace knowledge is needed.
"""
from __future__ import annotations


def test_fake_cloud_config_resolves_in_every_namespace(fake_cloud_config):
    # The canonical fixture sets BEACON_PROJECT_FILE; every from-imported copy of
    # _get_cloud_config_path derives the SAME cloud.json from get_project_file(),
    # with no per-module monkeypatch.
    import commands
    import cmd_trigger
    import commands_shared
    expected = str(fake_cloud_config / "cloud.json")
    assert commands._get_cloud_config_path() == expected
    assert cmd_trigger._get_cloud_config_path() == expected
    assert commands_shared._get_cloud_config_path() == expected


def test_seam_is_the_env_var_not_the_symbol(fake_cloud_config, monkeypatch):
    # Prove the seam is BEACON_PROJECT_FILE, not the function symbol: repoint the env
    # var and every namespace follows, still with no symbol patch.
    import commands
    import cmd_trigger
    other = fake_cloud_config.parent / "elsewhere" / ".beacon"
    other.mkdir(parents=True)
    (other / "project.json").write_text("{}")
    monkeypatch.setenv("BEACON_PROJECT_FILE", str(other / "project.json"))
    expected = str(other / "cloud.json")
    assert commands._get_cloud_config_path() == expected
    assert cmd_trigger._get_cloud_config_path() == expected

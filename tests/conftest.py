import pytest


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch, tmp_path):
    """Tests must not depend on the developer's shell (TPCTL_NO_TRACK, TPCTL_FORMAT, TPLINK_HOST ...) nor read or
    write their real ~/.config/tplink-router (saved host, aliases, password file)."""
    import os
    for k in list(os.environ):
        if k.startswith(("TPCTL_", "TPLINK_")):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("TPLINK_CONFIG", str(tmp_path / "config"))

import pytest


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch):
    """Tests must not depend on the developer's shell (TPCTL_NO_TRACK, TPCTL_FORMAT, TPLINK_HOST ...)."""
    import os
    for k in list(os.environ):
        if k.startswith(("TPCTL_", "TPLINK_")):
            monkeypatch.delenv(k, raising=False)

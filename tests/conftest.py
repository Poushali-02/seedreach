"""Keep tests independent of whatever is in a contributor's .env or shell.

Settings() normally reads the project's .env and environment variables. Here it reads neither, so every
test sees the defaults (plus what the test sets itself): a real API key can't reach Apollo or anything
else, and a personal setting such as PREFER_REGIONS can't change what a test expects.
"""

import pytest

from src.config import Settings


@pytest.fixture(autouse=True)
def _default_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.setenv("APOLLO_API_KEY", "")  # belt and braces: never a real key

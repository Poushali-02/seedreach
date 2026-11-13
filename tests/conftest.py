"""Keep tests off the network whatever is in a contributor's .env.

Settings() reads the project's .env, and environment variables override it, so blanking the API keys
here means no test can call Apollo (or anything else) with a real key by accident.
"""

import pytest


@pytest.fixture(autouse=True)
def _no_real_api_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APOLLO_API_KEY", "")

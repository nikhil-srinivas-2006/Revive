from dataclasses import replace

import pytest

from revive.config import Config
from revive.engine import Engine


@pytest.fixture
def config(tmp_path):
    source = tmp_path / "input"
    source.mkdir()
    return Config(
        input=source,
        output=tmp_path / "output",
        app_data=tmp_path / "app",
        quiet_seconds=0.01,
        sample_seconds=0.005,
        stable_samples=2,
        reconcile_seconds=0.02,
        min_free_disk_mib=1,
    )


@pytest.fixture
def engine(config):
    instance = Engine(config)
    yield instance
    instance.close()


@pytest.fixture
def change_config():
    return replace

"""`config init` must emit a config its own validator accepts, for every built-in profile.

Found live on 2026-11-08: on a VENTUNO Q (no GPU clock bands) the template wrote
`gpu_clock_bands = []`, which `config check` rejects. The generic profile had the same bug.
"""

import tomllib

import pytest

from heat_governor import config, platforms
from tests.fakes import VENTUNOQ_ZONES, make_sys

PROFILES = sorted(platforms.BUILTIN)


@pytest.mark.parametrize("name", PROFILES)
def test_template_validates_for_builtin_profile(name):
    text = config.template(platforms.BUILTIN[name])
    assert config.validate(tomllib.loads(text)) == []


def test_template_validates_for_generic_profile(tmp_path):
    make_sys(tmp_path, VENTUNOQ_ZONES)
    prof = platforms.generic_profile(tmp_path / "sys")
    assert not prof.gpu_clock_bands
    text = config.template(prof)
    assert config.validate(tomllib.loads(text)) == []

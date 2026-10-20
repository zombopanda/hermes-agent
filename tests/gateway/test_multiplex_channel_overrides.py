"""A turn routed to a secondary multiplex profile resolves that profile's own ``channel_overrides``.

Under ``gateway.multiplex_profiles`` the runner's ``GatewayConfig`` is the default profile's. A
private DM's chat id is the user's id on every bot, so a default-profile override for the user's
DM also rerouted the same user's DM with a secondary profile's bot, and the secondary's own
``model`` never ran.
"""

import pytest

from gateway.config import ChannelOverride, GatewayConfig, Platform, PlatformConfig
from gateway.run import _get_channel_override
from hermes_constants import reset_hermes_home_override, set_hermes_home_override

DM = "123456789"
DEFAULT_OVERRIDE = ChannelOverride(model="default-model", provider="default-provider")


def _config(*, multiplex: bool = True) -> GatewayConfig:
    telegram = PlatformConfig(enabled=True, channel_overrides={DM: DEFAULT_OVERRIDE})
    return GatewayConfig(platforms={Platform.TELEGRAM: telegram}, multiplex_profiles=multiplex)


@pytest.fixture
def homes(tmp_path, monkeypatch):
    default_home = tmp_path / "hermes"
    secondary_home = default_home / "profiles" / "secondary"
    secondary_home.mkdir(parents=True)
    (default_home / "config.yaml").write_text(
        f"platforms:\n  telegram:\n    channel_overrides:\n      '{DM}':\n"
        "        model: default-model\n        provider: default-provider\n")
    (secondary_home / "config.yaml").write_text("model:\n  default: secondary-model\n")
    monkeypatch.setenv("HERMES_HOME", str(default_home))
    return default_home, secondary_home


@pytest.fixture
def route():
    tokens = []
    yield lambda home: tokens.append(set_hermes_home_override(home))
    for token in reversed(tokens):
        reset_hermes_home_override(token)


def _lookup(config: GatewayConfig):
    return _get_channel_override(config, Platform.TELEGRAM, DM)


def test_secondary_profile_does_not_inherit_default_dm_override(homes, route):
    route(homes[1])
    assert _lookup(_config()) is None


def test_secondary_profile_uses_its_own_override(homes, route):
    secondary = homes[1]
    (secondary / "config.yaml").write_text(
        f"telegram:\n  channel_overrides:\n    '{DM}':\n      model: secondary-override\n")
    route(secondary)
    assert _lookup(_config()) == ChannelOverride(model="secondary-override")


def test_default_profile_keeps_its_override(homes, route):
    assert _lookup(_config()) == DEFAULT_OVERRIDE
    route(homes[0])
    assert _lookup(_config()) == DEFAULT_OVERRIDE


def test_single_profile_gateway_is_unchanged(homes, route):
    route(homes[1])
    assert _lookup(_config(multiplex=False)) == DEFAULT_OVERRIDE

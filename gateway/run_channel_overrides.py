"""``channel_overrides`` lookup, scoped to the profile a turn is routed to."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from gateway.config import ChannelOverride, GatewayConfig, Platform
from hermes_constants import get_hermes_home_override, get_routing_process_hermes_home


def _channel_override_lookup_keys(
    chat_id: str, *, thread_id: Optional[str] = None, parent_id: Optional[str] = None) -> list[str]:
    """Ordered, de-duplicated ``channel_overrides`` lookup keys (matches ``resolve_channel_prompt``:
    exact id first, then parent — Discord threads inherit parent overrides)."""
    return list(dict.fromkeys(str(key) for key in (chat_id, thread_id, parent_id) if key))


def _routed_profile_channel_overrides(
    config: GatewayConfig, platform: Platform) -> Optional[dict[str, ChannelOverride]]:
    """A routed secondary profile's own ``channel_overrides``; None outside one.

    Under multiplex ``config`` is the DEFAULT profile's GatewayConfig, and a private DM's chat id is the
    user's id on every bot, so reading it would hand a secondary bot's DM the default's override."""
    if not getattr(config, "multiplex_profiles", False):
        return None
    home = get_hermes_home_override()
    if not home or Path(home).resolve() == get_routing_process_hermes_home().resolve():
        return None
    from gateway.config_loader import platform_section
    from gateway.run import _load_gateway_config
    yaml_cfg = _load_gateway_config()
    gateway_section = yaml_cfg.get("gateway")
    section, _ = platform_section(
        yaml_cfg, platform.value, gateway_section.get("platforms") if isinstance(gateway_section, dict) else None)
    raw = section.get("channel_overrides") if isinstance(section, dict) else None
    if not isinstance(raw, dict):
        return {}
    return {str(cid): ChannelOverride.from_dict(ov) for cid, ov in raw.items() if isinstance(ov, dict)}


def _get_channel_override(
    config: GatewayConfig, platform: Platform, chat_id: str, *, thread_id: Optional[str] = None,
    parent_id: Optional[str] = None) -> Optional[ChannelOverride]:
    """Per-channel override via chat_id, then thread_id, then parent_id; None if absent.

    A turn routed to a secondary multiplex profile reads that profile's own overrides."""
    overrides = _routed_profile_channel_overrides(config, platform)
    if overrides is None:
        platforms = getattr(config, "platforms", None)
        if not platforms:
            return None
        platform_config = platforms.get(platform)
        if not platform_config or not platform_config.channel_overrides:
            return None
        overrides = platform_config.channel_overrides
    for key in _channel_override_lookup_keys(chat_id, thread_id=thread_id, parent_id=parent_id):
        ov = overrides.get(key)
        if ov is not None:
            return ov
    return None

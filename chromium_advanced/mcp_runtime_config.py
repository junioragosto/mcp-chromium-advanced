import os
import secrets
from typing import Dict


def resolve_mcp_headless(config: Dict) -> bool:
    mcp = config.get("mcp", {}) if isinstance(config, dict) else {}
    if isinstance(mcp, dict) and "headless" in mcp:
        return bool(mcp.get("headless"))
    env_value = str(os.environ.get("CHROMIUM_ADVANCED_MCP_HEADLESS", "") or "").strip().lower()
    return env_value in {"1", "true", "yes", "on"}


def resolve_mcp_start_minimized(config: Dict) -> bool:
    if resolve_mcp_headless(config):
        return False
    mcp = config.get("mcp", {}) if isinstance(config, dict) else {}
    if isinstance(mcp, dict) and "start_minimized" in mcp:
        return bool(mcp.get("start_minimized"))
    env_value = str(os.environ.get("CHROMIUM_ADVANCED_MCP_START_MINIMIZED", "") or "").strip().lower()
    if env_value:
        return env_value in {"1", "true", "yes", "on"}
    return False


def resolve_mcp_api_token(config: Dict) -> str:
    """Return the configured API token, generating one when persistence needs to seed it."""
    mcp = config.get("mcp", {}) if isinstance(config, dict) else {}
    token = str(mcp.get("api_token", "")).strip() if isinstance(mcp, dict) else ""
    if not token:
        token = secrets.token_hex(24)
    return token


def resolve_control_api_token(config: Dict) -> str:
    """Return the configured control API token, generating one when persistence needs to seed it."""
    control = config.get("control", {}) if isinstance(config, dict) else {}
    token = str(control.get("api_token", "")).strip() if isinstance(control, dict) else ""
    if not token:
        token = secrets.token_hex(24)
    return token


def is_mcp_localhost_listening(config: Dict) -> bool:
    """Return True when the MCP host binding is local-only."""
    mcp = config.get("mcp", {}) if isinstance(config, dict) else {}
    host = str(mcp.get("host", "127.0.0.1")).strip().lower() if isinstance(mcp, dict) else "127.0.0.1"
    return host in {"127.0.0.1", "::1", "localhost"} or host.startswith("127.")


def mcp_auth_required(config: Dict) -> bool:
    """Return True when API token authentication is required."""
    mcp = config.get("mcp", {}) if isinstance(config, dict) else {}
    if not isinstance(mcp, dict):
        return False
    if not bool(mcp.get("enabled", False)):
        return False
    return bool(str(mcp.get("api_token", "")).strip())


def control_auth_required(config: Dict) -> bool:
    """Return True when control API token authentication is required."""
    control = config.get("control", {}) if isinstance(config, dict) else {}
    if not isinstance(control, dict):
        return False
    if not bool(control.get("enabled", True)):
        return False
    return bool(str(control.get("api_token", "")).strip())

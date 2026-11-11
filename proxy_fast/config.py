"""Load the user-editable INI configuration without third-party dependencies."""
from __future__ import annotations

import configparser
import os
from pathlib import Path

from .cloud import ProxyError


# INI names -> CLI/Settings names and accepted value types.
FIELDS = {
    "cloud": {"region": ("region", str), "profile": ("profile", str)},
    "instance": {"price_limit": ("price_limit", str), "zone": ("zone", str),
                 "instance_type": ("instance_type", str), "image_id": ("image_id", str),
                 "protection_hours": ("protection", int)},
    "disk": {"size_gib": ("disk_size", int), "category": ("disk_category", str)},
    "network": {"bandwidth_mbps": ("bandwidth", int)},
    "controller": {"timeout_seconds": ("timeout", int), "state_dir": ("state_dir", Path)},
}


def load_config(path=None, *, env=None, cwd=None, repository=None):
    env = os.environ if env is None else env
    cwd = Path.cwd() if cwd is None else Path(cwd)
    explicit = path if path is not None else env.get("PROXY_FAST_CONFIG")
    if explicit is not None:
        selected = Path(explicit).expanduser()
        if not selected.is_absolute(): selected = cwd / selected
        if not selected.is_file():
            raise ProxyError(f"Configuration file not found: {selected}")
    else:
        repository = Path(repository) if repository else Path(__file__).resolve().parent.parent
        candidates = [cwd / "proxy-fast.ini"]
        # Editable/source installs can find the repository's config from any working directory.
        if (repository / "pyproject.toml").is_file():
            candidates.append(repository / "proxy-fast.ini")
        selected = next((p for p in candidates if p.is_file()), None)
    if selected is None:
        return {}, None
    selected = selected.resolve()
    parser = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=("#", ";"))
    try:
        parser.read_string(selected.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, configparser.Error) as exc:
        raise ProxyError(f"Cannot read INI configuration: {selected} ({type(exc).__name__}).") from exc
    if parser.defaults():
        raise ProxyError("Configuration [DEFAULT] is unsupported; use the named sections in proxy-fast.ini.")
    values = {}
    for section in parser.sections():
        if section not in FIELDS:
            raise ProxyError(f"Unknown configuration section [{section}] in {selected}.")
        for key, text in parser.items(section):
            if key not in FIELDS[section]:
                raise ProxyError(f"Unknown configuration option [{section}] {key} in {selected}.")
            name, convert = FIELDS[section][key]
            text = text.strip()
            if not text:  # Empty optional values use automatic selection/defaults.
                continue
            try:
                value = convert(text)
            except ValueError as exc:
                raise ProxyError(f"Configuration [{section}] {key} must be an integer.") from exc
            if name == "state_dir":
                value = value.expanduser()
                if not value.is_absolute(): value = selected.parent / value
                value = value.resolve()
            values[name] = value
    return values, selected


def apply_config(arguments):
    """Priority: explicit CLI > legacy environment overrides > INI > built-in defaults."""
    values, path = load_config(getattr(arguments, "config", None))
    for key, variable in (("region", "PROXY_FAST_REGION"), ("price_limit", "PROXY_FAST_PRICE_LIMIT")):
        if os.environ.get(variable): values[key] = os.environ[variable]
    for key, value in values.items():
        if getattr(arguments, key, None) is None:
            setattr(arguments, key, value)
    if arguments.timeout is None: arguments.timeout = 600
    if arguments.action == "on":
        for name, default in (("price_limit", "0.10"), ("bandwidth", 100), ("protection", 1),
                              ("disk_size", 20), ("disk_category", "auto")):
            if getattr(arguments, name, None) is None: setattr(arguments, name, default)
    return path

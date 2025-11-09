from __future__ import annotations

import os

try:  # optional .env loader
    from dotenv import load_dotenv  # type: ignore
except Exception:  # pragma: no cover
    load_dotenv = None

if load_dotenv is not None:  # pragma: no branch - best effort
    load_dotenv()


def env_flag(name: str, default: bool = False) -> bool:
    """Parse truthy/falsey strings from environment variables."""

    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "t", "yes", "y", "on"}:
        return True
    if value in {"0", "false", "f", "no", "n", "off"}:
        return False
    return default


HEADLESS_DEFAULT = env_flag("PULSE_PLAYWRIGHT_HEADLESS", False)


__all__ = ["env_flag", "HEADLESS_DEFAULT"]

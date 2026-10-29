"""Optional environment configuration for the EP API explorer."""

import math
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    base_url: str = "https://data.europarl.europa.eu/api/v2"
    output_dir: str = "ep_api_test_v2_output"
    timeout: float = 30.0
    request_delay: float = 0.5
    max_procedures: int = 50
    lookback_months: int = 3
    meetings_horizon: int = 50
    max_meetings: int = 20


def load_settings() -> Settings:
    """Existing environment variables override the .env beside this module."""
    load_dotenv(Path(__file__).with_name(".env"), override=False)
    values = {}
    for name, default in vars(Settings()).items():
        key = f"EP_{name.upper()}"
        raw = os.environ.get(key)
        if raw is None:
            continue
        raw = raw.strip()
        if not raw:
            raise ValueError(f"{key} must not be empty")
        if isinstance(default, (int, float)):
            try:
                number = type(default)(raw)
            except ValueError:
                raise ValueError(f"{key} must be a number") from None
            if not math.isfinite(number) or number < 0:
                raise ValueError(f"{key} must be finite and non-negative")
            if name != "request_delay" and number == 0:
                raise ValueError(f"{key} must be greater than zero")
            values[name] = number
        else:
            values[name] = raw
    settings = Settings(**values)
    parsed = urlparse(settings.base_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("EP_BASE_URL must be an absolute HTTP(S) API URL")
    return settings

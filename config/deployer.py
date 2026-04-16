"""Load deployed per-symbol whale swing configs from config/deployed/*.json."""
import json
import os
from typing import Optional

from utils.logger import setup_logger

log = setup_logger("deployer")

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEPLOYED_DIR = os.path.join(_BASE_DIR, "config", "deployed")


def load_deployed(symbol: str) -> Optional[dict]:
    """Return the deployed config dict for a symbol (or None)."""
    safe = symbol.replace(":", "_")
    path = os.path.join(DEPLOYED_DIR, f"whale_{safe}.json")
    if not os.path.exists(path):
        log.warning(f"no deployed config for {symbol}: {path}")
        return None
    with open(path) as f:
        return json.load(f)


def load_all() -> dict[str, dict]:
    """Return {symbol: config_dict} for every deployed file in config/deployed/."""
    out: dict[str, dict] = {}
    if not os.path.isdir(DEPLOYED_DIR):
        return out
    for fn in os.listdir(DEPLOYED_DIR):
        if not fn.startswith("whale_") or not fn.endswith(".json"):
            continue
        with open(os.path.join(DEPLOYED_DIR, fn)) as f:
            cfg = json.load(f)
        out[cfg["symbol"]] = cfg
    return out

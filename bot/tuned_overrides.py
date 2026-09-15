"""
Applies optimizer-recommended parameter overrides on top of config.py's
hardcoded dataclass defaults, without ever touching config.py's source.

Why a separate override file instead of patching config.py directly: several
of the tunable fields (e.g. AccountConfig.min_reward_risk_ratio) are
multi-line dict literals built via field(default_factory=...) -- regex-
patching that kind of structure is fragile, and a bad substitution produces
a SyntaxError at container startup (crash-looping the single-replica
production Deployment). A malformed or missing override file, by contrast,
degrades gracefully to "use the coded defaults" -- see apply_overrides()
below, which never raises.

The override file (config/tuned_overrides.json, tracked in git) is written
by optimizer.py, reviewed as a PR like any other change, and applied here
at import time -- see the bottom of config.py.
"""
from __future__ import annotations
import json
import logging
from pathlib import Path
from typing import Any

# Plain stdlib logging, not bot.logger_setup: this module is imported from
# the bottom of config.py itself, and logger_setup.get_logger() imports
# LOG_DIR from bot.config -- a genuine circular import if this module (also
# imported from within config.py's own execution) depended on it too.
log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_OVERRIDES_FILE = BASE_DIR / "config" / "tuned_overrides.json"


def _deep_merge(target: dict, updates: dict) -> None:
    """Merges `updates` into `target` in place. Dict values are merged
    key-by-key (so overriding one entry of AccountConfig.min_reward_risk_ratio
    doesn't drop the others); non-dict values are replaced outright."""
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_merge(target[key], value)
        else:
            target[key] = value


def _apply_to_object(obj: Any, obj_name: str, updates: dict) -> None:
    for field_name, value in updates.items():
        if not hasattr(obj, field_name):
            log.warning("tuned_overrides: unknown field %s.%s, ignoring.", obj_name, field_name)
            continue
        current = getattr(obj, field_name)
        if isinstance(value, dict) and isinstance(current, dict):
            merged = dict(current)
            _deep_merge(merged, value)
            setattr(obj, field_name, merged)
        else:
            setattr(obj, field_name, value)


def apply_overrides(targets: dict, overrides_file: Path | None = None) -> None:
    """
    targets: {"ACCOUNT": ACCOUNT, "REGIME": REGIME} -- the already-constructed
    config singletons to mutate in place.

    Never raises: a missing file is a silent no-op (matches today's
    behavior with no override file at all), and a malformed file or an
    unrecognized key is logged and skipped rather than crashing the import.
    """
    path = overrides_file or DEFAULT_OVERRIDES_FILE
    if not path.exists():
        return

    try:
        raw = json.loads(path.read_text())
    except Exception as e:
        log.error("tuned_overrides: could not parse %s, using coded defaults: %s", path, e)
        return

    for obj_name, updates in raw.items():
        if obj_name == "_meta":
            continue  # optimizer.py's own rationale/changelog, not applied
        obj = targets.get(obj_name)
        if obj is None:
            log.warning("tuned_overrides: unknown config object %s, ignoring.", obj_name)
            continue
        if not isinstance(updates, dict):
            log.warning("tuned_overrides: expected an object for %s, got %r, ignoring.", obj_name, updates)
            continue
        _apply_to_object(obj, obj_name, updates)

    log.info("tuned_overrides: applied overrides from %s.", path)

"""deebot_mower_fix – Home Assistant custom component.

Patches the installed ``deebot_client`` library so GOAT lawnmower devices
use the correct MQTT topic (``clean``, not ``clean_V2``) for all clean
commands.  Without this patch the mower logs:

    No response received for command "clean_V2"

and never starts, pauses, or stops.

Installation
------------
1. Copy the ``custom_components/deebot_mower_fix/`` directory into your HA
   ``config/custom_components/`` folder.
2. Add ``deebot_mower_fix:`` to ``configuration.yaml``.
3. Restart Home Assistant.

The patch is applied once at startup and is safe to leave in place even
after ``deebot_client`` is updated — if the installed version already
contains ``CleanMower`` the component logs a message and does nothing.

References
----------
- https://github.com/DeebotUniverse/client.py/issues/1467
- https://github.com/DeebotUniverse/client.py/issues/852
- https://github.com/home-assistant/core/issues/170261
"""

from __future__ import annotations

import importlib
import logging
import random
import string
import time
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from deebot_client.event_bus import EventBus

_LOGGER = logging.getLogger(__name__)
DOMAIN = "deebot_mower_fix"

# HA-configured timezone (IANA name, e.g. "America/New_York").
# Set from hass.config.time_zone in async_setup before any commands are issued.
# The firmware uses tzc to evaluate protection-window times in local time.
# Without it, the firmware defaults to UTC, which blocks mowing during US evenings
# when the UTC time falls inside the configured animal-protection window.
_HA_TIMEZONE: str = "UTC"

# ---------------------------------------------------------------------------
# GOAT mower device classes.
#
# All families use the legacy ``clean`` topic with the app-compatible header
# (ver=0.0.22) and V2 nested payload:
#   START  → {"act": "start",  "content": {"type": "auto"}}
#   PAUSE  → {"act": "pause",  "content": {"type": "auto"}}
#   STOP   → {"act": "stop",   "content": {"type": "auto"}}
#   RESUME → {"act": "resume", "content": {"type": "auto"}}
#
# Root cause of code 20003 "unknow type": the library sends ver="0.0.50"
# but the firmware parser keyed on ver="0.0.22" (what the official app sends).
# ---------------------------------------------------------------------------

_GOAT_CLASSES: tuple[str, ...] = (
    # 5xu9h3 family (O1000 LiDAR PRO and close relatives)
    "5xu9h3", "0jbd6s", "2ap5uq", "2i0fns", "6n9pcz", "77atlz",
    "9bts2s", "aadham", "ao7fpq", "bfvvk", "qhq6i0", "s69g6z", "wwswjm",
    # A1600 RTK / other confirmed GOAT families
    "xmp9ds", "300lc5", "6cibhb", "51rcxt", "2px96q",
)


# ---------------------------------------------------------------------------
# Core patch logic (synchronous – called via executor)
# ---------------------------------------------------------------------------

def _apply_patch() -> None:
    """Inject CleanMower and patch all known GOAT hardware modules."""

    # ── Step 1: ensure CleanMower exists in the installed deebot_client ─────
    try:
        clean_mod = importlib.import_module("deebot_client.commands.json.clean")
    except ImportError:
        _LOGGER.error("deebot_client is not installed; patch cannot be applied")
        return

    if hasattr(clean_mod, "CleanMower"):
        _LOGGER.info(
            "deebot_mower_fix: CleanMower already present in installed "
            "deebot_client — no injection needed"
        )
    else:
        # Older install: define CleanMower as a Clean subclass with V2 args
        _LOGGER.warning(
            "deebot_mower_fix: CleanMower missing from installed deebot_client; "
            "injecting compatibility class"
        )
        _base_clean = clean_mod.Clean

        class CleanMower(_base_clean):  # type: ignore[misc,valid-type]
            """Clean command for mower devices (legacy topic, V2 content payload)."""

            _v2_args: ClassVar[bool] = True

        clean_mod.CleanMower = CleanMower  # type: ignore[attr-defined]

        # Also expose from the json commands package if present
        try:
            json_pkg = importlib.import_module("deebot_client.commands.json")
            if not hasattr(json_pkg, "CleanMower"):
                json_pkg.CleanMower = CleanMower  # type: ignore[attr-defined]
        except ImportError:
            pass

    # Root-cause fix: firmware uses ver field to select parser.
    # ver="0.0.50" (library default) → old parser → rejects type field → 20003.
    # ver="0.0.22" (official app)    → current parser → accepts normally.
    # All GOAT classes get _CleanMowerNG: V2 nested payload + app-compatible header.
    #
    # IMPORTANT: base on CleanMower (not Clean) so we inherit its _get_args which
    # produces V2 nested format.  Clean._get_args ignores _v2_args and always
    # produces flat V1, which is what was sent in the previous broken attempt.
    _base_for_ng = clean_mod.CleanMower  # guaranteed by Step 1

    class _CleanMowerNG(_base_for_ng):  # type: ignore[misc,valid-type]
        """Clean command for GOAT mowers with app-compatible protocol header.

        Overrides _get_payload to send the same header the official Ecovacs
        Android app uses (ver=0.0.22, pri=2 int, ts=ms-string, channel, m,
        reqid).  Without this the firmware parser (keyed on ver) rejects the
        command with code 20003 "unknow type" regardless of the body payload.

        Also overrides _get_args to guarantee V2 nested payload regardless of
        the base CleanMower implementation (the stub we inject in Step 1 does
        not override _get_args, so it would inherit the flat V1 format from
        Clean._get_args).

        Uses V2 nested payload:
          START  → {"act": "start",  "content": {"type": "auto"}}
          PAUSE  → {"act": "pause",  "content": {"type": "auto"}}
          STOP   → {"act": "stop",   "content": {"type": "auto"}}
          RESUME → {"act": "resume", "content": {"type": "auto"}}
        """

        _v2_args: ClassVar[bool] = True

        def _get_args(self, action: Any) -> dict[str, Any]:
            # Explicit V2 nested payload — do NOT rely on base _get_args since
            # Clean._get_args produces flat V1 and our injected CleanMower stub
            # also inherits that behaviour.
            return {"act": action.value, "content": {"type": "auto"}}

        def _get_payload(self) -> dict[str, Any] | list[Any]:
            import datetime as _dt
            reqid = "".join(random.choices(string.ascii_letters + string.digits, k=6))
            ts_ms = str(int(time.time() * 1000))
            # Calculate current UTC offset in minutes dynamically (handles DST).
            utc_offset = _dt.datetime.now().astimezone().utcoffset()
            tzm = int((utc_offset.total_seconds() if utc_offset else 0) / 60)
            # Body MUST come before header — the mower firmware JSON parser
            # is position-sensitive and expects this exact ordering.
            # tzc is required: without it the firmware evaluates protection
            # windows in UTC, blocking mowing when UTC falls inside the window.
            payload: dict[str, Any] = {}
            if self._args:
                payload["body"] = {"data": self._args}
            payload["header"] = {
                "channel": "Android",
                "m": "request",
                "pri": 2,
                "reqid": reqid,
                "ts": ts_ms,
                "tzc": _HA_TIMEZONE,
                "tzm": tzm,
                "ver": "0.0.22",
            }
            return payload

    get_clean_info = getattr(clean_mod, "GetCleanInfo", None)

    # ── Step 2: patch each hardware module and warm the device cache ─────────
    try:
        hw_mod = importlib.import_module("deebot_client.hardware")
    except ImportError:
        _LOGGER.error("deebot_client.hardware not found; skipping hardware patch")
        return

    devices_cache: dict[str, Any] = hw_mod._DEVICES  # type: ignore[attr-defined]  # noqa: SLF001
    not_found_cache: set[str] = hw_mod._NOT_FOUND  # type: ignore[attr-defined]  # noqa: SLF001

    patched = 0
    already_fixed = 0
    missing_classes: list[str] = []  # classes with no installed hardware module

    for class_ in _GOAT_CLASSES:
        not_found_cache.discard(class_)

        try:
            mod = importlib.import_module(f"deebot_client.hardware.{class_}")
        except ModuleNotFoundError:
            _LOGGER.debug("No hardware module for class %s; will alias later", class_)
            missing_classes.append(class_)
            continue

        changed = False

        # Replace CleanV2 → _CleanMowerNG (older hardware files using CleanV2).
        if getattr(mod, "CleanV2", None) is not None:
            mod.CleanV2 = _CleanMowerNG  # type: ignore[attr-defined]
            changed = True

        # Replace CleanMower → _CleanMowerNG (newer hardware files).
        if getattr(mod, "CleanMower", None) is not None:
            mod.CleanMower = _CleanMowerNG  # type: ignore[attr-defined]
            changed = True

        # Replace GetCleanInfoV2 → GetCleanInfo so state polling works.
        if getattr(mod, "GetCleanInfoV2", None) is not None and get_clean_info:
            mod.GetCleanInfoV2 = get_clean_info  # type: ignore[attr-defined]
            changed = True

        if changed:
            devices_cache.pop(class_, None)
            devices_cache[class_] = mod.get_device_info()
            patched += 1
            _LOGGER.warning(
                "deebot_mower_fix: patched %s with app-header V2 payload",
                class_,
            )
        else:
            _LOGGER.warning(
                "deebot_mower_fix: skipped %s — no CleanV2 or CleanMower found in module",
                class_,
            )
            already_fixed += 1

    # ── Step 3: alias classes that had no hardware module to 5xu9h3 ────────────
    # Some installed deebot_client versions lack hardware files for newer models
    # (e.g. 0jbd6s / O1000 LiDAR PRO).  Alias them to the patched 5xu9h3 entry.
    if missing_classes:
        try:
            family_mod = importlib.import_module("deebot_client.hardware.5xu9h3")
            # Patch 5xu9h3 itself so the aliased entry carries the new class
            if getattr(family_mod, "CleanMower", None) is not None:
                family_mod.CleanMower = _CleanMowerNG  # type: ignore[attr-defined]
            elif getattr(family_mod, "CleanV2", None) is not None:
                family_mod.CleanV2 = _CleanMowerNG  # type: ignore[attr-defined]
            if getattr(family_mod, "GetCleanInfoV2", None) is not None and get_clean_info:
                family_mod.GetCleanInfoV2 = get_clean_info  # type: ignore[attr-defined]
            devices_cache.pop("5xu9h3", None)
            family_info = family_mod.get_device_info()
            devices_cache["5xu9h3"] = family_info
            for class_ in missing_classes:
                devices_cache[class_] = family_info
                not_found_cache.discard(class_)
                patched += 1
                _LOGGER.warning(
                    "deebot_mower_fix: aliased %s → 5xu9h3 family (app-header V2 payload)",
                    class_,
                )
        except (ModuleNotFoundError, AttributeError) as alias_err:
            _LOGGER.warning(
                "deebot_mower_fix: could not alias missing classes (%s) — %s",
                ", ".join(missing_classes),
                alias_err,
            )

    _LOGGER.warning(
        "deebot_mower_fix: done — %d class(es) patched/aliased, %d skipped",
        patched,
        already_fixed,
    )


# ---------------------------------------------------------------------------
# Home Assistant entry points
# ---------------------------------------------------------------------------

async def async_setup(hass: Any, config: Any) -> bool:
    """Set up the deebot_mower_fix component and apply the patch."""
    global _HA_TIMEZONE
    _HA_TIMEZONE = getattr(hass.config, "time_zone", "UTC") or "UTC"
    _LOGGER.warning(
        "deebot_mower_fix: using timezone %s for clean command headers",
        _HA_TIMEZONE,
    )
    _LOGGER.info("deebot_mower_fix: starting up, applying CleanMower patch …")
    await hass.async_add_executor_job(_apply_patch)

    # Force-reload any active ecovacs config entries so the patched hardware
    # cache is picked up.  Without this, devices created before our patch ran
    # keep their original (unpatched) capability bindings.
    try:
        entries = hass.config_entries.async_entries("ecovacs")
    except Exception:  # noqa: BLE001
        entries = []

    for entry in entries:
        try:
            _LOGGER.info(
                "deebot_mower_fix: reloading ecovacs config entry %s to apply patch",
                entry.entry_id,
            )
            await hass.config_entries.async_reload(entry.entry_id)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "deebot_mower_fix: failed to reload ecovacs entry %s: %s",
                entry.entry_id,
                err,
            )

    return True

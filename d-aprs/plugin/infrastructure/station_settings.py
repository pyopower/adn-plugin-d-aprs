# ADN DMR Peer Server plugin - gps-lc-aprs station settings
#
# Copyright (C) 2026  C31AG
#
###############################################################################
#   This program is free software; you can redistribute it and/or modify
#   it under the terms of the GNU General Public License as published by
#   the Free Software Foundation; either version 3 of the License, or
#   (at your option) any later version.
#
#   This program is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#   GNU General Public License for more details.
#
#   You should have received a copy of the GNU General Public License
#   along with this program; if not, write to the Free Software Foundation,
#   Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301  USA
###############################################################################

"""Per-station APRS settings: the plugin config, plus a D-APRS user_settings file."""

from __future__ import annotations

import ast
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_KEYS = ("call", "ssid", "icon", "comment")


def _station_entry(raw: Any) -> dict[str, str]:
    """D-APRS keeps each station as a list of one-key dicts; plain dicts work too."""
    items = raw if isinstance(raw, list) else [raw]
    out: dict[str, str] = {}
    for item in items:
        if isinstance(item, dict):
            out.update({k: str(v) for k, v in item.items() if k in _KEYS and v not in (None, "")})
    return out


def settings_path(settings_file: str | None, project_root: str) -> Path | None:
    if not settings_file:
        return None
    path = Path(settings_file)
    return path if path.is_absolute() else Path(project_root) / path


def read_user_settings(path: Path | None) -> dict[int, dict[str, str]]:
    """Stations of a D-APRS user_settings file; a missing file is an empty one."""
    if path is None or not path.exists():
        return {}
    try:
        data = ast.literal_eval(path.read_text(encoding="utf-8").strip() or "{}")
        return {int(dmr_id): _station_entry(raw) for dmr_id, raw in (data or {}).items()}
    except (OSError, ValueError, SyntaxError, TypeError, AttributeError) as e:
        logger.warning("(D-APRS) user settings not loaded from %s: %s", path, e)
        return {}


def write_user_settings(path: Path, stations: dict[int, dict[str, str]]) -> None:
    """Same format as D-APRS: {id: [{'call': ..}, {'ssid': ..}, {'icon': ..}, {'comment': ..}]}.
    Blocking (call it off the reactor); written to a temporary file and renamed."""
    data = {dmr_id: [{k: st.get(k, "")} for k in _KEYS] for dmr_id, st in sorted(stations.items())}
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(repr(data))
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def merge_stations(from_file: dict[int, dict[str, str]], inline: Any) -> dict[int, dict[str, str]]:
    """``stations`` in the plugin config on top of the user settings file."""
    stations = {k: dict(v) for k, v in from_file.items()}
    for dmr_id, raw in (inline or {}).items():
        stations.setdefault(int(dmr_id), {}).update(_station_entry(raw))
    return stations


def load_stations(inline: Any, settings_file: str | None, project_root: str) -> dict[int, dict[str, str]]:
    """Stations from ``user_settings_file`` (D-APRS format), then ``stations`` in config on top."""
    return merge_stations(read_user_settings(settings_path(settings_file, project_root)), inline)

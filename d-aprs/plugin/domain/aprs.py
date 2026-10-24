# ADN DMR Peer Server plugin - gps-lc-aprs APRS position report
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

"""APRS-IS position report for a DMR station (pure formatting)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from .gps_lc import GpsFix

# A plausible amateur callsign inside a longer string (alias, talker alias).
CALLSIGN_RE = re.compile(r"[A-Z0-9]{1,3}[0-9][A-Z]{1,4}")


@dataclass(frozen=True)
class Station:
    callsign: str
    ssid: str
    symbol_table: str
    symbol: str
    comment: str


def find_callsign(text: str | None) -> str | None:
    if not text:
        return None
    match = CALLSIGN_RE.search(text.upper())
    if not match or match.group(0) == "N0CALL":  # D-APRS placeholder for "not set"
        return None
    return match.group(0)


def _aprs_coordinate(value: float, is_latitude: bool) -> str:
    hemisphere = ("N" if value >= 0 else "S") if is_latitude else ("E" if value >= 0 else "W")
    value = abs(value)
    degrees = int(value)
    minutes = round((value - degrees) * 60.0, 2)
    if minutes >= 60.0:
        degrees, minutes = degrees + 1, 0.0
    return ("%02d%05.2f%s" if is_latitude else "%03d%05.2f%s") % (degrees, minutes, hemisphere)


def _course_speed(course_deg: float | None, speed_knots: float | None) -> str:
    """APRS CSE/SPD extension: course 001-360 (000 unknown), speed in knots."""
    if course_deg is None or not speed_knots:  # unknown, or standing still: no course to show
        return ""
    course = round(course_deg) % 360 or 360
    return "%03d/%03d" % (course, min(round(speed_knots), 999))


def position_report(
    station: Station,
    fix: GpsFix,
    dmr_id: int,
    when: datetime | str | None,
    tocall: str,
    course_deg: float | None = None,
    speed_knots: float | None = None,
) -> str:
    """One APRS-IS line: position with symbol, course/speed if known, and comment.

    ``when``: a datetime or an "hhmmss" UTC string (the fix time a radio sent) for a
    timestamped report, or None for one without timestamp ("!"), as digital gateways
    usually send.
    """
    source = f"{station.callsign}-{station.ssid}" if station.ssid else station.callsign
    if when is None:
        stamp = "!"
    elif isinstance(when, str):
        stamp = "@%sh" % when
    else:
        stamp = "@%sh" % when.strftime("%H%M%S")
    return "%s>%s,TCPIP*:%s%s%s%s%s%s%s DMR %d" % (
        source,
        tocall,
        stamp,
        _aprs_coordinate(fix.latitude, True),
        station.symbol_table,
        _aprs_coordinate(fix.longitude, False),
        station.symbol,
        _course_speed(course_deg, speed_knots),
        station.comment,
        dmr_id,
    )

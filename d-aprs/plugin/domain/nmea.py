# ADN DMR Peer Server plugin - gps-lc-aprs NMEA RMC
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

"""NMEA RMC sentences, as Anytone-type radios send their position in packet data."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .gps_lc import GpsFix

_RMC_RE = re.compile(rb"\$(G[PNL]RMC),([^*\r\n$]*)(?:\*([0-9A-Fa-f]{2}))?")


@dataclass(frozen=True)
class RmcFix:
    fix: GpsFix
    speed_knots: float | None
    course_deg: float | None


def _checksum_ok(talker_and_body: bytes, given: bytes | None) -> bool:
    if given is None:
        return True  # some radios send no checksum; the field checks below still apply
    value = 0
    for byte in talker_and_body:
        value ^= byte
    return value == int(given, 16)


def _degrees(text: str, hemisphere: str, degree_digits: int) -> float:
    value = int(text[:degree_digits]) + float(text[degree_digits:]) / 60.0
    return -value if hemisphere in ("S", "W") else value


def _optional_float(text: str) -> float | None:
    try:
        return float(text) if text else None
    except ValueError:
        return None


def parse_rmc(payload: bytes) -> RmcFix | None:
    """The first valid (status A) RMC position in a data payload, or None."""
    for match in _RMC_RE.finditer(payload):
        talker, body, given = match.group(1), match.group(2), match.group(3)
        if not _checksum_ok(talker + b"," + body, given):
            continue
        fields = body.decode("ascii", "replace").split(",")
        if len(fields) < 8 or fields[1] != "A":
            continue
        try:
            latitude = _degrees(fields[2], fields[3], 2)
            longitude = _degrees(fields[4], fields[5], 3)
        except (ValueError, IndexError):
            continue
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180) or fields[3] not in ("N", "S") or fields[5] not in ("E", "W"):
            continue
        return RmcFix(GpsFix(latitude, longitude), _optional_float(fields[6]), _optional_float(fields[7]))
    return None

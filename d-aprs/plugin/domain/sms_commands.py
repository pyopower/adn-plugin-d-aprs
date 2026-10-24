# ADN DMR Peer Server plugin - d-aprs: SMS commands
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

"""The D-APRS (hbnet) SMS commands a radio sends to the gateway's DMR ID.

    @SSID 9          APRS SSID of this radio (1-15)
    @ICON />         APRS symbol: table (/ \\ or an overlay) + symbol
    @COM text        APRS comment (up to 35 characters; "@COM" alone clears it)
    @APRS            back to the gateway defaults
    @MH JN02SM       report a position by Maidenhead locator (4, 6 or 8 characters)
    @EA1ABC-7 A-hi   APRS message to EA1ABC-7
    ID | TEST | HELP
"""

from __future__ import annotations

import re
from dataclasses import dataclass

COMMENT_MAX = 35
APRS_MESSAGE_MAX = 67

_SSID = re.compile(r"(?:[1-9]|1[0-5])")
_ICON = re.compile(r"[/\\0-9A-Z][!-~]")
_LOCATOR = re.compile(r"[A-R]{2}(?:[0-9]{2}(?:[A-X]{2}(?:[0-9]{2})?)?)?", re.I)
_APRS_MESSAGE = re.compile(r"@([A-Z0-9]{1,6}(?:-[A-Z0-9]{1,2})?)\s+A-(.*)", re.I | re.S)


@dataclass(frozen=True)
class Command:
    kind: str  # ssid | icon | comment | reset | locator | message | id | test | help | invalid | unknown
    value: str = ""
    to: str = ""  # message addressee


def parse(text: str) -> Command:
    t = text.strip()
    upper = t.upper()
    if upper in ("ID", "TEST", "HELP", "?", "@HELP"):
        return Command({"ID": "id", "TEST": "test"}.get(upper, "help"))
    m = _APRS_MESSAGE.fullmatch(t)
    if m:
        body = m.group(2).strip()
        return Command("message", body[:APRS_MESSAGE_MAX], m.group(1).upper()) if body else Command("invalid", "message")
    word, _, rest = t.partition(" ")
    word, rest = word.upper(), rest.strip()
    for prefix in ("@SSID", "@ICON", "@COM", "@MH"):  # also without the space, as hbnet took them
        if word.startswith(prefix) and word != prefix:
            word, rest = prefix, t.strip()[len(prefix):].strip()
            break
    if word == "@SSID":
        return Command("ssid", rest) if _SSID.fullmatch(rest) else Command("invalid", "ssid")
    if word == "@ICON":
        return Command("icon", rest) if _ICON.fullmatch(rest) else Command("invalid", "icon")
    if word == "@COM":
        return Command("comment", rest[:COMMENT_MAX])
    if word == "@APRS" and not rest:
        return Command("reset")
    if word == "@MH":
        return Command("locator", rest.upper()) if len(rest) in (4, 6, 8) and _LOCATOR.fullmatch(rest) \
            else Command("invalid", "locator")
    return Command("unknown")


def locator_to_latlon(locator: str) -> tuple[float, float]:
    """Centre of a Maidenhead square (4, 6 or 8 characters)."""
    loc = locator.upper()
    lon = (ord(loc[0]) - 65) * 20.0 - 180.0
    lat = (ord(loc[1]) - 65) * 10.0 - 90.0
    lon_size, lat_size = 20.0, 10.0
    if len(loc) >= 4:
        lon_size, lat_size = 2.0, 1.0
        lon += int(loc[2]) * lon_size
        lat += int(loc[3]) * lat_size
    if len(loc) >= 6:
        lon_size, lat_size = lon_size / 24, lat_size / 24
        lon += (ord(loc[4]) - 65) * lon_size
        lat += (ord(loc[5]) - 65) * lat_size
    if len(loc) >= 8:
        lon_size, lat_size = lon_size / 10, lat_size / 10
        lon += int(loc[6]) * lon_size
        lat += int(loc[7]) * lat_size
    return round(lat + lat_size / 2, 6), round(lon + lon_size / 2, 6)

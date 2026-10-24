# ADN DMR Peer Server plugin - d-aprs: Hytera HDAP (RRS, LP)
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

"""Hytera DMR Application Protocol, the parts a location gateway needs.

Message: | service (bit 7 = reliable) | opcode (2) | length (2) | payload | checksum | 0x03 |
checksum = ((~sum(opcode .. payload) & 0xFF) + 0x33) & 0xFF.

Over the air (Hytera PD786G, 24-oct-2026) the radio sends it as DMR short data, with
two octets before the message: the service (01 location, 02 registration) and the
retry number.

Location protocol message layout from OK-DMR/dmr-kaitai (location_protocol.ksy).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

SERVICE_RRS = 0x11
SERVICE_LP = 0x08
SERVICE_TMP = 0x09  # text messages

RRS_REGISTRATION = 0x0003
RRS_REGISTRATION_ACK = 0x0080

LP_CONDITION_REQUEST = 0xD001
LP_CONDITION_ANSWER = 0xD002

TRIGGER_DISTANCE = 1
TRIGGER_TIME = 2
TRIGGER_DISTANCE_AND_TIME = 3
TRIGGER_DISTANCE_OR_TIME = 4

# Octet before the message in short data, per service (learned from the radio).
DEFAULT_SD_PREFIX = {SERVICE_LP: 0x01, SERVICE_RRS: 0x02}


@dataclass(frozen=True)
class Hdap:
    service: int  # without the reliable bit
    opcode: int
    payload: bytes
    reliable: bool = False


def checksum(opcode_to_payload: bytes) -> int:
    return ((~sum(opcode_to_payload) & 0xFF) + 0x33) & 0xFF


def build(service: int, opcode: int, payload: bytes, reliable: bool = False) -> bytes:
    body = opcode.to_bytes(2, "big") + len(payload).to_bytes(2, "big") + payload
    return bytes([service | (0x80 if reliable else 0)]) + body + bytes([checksum(body), 0x03])


def find(buf: bytes) -> tuple[Hdap, int] | None:
    """First valid HDAP message in ``buf`` (envelope, length, checksum and end checked) and its offset."""
    services = (SERVICE_RRS, SERVICE_LP, SERVICE_TMP)
    for off in range(0, max(0, len(buf) - 6)):
        if buf[off] & 0x7F not in services:
            continue
        length = int.from_bytes(buf[off + 3:off + 5], "big")
        end = off + 7 + length
        if end > len(buf) or buf[end - 1] != 0x03:
            continue
        body = buf[off + 1:off + 5 + length]
        if checksum(body) != buf[end - 2]:
            continue
        return Hdap(buf[off] & 0x7F, int.from_bytes(body[:2], "big"), bytes(body[4:]), bool(buf[off] & 0x80)), off
    return None


def radio_ip(radio_id: int, subnet: int = 10) -> bytes:
    return bytes([subnet]) + radio_id.to_bytes(3, "big")


# ---------------------------------------------------------------- RRS


def registration_id(msg: Hdap) -> int | None:
    """Radio ID of a registration request."""
    if msg.service != SERVICE_RRS or msg.opcode != RRS_REGISTRATION or len(msg.payload) < 4:
        return None
    return int.from_bytes(msg.payload[1:4], "big")


def registration_ack(radio_id: int, valid_s: int, ok: bool = True) -> bytes:
    """Registration answer: the radio registers again after ``valid_s`` seconds."""
    payload = radio_ip(radio_id) + bytes([0 if ok else 1]) + int(valid_s).to_bytes(4, "big")
    return build(SERVICE_RRS, RRS_REGISTRATION_ACK, payload)


# ---------------------------------------------------------------- LP

# GPS field: status, hhmmss, ddmmyy, N/S, ddmm.mmmm, E/W, dddmm.mmmm, speed (knots), direction.
_GPS = re.compile(rb"([AV])(\d{6})(\d{6})([NS])(\d{4}\.\d{4})([EW])(\d{5}\.\d{4})([ 0-9.]{3})?([ 0-9.]{3})?")


@dataclass(frozen=True)
class LocationReport:
    radio_id: int | None
    valid: bool
    gps_time: str  # hhmmss UTC as the radio sent it
    latitude: float
    longitude: float
    speed_knots: float | None = None
    course_deg: int | None = None


def _ddmm(value: str, hemisphere: str) -> float:
    number = float(value)
    degrees = int(number // 100)
    result = degrees + (number - degrees * 100) / 60.0
    return round(-result if hemisphere in ("S", "W") else result, 6)


def location_report(msg: Hdap) -> LocationReport | None:
    """Any LP message carrying a GPS field: standard, triggered or condition report (with RSSI or not)."""
    if msg.service != SERVICE_LP:
        return None
    m = _GPS.search(msg.payload)
    if not m:
        return None
    radio = int.from_bytes(msg.payload[5:8], "big") if len(msg.payload) >= 8 else None
    speed = course = None
    try:
        if m.group(8) and m.group(8).strip():
            speed = float(m.group(8))
        if m.group(9) and m.group(9).strip():
            course = int(float(m.group(9)))
    except ValueError:
        pass
    return LocationReport(
        radio_id=radio,
        valid=m.group(1) == b"A",
        gps_time=m.group(2).decode(),
        latitude=_ddmm(m.group(5).decode(), m.group(4).decode()),
        longitude=_ddmm(m.group(7).decode(), m.group(6).decode()),
        speed_knots=speed,
        course_deg=course,
    )


def _ddhhmmss(seconds: int) -> bytes:
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    return b"%02d%02d%02d%02d" % (days, hours, minutes, secs)


def condition_request(radio_id: int, request_id: int, interval_s: int, trigger: int = TRIGGER_TIME,
                      distance_m: int = 0) -> bytes:
    """Ask the radio for reports every ``interval_s`` (and/or ``distance_m``), starting now, without end."""
    interval = _ddhhmmss(interval_s)
    payload = (
        (request_id & 0xFFFFFFFF).to_bytes(4, "big") + radio_ip(radio_id) + bytes([trigger])
        + int(distance_m).to_bytes(4, "big") + b"0" * 14 + b"0" * 14 + interval + interval
    )
    return build(SERVICE_LP, LP_CONDITION_REQUEST, payload)


@dataclass(frozen=True)
class ConditionAnswer:
    request_id: int
    radio_id: int
    trigger: int
    result: int  # 0 = accepted


def condition_answer(msg: Hdap) -> ConditionAnswer | None:
    if msg.service != SERVICE_LP or msg.opcode != LP_CONDITION_ANSWER or len(msg.payload) < 11:
        return None
    p = msg.payload
    return ConditionAnswer(int.from_bytes(p[0:4], "big"), int.from_bytes(p[5:8], "big"), p[8],
                           int.from_bytes(p[9:11], "big"))

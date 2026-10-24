# ADN DMR Peer Server plugin - d-aprs: MOTOTRBO ARS / LRRP / TMS over IP
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

"""MOTOTRBO data services over IPv4/UDP inside DMR packet data.

IP addresses are derived from DMR IDs: radio = 12.x.y.z, gateway = 13.x.y.z.
ARS (UDP 4005) registers the radio, LRRP (UDP 4001) carries locations, TMS (4007) text.
Message layouts as captured from MOTOTRBO radios and BrandMeister (2026).
"""

from __future__ import annotations

from dataclasses import dataclass

PORT_LRRP = 4001
PORT_ARS = 4005
PORT_TMS = 4007

RADIO_NET = 0x0C
GATEWAY_NET = 0x0D

# Registration answer and periodic location request, as BrandMeister sends them.
ARS_ACK = bytes.fromhex("0002bf011080")
LRRP_TRIGGERED_START = bytes.fromhex("090c22047fffffff54625734313c")  # every 60 s

ARS_HELLO_OPCODES = (0x3820, 0x3840)  # initial / periodic registration


def _ones_complement(data: bytes) -> int:
    if len(data) % 2:
        data += b"\x00"
    total = 0
    for i in range(0, len(data), 2):
        total += (data[i] << 8) | data[i + 1]
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return ~total & 0xFFFF


def ip_udp(src_id: int, dst_id: int, port: int, payload: bytes, ip_id: int) -> bytes:
    """Gateway -> radio IPv4/UDP datagram (same port both ends)."""
    src_ip = bytes([GATEWAY_NET]) + src_id.to_bytes(3, "big")
    dst_ip = bytes([RADIO_NET]) + dst_id.to_bytes(3, "big")
    udp_len = 8 + len(payload)
    ports = port.to_bytes(2, "big") * 2
    pseudo = src_ip + dst_ip + b"\x00\x11" + udp_len.to_bytes(2, "big")
    udp_check = _ones_complement(pseudo + ports + udp_len.to_bytes(2, "big") + b"\x00\x00" + payload)
    udp = ports + udp_len.to_bytes(2, "big") + udp_check.to_bytes(2, "big") + payload
    header = (
        bytes([0x45, 0x01]) + (20 + len(udp)).to_bytes(2, "big") + (ip_id & 0xFFFF).to_bytes(2, "big")
        + b"\x00\x00\x40\x11\x00\x00" + src_ip + dst_ip
    )
    check = _ones_complement(header)
    return header[:10] + check.to_bytes(2, "big") + header[12:] + udp


@dataclass(frozen=True)
class UdpMessage:
    radio_id: int  # from the source address
    port: int  # destination port
    payload: bytes


def parse_ip_udp(packet: bytes) -> UdpMessage | None:
    """The UDP message of a radio's IPv4 datagram (packet may carry pad octets after it)."""
    if len(packet) < 28 or packet[0] != 0x45 or packet[9] != 0x11:
        return None
    total = int.from_bytes(packet[2:4], "big")
    if total < 28 or total > len(packet):
        return None
    return UdpMessage(int.from_bytes(packet[13:16], "big"), int.from_bytes(packet[22:24], "big"),
                      bytes(packet[28:total]))


def ars_hello(payload: bytes) -> int | None:
    """The ARS opcode of a registration (0x3820 initial, 0x3840 periodic), else None."""
    if len(payload) < 4 or int.from_bytes(payload[:2], "big") != len(payload) - 2:
        return None
    i, value = 2, payload[2] & 0x7F
    while payload[i] & 0x80 and i + 1 < len(payload):
        i += 1
        value = value * 128 + (payload[i] & 0x7F)
    return value if value in ARS_HELLO_OPCODES else None


def tms_text(payload: bytes) -> str:
    """The text of a TMS message: its longest run of UTF-16 printable ASCII (either byte order)."""

    def longest(big_endian: bool) -> str:
        best, i, n = "", 0, len(payload)
        while i < n - 1:
            j, chars = i, []
            while j < n - 1:
                hi, lo = (payload[j], payload[j + 1]) if big_endian else (payload[j + 1], payload[j])
                if hi != 0 or not 0x20 <= lo <= 0x7E:
                    break
                chars.append(chr(lo))
                j += 2
            if chars:
                best = max(best, "".join(chars), key=len)
                i = j
            else:
                i += 1
        return best

    le, be = longest(False), longest(True)
    return (le if len(le) >= len(be) else be).strip()


# ---------------------------------------------------------------- LRRP

@dataclass(frozen=True)
class LrrpReport:
    latitude: float
    longitude: float
    speed_knots: float | None = None
    course_deg: int | None = None
    altitude_m: float | None = None


def _lat(raw: int) -> float:
    value = raw * 180.0 / 0xFFFFFFFF
    return round(value - 180.0 if value > 90.0 else value, 6)


def _lon(raw: int) -> float:
    value = raw * 360.0 / 0xFFFFFFFF
    return round(value - 360.0 if value > 180.0 else value, 6)


def _ufloat(b: bytes) -> float:
    return b[0] + b[1] * 0.01


def lrrp_report(payload: bytes) -> LrrpReport | None:
    """Location of a triggered (0x0D) or immediate (0x07) LRRP report; None without a fix."""
    if len(payload) < 2 or payload[0] not in (0x07, 0x0D, 0x0B, 0x11):
        return None
    i = 2
    lat = lon = None
    speed = course = alt = None
    try:
        while i < len(payload):
            tag = payload[i]
            i += 1
            if tag == 0x22:
                i += 1 + payload[i]
            elif tag in (0x51, 0x55, 0x66, 0x69):
                lat, lon = _lat(int.from_bytes(payload[i:i + 4], "big")), _lon(int.from_bytes(payload[i + 4:i + 8], "big"))
                if tag == 0x55:
                    alt = round(_ufloat(payload[i + 10:i + 12]), 2)
                elif tag == 0x69:
                    alt = round(_ufloat(payload[i + 10:i + 12]), 2)
                i += {0x51: 10, 0x55: 14, 0x66: 8, 0x69: 10}[tag]
            elif tag == 0x34:
                i += 5
            elif tag == 0x37:
                code = payload[i]
                i += 2
                if code & 0x80:
                    i += 1
            elif tag == 0x38:
                pass
            elif tag == 0x56:
                course = payload[i] * 2
                i += 1
            elif tag == 0x6C:
                speed = round(_ufloat(payload[i:i + 2]) * 1.943844, 2)  # m/s -> knots
                i += 2
            else:
                break  # unknown trailing fields: keep what we have
    except IndexError:
        pass
    if lat is None or lon is None:
        return None
    return LrrpReport(lat, lon, speed, course, alt)

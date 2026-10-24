# ADN DMR Peer Server plugin - gps-lc-aprs NMEA tests
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

"""Positions sent as NMEA RMC in packet data (Anytone type radios)."""

from __future__ import annotations

from datetime import datetime, timezone

from bitarray import bitarray
from plugin.application.position_use_case import PositionUseCase, Settings
from plugin.domain.aprs import Station, position_report
from plugin.domain.data_blocks import DataPacketAssembler, bptc_196_96
from plugin.domain.gps_lc import GpsFix
from plugin.domain.nmea import parse_rmc

from adn_server.domain.dmr import bptc

GATEWAY = 900999


def _nmea(body: str) -> bytes:
    checksum = 0
    for ch in body.encode():
        checksum ^= ch
    return b"$" + body.encode() + b"*%02X\r\n" % checksum


RMC = _nmea("GPRMC,210509.00,A,4230.378,N,00131.310,E,12.4,87.5,240926,,,A")


def _burst(block12: bytes) -> bytes:
    """A data burst carrying 12 bytes, BPTC(196,96)-encoded by the server's encoder."""
    coded = bptc.interleave_19696(bptc.encode_19696(block12))
    burst = bitarray(264, endian="big")
    burst.setall(0)
    burst[0:98] = coded[0:98]
    burst[166:264] = coded[98:196]
    return burst.tobytes()


def _packet(payload: bytes) -> list[tuple[int, bytes]]:
    """Unconfirmed data header + rate 1/2 blocks, padded to 12-byte blocks."""
    payload += bytes(-len(payload) % 12)
    blocks = [payload[i : i + 12] for i in range(0, len(payload), 12)]
    header = bytes([0x02, 0x40]) + GATEWAY.to_bytes(3, "big") + (2130035).to_bytes(3, "big") + bytes([0x80 | len(blocks), 0, 0, 0])
    return [(6, _burst(header))] + [(7, _burst(b)) for b in blocks]


def test_bptc_196_96_round_trip() -> None:
    block = bytes(range(1, 13))
    assert bptc_196_96(_burst(block)) == block


def test_assembler_returns_the_payload_after_the_last_block() -> None:
    asm = DataPacketAssembler()
    out = [asm.feed(d, p) for d, p in _packet(b"\x45\x00" + RMC)]
    assert out[:-1] == [None] * (len(out) - 1)
    assert RMC in out[-1]


def test_rmc_position_course_and_speed() -> None:
    rmc = parse_rmc(b"junk-ip-udp-header" + RMC)
    assert (round(rmc.fix.latitude, 5), round(rmc.fix.longitude, 5)) == (42.5063, 1.52183)
    assert (rmc.speed_knots, rmc.course_deg) == (12.4, 87.5)


def test_gnrmc_southern_western_and_no_checksum() -> None:
    rmc = parse_rmc(b"$GNRMC,120000,A,3326.933,S,07040.156,W,0.0,,240926,,,A\r\n")
    assert (round(rmc.fix.latitude, 4), round(rmc.fix.longitude, 4)) == (-33.4489, -70.6693)
    assert rmc.course_deg is None


def test_invalid_fix_or_bad_checksum_is_ignored() -> None:
    assert parse_rmc(_nmea("GPRMC,210509.00,V,,,,,,,240926,,,N")) is None
    assert parse_rmc(RMC.replace(b"4230.378", b"4230.379")) is None
    assert parse_rmc(b"hola, esto es un SMS") is None


def test_aprs_line_carries_course_and_speed() -> None:
    line = position_report(
        Station("EA3XYZ", "9", "/", ">", "coche"), GpsFix(42.5063, 1.52183), 2130035,
        datetime(2026, 9, 24, 21, 5, 9, tzinfo=timezone.utc), "APHBL3", 87.5, 12.4,
    )
    assert line == "EA3XYZ-9>APHBL3,TCPIP*:@210509h4230.38N/00131.31E>088/012coche DMR 2130035"


def _feed(uc: PositionUseCase, dst: int) -> None:
    for dtype, pkt in _packet(RMC):
        uc.on_data_frame(55, 2130035, dst, {"src_callsign": "EA3XYZ"}, dtype, pkt, 1000.0)


def test_use_case_reports_data_sent_to_the_gateway_id_only() -> None:
    sent: list[str] = []
    uc = PositionUseCase(Settings(data_dmr_ids=frozenset({GATEWAY})), sent.append)
    _feed(uc, 213)
    assert sent == []
    _feed(uc, GATEWAY)
    assert len(sent) == 1 and sent[0].startswith("EA3XYZ-15>") and "[088/012" in sent[0]


def test_data_positions_are_off_by_default() -> None:
    sent: list[str] = []
    uc = PositionUseCase(Settings(), sent.append)
    _feed(uc, GATEWAY)
    assert sent == []

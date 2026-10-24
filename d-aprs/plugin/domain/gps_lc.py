# ADN DMR Peer Server plugin - gps-lc-aprs GPS Info embedded LC
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

"""GPS positions carried in the embedded LC of a voice call (ETSI GPS Info FLCO).

OpenGD77 and other ETSI firmwares send their position this way, inside the
voice superframe, rather than as UDT (MD-380) or LRRP (MOTOTRBO) data.
"""

from __future__ import annotations

from dataclasses import dataclass

from bitarray import bitarray

from adn_server.domain.dmr import bptc, crc

FLCO_GPS_INFO = 0x08
_FID_STANDARD = 0x00
# Embedded LC fragments ride on voice bursts B-E (dtype_vseq 1-4).
_FRAGMENT_VSEQS = (1, 2, 3, 4)
# decode_emblc() keeps the 72 LC bits; the 5 checksum bits are column 10 of
# rows 2-6 of the 8x16 matrix, sent column by column (bit = row + 8 * column).
_CHECKSUM_BITS = tuple(row + 8 * 10 for row in range(2, 7))


@dataclass(frozen=True)
class GpsFix:
    latitude: float
    longitude: float


def _signed(value: int, bits: int) -> int:
    return value - (1 << bits) if value & (1 << (bits - 1)) else value


def gps_fix_from_lc(lc: bytes) -> GpsFix | None:
    """The position in a 9-byte GPS Info LC, or None for any other LC.

    Layout (ETSI TS 102 361-2): PF, R, FLCO(6), FID(8), reserved(4),
    position error(3), longitude(25, 360/2^25 deg), latitude(24, 180/2^24 deg).
    """
    if len(lc) != 9 or lc[0] & 0x3F != FLCO_GPS_INFO or lc[1] != _FID_STANDARD:
        return None
    payload = int.from_bytes(lc[2:], "big")  # 56 bits
    longitude = _signed((payload >> 24) & ((1 << 25) - 1), 25) * 360 / 2**25
    latitude = _signed(payload & ((1 << 24) - 1), 24) * 180 / 2**24
    if not (-90 <= latitude <= 90 and -180 <= longitude < 180):
        return None
    return GpsFix(latitude, longitude)


def lc_from_fragments(fragments: bitarray) -> bytes | None:
    """The 9-byte LC in four reassembled embedded fragments, if its checksum holds."""
    if len(fragments) != 128:
        return None
    lc = bptc.decode_emblc(fragments)
    received = bitarray([fragments[i] for i in _CHECKSUM_BITS])
    return lc if received == crc.csum5(lc) else None


class EmbeddedLcAssembler:
    """Collects the embedded LC of one voice stream, one superframe at a time.

    Runs on the reactor for every voice frame, so it keeps each fragment as a plain
    int (bits 116-147 of the burst) and builds a bitarray only once per superframe.
    """

    def __init__(self) -> None:
        self._acc: dict[int, int] = {}

    def feed(self, dtype_vseq: int, dmrpkt: bytes) -> bytes | None:
        """Add one voice burst; the LC when burst E completes a valid one."""
        if dtype_vseq not in _FRAGMENT_VSEQS or len(dmrpkt) < 33:
            return None
        if dtype_vseq == 1:
            self._acc.clear()
        # Bits 116..147 of the 264-bit burst: bytes 14..18, then drop 4 bits each side.
        self._acc[dtype_vseq] = (int.from_bytes(dmrpkt[14:19], "big") >> 4) & 0xFFFFFFFF
        if dtype_vseq != 4 or len(self._acc) != 4:
            return None
        value = 0
        for vseq in _FRAGMENT_VSEQS:
            value = (value << 32) | self._acc[vseq]
        self._acc.clear()
        fragments = bitarray(endian="big")
        fragments.frombytes(value.to_bytes(16, "big"))
        return lc_from_fragments(fragments)

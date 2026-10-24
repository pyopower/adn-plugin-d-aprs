# ADN DMR Peer Server plugin - gps-lc-aprs data blocks
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

"""DMR packet data: the 12-byte blocks of a data header and its rate 1/2 blocks.

Only what an unconfirmed position report needs: no FEC correction, no CRC-32 of
the reassembled packet (the NMEA checksum guards the payload).
"""

from __future__ import annotations

from bitarray import bitarray

DTYPE_DATA_HEADER = 6
DTYPE_RATE_12_DATA = 7

# BPTC(196,96), ETSI TS 102 361-1 B.1.1: bit i of the matrix is sent at (i * 181) % 196.
# Its 96 data bits: row 0 after the reserved bits, then 11 per row for rows 1-8.
_DATA_POSITIONS = tuple(range(4, 12)) + tuple(r * 15 + 1 + c for r in range(1, 9) for c in range(11))
_SENT_AT = tuple((i * 181) % 196 for i in _DATA_POSITIONS)


def bptc_196_96(dmrpkt: bytes) -> bytes:
    """The 12 bytes a data burst carries (its 196 info bits sit around the sync)."""
    burst = bitarray(endian="big")
    burst.frombytes(dmrpkt[:33])
    info = burst[0:98] + burst[166:264]
    return bitarray([info[i] for i in _SENT_AT], endian="big").tobytes()


def blocks_to_follow(header: bytes) -> int:
    """Blocks after a data header (octet 8, low 7 bits, for unconfirmed and confirmed data)."""
    return header[8] & 0x7F


class DataPacketAssembler:
    """Collects one data packet: a header, then as many rate 1/2 blocks as it announces."""

    def __init__(self) -> None:
        self._expected = 0
        self._blocks: list[bytes] = []

    def feed(self, dtype_vseq: int, dmrpkt: bytes) -> bytes | None:
        """Add one burst; the packet's payload once its last block arrives."""
        if len(dmrpkt) < 33:
            return None
        if dtype_vseq == DTYPE_DATA_HEADER:
            self._expected = blocks_to_follow(bptc_196_96(dmrpkt))
            self._blocks = []
            return None
        if dtype_vseq != DTYPE_RATE_12_DATA or not self._expected:
            return None
        self._blocks.append(bptc_196_96(dmrpkt))
        if len(self._blocks) < self._expected:
            return None
        self._expected = 0
        return b"".join(self._blocks)

# ADN DMR Peer Server plugin - d-aprs: BPTC(196,96)
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

"""BPTC(196,96) of data headers, CSBKs and rate 1/2 blocks (ETSI TS 102 361-1 B.1.1).

Decoding takes the 96 data bits straight from the matrix (no FEC correction: the
CRCs of each block and of the packet catch errors). Encoding uses the server's own
encoder.
"""

from __future__ import annotations

from bitarray import bitarray

from adn_server.domain.dmr import bptc as _server_bptc

# Bit i of the matrix is sent at (i * 181) % 196. Its 96 data bits: row 0 after the
# reserved bits, then 11 per row for rows 1-8.
_DATA_POSITIONS = tuple(range(4, 12)) + tuple(r * 15 + 1 + c for r in range(1, 9) for c in range(11))
_SENT_AT = tuple((i * 181) % 196 for i in _DATA_POSITIONS)


def decode_bptc(info196: bitarray) -> bytes:
    """196 on-air info bits -> the 12 bytes they carry."""
    return bitarray([info196[i] for i in _SENT_AT], endian="big").tobytes()


def encode_bptc(twelve: bytes) -> bitarray:
    """12 bytes -> 196 on-air info bits."""
    if len(twelve) != 12:
        raise ValueError("BPTC(196,96) carries 12 bytes")
    return _server_bptc.interleave_19696(_server_bptc.encode_19696(bytes(twelve)))

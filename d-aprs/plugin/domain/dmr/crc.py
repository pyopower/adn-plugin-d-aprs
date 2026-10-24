# ADN DMR Peer Server plugin - d-aprs: DMR data CRCs
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

"""CRCs of DMR packet data (ETSI TS 102 361-1 B.3), as real radios compute them.

All checked against on-air captures of MOTOTRBO and Hytera radios and BrandMeister.
"""

from __future__ import annotations

# CRC-CCITT, inverted, then masked (B.3.7 / B.3.12): data header and CSBK.
_MASK_DATA_HEADER = 0xCCCC
_MASK_CSBK = 0xA5A5


def _crc_ccitt(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def crc16_data_header(ten_bytes: bytes) -> int:
    return _crc_ccitt(ten_bytes) ^ 0xFFFF ^ _MASK_DATA_HEADER


def crc16_csbk(ten_bytes: bytes) -> int:
    return _crc_ccitt(ten_bytes) ^ 0xFFFF ^ _MASK_CSBK


def crc32_data(user_and_pad: bytes) -> bytes:
    """Packet CRC-32 (B.3.9) as the 4 octets that end the last block.

    Poly 0x04C11DB7, MSB first, init 0, no xor, over the user data and pad octets
    taken in byte-swapped pairs; the result is sent little-endian.
    """
    data = bytearray(user_and_pad)
    tail = b""
    if len(data) % 2:
        tail, data = bytes(data[-1:]), data[:-1]
    data[0::2], data[1::2] = data[1::2], data[0::2]
    reg = 0
    for byte in bytes(data) + tail:
        for i in range(7, -1, -1):
            msb = (reg >> 31) & 1
            reg = (reg << 1) & 0xFFFFFFFF
            if msb ^ ((byte >> i) & 1):
                reg ^= 0x04C11DB7
    return reg.to_bytes(4, "little")


def crc9_block(data: bytes, dbsn: int, crc32: bytes | None = None) -> int:
    """CRC-9 of a confirmed data block (B.3.10), the way MOTOTRBO, Hytera and BrandMeister do it.

    Poly x^9+x^6+x^4+x^3+1, MSB first, init 0, over the block data, then (last block
    only) the CRC-32 octets as they are sent, then the 7-bit DBSN. The register goes
    on air as is (MSB first) in the 9 bits after the DBSN.
    """
    bits: list[int] = []
    for byte in bytes(data) + (crc32 or b""):
        bits.extend((byte >> i) & 1 for i in range(7, -1, -1))
    bits.extend((dbsn >> i) & 1 for i in range(6, -1, -1))
    reg = 0
    for bit in bits:
        feedback = ((reg >> 8) & 1) ^ bit
        reg = (reg << 1) & 0x1FF
        if feedback:
            reg ^= 0x059
    return reg

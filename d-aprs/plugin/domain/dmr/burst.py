# ADN DMR Peer Server plugin - d-aprs: data bursts, blocks and DMRD frames
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

"""Data bursts (33 bytes of a DMRD frame), the blocks they carry, and DMRD framing.

A burst is 98 info bits, 68 middle bits (slot type, sync, slot type), 98 info bits.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from bitarray import bitarray

from .bptc import decode_bptc, encode_bptc
from .crc import crc9_block, crc16_csbk, crc32_data
from .trellis import decode_trellis34, encode_trellis34

DT_CSBK = 3
DT_DATA_HEADER = 6
DT_RATE12 = 7
DT_RATE34 = 8

# Middle 68 bits per data type, taken from real radio bursts; the sync is swapped for the
# base station data sync on the way down (a hotspot rewrites slot type and sync anyway).
_TEMPLATES = {
    DT_CSBK: "52ce058589a413fc6b38481144cd5d7f77fd757acb324bb8e5c97483fb1f761396",
    DT_DATA_HEADER: "4f650ace65167233e1836c43c58d5d7f77fd757338c0355033256183828286b4",
    DT_RATE12: "0cbfb16b034d00f4a2c0888205ed5d7f77fd757095b0409c42d30026130141987a",
    DT_RATE34: "2b320a2f88c4b20226a22ab0463dff57d75df5da8723e8cf2e8093b8aa32752bd1",
}
_BS_DATA_SYNC = bitarray(bin(0xDFF57D75DF5D)[2:].zfill(48), endian="big")


def _middle(dtype: int) -> bitarray:
    bits = bitarray(endian="big")
    bits.frombytes(bytes.fromhex(_TEMPLATES[dtype]))
    mid = bits[98:166]
    return mid[0:10] + _BS_DATA_SYNC + mid[58:68]


_MIDDLE = {dtype: _middle(dtype) for dtype in _TEMPLATES}


def info_bits(payload33: bytes) -> bitarray:
    bits = bitarray(endian="big")
    bits.frombytes(bytes(payload33[:33]))
    return bits[0:98] + bits[166:264]


def make_burst(info196: bitarray, dtype: int) -> bytes:
    frame = info196[0:98] + _MIDDLE[dtype] + info196[98:196]
    return frame.tobytes()


# ---------------------------------------------------------------- blocks


@dataclass(frozen=True)
class Block:
    data: bytes  # user data octets of the block (CRC-32 of the last block not included)
    dbsn: int = 0
    crc_ok: bool = True  # CRC-9 of a confirmed block (unconfirmed: always True)
    crc32: bytes = b""  # last block only


def decode_block(payload33: bytes, dtype: int, confirmed: bool, last: bool) -> Block | None:
    """One rate 1/2 or rate 3/4 data burst -> its block; None if undecodable."""
    info = info_bits(payload33)
    if dtype == DT_RATE12:
        raw = decode_bptc(info)
    elif dtype == DT_RATE34:
        raw = decode_trellis34(info)
        if raw is None:
            return None
    else:
        return None
    if confirmed:
        dbsn = raw[0] >> 1
        crc9 = ((raw[0] & 1) << 8) | raw[1]
        body = raw[2:]
    else:
        dbsn, crc9, body = 0, None, raw
    data, crc32 = (body[:-4], body[-4:]) if last else (body, b"")
    ok = crc9 is None or crc9_block(data, dbsn, crc32 or None) == crc9
    return Block(data, dbsn, ok, crc32)


def _block_bytes(chunk: bytes, dbsn: int, confirmed: bool, crc32: bytes) -> bytes:
    if not confirmed:
        return chunk + crc32
    crc9 = crc9_block(chunk, dbsn, crc32 or None)
    return bytes([((dbsn & 0x7F) << 1) | (crc9 >> 8), crc9 & 0xFF]) + chunk + crc32


def build_blocks(user: bytes, rate34: bool, confirmed: bool) -> tuple[list[tuple[int, bytes]], int]:
    """User data -> (data type, on-air burst) of its blocks (padded, CRC-32 on the last), and the pad octets."""
    size = (18 if rate34 else 12) - (2 if confirmed else 0)
    n = 1
    while size * n - 4 < len(user):
        n += 1
    pad = size * n - 4 - len(user)
    body = bytes(user) + b"\x00" * pad
    crc32 = crc32_data(body)
    bursts = []
    for i in range(n):
        last = i == n - 1
        chunk = body[i * size:(i + 1) * size] if not last else body[i * size:]
        raw = _block_bytes(chunk, i, confirmed, crc32 if last else b"")
        if rate34:
            bursts.append((DT_RATE34, make_burst(encode_trellis34(raw), DT_RATE34)))
        else:
            bursts.append((DT_RATE12, make_burst(encode_bptc(raw), DT_RATE12)))
    return bursts, pad


def header_burst(header12: bytes) -> bytes:
    return make_burst(encode_bptc(header12), DT_DATA_HEADER)


def decode_header_burst(payload33: bytes) -> bytes:
    return decode_bptc(info_bits(payload33))


def preamble_burst(src: int, dst: int, blocks_to_follow: int) -> bytes:
    """CSBK preamble (opcode 0x3D, data, individual) announcing the blocks that follow."""
    b = bytearray(12)
    b[0], b[1], b[2], b[3] = 0xBD, 0x00, 0x80, blocks_to_follow & 0xFF
    b[4:7] = dst.to_bytes(3, "big")
    b[7:10] = src.to_bytes(3, "big")
    b[10:12] = crc16_csbk(bytes(b[:10])).to_bytes(2, "big")
    return make_burst(encode_bptc(bytes(b)), DT_CSBK)


# ---------------------------------------------------------------- DMRD


def dmrd(seq: int, src: int, dst: int, peer: int, dtype: int, stream_id: bytes, slot: int, payload33: bytes) -> bytes:
    """One HBP DMRD frame of private (unit) data: 55 bytes like a repeater sends them."""
    bits = (0x80 if slot == 2 else 0x00) | 0x40 | (2 << 4) | (dtype & 0x0F)  # unit, data sync
    return (
        b"DMRD" + bytes([seq & 0xFF]) + src.to_bytes(3, "big") + dst.to_bytes(3, "big")
        + peer.to_bytes(4, "big") + bytes([bits]) + stream_id + bytes(payload33) + b"\x00\x00"
    )


def data_frames(
    header12: bytes,
    blocks: list[tuple[int, bytes]],
    src: int,
    dst: int,
    peer: int,
    slot: int,
    preambles: int = 0,
    stream_id: bytes | None = None,
) -> list[bytes]:
    """DMRD frames of one packet: CSBK preambles, header, blocks."""
    sid = stream_id or os.urandom(4)
    bursts = [
        (DT_CSBK, preamble_burst(src, dst, (preambles - 1 - i) + 1 + len(blocks))) for i in range(preambles)
    ]
    bursts.append((DT_DATA_HEADER, header_burst(header12)))
    bursts.extend(blocks)
    return [dmrd(i, src, dst, peer, dtype, sid, slot, p) for i, (dtype, p) in enumerate(bursts)]


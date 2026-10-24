# ADN DMR Peer Server plugin - d-aprs: location data gateway
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

"""Packet data sent to the gateway's DMR ID: MOTOTRBO (ARS/LRRP) and Hytera (RRS/LP).

Reassembles each packet, answers the radio the way BrandMeister does (registration,
periodic location request, link layer ACK) and hands every location to ``on_position``.
Replies go out one packet after another: two streams on one slot reach no radio.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable

from ..domain.dmr import header as hd
from ..domain.dmr.burst import (
    DT_DATA_HEADER, DT_RATE12, DT_RATE34, Block, build_blocks, data_frames, decode_block, decode_header_burst,
)
from ..domain.dmr.crc import crc32_data
from ..domain.hytera import hdap
from ..domain.motorola import mototrbo as moto

logger = logging.getLogger(__name__)

FRAME_SPACING_S = 0.09
# Listen before talk: a radio on a simplex hotspot can't hear us while it transmits, so a
# packet waits until the radio has been quiet this long (checked again every RETRY_S).
QUIET_S = 1.5
RETRY_S = 1.0
MAX_WAIT_S = 20.0


@dataclass(frozen=True)
class MotorolaSettings:
    enabled: bool = True
    request_location: bool = True  # ask each registering radio for periodic reports (60 s)
    link_ack_delay_s: float = 0.15  # after the radio's packet: a simplex hotspot must be back on RX
    answer_delay_s: float = 4.0  # registration answer after the link ACK, like BrandMeister
    request_delay_s: float = 4.5  # location request after the registration answer
    request_min_gap_s: float = 8.0  # only drop near-simultaneous retries of the same registration
    preambles: int = 1


@dataclass(frozen=True)
class HyteraSettings:
    enabled: bool = True
    interval_s: int = 30  # periodic reports asked for; 0 = only what the radio sends by itself
    trigger: int = hdap.TRIGGER_TIME
    distance_m: int = 100
    registration_valid_s: int = 600  # the radio registers again after this
    rerequest_s: float = 600.0  # ask again at most this often (and at every registration)
    reply_delay_s: float = 8.0  # the radio repeats its packet ~3-4 s after it; answer after that
    preambles: int = 1  # CSBK preambles before each packet to the radio (it answered with 1)
    link_ack: bool = False  # SARQ ACK: only useful with the gateway close to the hotspot
    link_ack_delay_s: float = 0.0
    # Per radio: DMR ID -> interval_s (0 = ask nothing), overriding interval_s.
    radio_intervals: dict[int, int] = field(default_factory=dict)

    def interval_for(self, radio: int) -> int:
        return int(self.radio_intervals.get(radio, self.interval_s))


@dataclass(frozen=True)
class GatewaySettings:
    data_dmr_id: int = 900999
    motorola: MotorolaSettings = field(default_factory=MotorolaSettings)
    hytera: HyteraSettings = field(default_factory=HyteraSettings)


@dataclass(frozen=True)
class Position:
    radio_id: int
    latitude: float
    longitude: float
    source: str  # "motorola" | "hytera"
    gps_time: str | None = None  # hhmmss UTC, when the radio sent it
    course_deg: float | None = None
    speed_knots: float | None = None


@dataclass
class _Packet:
    header: hd.DataHeader
    slot: int
    seen: float
    blocks: list[Block] = field(default_factory=list)

    @property
    def confirmed_blocks(self) -> bool:
        """Confirmed data, and Hytera short data with SARQ, carry DBSN + CRC-9 in each block."""
        h = self.header
        return h.confirmed or (h.dpf == hd.DPF_SHORT_DATA_DEFINED and h.sarq)


class DataGateway:
    def __init__(
        self,
        settings: GatewaySettings,
        send: Callable[[bytes], object],
        call_later: Callable[..., object],
        on_position: Callable[[Position], None],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._s = settings
        self._send = send
        self._call_later = call_later
        self._on_position = on_position
        self._clock = clock
        self._packets: dict[int, _Packet] = {}
        self._send_until = 0.0
        self._ip_id = 0
        self._moto_last_request: dict[int, float] = {}
        self._hyt_last_request: dict[int, float] = {}
        self._hyt_seen: dict[tuple[int, str], float] = {}
        self._sd_prefix = dict(hdap.DEFAULT_SD_PREFIX)
        self._last_rx: dict[int, float] = {}  # radio -> last time we heard it

    def update_settings(self, settings: GatewaySettings) -> None:
        self._s = settings

    # ------------------------------------------------------------ input

    def on_frame(self, stream_id: int, src_id: int, dst_id: int, slot: int, dtype: int, dmrpkt: bytes, now: float) -> None:
        if dst_id != self._s.data_dmr_id or len(dmrpkt) < 33:
            return
        self._last_rx[src_id] = now
        if dtype == DT_DATA_HEADER:
            header = hd.DataHeader.from_bytes(decode_header_burst(dmrpkt))
            if header is None:
                return
            if header.dpf == hd.DPF_RESPONSE:
                logger.info("(D-APRS) response from %s: class %d type %d status %d",
                            src_id, header.response_class, header.response_type, header.response_status)
                return
            self._reap(now)
            if header.blocks:
                self._packets[stream_id] = _Packet(header, slot, now)
            return
        packet = self._packets.get(stream_id)
        if packet is None or dtype not in (DT_RATE12, DT_RATE34):
            return
        last = len(packet.blocks) + 1 >= packet.header.blocks
        block = decode_block(dmrpkt, dtype, packet.confirmed_blocks, last)
        if block is None:
            self._packets.pop(stream_id, None)
            return
        packet.blocks.append(block)
        if not last:
            return
        self._packets.pop(stream_id, None)
        user = b"".join(b.data for b in packet.blocks)
        if not all(b.crc_ok for b in packet.blocks) or crc32_data(user) != packet.blocks[-1].crc32:
            logger.info("(D-APRS) packet from %s with CRC errors, dropped (the radio sends it again)", src_id)
            return
        self._dispatch(packet, src_id, user, now)

    def _dispatch(self, packet: _Packet, radio: int, user: bytes, now: float) -> None:
        sap = packet.header.sap
        if sap == hd.SAP_IP and self._s.motorola.enabled:
            self._motorola(packet, radio, user)
        elif sap == hd.SAP_SHORT_DATA and self._s.hytera.enabled:
            self._hytera(packet, radio, user, now)
        else:
            logger.info("(D-APRS) data from %s with SAP %d ignored", radio, sap)

    # ------------------------------------------------------------ MOTOTRBO

    def _motorola(self, packet: _Packet, radio: int, user: bytes) -> None:
        s = self._s.motorola
        h = packet.header
        msg = moto.parse_ip_udp(user)
        if h.confirmed and h.response_requested:
            ack = hd.response_ack(radio, self._s.data_dmr_id, hd.SAP_IP, h.send_seq).to_bytes()
            self._queue(data_frames(ack, [], self._s.data_dmr_id, radio, self._s.data_dmr_id, packet.slot,
                                    s.preambles), s.link_ack_delay_s, listen=False)
            logger.info("(D-APRS) MOTOTRBO link ACK -> %s (N(S)=%d)", radio, h.send_seq)
            if msg is not None:
                self._call_later(s.answer_delay_s, self._motorola_message, msg, packet.slot)
            return
        if msg is not None:
            self._motorola_message(msg, packet.slot)

    def _motorola_message(self, msg: moto.UdpMessage, slot: int) -> None:
        s = self._s.motorola
        radio = msg.radio_id
        if msg.port == moto.PORT_ARS:
            opcode = moto.ars_hello(msg.payload)
            if opcode is None:
                return
            logger.info("(D-APRS) MOTOTRBO registration (ARS %04x) from %s", opcode, radio)
            self._motorola_send(radio, moto.PORT_ARS, moto.ARS_ACK, slot)
            if s.request_location:
                self._call_later(s.request_delay_s, self._motorola_request, radio, slot)
        elif msg.port == moto.PORT_LRRP:
            report = moto.lrrp_report(msg.payload)
            if report is None:
                kind = {0x0B: "answer to the location request", 0x0D: "report without a fix",
                        0x07: "immediate report without a fix"}.get(msg.payload[0] if msg.payload else -1, "message")
                logger.info("(D-APRS) MOTOTRBO LRRP %s from %s: %s", kind, radio, msg.payload.hex())
                return
            self._on_position(Position(radio, report.latitude, report.longitude, "motorola",
                                       course_deg=report.course_deg, speed_knots=report.speed_knots))
        elif msg.port == moto.PORT_TMS:
            text = moto.tms_text(msg.payload)
            if text:
                logger.info("(D-APRS) MOTOTRBO text from %s: %r", radio, text)

    def _motorola_request(self, radio: int, slot: int) -> None:
        now = self._clock()
        if now - self._moto_last_request.get(radio, 0.0) < self._s.motorola.request_min_gap_s:
            return
        self._moto_last_request[radio] = now
        logger.info("(D-APRS) MOTOTRBO periodic location request -> %s", radio)
        self._motorola_send(radio, moto.PORT_LRRP, moto.LRRP_TRIGGERED_START, slot)

    def _motorola_send(self, radio: int, port: int, payload: bytes, slot: int) -> None:
        gw = self._s.data_dmr_id
        self._ip_id = (self._ip_id + 1) & 0xFFFF
        ip = moto.ip_udp(gw, radio, port, payload, self._ip_id)
        blocks, pad = build_blocks(ip, rate34=True, confirmed=True)
        header = hd.DataHeader(
            dpf=hd.DPF_CONFIRMED, dst=radio, src=gw, sap=hd.SAP_IP, response_requested=True,
            pad_octets=pad, blocks_to_follow=len(blocks), resync=True, fsn=hd.FSN_SINGLE_CONFIRMED,
        ).to_bytes()
        self._queue(data_frames(header, blocks, gw, radio, gw, slot, self._s.motorola.preambles), 0.0)

    # ------------------------------------------------------------ Hytera

    def _hytera(self, packet: _Packet, radio: int, user: bytes, now: float) -> None:
        s = self._s.hytera
        found = hdap.find(user)
        if found is None:
            logger.info("(D-APRS) Hytera short data from %s without HDAP: %s", radio, user.hex())
            return
        msg, offset = found
        if offset >= 2:
            self._sd_prefix[msg.service] = user[offset - 2]
        if s.link_ack and packet.header.sarq:
            ack = hd.response_ack(radio, self._s.data_dmr_id, hd.SAP_SHORT_DATA).to_bytes()
            self._queue(data_frames(ack, [], self._s.data_dmr_id, radio, self._s.data_dmr_id, packet.slot, 0),
                        s.link_ack_delay_s, listen=False)

        rid = hdap.registration_id(msg)
        if rid is not None:
            logger.info("(D-APRS) Hytera registration from %s", radio)
            self._hytera_send(radio, hdap.registration_ack(radio, s.registration_valid_s), packet.slot)
            self._hyt_last_request.pop(radio, None)  # a registered radio takes a new request
            self._hytera_request(radio, packet.slot, now)
            return
        report = hdap.location_report(msg)
        if report is not None:
            key = (radio, report.gps_time)
            if now - self._hyt_seen.get(key, 0.0) >= 120:  # the radio repeats a report nobody ACKed
                self._hyt_seen = {k: t for k, t in self._hyt_seen.items() if now - t < 120}
                self._hyt_seen[key] = now
                if report.valid:
                    self._on_position(Position(radio, report.latitude, report.longitude, "hytera",
                                               gps_time=report.gps_time, course_deg=report.course_deg,
                                               speed_knots=report.speed_knots))
                else:
                    logger.info("(D-APRS) Hytera location from %s without a GPS fix", radio)
            self._hytera_request(radio, packet.slot, now)
            return
        answer = hdap.condition_answer(msg)
        if answer is not None:
            logger.info("(D-APRS) Hytera %s the periodic request %08x (trigger %d, result %d)",
                        radio, answer.request_id, answer.trigger, answer.result)
            return
        logger.info("(D-APRS) Hytera message from %s not handled: service %02x opcode %04x",
                    radio, msg.service, msg.opcode)

    def _hytera_request(self, radio: int, slot: int, now: float) -> None:
        s = self._s.hytera
        interval = s.interval_for(radio)
        if interval <= 0 or now - self._hyt_last_request.get(radio, 0.0) < s.rerequest_s:
            return
        self._hyt_last_request[radio] = now
        request_id = 0x20000000 | (int(now) & 0x0FFFFFFF)  # new each time: a repeated ID is ignored
        logger.info("(D-APRS) Hytera periodic location request -> %s (every %d s, trigger %d)",
                    radio, interval, s.trigger)
        self._hytera_send(radio, hdap.condition_request(radio, request_id, interval, s.trigger, s.distance_m), slot)

    def _hytera_send(self, radio: int, message: bytes, slot: int) -> None:
        """Short data like the radio sends it: LP rate 3/4 with SARQ, registration rate 1/2 without."""
        gw = self._s.data_dmr_id
        service = message[0] & 0x7F
        location = service == hdap.SERVICE_LP
        user = bytes([self._sd_prefix.get(service, 0x01), 0x00]) + message
        blocks, pad = build_blocks(user, rate34=location, confirmed=location)
        header = hd.DataHeader(
            dpf=hd.DPF_SHORT_DATA_DEFINED, dst=radio, src=gw, sap=hd.SAP_SHORT_DATA,
            response_requested=location, appended_blocks=len(blocks), sarq=location, bit_padding=pad * 8,
        ).to_bytes()
        self._queue(data_frames(header, blocks, gw, radio, gw, slot, self._s.hytera.preambles),
                    self._s.hytera.reply_delay_s)

    # ------------------------------------------------------------ output

    def _queue(self, frames: list[bytes], delay_s: float, listen: bool = True) -> None:
        """Send one packet after ``delay_s``, after what is already queued, and (``listen``) when
        the radio is quiet. A link ACK doesn't listen: it answers the transmission that just ended."""
        now = self._clock()
        start = max(delay_s, self._send_until - now + 0.3 if self._send_until > now else 0.0)
        self._send_until = now + start + len(frames) * FRAME_SPACING_S
        self._call_later(start, self._transmit, frames, now + start + (MAX_WAIT_S if listen else 0.0))

    def _transmit(self, frames: list[bytes], give_up_at: float) -> None:
        now = self._clock()
        radio = int.from_bytes(frames[0][8:11], "big")
        if now < give_up_at and now - self._last_rx.get(radio, 0.0) < QUIET_S:
            # the radio is talking: everything queued moves back with this packet
            self._send_until = max(self._send_until, now + RETRY_S + len(frames) * FRAME_SPACING_S)
            self._call_later(RETRY_S, self._transmit, frames, give_up_at)
            return
        for i, frame in enumerate(frames):
            self._call_later(i * FRAME_SPACING_S, self._send, frame)

    def _reap(self, now: float) -> None:
        for sid in [k for k, p in self._packets.items() if now - p.seen > 10]:
            del self._packets[sid]


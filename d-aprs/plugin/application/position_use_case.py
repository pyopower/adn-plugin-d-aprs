# ADN DMR Peer Server plugin - gps-lc-aprs position use case
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

"""From voice frames to APRS-IS position reports, one station at a time."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from ..domain.aprs import Station, find_callsign, position_report
from ..domain.data_blocks import DataPacketAssembler
from ..domain.gps_lc import EmbeddedLcAssembler, GpsFix, gps_fix_from_lc
from ..domain.nmea import RmcFix, parse_rmc

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Settings:
    ssid: str = "15"
    symbol_table: str = "/"
    symbol: str = "["
    comment: str = "D-APRS ADN Systems"
    tocall: str = "APHBL3"
    # Timestamp of the APRS report: "none" ("!", usual on digital), "gps" (the fix time a
    # radio sends, else the server time) or "server".
    timestamp: str = "none"
    min_interval_s: float = 30.0
    stream_ttl_s: float = 12.0
    # Packet data positions (NMEA RMC, Anytone type) are read only when sent to these IDs.
    data_dmr_ids: frozenset[int] = frozenset()
    # Per DMR ID: call / ssid / icon (table + symbol) / comment, overriding the above.
    stations: dict[int, dict[str, str]] = field(default_factory=dict)


@dataclass
class _Stream:
    src_id: int
    assembler: EmbeddedLcAssembler
    seen: float
    callsign_hint: str | None = None
    pending: GpsFix | None = None
    data: DataPacketAssembler = field(default_factory=DataPacketAssembler)


def _source(station: Station) -> str:
    """The APRS station a report goes out as: the rate limit applies per station, so two
    radios of one operator with different SSIDs don't hold each other back."""
    return f"{station.callsign}-{station.ssid}" if station.ssid else station.callsign


class PositionUseCase:
    """Decodes GPS Info LCs per stream and reports each station at most every ``min_interval_s``."""

    def __init__(self, settings: Settings, send: Callable[[str], None]) -> None:
        self._settings = settings
        self._send = send
        self._streams: dict[int, _Stream] = {}
        self._last_sent: dict[str, float] = {}

    def update_settings(self, settings: Settings) -> None:
        self._settings = settings

    def on_frame(
        self, stream_id: int, src_id: int, extra: dict[str, Any], dtype_vseq: int, dmrpkt: bytes, now: float
    ) -> None:
        stream = self._stream(stream_id, src_id, extra, now)
        lc = stream.assembler.feed(dtype_vseq, dmrpkt)
        fix = gps_fix_from_lc(lc) if lc is not None else None
        if fix is None:
            return
        logger.info("(D-APRS) stream %s src %s at %.5f, %.5f", stream_id, src_id, fix.latitude, fix.longitude)
        if not self._report(stream, fix, now):
            stream.pending = fix  # no callsign yet: try again with the talker alias at call end

    def on_data_frame(
        self,
        stream_id: int,
        src_id: int,
        dst_id: int,
        extra: dict[str, Any],
        dtype_vseq: int,
        dmrpkt: bytes,
        now: float,
    ) -> None:
        if dst_id not in self._settings.data_dmr_ids:
            return
        stream = self._stream(stream_id, src_id, extra, now)
        payload = stream.data.feed(dtype_vseq, dmrpkt)
        rmc = parse_rmc(payload) if payload is not None else None
        if rmc is None:
            return
        logger.info(
            "(D-APRS) NMEA from src %s at %.5f, %.5f", src_id, rmc.fix.latitude, rmc.fix.longitude
        )
        if not self._report(stream, rmc.fix, now, rmc):
            logger.info("(D-APRS) src %s sent a position but no callsign is known, not reported", src_id)

    def on_end(self, stream_id: int, extra: dict[str, Any], now: float) -> None:
        stream = self._streams.pop(stream_id, None)
        if stream is None or stream.pending is None:
            return
        stream.callsign_hint = stream.callsign_hint or find_callsign(extra.get("talker_alias"))
        if not self._report(stream, stream.pending, now):
            logger.info("(D-APRS) src %s sent a position but no callsign is known, not reported", stream.src_id)

    def _stream(self, stream_id: int, src_id: int, extra: dict[str, Any], now: float) -> _Stream:
        stream = self._streams.get(stream_id)
        if stream is None:
            self._reap(now)
            stream = self._streams[stream_id] = _Stream(src_id, EmbeddedLcAssembler(), now)
        stream.seen = now
        if stream.callsign_hint is None:
            stream.callsign_hint = find_callsign(extra.get("src_callsign"))
        return stream

    def _station(self, stream: _Stream) -> Station | None:
        return self._station_for(stream.src_id, stream.callsign_hint)

    def _station_for(self, src_id: int, callsign_hint: str | None) -> Station | None:
        s = self._settings
        own = s.stations.get(src_id, {})
        callsign = find_callsign(own.get("call")) or callsign_hint
        if not callsign:
            return None
        icon = own.get("icon") or ""
        table, symbol = (icon[0], icon[1]) if len(icon) >= 2 else (s.symbol_table, s.symbol)
        return Station(callsign, str(own.get("ssid") or s.ssid), table, symbol, own.get("comment") or s.comment)

    def _report(self, stream: _Stream, fix: GpsFix, now: float, rmc: RmcFix | None = None) -> bool:
        station = self._station(stream)
        if station is None:
            return False
        stream.pending = None
        if now - self._last_sent.get(_source(station), 0.0) < self._settings.min_interval_s:
            return True
        self._last_sent[_source(station)] = now
        course, speed = (rmc.course_deg, rmc.speed_knots) if rmc is not None else (None, None)
        self._send(position_report(station, fix, stream.src_id, self._when(now, None), self._settings.tocall,
                                   course, speed))
        return True

    def on_position(
        self,
        src_id: int,
        callsign_hint: str | None,
        fix: GpsFix,
        now: float,
        gps_time: str | None = None,
        course_deg: float | None = None,
        speed_knots: float | None = None,
    ) -> bool:
        """A position decoded from packet data (MOTOTRBO LRRP, Hytera LP); False without a callsign."""
        station = self._station_for(src_id, callsign_hint)
        if station is None:
            logger.info("(D-APRS) src %s sent a position but no callsign is known, not reported", src_id)
            return False
        logger.info("(D-APRS) position of %s (%s) at %.5f, %.5f", src_id, station.callsign, fix.latitude, fix.longitude)
        if now - self._last_sent.get(_source(station), 0.0) < self._settings.min_interval_s:
            return True
        self._last_sent[_source(station)] = now
        self._send(position_report(station, fix, src_id, self._when(now, gps_time), self._settings.tocall,
                                   course_deg, speed_knots))
        return True

    def _when(self, now: float, gps_time: str | None) -> datetime | str | None:
        mode = self._settings.timestamp
        if mode == "none":
            return None
        if mode == "gps" and gps_time and len(gps_time) == 6 and gps_time.isdigit():
            return gps_time
        return datetime.fromtimestamp(now, timezone.utc)

    def _reap(self, now: float) -> None:
        """Drop streams that never saw their end (lost terminator)."""
        ttl = self._settings.stream_ttl_s
        for stream_id in [k for k, v in self._streams.items() if now - v.seen > ttl]:
            del self._streams[stream_id]

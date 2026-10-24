# ADN DMR Peer Server plugin - d-aprs: plugin adapter
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

"""ServerPlugin adapter: voice and packet data in, APRS-IS position reports out; SMS commands."""

from __future__ import annotations

import dataclasses
import logging
import threading
import time
from typing import Any

from adn_server.application.plugins.domain.events import UnitDataFrame, VoiceCallEnd, VoiceCallFrame
from adn_server.domain.hbp_protocol import HBPF_VOICE

from ..domain.aprs import find_callsign
from ..domain.gps_lc import GpsFix
from ..domain.hytera import hdap
from ..infrastructure.aprs_is import AprsIsClient
from ..infrastructure.station_settings import (
    merge_stations, read_user_settings, settings_path, write_user_settings,
)
from .data_gateway import DataGateway, GatewaySettings, HyteraSettings, MotorolaSettings, Position
from .position_use_case import PositionUseCase, Settings
from .sms_use_case import SmsSettings, SmsUseCase

logger = logging.getLogger(__name__)

_TRIGGERS = {
    "time": hdap.TRIGGER_TIME,
    "distance": hdap.TRIGGER_DISTANCE,
    "distance_and_time": hdap.TRIGGER_DISTANCE_AND_TIME,
    "distance_or_time": hdap.TRIGGER_DISTANCE_OR_TIME,
}


def _settings(config: dict[str, Any], stations: dict[int, dict[str, str]]) -> Settings:
    defaults = Settings()
    voice = config.get("voice_gps") or {}
    return Settings(
        ssid=str(config.get("default_ssid", defaults.ssid)),
        symbol_table=str(config.get("symbol_table", defaults.symbol_table)),
        symbol=str(config.get("symbol", defaults.symbol)),
        comment=str(config.get("comment", defaults.comment)),
        tocall=str(config.get("tocall", defaults.tocall)),
        timestamp=str(config.get("timestamp", defaults.timestamp)).lower(),
        min_interval_s=float(config.get("min_interval_s", defaults.min_interval_s)),
        stream_ttl_s=float(voice.get("stream_ttl_s", defaults.stream_ttl_s)),
        data_dmr_ids=frozenset(int(i) for i in (voice.get("nmea_data_dmr_ids") or [])),
        stations=stations,
    )


def _sms_settings(config: dict[str, Any]) -> SmsSettings:
    sms, d = config.get("sms") or {}, SmsSettings()
    return SmsSettings(
        enabled=bool(sms.get("enabled", d.enabled)),
        replies=bool(sms.get("replies", d.replies)),
        language=str(sms.get("language", d.language)).lower(),
        repeat_window_s=float(sms.get("repeat_window_s", d.repeat_window_s)),
        max_per_hour=int(sms.get("max_per_hour", d.max_per_hour)),
    )


def _inline_stations(config: dict[str, Any]) -> dict[int, dict[str, str]]:
    return merge_stations({}, config.get("stations"))


def _gateway_settings(config: dict[str, Any]) -> GatewaySettings:
    m, h = config.get("motorola") or {}, config.get("hytera") or {}
    md, hd_ = MotorolaSettings(), HyteraSettings()
    trigger = str(h.get("trigger", "time")).lower()
    return GatewaySettings(
        data_dmr_id=int(config.get("data_dmr_id", GatewaySettings().data_dmr_id)),
        motorola=MotorolaSettings(
            enabled=bool(m.get("enabled", md.enabled)),
            request_location=bool(m.get("request_location", md.request_location)),
            link_ack_delay_s=float(m.get("link_ack_delay_s", md.link_ack_delay_s)),
            answer_delay_s=float(m.get("answer_delay_s", md.answer_delay_s)),
            request_delay_s=float(m.get("request_delay_s", md.request_delay_s)),
            preambles=int(m.get("preambles", md.preambles)),
        ),
        hytera=HyteraSettings(
            enabled=bool(h.get("enabled", hd_.enabled)),
            interval_s=int(h.get("interval_s", hd_.interval_s)),
            trigger=_TRIGGERS.get(trigger, hdap.TRIGGER_TIME),
            distance_m=int(h.get("distance_m", hd_.distance_m)),
            registration_valid_s=int(h.get("registration_valid_s", hd_.registration_valid_s)),
            rerequest_s=float(h.get("rerequest_s", hd_.rerequest_s)),
            reply_delay_s=float(h.get("reply_delay_s", hd_.reply_delay_s)),
            preambles=int(h.get("preambles", hd_.preambles)),
            link_ack=bool(h.get("link_ack", hd_.link_ack)),
            link_ack_delay_s=float(h.get("link_ack_delay_s", hd_.link_ack_delay_s)),
            radio_intervals={int(k): int(v) for k, v in (h.get("radio_intervals") or {}).items()},
        ),
    )


def _client(config: dict[str, Any]) -> AprsIsClient | None:
    aprs = config.get("aprs_is") or {}
    login, passcode = str(aprs.get("login", "")), str(aprs.get("passcode", ""))
    if not login or login.upper().startswith("N0CALL") or passcode in ("", "-1"):
        logger.warning("(D-APRS) aprs_is.login/passcode not set: positions are decoded and logged, not sent")
        return None
    return AprsIsClient(str(aprs.get("server", "rotate.aprs2.net")), int(aprs.get("port", 14580)), login, passcode)


class DAprsPlugin:
    name = "d-aprs"
    # Only these are built for this plugin (servers with plugin event interest; ignored by older ones).
    events = (VoiceCallFrame, VoiceCallEnd, UnitDataFrame)

    def __init__(self) -> None:
        self._ctx: Any = None
        self._client: AprsIsClient | None = None
        self._positions: PositionUseCase | None = None
        self._gateway: DataGateway | None = None
        self._voice_gps = True
        self._callsigns: dict[int, str] = {}
        self._sms: SmsUseCase | None = None
        self._settings_path = None
        self._config: dict[str, Any] = {}
        self._save_lock = threading.Lock()
        self._to_save: dict[int, dict[str, str]] | None = None

    def on_load(self, bus: Any, config: dict[str, Any], server_ctx: Any) -> None:
        del bus
        self._ctx = server_ctx
        self._config = config
        self._client = _client(config)
        self._settings_path = settings_path(config.get("user_settings_file"), server_ctx.project_root)
        user, inline = read_user_settings(self._settings_path), _inline_stations(config)
        settings = _settings(config, merge_stations(user, inline))
        self._positions = PositionUseCase(settings, self._send_aprs)
        self._sms = SmsUseCase(_sms_settings(config), self._positions, user, inline, self._stations_changed,
                               self._send_aprs, self._reply, settings.tocall)
        self._voice_gps = bool((config.get("voice_gps") or {}).get("enabled", True))
        gw = _gateway_settings(config)
        self._gateway = DataGateway(gw, self._send_dmrd, server_ctx.call_later, self._on_position,
                                    on_sms=self._on_sms)
        if server_ctx.send_dmrd is None:
            logger.warning("(D-APRS) no PLUGINS.send entry: positions are received, but radios get no "
                           "answers (MOTOTRBO/Hytera registration and periodic requests need it)")
        if self._settings_path is None:
            logger.warning("(D-APRS) user_settings_file not set: SMS settings (@SSID, @ICON...) last "
                           "until the server restarts")
        logger.info("(D-APRS) loaded: data ID %d, %d station overrides", gw.data_dmr_id, len(settings.stations))

    def on_reload(self, config: dict[str, Any]) -> None:
        old = self._client
        self._client = _client(config)
        if old is not None:
            self._ctx.defer_to_thread(old.close)
        self._config = config
        self._settings_path = settings_path(config.get("user_settings_file"), self._ctx.project_root)
        user, inline = read_user_settings(self._settings_path), _inline_stations(config)
        settings = _settings(config, merge_stations(user, inline))
        if self._positions is not None:
            self._positions.update_settings(settings)
        if self._sms is not None:
            self._sms.update(_sms_settings(config), user, inline, settings.tocall)
        if self._gateway is not None:
            self._gateway.update_settings(_gateway_settings(config))
        self._voice_gps = bool((config.get("voice_gps") or {}).get("enabled", True))

    def on_shutdown(self) -> None:
        if self._client is not None:
            self._ctx.defer_to_thread(self._client.close)
            self._client = None

    def on_event(self, event: object) -> None:
        if self._positions is None or self._gateway is None:
            return
        if isinstance(event, UnitDataFrame):
            ctx = event.context
            if ctx.direction != "RX" or ctx.is_synthetic:
                return  # our own answers come back as events too
            hint = find_callsign(ctx.extra.get("src_callsign"))
            if hint:
                self._callsigns[ctx.src_id] = hint
            self._gateway.on_frame(ctx.stream_id, ctx.src_id, ctx.dst_id, ctx.slot, event.dtype_vseq,
                                   event.dmrpkt, ctx.pkt_time)
            self._positions.on_data_frame(ctx.stream_id, ctx.src_id, ctx.dst_id, ctx.extra, event.dtype_vseq,
                                          event.dmrpkt, ctx.pkt_time)
        elif not self._voice_gps:
            return
        elif isinstance(event, VoiceCallFrame):
            ctx = event.context
            # Only what enters the server (not our own forwarded legs), and only bursts
            # B-E, which carry the embedded LC.
            if ctx.direction != "RX" or ctx.is_synthetic or event.frame_type != HBPF_VOICE:
                return
            self._positions.on_frame(ctx.stream_id, ctx.src_id, ctx.extra, event.dtype_vseq, event.dmrpkt, ctx.pkt_time)
        elif isinstance(event, VoiceCallEnd):
            self._positions.on_end(event.context.stream_id, event.context.extra, event.context.pkt_time)

    def _on_position(self, pos: Position) -> None:
        self._positions.on_position(pos.radio_id, self._callsigns.get(pos.radio_id),
                                    GpsFix(pos.latitude, pos.longitude), time.time(), pos.gps_time,
                                    pos.course_deg, pos.speed_knots)

    def _on_sms(self, radio: int, text: str) -> None:
        if self._sms is not None:
            self._sms.on_sms(radio, text, self._callsigns.get(radio))

    def _reply(self, radio: int, text: str) -> None:
        if self._gateway is not None:
            self._gateway.send_text(radio, text)

    def _stations_changed(self, user: dict[int, dict[str, str]]) -> None:
        """An SMS changed a station: use it now, save it off the reactor."""
        if self._positions is not None:
            merged = merge_stations(user, _inline_stations(self._config))
            self._positions.update_settings(dataclasses.replace(self._positions.settings, stations=merged))
        if self._settings_path is not None:
            self._to_save = user
            self._ctx.defer_to_thread(self._save, self._settings_path)

    def _save(self, path: Any) -> None:
        """In a worker thread; always writes the newest settings, whatever order the saves run in."""
        with self._save_lock:
            user, self._to_save = self._to_save, None
            if user is None:
                return
            try:
                write_user_settings(path, user)
                logger.info("(D-APRS) user settings saved to %s (%d stations)", path, len(user))
            except OSError as e:
                logger.warning("(D-APRS) user settings not saved to %s: %s", path, e)

    def _send_dmrd(self, frame: bytes) -> None:
        sender = self._ctx.send_dmrd
        if sender is None or not sender(frame):
            logger.debug("(D-APRS) frame to the radio not sent (no PLUGINS.send permission?)")

    def _send_aprs(self, line: str) -> None:
        if self._client is None:
            logger.info("(D-APRS) not sent (APRS-IS off): %s", line)
            return
        self._ctx.defer_to_thread(self._client.send, line)

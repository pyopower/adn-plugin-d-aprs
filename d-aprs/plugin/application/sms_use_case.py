# ADN DMR Peer Server plugin - d-aprs: SMS commands use case
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

"""SMS sent to the gateway: per-station APRS settings, manual positions, APRS messages."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable

from ..domain import sms_commands as cmd
from ..domain.aprs import message as aprs_message
from ..domain.gps_lc import GpsFix
from .position_use_case import PositionUseCase

logger = logging.getLogger(__name__)

TEXTS = {
    "en": {
        "ssid": "SSID set to {v}", "icon": "Icon set to {v}", "comment": "Comment set",
        "comment_cleared": "Comment cleared", "reset": "APRS settings back to defaults",
        "locator": "Position {v} sent", "message": "APRS message to {to} sent",
        "no_call": "No callsign known for ID {id}", "id": "ID {id} {call}", "id_unknown": "ID {id}, callsign unknown",
        "test": "D-APRS OK", "overridden": " (server config sets it)",
        "invalid_ssid": "SSID: 1-15", "invalid_icon": "Icon: 2 characters, e.g. />",
        "invalid_locator": "Locator: 4, 6 or 8 characters", "invalid_message": "Usage: @CALL A-text",
        "unknown": "Unknown command. Send HELP",
        "help": "@SSID 9 | @ICON /> | @COM text | @APRS (reset) | @MH JN02 | @CALL A-text | ID",
    },
    "es": {
        "ssid": "SSID cambiado a {v}", "icon": "Icono cambiado a {v}", "comment": "Comentario guardado",
        "comment_cleared": "Comentario borrado", "reset": "Ajustes APRS por defecto",
        "locator": "Posicion {v} enviada", "message": "Mensaje APRS a {to} enviado",
        "no_call": "Sin indicativo para el ID {id}", "id": "ID {id} {call}", "id_unknown": "ID {id}, sin indicativo",
        "test": "D-APRS OK", "overridden": " (lo fija la config del servidor)",
        "invalid_ssid": "SSID: 1-15", "invalid_icon": "Icono: 2 caracteres, p.ej. />",
        "invalid_locator": "Locator: 4, 6 u 8 caracteres", "invalid_message": "Uso: @INDICATIVO A-texto",
        "unknown": "Comando desconocido. Envia HELP",
        "help": "@SSID 9 | @ICON /> | @COM texto | @APRS (reset) | @MH JN02 | @CALL A-texto | ID",
    },
}

_FIELD = {"ssid": "ssid", "icon": "icon", "comment": "comment"}


@dataclass(frozen=True)
class SmsSettings:
    enabled: bool = True
    replies: bool = True  # answer every command with an SMS
    language: str = "en"
    repeat_window_s: float = 30.0  # the same text again within this time is a retry
    max_per_hour: int = 20


class SmsUseCase:
    def __init__(
        self,
        settings: SmsSettings,
        positions: PositionUseCase,
        user_stations: dict[int, dict[str, str]],
        inline_stations: dict[int, dict[str, str]],
        on_change: Callable[[dict[int, dict[str, str]]], None],
        send_aprs: Callable[[str], None],
        reply: Callable[[int, str], None],
        tocall: str,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._s = settings
        self._positions = positions
        self._user = {k: dict(v) for k, v in user_stations.items()}
        self._inline = inline_stations
        self._on_change = on_change
        self._send_aprs = send_aprs
        self._reply = reply
        self._tocall = tocall
        self._clock = clock
        self._seen: dict[tuple[int, str], float] = {}
        self._recent: dict[int, list[float]] = {}

    def update(self, settings: SmsSettings, user_stations: dict[int, dict[str, str]],
               inline_stations: dict[int, dict[str, str]], tocall: str) -> None:
        self._s, self._inline, self._tocall = settings, inline_stations, tocall
        self._user = {k: dict(v) for k, v in user_stations.items()}

    def on_sms(self, radio: int, text: str, callsign_hint: str | None) -> None:
        if not self._s.enabled or not text:
            return
        now = self._clock()
        if now - self._seen.get((radio, text), -1e9) < self._s.repeat_window_s:
            return  # the radio sent it again (no answer heard in time)
        self._seen = {k: t for k, t in self._seen.items() if now - t < self._s.repeat_window_s}
        self._seen[(radio, text)] = now
        recent = [t for t in self._recent.get(radio, []) if now - t < 3600]
        if len(recent) >= self._s.max_per_hour:
            logger.info("(D-APRS) SMS from %s ignored: more than %d in an hour", radio, self._s.max_per_hour)
            return
        self._recent[radio] = recent + [now]
        logger.info("(D-APRS) SMS from %s: %r", radio, text)
        answer = self._run(radio, cmd.parse(text), callsign_hint, now)
        if answer and self._s.replies:
            self._reply(radio, answer)

    # ------------------------------------------------------------

    def _t(self, key: str, **kw: object) -> str:
        texts = TEXTS.get(self._s.language, TEXTS["en"])
        return texts[key].format(**kw)

    def _run(self, radio: int, c: cmd.Command, hint: str | None, now: float) -> str:
        if c.kind in _FIELD:
            return self._set(radio, _FIELD[c.kind], c.value, hint)
        if c.kind == "reset":
            call = self._user.get(radio, {}).get("call") or hint
            self._user[radio] = {"call": call} if call else {}
            self._changed()
            return self._t("reset")
        if c.kind == "locator":
            station = self._positions.station_for(radio, hint)
            if station is None:
                return self._t("no_call", id=radio)
            lat, lon = cmd.locator_to_latlon(c.value)
            self._positions.on_position(radio, hint, GpsFix(lat, lon), now)
            return self._t("locator", v=c.value)
        if c.kind == "message":
            station = self._positions.station_for(radio, hint)
            if station is None:
                return self._t("no_call", id=radio)
            source = f"{station.callsign}-{station.ssid}" if station.ssid else station.callsign
            self._send_aprs(aprs_message(source, c.to, c.value, self._tocall))
            return self._t("message", to=c.to)
        if c.kind == "id":
            station = self._positions.station_for(radio, hint)
            if station is None:
                return self._t("id_unknown", id=radio)
            return self._t("id", id=radio, call=f"{station.callsign}-{station.ssid}" if station.ssid else station.callsign)
        if c.kind == "test":
            return self._t("test")
        if c.kind == "help":
            return self._t("help")
        if c.kind == "invalid":
            return self._t("invalid_" + c.value)
        return self._t("unknown")

    def _set(self, radio: int, field: str, value: str, hint: str | None) -> str:
        own = self._user.setdefault(radio, {})
        if hint and not own.get("call"):
            own["call"] = hint
        if value:
            own[field] = value
        else:
            own.pop(field, None)
        self._changed()
        key = field if value or field != "comment" else "comment_cleared"
        answer = self._t(key, v=value)
        if field in self._inline.get(radio, {}):
            answer += self._t("overridden")
        return answer

    def _changed(self) -> None:
        self._on_change({k: dict(v) for k, v in self._user.items()})

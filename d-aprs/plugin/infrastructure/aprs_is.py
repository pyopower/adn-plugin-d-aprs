# ADN DMR Peer Server plugin - gps-lc-aprs APRS-IS client
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

"""Minimal APRS-IS client: log in once, send lines, reconnect on failure.

Blocking by design: the plugin calls ``send`` through ``defer_to_thread``.
"""

from __future__ import annotations

import logging
import socket
import threading

logger = logging.getLogger(__name__)

_SOFTWARE = "adn-gps-lc-aprs 1.0"


class AprsIsClient:
    def __init__(self, host: str, port: int, login: str, passcode: str, timeout_s: float = 10.0) -> None:
        self._host = host
        self._port = port
        self._login = login
        self._passcode = passcode
        self._timeout_s = timeout_s
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()

    def _connect(self) -> socket.socket:
        sock = socket.create_connection((self._host, self._port), timeout=self._timeout_s)
        sock.recv(512)  # server banner
        sock.sendall(f"user {self._login} pass {self._passcode} vers {_SOFTWARE}\r\n".encode("ascii"))
        reply = sock.recv(512).decode("ascii", "replace")
        if "unverified" in reply:
            sock.close()
            raise ConnectionError(f"APRS-IS rejected passcode for {self._login}")
        logger.info("(D-APRS) APRS-IS connected to %s:%s as %s", self._host, self._port, self._login)
        return sock

    def send(self, line: str) -> bool:
        with self._lock:
            for attempt in (1, 2):
                try:
                    if self._sock is None:
                        self._sock = self._connect()
                    self._sock.sendall(line.encode("ascii", "replace") + b"\r\n")
                    logger.info("(D-APRS) APRS-IS >> %s", line)
                    return True
                except OSError as e:
                    logger.warning("(D-APRS) APRS-IS send failed (attempt %d): %s", attempt, e)
                    self.close_locked()
        return False

    def close(self) -> None:
        with self._lock:
            self.close_locked()

    def close_locked(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

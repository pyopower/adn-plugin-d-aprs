# ADN DMR Peer Server plugin - gps-lc-aprs tests
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

"""GPS Info LCs from voice bursts to APRS-IS lines."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from bitarray import bitarray
from plugin.application.plugin_impl import DAprsPlugin
from plugin.application.position_use_case import PositionUseCase, Settings
from plugin.domain.aprs import Station, find_callsign, position_report
from plugin.domain.gps_lc import EmbeddedLcAssembler, GpsFix, gps_fix_from_lc
from plugin.infrastructure.station_settings import load_stations

from adn_server.application.plugins.domain.events import CallLegContext, VoiceCallEnd, VoiceCallFrame
from adn_server.domain.dmr import bptc

# Encoded with okdmr (FullLinkControl GPSInfo), decoded back to these values.
ANDORRA_LC = bytes.fromhex("080000022a143c7413")  # 42.506307, 1.521821
SANTIAGO_LC = bytes.fromhex("0800019b7e1cd06da2")  # -33.448884, -70.669255
TA_HEADER_LC = bytes.fromhex("0400") + b"C31AG\x00\x00"


def _bursts(lc: bytes) -> list[tuple[int, bytes]]:
    """Voice bursts B-E carrying lc as embedded LC, as a radio sends them."""
    frags = bptc.encode_emblc(lc)
    out = []
    for vseq in (1, 2, 3, 4):
        bits = bitarray(264, endian="big")
        bits.setall(0)
        bits[116:148] = frags[vseq]
        out.append((vseq, bits.tobytes()))
    return out


def _decode(lc: bytes) -> bytes | None:
    asm = EmbeddedLcAssembler()
    results = [asm.feed(vseq, pkt) for vseq, pkt in _bursts(lc)]
    assert results[:3] == [None, None, None]
    return results[3]


def test_gps_info_matches_okdmr() -> None:
    a, s = gps_fix_from_lc(ANDORRA_LC), gps_fix_from_lc(SANTIAGO_LC)
    assert (round(a.latitude, 6), round(a.longitude, 6)) == (42.506307, 1.521821)
    assert (round(s.latitude, 6), round(s.longitude, 6)) == (-33.448884, -70.669255)


def test_other_lcs_are_not_positions() -> None:
    assert gps_fix_from_lc(TA_HEADER_LC) is None
    assert gps_fix_from_lc(bytes(9)) is None  # group voice LC


def test_embedded_lc_round_trip_through_voice_bursts() -> None:
    assert _decode(ANDORRA_LC) == ANDORRA_LC


def test_a_corrupted_fragment_fails_the_checksum() -> None:
    bursts = _bursts(ANDORRA_LC)
    vseq, pkt = bursts[2]
    bad = bytearray(pkt)
    bad[16] ^= 0x01  # inside the EMBED bits (116..147)
    bursts[2] = (vseq, bytes(bad))
    asm = EmbeddedLcAssembler()
    assert [asm.feed(v, p) for v, p in bursts][3] is None


def test_aprs_line() -> None:
    line = position_report(
        Station("C31AG", "15", "/", "[", "GPS-LC"),
        GpsFix(42.506307, 1.521821),
        2130035,
        datetime(2026, 9, 24, 21, 5, 9, tzinfo=timezone.utc),
        "APHBL3",
    )
    assert line == "C31AG-15>APHBL3,TCPIP*:@210509h4230.38N/00131.31E[GPS-LC DMR 2130035"


def test_minutes_never_round_up_to_sixty() -> None:
    line = position_report(Station("C31AG", "", "/", "[", ""), GpsFix(41.9999999, -0.9999999), 1, datetime(2026, 1, 1, tzinfo=timezone.utc), "APHBL3")
    assert "4200.00N" in line and "00100.00W" in line


def test_callsigns() -> None:
    assert find_callsign("C31AG") == "C31AG"
    assert find_callsign("EA3XYZ Juan") == "EA3XYZ"
    assert find_callsign("N0CALL") is None
    assert find_callsign("") is None


def _use_case(**kw):
    sent: list[str] = []
    return PositionUseCase(Settings(**kw), sent.append), sent


def _feed(uc: PositionUseCase, lc: bytes, stream_id: int = 1, src_id: int = 2130035, extra=None, now: float = 1000.0) -> None:
    for vseq, pkt in _bursts(lc):
        uc.on_frame(stream_id, src_id, extra or {}, vseq, pkt, now)


def test_reports_the_registered_callsign_once_per_interval() -> None:
    uc, sent = _use_case()
    _feed(uc, ANDORRA_LC, extra={"src_callsign": "C31AG"})
    _feed(uc, ANDORRA_LC, stream_id=2, extra={"src_callsign": "C31AG"}, now=1010.0)
    _feed(uc, ANDORRA_LC, stream_id=3, extra={"src_callsign": "C31AG"}, now=1031.0)
    assert len(sent) == 2 and sent[0].startswith("C31AG-15>APHBL3")


def test_station_overrides_win() -> None:
    uc, sent = _use_case(stations={2130035: {"call": "C31AG", "ssid": "9", "icon": "/>", "comment": "coche"}})
    _feed(uc, ANDORRA_LC, extra={"src_callsign": "EA3XYZ"})
    assert sent[0].startswith("C31AG-9>") and "E>coche DMR" in sent[0]


def test_without_callsign_waits_for_the_talker_alias_at_call_end() -> None:
    uc, sent = _use_case()
    _feed(uc, ANDORRA_LC)
    assert sent == []
    uc.on_end(1, {"talker_alias": "C31AG Adan"}, 1002.0)
    assert sent and sent[0].startswith("C31AG-15>")


def test_stations_from_a_daprs_user_settings_file(tmp_path) -> None:
    f = tmp_path / "user_settings.txt"
    f.write_text("{7149999: [{'call': 'N0TEST'}, {'ssid': '9'}, {'icon': ''}, {'comment': 'hola'}], 1: [{'call': 'N0CALL'}]}")
    stations = load_stations({7149999: {"comment": "override"}}, "user_settings.txt", str(tmp_path))
    assert stations[7149999] == {"call": "N0TEST", "ssid": "9", "comment": "override"}


def _ctx(**kw) -> CallLegContext:
    base = dict(call_family="GROUP", direction="RX", origin_system="SYSTEM-1", system_mode="MASTER", peer_id=1,
                src_id=2130035, dst_id=213, slot=2, stream_id=7, server_id=2131, is_synthetic=False,
                is_proxy_ingress=False, pkt_time=1000.0, extra={"src_callsign": "C31AG"})
    base.update(kw)
    return CallLegContext(**base)


def _plugin(config=None):
    deferred: list = []
    ctx = SimpleNamespace(project_root="/tmp", defer_to_thread=lambda fn, *a: deferred.append((fn, a)),
                          call_later=lambda *a: None, send_dmrd=None)
    plugin = DAprsPlugin()
    default = {"aprs_is": {"login": "C31AG-12", "passcode": "12345"}}
    plugin.on_load(None, default if config is None else config, ctx)
    return plugin, deferred


def test_plugin_sends_through_a_worker_thread_and_ignores_tx_legs() -> None:
    plugin, deferred = _plugin()
    for vseq, pkt in _bursts(ANDORRA_LC):
        plugin.on_event(VoiceCallFrame(context=_ctx(direction="TX"), dmrpkt=pkt, frame_type=0, dtype_vseq=vseq))
    assert deferred == []
    for vseq, pkt in _bursts(ANDORRA_LC):
        plugin.on_event(VoiceCallFrame(context=_ctx(), dmrpkt=pkt, frame_type=0, dtype_vseq=vseq))
    assert len(deferred) == 1
    fn, (line,) = deferred[0]
    assert fn.__name__ == "send" and line.startswith("C31AG-15>")
    plugin.on_event(VoiceCallEnd(context=_ctx(), duration_s=1.0, frame_count=6))


def test_without_aprs_is_login_it_only_logs() -> None:
    plugin, deferred = _plugin({})
    for vseq, pkt in _bursts(ANDORRA_LC):
        plugin.on_event(VoiceCallFrame(context=_ctx(), dmrpkt=pkt, frame_type=0, dtype_vseq=vseq))
    assert deferred == []

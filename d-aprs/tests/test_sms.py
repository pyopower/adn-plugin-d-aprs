# ADN DMR Peer Server plugin - d-aprs: SMS tests
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

"""SMS commands: parsing, the three on-air formats, settings file and replies."""

from __future__ import annotations

import ast

from plugin.application.position_use_case import PositionUseCase, Settings
from plugin.application.sms_use_case import SmsSettings, SmsUseCase
from plugin.domain import sms_commands as cmd
from plugin.domain.aprs import message
from plugin.domain.dmr import header as hd
from plugin.domain.dmr.burst import build_blocks, data_frames
from plugin.domain.hytera import hdap
from plugin.domain.motorola import mototrbo as moto
from plugin.infrastructure.station_settings import read_user_settings, write_user_settings

from test_data_gateway import GATEWAY, RADIO, _Harness, _packets


# ---------------------------------------------------------------- commands


def test_parse_commands() -> None:
    assert cmd.parse("@SSID 9") == cmd.Command("ssid", "9")
    assert cmd.parse("@ssid 16").kind == "invalid"
    assert cmd.parse("@ICON />") == cmd.Command("icon", "/>")
    assert cmd.parse("@ICON x").kind == "invalid"
    assert cmd.parse("@COM " + "x" * 50) == cmd.Command("comment", "x" * 35)
    assert cmd.parse("@COM") == cmd.Command("comment", "")
    assert cmd.parse("@APRS") == cmd.Command("reset")
    assert cmd.parse("@MH jn02sm") == cmd.Command("locator", "JN02SM")
    assert cmd.parse("@MH JN0").kind == "invalid"
    assert cmd.parse("@ea1abc-7 A-hola que tal") == cmd.Command("message", "hola que tal", "EA1ABC-7")
    assert cmd.parse(" test ") == cmd.Command("test")
    assert cmd.parse("?").kind == "help"
    assert cmd.parse("hello").kind == "unknown"
    assert cmd.parse("@MHJN02SM") == cmd.Command("locator", "JN02SM")  # without the space
    assert cmd.parse("@SSID9") == cmd.Command("ssid", "9")
    assert cmd.parse("@COMportable") == cmd.Command("comment", "portable")


def test_locator_centre() -> None:
    lat, lon = cmd.locator_to_latlon("JN02SM")
    assert 42.5 < lat < 42.5417 and 1.5 < lon < 1.5834
    assert cmd.locator_to_latlon("JN02") == (42.5, 1.0)


def test_aprs_message_line() -> None:
    assert message("N0TEST-9", "ea1abc", "hi {1}", "APHBL3") == "N0TEST-9>APHBL3,TCPIP*::EA1ABC   :hi 1}"


# ---------------------------------------------------------------- formats


def test_tms_round_trip_and_ack() -> None:
    m = moto.tms_parse(moto.tms_simple_text("@SSID 9", 5))
    assert (m.text, m.sequence, m.is_ack) == ("@SSID 9", 5, False)
    assert moto.tms_ack(5) == bytes.fromhex("0003bf0085")
    assert moto.tms_parse(moto.tms_ack(5)).is_ack


def test_hytera_tmp_round_trip_and_ack() -> None:
    raw = hdap.build(hdap.SERVICE_TMP, hdap.TMP_PRIVATE_MESSAGE,  # flags 0: ACK wanted
                     (7).to_bytes(4, "big") + hdap.radio_ip(GATEWAY) + hdap.radio_ip(RADIO) + "TEST".encode("utf-16-le"))
    msg, _ = hdap.find(b"\x03\x00" + raw)
    t = hdap.text_message(msg)
    assert (t.request_id, t.dst_id, t.src_id, t.text, t.ack_required) == (7, GATEWAY, RADIO, "TEST", True)
    ack, _ = hdap.find(hdap.text_ack(7, RADIO, GATEWAY))
    assert ack.opcode == 0x80A2 and ack.payload == (7).to_bytes(4, "big") + hdap.radio_ip(RADIO) + hdap.radio_ip(GATEWAY) + b"\x00"
    assert hdap.text_message(hdap.find(hdap.text(1, RADIO, GATEWAY, "ok"))[0]).ack_required is False


# ---------------------------------------------------------------- gateway


def _radio_ip_frames(port: int, payload: bytes, dst_net: int = moto.RADIO_NET) -> list[tuple[int, bytes]]:
    ip = bytearray(moto.ip_udp(RADIO, GATEWAY, port, payload, 1))
    ip[12], ip[16] = moto.RADIO_NET, dst_net  # radio -> gateway
    blocks, pad = build_blocks(bytes(ip), rate34=True, confirmed=True)
    header = hd.DataHeader(dpf=hd.DPF_CONFIRMED, dst=GATEWAY, src=RADIO, response_requested=True,
                           pad_octets=pad, blocks_to_follow=len(blocks), resync=True, send_seq=1,
                           fsn=hd.FSN_SINGLE_CONFIRMED)
    frames = data_frames(header.to_bytes(), blocks, RADIO, GATEWAY, RADIO, 2)
    return [(f[15] & 0x0F, f[20:53]) for f in frames]


def _harness_with_sms():
    h = _Harness()
    texts: list[tuple[int, str]] = []
    h.gw._on_sms = lambda radio, text: texts.append((radio, text))
    return h, texts


def test_mototrbo_tms_is_acknowledged_and_answered_in_tms() -> None:
    h, texts = _harness_with_sms()
    payload = bytearray(moto.tms_simple_text("@SSID 9", 3))
    payload[2] |= 0x40  # ACK wanted
    h.feed(_radio_ip_frames(moto.PORT_TMS, bytes(payload)), stream_id=1)
    h.run(until=h.now + 10)
    assert texts == [(RADIO, "@SSID 9")]
    assert h.gw.send_text(RADIO, "SSID set to 9")
    h.run(until=h.now + 10)
    link_ack, (ack_h, ack), (_, reply) = _packets(h.sent)
    assert link_ack[0].dpf == hd.DPF_RESPONSE
    ack_ip = moto.parse_ip_udp(ack)
    assert ack_ip.port == moto.PORT_TMS and ack_ip.payload == moto.tms_ack(3)
    assert ack[12] == moto.RADIO_NET  # from the address the radio wrote to
    assert moto.tms_parse(moto.parse_ip_udp(reply).payload).text == "SSID set to 9"


def test_dmr_standard_sms_is_answered_on_5016() -> None:
    h, texts = _harness_with_sms()
    h.feed(_radio_ip_frames(moto.PORT_DMR_STANDARD_SMS, moto.standard_sms("TEST")), stream_id=1)
    h.run(until=h.now + 10)
    assert texts == [(RADIO, "TEST")]
    h.gw.send_text(RADIO, "D-APRS OK")
    h.run(until=h.now + 10)
    reply = moto.parse_ip_udp(_packets(h.sent)[-1][1])
    assert reply.port == moto.PORT_DMR_STANDARD_SMS and moto.standard_sms_parse(reply.payload) == "D-APRS OK"


def test_hytera_tmp_is_acknowledged_once_and_answered_in_tmp() -> None:
    h, texts = _harness_with_sms()
    raw = hdap.build(hdap.SERVICE_TMP, hdap.TMP_PRIVATE_MESSAGE,
                     (9).to_bytes(4, "big") + hdap.radio_ip(GATEWAY) + hdap.radio_ip(RADIO) + "ID".encode("utf-16-le"))
    user = b"\x03\x00" + raw
    blocks, pad = build_blocks(user, rate34=True, confirmed=True)
    header = hd.DataHeader(dpf=hd.DPF_SHORT_DATA_DEFINED, dst=GATEWAY, src=RADIO, sap=hd.SAP_SHORT_DATA,
                           response_requested=True, appended_blocks=len(blocks), sarq=True, bit_padding=pad * 8)
    frames = [(f[15] & 0x0F, f[20:53]) for f in data_frames(header.to_bytes(), blocks, RADIO, GATEWAY, RADIO, 2)]
    h.feed(frames, stream_id=1)
    h.feed(frames, stream_id=2)  # the radio's retry
    assert texts == [(RADIO, "ID")]
    h.gw.send_text(RADIO, "ID 7149999 N0TEST-9")
    h.run(until=h.now + 40)
    sent = [hdap.find(u) for _, u in _packets(h.sent)]
    assert [m.opcode for m, _ in sent] == [0x80A2, 0x80A2, 0x80A1]  # ACK of each copy, then our text
    assert all(u[:2] == b"\x03\x00" for _, u in _packets(h.sent))  # prefix learned from the radio
    assert hdap.text_message(sent[-1][0]).text == "ID 7149999 N0TEST-9"


def test_hytera_tmp_over_udp_5016() -> None:
    h, texts = _harness_with_sms()
    raw = hdap.build(hdap.SERVICE_TMP, hdap.TMP_PRIVATE_MESSAGE,
                     (4).to_bytes(4, "big") + hdap.radio_ip(GATEWAY) + hdap.radio_ip(RADIO) + "@COM hi".encode("utf-16-le"))
    h.feed(_radio_ip_frames(moto.PORT_DMR_STANDARD_SMS, raw), stream_id=1)
    h.run(until=h.now + 10)
    assert texts == [(RADIO, "@COM hi")]
    h.gw.send_text(RADIO, "Comment set")
    h.run(until=h.now + 10)
    ack, reply = (hdap.find(moto.parse_ip_udp(u).payload)[0] for _, u in _packets(h.sent)[1:])
    assert ack.opcode == 0x80A2 and hdap.text_message(reply).text == "Comment set"


def test_send_text_to_a_radio_that_never_wrote() -> None:
    h, _ = _harness_with_sms()
    assert h.gw.send_text(RADIO, "hi") is False and h.sent == []


# ---------------------------------------------------------------- use case


class _Sms:
    def __init__(self, user=None, inline=None, **kw) -> None:
        self.aprs: list[str] = []
        self.replies: list[tuple[int, str]] = []
        self.saved: list[dict] = []
        self.now = 1000.0
        self.positions = PositionUseCase(Settings(stations=dict(user or {}), min_interval_s=0), self.aprs.append)
        self.uc = SmsUseCase(SmsSettings(**kw), self.positions, user or {}, inline or {}, self._changed,
                             self.aprs.append, lambda r, t: self.replies.append((r, t)), "APHBL3",
                             clock=lambda: self.now)

    def _changed(self, user) -> None:
        self.saved.append(user)
        self.positions.update_settings(Settings(stations=user, min_interval_s=0))

    def send(self, text: str, hint: str | None = "N0TEST") -> str:
        self.now += 60
        self.uc.on_sms(RADIO, text, hint)
        return self.replies[-1][1] if self.replies else ""


def test_settings_by_sms() -> None:
    s = _Sms()
    assert s.send("@SSID 9") == "SSID set to 9"
    assert s.send("@ICON />") == "Icon set to />"
    assert s.send("@COM portable") == "Comment set"
    assert s.saved[-1] == {RADIO: {"call": "N0TEST", "ssid": "9", "icon": "/>", "comment": "portable"}}
    assert s.send("ID") == "ID 7149999 N0TEST-9"
    assert s.send("@APRS") == "APRS settings back to defaults"
    assert s.saved[-1] == {RADIO: {"call": "N0TEST"}}


def test_manual_position_and_aprs_message() -> None:
    s = _Sms(user={RADIO: {"call": "N0TEST", "ssid": "9"}})
    assert s.send("@MH JN02SM") == "Position JN02SM sent"
    assert s.aprs[-1].startswith("N0TEST-9>APHBL3,TCPIP*:!4231.25N/00132.50E[")
    assert s.send("@EA1ABC A-hola") == "APRS message to EA1ABC sent"
    assert s.aprs[-1] == "N0TEST-9>APHBL3,TCPIP*::EA1ABC   :hola"


def test_no_callsign_known() -> None:
    s = _Sms()
    assert s.send("@MH JN02", hint=None) == "No callsign known for ID 7149999"
    assert s.aprs == []


def test_retries_and_flood_are_ignored() -> None:
    s = _Sms(max_per_hour=3)
    s.uc.on_sms(RADIO, "TEST", None)
    s.uc.on_sms(RADIO, "TEST", None)  # retry, same second
    assert len(s.replies) == 1
    for text in ("ID", "?", "@SSID 5"):
        s.now += 1
        s.uc.on_sms(RADIO, text, None)
    assert len(s.replies) == 3


def test_spanish_and_overridden_by_config() -> None:
    s = _Sms(inline={RADIO: {"ssid": "7"}}, language="es")
    assert s.send("@SSID 9") == "SSID cambiado a 9 (lo fija la config del servidor)"


def test_settings_file_round_trip_in_dapr_format(tmp_path) -> None:
    path = tmp_path / "d-aprs" / "user_settings.txt"
    write_user_settings(path, {RADIO: {"call": "N0TEST", "ssid": "9"}})
    raw = ast.literal_eval(path.read_text())
    assert raw == {RADIO: [{"call": "N0TEST"}, {"ssid": "9"}, {"icon": ""}, {"comment": ""}]}
    assert read_user_settings(path) == {RADIO: {"call": "N0TEST", "ssid": "9"}}
    assert read_user_settings(tmp_path / "missing.txt") == {}

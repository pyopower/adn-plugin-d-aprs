# ADN DMR Peer Server plugin - d-aprs: packet data gateway tests
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

"""On-air captures (Hytera PD786G and MOTOTRBO) through the gateway.

tests/data/*.txt: one burst per line, "<data type> <33-byte payload hex>", as the ADN
server delivered them to a D-APRS gateway on 24-oct-2026. Anonymized: the radio ID
(-> 7149999) and the position (-> 42.5083 N 1.525 E) were replaced and the bursts
re-encoded with the same format (rate, SARQ, preambles), checksums and CRCs; the
plugin's encoder was checked bit-identical against the original captures.
"""

from __future__ import annotations

import os
from pathlib import Path

from bitarray import bitarray

from plugin.application.data_gateway import DataGateway, GatewaySettings, HyteraSettings
from plugin.domain.dmr import header as hd
from plugin.domain.dmr.burst import (
    DT_DATA_HEADER, build_blocks, data_frames, decode_block, decode_header_burst, info_bits,
)
from plugin.domain.dmr.crc import crc32_data
from plugin.domain.dmr.trellis import decode_trellis34, encode_trellis34
from plugin.domain.hytera import hdap
from plugin.domain.motorola import mototrbo as moto

DATA = Path(__file__).parent / "data"
RADIO, GATEWAY = 7149999, 900999  # test ID, unassigned (radioid.net, 24-oct-2026)

# MOTOTRBO ARS registration (rate 1/2 unconfirmed), anonymized like tests/data.
MOTO_ARS = [
    (6, "4d12080268067a03f6e32283c58dff57d75df5d338fc3db001f522a30b439f35e3"),
    (7, "0cbfb1e302c102e4a680800205edff57d75df5d095a8418442b300a61e8153985f"),
    (7, "4bcd21ea006190552b3d318a45edff57d75df5d09790ed33ce256501431db52f7a"),
    (7, "04d12a7241641b4323a5112685edff57d75df5d0974977d0241360609d042b802c"),
    (7, "70e1212303b5015902a99b9805edff57d75df5d0947fa37bce20170420d81a385b"),
]


def _bursts(name: str) -> list[tuple[int, bytes]]:
    out = []
    for line in (DATA / name).read_text().split("\n"):
        if line.strip():
            dtype, payload = line.split()
            out.append((int(dtype), bytes.fromhex(payload)))
    return out


class _Harness:
    def __init__(self, settings: GatewaySettings | None = None) -> None:
        self.now = 1000.0
        self.scheduled: list[tuple[float, object, tuple]] = []
        self.sent: list[bytes] = []
        self.positions = []
        self.gw = DataGateway(settings or GatewaySettings(data_dmr_id=GATEWAY), self.sent.append,
                              self._call_later, self.positions.append, clock=lambda: self.now)

    def _call_later(self, delay, fn, *args):
        self.scheduled.append((self.now + delay, fn, args))

    def feed(self, bursts, stream_id: int, src: int = RADIO, dst: int = GATEWAY) -> None:
        for dtype, payload in bursts:
            self.gw.on_frame(stream_id, src, dst, 2, dtype, payload, self.now)

    def run(self, until: float) -> None:
        while True:
            due = sorted((t, i) for i, (t, _, _) in enumerate(self.scheduled) if t <= until)
            if not due:
                break
            t, i = due[0]
            _, fn, args = self.scheduled.pop(i)
            self.now = t
            fn(*args)
        self.now = until


def _packets(frames: list[bytes]) -> list[tuple[hd.DataHeader, bytes]]:
    """Reassemble DMRD frames we send (as a radio would)."""
    out, header, blocks = [], None, []
    for f in frames:
        dtype = f[15] & 0x0F
        payload = f[20:53]
        if dtype == DT_DATA_HEADER:
            header, blocks = hd.DataHeader.from_bytes(decode_header_burst(payload)), []
            if header.blocks == 0:
                out.append((header, b""))
            continue
        if dtype in (7, 8) and header is not None:
            confirmed = header.confirmed or header.sarq
            blk = decode_block(payload, dtype, confirmed, len(blocks) + 1 == header.blocks)
            assert blk is not None and blk.crc_ok
            blocks.append(blk)
            if len(blocks) == header.blocks:
                user = b"".join(b.data for b in blocks)
                assert crc32_data(user) == blocks[-1].crc32
                out.append((header, user))
    return out


# ---------------------------------------------------------------- DMR layer


def test_trellis_round_trip_and_single_bit_errors() -> None:
    for _ in range(50):
        data = os.urandom(18)
        coded = encode_trellis34(data)
        assert decode_trellis34(coded) == data
        flipped = bitarray(coded)
        flipped[os.urandom(1)[0] % 196] ^= 1
        assert decode_trellis34(flipped) == data


def test_real_headers_decode_and_encode_back_identically() -> None:
    for name in ("hytera_lp_report.txt", "hytera_rrs_registration.txt", "hytera_sack.txt"):
        for dtype, payload in _bursts(name):
            if dtype == DT_DATA_HEADER:
                raw = decode_header_burst(payload)
                header = hd.DataHeader.from_bytes(raw)
                assert header is not None and header.to_bytes() == raw


def test_real_hytera_blocks_pass_crc9_and_crc32() -> None:
    bursts = _bursts("hytera_lp_report.txt")
    start = next(i for i, (d, _) in enumerate(bursts) if d == DT_DATA_HEADER)
    blocks = [decode_block(p, d, True, k == 4) for k, (d, p) in enumerate(bursts[start + 1:start + 6])]
    assert all(b.crc_ok for b in blocks)
    assert crc32_data(b"".join(b.data for b in blocks)) == blocks[-1].crc32


def test_built_packets_reassemble() -> None:
    for rate34, confirmed in ((True, True), (False, False), (False, True), (True, False)):
        user = os.urandom(37)
        blocks, pad = build_blocks(user, rate34, confirmed)
        h = hd.DataHeader(dpf=hd.DPF_CONFIRMED if confirmed else hd.DPF_UNCONFIRMED, dst=RADIO, src=GATEWAY,
                          pad_octets=pad, blocks_to_follow=len(blocks))
        ((header, got),) = _packets(data_frames(h.to_bytes(), blocks, GATEWAY, RADIO, GATEWAY, 2))
        assert got[:len(user)] == user and header.blocks_to_follow == len(blocks)


# ---------------------------------------------------------------- Hytera


def test_hytera_report_gives_one_position_and_a_periodic_request() -> None:
    h = _Harness()
    bursts = _bursts("hytera_lp_report.txt")
    h.feed(bursts[:15], stream_id=1)  # report
    h.feed(bursts[15:], stream_id=2)  # the radio's retry of the same report
    assert len(h.positions) == 1
    pos = h.positions[0]
    assert abs(pos.latitude - 42.508333) < 1e-6 and abs(pos.longitude - 1.525) < 1e-6 and pos.gps_time == "051748"
    h.run(until=h.now + 20)
    ((header, user),) = _packets(h.sent)
    assert header.dpf == hd.DPF_SHORT_DATA_DEFINED and header.sarq and header.dst == RADIO
    assert user[:2] == b"\x01\x00"  # same prefix the radio uses for location
    msg, _ = hdap.find(user)
    assert msg.opcode == hdap.LP_CONDITION_REQUEST
    assert msg.payload[8] == hdap.TRIGGER_TIME and msg.payload[-16:] == b"0000003000000030"


def test_hytera_request_waits_for_the_radio_retry() -> None:
    h = _Harness()
    h.feed(_bursts("hytera_lp_report.txt")[:15], stream_id=1)
    assert min(t for t, _, _ in h.scheduled) >= h.now + 8


def test_hytera_registration_is_answered_like_the_radio_sends_it() -> None:
    h = _Harness()
    h.feed(_bursts("hytera_rrs_registration.txt"), stream_id=3)
    h.run(until=h.now + 30)
    (ack_header, ack), (req_header, req) = _packets(h.sent)
    assert ack_header.dpf == hd.DPF_SHORT_DATA_DEFINED and not ack_header.sarq  # rate 1/2, no SARQ
    assert ack == bytes.fromhex("0200") + hdap.registration_ack(RADIO, 600) + ack[2 + 16:]
    assert hdap.find(req)[0].opcode == hdap.LP_CONDITION_REQUEST


def test_hytera_per_radio_interval() -> None:
    h = _Harness(GatewaySettings(data_dmr_id=GATEWAY, hytera=HyteraSettings(radio_intervals={RADIO: 120})))
    h.feed(_bursts("hytera_lp_report.txt")[:15], stream_id=1)
    h.run(until=h.now + 20)
    ((_, user),) = _packets(h.sent)
    assert hdap.find(user)[0].payload[-16:] == b"0000020000000200"


def test_hytera_radio_with_interval_zero_gets_no_request() -> None:
    h = _Harness(GatewaySettings(data_dmr_id=GATEWAY, hytera=HyteraSettings(radio_intervals={RADIO: 0})))
    h.feed(_bursts("hytera_lp_report.txt")[:15], stream_id=1)
    h.run(until=h.now + 20)
    assert h.sent == [] and len(h.positions) == 1


def test_frames_for_other_ids_are_ignored() -> None:
    h = _Harness()
    h.feed(_bursts("hytera_lp_report.txt")[:15], stream_id=1, dst=123456)
    assert h.positions == [] and h.scheduled == []


# ---------------------------------------------------------------- MOTOTRBO


def test_mototrbo_registration_gets_answer_and_location_request() -> None:
    h = _Harness()
    h.feed([(d, bytes.fromhex(p)) for d, p in MOTO_ARS], stream_id=9)
    h.run(until=h.now + 30)
    packets = _packets(h.sent)
    assert len(packets) == 2
    (ack_h, ack), (req_h, req) = packets
    assert ack_h.confirmed and ack_h.sap == hd.SAP_IP and ack_h.dst == RADIO
    assert moto.parse_ip_udp(ack).payload == moto.ARS_ACK
    assert moto.parse_ip_udp(req).payload == moto.LRRP_TRIGGERED_START


def test_lrrp_reports() -> None:
    # Same values as the D-APRS (hbnet) decoder: lat/lon, speed 2.21 m/s, direction 60.
    a = moto.lrrp_report(bytes.fromhex("0D1A220400000001341F973280C6514BE8AED73B882B4603526C0215"))
    assert a is not None and round(a.latitude, 5) == 53.37346 and round(a.longitude, 5) == 83.71675
    assert a.speed_knots == round(2.21 * 1.943844, 2)
    b = moto.lrrp_report(bytes.fromhex("0D152203000001514BDE538F3B94F1E602436C0000561E"))
    assert b is not None and round(b.latitude, 5) == 53.34501 and b.course_deg == 60
    assert moto.lrrp_report(bytes.fromhex("0D0822030000013708")) is None


def test_tms_text() -> None:
    assert moto.tms_text(b"\x00\x10\xe0\x00" + "@SSID 9".encode("utf-16-le")) == "@SSID 9"


def test_mototrbo_confirmed_packet_gets_link_ack_first() -> None:
    gw_ip = moto.ip_udp(RADIO, GATEWAY, moto.PORT_ARS, bytes.fromhex("000ef02007373134303032330000"), 1)
    # turn our gateway->radio builder into a radio->gateway datagram: swap the nets
    ip = bytearray(gw_ip)
    ip[12], ip[16] = moto.RADIO_NET, moto.GATEWAY_NET
    blocks, pad = build_blocks(bytes(ip), rate34=True, confirmed=True)
    header = hd.DataHeader(dpf=hd.DPF_CONFIRMED, dst=GATEWAY, src=RADIO, response_requested=True,
                           pad_octets=pad, blocks_to_follow=len(blocks), resync=True, send_seq=2,
                           fsn=hd.FSN_SINGLE_CONFIRMED)
    frames = data_frames(header.to_bytes(), blocks, RADIO, GATEWAY, RADIO, 2)
    h = _Harness()
    h.feed([(f[15] & 0x0F, f[20:53]) for f in frames], stream_id=5)
    h.run(until=h.now + 1)
    ((ack, _),) = _packets(h.sent)
    assert ack.dpf == hd.DPF_RESPONSE and (ack.response_class, ack.response_type, ack.response_status) == (0, 1, 2)


def test_info_bits_round_trip() -> None:
    payload = bytes.fromhex(MOTO_ARS[1][1])
    assert len(info_bits(payload)) == 196


def test_hytera_packets_start_with_one_csbk_preamble() -> None:
    h = _Harness()
    h.feed(_bursts("hytera_lp_report.txt")[:15], stream_id=1)
    h.run(until=h.now + 20)
    assert [f[15] & 0x0F for f in h.sent][:2] == [3, 6]


def test_standing_still_reports_no_course() -> None:
    from plugin.domain.aprs import Station, position_report
    from plugin.domain.gps_lc import GpsFix

    line = position_report(Station("N0TEST", "9", "/", "[", "x"), GpsFix(8.5, -80.3), 7149999, None, "APHBL3", 0, 0.0)
    assert line == "N0TEST-9>APHBL3,TCPIP*:!0830.00N/08018.00W[x DMR 7149999"


def test_waits_while_the_radio_is_transmitting() -> None:
    h = _Harness()
    bursts = _bursts("hytera_lp_report.txt")
    h.feed(bursts[:15], stream_id=1)  # report: request due 8 s later
    h.run(until=h.now + 7.5)
    h.feed(bursts[15:], stream_id=2)  # the radio transmits again right then
    h.run(until=h.now + 1.0)
    assert h.sent == []  # held back
    h.run(until=h.now + 5.0)
    assert h.sent and len(_packets(h.sent)) == 1


def test_two_radios_of_one_operator_with_different_ssids_are_both_reported() -> None:
    from plugin.application.position_use_case import PositionUseCase, Settings
    from plugin.domain.gps_lc import GpsFix

    sent: list[str] = []
    uc = PositionUseCase(Settings(stations={1: {"call": "N0TEST", "ssid": "9"}, 2: {"call": "N0TEST", "ssid": "7"}},
                                  min_interval_s=20), sent.append)
    uc.on_position(1, None, GpsFix(42.5, 1.5), 1000.0)
    uc.on_position(2, None, GpsFix(42.5, 1.5), 1005.0)
    uc.on_position(2, None, GpsFix(42.5, 1.5), 1010.0)  # same station within 20 s: held back
    assert [line.split(">")[0] for line in sent] == ["N0TEST-9", "N0TEST-7"]

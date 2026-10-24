# d-aprs

Drop-in plugin for **ADN DMR Peer Server v2**: a complete D-APRS gateway. It sends to APRS-IS the
GPS positions of DMR radios, whichever way they report them:

| Radios | How they report | What the plugin does |
|---|---|---|
| **MOTOTRBO** | ARS registration + LRRP over IP/UDP in packet data | Link layer ACK, registration answer, periodic location request (every 60 s), decodes the reports |
| **Hytera** | RRS registration + LP (HDAP) in short data | Registration answer, periodic location request (interval per radio), decodes the reports |
| **OpenGD77 and other ETSI radios** | GPS Info LC embedded in voice calls | Decodes it on any talkgroup (passive) |
| **Anytone type radios** | NMEA `$GPRMC` in unconfirmed packet data | Decodes it (optional, `voice_gps.nmea_data_dmr_ids`) |

Users set their APRS station **by SMS** to the gateway ID, with the D-APRS commands (see [SMS commands](#sms-commands)).

It replaces the standalone hbnet `gps_data.py` D-APRS gateway (and the `gps-lc-aprs` plugin, which it
includes): no extra peer, no `[D-APRS]` system, one APRS-IS connection, and answers to the radios go
out from the server itself through `ServerContext.send_dmrd`.

- **No extra dependencies:** its own DMR packet data layer (data header, BPTC(196,96), rate 3/4
  trellis with Viterbi decoding, CRC-9/CRC-32 as real radios compute them, CSBK preambles).
  Checked byte for byte against the field validated hbnet gateway and against on-air captures.
- **Simplex hotspot friendly:** replies wait until the radio has been quiet (listen before talk),
  after the radio's own retries, one packet after another.
- **Never blocks the reactor:** APRS-IS runs through `defer_to_thread`.

## Requirements

- ADN DMR Peer Server v2 with plugin send support (`PLUGINS.send`, v2.6.0 and later).
- **Unit data delivered to hotspots as 55-byte frames**:
  [ADN-DMR-Peer-Server#154](https://github.com/Amateur-Digital-Network/ADN-DMR-Peer-Server/pull/154).
  Up to v2.6.1 the server forwards unit data to hotspots as 53-byte frames, which DMRGateway drops:
  positions are received and published, but the radios never hear the answers (no registration, no
  periodic reports). Apply that fix until it is in a release.

## Install

```bash
cp -r d-aprs /opt/ADN-DMR-Peer-Server/plugins/
# edit plugins/d-aprs/config.yaml: enabled: true, data_dmr_id, aprs_is login/passcode
```

Allow it to answer the radios, in `adn-server.yaml`:

```yaml
PLUGINS:
  send:
    d-aprs:
      allowed_src_ids: [900999]      # = data_dmr_id
```

Then `systemctl reload adn-server` (SIGHUP rescans `plugins/`). In Docker, mount the directory
(`./d-aprs:/opt/ADN-DMR-Peer-Server/plugins/d-aprs`). Secrets can stay out of the plugin directory with
`PLUGINS.overrides.d-aprs.aprs_is`.

**Don't run another gateway on the same IDs** (hbnet D-APRS peer, `gps-lc-aprs`): answers and
positions would go out twice. Note that the server caches a plugin's code once loaded: after
updating the plugin files, restart the server.

## Radio setup

- **MOTOTRBO:** ARS and GPS enabled, ARS/location server ID = `data_dmr_id`.
- **Hytera:** *Conventional → General Setting → Network → Radio Services*: Control Center ID =
  `data_dmr_id`, default ports (RRS 3002, Location 3003, TMS 5016).
- **OpenGD77:** GPS on and "send in voice" (no gateway ID needed).

## SMS commands

Sent as a private text message to `data_dmr_id`, in any of the formats radios use: MOTOTRBO TMS
(UDP 4007), "DMR standard" (UDP 5016: Hytera, Anytone, TYT...) or Hytera TMP (HDAP, in short data
or over UDP 5016). The gateway acknowledges the message when the radio asks for it and answers in
the same format.

| SMS | Effect |
|-----|--------|
| `@SSID 9` | APRS SSID of this radio (1-15) |
| `@ICON />` | APRS symbol: table (`/`, `\` or an overlay) + symbol |
| `@COM text` | Position comment, up to 35 characters (`@COM` alone clears it) |
| `@APRS` | Back to the gateway defaults |
| `@MH JN02SM` | Report a position by Maidenhead locator (4, 6 or 8 characters) |
| `@EA1ABC-7 A-text` | APRS message to EA1ABC-7 (up to 67 characters) |
| `ID` / `TEST` / `HELP` | Station the radio reports as / gateway alive / command list |

Settings are saved to `user_settings_file` (hbnet D-APRS format, so an existing file keeps working)
and apply at once. A field also set under `stations` in the config wins over the SMS (the answer says so).

## Configuration (`config.yaml`)

| Key | Default | Role |
|-----|---------|------|
| `data_dmr_id` | `900999` | The ID radios send their data to |
| `aprs_is.login` / `passcode` | unset | Igate login. Unset: positions are decoded and logged, not sent |
| `aprs_is.server` / `port` | `rotate.aprs2.net` / `14580` | APRS-IS server |
| `default_ssid` | `15` | SSID for stations without their own |
| `symbol_table` / `symbol` | `/` `[` | APRS symbol (person) |
| `comment` | `D-APRS ADN Systems` | Position comment (followed by `DMR <id>`) |
| `tocall` | `APHBL3` | APRS destination (software id) |
| `timestamp` | `none` | `none` (`!`, usual on digital), `gps` (fix time the radio sends) or `server` |
| `min_interval_s` | `20` | At most one report per callsign in this time |
| `motorola.enabled` | `true` | MOTOTRBO ARS/LRRP |
| `motorola.request_location` | `true` | Ask each registering radio for a report every 60 s |
| `motorola.preambles` | `1` | CSBK preambles before each packet to the radio |
| `hytera.enabled` | `true` | Hytera RRS/LP |
| `hytera.interval_s` | `30` | Periodic reports asked for at registration (power on); `0` = only what the radio sends by itself |
| `hytera.trigger` | `time` | `time`, `distance`, `distance_or_time`, `distance_and_time` |
| `hytera.distance_m` | `100` | For the distance triggers |
| `hytera.radio_intervals` | `{}` | Per DMR ID interval, e.g. `{7149999: 30, 2139999: 120}` (`0` = ask nothing) |
| `hytera.rerequest_s` | `600` | Ask a radio again at most this often (and at every registration) |
| `hytera.reply_delay_s` | `8` | The radio repeats its packet 3-4 s later; answer after that |
| `hytera.preambles` | `1` | CSBK preambles before each packet to the radio |
| `hytera.link_ack` | `false` | SARQ ACK of each report. The radio retries ~0.5 s after its last block: only a server close to the hotspot answers in time |
| `voice_gps.enabled` | `true` | GPS in voice calls |
| `voice_gps.nmea_data_dmr_ids` | `[]` | Gateway IDs whose packet data is read for NMEA positions |
| `sms.enabled` | `true` | SMS commands |
| `sms.replies` | `true` | Answer each command with an SMS |
| `sms.language` | `en` | Answers in `en` or `es` |
| `sms.max_per_hour` | `20` | Commands per radio and hour; more are ignored |
| `user_settings_file` | `data/d-aprs/user_settings.txt` | Station settings set by SMS (hbnet D-APRS format). Empty: kept until a restart |
| `stations` | `{}` | Per DMR ID `call` / `ssid` / `icon` / `comment`, on top of the file |

**Callsign** of a position, in order: the station's `call` override → the server's subscriber alias
for the DMR ID → the talker alias of the call (voice). Without one the position is logged, not sent.

## Tests

```bash
cd plugins/d-aprs
PYTHONPATH=/path/to/ADN-DMR-Peer-Server/src python3 -m pytest tests/ -q
```

`tests/data/` holds on-air bursts of a Hytera PD786G (location report, registration, selective ACK)
as the server delivered them; MOTOTRBO vectors come from real ARS registrations and LRRP reports.

## References

- ETSI TS 102 361-1 (data headers, BPTC, rate 3/4 trellis, CRCs) and 102 361-3 (packet data).
- Hytera location and text message protocol layouts: [OK-DMR/dmr-kaitai](https://github.com/OK-DMR/dmr-kaitai)
  (`location_protocol.ksy`, `text_message_protocol.ksy`).
- MOTOTRBO TMS layout: Moto.Net and node-dmr-lib; SMS commands as in hbnet D-APRS (KF7EEL).
- MOTOTRBO ARS/LRRP behaviour as captured from BrandMeister and real radios (hbnet D-APRS, C31AG).

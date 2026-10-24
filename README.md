# adn-plugin-d-aprs

Plugin for [ADN DMR Peer Server v2](https://github.com/Amateur-Digital-Network/ADN-DMR-Peer-Server):
a complete **D-APRS gateway** inside the server. It igates to APRS-IS the GPS positions of
**MOTOTRBO** (ARS/LRRP) and **Hytera** (RRS/LP) radios, answering them as BrandMeister does
(registration, periodic location requests), plus the positions that OpenGD77 and other ETSI radios
send **inside their voice calls** and NMEA positions in packet data (Anytone type).

Copy [`d-aprs/`](d-aprs/) into the server's `plugins/` directory, grant it `PLUGINS.send`, enable it
and reload — see [its README](d-aprs/README.md) for requirements, configuration and tests.

Needs unit data delivered to hotspots as full 55-byte frames
([ADN-DMR-Peer-Server#154](https://github.com/Amateur-Digital-Network/ADN-DMR-Peer-Server/pull/154)).

Licensed under the GNU GPL v3 or later, like the server.

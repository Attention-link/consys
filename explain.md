# How the link tools work: detailed reference

This is the English technical reference for `gs.py`, `drone.py`, and
`podglad_testu.py`. It documents the implemented behavior without changing
deployment, service names, paths, radio defaults, or hardware policy.

## 1. Project components

`gs.py` and `drone.py` are role-specific programs for the ground station and
drone. Each combines first-run installation, boot repair, WFB-NG
configuration, adapter inventory, live diagnostics, automatic link tuning,
test recording, and a curses TUI. They deliberately remain separate because
their addresses, adapter count, TX/RX policy, and service configuration differ.

`podglad_testu.py` is the Windows Tkinter viewer for recorded link tests.
`ap_test.py` temporarily converts the normal WFB adapter into an open access
point for a practical phone range test.

## 2. End-to-end data flow

The drone accepts video on UDP 5602; WFB-NG transports it to the GS output on
UDP 5600. The WFB tunnel uses `10.5.0.1` on the drone and `10.5.0.2` on the
GS. The GS exposes its local MAVLink endpoint at `127.0.0.1:14550`.

The displayed measurements originate in three places:

1. the local WFB-NG statistics API provides packet, FEC, RSSI/SNR, modulation,
   injection, and delay counters;
2. ping through the WFB tunnel measures peer reachability and latency;
3. the video-load generator sends timestamped UDP traffic through the ordinary
   video path.

Loss before FEC is `(lost + fec_rec) / (all + lost)`. Loss after FEC is
`lost / (all + lost)`. The recovered component is the difference between the
two. These measurements must be read as counter deltas: a service restart
resets counters and starts a new measurement baseline.

## 3. Module constants

The role constants define peer identity, expected adapter count, driver tag,
configuration paths, service names, and tunnel addresses. Radio defaults are
channel 13 (2472 MHz) and region `PL`; both endpoints must use matching
settings. Channel 13 is an ETSI/PL default, not a substitute for checking the
deployment site's regulations and hardware support.

The driver power range is an index from 0 to 63. The application caps it at 56
(90% of the driver range) to reduce USB brownout risk. This is a driver value,
not dBm or a regulatory measurement. Zero retains EEPROM calibration behavior.

The embedded matching WFB keys only bootstrap a new pair. They are not secret
and must be replaced before private operation.

## 4. Command helpers and system state

Command helpers run external utilities and preserve their output for the TUI
and diagnostics. The operational tools include `ip`, `iw`, `rfkill`, `lsusb`,
`dmesg`, `systemctl`, `journalctl`, `udevadm`, and WFB-NG commands. Failure
messages are diagnostic evidence; they do not silently change the topology.

The scripts inspect service state, loaded modules, adapter binding, monitor
mode, effective configuration, and running process arguments. A configured
value is not considered active until the running WFB process confirms it.

## 5. Network adapters

The GS requires one adapter named `gs_wfb`; it is `txrx`. The drone requires
two adapters: `drone_TX` is `tx` and `drone_RX` is `rx` only. The TX-only
choice is deliberate: it preserves the tested directional adapter policy, so
transmitting through the receive-only path is unsupported.

Persistent udev rules bind logical names to MAC addresses. USB location is a
fallback when MAC data is missing or duplicated. `/etc/wfb-cards.json` stores
last-known MAC, role, USB location, and timestamp, allowing the TUI to report
which physical adapter was removed even after it is no longer queryable.

Extra adapters are not silently assigned to a role. The operator must retain a
clear mapping between physical hardware and the role policy.

## 6. Systemd service and autostart

The existing role services are `wifibroadcast@gs.service` and
`wifibroadcast@drone.service`. First-run installation also creates the
role-specific repair units `wfb-gs-autostart.service` and
`wfb-drone-autostart.service`. Their `--autostart` invocation discovers
adapters, restores naming and role policy, checks the driver, and verifies
service readiness without opening the interactive menu.

This preserves the deployment procedure:

```bash
sudo python3 gs.py
sudo python3 drone.py
sudo python3 gs.py --autostart
sudo python3 drone.py --autostart
```

## 7. WFB-NG configuration

`/etc/wifibroadcast.cfg` is updated surgically so unrelated settings survive.
A one-time backup is created before the first managed modification. The scripts
also manage `/etc/default/wifibroadcast`, module options, persistent naming,
and the card inventory. On the drone, hotplug handling keeps the adapter list
consistent with interfaces that actually exist, rather than failing solely
because a configured adapter disappeared.

## 8. Transmit power, channels, and frequency

Live power uses the 88XXau WFB driver's sysfs setting. Generic
`iw ... set txpower` can report success while only updating cfg80211 state, so
it is not treated as a hardware power control. The access-point test uses the
same driver scale and restores pre-test power on cleanup.

Channel, region, modulation, and power are operational parameters. A live
change is applied only where the driver supports it; otherwise a service
restart is required. All link-affecting settings must agree at both endpoints.

## 9. Network diagnostics

Diagnostics distinguish local adapter/service health from end-to-end health.
They inspect USB presence and speed, interface names, MAC addresses, monitor
mode, channel, region, module ownership, service logs, tunnel ping, and SSH
reachability. A running service alone is insufficient evidence of a usable
radio link.

## 10. Reading WFB-NG statistics

The statistics reader speaks WFB-NG's local length-prefixed msgpack TCP API.
It accepts common byte-key and string-key variants and normalizes fields before
the rest of the UI uses them. This is a compatibility layer, not a stable
vendor-independent protocol: an API endpoint, framing, or schema change must
be validated after every WFB-NG upgrade.

Per-antenna readings are retained individually. Averaging them can hide a
single failed, shadowed, or poorly connected receive path.

## 11. Modulation and FEC

Modulation/MCS is reported alongside PHY rate and injection state because a
high configured MCS does not prove it is usable under current RF conditions.
Forward error correction adds redundant packets: for a `k/n` setting, `n-k`
redundant capacity allows recovery from loss within the block.

The FEC ladder starts at `1/1` (no added redundancy) and progresses to stronger
levels up to `1/5`. More protection consumes airtime. “No automatic FEC
change” and “no FEC protection” are separate states: automatic control can be
disabled while a fixed FEC level remains active.

## 12. Background probes

`WfbStatsProbe` samples local WFB statistics at a regular interval.
`PingProbe` samples tunnel ping independently. Both isolate a slow or failed
probe from the interactive loop and provide timestamps so the recorder and
viewer can show missing data as a gap rather than as a false zero.

## 13. Formatting and channel scanning

Formatting functions convert raw values to compact TUI rows and status bands.
They preserve unknown or unavailable values instead of inventing measurements.
Channel scanning is an observation aid; it does not override the configured
endpoint policy or independently retune one side of a link.

## 14. Automatic peer, channel, and FEC control

Automation is deliberately conservative. `AutoPeer` exchanges peer state and
configuration-change requests. `AutoChannel` requires sustained poor
conditions, coordinates a change with the peer, and waits for settling and
cooldown periods. It handles stale peer state so an old request cannot be
mistaken for current agreement.

`AutoFec` increases redundancy only after sustained poor post-recovery loss and
removes redundancy only after sustained good conditions. It does not
automatically remove all protection. Any automatic action can restart WFB-NG,
so its counter baseline and short settling period must be considered when
interpreting the next samples.

## 15. Installation, keys, and pairing

First-run installation prepares required packages, the project RTL driver,
WFB-NG configuration, key files, udev naming, and systemd support. It makes
privileged changes to `/etc`, modules, interfaces, udev, and services; it is
not a general-purpose Wi-Fi installer.

The pairing workflow displays an eight-character code on one side and accepts
it on the other. Both endpoints derive matching private key material. Use it,
or generate a new pair, before handling traffic that needs confidentiality or
authenticity. The shipped base64 defaults must be assumed public.

## 16. Curses interface and link-test recording

The curses layer provides menus for adapter assignment, configuration,
diagnostics, service inspection, radio settings, keys, and test control. It
renders only within terminal boundaries and keeps long command output and
service logs usable in a small screen.

The recorder runs independently of the visible screen so a test can continue
after the TUI exits. It writes a semicolon-separated log containing a header,
column names, timestamped samples, inserted comments, user markers, and a
summary. The `m` key records a numbered marker at the current test time.
Separate recording avoids blocking status refresh or losing a long test when
the terminal session is interrupted.

Totals distinguish sampled rates from cumulative counters. A counter reset,
such as a service restart, is not added as a negative packet count.

## 17. Link metrics and diagnosis

The test screen displays per-antenna RSSI/SNR, modulation, packet rates, loss
before/after FEC, recovered packets, cumulative packet error rate, delay, and
ping. It evaluates receive and return paths separately: successful reception
with missing ping replies can indicate an impaired uplink/return direction,
not necessarily a total radio outage.

The video-load test is more representative than ping alone because it uses the
same video FEC, packetization, modulation, and radio route as the intended
stream.

## 18. Test-log viewer

`podglad_testu.py` reads headers, samples, in-test events, markers, and
summaries from a log. `TestLog` parses scalar columns and antenna values.
`Panel` and `ChartArea` draw a shared time axis with independent panels for
RSSI, SNR, modulation, loss, recovered traffic, and ping.

Missing samples are represented as gaps. This is especially important for a
broken return direction: a flat line would incorrectly imply measured zero,
whereas an absent ping response is no measurement at all. Markers are red
vertical lines; ordinary comments are neutral event lines. Multiple logs are
normalized to their own start time for comparison.

Use the mouse wheel to zoom around the pointer, drag or Shift+wheel to pan,
Home/double-click to reset, and Ctrl+wheel to scroll vertically.

## 19. Safe changes

Keep the GS one-adapter `txrx` policy and the drone `drone_TX`/`drone_RX`
directional adapter policy. No power amplifier is used in this deployment. Do
not reinterpret the power index as dBm. Do not rely on the embedded keys for
security. Treat WFB-NG API changes as an integration test requirement. Preserve
matching settings and coordinated changes on both ends of the link.

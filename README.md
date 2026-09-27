# WFB-NG Connection and Link Diagnostics

This repository contains the Raspberry Pi side of a two-node wireless link built around **wfb-ng** and RTL8812AU-class USB Wi-Fi adapters. It is designed for a drone and a ground station (GS), with the drone-to-GS video path, bidirectional MAVLink/RC traffic, live radio diagnostics, link testing, and recovery after USB or reboot events.

The project is deliberately operational rather than library-oriented: `gs.py` and `drone.py` are root-run installers, repair tools, terminal user interfaces, and wfb-ng diagnostics in one program.

## Topology

```text
Drone Raspberry Pi                         Ground-station Raspberry Pi
--------------------                       ----------------------------
RTL8812AU: drone_TX  -- radio TX/RX ---->  RTL8812AU: gs_wfb
RTL8812AU: drone_RX  <-- radio RX only --  (same GS adapter is tx/rx)

wfb-ng tunnel: 10.5.0.1                  wfb-ng tunnel: 10.5.0.2
video input: UDP 5602  ---------------->  video output: UDP 5600
MAVLink/RC: configured by wfb-ng          local MAVLink endpoint: 127.0.0.1:14550
```

The drone normally uses two adapters. Both are initialized for reception, but only `drone_TX` is allowed to transmit. This preserves the tested directional adapter policy. The GS uses one adapter named `gs_wfb` for both directions.

## Repository contents

| File | Purpose |
| --- | --- |
| [`drone.py`](drone.py) | Installer, repair logic, TUI, and diagnostics for the airborne Raspberry Pi. |
| [`gs.py`](gs.py) | The corresponding ground-station program. It shares the same implementation and differs in role-specific constants. |
| [`ap_test.py`](ap_test.py) | Temporary open Wi-Fi access point for a practical range test with a phone. |
| [`podglad_testu.py`](podglad_testu.py) | Windows/Tkinter viewer for recorded link-test logs. The filename means “test viewer”. |
| [`physical_test_diagnostics.py`](physical_test_diagnostics.py) | Opt-in, read-only collector for structured physical-test evidence. |
| [`explain.md`](explain.md) | Detailed developer notes describing the functions and internal calculations. |

The original Polish `manual.docx` was intentionally omitted from this branch.
It is not replaced here and this repository does not claim to provide a full
translation of that manual.

## ALFA test branch

This branch is an ALFA test/simulation workspace. Its documentation and
operator-facing source text are maintained in English while retaining the
existing link behavior and hardware policy.

## Preserved deployment procedure

The existing deployment commands remain unchanged:

```bash
sudo python3 gs.py
sudo python3 drone.py
```

Their `--autostart` deployment remains unchanged:

```bash
sudo python3 gs.py --autostart
sudo python3 drone.py --autostart
```

`gs.py` and `drone.py` intentionally remain separate executable files because each role has different hardware, addresses, network direction, and adapter policy. Their shared logic should normally be kept synchronized.

For the physical HQ-over-tunnel test, use
[`DEPLOY-INSTRUCTIONS.md`](DEPLOY-INSTRUCTIONS.md). It provides the exact
Before / During / After commands for this repository's setup and diagnostic
collection.

## Requirements

### Hardware and operating system

- Raspberry Pi OS on each endpoint.
- One RTL8812AU-compatible USB adapter on the GS.
- Two RTL8812AU-compatible USB adapters on the drone.
- Appropriate antennas for the configured adapters. No power amplifier is used in this deployment.
- Sufficient USB power. The code caps the driver power index at 56, which is 90% of the driver scale of 63. The cap is intended to reduce Raspberry Pi USB brownouts; it is not a dBm value.
- Matching radio channel and regulatory region on both endpoints. The defaults are channel 13 / 2472 MHz and region `PL`.

### Software

The installer expects a Debian/Raspberry Pi environment with privileges to install packages and write system configuration. It uses wfb-ng and its tools, the project RTL driver (`rtl88xxau_wfb`, tag `v5.2.20`), systemd, udev, `iw`, `rfkill`, `lsusb`, `ip`, `dmesg`, and standard Python 3 modules. The diagnostics also inspect wfb-ng's local statistics API and running processes.

The terminal interface uses Python `curses`. The log viewer uses the standard-library `tkinter` module and does not require third-party plotting packages.

## Opt-in physical-test diagnostics

`physical_test_diagnostics.py` is a standalone standard-library Python CLI for
collecting evidence during a physical test. It is not called by `gs.py`,
`drone.py`, or their autostart units, and it does not change WFB configuration,
services, adapters, or radio settings.

Supply run metadata explicitly and choose a writable output directory:

```bash
sudo python3 physical_test_diagnostics.py \
  --run-id flight-20260926-01 --role drone \
  --metadata operator=example --metadata location=test-field \
  --output-dir ./physical-test-evidence \
  --sample-count 60 --sample-interval 1
```

The collector writes matching JSON, CSV, and JSONL files containing the supplied
run metadata, WFB systemd state, NIC/driver identity, and a SHA-256 of the WFB
configuration (never its contents). It also attempts the local WFB statistics
API on ports 8002 and 8003, or ports supplied with `--api-port`. `--sample-count`
and `--sample-interval` collect a timestamped raw WFB API series so an operator
can compare the same workload across runs. API metrics are recorded only when
returned by the API; it never invents RSSI, SNR, PER, or BER.
Unavailable sources are explicitly marked `not_recorded` with their sanitized
failure messages. Assignment-style secrets are redacted from saved text.

## Installation and first start

Copy the `consys` directory to the relevant Raspberry Pi. Run the role-specific script as root:

```bash
cd consys
sudo python3 gs.py       # ground station
sudo python3 drone.py    # drone
```

On a fresh Raspberry Pi OS installation, the script performs the setup required by its role, including package installation, driver handling, key/configuration creation, adapter naming, and systemd setup. Later starts open the configuration and verification TUI instead of repeating the full installation.

For unattended boot-time repair, the installer can be run in its systemd mode:

```bash
sudo python3 gs.py --autostart
sudo python3 drone.py --autostart
```

The first interactive setup creates an autostart unit that invokes the script after reboot. That unit repeats adapter discovery, driver binding, stable naming, role enforcement, and service checks before the normal wfb-ng service is expected to work.

## Stable adapter identity and roles

Linux's `wlan0`/`wlan1` names can change with enumeration order. The scripts create `/etc/udev/rules.d/70-wfb-names.rules` and bind names to adapter MAC addresses, with USB-port fallback for adapters with unreadable or duplicate MAC addresses.

The role mapping is:

| Endpoint | Required adapters | Role policy |
| --- | ---: | --- |
| GS | 1 | `gs_wfb` is `txrx`; it carries both received video and outgoing tunnel traffic. |
| Drone | 2 | `drone_TX` is `tx`; `drone_RX` is `rx` only. |

The TUI can assign the physical adapters to these logical names. The assignment follows the adapter when it is moved to another USB port. `/etc/wfb-cards.json` records the last known MAC, role, USB location, and timestamp so that a missing adapter can still be identified after it has been unplugged.

On the drone, `/etc/udev/rules.d/71-wfb-hotplug.rules` and the `--nic-hotplug` path keep `WFB_NICS` synchronized with the adapters that are actually present. If one adapter disappears, the remaining adapter can continue operating instead of wfb-ng failing because a configured interface is missing.

## Files generated on the Raspberry Pi

The scripts manage or inspect the following paths:

- `/etc/wifibroadcast.cfg`: wfb-ng configuration, updated surgically so unrelated settings are preserved.
- `/etc/default/wifibroadcast`: adapter list and service defaults.
- `/etc/modprobe.d/wfb.conf`: competing-driver blacklists and the transmit-power override.
- `/etc/drone.key` and `/etc/gs.key`: wfb-ng key files.
- `/etc/udev/rules.d/70-wfb-names.rules`: persistent adapter names.
- `/etc/udev/rules.d/71-wfb-hotplug.rules`: drone adapter hotplug recovery.
- `/etc/wfb-cards.json`: adapter inventory and last-seen information.
- `/etc/systemd/system/wfb-gs-autostart.service` or `wfb-drone-autostart.service`: boot-time repair unit.
- `test-<role>-*.log` beside the script: link-test recordings.

A one-time `.bak` backup is made before the program first changes the wfb-ng configuration.

## TUI capabilities

The curses TUI is both a configurator and a verifier. Depending on the screen and current wfb-ng state, it can:

- show physical USB presence, named interfaces, MAC addresses, USB bus/port and USB speed;
- detect a missing adapter and identify its previous logical name and port;
- check that the expected driver is built, loaded, and bound instead of a competing RTL driver;
- check monitor mode, channel, region, service state, service logs, and actual packet counters;
- inspect the effective wfb-ng configuration after master/site/project files are merged;
- change channel, region, transmit power, modulation/MCS, and FEC settings;
- apply channel and power changes live where the driver supports it;
- inspect per-antenna RSSI/SNR, packet rates, MCS, PHY rate, transmit injection, drops, and delay when supplied by the live WFB-NG API;
- test the peer through the wfb tunnel with ping and an SSH connection check;
- generate a realistic UDP video load through the same wfb-ng video path;
- run the link test, add time markers, and write samples and a summary to a log;
- start or inspect the background test recorder, which can continue after the visible TUI closes;
- pair the endpoints using an 8-character code and generate a new key pair.

The statistics reader handles wfb-ng's length-prefixed msgpack TCP API and tolerates version differences such as byte versus string keys. It also reads the actual command lines of running `wfb_tx` processes, because a value in a configuration file may not be active until the service is restarted.

## Link metrics, FEC, and automation

The link test distinguishes radio loss from loss remaining after forward error correction (FEC):

- **Loss before FEC** = `(lost + fec_rec) / (all + lost)`.
- **Loss after FEC** = `lost / (all + lost)`.
- **Recovered traffic** is the difference between those two values.

The FEC ladder ranges from no redundancy (`1/1`) through increasingly strong redundancy, up to `1/5`. Stronger FEC can recover more losses but consumes more airtime. The automatic FEC controller raises redundancy after sustained poor loss and removes it only after sustained good conditions; it does not automatically go all the way to no protection.

The automatic channel controller waits for sustained poor loss before changing channel, coordinates the change with the peer, and waits for the link to settle. Both automation systems have cooldowns and stale-peer handling because every configuration change can restart wfb-ng.

The video load generator sends timestamped UDP packets to the local video input port at a configured bit rate. This is more representative than a ping-only test because it exercises the video stream's FEC, modulation, packet size, and radio path.

## Video and tunnel ports

The role-specific wfb-ng configuration includes:

- Drone video input: `0.0.0.0:5602`.
- GS video output: `127.0.0.1:5600`.
- Local MAVLink endpoint on the GS: `127.0.0.1:14550`.
- Peer tunnel addresses: drone `10.5.0.1`, GS `10.5.0.2`.
- SSH reachability checks use TCP port 22.

The scripts deliberately test the radio path through the tunnel rather than an Ethernet interface, so the reported ping and video-load results describe the link being evaluated.

### Confirmed application tunnel path

The ALFA `system` branch sends its selected HQ stream as ordinary UDP to
ground tunnel peer `10.5.0.1:5000`. The WFB IP tunnel transports that traffic
and the ground application receives it on UDP `5000`. No external relay or
forwarding rule is required. LQ and COMBINED remain supported by the
application but are not selected for this first hardware test. The
`5602/5600` direct-video service is separate. Follow the exact commands in
[`DEPLOY-INSTRUCTIONS.md`](DEPLOY-INSTRUCTIONS.md).

## Access-point range test

`ap_test.py` temporarily stops the relevant `wifibroadcast@<role>.service`, configures the normal wfb adapter as an open 2.4 GHz access point, starts `hostapd` and `dnsmasq`, and assigns `192.168.50.1/24`. A phone can connect and use its own signal indicator while walking the intended coverage area.

Run it as root on either endpoint:

```bash
sudo python3 ap_test.py
sudo python3 ap_test.py MyRangeTest 13 40
```

The optional arguments are SSID, channel, and driver power-index scale. The script detects `gs_wfb` or `drone_TX`; on the drone it tests the adapter that normally transmits video. Arrow keys or `+`/`-` change power live. Ctrl+C, `q`, process termination, and most failures go through cleanup: hostapd/dnsmasq are stopped, the IP address is removed, the previous power is restored, and the wfb-ng service is started again.

This is a range test, not an operational mode. The wireless link is intentionally down while the access point is active.

## Viewing recorded tests on Windows

The TUI records semicolon-separated text logs containing a header, samples, in-test comments/markers, and a summary. Open the newest or a selected log with:

```text
python consys/podglad_testu.py path\to\test-gs-....log
```

The viewer plots each antenna's RSSI, SNR, modulation, loss before and after FEC, recovered-packet rate, cumulative packet-error rate, and ping. It marks missing-data gaps instead of drawing misleading lines across them. Markers entered during the test are red vertical lines; ordinary comments are grey event lines. Multiple logs can be compared from the viewer, with each test normalized to its own start time. Mouse-wheel zoom, drag/pan, Home/double-click reset, and Ctrl+wheel vertical scrolling are supported.

## Security and operational warnings

The two Python scripts contain a matching, base64-encoded default key pair so that a fresh pair can communicate without manually copying keys. Those embedded values are **not secret**: anyone who obtains the scripts can decrypt or inject traffic using them. Use the pairing/new-key menu before treating the link as private, and protect the generated key files.

The scripts make privileged changes to kernel modules, udev, systemd, network interfaces, and `/etc`. Do not run them on a production machine with unrelated wireless adapters without reviewing the generated configuration. Channel 13 and the power policy are defaults for the Polish/ETSI assumptions encoded in the comments; verify local regulations and hardware support before changing the region or channel.

## Troubleshooting checklist

1. Verify the expected number of RTL adapters with `lsusb` and the TUI's USB count.
2. Check that the adapters are named `gs_wfb`, `drone_RX`, and `drone_TX` as appropriate.
3. Confirm the `rtl88xxau_wfb` driver is bound and competing drivers are not claiming the adapters.
4. Confirm both endpoints use the same channel, region, radio parameters, and compatible wfb-ng configuration.
5. Check `/etc/wfb-cards.json` and the USB speed; a high-throughput adapter in a USB 2.0 path can lose packets.
6. Inspect `systemctl status wifibroadcast@gs.service` or `wifibroadcast@drone.service` and the recent journal output.
7. Compare per-antenna counters, FEC-before/FEC-after loss, and the realistic video-load test rather than relying on service state alone.
8. If the link fails after a reboot, check the role-specific `wfb-*-autostart.service` and allow its adapter repair pass to finish.

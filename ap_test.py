#!/usr/bin/env python3
"""Temporary access point on a WFB adapter for a real phone range test.

Instead of reading RSSI from the wfb-ng API (see web_rssi.py), this script
temporarily turns the adapter into a standard open Wi-Fi access point. Connect
to it with a phone as to any other network and check signal bars while walking
the area, without involving wfb-ng.

During the test, a curses status screen shows the SSID, channel, adapter, and
current TX power. Up/Down (or +/-) changes the power live without interrupting
the test, using the same driver scale as the "Transmit power (TX)" screen in
drone.py/gs.py.

It works on both the GS (the gs_wfb adapter) and the drone (the drone_TX
adapter, which normally transmits video to the ground, so its range is the one
that matters). The role is detected automatically from the adapter physically
present in the system.

The tested adapter is normally used by the link. The corresponding
wifibroadcast@... service is stopped for the test, so the link is down. After
Ctrl+C the script cleans up hostapd, dnsmasq, and the IP address, then starts
the service again. Cleanup also runs after an in-test failure (for example an
ip or hostapd error), ensuring the service is not left permanently stopped.

Root is required (hostapd, ip, dnsmasq, systemctl).

Usage:
    sudo python3 ap_test.py [ssid] [channel] [tx_power]
    # Default: SSID "RTL_test", channel 13—the channel normally used by the
    # drone link, so the test result is comparable with that link.
    # tx_power: optionally forces transmit power BEFORE starting the AP, for
    # comparing range at different levels. It is 0-TX_POWER_CAP on the driver
    # scale (see mod.apply_tx_power_live below), NOT dBm. Ctrl+C restores the
    # previous value. Without this argument the adapter keeps its current power.
"""

import curses
import importlib
import os
import signal
import subprocess
import sys
import tempfile
import time

AP_CIDR = "192.168.50.1/24"
DHCP_RANGE = "192.168.50.10,192.168.50.100,12h"

# (adapter name, wifibroadcast service to stop for the test), in detection
# order. The GS has one txrx adapter; the drone uses drone_TX because it
# normally transmits video to the ground and its range is what matters.
ROLE_CANDIDATES = [
    ("gs", "gs_wfb", "wifibroadcast@gs.service"),
    ("drone", "drone_TX", "wifibroadcast@drone.service"),
]

HOSTAPD_CONF = """\
interface={iface}
driver=nl80211
ssid={ssid}
hw_mode=g
channel={channel}
country_code=PL
ieee80211n=1
wmm_enabled=1
"""


def sh(*cmd, check=True):
    print("+", " ".join(cmd))
    r = subprocess.run(cmd)
    if check and r.returncode != 0:
        raise RuntimeError(f"command failed: {' '.join(cmd)}")


def require_root():
    if os.geteuid() != 0:
        print(f"Run as root: sudo python3 {os.path.basename(sys.argv[0])}")
        sys.exit(1)


def iface_exists(name):
    r = subprocess.run(
        ["ip", "link", "show", name],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return r.returncode == 0


def detect_role():
    """Return the imported role module, which owns working 88XXau_wfb TX-power
    logic (see main: apply_tx_power_live). For this driver, `iw ... set txpower`
    does not change hardware: cfg80211 only stores the value and reports success."""
    for role, iface, service in ROLE_CANDIDATES:
        if iface_exists(iface):
            return role, iface, service, importlib.import_module(role)
    known = ", ".join(iface for _, iface, _ in ROLE_CANDIDATES)
    print(f"No known WFB adapter found ({known}); a connected and named WFB "
          "adapter is required for the AP test.")
    sys.exit(1)


def main():
    require_root()
    role, iface, service, mod = detect_role()

    ssid = sys.argv[1] if len(sys.argv) > 1 else "RTL_test"
    channel = sys.argv[2] if len(sys.argv) > 2 else "13"
    tx_power = sys.argv[3] if len(sys.argv) > 3 else None

    if tx_power is not None and not (tx_power.isdigit() and 0 <= int(tx_power) <= mod.TX_POWER_CAP):
        print(f"tx_power must be 0-{mod.TX_POWER_CAP} (88XXau_wfb driver scale; "
              f"0 = EEPROM calibration, {mod.TX_POWER_CAP} = 90% cap of {mod.TX_POWER_MAX}).")
        sys.exit(1)

    state = {"dnsmasq": None, "hostapd": None, "service_stopped": False,
             "orig_tx_power": None, "done": False, "stop": False}

    def do_cleanup():
        if state["done"]:
            return
        state["done"] = True
        print("\nCleaning up...")
        for key in ("hostapd", "dnsmasq"):
            p = state[key]
            if p and p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()
        sh("ip", "addr", "flush", "dev", iface, check=False)
        if state["orig_tx_power"] is not None:
            print(f"Restoring TX power to {state['orig_tx_power']}...")
            mod.apply_tx_power_live(state["orig_tx_power"])
        if state["service_stopped"]:
            print(f"Restoring {service}...")
            sh("systemctl", "start", service, check=False)

    def signal_stop(*_):
        # Only set a flag: do_cleanup() prints and starts processes, which would
        # corrupt the curses status screen. The loop exits before cleanup runs.
        state["stop"] = True

    signal.signal(signal.SIGINT, signal_stop)
    signal.signal(signal.SIGTERM, signal_stop)

    try:
        print(f"Detected role: {role} (adapter {iface}).")
        print(f"Stopping {service} (adapter {iface} is needed by the AP; the link will be down)...")
        sh("systemctl", "stop", service)
        state["service_stopped"] = True
        time.sleep(1)

        sh("ip", "link", "set", iface, "down", check=False)
        sh("ip", "addr", "flush", "dev", iface, check=False)
        sh("ip", "addr", "add", AP_CIDR, "dev", iface)
        sh("ip", "link", "set", iface, "up")

        # Save pre-test power regardless of tx_power so status-screen +/- can
        # also be restored, not only a forced initial value.
        state["orig_tx_power"] = mod.read_tx_power_live()
        if tx_power is not None:
            print(f"Setting TX power to {tx_power}/{mod.TX_POWER_CAP} "
                  f"(live through {mod.TX_POWER_SYSFS})...")
            if not mod.apply_tx_power_live(tx_power):
                raise RuntimeError(
                    f"could not set TX power: {mod.TX_POWER_SYSFS} is missing "
                    "(88XXau_wfb module not loaded?)")

        conf_path = tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False).name
        with open(conf_path, "w") as f:
            f.write(HOSTAPD_CONF.format(iface=iface, ssid=ssid, channel=channel))

        print(f"Starting dnsmasq (DHCP {DHCP_RANGE})...")
        state["dnsmasq"] = subprocess.Popen([
            "dnsmasq", "--no-daemon", "--port=0",
            f"--interface={iface}", "--bind-interfaces",
            f"--dhcp-range={DHCP_RANGE}",
        ])

        state["hostapd"] = subprocess.Popen(["hostapd", conf_path])
        time.sleep(0.3)  # allow an immediate failure (bad channel/configuration)
        if state["hostapd"].poll() is not None:
            raise RuntimeError("hostapd did not start (bad channel or unsupported adapter)")

        def status_screen(stdscr):
            """Curses test status with live TX-power changes (Up/Down, +/-).
            It refreshes every 300 ms so displayed power reflects sysfs, rather
            than merely the last value written by this script."""
            curses.curs_set(0)
            if curses.has_colors():
                mod.init_colors()
            stdscr.timeout(300)

            while not state["stop"] and state["hostapd"].poll() is None:
                live = mod.read_tx_power_live()
                stdscr.erase()
                mod.draw_header(stdscr, f"Test AP [{role}] - {iface}")
                mod.safe_addstr(stdscr, 2, 2,
                                 f"SSID: {ssid}   channel: {channel}   adapter: {iface} ({role})")
                if live is not None:
                    mod.safe_addstr(stdscr, 4, 2,
                                     f"TX power: {live}/{mod.TX_POWER_CAP}   "
                                     "(Up/Down or +/- changes it live)")
                else:
                    mod.safe_addstr(stdscr, 4, 2,
                                     "TX power: unavailable (driver sysfs missing)",
                                     mod.color_for("warn"))
                mod.safe_addstr(stdscr, 6, 2,
                                 "Connect a phone to this open network and check range "
                                 "while walking the area.")
                mod.safe_addstr(stdscr, 8, 2,
                                 "q / Ctrl+C = end test and restore the link", curses.A_DIM)
                stdscr.refresh()

                c = stdscr.getch()
                if c in (ord("q"), ord("Q"), 27):
                    return
                if live is None or c == -1:
                    continue
                cur = int(live)
                if c in (curses.KEY_UP, ord("+"), ord("=")):
                    new = min(cur + 1, mod.TX_POWER_CAP)
                elif c in (curses.KEY_DOWN, ord("-"), ord("_")):
                    new = max(cur - 1, 0)
                else:
                    continue
                if new != cur:
                    mod.apply_tx_power_live(str(new))

        curses.wrapper(status_screen)

        if state["hostapd"].poll() is not None and not state["stop"]:
            print("hostapd failed during the test; check configuration/logs (journalctl).")
    finally:
        do_cleanup()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Testowy Access Point na karcie gs_wfb - realny test zasiegu telefonem.

Zamiast czytac RSSI z API wfb-ng (patrz web_rssi.py), ten skrypt na chwile
zamienia karte w zwykly, otwarty punkt dostepu WiFi. Laczysz sie z nim
telefonem tak jak z kazda inna siecia i sprawdzasz zasieg paskami sygnalu
w telefonie, chodzac po terenie - bez posrednictwa wfb-ng.

Karta jest jedna i ta sama, ktora normalnie obsluguje link do drona - na
czas testu usluga wifibroadcast@gs jest zatrzymana (link stoi). Po Ctrl+C
skrypt sam sprzata (hostapd, dnsmasq, adres IP) i z powrotem odpala usluge,
wiec karta wraca do normalnej roli bez recznej naprawy.

Wymaga roota (hostapd, ip, dnsmasq, systemctl).

Uzycie:
    sudo python3 ap_test.py [ssid] [kanal]
    # domyslnie: SSID "RTL_test", kanal 13 - ten sam, na ktorym normalnie
    # chodzi link do drona, wiec wynik testu jest z nim porownywalny.
"""

import signal
import subprocess
import sys
import tempfile
import time

import gs

IFACE = "gs_wfb"
AP_CIDR = "192.168.50.1/24"
DHCP_RANGE = "192.168.50.10,192.168.50.100,12h"
SERVICE = "wifibroadcast@gs.service"

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
        raise RuntimeError(f"polecenie nie powiodlo sie: {' '.join(cmd)}")


def main():
    gs.require_root()

    ssid = sys.argv[1] if len(sys.argv) > 1 else "RTL_test"
    channel = sys.argv[2] if len(sys.argv) > 2 else "13"

    state = {"dnsmasq": None, "hostapd": None, "service_stopped": False, "done": False}

    def cleanup(*_):
        if state["done"]:
            return
        state["done"] = True
        print("\nSprzatanie...")
        for key in ("hostapd", "dnsmasq"):
            p = state[key]
            if p and p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()
        sh("ip", "addr", "flush", "dev", IFACE, check=False)
        if state["service_stopped"]:
            print(f"Przywracam {SERVICE}...")
            sh("systemctl", "start", SERVICE, check=False)
        sys.exit(0)

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    print(f"Zatrzymuje {SERVICE} (karta {IFACE} potrzebna dla AP, link do drona stanie)...")
    sh("systemctl", "stop", SERVICE)
    state["service_stopped"] = True
    time.sleep(1)

    sh("ip", "link", "set", IFACE, "down", check=False)
    sh("ip", "addr", "flush", "dev", IFACE, check=False)
    sh("ip", "addr", "add", AP_CIDR, "dev", IFACE)
    sh("ip", "link", "set", IFACE, "up")

    conf_path = tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False).name
    with open(conf_path, "w") as f:
        f.write(HOSTAPD_CONF.format(iface=IFACE, ssid=ssid, channel=channel))

    print(f"Startuje dnsmasq (DHCP {DHCP_RANGE})...")
    state["dnsmasq"] = subprocess.Popen([
        "dnsmasq", "--no-daemon", "--port=0",
        f"--interface={IFACE}", "--bind-interfaces",
        f"--dhcp-range={DHCP_RANGE}",
    ])

    print(f"\nSSID: {ssid}  (otwarta siec, kanal {channel})")
    print("Polacz sie telefonem z ta siecia i sprawdz pasek sygnalu / dBm chodzac po terenie.")
    print("Ctrl+C konczy test i przywraca link do drona.\n")

    state["hostapd"] = subprocess.Popen(["hostapd", conf_path])
    state["hostapd"].wait()
    cleanup()


if __name__ == "__main__":
    main()

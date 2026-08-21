#!/usr/bin/env python3
"""Testowy Access Point na karcie wfb - realny test zasiegu telefonem.

Zamiast czytac RSSI z API wfb-ng (patrz web_rssi.py), ten skrypt na chwile
zamienia karte w zwykly, otwarty punkt dostepu WiFi. Laczysz sie z nim
telefonem tak jak z kazda inna siecia i sprawdzasz zasieg paskami sygnalu
w telefonie, chodzac po terenie - bez posrednictwa wfb-ng.

W trakcie testu dziala pseudograficzny (curses) ekran statusu - SSID, kanal,
karta i biezaca moc TX - z mozliwoscia zmiany mocy na zywo strzalkami
Gora/Dol (albo +/-) bez przerywania testu, w tej samej skali sterownika co
ekran "Moc nadawania (TX)" w drone.py/gs.py.

Dziala zarowno na gs (karta gs_wfb), jak i na dronie (karta drone_TX - to ona
normalnie nadaje wideo do ziemi, wiec jej zasieg jest tym, co realnie
interesuje). Rola wykrywana jest automatycznie po tym, ktora karta fizycznie
istnieje w systemie.

Karta jest jedna i ta sama, ktora normalnie obsluguje link - na czas testu
odpowiednia usluga wifibroadcast@... jest zatrzymana (link stoi). Po Ctrl+C
skrypt sam sprzata (hostapd, dnsmasq, adres IP) i z powrotem odpala usluge,
wiec karta wraca do normalnej roli bez recznej naprawy. Sprzatanie odpala sie
rowniez wtedy, gdy skrypt padnie w trakcie (np. blad ip/hostapd) - nie tylko
po Ctrl+C - zeby usluga nigdy nie zostala trwale zatrzymana.

Wymaga roota (hostapd, ip, dnsmasq, systemctl).

Uzycie:
    sudo python3 ap_test.py [ssid] [kanal] [moc_tx]
    # domyslnie: SSID "RTL_test", kanal 13 - ten sam, na ktorym normalnie
    # chodzi link do drona, wiec wynik testu jest z nim porownywalny.
    # moc_tx: opcjonalnie wymusza moc nadawania PRZED startem AP, np. do
    # porownania zasiegu przy roznej mocy - liczba 0-TX_POWER_CAP w skali
    # sterownika (patrz mod.apply_tx_power_live nizej), NIE dBm. Po Ctrl+C
    # wraca do wartosci sprzed testu. Bez tego argumentu karta zostaje na
    # mocy, jaka akurat ma ustawiona.
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

# (nazwa karty, usluga wifibroadcast do zatrzymania na czas testu) - w kolejnosci
# sprawdzania. gs ma jedna karte txrx; na dronie testujemy drone_TX, bo to ona
# normalnie nadaje wideo do ziemi i jej zasieg jest tym, co ma znaczenie.
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
        raise RuntimeError(f"polecenie nie powiodlo sie: {' '.join(cmd)}")


def require_root():
    if os.geteuid() != 0:
        print(f"Uruchom jako root: sudo python3 {os.path.basename(sys.argv[0])}")
        sys.exit(1)


def iface_exists(name):
    r = subprocess.run(
        ["ip", "link", "show", name],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return r.returncode == 0


def detect_role():
    """Zwraca tez zaimportowany modul (gs albo drone) - to on ma prawdziwa,
    dzialajaca logike mocy TX dla karty 88XXau_wfb (patrz main: apply_tx_power_live).
    'iw ... set txpower' dla tego sterownika NIC nie zmienia w sprzecie - cfg80211
    tylko zapamietuje wartosc i zglasza sukces, wiec liczylby sie tu tylko pozornie."""
    for role, iface, service in ROLE_CANDIDATES:
        if iface_exists(iface):
            return role, iface, service, importlib.import_module(role)
    known = ", ".join(iface for _, iface, _ in ROLE_CANDIDATES)
    print(f"Nie znaleziono zadnej znanej karty wfb ({known}) - "
          "podlaczona/nazwana karta wfb jest wymagana do testu AP.")
    sys.exit(1)


def main():
    require_root()
    role, iface, service, mod = detect_role()

    ssid = sys.argv[1] if len(sys.argv) > 1 else "RTL_test"
    channel = sys.argv[2] if len(sys.argv) > 2 else "13"
    tx_power = sys.argv[3] if len(sys.argv) > 3 else None

    if tx_power is not None and not (tx_power.isdigit() and 0 <= int(tx_power) <= mod.TX_POWER_CAP):
        print(f"moc_tx musi byc liczba 0-{mod.TX_POWER_CAP} (skala sterownika 88XXau_wfb, "
              f"0 = kalibracja EEPROM, {mod.TX_POWER_CAP} = pulap 90% z {mod.TX_POWER_MAX}).")
        sys.exit(1)

    state = {"dnsmasq": None, "hostapd": None, "service_stopped": False,
             "orig_tx_power": None, "done": False, "stop": False}

    def do_cleanup():
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
        sh("ip", "addr", "flush", "dev", iface, check=False)
        if state["orig_tx_power"] is not None:
            print(f"Przywracam moc TX na {state['orig_tx_power']}...")
            mod.apply_tx_power_live(state["orig_tx_power"])
        if state["service_stopped"]:
            print(f"Przywracam {service}...")
            sh("systemctl", "start", service, check=False)

    def signal_stop(*_):
        # Tylko flaga - do_cleanup() drukuje i odpala procesy, co w trakcie
        # dzialania curses (ekran statusu) rozjechaloby ekran. Petla curses
        # sama sprawdza state["stop"] i konczy sie, zanim leci sprzatanie.
        state["stop"] = True

    signal.signal(signal.SIGINT, signal_stop)
    signal.signal(signal.SIGTERM, signal_stop)

    try:
        print(f"Wykryto role: {role} (karta {iface}).")
        print(f"Zatrzymuje {service} (karta {iface} potrzebna dla AP, link stanie)...")
        sh("systemctl", "stop", service)
        state["service_stopped"] = True
        time.sleep(1)

        sh("ip", "link", "set", iface, "down", check=False)
        sh("ip", "addr", "flush", "dev", iface, check=False)
        sh("ip", "addr", "add", AP_CIDR, "dev", iface)
        sh("ip", "link", "set", iface, "up")

        # Zapamietaj moc SPRZED testu niezaleznie od tx_power - +/- na ekranie
        # statusu tez ma sie dac cofnac, nie tylko wymuszona wartosc startowa.
        state["orig_tx_power"] = mod.read_tx_power_live()
        if tx_power is not None:
            print(f"Ustawiam moc TX na {tx_power}/{mod.TX_POWER_CAP} "
                  f"(live przez {mod.TX_POWER_SYSFS})...")
            if not mod.apply_tx_power_live(tx_power):
                raise RuntimeError(
                    f"nie udalo sie ustawic mocy TX - brak {mod.TX_POWER_SYSFS} "
                    "(modul 88XXau_wfb niezaladowany?)")

        conf_path = tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False).name
        with open(conf_path, "w") as f:
            f.write(HOSTAPD_CONF.format(iface=iface, ssid=ssid, channel=channel))

        print(f"Startuje dnsmasq (DHCP {DHCP_RANGE})...")
        state["dnsmasq"] = subprocess.Popen([
            "dnsmasq", "--no-daemon", "--port=0",
            f"--interface={iface}", "--bind-interfaces",
            f"--dhcp-range={DHCP_RANGE}",
        ])

        state["hostapd"] = subprocess.Popen(["hostapd", conf_path])
        time.sleep(0.3)  # czas na ewentualny natychmiastowy krach (zly kanal/config)
        if state["hostapd"].poll() is not None:
            raise RuntimeError("hostapd nie wystartowal (zly kanal albo karta go nie obsluguje)")

        def status_screen(stdscr):
            """Pseudograficzny status testu + zywa zmiana mocy TX (Gora/Dol, +/-).
            Petla, nie jednorazowy ekran - odswieza sie co 300 ms, zeby moc
            pokazywana na ekranie zawsze zgadzala sie z tym, co faktycznie
            siedzi w sysfs (a nie tylko z tym, co skrypt tam ostatnio wpisal)."""
            curses.curs_set(0)
            if curses.has_colors():
                mod.init_colors()
            stdscr.timeout(300)

            while not state["stop"] and state["hostapd"].poll() is None:
                live = mod.read_tx_power_live()
                stdscr.erase()
                mod.draw_header(stdscr, f"Test AP [{role}] - {iface}")
                mod.safe_addstr(stdscr, 2, 2,
                                 f"SSID: {ssid}   kanal: {channel}   karta: {iface} ({role})")
                if live is not None:
                    mod.safe_addstr(stdscr, 4, 2,
                                     f"Moc TX: {live}/{mod.TX_POWER_CAP}   "
                                     "(Gora/Dol albo +/- zmienia na zywo)")
                else:
                    mod.safe_addstr(stdscr, 4, 2,
                                     "Moc TX: niedostepna (brak sysfs sterownika)",
                                     mod.color_for("warn"))
                mod.safe_addstr(stdscr, 6, 2,
                                 "Polacz sie telefonem z ta siecia (otwarta) i sprawdz "
                                 "zasieg chodzac po terenie.")
                mod.safe_addstr(stdscr, 8, 2,
                                 "q / Ctrl+C = zakoncz test i przywroc link", curses.A_DIM)
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
            print("hostapd padl w trakcie testu - sprawdz konfiguracje/logi (journalctl).")
    finally:
        do_cleanup()


if __name__ == "__main__":
    main()

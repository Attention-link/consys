#!/usr/bin/env python3
"""WFB-NG - instalator + pseudo-graficzny (curses) TUI dla drona i gs.

drone.py i gs.py to TEN SAM kod - rozni je tylko linia ROLE = "..." ponizej,
a wszystko, co zalezy od strony (adres drugiej strony, porty wideo, sekcja
configu, nazwy kart), liczy sie z niej. Poprawka w jednym pliku = skopiowac
plik i podmienic ROLE, zeby obie strony zawsze zachowywaly sie identycznie.

Pierwsze uruchomienie (na swiezym Raspberry Pi OS, z podlaczona karta
RTL8812AU) robi caly setup: pakiety systemowe, sterownik karty, klucze
szyfrujace, /etc/wifibroadcast.cfg, usluge systemd. Kolejne uruchomienia
(setup juz gotowy) od razu otwieraja konfigurator/weryfikator.

Obie strony maja domyslnie JEDNA karte, ktora nadaje i odbiera (<rola>_TXRX)
- EXPECTED_NICS to minimum, wpiac mozna dowolnie wiecej. Wfb-ng odbiera ze
wszystkich kart zwroconych przez wfb-nics (dywersyfikacja - wygrywa ta
z lepszym sygnalem), a nadaje przez te z rola nadawcza. Kazdy start sprawdza, czy karty faktycznie sa widoczne,
przepiete pod nasz sterownik i przepuszczaja ruch.

Karty dostaja stale nazwy zamiast wlanX - przypiete regula udev do MAC-a
karty, wiec ta sama karta ma zawsze te sama nazwe, niezaleznie od portu USB.
Nazwa niesie role: <rola>_TXRX robi oba kierunki (tak startuje jedyna karta),
<rola>_TX nadaje, <rola>_RX tylko slucha (wpis wifi_txpower = 'off' w configu);
kolejne karty tej samej roli dostaja numer (drone_RX2, gs_RX2...). Role KAZDEJ karty ustawia sie przelacznikiem na ekranie
"Karty na zywo" (tam tez chip i urzadzenie kazdej karty), bo przy
jednokierunkowym wzmacniaczu nadawac ma konkretna karta.

Menu pokazuje tez, w ktorym gniezdzie USB siedzi kazda karta, a po wypieciu
dongla mowi, KTORA karta zniknela i z ktorego gniazda - z ewidencji w
/etc/wfb-cards.json, bo nieobecnej karty nie ma juz o co zapytac.

Klucze szyfrujace sa wbudowane w oba skrypty (identyczne), wiec link wstaje
od razu, bez przenoszenia plikow. W menu jest parowanie: jedna strona pokazuje
8-znakowy kod, na drugiej sie go wpisuje i obie licza z niego te sama, prywatna
pare kluczy.

Pierwsze uruchomienie wpisuje skrypt do autostartu (wfb-<rola>-autostart.service),
wiec po kazdym reboocie powtarza sie to samo wykrywanie i te same naprawy kart -
bez wchodzenia na Pi. Weryfikacja pokazuje, czy ten autostart jest wlaczony.

Uzycie:
    sudo python3 drone.py              # (albo gs.py) setup + konfigurator/weryfikator
    sudo python3 drone.py --autostart  # tryb dla systemd: same naprawy, bez menu
"""

import ast
import base64
import curses
import hashlib
import io
import json
import os
import re
import secrets
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

ROLE = "drone"  # JEDYNA roznica miedzy drone.py i gs.py - reszta liczy sie z niej

if ROLE not in ("drone", "gs"):
    raise SystemExit(f"nieznana rola {ROLE!r} - dozwolone: drone, gs")
IS_DRONE = ROLE == "drone"
PEER_NAME = "gs" if IS_DRONE else "drone"
PEER_IP = "10.5.0.1" if IS_DRONE else "10.5.0.2"  # adres drugiej strony w tunelu
SSH_PORT = 22

EXPECTED_NICS = 1  # MINIMUM kart po KAZDEJ stronie (jedna TX+RX); wiecej wolno - role w menu

DRIVER_TAG = "v5.2.20"
APT_RELEASE = "master"
# Link ma chodzic na 2.4 GHz. Kanal 13 (2472 MHz) - najwyzszy dozwolony w PL
# i w calym ETSI, zwykle mniej zatloczony niz standardowe 1/6/11. Region PL
# obejmuje 2400-2483, wiec kanaly 1-13 sa w nim legalne; pasma 5 GHz nie
# ruszamy (kanal 161 = 5805 MHz w PL w ogole nie istnieje i karta na nim nie
# nadaje). Zmiana obu wartosci jest w menu i MUSI byc taka sama na dronie i gs.
DEFAULT_CHANNEL = "13"
DEFAULT_REGION = "PL"
TX_POWER_MAX = 63  # skala sterownika: 0 = wylaczone (kalibracja z EEPROM), 63 = max

# Gorny pulap mocy: 90% skali, czyli 56. Powyzej tego nie pozwalamy ustawic.
# 8812AU przy pelnej mocy wyrywa z portu USB tyle pradu, ze Pi 5 z budzetem
# 600 mA na wszystkie porty potrafi sie po prostu wylaczyc - a impulsy TX to
# dokladnie ten moment, w ktorym zasilanie siada. Ostatnie 10% skali daje
# ulamek dB zasiegu i kosztuje najwiecej pradu, wiec to najtanszy z mozliwych
# kompromisow. Floor, a nie zaokraglenie: 90% ma byc SUFITEM, nie celem.
TX_POWER_CAP = int(TX_POWER_MAX * 0.9)
DEFAULT_TX_POWER = str(TX_POWER_CAP)


def clamp_tx_power(value):
    """Moc przyciety do pulapu. Wolane przy KAZDYM zapisie i kazdym ustawieniu
    na zywo, bo wartosc wchodzi tu z trzech stron (menu, modprobe.d z poprzedniej
    instalacji, stala domyslna) i pulap ma obowiazywac niezaleznie od drogi.
    Zero zostawiamy nietkniete - to nie moc, tylko 'uzyj kalibracji EEPROM'."""
    try:
        num = int(str(value).strip())
    except (TypeError, ValueError):
        return DEFAULT_TX_POWER
    if num <= 0:
        return "0"
    return str(min(num, TX_POWER_CAP))

MODPROBE_WFB = Path("/etc/modprobe.d/wfb.conf")
TX_POWER_SYSFS = Path("/sys/module/88XXau_wfb/parameters/rtw_tx_pwr_idx_override")

CFG_PATH = Path("/etc/wifibroadcast.cfg")
DRONE_KEY = Path("/etc/drone.key")
GS_KEY = Path("/etc/gs.key")
# Zapisy z ekranu "Test polaczenia" laduja obok skryptu - tam, gdzie uzytkownik
# go wgral i skad go uruchamia, wiec plik widac zwyklym 'ls' zaraz po wyjsciu
# z testu. Katalog skryptu, a nie biezacy, bo sudo bywa wolane z innego miejsca.
SCRIPT_PATH = Path(__file__).resolve()
TEST_LOG_DIR = SCRIPT_PATH.parent
REBOOT_MARKER = Path(f"/etc/.wfb-{ROLE}-reboot-attempted")

# Autostart: skrypt wpisuje sam siebie do systemd, zeby po KAZDYM restarcie Pi
# powtorzylo sie to, co robi uruchomienie z reki - przepiecie kart pod nasz
# sterownik, stale nazwy, rozdzial RX/TX i dopilnowanie, ze usluga faktycznie
# tych kart uzywa. Sama wifibroadcast@ tego nie robi, wiec bez tej jednostki
# link po zwyklym reboocie potrafi nie wstac, chociaz "usluga dziala".
AUTOSTART_UNIT_NAME = f"wfb-{ROLE}-autostart.service"
AUTOSTART_UNIT = Path("/etc/systemd/system") / AUTOSTART_UNIT_NAME
AUTOSTART_FLAG = "--autostart"

# Karty dostaja stale, czytelne nazwy zamiast wlanX (numer zalezy od kolejnosci
# wykrycia i potrafi sie zmienic miedzy bootami). Nazwa jest przypieta regula
# udev do MAC-a karty, wiec jedzie razem z donglem - takze po przelozeniu do
# innego portu USB - i niesie ROLE karty: <rola>_TX nadaje, <rola>_RX tylko
# odbiera, <rola>_TXRX robi oba; kolejne karty tej samej roli dostaja numer
# (drone_RX2...). Kart moze byc dowolnie duzo, a role kazdej
# zmienia sie w menu ("Karty na zywo", assign_nic_role) - patrz ROLE_TAGS
# i plan_nic_names. Sama nazwa nie wylacza nadawania - robi to dopiero wpis
# wifi_txpower = 'off' w configu (rx_only_nics, ensure_tx_split).
#
# Tu tylko uklad NA START: jakie role dostaja pierwsze wpiete karty (po kolei
# wg gniazda USB). Po obu stronach tak samo: jedna karta, ktora nadaje
# i odbiera. Kazda karta ponad ten uklad dostaje SPARE_NIC_ROLE (tylko
# odbior), zeby dongiel wpiety na probe nie zabral czesci nadawania. Starsze
# instalacje maja jeszcze nazwe gs_wfb - rozpoznajemy ja jako txrx
# (LEGACY_NIC_NAMES), a jedyna karte z rola rx po starym ukladzie drona
# ("rx", "tx") plan_nic_names sam przestawia na txrx.
DEFAULT_NIC_ROLES = ["txrx"]

# Strumien wideo idzie w JEDNA strone: dron -> gs. Dron wpycha go do wfb-ng na
# UDP 5602 ([drone_video] peer = 'listen://'), a gs oddaje odebrany strumien na
# UDP 5600 ([gs_video] peer = 'connect://'). Test obciazeniowy uzywa dokladnie
# tej samej drogi, wiec mierzy ten port radiowy, ten FEC i te modulacje, ktorymi
# naprawde poleci obraz - a nie tunel, ktory ma wlasne, inne ustawienia.
VIDEO_SENDS = IS_DRONE  # dron nadaje obraz; gs odbiera
VIDEO_UDP_PORT = 5602 if IS_DRONE else 5600

UDEV_NAMES = Path("/etc/udev/rules.d/70-wfb-names.rules")
WFB_DEFAULTS = Path("/etc/default/wifibroadcast")

# WFB_NICS w /etc/default/wifibroadcast wymienia karty na sztywno (np.
# "drone_RX drone_TX" ze starego ukladu albo "gs_wfb"). wfb-server odpala sie
# TYLKO gdy WSZYSTKIE wymienione tam karty istnieja - jak ktorejs zabraknie
# (wypiety dongiel, zmiana nazwy), caly proces odmawia startu bledem "Device
# not found" i milknie TAKZE karta, ktora nadal jest podpieta. Dotyczy to
# tak samo drona, jak i gs. Regula udev ponizej
# (patrz ensure_hotplug_rule, sync_wfb_nics) na kazde dodanie/usuniecie karty
# przepisuje WFB_NICS na to, co NAPRAWDE jest podpiete, i restartuje usluge -
# ocalala karta wraca do nadawania w kilka sekund zamiast milczec w nieskonczonosc.
HOTPLUG_RULES = Path("/etc/udev/rules.d/71-wfb-hotplug.rules")
HOTPLUG_FLAG = "--nic-hotplug"

# Ewidencja kart: co, kiedy i w ktorym gniezdzie USB widzielismy ostatnio.
# Bez niej wypieta karta znika bez sladu - system widzi tylko "jest 1 z 2" i nie
# ma jak powiedziec, KTORA zniknela ani w jakim porcie siedziala, bo nieobecny
# interfejs nie ma juz ani MAC-a, ani gniazda. Plik pozwala nazwac brakujaca
# karte po imieniu i roli takze po reboocie z wypietym donglem.
WFB_CARDS = Path("/etc/wfb-cards.json")

# Staly komplet kluczy, ten sam w drone.py i gs.py - dzieki temu nic nie trzeba
# przenosic miedzy urzadzeniami (wfb_keygen na kazdym Pi zrobilby INNA pare i
# strony by sie nie dogadaly). Format wfb-ng: 64 bajty na plik = 32B wlasnego
# klucza tajnego + 32B klucza publicznego drugiej strony.
#
# UWAGA: to nie jest sekret - kto ma ten skrypt, moze podsluchac transmisje i
# wstrzykiwac ramki. Menu ma opcje wygenerowania wlasnej pary.
DRONE_KEY_B64 = "ONKU2CxymjK/C/RQ6uMT7ag9o9pGlcPXegmvGoW2tkOn4iXuoGKSDQ8MG8yGXjiON+I3plWs2rnKn8p4XHK5aw=="
GS_KEY_B64 = "qJj1/pcDLw3vG22U/MWmjtT5EWx+iPCKFbFGt3Gh5WD4kzkppwvbQfX4rZUkdmflvy+TDojAxEit/ey2lr+wVQ=="

ROLE_SECTION = (
    "[drone_mavlink]\n"
    "# peer = 'listen://0.0.0.0:14550'\n\n"
    "[drone_video]\n"
    "peer = 'listen://0.0.0.0:5602'\n"
) if IS_DRONE else (
    "[gs_mavlink]\n"
    "peer = 'connect://127.0.0.1:14550'\n\n"
    "[gs_video]\n"
    "peer = 'connect://127.0.0.1:5600'\n"
)


# ------------------------- pomocnicze -------------------------

def log(msg=""):
    print(msg, flush=True)


def run(cmd, timeout=None):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except FileNotFoundError:
        return 127, f"brak polecenia: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, "timeout"


def run_tool(name, *args, timeout=10):
    """Jak run(), ale probuje tez /usr/sbin i /sbin - tam czesto leza
    narzedzia (rfkill, modinfo, ...), ktorych zwykly PATH nie zawiera."""
    for base in (name, f"/usr/sbin/{name}", f"/sbin/{name}"):
        code, out = run([base, *args], timeout=timeout)
        if code != 127:
            return code, out
    return 127, f"brak polecenia: {name}"


def require_root():
    if os.geteuid() != 0:
        print(f"Uruchom jako root: sudo python3 {Path(__file__).name}")
        sys.exit(1)


def wfb_nics():
    code, out = run_tool("wfb-nics")
    if code != 0:
        return []
    return [n for n in out.split() if n]


COMPETING_USB_DRIVERS = ["rtw88_8812au", "88XXau", "8812au", "rtl8812au"]
TARGET_USB_DRIVER = "rtl88xxau_wfb"

# Pomocnicze przy szukaniu dongli w lsusb. To tylko wskazowka dla uzytkownika
# ("czy kernel w ogole widzi obie karty") - wiazaca lista interfejsow i tak
# pochodzi z wfb-nics. Czesc klonow raportuje samo ID bez opisu, stad ID.
RTL_USB_MARKERS = ("8812", "8811", "8813", "8814", "0bda:881")


def usb_rtl_dongles():
    code, out = run(["lsusb"])
    if code != 0:
        return []
    return [line.strip() for line in out.splitlines()
            if any(m in line.lower() for m in RTL_USB_MARKERS)]


def nic_usb_slot(nic):
    """Gniazdo USB karty, np. '1-1:1.0'. Stale dla danego portu niezaleznie od
    tego, ktory dongiel w nim siedzi - uzywane jako zapasowa kotwica nazwy,
    gdy MAC-a nie da sie odczytac albo dwie karty maja ten sam."""
    dev = Path("/sys/class/net") / nic / "device"
    try:
        return dev.resolve().name if dev.exists() else ""
    except OSError:
        return ""


USB_DEVICES = Path("/sys/bus/usb/devices")


def usb_port_path(nic):
    """Sam port USB, bez koncowki interfejsu: '1-1.4:1.0' -> '1-1.4'. Tak
    nazywa gniazdo cale sysfs, wiec dopiero pod ta postacia da sie doczytac
    predkosc, producenta i drzewko hubow."""
    return nic_usb_slot(nic).split(":")[0]


def _usb_attr(port, name):
    try:
        return (USB_DEVICES / port / name).read_text().strip()
    except OSError:
        return ""


def usb_speed_txt(speed):
    """Surowe Mb/s z sysfs na nazwe generacji USB. Wazne przy 8812AU: dongiel
    wpiety w port USB 2.0 raportuje 480 i przy pelnym strumieniu wideo potrafi
    gubic pakiety - a po samym wygladzie gniazda tego nie widac."""
    table = {"1.5": "USB 1.1", "12": "USB 1.1", "480": "USB 2.0",
             "5000": "USB 3.0", "10000": "USB 3.1", "20000": "USB 3.2"}
    if not speed:
        return ""
    return f"{table.get(speed, 'USB ?')}, {speed} Mb/s"


def usb_port_txt(port, short=False):
    """Gniazdo USB po ludzku: '1-1.4  (magistrala 1, gniazdo 1.4, USB 2.0)'.
    Bez tego port jest tylko ciagiem cyfr - a przy dwoch identycznych donglach
    to wlasnie numer gniazda mowi, ktory z nich trzymasz w rece."""
    if not port:
        return "gniazdo nieznane"
    if short:
        return port
    bits = []
    bus, _, chain = port.partition("-")
    if chain:
        bits.append(f"magistrala {bus}, gniazdo {chain}")
    speed = usb_speed_txt(_usb_attr(port, "speed"))
    if speed:
        bits.append(speed)
    product = _usb_attr(port, "product")
    if product:
        bits.append(product[:28])
    return f"{port}" + (f"  ({', '.join(bits)})" if bits else "")


def nic_usb_txt(nic, short=False):
    return usb_port_txt(usb_port_path(nic), short)


def nic_mac(nic):
    """MAC karty, malymi literami. To na nim wieszamy nazwy: MAC jedzie razem
    z dongla, wiec karta przelozona do innego portu zachowuje swoja nazwe -
    a przy sprzecie przykreconym do konkretnej karty (wzmacniacz, antena) to
    wlasnie karta, a nie gniazdo, musi trzymac tozsamosc."""
    try:
        return (Path("/sys/class/net") / nic / "address").read_text().strip().lower()
    except OSError:
        return ""


def nic_details(nic):
    """Skad karta pochodzi i w jakim jest stanie: sterownik, MAC, fizyczny
    port USB (rozroznia dwa identyczne dongle), tryb pracy i kanal."""
    base = Path("/sys/class/net") / nic
    info = {"driver": "?", "mac": "?", "usb": "?", "mode": "?", "channel": "?"}

    info["mac"] = nic_mac(nic) or "?"

    drv = base / "device" / "driver"
    if drv.exists():
        info["driver"] = drv.resolve().name
    # np. "1-1.4:1.0" - identyfikuje gniazdo USB, wiec po zamianie kart
    # widac ktora jest ktora
    info["usb"] = nic_usb_slot(nic) or "?"

    code, out = run_tool("iw", "dev", nic, "info")
    if code == 0:
        m = re.search(r"type (\w+)", out)
        if m:
            info["mode"] = m.group(1)
        m = re.search(r"channel (\d+)", out)
        if m:
            info["channel"] = m.group(1)
    return info


def nic_counters(nic):
    base = Path("/sys/class/net") / nic / "statistics"

    def rd(name):
        try:
            return int((base / name).read_text().strip())
        except (OSError, ValueError):
            return 0

    return rd("rx_packets"), rd("tx_packets")


def nic_traffic(nics, window=2.0):
    """Ile pakietow na sekunde faktycznie przechodzi przez kazda karte.
    To jest wlasciwy test "czy dziala": sterownik moze byc zaladowany,
    interfejs istniec, a karta i tak nic nie robic (martwy port USB, za
    slabe zasilanie, zly kanal). Zwraca {nic: (rx_pps, tx_pps)}."""
    first = {n: nic_counters(n) for n in nics}
    time.sleep(window)
    result = {}
    for n in nics:
        rx0, tx0 = first[n]
        rx1, tx1 = nic_counters(n)
        result[n] = ((rx1 - rx0) / window, (tx1 - tx0) / window)
    return result


# ------------------------- identyfikacja dongli USB -------------------------

# Baza nazw urzadzen USB - ta sama, z ktorej lsusb bierze opisy. Rozne obrazy
# systemu trzymaja ja w roznych miejscach; bierzemy pierwsza, ktora istnieje.
USB_IDS_PATHS = (Path("/usr/share/misc/usb.ids"), Path("/usr/share/hwdata/usb.ids"),
                 Path("/var/lib/usbutils/usb.ids"))

# VID Realteka. Pod nim siedza ID REFERENCYJNE, ktore wstawia do EEPROM kazdy
# producent bez wlasnego VID-u - pod 0bda:8812 kryje sie i markowa karta, i klon
# za grosze, a moc maja zupelnie inna. Nazwy urzadzenia z takiego ID nie ma.
REALTEK_VID = "0bda"

# Chip po ID referencyjnym Realteka - dopiero gdy ani napis z karty, ani usb.ids
# nie podaja go wprost (np. chip nowszy niz baza w systemie).
REALTEK_PID_CHIPS = {
    "8812": "RTL8812AU", "881a": "RTL8812AU", "881b": "RTL8812AU", "881c": "RTL8812AU",
    "8813": "RTL8814AU", "0811": "RTL8811AU/8821AU", "0821": "RTL8821AU", "0823": "RTL8821AU",
    "a811": "RTL8811AU", "b812": "RTL8812BU", "a81a": "RTL8812EU",
}

# Chip po sterowniku - ostatnia deska ratunku, bo jeden sterownik obsluguje cala
# rodzine chipow. rtw88_<chip> rozpoznajemy po samej nazwie (usb_chip_txt).
DRIVER_CHIPS = {
    TARGET_USB_DRIVER: "RTL8812AU/8821AU/8814AU", "88XXau": "RTL8812AU/8821AU/8814AU",
    "rtl8812au": "RTL8812AU", "8812au": "RTL8812AU", "rtl8814au": "RTL8814AU",
    "8812eu": "RTL8812EU", "rtl88x2bu": "RTL8812BU/8822BU", "88x2bu": "RTL8812BU/8822BU",
}

WIFI_DRIVER_RE = re.compile(r"^(rtl8[0-9]|rtw8|88|8812|8814|8821|mt7|ath9k|carl9170|rt2800|rt73)", re.I)
CHIP_RE = re.compile(r"(?:RTL|Realtek)[\s_-]?(8\d{3}[A-Z]{1,2})\b", re.I)
# Napisy, ktore nic nie mowia o tym, kto zrobil karte - klony wpisuja wlasnie takie.
GENERIC_USB_RE = re.compile(r"802\.11|\bnic\b|wlan|wireless|wi-?fi|realtek|rtl ?8\d{3}|adapter|^\s*$", re.I)

_usb_ids_cache = {}


def usb_ids_names(vid, pid):
    """(producent, produkt) z bazy usb.ids albo puste napisy. Plik ma kilkaset
    kB, wiec wynik trzymamy w pamieci - ekran kart pyta o to co pol sekundy."""
    key = (vid, pid)
    if key in _usb_ids_cache:
        return _usb_ids_cache[key]
    vendor = product = ""
    for path in USB_IDS_PATHS:
        try:
            with path.open(encoding="utf-8", errors="replace") as f:
                in_vendor = False
                for line in f:
                    if line.startswith("#") or not line.strip():
                        continue
                    if not line.startswith("\t"):
                        if in_vendor:
                            break  # nastepny producent - tego produktu w bazie nie ma
                        if line[:4].lower() == vid:
                            vendor, in_vendor = line[4:].strip(), True
                    elif in_vendor and not line.startswith("\t\t") and line[1:5].lower() == pid:
                        product = line[5:].strip()
                        break
        except OSError:
            continue
        break  # pierwsza istniejaca baza wystarczy
    _usb_ids_cache[key] = (vendor, product)
    return vendor, product


def usb_intf_driver_nics(port):
    """(sterownik, [interfejsy sieciowe]) urzadzenia USB, zebrane z jego
    interfejsow <gniazdo>:<konfiguracja>.<numer> w sysfs."""
    driver, nics = "", []
    try:
        intfs = sorted(USB_DEVICES.glob(f"{port}:*"))
    except (OSError, ValueError):
        return driver, nics
    for intf in intfs:
        try:
            if not driver and (intf / "driver").exists():
                driver = (intf / "driver").resolve().name
            if (intf / "net").is_dir():
                nics += sorted(p.name for p in (intf / "net").iterdir())
        except OSError:
            continue
    return driver, nics


def usb_wifi_dongles():
    """Dongle Wi-Fi na USB prosto z sysfs: {gniazdo: info}, po kolei gniazd.
    Bez lsusb i bez wfb-nics - ekran kart na zywo pyta o to dwa razy na
    sekunde, a tylko sysfs widzi karte od razu po wpieciu: zanim dostanie nazwe
    i takze wtedy, gdy wisi pod cudzym sterownikiem albo pod zadnym.
    info: port, vid, pid, manufacturer, product, speed, driver, nics."""
    try:
        ports = sorted(p.name for p in USB_DEVICES.iterdir() if ":" not in p.name)
    except OSError:
        return {}
    out = {}
    for port in ports:
        vid, pid = _usb_attr(port, "idVendor").lower(), _usb_attr(port, "idProduct").lower()
        if not vid:
            continue  # kontroler albo cos, co nie jest urzadzeniem
        driver, nics = usb_intf_driver_nics(port)
        product = _usb_attr(port, "product")
        wifi = (any((Path("/sys/class/net") / n / "phy80211").exists() for n in nics)
                or WIFI_DRIVER_RE.match(driver)
                or (vid == REALTEK_VID and pid in REALTEK_PID_CHIPS)
                or any(mk in f"{vid}:{pid} {product}".lower() for mk in RTL_USB_MARKERS)
                or re.search(r"802\.11|wlan", usb_ids_names(vid, pid)[1], re.I))
        if wifi:
            out[port] = dict(port=port, vid=vid, pid=pid, manufacturer=_usb_attr(port, "manufacturer"),
                             product=product, speed=_usb_attr(port, "speed"), driver=driver, nics=nics)
    return out


def usb_chip_txt(info):
    """(chip, skad to wiadomo). Od najpewniejszego: napis z EEPROM karty, baza
    usb.ids, ID referencyjne Realteka, a na koniec sterownik - ten mowi tylko
    o rodzinie, bo jeden sterownik obsluguje kilka chipow."""
    for text, source in ((info["product"], "napis z karty"),
                         (usb_ids_names(info["vid"], info["pid"])[1], "usb.ids")):
        m = CHIP_RE.search(text or "")
        if m:
            return "RTL" + m.group(1).upper(), source
    if info["vid"] == REALTEK_VID and info["pid"] in REALTEK_PID_CHIPS:
        return REALTEK_PID_CHIPS[info["pid"]], "ID USB"
    m = re.match(r"rtw88_(\d{4}[a-z]{2})$", info["driver"])
    if m:
        return "RTL" + m.group(1).upper(), "sterownik"
    if info["driver"] in DRIVER_CHIPS:
        return DRIVER_CHIPS[info["driver"]], "sterownik"
    return "nieznany", ""


def usb_device_txt(info):
    """Nazwa urzadzenia albo "generic". Wygrywa wlasny napis producenta z EEPROM
    (marka, model); potem nazwa z usb.ids - ale TYLKO dla VID-u innego niz
    Realteka, bo pod 0bda baza opisuje chip, a nie to, kto zrobil karte."""
    maker = "" if GENERIC_USB_RE.search(info["manufacturer"]) else info["manufacturer"]
    product = "" if GENERIC_USB_RE.search(info["product"]) else info["product"]
    if maker or product:
        return f"{maker} {product}".strip()
    if info["vid"] != REALTEK_VID:
        vendor, db_product = usb_ids_names(info["vid"], info["pid"])
        if vendor or db_product:
            return f"{vendor} {db_product}".strip()
    return "generic"


def count_txt(n):
    """Liczba kart do komunikatow: "1/2", gdy brakuje do minimum, a samo "3",
    gdy jest ich tyle albo wiecej. Kart moze byc dowolnie duzo - EXPECTED_NICS
    to tylko minimum, wiec "3/2" wygladaloby jak blad."""
    return f"{n}/{EXPECTED_NICS}" if n < EXPECTED_NICS else str(n)


_nic_status_cache = {"t": 0.0, "val": None}


def nic_status_summary(max_age=2.0):
    """Jedna linia stanu kart do naglowka menu - zeby brak dongla bylo widac
    od razu, bez wchodzenia w weryfikacje. Trzy liczniki, bo kazdy pokazuje
    inny etap: ile kart widzi USB, ile z nich dostalo interfejs pod naszym
    sterownikiem i ile z nich naprawde uzywa usluga. Wynik cache'owany, bo
    liczy sie go przy kazdym przerysowaniu menu."""
    now = time.monotonic()
    if _nic_status_cache["val"] and now - _nic_status_cache["t"] < max_age:
        return _nic_status_cache["val"]

    nics = wfb_nics()
    props = service_props()
    used = service_nics(set(nics)) if nics else set()
    dongles = len(usb_rtl_dongles())
    remember_cards(nics)  # zeby bylo czym nazwac karte, gdy za chwile zniknie

    txt = (f"Karty: {count_txt(len(nics))}"
           f"{' [' + ' '.join(nics) + ']' if nics else ''}"
           f"   USB: {count_txt(dongles)}"
           f"   w usludze: {len(used)}/{len(nics)}")

    # Sama liczba "1/2" nie mowi nic o tym, ktorej karty brakuje - a przy
    # rozdziale rol to jest cala roznica miedzy "nie ma czym nadawac"
    # a "leci bez dywersyfikacji". Nazwe bierzemy z ewidencji, bo po
    # wypieciu nie ma juz kogo o nia zapytac.
    gone = missing_cards_txt(nics)
    if len(nics) < EXPECTED_NICS:
        status = "fail"
        txt += "   <- BRAK: " + (gone if gone else "KARTY")
        if dongles > len(nics):
            txt += ", dongiel wisi na innym sterowniku"
    elif not service_active(props):
        # Karty moga byc idealne, a i tak 0/1 - bo usluga w ogole nie wstala.
        # Radzenie "zrestartuj usluge" byloby wtedy myleniem tropu.
        status = "fail"
        txt += f"   <- USLUGA NIE DZIALA ({service_state_txt(props)})"
    elif len(used) < len(nics):
        status = "warn"
        txt += "   <- zrestartuj usluge"
    elif gone:
        # Minimum jest, ale ktoras ze znanych kart zniknela - przy kilku
        # kartach na probe to wlasnie ta, ktorej teraz szukasz.
        status = "warn"
        txt += "   <- BRAK: " + gone
    else:
        status = "ok"

    _nic_status_cache.update(t=now, val=(status, txt))
    return status, txt


def service_props():
    """Stan uslugi wprost z systemd. ActiveState/SubState ida do komunikatow,
    InvocationID - do wyciecia z journala TYLKO biezacego uruchomienia."""
    code, out = run(["systemctl", "show", f"wifibroadcast@{ROLE}",
                     "-p", "ActiveState", "-p", "SubState", "-p", "InvocationID"])
    if code != 0:
        return {}
    return dict(ln.split("=", 1) for ln in out.splitlines() if "=" in ln)


def service_active(props=None):
    props = service_props() if props is None else props
    return props.get("ActiveState") == "active"


def service_state_txt(props=None):
    props = service_props() if props is None else props
    return f"{props.get('ActiveState', '?')}/{props.get('SubState', '?')}"


ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
ERROR_MARKERS = ("#error", "exception", "traceback", "error:", "fatal", "failed")


def service_last_errors(n=6, scan=300):
    """Linie z journala uslugi, ktore faktycznie cos MOWIA. Sam ogon nie
    wystarcza: gdy serwer sie wywala, ostatnie linie to sprzatanie po nim
    (systemd zabija wfb_tx, "Failed with result"), a powod - wyjatek - jest
    kilkanascie linii wyzej. Bierzemy wiec szerszy kawalek i filtrujemy po
    slowach kluczowych, a gdy nic nie pasuje, wracamy do zwyklego ogona.
    Przy petli restartow te same linie powtarzaja sie w kolko, wiec zwracamy
    je bez duplikatow. Kody ANSI (wfb-ng loguje w kolorach) ida precz, bo
    w curses robia z ekranu sieczke."""
    code, out = run(["journalctl", "-u", f"wifibroadcast@{ROLE}", "-n", str(scan),
                     "-o", "cat", "--no-pager"], timeout=15)
    if code != 0:
        return []

    lines = [ANSI_RE.sub("", ln).strip() for ln in out.splitlines() if ln.strip()]
    hits = [ln for ln in lines if any(m in ln.lower() for m in ERROR_MARKERS)]

    seen, uniq = set(), []
    for ln in reversed(hits or lines):
        if ln in seen:
            continue
        seen.add(ln)
        uniq.append(ln)
        if len(uniq) >= n:
            break
    return list(reversed(uniq))


def packet_socket_nics(known):
    """Karty, na ktorych ktos trzyma otwarte gniazdo AF_PACKET - czyli realnie
    z nich czyta i przez nie wstrzykuje (wfb_rx/wfb_tx robia to przez libpcap).
    Najpewniejsze zrodlo, bo pyta jadro o stan TERAZ, a nie o to, co bylo
    w argumentach procesu przy starcie: po zmianie nazwy interfejsu argumenty
    i log uslugi nadal pokazuja stara nazwe, a gniazdo siedzi na tej karcie.
    /proc/net/packet: kolumny sk RefCnt Type Proto Iface R Rmem User Inode."""
    try:
        lines = Path("/proc/net/packet").read_text().splitlines()[1:]
    except OSError:
        return set()

    bound = set()
    for ln in lines:
        f = ln.split()
        if len(f) >= 5 and f[4].isdigit() and f[4] != "0":  # 0 = gniazdo na "any"
            bound.add(int(f[4]))

    used = set()
    for nic in known:
        try:
            if int((Path("/sys/class/net") / nic / "ifindex").read_text()) in bound:
                used.add(nic)
        except (OSError, ValueError):
            pass
    return used


def proc_cmdlines():
    """Pelne linie polecen wszystkich procesow, prosto z /proc. Nie przez 'ps':
    ten - gdy nie pisze na terminal - tnie wynik do 80 kolumn i obcina
    dokladnie to, czego tu szukamy, czyli nazwy kart na koncu polecenia."""
    out = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue  # proces zdazyl sie zakonczyc
        if raw:
            out.append([a for a in raw.decode("utf-8", "replace").split("\0") if a])
    return out


def service_log_nics(known):
    """Karty przejete przez usluge w BIEZACYM uruchomieniu - wfb-ng loguje dla
    kazdej "Interface <nic> has driver <sterownik>". Drugie zrodlo prawdy obok
    argumentow procesow, bo kolejne wersje wfb-ng przekazuja karty do
    wfb_rx/wfb_tx inaczej (gniazda unix zamiast argumentow), a ten log jest
    w kazdej. Patrzymy tylko na biezace uruchomienie uslugi - logi sprzed
    restartu klamalyby, ze wypieta karta nadal jest uzywana."""
    inv = service_props().get("InvocationID", "").strip()
    if not inv:
        return set()

    base = ["journalctl", f"_SYSTEMD_INVOCATION_ID={inv}", "-o", "cat", "--no-pager"]
    code, out = run(base + ["-g", "has driver"], timeout=15)  # -g = filtr po stronie journalctl
    if code != 0:
        code, out = run(base, timeout=15)  # starszy journalctl bez -g
        if code != 0:
            return set()
    return {n for n in re.findall(r"Interface (\S+) has driver", out) if n in known}


def service_nics(known):
    """Interfejsy, ktorych FAKTYCZNIE uzywa dzialajaca usluga. Dongiel wpiety
    po jej starcie istnieje w systemie, ale wfb-ng go nie uzywa, dopoki uslugi
    sie nie zrestartuje - i tego golym okiem nie widac.

    Trzy niezalezne zrodla, od najpewniejszego: otwarte gniazda AF_PACKET (stan
    jadra TERAZ), argumenty procesow wfb_rx/wfb_tx i log uslugi z biezacego
    uruchomienia. Kazde z nich osobno potrafi sie mylic przy innej wersji
    wfb-ng albo po zmianie nazwy interfejsu, wiec bierzemy ich sume."""
    known = set(known)
    if not known or not service_active():
        return set()  # nie ma uslugi - zadna karta nie jest "w usludze"

    used = packet_socket_nics(known)
    if known.issubset(used):
        return used & known

    for args in proc_cmdlines():
        if not any("wfb_rx" in a or "wfb_tx" in a for a in args):
            continue
        used.update(a for a in args if a in known)
    if not known.issubset(used):
        # journalctl wolamy na koncu - ta funkcja liczy sie przy kazdym
        # przerysowaniu menu, a to najdrozszy z jej kawalkow
        used |= service_log_nics(known)
    return used & known


def rebind_to_wfb_driver():
    """Jesli karta RTL8812AU jest podpieta pod inny sterownik (np. wbudowany
    w nowsze jadra rtw88_8812au, ktory rejestruje sie na USB ID karty
    wczesniej niz nasz dkms-owy modul), odpina ja stamtad i podpina pod
    nasz sterownik. Bez tego trzeba by bylo robic to recznie po kazdym
    boocie."""
    target = Path(f"/sys/bus/usb/drivers/{TARGET_USB_DRIVER}")
    if not target.exists():
        return False
    rebound = False
    for drv_name in COMPETING_USB_DRIVERS:
        drv_path = Path(f"/sys/bus/usb/drivers/{drv_name}")
        if not drv_path.exists():
            continue
        for entry in drv_path.iterdir():
            if ":" not in entry.name:
                continue
            dev_id = entry.name
            log(f"    Odpinam {dev_id} od {drv_name}...")
            try:
                (drv_path / "unbind").write_text(dev_id)
            except OSError as e:
                log(f"    (nie udalo sie odpiac: {e})")
                continue
            try:
                (target / "bind").write_text(dev_id)
                log(f"    Podpiety {dev_id} pod {TARGET_USB_DRIVER}")
                rebound = True
            except OSError as e:
                log(f"    (nie udalo sie podpiac: {e})")
    return rebound and bool(wfb_nics())


def driver_loaded():
    code, out = run(["lsmod"])
    return "88XXau_wfb" in out


def driver_built():
    code, _ = run_tool("modinfo", "88XXau_wfb")
    return code == 0


def wfb_ng_installed():
    code, _ = run(["which", "wfb_keygen"])
    return code == 0


def parse_common(txt):
    """Kanal i region WPISANE do /etc/wifibroadcast.cfg. Brak wpisu zwraca
    nasza wartosc domyslna - ale uwaga: to nie znaczy, ze wfb-ng jej uzywa.
    Do pokazywania stanu sluzy wfb_effective_common()."""
    ch = re.search(r"wifi_channel\s*=\s*(\d+)", txt)
    reg = re.search(r"wifi_region\s*=\s*'([^']*)'", txt)
    return (ch.group(1) if ch else DEFAULT_CHANNEL, reg.group(1) if reg else DEFAULT_REGION)


def cfg_has_common():
    """Czy kanal i region stoja w naszym pliku, czy tylko je zakladamy."""
    if not CFG_PATH.exists():
        return False, False
    txt = CFG_PATH.read_text()
    return (bool(re.search(r"^\s*wifi_channel\s*=", txt, re.M)),
            bool(re.search(r"^\s*wifi_region\s*=", txt, re.M)))


_common_cache = {"t": 0.0, "val": None}


def wfb_effective_common(max_age=5.0):
    """(kanal, region) tak, jak widzi je wfb-ng PO scaleniu master.cfg
    z /etc/wifibroadcast.cfg - czyli to, na czym karta naprawde nadaje.

    Pytamy biblioteke, a nie sam plik, bo brak wpisu w /etc NIE znaczy "no to
    domyslnie 13". Znaczy "to, co wfb-ng ma u siebie", a tam domyslny kanal to
    161, czyli 5805 MHz - i wlasnie stad link potrafi wstac na 5.8 GHz, mimo ze
    ten skrypt jest pisany pod 2.4 GHz."""
    now = time.monotonic()
    if _common_cache["val"] and now - _common_cache["t"] < max_age:
        return _common_cache["val"]

    value = None
    code, out = run(["python3", "-c",
                     "from wfb_ng.conf import settings; "
                     "print(settings.common.wifi_channel, settings.common.wifi_region)"],
                    timeout=30)
    if code == 0:
        for ln in reversed(out.splitlines()):
            m = re.match(r"^\s*(\d+)\s+(\S+)\s*$", ln)
            if m:
                value = (m.group(1), m.group(2).strip("'\""))
                break
    if value is None:  # brak wfb-ng albo inna wersja - zostaje sam plik
        value = (parse_common(CFG_PATH.read_text()) if CFG_PATH.exists()
                 else (DEFAULT_CHANNEL, DEFAULT_REGION))
    _common_cache.update(t=now, val=value)
    return value


def channel_source_note(channel):
    """Skad wzial sie kanal, na ktorym stoi link - albo None, gdy wszystko sie
    zgadza. Bez tego "13" na ekranie potrafi byc nasza domyslna wartoscia,
    a karta i tak siedzi na 161."""
    has_channel, _ = cfg_has_common()
    if not has_channel:
        return (f"kanal {channel} pochodzi z ustawien wfb-ng, w {CFG_PATH} nie ma "
                f"wpisu wifi_channel")
    written = parse_common(CFG_PATH.read_text())[0]
    if written != channel:
        return f"w {CFG_PATH} stoi kanal {written}, a wfb-ng uzywa {channel}"
    return None


def wfb_streams():
    """Lista strumieni profilu tak, jak widzi ja wfb-ng PO scaleniu master.cfg,
    site.cfg i /etc/wifibroadcast.cfg. Pytamy biblioteke zamiast parsowac pliki,
    bo typ uslugi nie stoi w sekcji [<rola>_video] - tam sa tylko fwmark i peer
    - tylko w profilu [<rola>] w liscie 'streams'. Osobny interpreter, a nie
    import u siebie, bo wfb_ng.conf cache'uje config przy imporcie i po naszej
    zmianie oddawalby nieaktualne dane."""
    code, out = run(["python3", "-c",
                     f"from wfb_ng.conf import settings; print(repr(settings.{ROLE}.streams))"],
                    timeout=30)
    if code != 0:
        return None

    start, end = out.find("["), out.rfind("]")  # run() sklei stdout ze stderr,
    if start == -1 or end <= start:             # wiec wycinamy sam literal
        return None
    try:
        streams = ast.literal_eval(out[start:end + 1])
    except (ValueError, SyntaxError):
        return None
    return streams if isinstance(streams, list) else None


def video_service_type(streams=None):
    """Tryb uslugi wideo widziany przez wfb-ng albo None, gdy nie da sie go
    ustalic (np. wfb-ng jeszcze nie zainstalowany)."""
    streams = wfb_streams() if streams is None else streams
    if not streams:
        return None
    return next((s.get("service_type") for s in streams if s.get("name") == "video"), None)


def backup_config_once():
    """Kopia oryginalnego configu przed pierwsza nasza ingerencja - zeby bylo
    do czego wrocic, gdyby nadpisanie 'streams' okazalo sie nietrafione."""
    bak = Path(str(CFG_PATH) + ".bak")
    if CFG_PATH.exists() and not bak.exists():
        bak.write_text(CFG_PATH.read_text())


def set_cfg_option(section, key, value_txt):
    """Ustawia klucz w sekcji /etc/wifibroadcast.cfg: dopisuje sekcje, gdy jej
    nie ma, podmienia wartosc, gdy klucz juz tam jest."""
    txt = CFG_PATH.read_text()
    header = f"[{section}]"
    line = f"{key} = {value_txt}"
    start = txt.find(header)

    if start == -1:
        CFG_PATH.write_text(txt.rstrip("\n") + f"\n\n{header}\n{line}\n")
        return

    end = txt.find("\n[", start + 1)
    end = len(txt) if end == -1 else end
    body = txt[start:end]
    if re.search(rf"^\s*{re.escape(key)}\s*=", body, re.M):
        body = re.sub(rf"^\s*{re.escape(key)}\s*=.*$", line, body, count=1, flags=re.M)
    else:
        body = body.replace(header, f"{header}\n{line}", 1)
    CFG_PATH.write_text(txt[:start] + body + txt[end:])


def get_cfg_option(section, key):
    """Wartosc klucza z sekcji albo None. Wycina sekcje dokladnie tak samo jak
    set_cfg_option/drop_cfg_option, wiec czyta to, co same zapisuja."""
    if not CFG_PATH.exists():
        return None
    txt = CFG_PATH.read_text()
    start = txt.find(f"[{section}]")
    if start == -1:
        return None
    end = txt.find("\n[", start + 1)
    body = txt[start:len(txt) if end == -1 else end]
    m = re.search(rf"^\s*{re.escape(key)}\s*=\s*(.+?)\s*$", body, re.M)
    return m.group(1) if m else None


def drop_cfg_option(section, key):
    """Usuwa klucz z sekcji - sprzata po wpisie, ktory i tak nic nie robil."""
    txt = CFG_PATH.read_text()
    header = f"[{section}]"
    start = txt.find(header)
    if start == -1:
        return False
    end = txt.find("\n[", start + 1)
    end = len(txt) if end == -1 else end
    body = txt[start:end]
    new_body = re.sub(rf"^\s*{re.escape(key)}\s*=.*\n?", "", body, flags=re.M)
    if new_body == body:
        return False
    CFG_PATH.write_text(txt[:start] + new_body + txt[end:])
    return True


def ensure_video_service_type(nics):
    """Domyslny tryb wideo (udp_direct_tx) nie umie nadawac z kilku kart:
    serwer konczy sie wtedy bledem "udp_direct_tx doesn't supports diversity
    and/or rx-only wlans. Use udp_proxy for such case." i systemd restartuje go
    w kolko - z zewnatrz widac tylko status "activating", a karty wygladaja na
    sprawne. Przy wiecej niz jednej karcie nadpisujemy w profilu [<rola>] cala
    liste 'streams' z podmienionym service_type dla wideo. Liste bierzemy od
    wfb-ng, a nie z zaszytej u nas kopii, bo kolejne wersje dokladaja strumienie
    i zmieniaja numery portow."""
    if len(nics) < 2 or not CFG_PATH.exists():
        return False

    # sprzatanie po wczesniejszej wersji tego skryptu: service_type w sekcji
    # [<rola>_video] byl martwym wpisem, wfb-ng go tam nie czyta
    dead_key = drop_cfg_option(f"{ROLE}_video", "service_type")

    streams = wfb_streams()
    if not streams or video_service_type(streams) != "udp_direct_tx":
        return dead_key  # juz naprawione, inna wersja albo brak wfb-ng

    backup_config_once()
    fixed = [dict(s, service_type="udp_proxy") if s.get("name") == "video" else s
             for s in streams]
    # repr() daje jedna linie - parser configu nie lubi zawijanych wartosci
    set_cfg_option(ROLE, "streams", repr(fixed))
    return True


def build_config(channel, region):
    return (
        "[common]\n"
        f"wifi_channel = {channel}\n"
        f"wifi_region = '{region}'\n\n"
        f"{ROLE_SECTION}"
    )


def rx_only_nics(nics):
    """Karty, ktore maja NIE nadawac: te z rola rx ORAZ te bez zadnego
    przydzialu (wlanX). wfb-ng ma na to wartosc wifi_txpower = 'off' (w
    master.cfg "special value for RX only cards"): taka karta jest inicjowana
    i odbiera, ale nie trafia na liste interfejsow wfb_tx. Bez tego wfb_tx
    rozklada pakiety miedzy wszystkie karty (mirror jest domyslnie wylaczony),
    wiec czesc wideo wychodzilaby torem bez wzmacniacza - a karta bez
    przydzialu to zwykle dongiel wpiety "na probe", ktory tez nie ma nadawac."""
    return [n for n in nics if role_of_name(n) in ("", "rx")]


def muted_nics(nics):
    """Karty, ktore config NAPRAWDE wycisza ('off'): rx-only, ale tylko gdy jest
    kim nadawac. Przy jednej karcie albo samych RX bezpiecznik zostawia nadawanie
    wszystkim - lepiej nadawac torem bez wzmacniacza niz nie nadawac wcale."""
    rx_only = rx_only_nics(nics)
    return set(rx_only) if len(nics) >= 2 and len(rx_only) < len(nics) else set()


def txpower_cfg_value(nics):
    """Tresc wpisu wifi_txpower dla sekcji [common] albo None, gdy nie ma w nim
    nic do powiedzenia. Na karte: 'off' = tylko odbior (muted_nics), liczba =
    indeks ustawiany karcie osobno (card_power_plan: wlasna moc albo limit;
    -indeks*100, tak wfb-ng wola 'iw set txpower fixed'), None = moc wspolna,
    czyli parametr modulu (TX_POWER_SYSFS).

    Liczby piszemy WYLACZNIE przy sterowniku z latka mocy per karta: bez niej iw
    ustawia moc wszystkim kartom naraz i wygrywalaby ta, ktora wfb-ng ustawi
    ostatnia. Slownik musi miec wpis dla KAZDEJ karty - inaczej wfb-ng nie wstaje."""
    muted = muted_nics(nics)
    powers = card_power_plan(nics) if driver_card_txpower() == "on" else {}
    entries = {n: "'off'" if n in muted else str(-100 * powers[n]) if n in powers else "None"
               for n in nics}
    if all(v == "None" for v in entries.values()):
        return None
    return "{" + ", ".join(f"'{n}': {entries[n]}" for n in sorted(nics)) + "}"


def ensure_tx_split(nics):
    """Wymusza wpis wifi_txpower zgodny z rolami i mocami kart: karty z rola rx
    (i bez przydzialu) nie nadaja, karty z wlasna moca dostaja swoja. Zwraca
    True, gdy config zostal zmieniony - wolajacy restartuje usluge i sprawdza,
    czy wstala (patrz apply_tx_split)."""
    if not CFG_PATH.exists():
        return False
    want = txpower_cfg_value(nics)
    current = get_cfg_option("common", "wifi_txpower")

    if want is None:
        # Nie ma nic do rozdzielenia ani wlasnych mocy - np. padla karta nadawcza
        # albo wszystkim ustawiono rx, a wpis 'off' odebralby nadawanie W OGOLE.
        # Kasujemy go; ruszamy tylko wpis w formie slownika, czyli ten, ktory
        # sami piszemy.
        if nics and current and current.startswith("{"):
            backup_config_once()
            return drop_cfg_option("common", "wifi_txpower")
        return False

    if current == want:
        return False
    backup_config_once()
    set_cfg_option("common", "wifi_txpower", want)
    return True


def apply_tx_split(nics, say):
    """Rozdzial rol + restart uslugi z wycofaniem, gdy usluga nie wstanie.
    Dron w powietrzu nie ma jak zglosic, ze config jest nie do przyjecia dla
    tej wersji wfb-ng - wiec jesli po zmianie usluga nie zyje, wracamy do
    poprzedniego stanu i mowimy o tym wprost. Zwraca True, gdy cos zmieniono."""
    if not ensure_tx_split(nics):
        return False

    muted = muted_nics(nics)
    if muted:
        say(f"config: {', '.join(sorted(muted))} tylko do odbioru (wifi_txpower = 'off')", "warn")
    elif txpower_cfg_value(nics):
        say("config: wlasna moc kart zapisana w wifi_txpower", "warn")
    elif nics and len(rx_only_nics(nics)) == len(nics):
        say("config: zadna karta nie ma roli nadawczej - zdejmuje wifi_txpower,"
            " nadaja wszystkie", "warn")
    else:
        say("config: zdejmuje wifi_txpower - karty nadaja z moca wspolna", "warn")
    run(["systemctl", "restart", f"wifibroadcast@{ROLE}"])
    time.sleep(3)
    if service_active():
        return True

    drop_cfg_option("common", "wifi_txpower")
    run(["systemctl", "restart", f"wifibroadcast@{ROLE}"])
    time.sleep(3)
    say("ta wersja wfb-ng nie przyjela wpisu wifi_txpower - wycofano zmiane", "fail")
    return True


def save_common_config(channel, region):
    """Zapis kanalu i regionu BEZ deptania reszty pliku. Ekran zmiany
    konfiguracji przepisywal go wczesniej od zera z build_config(), przez co
    kasowal nadpisanie 'streams' zrobione przez ensure_video_service_type() -
    i usluga wracala do petli restartow zaraz po zmianie kanalu albo mocy."""
    if not CFG_PATH.exists():
        CFG_PATH.write_text(build_config(channel, region))
        return
    set_cfg_option("common", "wifi_channel", channel)
    set_cfg_option("common", "wifi_region", f"'{region}'")


def parse_tx_power():
    """Aktualnie zapisana (persystowana) wartosc mocy - z pliku modprobe.d,
    nie z live sysfs (ta moze byc chwilowo inna np. tuz po instalacji)."""
    if MODPROBE_WFB.exists():
        m = re.search(r"rtw_tx_pwr_idx_override=(\d+)", MODPROBE_WFB.read_text())
        if m:
            return m.group(1)
    return DEFAULT_TX_POWER


def write_modprobe_wfb(tx_power):
    MODPROBE_WFB.write_text(
        "blacklist 88XXau\n"
        "blacklist 8812au\n"
        "blacklist rtl8812au\n"
        "blacklist rtw88_8812au\n"
        f"options 88XXau_wfb rtw_tx_pwr_idx_override={clamp_tx_power(tx_power)}\n"
    )


def apply_tx_power_live(tx_power):
    """Wymusza moc nadawania natychmiast, bez przeladowania modulu. Parametr
    modulu 88XXau_wfb jest zapisywalny na zywo przez sysfs. Wartosc przechodzi
    przez pulap, bo to jest ostatnie miejsce przed samym sterownikiem."""
    if not TX_POWER_SYSFS.exists():
        return False
    try:
        TX_POWER_SYSFS.write_text(clamp_tx_power(tx_power))
        return True
    except OSError:
        return False


def read_tx_power_live():
    if TX_POWER_SYSFS.exists():
        try:
            return TX_POWER_SYSFS.read_text().strip()
        except OSError:
            return None
    return None


# Znak latki "moc per karta" (CARD_TXPOWER_PATCH): po nim, a nie po wersji
# sterownika, poznajemy, czy zaladowany modul umie ustawic moc jednej karcie.
CARD_TXPOWER_PARAM = Path("/sys/module/88XXau_wfb/parameters/rtw_wfb_card_txpower")
_card_txpower_cache = {"t": 0.0, "val": None}


def driver_card_txpower(max_age=5.0):
    """Czy sterownik umie moc per karta: "on" - zaladowany modul ma latke (albo
    modul jest niezaladowany, ale zbudowany z latka, wiec wstanie z nia),
    "reload" - zbudowany z latka, ale w pamieci siedzi jeszcze stary modul,
    "" - sterownik bez latki.

    Bez latki 'iw set txpower' na tym sterowniku ustawia moc WSZYSTKIM kartom
    (jedna zmienna rtw_tx_pwr_idx_override) - dlatego liczby per karta trafiaja
    do configu tylko przy "on", inaczej karty nadpisywalyby sobie moc."""
    now = time.monotonic()
    if _card_txpower_cache["val"] is not None and now - _card_txpower_cache["t"] < max_age:
        return _card_txpower_cache["val"]
    if CARD_TXPOWER_PARAM.exists():
        state = "on"
    else:
        code, out = run_tool("modinfo", "-p", "88XXau_wfb")
        built = code == 0 and CARD_TXPOWER_PARAM.name in out
        state = ("reload" if TX_POWER_SYSFS.exists() else "on") if built else ""
    _card_txpower_cache.update(t=now, val=state)
    return state


def _card_numbers(nics, field):
    """{interfejs: liczba z ewidencji} dla pola karty ("power", "limit") - tylko
    dla kart, ktore je maja. Liczby wisza na kotwicy karty (MAC), wiec jada z nia
    tak jak rola. Nigdy ponad TX_POWER_CAP, cokolwiek lezaloby w pliku."""
    cards = load_cards()
    anchors = nic_anchors(nics)
    out = {}
    for nic in nics:
        value = cards.get(anchor_key(anchors.get(nic)), {}).get(field)
        if isinstance(value, int) and value > 0:
            out[nic] = min(value, TX_POWER_CAP)
    return out


def card_powers(nics):
    """{interfejs: wlasny indeks mocy karty} - tylko dla kart, ktore go maja."""
    return _card_numbers(nics, "power")


def card_limits(nics):
    """{interfejs: limit mocy karty} - sufit, ktorego karta nie przekroczy ani
    wlasna moca, ani wspolna (np. mocny dongiel, ktory na pelnej mocy grzeje sie
    albo ciagnie za duzo pradu z USB)."""
    return _card_numbers(nics, "limit")


def shared_power_index(live=False):
    """Moc wspolna jako liczba: zapisana w modprobe.d albo (live) z sysfs. None
    dla 0 - to nie moc, tylko 'kalibracja EEPROM', ktorej wartosci nie znamy."""
    txt = str((read_tx_power_live() if live else None) or parse_tx_power())
    return int(txt) if txt.isdigit() and int(txt) > 0 else None


def card_power_plan(nics, live=False):
    """{interfejs: indeks mocy ustawiany tej karcie OSOBNO}. Wlasna moc zawsze,
    ale nie ponad limit karty; karta bez wlasnej mocy dostaje swoj limit tylko
    wtedy, gdy wspolna go przekracza - inaczej zostaje na wspolnej. Przy wspolnej
    0 (kalibracja EEPROM) nie wiadomo, ile to jest, wiec limit scina wtedy tylko
    wlasna moc. Z tego planu biora sie config, ustawienie na zywo i weryfikacja."""
    powers, limits = card_powers(nics), card_limits(nics)
    shared = shared_power_index(live)
    plan = {}
    for nic in nics:
        limit = limits.get(nic, TX_POWER_CAP)
        if nic in powers:
            plan[nic] = min(powers[nic], limit)
        elif shared and shared > limit:
            plan[nic] = limit
    return plan


def power_meter(value, limit=None, width=20):
    """Pasek mocy jak meter(), z kreska '|' na ostatniej kratce, na ktora pozwala
    limit karty - zeby sufit bylo widac bez czytania liczb."""
    bar = list(meter(value, 0, TX_POWER_CAP, width))
    if limit and limit < TX_POWER_CAP:
        bar[max(1, min(width, int(round(limit / TX_POWER_CAP * width))))] = "|"
    return "".join(bar)


def card_power_live(nic):
    """Indeks mocy karty tak, jak widzi go sterownik ('iw dev X info' - latka
    zwraca tam -indeks), 0 = bez nadpisania (kalibracja EEPROM), None = nie wiadomo."""
    code, out = run_tool("iw", "dev", nic, "info")
    m = re.search(r"txpower (-?\d+)(?:\.\d+)? dBm", out) if code == 0 else None
    if not m:
        return None
    value = int(m.group(1))
    return -value if value < 0 else 0


def _card_key(nic):
    """(wfb-nics, klucz karty w ewidencji) albo (None, powod odmowy) - wspolne
    sprawdzenie dla ustawien mocy: musza trzymac sie karty (MAC), a do tego
    dzialaja tylko ze sterownikiem z latka mocy per karta."""
    if driver_card_txpower() != "on":
        return None, "sterownik nie ma mocy per karta - P = przebuduj/przeladuj sterownik"
    nics = wfb_nics()
    if nic not in nics:
        return None, f"karty {nic} nie ma pod wfb"
    key = anchor_key(nic_anchors(nics).get(nic))
    if not key:
        return None, f"{nic} nie ma ani MAC-a, ani gniazda - nie ma gdzie zapamietac mocy"
    return nics, key


def _save_card_numbers(nics, key, **fields):
    """Zapis liczb karty do ewidencji; wartosc pusta (None, 0) usuwa pole."""
    cards = remember_cards(nics)
    entry = dict(cards.get(key, {}))
    for field, value in fields.items():
        if value:
            entry[field] = value
        else:
            entry.pop(field, None)
    cards[key] = entry
    return save_cards(cards)


def apply_card_power_live(nic, nics):
    """Ustawia sterownikowi moc karty wedlug card_power_plan: osobny indeks
    (ujemna wartosc dla iw) albo 1800, ktore zdejmuje osobny indeks - w latce
    dodatnia wartosc zeruje nadpisanie karty, a 18 to domyslny CurrentTxPwrIdx,
    wiec 8814AU nie traci przy tym mocy. Zwraca (indeks albo None, kod, wyjscie)."""
    target = card_power_plan(nics, live=True).get(nic)
    code, out = run_tool("iw", "dev", nic, "set", "txpower", "fixed",
                         str(-100 * target) if target else "1800")
    return target, code, out


def set_card_power(nic, power):
    """Wlasna moc karty (1..limit karty) albo 0 = powrot do wspolnej. Zapis idzie
    w trzy miejsca: ewidencja (moc jedzie z karta po MAC-u), config (wfb-ng
    ustawi ja przy kazdym starcie) i od razu sterownik przez iw - bez restartu
    uslugi, wiec link nie staje. Zwraca (ok, komunikat)."""
    nics, key = _card_key(nic)
    if nics is None:
        return False, key
    limit = card_limits(nics).get(nic, TX_POWER_CAP)
    power = max(0, min(int(power), limit))
    if not _save_card_numbers(nics, key, power=power):
        return False, f"nie moge zapisac {WFB_CARDS}"
    target, code, out = apply_card_power_live(nic, nics)
    ensure_tx_split(nics)  # config na nastepny start uslugi; restartu nie trzeba
    if code != 0:
        return False, f"{nic}: moc zapisana, ale iw odmowilo: {out.strip()[:60]}"
    if power:
        return True, (f"{nic}: moc {power}/{TX_POWER_CAP} - tylko ta karta"
                      + (f" (limit {limit})" if limit < TX_POWER_CAP else ""))
    if target:
        return True, f"{nic}: moc wspolna, ale scieta limitem do {target}"
    return True, f"{nic}: moc wspolna ({read_tx_power_live() or '?'}/{TX_POWER_CAP})"


def set_card_limit(nic, limit):
    """Limit mocy karty (1..TX_POWER_CAP; TX_POWER_CAP albo 0 = bez limitu) - sufit,
    ktorego karta nie przekroczy ani wlasna moca, ani wspolna. Wlasna moc ponad
    nowy limit od razu schodzi do niego, zeby zapis nie obiecywal wiecej, niz
    karta dostanie. Zapis jak w set_card_power. Zwraca (ok, komunikat)."""
    nics, key = _card_key(nic)
    if nics is None:
        return False, key
    limit = max(0, min(int(limit), TX_POWER_CAP))
    if limit == TX_POWER_CAP:
        limit = 0  # sufit rowny pulapowi to po prostu brak limitu
    own = card_powers(nics).get(nic)
    lowered = bool(limit and own and own > limit)
    fields = {"limit": limit, **({"power": limit} if lowered else {})}
    if not _save_card_numbers(nics, key, **fields):
        return False, f"nie moge zapisac {WFB_CARDS}"
    target, code, out = apply_card_power_live(nic, nics)
    ensure_tx_split(nics)
    if code != 0:
        return False, f"{nic}: limit zapisany, ale iw odmowilo: {out.strip()[:60]}"
    msg = f"{nic}: limit {limit}/{TX_POWER_CAP}" if limit else f"{nic}: bez limitu (pulap {TX_POWER_CAP})"
    if lowered:
        msg += f", wlasna moc {own} -> {limit}"
    return True, msg + (f" - nadaje z {target}" if target else " - nadaje z moca wspolna")


def reapply_card_powers():
    """Po zmianie mocy WSPOLNEJ na zywo: karty z limitem ponizej nowej wspolnej
    musza dostac limit osobno, a te, ktorych wspolna juz nie przekracza - wrocic
    do niej. Bez tego limit bylby lamany az do restartu uslugi, bo wfb-ng czyta
    config tylko przy starcie. Zwraca linijki do komunikatu (pusto, gdy nic)."""
    if driver_card_txpower() != "on":
        return []
    nics = wfb_nics()
    if not nics:
        return []
    for nic in nics:
        apply_card_power_live(nic, nics)
    ensure_tx_split(nics)  # config na nastepny start uslugi
    plan = card_power_plan(nics, live=True)
    return [f"osobno: {nic} = {power}/{TX_POWER_CAP}" for nic, power in sorted(plan.items())]


def channel_freq(channel):
    """Czestotliwosc srodkowa kanalu w MHz (2.4 GHz i 5 GHz)."""
    try:
        ch = int(channel)
    except (TypeError, ValueError):
        return None
    if 1 <= ch <= 13:
        return 2407 + 5 * ch
    if ch == 14:
        return 2484
    if 32 <= ch <= 177:
        return 5000 + 5 * ch
    return None


HT20_HALF = 10  # MHz w kazda strone od srodka kanalu


def channel_span(freq):
    """Zakres zajmowany przez kanal HT20 - to on, a nie sama czestotliwosc
    srodkowa, decyduje przy krawedziach przydzialu."""
    return (freq - HT20_HALF, freq + HT20_HALF) if freq else None


def reg_domain_ranges():
    """(kraj, [(od_MHz, do_MHz), ...]) z pierwszego bloku 'iw reg get'. Sluzy
    do sprawdzenia, czy w ustawionym regionie kanal w ogole istnieje: domeny
    europejskie (PL i reszta ETSI) nie obejmuja pasma 5.8 GHz, wiec po ich
    ustawieniu karta przestaje nadawac na kanale 161, a wszystko inne -
    sterownik, tryb monitor, usluga - wyglada dalej poprawnie."""
    code, out = run_tool("iw", "reg", "get")
    if code != 0:
        return None, []

    country, ranges, seen = None, [], False
    for ln in out.splitlines():
        m = re.match(r"\s*country (\S+?):", ln)
        if m:
            if seen:
                break  # kolejny blok (phy#N) to zwykle to samo
            country, seen = m.group(1), True
            continue
        if not seen:
            continue
        m = re.match(r"\s*\((\d+)\s*-\s*(\d+)\s*@", ln)
        if m:
            ranges.append((int(m.group(1)), int(m.group(2))))
    return country, ranges


def ping_stats(ip, count=5, timeout=2):
    """Ping idzie przez tunel wfb, czyli fizycznie przez karte RTL8812AU."""
    code, out = run(["ping", "-c", str(count), "-W", str(timeout), ip], timeout=count * timeout + 5)
    loss_m = re.search(r"(\d+)% packet loss", out)
    rtt_m = re.search(r"=\s*[\d.]+/([\d.]+)/", out)
    loss = loss_m.group(1) if loss_m else "?"
    avg = rtt_m.group(1) if rtt_m else None
    return code, loss, avg


def ip_addresses():
    """[(interfejs, adres/maska)] - wszystkie adresy IPv4 poza loopbackiem.
    Zeby na jednym ekranie bylo widac, pod jakim adresem to Pi jest w sieci
    lokalnej (do ssh) i czy tunel wfb dostal swoj adres."""
    code, out = run(["ip", "-4", "-brief", "addr", "show"])
    if code != 0:
        return []
    result = []
    for ln in out.splitlines():
        fields = ln.split()
        if len(fields) >= 3 and fields[0] != "lo":
            result.extend((fields[0], addr) for addr in fields[2:] if "/" in addr)
    return result


def wfb_ng_version():
    code, out = run(["dpkg-query", "-W", "-f=${Version}", "wfb-ng"])
    return out.strip() if code == 0 and out.strip() else "?"


def check_ssh(ip, port=SSH_PORT, timeout=3):
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


# ------------------------- statystyki lacza (API wfb-ng) -------------------------

# Liczniki jadra (rx/tx pakietow) mowia tylko "cos leci". O tym, JAK leci -
# jaki jest sygnal, ile pakietow poszlo w kosmos, ile uratowal FEC - wie
# wylacznie wfb-ng. Wystawia te dane na lokalnym porcie TCP; to samo zrodlo,
# z ktorego korzysta wfb-cli. Format: ramka = 4 bajty dlugosci (big-endian)
# + slownik msgpack, komplet raz na sekunde.
#
# UWAGA: port jest INNY DLA KAZDEGO PROFILU (dron 8002, gs 8003). Pytanie
# o settings.common.cli_port zwracalo jedna wspolna wartosc, wiec na dronie
# ekran testu pukal pod port stacji naziemnej, dostawal "connection refused"
# i pokazywal "brak sygnalu" mimo dzialajacego lacza. Stad ta stala jest tylko
# ostatnia deska ratunku, a nie zrodlem prawdy.
WFB_CLI_PORT_DEFAULT = 8003
# Porty, ktore wfb-ng rozdaje profilom. Sluza wylacznie jako lista do
# sprawdzenia sonda, gdy ani journal, ani config nie daja odpowiedzi - bez nich
# zle podana wartosc z configu nie mialaby czym zostac nadpisana.
WFB_CLI_PORT_CANDIDATES = (8002, 8003)
_cli_port_cache = {"val": None}


def _cli_port_from_journal():
    """Port, ktory usluga NAPRAWDE otworzyla - wypisuje go do journala przy
    kazdym starcie. To jedyne zrodlo, ktore nie zalezy od tego, jak dana wersja
    wfb-ng liczy porty z configu."""
    code, out = run(["journalctl", "-u", f"wifibroadcast@{ROLE}", "-b",
                     "--no-pager", "-o", "cat"], timeout=15)
    if code != 0:
        return None
    found = re.findall(r"MsgPackAPIFactory starting on (\d+)", out)
    return int(found[-1]) if found else None


def _cli_port_from_settings():
    """To samo pytanie co wczesniej, ale o sekcje NASZEJ roli, z zejsciem na
    wspolna. getattr na None oddaje None, wiec brak sekcji profilu nie boli."""
    code, out = run(["python3", "-c",
                     "from wfb_ng.conf import settings; "
                     "p = getattr(settings, %r, None); "
                     "v = getattr(p, 'cli_port', None); "
                     "print(v or settings.common.cli_port)" % ROLE], timeout=30)
    if code != 0:
        return None
    # run() sklei stdout ze stderr, wiec bierzemy ostatnia linie bedaca sama
    # liczba - ewentualne ostrzezenia importu nie podmienia portu
    for ln in reversed(out.splitlines()):
        if ln.strip().isdigit():
            return int(ln.strip())
    return None


def _port_answers(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
        return True
    except OSError:
        return False


def wfb_cli_port():
    """Port API wfb-ng dla naszej roli. Kandydatow zbieramy z trzech zrodel,
    ale rozstrzyga ten, ktory faktycznie przyjmuje polaczenie - zgadywanie
    z configu juz raz kosztowalo nas 'brak sygnalu' przy dzialajacym linku."""
    if _cli_port_cache["val"]:
        return _cli_port_cache["val"]

    candidates = []
    for getter in (_cli_port_from_journal, _cli_port_from_settings):
        port = getter()
        if port and port not in candidates:
            candidates.append(port)
    for port in (WFB_CLI_PORT_DEFAULT,) + WFB_CLI_PORT_CANDIDATES:
        if port not in candidates:
            candidates.append(port)

    live = next((p for p in candidates if _port_answers(p)), None)
    if live is None:
        # Usluga moze dopiero wstawac - oddajemy najlepszy typ, ale NIE
        # zapamietujemy go, zeby nastepne wywolanie sprobowalo jeszcze raz.
        return candidates[0]
    _cli_port_cache["val"] = live
    return live


def _to_text(value):
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else value


def _mget(mapping, name, default=None):
    """Wartosc z rozpakowanego msgpacka. Starsze wersje biblioteki oddaja
    klucze jako bajty, nowsze jako tekst - sprawdzamy oba warianty."""
    if not isinstance(mapping, dict):
        return default
    if name in mapping:
        return mapping[name]
    return mapping.get(name.encode(), default)


def _num(value, default=0):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return value


def _flatten(value):
    if not isinstance(value, (list, tuple)):
        return [value]
    out = []
    for item in value:
        out.extend(_flatten(item))
    return out


def _unpack_msg(payload):
    """use_list=False jest tu istotne: klucze statystyk anten to krotki, a
    rozpakowane do list byly by niehaszowalne i cala wiadomosc padalaby przy
    skladaniu slownika. Kolejne warianty argumentow to ustepstwo dla starszych
    wersji msgpacka, ktore ich jeszcze nie znaja."""
    import msgpack
    for kwargs in ({"strict_map_key": False, "use_list": False, "raw": False},
                   {"use_list": False, "raw": False},
                   {"use_list": False}):
        try:
            return msgpack.unpackb(payload, **kwargs)
        except TypeError:
            continue
    return None


def rx_packets(msg, name):
    """Licznik ze statystyk: (w ostatniej sekundzie, lacznie). wfb-ng podaje
    pare [przyrost_w_okresie, suma], starsze wersje samo pojedyncze liczby."""
    value = _mget(_mget(msg, "packets") or {}, name)
    if isinstance(value, (list, tuple)):
        cur = _num(value[0]) if len(value) > 0 else 0
        return cur, (_num(value[1]) if len(value) > 1 else cur)
    return _num(value), _num(value)


# 802.11n, jeden strumien przestrzenny: modulacja, sprawnosc kodowania i
# predkosc PHY w Mbit/s dla 20 i 40 MHz przy dlugim i krotkim odstepie
# ochronnym (GI). Im wyzszy MCS, tym gestsza modulacja: wiecej Mbit/s, ale
# potrzeba mocniejszego sygnalu - stad ma sens ogladanie tego obok RSSI.
MCS_TABLE = {
    0: ("BPSK", "1/2", 6.5, 7.2, 13.5, 15.0),
    1: ("QPSK", "1/2", 13.0, 14.4, 27.0, 30.0),
    2: ("QPSK", "3/4", 19.5, 21.7, 40.5, 45.0),
    3: ("16-QAM", "1/2", 26.0, 28.9, 54.0, 60.0),
    4: ("16-QAM", "3/4", 39.0, 43.3, 81.0, 90.0),
    5: ("64-QAM", "2/3", 52.0, 57.8, 108.0, 120.0),
    6: ("64-QAM", "3/4", 58.5, 65.0, 121.5, 135.0),
    7: ("64-QAM", "5/6", 65.0, 72.2, 135.0, 150.0),
}


def bw_mhz(value, default=20):
    """Szerokosc kanalu w MHz. Nowsze wfb-ng podaje ja wprost, starsze surowym
    kodem z radiotapu (0 = 20 MHz, 1 = 40 MHz, 2/3 = polowki 40 MHz). Napisy
    tez sa w porzadku - z linii polecen wfb_tx wszystko przychodzi tekstem."""
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    if v >= 20:
        return v
    return 40 if v == 1 else 20


def mcs_info(mcs, bandwidth=20, short_gi=False):
    """(opis modulacji, predkosc PHY w Mbit/s). MCS 8-15 to te same modulacje
    puszczone dwoma strumieniami przestrzennymi - wtedy predkosc sie podwaja."""
    if mcs is None:
        return "?", None
    try:
        mcs = int(mcs)
    except (TypeError, ValueError):
        return "?", None
    entry = MCS_TABLE.get(mcs % 8)
    if entry is None or mcs < 0:
        return f"MCS {mcs}", None
    mod, coding, r20, r20s, r40, r40s = entry
    streams = mcs // 8 + 1
    rate = ((r40s if short_gi else r40) if bw_mhz(bandwidth) >= 40
            else (r20s if short_gi else r20))
    desc = f"MCS {mcs} = {mod} {coding}"
    if streams > 1:
        desc += f" x{streams} strumienie"
    return desc, rate * streams


_tx_params_cache = {"t": 0.0, "val": None}


def tx_radio_params(max_age=5.0):
    """Parametry nadawania odczytane z linii polecen dzialajacych wfb_tx -
    czyli czym NAPRAWDE nadajemy w tej chwili. Config moglby klamac: zmiana
    w pliku dziala dopiero po restarcie uslugi. wfb_tx dostaje je flagami:
    -M mcs, -B szerokosc, -G odstep ochronny, -S STBC, -L LDPC, -k/-n FEC,
    -p port radiowy. Wynik cache'owany, bo przejscie po calym /proc jest
    zbyt drogie na kazde przerysowanie ekranu."""
    now = time.monotonic()
    if _tx_params_cache["val"] is not None and now - _tx_params_cache["t"] < max_age:
        return _tx_params_cache["val"]

    flags = {"-M": "mcs", "-B": "bw", "-G": "gi", "-S": "stbc", "-L": "ldpc",
             "-k": "fec_k", "-n": "fec_n", "-p": "port"}
    out = []
    for args in proc_cmdlines():
        if not any("wfb_tx" in a for a in args[:2]):
            continue
        info = {}
        for flag, name in flags.items():
            if flag in args:
                idx = args.index(flag)
                if idx + 1 < len(args):
                    info[name] = args[idx + 1]
        if info:
            out.append(info)
    out.sort(key=lambda i: i.get("port", ""))
    _tx_params_cache.update(t=now, val=out)
    return out


# Krotkie podpisy przy skrajnych i srodkowym MCS - reszta ustawia sie miedzy
# nimi, nie ma po co powtarzac tego przy kazdym wierszu.
MCS_HINTS = {
    0: "najwiekszy zasieg, najmniej danych",
    3: "kompromis zasieg / przepustowosc",
    7: "najwiecej danych, najmniejszy zasieg",
}


def mcs_config_sections():
    """{strumien: sekcja configu}, w ktorej ustawiamy mcs_index - tylko dla
    strumieni, ktore ta rola NADAJE (na gs wideo jest tylko odbierane, wiec
    ustawianie mu modulacji nic by nie dalo).

    Bierzemy najbardziej szczegolowy profil strumienia (ostatni na liscie
    'profiles'), bo ten wygrywa przy scalaniu ustawien przez wfb-ng. Gdy
    wfb-ng nie odpowiada, wracamy do domyslnych nazw <rola>_<strumien>."""
    out = {}
    for s in wfb_streams() or []:
        name, profiles = s.get("name"), s.get("profiles") or []
        if not name or not profiles:
            continue
        if "stream_tx" in s and s.get("stream_tx") is None:
            continue  # strumien tylko odbierany
        out[name] = profiles[-1]
    return out or {n: f"{ROLE}_{n}" for n in ("video", "mavlink", "tunnel")}


def current_mcs_setting(sections):
    """MCS wpisany przez nas do configu albo None, czyli "automatycznie".
    None takze wtedy, gdy sekcje maja rozne wartosci - wtedy zadna nie opisuje
    calosci, a i tak obok pokazujemy, czym naprawde nadaje wfb_tx."""
    values = {get_cfg_option(section, "mcs_index") for section in set(sections.values())}
    if len(values) != 1:
        return None
    value = values.pop()
    return int(value) if value and value.isdigit() else None


def apply_mcs_setting(mcs, sections):
    """Zapisuje mcs_index we wszystkich nadawanych strumieniach albo - w trybie
    automatycznym - kasuje nasz wpis, zeby zostalo to, co ustawia sam wfb-ng.
    Zwraca liste ruszonych sekcji."""
    if not CFG_PATH.exists():
        return []
    backup_config_once()
    changed = []
    for section in sorted(set(sections.values())):
        if mcs is None:
            if drop_cfg_option(section, "mcs_index"):
                changed.append(section)
        else:
            set_cfg_option(section, "mcs_index", str(mcs))
            changed.append(section)
    return changed


# ------------------- naprawa utraconych pakietow (FEC tunelu) -------------------
#
# Pakietu, ktory przepadl w powietrzu, nie da sie "naprawic" po fakcie - nikt go
# juz nie ma. Wfb-ng radzi sobie z tym z gory: do kazdych k pakietow danych
# dokłada n-k pakietow nadmiarowych i z dowolnych k odebranych odtwarza cala
# paczke (FEC). Utracony pakiet wraca wiec z nadmiarowosci, o ile bylo jej dosc.
#
# Caly ten modul jest o dobieraniu tego "dosc": im gorszy link, tym wiecej
# nadmiarowosci trzeba wysylac, ale kazdy nadmiarowy pakiet zjada czas antenowy,
# wiec przy czystym linku placi sie za darmo. Stad drabinka poziomow i automat,
# ktory po niej chodzi w gore przy stratach i w dol przy ciszy.

# (k, n, nazwa) - z kazdych n pakietow k niesie dane. n/k to koszt: 1/2 znaczy
# "kazdy pakiet leci dwa razy". Kolejnosc od najtanszego do najmocniejszego -
# indeks na tej liscie jest "poziomem naprawy", ktorym rusza AutoFec.
#
# Poziom 0 to naprawa WYLACZONA: n = k, czyli zero pakietow nadmiarowych. Nic
# nie wraca, za to nic nie zjada czasu antenowego. Przydaje sie do zmierzenia,
# ile gubi samo radio (na wykresie obie krzywe strat leza wtedy na sobie) i przy
# bardzo czystym linku, gdzie nadmiarowosc to czysty koszt.
FEC_LEVELS = [
    (1, 1, "wylaczona"),
    (8, 9, "minimalna"),
    (4, 5, "oszczedna"),
    (2, 3, "srednia"),
    (1, 2, "domyslna wfb-ng"),
    (1, 3, "mocna"),
    (1, 4, "bardzo mocna"),
    (1, 5, "maksymalna"),
]

FEC_OFF_LEVEL = 0

# Poziom, na ktory wracamy przyciskiem "domyslne" - tyle ma tunel i mavlink po
# swiezej instalacji wfb-ng (k=1, n=2).
FEC_DEFAULT_LEVEL = 4

# Najnizszy poziom, na ktory wolno ZEJSC AUTOMATOWI. Wylaczyc naprawe mozna
# recznie, ale automat sam tego nie zrobi: zdjecie calej ochrony zamienia kazda
# nastepna dziure w bezpowrotna strate, a w powietrzu nie ma jak tego cofnac
# szybciej niz przez restart uslugi. W gore z zera automat wyjdzie normalnie.
AUTO_FEC_MIN_LEVEL = 1


def fec_overhead(k, n):
    """Ile razy wiecej pakietow trzeba wyslac niz danych - czyli cena naprawy."""
    return (n / k) if k else 1.0


def fec_off(level):
    """Czy ten poziom to "bez naprawy" - n rowne k, czyli zero nadmiarowosci."""
    k, n, _name = FEC_LEVELS[level]
    return n <= k


def fec_level_txt(level):
    k, n, name = FEC_LEVELS[level]
    if fec_off(level):
        return f"FEC {k}/{n} ({name} - nic nie dokladamy, nic nie wroci)"
    return f"FEC {k}/{n} ({name}, {fec_overhead(k, n):.2f}x pakietow)"


def fec_level_of(k, n):
    """Numer poziomu dla pary (k, n) albo None, gdy w configu siedzi cos spoza
    drabinki - wtedy automat nie ma od czego zaczac i trzeba wybrac recznie."""
    for i, (lk, ln, _name) in enumerate(FEC_LEVELS):
        if (lk, ln) == (k, n):
            return i
    return None


def fec_section():
    """Sekcja configu ze strumieniem tunelu - to w niej ustawia sie fec_k/fec_n
    dla tego, co NADAJEMY w gore/dol tunelu.

    Tunel jest dwukierunkowy i kazda strona nadaje wlasnym FEC, wiec ten wpis
    dotyczy tylko naszego kierunku. Druga strona ma swoj wlasny i moze miec
    inny - odbiornik czyta k/n z pakietu sesyjnego, wiec nie trzeba tego
    uzgadniac tak jak kanalu."""
    return mcs_config_sections().get("tunnel", f"{ROLE}_tunnel")


def current_fec_setting(section=None):
    """(k, n) wpisane przez nas do configu albo None, gdy nie ma wpisu i zostaje
    to, co ustawia sam wfb-ng."""
    section = section or fec_section()
    k, n = get_cfg_option(section, "fec_k"), get_cfg_option(section, "fec_n")
    if not (k and n and k.isdigit() and n.isdigit()):
        return None
    return int(k), int(n)


def apply_fec_setting(k, n, section=None):
    """Zapisuje fec_k/fec_n dla tunelu albo - gdy k jest None - kasuje nasz wpis
    i zostawia ustawienia wfb-ng. Zwraca ruszona sekcje albo None.

    Samo zapisanie nie wystarczy: wfb_tx czyta config przy starcie, wiec
    wolajacy musi zrestartowac usluge (i wie o tym, bo restart zrywa link)."""
    if not CFG_PATH.exists():
        return None
    backup_config_once()
    section = section or fec_section()
    if k is None:
        dropped = drop_cfg_option(section, "fec_k")
        dropped = drop_cfg_option(section, "fec_n") or dropped
        return section if dropped else None
    set_cfg_option(section, "fec_k", str(k))
    set_cfg_option(section, "fec_n", str(n))
    return section


def tunnel_tx_port():
    """Port radiowy, na ktorym nadajemy tunel ('stream_tx' strumienia) albo
    None. Sluzy do rozpoznania WLASCIWEGO procesu wfb_tx - kazdy strumien ma
    swoj, a wideo ma zwykle zupelnie inne FEC niz tunel."""
    for s in wfb_streams() or []:
        if s.get("name") == "tunnel":
            port = s.get("stream_tx")
            return str(port) if port is not None else None
    return None


def live_tunnel_fec():
    """(k, n) faktycznie uzywane przez wfb_tx tunelu albo None. Czytamy to
    z linii polecen procesu, a nie z configu - po to, zeby bylo widac, gdy wpis
    nie zadzialal (np. wfb-ng wzielo ustawienie z innej sekcji albo usluga
    jeszcze nie zostala zrestartowana po zapisie)."""
    def pair(tx):
        k, n = tx.get("fec_k"), tx.get("fec_n")
        if k and n and str(k).isdigit() and str(n).isdigit():
            return int(k), int(n)
        return None

    txs = tx_radio_params()
    port = tunnel_tx_port()
    if port is not None:
        for tx in txs:
            if str(tx.get("port")) == port:
                return pair(tx)
        return None
    # Bez listy strumieni nie ma po czym rozpoznac tunelu; zgadywanie po
    # kolejnosci portow trafialo by czasem w wideo, a to zupelnie inne FEC.
    return pair(txs[0]) if len(txs) == 1 else None


def tx_modulation_txt(tx):
    """Opis nadawania z wpisu tx_radio_params(), rozbity na dwa kawalki: sama
    modulacja i ustawienia kodowania. Ekran pokazuje je w dwoch wierszach, bo
    w jednym nie mieszcza sie na 80 kolumnach; plik zapisu je skleja."""
    short_gi = str(tx.get("gi", "")).lower().startswith("s")
    desc, rate = mcs_info(tx.get("mcs"), tx.get("bw"), short_gi)
    main = f"{desc}   {bw_mhz(tx.get('bw'))} MHz   GI {'krotki' if short_gi else 'dlugi'}"
    extra = f"STBC {tx.get('stbc', '?')}  LDPC {tx.get('ldpc', '?')}"

    k, n = tx.get("fec_k"), tx.get("fec_n")
    if k and n:
        extra += f"   FEC {k}/{n}"
        try:
            # z kazdych n wyslanych pakietow k niesie dane - reszta to
            # nadmiarowosc, ktora ratuje transmisje, ale zjada pasmo
            if rate:
                extra += f"  ->  ~{rate * int(k) / int(n):.1f} Mbit/s uzytecznych"
        except (TypeError, ValueError, ZeroDivisionError):
            pass
    elif rate:
        extra += f"   ~{rate:.1f} Mbit/s (PHY)"
    return main, extra


def antenna_rows(msg, nics):
    """Statystyki kazdej anteny z jednej wiadomosci 'rx' jako slowniki:
    etykieta, pakiety/s, RSSI i SNR (min, sr, max), czestotliwosc, MCS
    i szerokosc kanalu.

    Kluczem statystyk anteny jest u wfb-ng krotka (czestotliwosc, MCS,
    szerokosc, id anteny), w starszych wersjach samo id. Id koduje karte
    w gornym bajcie (numer wlan w kolejnosci przekazanej do wfb_rx) i numer
    anteny w dolnym - stad da sie podpiac nazwe interfejsu."""
    stats = _mget(msg, "rx_ant_stats") or {}
    items = stats.items() if isinstance(stats, dict) else stats
    rows = []
    for pair in items:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            continue
        key, val = pair
        nums = [_num(x) for x in _flatten(key)]
        ant_id = int(nums[-1]) if nums else 0
        freq = int(nums[0]) if nums and nums[0] > 1000 else None
        # (czestotliwosc, MCS, szerokosc, id) - MCS i szerokosc tylko wtedy,
        # gdy klucz naprawde ma cztery pola; starsze wersje daja samo id
        mcs = int(nums[1]) if len(nums) >= 4 else None
        bw = bw_mhz(nums[2]) if len(nums) >= 4 else None
        val = list(val) if isinstance(val, (list, tuple)) else [val]
        count = _num(val[0]) if val else 0
        rssi = tuple(_num(x) for x in val[1:4]) if len(val) >= 4 else None
        snr = tuple(_num(x) for x in val[4:7]) if len(val) >= 7 else None
        idx, ant = ant_id >> 8, ant_id & 0xFF
        label = nics[idx] if 0 <= idx < len(nics) else f"karta{idx}"
        rows.append({"label": f"{label} ant{ant}", "count": count, "rssi": rssi,
                     "snr": snr, "freq": freq, "mcs": mcs, "bw": bw})
    rows.sort(key=lambda r: r["label"])
    return rows


def tx_wlan_rows(msg, nics):
    """[(etykieta karty, wstrzykniete/s, odrzucone/s, opoznienie ms)] - czyli
    ktora karta faktycznie nadaje i czy sterownik nadaza. wfb-ng trzyma to
    w polu 'latency' pod numerem wlan; wartosci czasu sa w mikrosekundach."""
    stats = _mget(msg, "latency") or {}
    items = stats.items() if isinstance(stats, dict) else stats
    rows = []
    for pair in items:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            continue
        key, val = pair
        nums = [_num(x) for x in _flatten(key)]
        idx = int(nums[-1]) if nums else 0
        val = list(val) if isinstance(val, (list, tuple)) else [val]
        if len(val) < 2:
            continue
        injected, dropped = _num(val[0]), _num(val[1])
        lat_avg = _num(val[3]) / 1000.0 if len(val) >= 4 else None
        label = nics[idx] if 0 <= idx < len(nics) else f"karta{idx}"
        rows.append((label, injected, dropped, lat_avg))
    rows.sort(key=lambda r: r[0])
    return rows


class WfbStatsProbe:
    """Statystyki z API wfb-ng czytane w watku w tle.

    Polaczenie trzeba trzymac otwarte, a komplet danych przychodzi raz na
    sekunde - ekran testu odrysowuje sie czesciej i nie moze na to czekac,
    stad osobny watek. Gdy usluga sie zrestartuje, watek po prostu laczy sie
    ponownie, wiec test mozna zostawic wlaczony przez caly czas grzebania
    w konfiguracji."""

    def __init__(self):
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._msgs = {}
        self._error = "laczenie z wfb-ng..."
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def close(self):
        self._stop.set()

    def snapshot(self):
        with self._lock:
            return dict(self._msgs), self._error

    def _set_error(self, msg):
        with self._lock:
            self._error = msg

    def _loop(self):
        while not self._stop.is_set():
            self._session()
            self._stop.wait(2.0)

    def _session(self):
        try:
            import msgpack  # noqa: F401 - sprawdzamy tylko dostepnosc
        except ImportError:
            self._set_error("brak modulu python3-msgpack - nie odczytam statystyk wfb-ng")
            self._stop.wait(10)
            return

        port = wfb_cli_port()
        try:
            sock = socket.create_connection(("127.0.0.1", port), timeout=2)
        except OSError as e:
            self._set_error(f"API wfb-ng (127.0.0.1:{port}) nie odpowiada - usluga nie dziala? [{e}]")
            return

        sock.settimeout(0.5)
        buf = b""
        try:
            while not self._stop.is_set():
                try:
                    chunk = sock.recv(65536)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not chunk:
                    break  # usluga zamknela polaczenie - petla sprobuje jeszcze raz
                buf = self._consume(buf + chunk)
        finally:
            sock.close()
        if not self._stop.is_set():
            self._set_error("polaczenie z API wfb-ng zerwane - usluga sie restartuje?")

    def _consume(self, buf):
        while len(buf) >= 4:
            size = struct.unpack(">I", buf[:4])[0]
            if size > 8 * 1024 * 1024:
                self._set_error(f"port {wfb_cli_port()} odpowiada nieznanym protokolem")
                return b""
            if len(buf) < 4 + size:
                break
            payload, buf = buf[4:4 + size], buf[4 + size:]
            try:
                msg = _unpack_msg(payload)
            except Exception:
                continue  # jedna zepsuta ramka nie moze zabic calego testu
            if not isinstance(msg, dict):
                continue
            mtype = _to_text(_mget(msg, "type"))
            if mtype in ("rx", "tx"):
                with self._lock:
                    self._msgs[(mtype, _to_text(_mget(msg, "id")))] = msg
                    self._error = None
        return buf


class PingProbe:
    """Ping do drugiej strony, tez w watku w tle - jedna proba trwa okolo
    sekundy, a ekran ma sie odswiezac plynnie. Zlicza tez sumy od poczatku
    testu: przy sprawdzaniu zasiegu wazniejsze od chwilowej wartosci jest to,
    ile pakietow przepadlo przez caly przelot."""

    def __init__(self, ip, count=3):
        self.ip = ip
        self.count = count
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._rtt = None        # (min, sr, max) z ostatniej proby
        self._last_loss = None  # % z ostatniej proby
        self._sent = 0
        self._recv = 0
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def close(self):
        self._stop.set()

    def reset(self):
        with self._lock:
            self._sent = self._recv = 0

    def snapshot(self):
        """(rtt, utrata w ostatniej probie %, utrata od poczatku %, wyslane, odebrane)"""
        with self._lock:
            total = (100.0 * (self._sent - self._recv) / self._sent) if self._sent else None
            return self._rtt, self._last_loss, total, self._sent, self._recv

    def _loop(self):
        while not self._stop.is_set():
            _, out = run(["ping", "-c", str(self.count), "-i", "0.3", "-W", "1", self.ip],
                         timeout=self.count + 6)
            m = re.search(r"(\d+) packets transmitted, (\d+)[^,]*received", out)
            r = re.search(r"=\s*([\d.]+)/([\d.]+)/([\d.]+)", out)
            with self._lock:
                if m:
                    sent, recv = int(m.group(1)), int(m.group(2))
                    self._sent += sent
                    self._recv += recv
                    self._last_loss = 100.0 * (sent - recv) / sent if sent else None
                else:
                    self._last_loss = None
                self._rtt = (float(r.group(1)), float(r.group(2)), float(r.group(3))) if r else None
            self._stop.wait(0.4)


# ------------------------- test obciazeniowy -------------------------

# Wielkosc pakietu zblizona do tego, co realnie wychodzi z kodera H.264 przez
# wfb-ng. Wieksze nie zmieszcza sie w ramce radiowej po dolozeniu naglowkow,
# mniejsze zawyzalyby narzut na pakiet i zanizaly wynik.
LOAD_PACKET_BYTES = 1200
LOAD_MAGIC = b"WFBL"  # zeby nie liczyc cudzych datagramow jako swoich
LOAD_DEFAULT_MBIT = 8.0
LOAD_HEAD = len(LOAD_MAGIC) + 8  # magic + 8-bajtowy numer sekwencyjny


class LoadSender:
    """Generator ruchu udajacy strumien wideo.

    Po co to w ogole jest: ekran testu mierzy to, co akurat leci przez lacze,
    a bez kamery leci tylko ping - okolo 20 pakietow na sekunde. Przy takim
    ruchu JEDEN zgubiony pakiet to kilka procent strat, a bloki FEC (dla wideo
    8/12) nie maja sie z czego zapelnic i lecza na fec_timeout. Zadna liczba
    zmierzona na pustym laczu nie mowi nic o tym, jak zachowa sie obraz.

    Wpychamy wiec ruch dokladnie tam, gdzie trafialby obraz z kamery, i w tym
    samym tempie."""

    def __init__(self, port=VIDEO_UDP_PORT, mbit=LOAD_DEFAULT_MBIT,
                 size=LOAD_PACKET_BYTES):
        self.port = port
        self.mbit = mbit
        self.size = max(LOAD_HEAD, size)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._sent = 0
        self._bytes = 0
        self._late = 0      # ile razy nie nadazylismy z tempem
        self._error = None
        self._sock = None
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        except OSError as e:
            self._error = f"nie moge otworzyc gniazda: {e}"
            return self
        self._thread.start()
        return self

    def close(self):
        self._stop.set()

    def snapshot(self):
        """(wyslane, bajty, ile razy nie nadazylismy, blad)"""
        with self._lock:
            return self._sent, self._bytes, self._late, self._error

    def _loop(self):
        addr = ("127.0.0.1", self.port)
        pad = bytes(self.size - LOAD_HEAD)
        interval = self.size * 8.0 / (self.mbit * 1e6)  # sekund na pakiet
        seq = 0
        late = 0
        next_at = time.monotonic()

        while not self._stop.is_set():
            now = time.monotonic()
            if now < next_at:
                # Krotki sen zamiast dlugiego: wideo tez leci rownomiernym
                # strumieniem, a nie seriami, i tempo ma to odwzorowac.
                self._stop.wait(min(0.002, next_at - now))
                continue

            # Nadrabiamy zaleglosc, ale najwyzej kilkadziesiat pakietow naraz.
            # Bez tego po chwilowym zatkaniu poszlaby seria, ktora sama z siebie
            # wywolalaby straty - i test mierzylby wlasny artefakt zamiast lacza.
            burst = 0
            while next_at <= now and burst < 64 and not self._stop.is_set():
                try:
                    self._sock.sendto(LOAD_MAGIC + struct.pack(">Q", seq) + pad, addr)
                except OSError as e:
                    with self._lock:
                        self._error = f"blad wysylania: {e}"
                    return
                seq += 1
                burst += 1
                next_at += interval

            if next_at < now:
                next_at = now  # nie nadazamy - odliczamy od nowa
                late += 1

            # Licznik pod zamkiem raz na serie, a nie na pakiet: przy kilku
            # tysiacach pakietow na sekunde samo zamykanie kosztowaloby wiecej
            # niz wysylka.
            if burst:
                with self._lock:
                    self._sent += burst
                    self._bytes += burst * self.size
                    self._late = late

        try:
            self._sock.close()
        except OSError:
            pass


class LoadReceiver:
    """Liczy strumien testowy tam, gdzie wfb-ng oddaje wideo.

    Dziury w numeracji to straty PO naprawie FEC - czyli dokladnie to, co
    zobaczylby dekoder obrazu. To jest liczba, o ktora w tym tescie chodzi;
    liczniki wfb-ng mowia, co sie dzialo na radiu, a ta mowi, co z tego
    wyszlo na wyjsciu."""

    def __init__(self, port=VIDEO_UDP_PORT):
        self.port = port
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._first = None
        self._last = None
        self._got = 0
        self._bytes = 0
        self._reordered = 0
        self._error = None
        self._sock = None
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.bind(("127.0.0.1", self.port))
            self._sock.settimeout(0.3)
        except OSError as e:
            self._error = (f"port {self.port} zajety - odtwarzacz obrazu juz na nim "
                           f"slucha? [{e}]")
            return self
        self._thread.start()
        return self

    def close(self):
        self._stop.set()
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass

    def reset(self):
        with self._lock:
            self._first = self._last = None
            self._got = self._bytes = self._reordered = 0

    def snapshot(self):
        """(odebrane, bajty, utracone, utrata %, przestawione, blad)"""
        with self._lock:
            if self._first is None:
                return 0, 0, 0, None, 0, self._error
            span = self._last - self._first + 1
            lost = max(0, span - self._got)
            pct = (100.0 * lost / span) if span else None
            return self._got, self._bytes, lost, pct, self._reordered, self._error

    def _loop(self):
        while not self._stop.is_set():
            try:
                data, _addr = self._sock.recvfrom(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            # Cudzy ruch na tym porcie (np. prawdziwy strumien z kamery) nie
            # moze zaburzac numeracji - liczymy wylacznie wlasne pakiety.
            if len(data) < LOAD_HEAD or not data.startswith(LOAD_MAGIC):
                continue
            seq = struct.unpack(">Q", data[len(LOAD_MAGIC):LOAD_HEAD])[0]
            with self._lock:
                self._got += 1
                self._bytes += len(data)
                if self._first is None or seq < self._first:
                    self._first = seq
                if self._last is None or seq > self._last:
                    self._last = seq
                elif self._last is not None and seq < self._last:
                    # Przestawienie kolejnosci to nie strata - pakiet doszedl.
                    # Liczymy je osobno, bo dla dekodera obrazu tez sa kosztem.
                    self._reordered += 1


# Sila sygnalu w skali 1-10. Punkty zaczepienia sa dobrane tak, zeby liczba
# nigdy nie przeczyla kolorowi z rssi_grade: 7 wypada dokladnie na granicy
# dobry/slaby (-65 dBm), a 4 na granicy slaby/na-granicy-zasiegu (-75 dBm).
# Miedzy nimi interpolujemy liniowo, bo skokowa skala pokazywalaby to samo
# "8" przy -52 i przy -64 dBm, a to sa dwa rozne swiaty przy ustawianiu anteny.
RSSI_SCALE = ((-95, 1.0), (-75, 3.5), (-65, 6.5), (-50, 8.5), (-40, 10.0))


def rssi_score(rssi):
    """Sila sygnalu 1-10 albo None, gdy nic nie przychodzi. Liczba zamiast
    slowa, bo przy przestawianiu anteny latwiej porownac "8 czy 9" niz dwa
    razy to samo "dobry"."""
    if rssi is None:
        return None
    if rssi <= RSSI_SCALE[0][0]:
        return 1
    for (x0, y0), (x1, y1) in zip(RSSI_SCALE, RSSI_SCALE[1:]):
        if rssi <= x1:
            # +0.5 zamiast round(): round() zaokragla polowki do PARZYSTEJ,
            # wiec round(6.5) daje 6, a round(8.5) daje 8 - a punkty zaczepienia
            # skali leza dokladnie na polowkach i wtedy liczba przeczyla kolorowi
            # (-65 dBm: kolor "ok", sila 6, czyli z zakresu "warn").
            return int(y0 + (y1 - y0) * (rssi - x0) / (x1 - x0) + 0.5)
    return 10


# Progi z praktyki dla 8812AU: powyzej -50 dBm karty sa praktycznie obok
# siebie, ponizej -75 dBm zaczynaja sie zrywy obrazu. Kolor zostaje na tych
# progach, zmienia sie tylko opis - zamiast przymiotnika idzie liczba.
def rssi_grade(rssi):
    if rssi is None:
        return None, "brak sygnalu"
    if rssi >= -65:
        status = "ok"
    elif rssi >= -75:
        status = "warn"
    else:
        status = "fail"
    return status, f"{rssi_score(rssi)}/10"


def loss_grade(pct):
    if pct is None:
        return None, "?"
    if pct < 0.5:
        return "ok", "znikome"
    if pct < 3:
        return "warn", "zauwazalne"
    return "fail", "duze"


# Ile trzeba uzbierac, zeby procent strat cokolwiek znaczyl. Ponizej tego progu
# ocena w naglowku jest None i naglowek opiera sie na samym sygnale - lepiej nie
# oceniac wcale niz oceniac z kilkunastu pakietow. Progi biora sie z mianownika:
# przy 20 pkt/s jeden zgubiony pakiet to 5%, wiec dopoki nie uzbiera sie ich
# kilkuset, kazda dziura wyrzuca ocene na "duze". Przy pingach jest jeszcze
# gorzej, bo probka ma tylko 3 pakiety i moze dac wylacznie 0/33/67/100%.
GRADE_MIN_PACKETS = 200  # ~10 s przy typowym ruchu tunelu
GRADE_MIN_PINGS = 15     # 5 prob po 3 pakiety, czyli okolo 7 s


def snr_grade(snr):
    if snr is None:
        return None, "?"
    if snr >= 20:
        return "ok", "czysto"
    if snr >= 10:
        return "warn", "szum blisko sygnalu"
    return "fail", "sygnal tonie w szumie"


def worst_status(statuses):
    for level in ("fail", "warn", "ok"):
        if level in statuses:
            return level
    return None


def mbit(bytes_per_s):
    return bytes_per_s * 8 / 1_000_000.0


# ------------------------- skan kanalow -------------------------

# 2.4 GHz: w PL (i calym ETSI) legalne sa kanaly 1-13. 5 GHz: tylko te bez
# obowiazku wykrywania radaru (DFS) - na kanalach 52-140 nie wolno tak po
# prostu nadawac, wiec ich nie proponujemy. Co z tego jest naprawde dozwolone
# w ustawionym regionie, sprawdzamy i tak przez 'iw reg get'.
CHANNELS_24 = list(range(1, 14))
CHANNELS_5 = [36, 40, 44, 48, 149, 153, 157, 161, 165]


def channel_allowed(freq, ranges=None):
    """Czy caly kanal HT20 miesci sie w pasmie dozwolonym w tym regionie."""
    ranges = reg_domain_ranges()[1] if ranges is None else ranges
    span = channel_span(freq)
    if not span or not ranges:
        return None  # nie wiadomo - nie udajemy, ze wiemy
    return any(lo <= span[0] and span[1] <= hi for lo, hi in ranges)


def iw_survey(nic):
    """{czestotliwosc MHz: (aktywny_ms, zajety_ms, szum_dBm)} z 'iw survey dump'.
    To jedyny pomiar zajetosci pasma dostepny w trybie monitor - zwykly skan
    (iw scan) w tym trybie nie przechodzi."""
    code, out = run_tool("iw", "dev", nic, "survey", "dump", timeout=15)
    if code != 0:
        return {}

    result = {}
    freq = active = busy = noise = None

    def flush():
        if freq is not None:
            result[freq] = (active, busy, noise)

    for ln in out.splitlines():
        m = re.search(r"frequency:\s+(\d+) MHz", ln)
        if m:
            flush()
            freq, active, busy, noise = int(m.group(1)), None, None, None
            continue
        m = re.search(r"noise:\s+(-?\d+)", ln)
        if m:
            noise = int(m.group(1))
            continue
        m = re.search(r"channel active time:\s+(\d+)", ln)
        if m:
            active = int(m.group(1))
            continue
        m = re.search(r"channel busy time:\s+(\d+)", ln)
        if m:
            busy = int(m.group(1))
    flush()
    return result


def set_nic_channel(nic, channel):
    """Przestawia karte na kanal. Niektore wersje sterownika przyjmuja tylko
    czestotliwosc, stad druga proba."""
    code, out = run_tool("iw", "dev", nic, "set", "channel", str(channel), timeout=10)
    if code == 0:
        return True, ""
    freq = channel_freq(channel)
    if freq:
        code, out = run_tool("iw", "dev", nic, "set", "freq", str(freq), timeout=10)
    return code == 0, out.strip()


def scan_channels(nic, channels, dwell=1.2, on_result=None):
    """Przechodzi po kanalach i mierzy, ile sie na kazdym dzieje.

    Dla kazdego kanalu: procent czasu, w ktorym pasmo bylo zajete przez cudze
    transmisje, poziom szumu i ile obcych ramek wpadlo na karte. Liczniki
    survey sa narastajace, wiec bierzemy roznice dwoch odczytow - inaczej
    pierwszy kanal wygladalby na najbardziej zatloczony tylko dlatego, ze
    karta siedziala na nim najdluzej.

    UWAGA: przez caly skan karta jest poza kanalem linku, czyli polaczenia
    nie ma. Kanal wyjsciowy przywraca wolajacy (patrz channel_scan_screen)."""
    results = []
    for channel in channels:
        freq = channel_freq(channel)
        entry = {"channel": channel, "freq": freq}
        ok, err = set_nic_channel(nic, channel)
        if not ok:
            entry["error"] = err[:60] or "karta nie przyjmuje tego kanalu"
        else:
            before = iw_survey(nic).get(freq)
            rx0 = nic_counters(nic)[0]
            time.sleep(dwell)
            after = iw_survey(nic).get(freq)
            rx1 = nic_counters(nic)[0]

            entry["pps"] = max(0.0, (rx1 - rx0) / dwell)
            entry["noise"] = after[2] if after else None
            if before and after and None not in (before[0], before[1], after[0], after[1]):
                d_active = after[0] - before[0]
                d_busy = after[1] - before[1]
                if d_active > 0:
                    entry["busy"] = min(100.0, 100.0 * d_busy / d_active)
        results.append(entry)
        if on_result:
            on_result(entry)
    return results


def rank_channels(results):
    """Od najlepszego: najmniej zajete pasmo, przy remisie nizszy szum, a na
    koncu mniej obcych ramek."""
    def key(r):
        return (r["busy"] if r.get("busy") is not None else 999.0,
                r["noise"] if r.get("noise") is not None else 0,
                r.get("pps", 0.0))
    return sorted([r for r in results if "error" not in r], key=key)


# ------------------------- automatyczny dobor kanalu -------------------------

# Port sterowania w tunelu wfb. Tryb automatyczny musi uzgadniac skoki
# z druga strona, bo kanal MUSI byc po obu stronach ten sam - inaczej skok
# to gwarantowana utrata linku, a nie jego ratowanie.
# --- automatyczna naprawa pakietow w tunelu (dobor FEC) ---
# Powyzej tylu procent strat NIEODRATOWANYCH dokladamy nadmiarowosci. Prog jest
# nizszy niz AUTO_BAD_LOSS od kanalu, bo naprawa jest tania i ma zadzialac
# ZANIM link nadaje sie tylko do ucieczki na inny kanal.
AUTO_FEC_BAD_LOSS = 1.0
# Ponizej tylu procent strat PRZED naprawa nadmiarowosc jest zbedna - schodzimy
# w dol i oddajemy czas antenowy. Patrzymy na "przed", a nie na "po": po
# naprawie zawsze jest zero i automat schodzil by w dol az do pierwszych strat.
AUTO_FEC_GOOD_LOSS = 0.2
AUTO_FEC_BAD_SECONDS = 6.0    # tyle musi byc zle, zeby dolozyc nadmiarowosci
AUTO_FEC_GOOD_SECONDS = 90.0  # tyle musi byc dobrze, zeby ja zdjac
# Kazda zmiana to restart uslugi, czyli kilka sekund bez obrazu i telemetrii -
# wiec miedzy zmianami musi minac wyraznie wiecej czasu niz trwa sam restart.
AUTO_FEC_COOLDOWN = 45.0
# Co ile sekund mowimy drugiej stronie, ile od niej gubimy. To ONA na tej
# podstawie dobiera swoje FEC - patrz AutoFec.
AUTO_FEC_REPORT_EVERY = 2.0
# Po tylu sekundach ciszy raporty drugiej strony sa nieaktualne i wracamy do
# oceny po wlasnym odbiorze.
AUTO_FEC_PEER_STALE = 12.0

AUTO_PORT = 14570
# Kanaly 2.4 GHz maksymalnie od siebie oddalone (13 pierwszy, bo to nasz
# domyslny). Uzywane, gdy nie bylo jeszcze skanu.
AUTO_CANDIDATES = [13, 1, 6, 11]
AUTO_BAD_LOSS = 5.0        # % strat, powyzej ktorych link uznajemy za zly
AUTO_BAD_SECONDS = 8.0     # tyle musi byc zle, zeby ruszyc kanal
AUTO_ACK_SECONDS = 3.0     # tyle czekamy na potwierdzenie od drugiej strony
AUTO_SETTLE_SECONDS = 8.0  # tyle czekamy, az link wstanie na nowym kanale
AUTO_SEARCH_DWELL = 4.0    # tyle nasluchujemy na kanale przy szukaniu drugiej strony


def set_channel_live(channel):
    """Przestawia wszystkie karty od razu, przez 'iw' - bez restartu uslugi.
    Skok ma trwac milisekundy: wfb-ng nadaje i odbiera na tym, na czym akurat
    stoi karta, wiec restart (kilka sekund ciszy) jest tu niepotrzebny."""
    nics = wfb_nics()
    return bool(nics) and all(set_nic_channel(nic, channel)[0] for nic in nics)


def auto_candidates(scan_results, current, ranges=None):
    """Kolejnosc, w ktorej probujemy kanalow: najpierw najciszsze ze skanu,
    a bez skanu - rozsunieta czworka z 2.4 GHz. Odpadaja kanaly spoza domeny
    regulacyjnej i ten, na ktorym wlasnie jestesmy."""
    ranges = reg_domain_ranges()[1] if ranges is None else ranges
    ranked = [r["channel"] for r in rank_channels(list(scan_results.values()))]
    order = ranked + [c for c in AUTO_CANDIDATES if c not in ranked]
    out = []
    for channel in order:
        if channel == current or channel in out:
            continue
        if channel_allowed(channel_freq(channel), ranges) is False:
            continue
        out.append(channel)
    return out


class AutoPeer:
    """Uzgadnianie skokow kanalu z druga strona - male datagramy w tunelu wfb.

    Nie szyfrujemy tego osobno: tunel jest juz szyfrowany kluczami wfb-ng,
    a kto jest w srodku, ten i tak moze wiecej niz przestawic kanal."""

    def __init__(self, port=AUTO_PORT, peer_ip=PEER_IP):
        self.port = port
        self.peer_ip = peer_ip
        self.error = None
        self._sock = None
        self._lock = threading.Lock()
        self._inbox = []
        self._last_seen = 0.0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.bind(("0.0.0.0", self.port))
            self._sock.settimeout(0.2)
        except OSError as e:
            self.error = f"nie moge otworzyc portu {self.port}: {e}"
            return self
        self._thread.start()
        return self

    def close(self):
        self._stop.set()
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass

    def send(self, text):
        if not self._sock:
            return
        try:
            self._sock.sendto(text.encode(), (self.peer_ip, self.port))
        except OSError:
            pass  # tunel wlasnie nie dziala - o to w tym trybie chodzi

    def take(self):
        with self._lock:
            msgs, self._inbox = self._inbox, []
        return msgs

    def peer_seen_ago(self):
        with self._lock:
            return time.monotonic() - self._last_seen if self._last_seen else None

    def _loop(self):
        while not self._stop.is_set():
            try:
                data, _addr = self._sock.recvfrom(512)
            except (socket.timeout, OSError):
                continue
            text = data.decode("utf-8", "replace").strip()
            with self._lock:
                self._inbox.append(text)
                self._last_seen = time.monotonic()
                del self._inbox[32:]


class AutoChannel:
    """Automat trybu automatycznego: dostaje czas, stan linku i wiadomosci od
    drugiej strony, a oddaje liste decyzji. Nie dotyka sam ani radia, ani
    plikow - dzieki temu da sie go sprawdzic bez sprzetu, a przy skokach
    kanalu pomylka kosztuje caly link.

    Zasady, ktore z tego wynikaja:
    - kanalu nie zmieniamy, dopoki link jest dobry;
    - nie skaczemy bez potwierdzenia od drugiej strony (skok w ciemno to
      pewna utrata lacznosci, a nie jej ratowanie);
    - po skoku obie strony same wracaja na poprzedni kanal, jesli link nie
      wstal - to ratuje sytuacje, gdy potwierdzenie doszlo, a dane juz nie;
    - szuka tylko gs. Dron zostaje na swoim kanale, zeby bylo gdzie go
      znalezc - gdyby szukaly obie strony, mijalyby sie w nieskonczonosc.

    Decyzje to krotki: ("send", tekst), ("hop", kanal, powod),
    ("persist", kanal), ("note", tekst)."""

    def __init__(self, channel, candidates, role=ROLE, now=0.0):
        self.channel = channel
        self.candidates = list(candidates)
        self.role = role
        self.initiator = role == "gs"
        self.state = "ok"
        self.state_since = now
        self.bad_since = None
        self.prev_channel = None
        self.target = None
        self.tries = 0
        self.search_order = []
        self.search_idx = 0
        self.blacklist = set()
        self._waiting_noted = False

    # --- pomocnicze ---

    def _next_candidate(self):
        for channel in self.candidates:
            if channel != self.channel and channel not in self.blacklist:
                return channel
        self.blacklist.clear()  # wszystko juz probowane - zaczynamy od nowa
        return next((c for c in self.candidates if c != self.channel), None)

    def _hop(self, target, reason, now, out):
        self.prev_channel = self.channel
        self.channel = target
        self.state, self.state_since = "settle", now
        self.bad_since = None
        out.append(("hop", target, reason))

    # --- glowna logika ---

    def tick(self, now, alive, loss, messages=()):
        out = []
        for text in messages:
            self._on_message(text, now, out)

        # Jesli wlasnie skoczylismy, to 'alive' opisuje jeszcze STARY kanal.
        # Ocena na takim pomiarze konczyla sie "link wstal" tuz po skoku na
        # martwy kanal - i automat nigdy nie wracal na dzialajacy.
        if self.state == "settle" and self.state_since == now:
            return out

        bad = (not alive) or (loss is not None and loss >= AUTO_BAD_LOSS)

        if self.state == "settle":
            if alive:
                self.state, self.state_since = "ok", now
                out.append(("persist", self.channel))
                out.append(("note", f"link wstal na kanale {self.channel}"))
            elif now - self.state_since >= AUTO_SETTLE_SECONDS:
                self.blacklist.add(self.channel)
                back = self.prev_channel
                self.channel = back
                self.state, self.state_since = "ok", now
                self.bad_since = now  # dalej jest zle, ale odliczamy od nowa
                out.append(("hop", back, "brak linku po skoku - wracam"))
            return out

        if self.state == "propose":
            if now - self.state_since >= AUTO_ACK_SECONDS:
                self.state, self.state_since = "ok", now
                self.bad_since = now
                out.append(("note", "druga strona nie potwierdza - zostaje na "
                                    f"kanale {self.channel}"))
            elif self.tries < 6:
                self.tries += 1
                out.append(("send", f"SWITCH {self.target}"))
            return out

        if self.state == "search":
            if alive:
                self.state, self.state_since = "ok", now
                out.append(("persist", self.channel))
                out.append(("note", f"znalazlem druga strone na kanale {self.channel}"))
            elif now - self.state_since >= AUTO_SEARCH_DWELL:
                self.search_idx = (self.search_idx + 1) % max(1, len(self.search_order))
                self.channel = self.search_order[self.search_idx]
                self.state_since = now
                out.append(("hop", self.channel, "szukam drugiej strony"))
            return out

        # stan "ok"
        if not bad:
            self.bad_since = None
            self._waiting_noted = False
            return out
        if self.bad_since is None:
            self.bad_since = now
            out.append(("note", "link sie sypie - obserwuje"))
            return out
        if now - self.bad_since < AUTO_BAD_SECONDS:
            return out

        if not self.initiator:
            if not self._waiting_noted:
                self._waiting_noted = True
                out.append(("note", "czekam na decyzje gs - dron kanalu nie zmienia"))
            return out

        target = self._next_candidate()
        if target is None:
            out.append(("note", "brak innego kanalu do sprobowania"))
            self.bad_since = now
            return out

        if alive:
            self.state, self.state_since = "propose", now
            self.target, self.tries = target, 1
            out.append(("send", f"SWITCH {target}"))
            out.append(("note", f"proponuje drugiej stronie kanal {target}"))
        else:
            # Zupelna cisza - nie ma z kim sie umawiac, wiec obchodzimy kanaly
            # i nasluchujemy. W obchodzie jest tez ten, na ktorym stoimy teraz:
            # druga strona moze wrocic na niego w kazdej chwili.
            self.search_order = [self.channel] + [c for c in self.candidates
                                                  if c != self.channel]
            self.search_idx = 1 % len(self.search_order)
            self.state, self.state_since = "search", now
            self.channel = self.search_order[self.search_idx]
            out.append(("hop", self.channel, "brak lacznosci - szukam drugiej strony"))
        return out

    def _on_message(self, text, now, out):
        parts = text.split()
        if not parts:
            return
        if parts[0] == "SWITCH" and len(parts) > 1 and parts[1].isdigit():
            target = int(parts[1])
            out.append(("send", f"SWITCH-OK {target}"))
            if target != self.channel:
                self._hop(target, "prosba drugiej strony", now, out)
        elif parts[0] == "SWITCH-OK" and self.state == "propose" and len(parts) > 1:
            if parts[1].isdigit() and int(parts[1]) == self.target:
                self._hop(self.target, "druga strona potwierdzila", now, out)
        elif parts[0] == "HELLO":
            out.append(("send", "HELLO-OK"))


class AutoFec:
    """Automat naprawy pakietow w tunelu: dobiera, ile nadmiarowosci FEC ma
    nadawac ta strona. Tak jak AutoChannel niczego sam nie dotyka - dostaje czas
    i pomiary, oddaje liste decyzji. Dzieki temu da sie go sprawdzic bez radia.

    Rzecz, ktora latwo zrobic tu zle: straty mierzymy na ODBIORZE, a ustawiamy
    FEC NADAWANIA. To sa dwa rozne kierunki. Nasze fec_k/fec_n decyduje o tym,
    ile pakietow odratuje DRUGA strona, a nie my - wiec pytamy o to ja. Kazda
    strona nadaje wiec swoj raport ("LOSS po przed") i dobiera nadmiarowosc pod
    to, co uslyszy z powrotem. Gdy druga strona milczy (nie ma tam wlaczonego
    tego ekranu albo tunel wlasnie lezy), wracamy do wlasnego odbioru i
    zakladamy, ze link jest z grubsza symetryczny - to gorsze niz raport, ale
    duzo lepsze niz nierobienie niczego.

    Zasady:
    - w gore szybko, w dol powoli. Za mala nadmiarowosc kosztuje utracone
      pakiety od razu, za duza tylko troche czasu antenowego;
    - patrzymy na straty PO naprawie, gdy decydujemy o dolozeniu (to one bola),
      a na straty PRZED naprawa, gdy decydujemy o zdjeciu (po naprawie zawsze
      jest zero, wiec automat schodzil by w dol az do pierwszej dziury);
    - kazda zmiana to restart uslugi, czyli zerwany link na kilka sekund -
      stad dlugi odstep miedzy zmianami.

    Decyzje to krotki: ("send", tekst), ("fec", poziom, powod), ("note", tekst)."""

    def __init__(self, level, role=ROLE, now=0.0):
        self.level = level          # indeks w FEC_LEVELS albo None (spoza drabinki)
        self.role = role
        self.peer_loss = None       # (po, przed) - ile druga strona gubi OD NAS
        self.peer_at = None
        self.bad_since = None
        self.good_since = None
        self.changed_at = now
        self.changes = 0
        self.source = None          # skad wzielismy ocene - do pokazania na ekranie
        self._last_report = None    # None, a nie 0.0: "jeszcze nie raportowalem"
        self._stuck_noted = False   # zero znaczylo by cos innego przy kazdym
                                    # zegarze zaczynajacym sie gdzie indziej

    # --- pomocnicze ---

    def peer_fresh(self, now):
        return (self.peer_at is not None
                and now - self.peer_at <= AUTO_FEC_PEER_STALE)

    def judged(self, now, loss_after, loss_before):
        """(po, przed, skad) - pomiar, na ktorym opieramy decyzje o WLASNYM
        nadawaniu. Raport drugiej strony ma pierwszenstwo, bo opisuje wlasciwy
        kierunek."""
        if self.peer_fresh(now) and self.peer_loss is not None:
            return self.peer_loss[0], self.peer_loss[1], "raport drugiej strony"
        return loss_after, loss_before, "wlasny odbior (link symetryczny?)"

    def _apply(self, level, reason, now, out):
        self.level = level
        self.changed_at = now
        self.changes += 1
        self.bad_since = self.good_since = None
        out.append(("fec", level, reason))

    # --- glowna logika ---

    def tick(self, now, loss_after, loss_before, messages=()):
        out = []
        for text in messages:
            self._on_message(text, now)

        # Raport dla drugiej strony: MY mowimy, ile gubimy OD NIEJ - ona pod to
        # dobiera swoje nadawanie. Lecimy tym samym gniazdem, co uzgadnianie
        # kanalu, wiec to jest zwykly datagram w tunelu.
        if loss_after is not None and (self._last_report is None
                                       or now - self._last_report >= AUTO_FEC_REPORT_EVERY):
            self._last_report = now
            before = loss_before if loss_before is not None else loss_after
            out.append(("send", f"LOSS {loss_after:.2f} {before:.2f}"))

        after, before, source = self.judged(now, loss_after, loss_before)
        self.source = source

        if self.level is None:
            if not self._stuck_noted:
                self._stuck_noted = True
                out.append(("note", "w configu jest FEC spoza drabinki - "
                                    "wybierz poziom recznie, wtedy ruszy automat"))
            return out
        if after is None:
            return out  # nic nie przychodzi - nie ma z czego wnioskowac

        if now - self.changed_at < AUTO_FEC_COOLDOWN:
            return out  # po restarcie uslugi liczniki i tak sa jeszcze zimne

        if after >= AUTO_FEC_BAD_LOSS:
            self.good_since = None
            if self.bad_since is None:
                self.bad_since = now
            elif now - self.bad_since >= AUTO_FEC_BAD_SECONDS:
                if self.level + 1 < len(FEC_LEVELS):
                    self._apply(self.level + 1,
                                f"tracimy {after:.1f}% mimo naprawy", now, out)
                else:
                    self.bad_since = now
                    out.append(("note", "jestem na najmocniejszym FEC, a straty "
                                        "zostaja - to juz na kanal albo antene"))
            return out

        self.bad_since = None
        # W dol schodzimy tylko wtedy, gdy samo radio przestalo gubic - jesli
        # gubi, a my tego nie widzimy, to znaczy, ze naprawa robi swoje i nie
        # ma jej po co zabierac. Nigdy ponizej AUTO_FEC_MIN_LEVEL: naprawe
        # wylacza sie recznie, automat nie zdejmuje calej ochrony sam.
        if (before is not None and before <= AUTO_FEC_GOOD_LOSS
                and self.level > AUTO_FEC_MIN_LEVEL):
            if self.good_since is None:
                self.good_since = now
            elif now - self.good_since >= AUTO_FEC_GOOD_SECONDS:
                self._apply(self.level - 1,
                            f"czysto od {AUTO_FEC_GOOD_SECONDS:.0f} s - oddaje pasmo",
                            now, out)
        else:
            self.good_since = None
        return out

    def _on_message(self, text, now):
        parts = text.split()
        if parts and parts[0] == "LOSS" and len(parts) >= 3:
            try:
                self.peer_loss = (float(parts[1]), float(parts[2]))
                self.peer_at = now
            except ValueError:
                pass


# ------------------------- instalacja (idempotentna) -------------------------

def is_fully_installed():
    return (
        driver_loaded()
        and bool(wfb_nics())  # modul zaladowany w jadrze to nie to samo co
                               # faktycznie skojarzony z karta USB (interfejs)
        and wfb_ng_installed()
        and DRONE_KEY.exists()
        and GS_KEY.exists()
        and CFG_PATH.exists()
    )


def setup_artifacts_present():
    """Czy setup w ogole sie odbyl - po SLADACH instalacji, a nie po zywych
    kartach. Roznica jest istotna przy autostarcie: is_fully_installed() zada
    dzialajacego wfb-nics, a to jest dokladnie ten stan, ktory po boocie bywa
    zepsuty i ktory autostart ma naprawiac. Gdyby pilnowal go ten warunek,
    tryb --autostart poddawalby sie zawsze wtedy, gdy jest najbardziej
    potrzebny."""
    return (
        driver_built()
        and wfb_ng_installed()
        and DRONE_KEY.exists()
        and GS_KEY.exists()
        and CFG_PATH.exists()
    )


def step_packages():
    log("==> [1/7] Pakiety podstawowe")
    run(["apt-get", "update", "-qq"])
    code, out = run([
        "apt-get", "install", "-y", "git", "build-essential", "bc", "libelf-dev", "dkms",
        f"linux-headers-{os.uname().release}", "curl", "gnupg", "lsb-release", "usbutils", "rfkill",
        "iw",
    ], timeout=300)
    if code != 0:
        log("UWAGA: instalacja pakietow zwrocila blad:")
        log(out)


def step_rfkill():
    log("==> [2/7] Odblokowuje rfkill")
    run_tool("rfkill", "unblock", "all")


# ------------------------- sterownik: moc per karta (latka) -------------------------

DRIVER_DKMS_NAME = "rtl8812au"
DRIVER_DKMS_VERSION = "5.2.20.2"  # pod ta wersja rejestruje sterownik jego dkms-install.sh
# Przebudowa z latka idzie OBOK zainstalowanego sterownika, pod inna wersja dkms:
# stary modul zostaje, dopoki nowy sie nie skompiluje (rebuild_driver_card_txpower).
DRIVER_DKMS_VERSION_CARD = "5.2.20.2.1"

# svpcom/rtl8812au trzyma moc w JEDNEJ zmiennej calego modulu
# (rtw_tx_pwr_idx_override), a 'iw dev X set txpower fixed -N00' wpisuje N wlasnie
# tam - czyli zmienia moc WSZYSTKIM kartom, nie tylko X. Dodatnia wartosc trafia do
# CurrentTxPwrIdx karty, ale ten czyta tylko kod 8814AU. Latka dodaje do danych HAL
# karty wlasny indeks mocy (TxPwrIdxCard): niezerowy wygrywa ze wspolnym we
# wszystkich 6 miejscach, w ktorych sterownik podmienia moc, a iw pisze juz tylko
# do tej karty. Parametr rtw_wfb_card_txpower to znak, ze latka siedzi w module.
# Kazda trojka (plik, stary tekst, nowy tekst) musi pasowac DOKLADNIE raz - inaczej
# nie ruszamy niczego (card_txpower_patched).
CARD_TXPOWER_PATCH = (
    ("include/hal_data.h",
     "\tu8\tCurrentTxPwrIdx;\n",
     "\tu8\tCurrentTxPwrIdx;\n"
     "\tu8\tTxPwrIdxCard;\t/* wfb: tx power index of this card only, 0 = module-wide */\n"),
    ("include/drv_types.h",
     "\t\treturn (u8)override_index;\n\treturn index;\n}\n",
     "\t\treturn (u8)override_index;\n\treturn index;\n}\n"
     "\n"
     "/* wfb: per-card tx power index (iw dev X set txpower fixed -N00) wins over the\n"
     " * module-wide rtw_tx_pwr_idx_override. A macro, because HAL_DATA_TYPE is not\n"
     " * complete yet at this point of the header. */\n"
     "#define get_card_tx_power_index(adapter, index) \\\n"
     "\t(GET_HAL_DATA(adapter)->TxPwrIdxCard ? GET_HAL_DATA(adapter)->TxPwrIdxCard \\\n"
     "\t : get_overridden_tx_power_index(index))\n"),
    ("os_dep/linux/os_intfs.c",
     'MODULE_PARM_DESC(rtw_tx_pwr_idx_override, "0-63 int value to force-set all power index values to");\n',
     'MODULE_PARM_DESC(rtw_tx_pwr_idx_override, "0-63 int value to force-set all power index values to");\n'
     "int rtw_wfb_card_txpower = 1;\n"
     "module_param(rtw_wfb_card_txpower, int, 0444);\n"
     'MODULE_PARM_DESC(rtw_wfb_card_txpower, "wfb: iw set txpower fixed -N00 sets index N for that card only");\n'),
    ("os_dep/linux/ioctl_cfg80211.c",
     "\t\trtw_tx_pwr_idx_override = -value;\n",
     "\t\tpHalData->TxPwrIdxCard = (-value > MAX_POWER_INDEX) ? MAX_POWER_INDEX : -value;\n"),
    ("os_dep/linux/ioctl_cfg80211.c",
     "\t\trtw_tx_pwr_idx_override = 0;\n",
     "\t\tpHalData->TxPwrIdxCard = 0;\n"),
    ("os_dep/linux/ioctl_cfg80211.c",
     "\toverride = get_overridden_tx_power_index(0);\n",
     "\toverride = get_card_tx_power_index(padapter, 0);\n"),
    ("hal/hal_com_phycfg.c",
     "\tValue = get_overridden_tx_power_index(Value);\n",
     "\tValue = get_card_tx_power_index(Adapter, Value);\n"),
    ("hal/hal_com_phycfg.c",
     "\tif (get_overridden_tx_power_index(0)) Value = 0;\n",
     "\tif (get_card_tx_power_index(pAdapter, 0)) Value = 0;\n"),
    ("hal/hal_com_phycfg.c",
     "\t\tpowerIndex = (u32)get_overridden_tx_power_index((u8)powerIndex);\n",
     "\t\tpowerIndex = (u32)get_card_tx_power_index(pAdapter, (u8)powerIndex);\n"),
    ("hal/hal_com_phycfg.c",
     "\tPowerIndex = (u32)get_overridden_tx_power_index((u8)PowerIndex);\n",
     "\tPowerIndex = (u32)get_card_tx_power_index(pAdapter, (u8)PowerIndex);\n"),
    ("hal/rtl8812a/rtl8812a_phycfg.c",
     "\tpower_idx = get_overridden_tx_power_index(power_idx);\n",
     "\tpower_idx = get_card_tx_power_index(pAdapter, power_idx);\n"),
    ("hal/rtl8812a/rtl8812a_phycfg.c",
     "\tPowerIndex = (u32)get_overridden_tx_power_index((u8)PowerIndex);\n",
     "\tPowerIndex = (u32)get_card_tx_power_index(Adapter, (u8)PowerIndex);\n"),
)


def card_txpower_patched(texts):
    """Latka CARD_TXPOWER_PATCH na tekstach zrodel {sciezka: tresc}. Zwraca
    (ok, komunikat, teksty). Przy jakimkolwiek niedopasowaniu ok=False i teksty
    NIETKNIETE - pol latki (np. pole w HAL bez makra) nie skompiluje sie albo,
    gorzej, skompiluje sie i zadziala tylko w czesci miejsc."""
    if any("rtw_wfb_card_txpower" in text for text in texts.values()):
        return True, "latka mocy per karta juz nalozona", texts
    out = dict(texts)
    for path, old, new in CARD_TXPOWER_PATCH:
        if path not in out:
            return False, f"brak pliku {path} w zrodlach", texts
        hits = out[path].count(old)
        if hits != 1:
            return False, (f"{path}: wzorzec latki pasuje {hits} razy zamiast 1"
                           " (inna wersja zrodel?)"), texts
        out[path] = out[path].replace(old, new)
    return True, f"latka mocy per karta nalozona ({len(CARD_TXPOWER_PATCH)} zmian)", out


def patch_driver_card_txpower(src_dir):
    """Latka mocy per karta na sklonowanych zrodlach. Zwraca (ok, komunikat).
    surrogateescape, bo zrodla Realteka maja komentarze w roznych kodowaniach -
    bajty, ktorych nie ruszamy, maja wrocic do pliku dokladnie takie same."""
    root = Path(src_dir)
    names = sorted({p for p, _, _ in CARD_TXPOWER_PATCH})
    try:
        texts = {p: (root / p).read_text(encoding="utf-8", errors="surrogateescape") for p in names}
    except OSError as e:
        return False, f"nie moge przeczytac zrodel: {e}"
    ok, msg, new = card_txpower_patched(texts)
    if ok and new is not texts:
        try:
            for p in names:
                (root / p).write_text(new[p], encoding="utf-8", errors="surrogateescape")
        except OSError as e:
            return False, f"nie moge zapisac zrodel: {e}"
    return ok, msg


def clone_driver_source(src_dir):
    """Zrodla sterownika (DRIVER_TAG) do src_dir, z poprawka dkms.conf pod
    naglowki Raspberry Pi OS. Zwraca (ok, wyjscie gita albo blad)."""
    run(["rm", "-rf", src_dir])
    code, out = run(["git", "clone", "-b", DRIVER_TAG, "--depth", "1",
                     "https://github.com/svpcom/rtl8812au.git", src_dir], timeout=120)
    if code != 0:
        return False, out

    # Raspberry Pi OS (trixie+) dzieli naglowki jadra na common+wariant.
    # dkms.conf tego sterownika nie ustawia KBUILD_OUTPUT, wiec jego
    # Makefile przekazuje "O=''" do sub-make, co kasuje KBUILD_OUTPUT
    # wariantu i psuje build (blad: "auto.conf: No such file or
    # directory"). Wymuszamy poprawna wartosc.
    dkms_conf = Path(src_dir) / "dkms.conf"
    try:
        dkms_conf.write_text(dkms_conf.read_text().replace(
            'KSRC=/lib/modules/${kernelver}/build"',
            'KSRC=/lib/modules/${kernelver}/build KBUILD_OUTPUT=/usr/src/linux-headers-${kernelver}"',
        ))
    except OSError as e:
        return False, f"dkms.conf: {e}"

    # Tag v5.2.20 w svpcom/rtl8812au bywa przesuwany na nowsze commity
    # w gore (bez zmiany nazwy taga). Jeden z takich commitow dodal w
    # core/rtw_br_ext.c blok "#if LINUX_VERSION_CODE >= KERNEL_VERSION(...)"
    # pod kernele 7.1+, ale zapomnial dolaczyc <linux/version.h> - bez
    # tego makra sa nieokreslone i build pada bledem "missing binary
    # operator". Dopisujemy brakujacy include, jesli go nie ma. Tutaj, a nie
    # w step_driver: kazda droga budowania (swieza instalacja, powtorka bez
    # latki, przebudowa z latka mocy) klonuje zrodla osobno.
    br_ext = Path(src_dir) / "core" / "rtw_br_ext.c"
    try:
        br_ext_src = br_ext.read_text(encoding="utf-8", errors="surrogateescape")
        if "#include <linux/version.h>" not in br_ext_src:
            br_ext.write_text(br_ext_src.replace(
                "#ifdef __KERNEL__\n\t#include <linux/if_arp.h>",
                "#ifdef __KERNEL__\n\t#include <linux/version.h>\n\t#include <linux/if_arp.h>",
                1,
            ), encoding="utf-8", errors="surrogateescape")
    except OSError as e:
        return False, f"core/rtw_br_ext.c: {e}"
    return True, out


def dkms_driver_versions():
    """Wersje sterownika zarejestrowane w dkms. 'dkms status' pisze to roznie
    zaleznie od wersji dkms ("rtl8812au/5.2.20.2, ..." albo "rtl8812au, 5.2.20.2, ..."),
    stad luzne dopasowanie."""
    code, out = run(["dkms", "status", DRIVER_DKMS_NAME])
    if code != 0:
        return []
    return sorted(set(re.findall(rf"\b{DRIVER_DKMS_NAME}[/,]\s*([0-9][^,:\s]*)", out)))


def rebuild_driver_card_txpower(say=None):
    """Przebudowa zainstalowanego sterownika z latka mocy per karta - dla Pi
    zainstalowanych, zanim latka powstala. Nowy modul buduje sie OBOK starego
    (DRIVER_DKMS_VERSION_CARD) i zastepuje go dopiero po udanej kompilacji, a gdy
    instalacja nie wyjdzie, stary wraca na miejsce: Pi nie moze zostac bez
    sterownika. Zaladowanego modulu nie rusza - to robi reload_wfb_driver.
    Zwraca (ok, komunikat)."""
    say = say or _default_say  # zdefiniowane nizej w pliku, wiec nie jako domyslny argument
    name, ver = DRIVER_DKMS_NAME, DRIVER_DKMS_VERSION_CARD
    src, dest = f"/tmp/rtl8812au-card-{os.getpid()}", f"/usr/src/{name}-{ver}"
    say(f"pobieram zrodla sterownika ({DRIVER_TAG}) z GitHuba...")
    ok, out = clone_driver_source(src)
    if not ok:
        return False, "nie udalo sie pobrac zrodel (brak sieci?): " + out.strip()[-120:]
    ok, msg = patch_driver_card_txpower(src)
    if not ok:
        run(["rm", "-rf", src])
        return False, msg
    say(msg)
    run(["dkms", "remove", f"{name}/{ver}", "--all"])  # slady po wczesniejszej nieudanej probie
    run(["rm", "-rf", dest])
    run(["cp", "-r", src, dest])
    run(["rm", "-rf", src])

    say("kompiluje modul (dkms) - kilka minut; link w tym czasie dziala dalej...")
    run(["dkms", "add", "-m", name, "-v", ver], timeout=120)
    code, out = run(["dkms", "build", "-m", name, "-v", ver], timeout=1800)
    if code != 0:
        run(["dkms", "remove", f"{name}/{ver}", "--all"])
        run(["rm", "-rf", dest])
        return False, "kompilacja z latka nie wyszla, zostaje stary sterownik: " + out.strip()[-160:]

    old = [v for v in dkms_driver_versions() if v != ver]
    for v in old:
        run(["dkms", "uninstall", "-m", name, "-v", v], timeout=300)
    code, out = run(["dkms", "install", "-m", name, "-v", ver], timeout=300)
    if code != 0 or not driver_card_txpower(max_age=0):
        run(["dkms", "remove", f"{name}/{ver}", "--all"])
        for v in old:
            run(["dkms", "install", "-m", name, "-v", v], timeout=300)
        return False, "instalacja nowego modulu nie wyszla, przywrocono stary: " + out.strip()[-160:]
    for v in old:
        run(["dkms", "remove", f"{name}/{v}", "--all"])
        run(["rm", "-rf", f"/usr/src/{name}-{v}"])
    return True, "sterownik z moca per karta zainstalowany"


def reload_wfb_driver(say=None):
    """Laduje od nowa modul 88XXau_wfb - po przebudowie nowy kod dziala dopiero
    po wyladowaniu starego. Usluga stoi przez te kilkanascie sekund; karty wracaja
    pod swoimi nazwami (reguly udev), moc wspolna z modprobe.d, a wlasna moc kart
    z configu przy starcie uslugi. Zwraca (ok, komunikat)."""
    say = say or _default_say  # zdefiniowane nizej w pliku, wiec nie jako domyslny argument
    was_active = service_active()
    say(f"zatrzymuje wifibroadcast@{ROLE} i przeladowuje modul 88XXau_wfb...")
    run(["systemctl", "stop", f"wifibroadcast@{ROLE}"])
    code, out = run(["modprobe", "-r", "88XXau_wfb"], timeout=60)
    if code != 0:
        if was_active:
            run(["systemctl", "start", f"wifibroadcast@{ROLE}"])
        return False, "modulu nie da sie wyladowac (zajety) - zrob reboot: " + out.strip()[:80]
    run(["modprobe", "88XXau_wfb"], timeout=60)
    run(["udevadm", "settle"], timeout=15)
    time.sleep(3)
    _card_txpower_cache["val"] = None
    nics = ensure_nic_names()  # karty wracaja pod swoimi nazwami
    if nics:
        release_nics_from_network_stack(nics)
        ensure_tx_split(nics)  # sterownik juz "on", wiec wlasne moce ida do configu
    if was_active:
        run(["systemctl", "start", f"wifibroadcast@{ROLE}"])
        time.sleep(3)
    _nic_status_cache["val"] = None
    if driver_card_txpower(max_age=0) != "on":
        return False, "modul przeladowany, ale dalej bez mocy per karta"
    if not nics:
        return False, "modul przeladowany, ale wfb-nics nie widzi kart - 'Wykryj karty ponownie'"
    if was_active and not service_active():
        return False, f"usluga nie wstala po przeladowaniu: {service_state_txt()}"
    return True, f"sterownik z moca per karta dziala ({len(nics)} kart)"


def step_driver():
    log("==> [3/7] Sterownik RTL8812AU")
    if driver_loaded() and wfb_nics():
        log("    juz zaladowany i skojarzony z karta, pomijam")
        return

    if not driver_built():
        code, out = run(["lsusb"])
        if "8812" not in out.lower():
            log("    UWAGA: nie widac karty 8812 w lsusb - podlacz ja przed dalszym krokiem")

        src_dir = f"/tmp/rtl8812au-build-{os.getpid()}"
        log(f"    Klonuje sterownik ({DRIVER_TAG})...")
        ok, out = clone_driver_source(src_dir)
        if not ok:
            log("    BLAD klonowania sterownika:")
            log(out)
            return
        patched, msg = patch_driver_card_txpower(src_dir)
        log(f"    {msg}" if patched else f"    Bez mocy per karta: {msg}")

        # dkms-install.sh robi "cp -r $(pwd) /usr/src/rtl8812au-5.2.20.2" -
        # jesli ten katalog juz istnieje (np. po wczesniejszej nieudanej
        # probie), cp wklei tam nowe zrodla jako PODFOLDER zamiast nadpisac,
        # wiec dkms i tak przeczyta stary dkms.conf bez poprawki z clone_driver_source.
        run(["rm", "-rf", f"/usr/src/{DRIVER_DKMS_NAME}-{DRIVER_DKMS_VERSION}"])

        log("    Buduje modul (dkms) - to moze potrwac kilka minut...")
        code, out = run(["bash", "-c", f"cd {src_dir} && ./dkms-install.sh"], timeout=600)
        run(["rm", "-rf", src_dir])

        if not driver_built() and patched:
            # Latka nie jest sprawdzona kompilacja na kazdym jadrze. Sterownik bez
            # mocy per karta jest lepszy niz zaden - drugi raz, z czystych zrodel.
            log("    Budowanie z latka mocy per karta nie wyszlo - buduje bez niej:")
            log(out[-1500:])
            run(["dkms", "remove", f"{DRIVER_DKMS_NAME}/{DRIVER_DKMS_VERSION}", "--all"])
            run(["rm", "-rf", f"/usr/src/{DRIVER_DKMS_NAME}-{DRIVER_DKMS_VERSION}"])
            ok, out = clone_driver_source(src_dir)
            if ok:
                code, out = run(["bash", "-c", f"cd {src_dir} && ./dkms-install.sh"], timeout=600)
            run(["rm", "-rf", src_dir])

        if not driver_built():
            log("    BLAD budowania sterownika:")
            log(out[-3000:])
            return

    run(["modprobe", "88XXau_wfb"])
    if driver_loaded() and wfb_nics():
        REBOOT_MARKER.unlink(missing_ok=True)
        return

    log("    Modul nie chce sie skojarzyc z karta USB - sprawdzam czy trzyma ja inny sterownik...")
    if rebind_to_wfb_driver():
        REBOOT_MARKER.unlink(missing_ok=True)
        return

    log("    Nadal nic - probuje wymusic ponowne wykrycie przez udev...")
    run(["udevadm", "trigger", "--action=add", "--subsystem-match=usb"])
    run(["udevadm", "settle"], timeout=15)
    time.sleep(2)
    if driver_loaded() and wfb_nics():
        REBOOT_MARKER.unlink(missing_ok=True)
        return

    if REBOOT_MARKER.exists():
        log("    Restart juz probowany wczesniej i nie pomogl. Sprawdz recznie:")
        log("    lsusb | grep 8812   oraz   wfb-nics   oraz   dmesg | tail -50")
        return

    log("    Karta byla juz podlaczona zanim sterownik zostal zbudowany, wiec kernel")
    log("    jej nie przepial na nowy modul. Restartuje system za 5 sekund - PO STARCIE")
    log("    URUCHOM TEN SKRYPT PONOWNIE, dokonczy konfiguracje automatycznie.")
    REBOOT_MARKER.write_text("1\n")
    time.sleep(5)
    run(["reboot"])
    sys.exit(0)


def step_tun():
    log("==> [4/7] Modul tun")
    run(["modprobe", "tun"])
    modules_file = Path("/etc/modules")
    txt = modules_file.read_text() if modules_file.exists() else ""
    if "tun" not in txt.split():
        with modules_file.open("a") as f:
            f.write("tun\n")


def step_wfb_ng_package():
    log("==> [5/7] Pakiet wfb-ng")
    if wfb_ng_installed():
        log("    juz zainstalowany, pomijam")
        return

    run(["bash", "-c",
         "curl -s https://apt.wfb-ng.org/public.asc | gpg --dearmor --yes -o /usr/share/keyrings/wfb-ng.gpg"])
    codename = run(["lsb_release", "-cs"])[1].strip() or "trixie"
    Path("/etc/apt/sources.list.d/wfb-ng.list").write_text(
        f"deb [signed-by=/usr/share/keyrings/wfb-ng.gpg] https://apt.wfb-ng.org/ {codename} {APT_RELEASE}\n"
    )
    code, out = run(["apt-get", "update"], timeout=120)
    if code != 0:
        run(["rm", "-f", "/etc/apt/sources.list.d/wfb-ng.list", "/usr/share/keyrings/wfb-ng.gpg"])
        run(["apt-get", "update"], timeout=120)

    code, out = run(["apt-get", "-y", "install", "wfb-ng"], timeout=180)
    if code == 0:
        return

    log("    brak gotowej paczki - buduje ze zrodel")
    run(["apt-get", "-y", "install", "python3-all", "python3-all-dev", "python3-venv", "libpcap-dev",
         "libsodium-dev", "libevent-dev", "python3-pip", "python3-pyroute2", "python3-msgpack",
         "python3-twisted", "python3-serial", "python3-jinja2", "iw", "debhelper", "dh-python",
         "fakeroot", "libgstrtspserver-1.0-dev", "socat", "libcatch2-dev"], timeout=300)
    tmp = f"/tmp/wfb-ng-build-{os.getpid()}"
    run(["rm", "-rf", tmp])
    run(["git", "clone", "-b", APT_RELEASE, "--depth", "1", "https://github.com/svpcom/wfb-ng.git", tmp],
        timeout=120)
    run(["bash", "-c", f"cd {tmp} && make deb"], timeout=300)
    run(["bash", "-c", f"apt-get -y install {tmp}/deb_dist/*.deb"], timeout=120)
    run(["rm", "-rf", tmp])


def step_keys():
    log("==> [6/7] Klucze szyfrujace")
    if DRONE_KEY.exists() and GS_KEY.exists():
        log(f"    juz obecne ({'wbudowane' if using_builtin_keys() else 'wlasne'}), pomijam")
        return

    ok, msg = builtin_keys_format_ok()
    if ok:
        write_builtin_keys()
        log(f"    Zapisano wbudowane klucze - {msg}.")
        log("    Sa identyczne w drone.py i gs.py, wiec NIC nie kopiujesz miedzy Pi.")
        return

    log(f"    UWAGA: {msg}")
    log("    Wbudowane klucze moglyby nie zadzialac - generuje wlasna pare.")
    generate_own_keys()


NM_CONF = Path("/etc/NetworkManager/conf.d/99-wfb-unmanaged.conf")


def ensure_nm_unmanaged(nics):
    """Raspberry Pi OS od bookworma nie uzywa juz dhcpcd tylko NetworkManagera
    - a ten probuje zarzadzac kazda karta wifi, takze ta w trybie monitor
    (potrafi jej ustawic tryb managed albo zrzucic kanal). Karty wfb musza byc
    dla niego 'unmanaged'. Onboard wifi Pi zostaje nietkniete, bo lista idzie
    z wfb-nics, czyli tylko nasze dongle."""
    if not nics or not Path("/etc/NetworkManager").is_dir():
        return
    want = ("# generowane przez skrypt wfb - nie edytuj recznie\n"
            "[keyfile]\n"
            "unmanaged-devices=" + ";".join(f"interface-name:{n}" for n in nics) + "\n")
    if NM_CONF.exists() and NM_CONF.read_text() == want:
        return
    NM_CONF.parent.mkdir(parents=True, exist_ok=True)
    NM_CONF.write_text(want)
    code, _ = run_tool("nmcli", "general", "reload")
    if code != 0:
        run(["systemctl", "reload", "NetworkManager"])


# ------------------------- parowanie -------------------------

# X25519 (RFC 7748) w czystym Pythonie. Swiezy Raspberry Pi OS nie ma
# gwarantowanego ani pynacl, ani cryptography, a doinstalowywanie biblioteki
# tylko po to, zeby raz policzyc klucz publiczny, to proszenie sie o problem
# przy braku sieci. Sprawdzone na wektorach z RFC 7748 i wzgledem libsodium.
_P = 2 ** 255 - 19
_A24 = 121665


def _cswap(swap, a, b):
    dummy = swap * ((a - b) % _P)
    return (a - dummy) % _P, (b + dummy) % _P


def x25519(scalar, u_bytes=None):
    """Mnozenie skalarne na Curve25519. u_bytes=None oznacza punkt bazowy,
    czyli wyliczenie klucza publicznego z tajnego."""
    k = bytearray(scalar)
    k[0] &= 248
    k[31] &= 127
    k[31] |= 64
    k = int.from_bytes(k, "little")
    u = 9 if u_bytes is None else int.from_bytes(u_bytes, "little") % (2 ** 255)

    x1, x2, z2, x3, z3, swap = u, 1, 0, u, 1, 0
    for t in range(254, -1, -1):
        kt = (k >> t) & 1
        swap ^= kt
        x2, x3 = _cswap(swap, x2, x3)
        z2, z3 = _cswap(swap, z2, z3)
        swap = kt

        a = (x2 + z2) % _P
        aa = a * a % _P
        b = (x2 - z2) % _P
        bb = b * b % _P
        e = (aa - bb) % _P
        c = (x3 + z3) % _P
        d = (x3 - z3) % _P
        da = d * a % _P
        cb = c * b % _P
        x3 = pow(da + cb, 2, _P)
        z3 = x1 * pow(da - cb, 2, _P) % _P
        x2 = aa * bb % _P
        z2 = e * ((aa + _A24 * e) % _P) % _P

    x2, x3 = _cswap(swap, x2, x3)
    z2, z3 = _cswap(swap, z2, z3)
    return (x2 * pow(z2, _P - 2, _P) % _P).to_bytes(32, "little")


PAIRING_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # bez I, O, 0, 1 - myli sie przy przepisywaniu
PAIRING_SALT = b"wfb-ng pairing v1"
PAIRING_CODE_PATH = Path("/etc/wfb-pairing.code")


def new_pairing_code():
    return "".join(secrets.choice(PAIRING_ALPHABET) for _ in range(8))


def format_pairing_code(code):
    return f"{code[:4]}-{code[4:]}"


def normalize_pairing_code(text):
    """Zwraca 8 znakow alfabetu albo None. Wybaczamy male litery, spacje i
    myslniki - kod przepisuje sie recznie z drugiego ekranu."""
    raw = "".join(ch for ch in text.upper() if ch.isalnum())
    if len(raw) != 8 or any(ch not in PAIRING_ALPHABET for ch in raw):
        return None
    return raw


def derive_keys_from_code(code):
    """Z jednego kodu obie strony licza IDENTYCZNA pare kluczy - w tym cala
    sztuczka: nie trzeba przenosic zadnych plikow, wystarczy przepisac 8
    znakow. Zwraca (drone_key, gs_key) w formacie wfb-ng (po 64 bajty)."""
    seed = hashlib.sha256(PAIRING_SALT + code.encode()).digest()
    drone_sk = hashlib.sha256(seed + b"drone").digest()
    gs_sk = hashlib.sha256(seed + b"gs").digest()
    return drone_sk + x25519(gs_sk), gs_sk + x25519(drone_sk)


def apply_pairing_code(code):
    """Zapisuje klucze wyliczone z kodu oraz sam kod - zeby dalo sie go
    podejrzec pozniej, jak sie zapomni przed pojsciem do drugiego Pi."""
    drone_key, gs_key = derive_keys_from_code(code)
    DRONE_KEY.write_bytes(drone_key)
    GS_KEY.write_bytes(gs_key)
    PAIRING_CODE_PATH.write_text(code + "\n")
    for p in (DRONE_KEY, GS_KEY, PAIRING_CODE_PATH):
        os.chmod(p, 0o600)


def read_pairing_code():
    try:
        return normalize_pairing_code(PAIRING_CODE_PATH.read_text())
    except OSError:
        return None


def key_mode():
    """Skad pochodza klucze lezace w /etc: (tryb, kod). Kod jest weryfikowany
    - zapisany plik z kodem nic nie znaczy, jesli klucze sa juz inne."""
    if not (DRONE_KEY.exists() and GS_KEY.exists()):
        return "brak", None
    if using_builtin_keys():
        return "wbudowane", None
    code = read_pairing_code()
    if code:
        try:
            if derive_keys_from_code(code)[0] == DRONE_KEY.read_bytes():
                return "sparowane", code
        except OSError:
            pass
    return "wlasne", None


def write_builtin_keys():
    DRONE_KEY.write_bytes(base64.b64decode(DRONE_KEY_B64))
    GS_KEY.write_bytes(base64.b64decode(GS_KEY_B64))
    for p in (DRONE_KEY, GS_KEY):
        os.chmod(p, 0o600)


def using_builtin_keys():
    try:
        return (DRONE_KEY.read_bytes() == base64.b64decode(DRONE_KEY_B64)
                and GS_KEY.read_bytes() == base64.b64decode(GS_KEY_B64))
    except OSError:
        return False


def builtin_keys_format_ok():
    """Wbudowane klucze musza miec taki sam uklad jak te z wfb_keygen, bo
    czytaja je wfb_rx/wfb_tx. Ten format nie zmienil sie w wfb-ng od lat, ale
    zamiast zakladac - porownujemy z para wygenerowana na TYM systemie. Lepiej
    dowiedziec sie tu niz szukac pozniej, czemu nie ma linku."""
    if not wfb_ng_installed():
        return True, "wfb_keygen niedostepny, pomijam kontrole formatu"
    tmp = f"/tmp/wfb-keycheck-{os.getpid()}"
    run(["rm", "-rf", tmp])
    run(["mkdir", "-p", tmp])
    run(["bash", "-c", f"cd {tmp} && wfb_keygen"])
    sizes = {}
    for name in ("drone.key", "gs.key"):
        p = Path(tmp) / name
        sizes[name] = p.stat().st_size if p.exists() else -1
    run(["rm", "-rf", tmp])
    ours = len(base64.b64decode(DRONE_KEY_B64))
    if sizes["drone.key"] != ours or sizes["gs.key"] != ours:
        return False, f"wfb_keygen robi klucze {sizes}, a wbudowane maja {ours} B"
    return True, f"format zgodny z wfb_keygen ({ours} B)"


def generate_own_keys():
    """Wlasna, prywatna para - bezpieczniejsza, ale trzeba ja przeniesc na
    druga strone recznie."""
    run(["bash", "-c", "cd /etc && wfb_keygen"])
    log("")
    log(f"    !!! Wygenerowano NOWA pare kluczy NA TYM urzadzeniu (rola: {ROLE}).")
    log(f"    !!! Odcisk: drone.key={key_fingerprint(DRONE_KEY)} gs.key={key_fingerprint(GS_KEY)}")
    log("    !!! Skopiuj OBA pliki na DRUGIE urzadzenie (nadpisz tam):")
    log("    !!!   scp /etc/drone.key /etc/gs.key <user>@<ip-drugiego-urzadzenia>:/tmp/")
    log("    !!!   # na drugim urzadzeniu:")
    log("    !!!   sudo mv /tmp/drone.key /tmp/gs.key /etc/")
    log("    !!! Do czasu skopiowania nie bedzie polaczenia.")
    log("")


def key_fingerprint(path):
    """Krotki odcisk pliku klucza. Sluzy do porownania go GOLYM OKIEM miedzy
    dronem a gs - wfb_keygen na kazdym urzadzeniu robi INNA pare, a sama
    obecnosc plikow (ktora sprawdzamy osobno) niczego nie gwarantuje."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:8]
    except OSError:
        return None


# Klasyczne objawy przeciazonych portow USB przy dwoch donglach 8812AU.
POWER_PATTERNS = ("over-current", "overcurrent", "under-voltage", "undervoltage",
                  "usb disconnect")


def usb_power_issues():
    code, out = run(["dmesg"])
    if code != 0:
        return []
    return [line.strip() for line in out.splitlines()
            if any(p in line.lower() for p in POWER_PATTERNS)]


def ensure_dhcpcd_deny(nics):
    """dhcpcd nie moze dotykac kart wfb. Dopisujemy PER INTERFEJS, bo drugi
    dongiel czesto pojawia sie dopiero pozniej - sprawdzanie "czy w pliku
    jest w ogole slowo denyinterfaces" przepuscilo by go bez wpisu."""
    dhcpcd = Path("/etc/dhcpcd.conf")
    if not dhcpcd.exists() or not nics:
        return
    txt = dhcpcd.read_text()
    listed = set()
    for line in txt.splitlines():
        if line.strip().startswith("denyinterfaces"):
            listed.update(line.split()[1:])
    missing = [n for n in nics if n not in listed]
    if missing:
        with dhcpcd.open("a") as f:
            f.write("denyinterfaces " + " ".join(missing) + "\n")


def release_nics_from_network_stack(nics):
    """Zdejmij karty wfb spod kontroli tego, co akurat zarzadza siecia.
    Starsze obrazy: dhcpcd, nowsze (bookworm/trixie, wiec i swieze Pi 5):
    NetworkManager. Wolane tez przy starcie, bo drugi dongiel potrafi
    pojawic sie dawno po instalacji."""
    ensure_dhcpcd_deny(nics)
    ensure_nm_unmanaged(nics)


# ------------------------- stale nazwy kart -------------------------

EMPTY_MACS = ("", "00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff")


# ------------------------- role kart (TX / RX) -------------------------

# Rola karty -> znacznik w jej nazwie. Nazwa = <ROLE>_<znacznik>[numer]:
# pierwsza karta danej roli jest bez numeru, kolejne od 2 (drone_RX, drone_RX2,
# drone_RX3...). Dzieki numerom kart moze byc dowolnie duzo i kazda moze miec
# dowolna role. Jadro pozwala na 15 znakow nazwy - drone_TXRX99 ma 12.
ROLE_TAGS = {"tx": "TX", "rx": "RX", "txrx": "TXRX"}

# wfb-ng nie ma trybu "tylko nadawanie": kazda karta trafia do wfb_rx, a z
# wfb_tx da sie wylaczyc tylko karte rx-only (wifi_txpower = 'off'). Dlatego
# tx i txrx konfiguruja wfb-ng TAK SAMO - tx to oznaczenie karty, ktora MA
# nadawac (np. ze wzmacniaczem), i tak jest opisane, zeby nikt nie liczyl na
# karte glucha.
ROLE_LABELS = {
    "tx": ("NADAJE", "nadaje (odbiera tez - wfb-ng nie ma trybu tylko-TX)"),
    "rx": ("TYLKO ODBIOR", "tylko odbior - nie nadaje"),
    "txrx": ("TX+RX", "nadaje i odbiera"),
}

# Nazwy sprzed rol w nazwie: gs mial jedna karte "gs_wfb" robiaca oba kierunki.
# Instalacje z tamtych czasow maja ja w regulach udev - rozpoznajemy ja dalej
# (jako swoja na gs i jako cudza na dronie), zamiast przemianowywac po cichu.
LEGACY_NIC_NAMES = {"gs_wfb": ("gs", "txrx")}

# Rola karty wpietej ponad DEFAULT_NIC_ROLES. Tylko odbior: wfb_tx rozklada
# pakiety miedzy wszystkie karty nadawcze, wiec dongiel wpiety "na probe"
# zabralby czesc wideo torowi ze wzmacniaczem. Nadawac zacznie dopiero wtedy,
# gdy ktos mu to swiadomie ustawi w menu.
SPARE_NIC_ROLE = "rx"

_NIC_NAME_RE = re.compile(r"^([a-z]+)_(TXRX|TX|RX)([2-9]|[1-9][0-9]+)?$")


def parse_nic_name(name):
    """(rola urzadzenia, rola karty, numer) odczytane z nazwy karty albo None
    dla wlanX i wszystkiego, czego nie nazywamy sami. Rozpoznaje tez nazwy
    DRUGIEJ roli - po nich refuse_wrong_role poznaje cudze Pi."""
    if name in LEGACY_NIC_NAMES:
        owner, role = LEGACY_NIC_NAMES[name]
        return owner, role, 1
    m = _NIC_NAME_RE.match(name or "")
    if not m or m.group(1) not in (ROLE, PEER_NAME):
        return None
    role = next(r for r, tag in ROLE_TAGS.items() if tag == m.group(2))
    return m.group(1), role, int(m.group(3) or 1)


def role_of_name(name):
    """Rola karty TEJ maszyny zapisana w nazwie. Pusta dla wlanX i dla nazw
    drugiej roli - taka karta nie ma u nas przydzialu."""
    parsed = parse_nic_name(name)
    return parsed[1] if parsed and parsed[0] == ROLE else ""


def free_role_name(role, taken):
    """Pierwsza wolna nazwa dla roli: drone_TX, potem drone_TX2, drone_TX3...
    'taken' to nazwy trzymane przez reguly udev i przez istniejace interfejsy -
    takze przez karty chwilowo wypiete, bo ich regula wciaz trzyma nazwe."""
    base = f"{ROLE}_{ROLE_TAGS[role]}"
    for num in range(1, 100):
        name = base if num == 1 else f"{base}{num}"
        if name not in taken:
            return name
    return ""


def role_txt(role, short=False):
    labels = ROLE_LABELS.get(role)
    if not labels:
        return "bez przydzialu"
    return labels[0] if short else labels[1]


def role_tag(name, fallback=""):
    """Etykieta roli doklejana po nazwie karty, np. "[NADAJE]". Jedno miejsce
    na jej wyglad, bo wychodzi w naglowku menu, na trzech ekranach i w
    weryfikacji. 'fallback' to rola z ewidencji - dla karty, ktorej juz nie ma
    i ktorej nazwa moze byc sprzed zmiany."""
    role = role_of_name(name) or fallback
    return f"[{role_txt(role, short=True)}]" if role else "[bez przydzialu]"


def parse_name_rules():
    """{kotwica: nazwa} z naszego pliku regul udev - czyli przypisania, ktore
    juz kiedys ustalilismy. Kotwica to ("mac", adres) albo ("slot", gniazdo);
    starsze wersje skryptu pisaly wylacznie reguly na gniazdo, wiec czytamy
    oba warianty."""
    mapping = {}
    if not UDEV_NAMES.exists():
        return mapping
    for line in UDEV_NAMES.read_text().splitlines():
        name = re.search(r'NAME="([^"]+)"', line)
        if not name:
            continue
        mac = re.search(r'ATTR\{address\}=="([^"]+)"', line)
        slot = re.search(r'KERNELS=="([^"]+)"', line)
        if mac:
            mapping[("mac", mac.group(1).lower())] = name.group(1)
        elif slot:
            mapping[("slot", slot.group(1))] = name.group(1)
    return mapping


def nic_anchors(nics):
    """{interfejs: kotwica nazwy}. Domyslnie MAC - jedzie razem z dongla, wiec
    karta przelozona do innego portu zostaje soba (istotne, gdy do konkretnej
    karty przykrecony jest wzmacniacz albo antena kierunkowa). Gniazdo USB
    zostaje awaryjnie: dla kart bez czytelnego MAC-a i dla tanich klonow, ktore
    potrafia miec fabrycznie ten sam adres - tam MAC nie rozroznia niczego."""
    macs = {nic: nic_mac(nic) for nic in nics}
    seen = list(macs.values())
    out = {}
    for nic in nics:
        mac = macs[nic]
        if mac not in EMPTY_MACS and seen.count(mac) == 1:
            out[nic] = ("mac", mac)
        else:
            slot = nic_usb_slot(nic)
            out[nic] = ("slot", slot) if slot else None
    return out


def anchor_key(anchor):
    """Kotwica jako jeden ciag do klucza w pliku ewidencji: ('mac', 'aa:..')
    -> 'mac:aa:..'. Ta sama kotwica co w regulach udev, wiec ewidencja i nazwy
    mowia o tej samej karcie."""
    return f"{anchor[0]}:{anchor[1]}" if anchor else ""


def load_cards():
    """Ewidencja kart z pliku: {kotwica: {name, role, usb, mac, seen}}.
    Uszkodzony plik traktujemy jak pusty - to tylko pamiec pomocnicza i nie ma
    powodu, zeby jej brak blokowal cokolwiek."""
    try:
        data = json.loads(WFB_CARDS.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_cards(cards):
    try:
        WFB_CARDS.write_text(json.dumps(cards, indent=1, sort_keys=True))
        return True
    except OSError:
        return False  # bez roota (np. podglad z konta usera) - trudno, jedziemy dalej


SEEN_REFRESH = 600  # co ile sekund odswiezamy sam znacznik czasu (patrz nizej)


def remember_cards(nics=None):
    """Dopisuje do ewidencji karty, ktore widac TERAZ, i zwraca cala ewidencje.
    Wpisow nieobecnych kart nie kasujemy - to wlasnie one pozwalaja powiedziec
    'brakuje drone_TX, ostatnio w gniezdzie 1-1.4', kiedy dongla juz nie ma
    w systemie i nie da sie o nic zapytac sterownika.

    Sam znacznik czasu odswiezamy najwyzej co SEEN_REFRESH sekund: funkcja jest
    wolana przy kazdym odswiezeniu naglowka menu, a zapis do /etc co sekunde
    mieliłby karte SD bez zadnego pozytku."""
    nics = wfb_nics() if nics is None else nics
    cards = load_cards()
    anchors = nic_anchors(nics)
    now = int(time.time())
    changed = False
    for nic in nics:
        key = anchor_key(anchors.get(nic))
        if not key:
            continue  # karta bez czytelnego MAC-a i bez gniazda - nie ma czego zapamietac
        old = cards.get(key, {})
        entry = dict(old, name=nic, mac=nic_mac(nic), usb=usb_port_path(nic),
                     role=role_of_name(nic))
        stale = now - int(old.get("seen_ts") or 0) >= SEEN_REFRESH
        if stale or any(entry.get(k) != old.get(k) for k in ("name", "mac", "usb", "role")):
            entry.update(seen=time.strftime("%Y-%m-%d %H:%M:%S"), seen_ts=now)
            cards[key] = entry
            changed = True
    if changed:
        save_cards(cards)
    return cards


def forget_card(key):
    """Usuwa karte z ewidencji RAZEM z jej regula nazwy - dla dongla wymienionego
    na inny albo wpietego tylko na probe. Bez tego wisialby wiecznie jako
    brakujacy i trzymal nazwe; jesli kiedys wroci, dostanie role jak nowa karta."""
    cards = load_cards()
    if cards.pop(key, None) is None:
        return False
    save_cards(cards)
    kind, _, value = key.partition(":")
    rules = parse_name_rules()
    if rules.pop((kind, value), None) is not None:
        try:
            write_name_rules(rules)
        except OSError:
            pass  # bez roota regula zostaje - nic nie psuje, karty i tak nie ma
    return True


def missing_cards(nics=None):
    """Karty, ktore ewidencja zna, a ktorych teraz nie ma - czyli dokladnie te,
    ktore ktos wypial (albo ktore nie wstaly po boocie). Zwraca liste wpisow
    z kluczem, posortowana po nazwie."""
    nics = wfb_nics() if nics is None else nics
    present = set(nics)
    out = []
    for key, entry in load_cards().items():
        if entry.get("name") not in present:
            out.append(dict(entry, key=key))
    return sorted(out, key=lambda e: e.get("name") or "")


def card_txt(entry, with_seen=True):
    """Jedna linijka o karcie z ewidencji: nazwa, rola, MAC i gniazdo USB.
    Uzywana tam, gdzie karty juz nie ma i nie ma sie o co pytac systemu."""
    name = entry.get("name") or "?"
    tag = role_tag(name, entry.get("role") or "")
    txt = name + (f" {tag}" if tag else "")
    txt += f"   mac={entry.get('mac') or '?'}   gniazdo USB {entry.get('usb') or '?'}"
    if with_seen and entry.get("seen"):
        txt += f"   ostatnio: {entry['seen']}"
    return txt


def missing_cards_txt(nics=None, sep="; "):
    """Krotki opis brakujacych kart do naglowka i do checkow - zeby zamiast
    samego 'BRAK KARTY' bylo widac, KTORA karta zniknela i z ktorego gniazda."""
    out = []
    for e in missing_cards(nics):
        name = e.get("name") or "?"
        tag = role_tag(name, e.get("role") or "")
        out.append(name + (f" {tag}" if tag else "")
                   + f" (gniazdo {e.get('usb') or '?'}, mac {e.get('mac') or '?'})")
    return sep.join(out)


def plan_nic_names(nics):
    """Przydziela kartom nazwy, czyli role. Raz ustalone przypisanie karta->nazwa
    zostaje (lezy w regulach udev) - takze dla karty chwilowo wypietej, zeby po
    ponownym wpieciu wrocila do SWOJEJ roli, a nie do tej, ktora akurat zostala.
    Nowa karta dostaje pierwsza nieobsadzona role z DEFAULT_NIC_ROLES, a gdy
    uklad startowy jest juz obsadzony - SPARE_NIC_ROLE. Nazw nie brakuje nigdy
    (kolejne karty roli dostaja numer), wiec zadna karta nie oddaje swojej.
    Zwraca (mapa kotwica->nazwa, mapa interfejs->nazwa)."""
    by_anchor = parse_name_rules()
    anchors = nic_anchors(nics)
    slots = {nic: nic_usb_slot(nic) for nic in nics}

    # Przejscie ze starych regul (na gniazdo) na nowe (na MAC): karta, ktora ma
    # juz nazwe z gniazda, zabiera ja ze soba na swoj MAC. Bez tego pierwsze
    # uruchomienie nowej wersji przetasowalo by nazwy.
    for nic, anchor in anchors.items():
        old = ("slot", slots[nic])
        if anchor and anchor[0] == "mac" and anchor not in by_anchor and old in by_anchor:
            by_anchor[anchor] = by_anchor.pop(old)

    # Karta, ktora juz nosi nasza nazwe, a nie ma reguly (np. ktos skasowal plik
    # regul), zostaje przy swojej nazwie - inaczej zmienilaby role po cichu.
    for nic, anchor in anchors.items():
        if (anchor and anchor not in by_anchor and role_of_name(nic)
                and nic not in by_anchor.values()):
            by_anchor[anchor] = nic

    # Stary uklad drona ("rx", "tx") dawal JEDYNEJ karcie nazwe <rola>_RX, ktora
    # w menu i w logach wyglada na glucha, chociaz jako jedyna i tak nadaje
    # (muted_nics nie wycisza ostatniej karty). Po obu stronach jedyna karta
    # ma byc <rola>_TXRX - przestawiamy ja, ale tylko gdy regul naszej roli jest
    # dokladnie jedna (druga karta chwilowo wypieta zostawia uklad w spokoju)
    # i tylko z rx: swiadomie ustawione tx zostaje.
    own = [(a, n) for a, n in by_anchor.items() if role_of_name(n)]
    if len(own) == 1 and role_of_name(own[0][1]) == "rx" and own[0][0] in anchors.values():
        anchor, old = own[0]
        new = free_role_name("txrx", (set(by_anchor.values()) | set(nics)) - {old})
        if new:
            by_anchor[anchor] = new

    # Nieobecnej karcie NIE zabieramy nazwy: nazw jest bez liku, wiec nowa karta
    # jej nie potrzebuje, a karta ze wzmacniaczem po zlym kablu ma wrocic jako
    # nadajaca. Regule zmiata dopiero "zapomnij" (forget_card).
    pending = list(DEFAULT_NIC_ROLES)
    for name in by_anchor.values():
        if role_of_name(name) in pending:
            pending.remove(role_of_name(name))

    taken = set(by_anchor.values()) | set(nics)
    per_nic = {}
    for nic in sorted(nics, key=lambda n: (slots[n] or "", n)):
        anchor = anchors[nic]
        if not anchor:
            continue  # nie ma czego zakotwiczyc w regule
        if anchor not in by_anchor:
            name = free_role_name(pending.pop(0) if pending else SPARE_NIC_ROLE, taken)
            if not name:
                continue  # 99 kart jednej roli - reszta zostaje przy wlanX
            by_anchor[anchor] = name
            taken.add(name)
        per_nic[nic] = by_anchor[anchor]
    return by_anchor, per_nic


def write_name_rules(by_anchor):
    txt = ("# generowane przez skrypt wfb - nie edytuj recznie\n"
           "# stale nazwy kart RTL88xx; nazwa jest przypieta do MAC-a karty,\n"
           "# wiec jedzie razem z donglem niezaleznie od portu USB.\n"
           "# Reguly na KERNELS== to zapasowe kotwiczenie na gniezdzie USB -\n"
           "# dla kart bez czytelnego MAC-a albo z powtorzonym adresem.\n")
    for (kind, value), name in sorted(by_anchor.items()):
        match = f'ATTR{{address}}=="{value}"' if kind == "mac" else f'KERNELS=="{value}"'
        txt += f'SUBSYSTEM=="net", ACTION=="add", {match}, NAME="{name}"\n'
    if UDEV_NAMES.exists() and UDEV_NAMES.read_text() == txt:
        return False
    UDEV_NAMES.parent.mkdir(parents=True, exist_ok=True)
    UDEV_NAMES.write_text(txt)
    run(["udevadm", "control", "--reload-rules"])
    return True


def hotplug_rules_text():
    """RUN+= jest wolane przez udev synchronicznie, wiec 'systemd-run --no-block'
    zeby nie trzymac kolejki zdarzen na czas trwania restartu uslugi."""
    return (
        "# generowane przez skrypt wfb - nie edytuj recznie\n"
        f"# po kazdym dodaniu/usunieciu karty {ROLE}_* odswieza\n"
        f"# WFB_NICS i restartuje usluge - patrz sync_wfb_nics() w {SCRIPT_PATH.name}\n"
        f'SUBSYSTEM=="net", KERNEL=="{ROLE}_*", ACTION=="add", '
        f'RUN+="/usr/bin/systemd-run --no-block --quiet {sys.executable} {SCRIPT_PATH} {HOTPLUG_FLAG}"\n'
        f'SUBSYSTEM=="net", KERNEL=="{ROLE}_*", ACTION=="remove", '
        f'RUN+="/usr/bin/systemd-run --no-block --quiet {sys.executable} {SCRIPT_PATH} {HOTPLUG_FLAG}"\n'
    )


def ensure_hotplug_rule():
    txt = hotplug_rules_text()
    if HOTPLUG_RULES.exists() and HOTPLUG_RULES.read_text() == txt:
        return False
    HOTPLUG_RULES.parent.mkdir(parents=True, exist_ok=True)
    HOTPLUG_RULES.write_text(txt)
    run(["udevadm", "control", "--reload-rules"])
    return True


def wfb_nics_defaults():
    """Karty aktualnie wpisane w WFB_NICS (kolejnosc z pliku)."""
    if not WFB_DEFAULTS.exists():
        return []
    m = re.search(r'^WFB_NICS="([^"]*)"', WFB_DEFAULTS.read_text(), re.M)
    return m.group(1).split() if m else []


def write_wfb_nics(nics):
    """Podmienia WFB_NICS na liste podana - posortowana (karty z nasza nazwa
    najpierw), zeby plik nie skakal bez powodu przy kazdym wywolaniu. Kart moze
    byc dowolnie duzo, wiec nie ma juz stalej listy nazw do kolejnosci; drone_RX
    i tak wypada przed drone_TX, jak dawniej."""
    if not WFB_DEFAULTS.exists() or not nics:
        return False
    ordered = sorted(nics, key=lambda n: (not role_of_name(n), n))
    txt = WFB_DEFAULTS.read_text()
    new_txt, count = re.subn(r'^WFB_NICS=".*"$', f'WFB_NICS="{" ".join(ordered)}"', txt, flags=re.M)
    if count == 0 or new_txt == txt:
        return False
    WFB_DEFAULTS.write_text(new_txt)
    return True


def sync_wfb_nics():
    """Wolane z reguly udev (ensure_hotplug_rule) po kazdym dodaniu/usunieciu
    karty <rola>_*. Patrz komentarz przy HOTPLUG_RULES: bez tego
    zniknieciecie jednej karty zabijaloby rowniez te, ktora zostala podpieta."""
    nics = wfb_nics()
    current = wfb_nics_defaults()
    if sorted(nics) == sorted(current):
        return
    if not write_wfb_nics(nics):
        return
    log(f"    WFB_NICS: {' '.join(current) or '(brak)'} -> {' '.join(nics) or '(brak)'}")
    # wifi_txpower w formie slownika musi miec wpis dla KAZDEJ karty z WFB_NICS
    # (rola RX = 'off', wlasna moc, limit) - bez tego wfb-ng po restarcie nie
    # wstaje, gdy wraca karta, ktorej nie bylo przy ostatnim zapisie configu.
    ensure_tx_split(nics)
    run(["systemctl", "restart", f"wifibroadcast@{ROLE}"])


def hotplug_run():
    """Tryb bez TUI wolany przez regule udev (HOTPLUG_FLAG). Tylko WFB_NICS +
    restart - zadnego innego sprzatania, zeby zdazyc, zanim ktos zauwazy
    przerwe w odbiorze po drugiej stronie."""
    log(f"==> Hotplug {ROLE} ({HOTPLUG_FLAG})")
    sync_wfb_nics()
    return 0


def rename_nic(old, new):
    """Jadro pozwala zmienic nazwe tylko interfejsowi w stanie DOWN."""
    run(["ip", "link", "set", old, "down"])
    code, out = run(["ip", "link", "set", old, "name", new])
    if code != 0:
        run(["ip", "link", "set", old, "up"])
        return False, out
    run(["ip", "link", "set", new, "up"])
    return True, ""


def update_wfb_defaults(renames):
    """Jesli /etc/default/wifibroadcast wymienia karty z nazwy (WFB_NICS),
    podmieniamy stare nazwy na nowe - inaczej usluga wystartowalaby na
    nieistniejacym juz interfejsie."""
    if not WFB_DEFAULTS.exists():
        return
    txt = WFB_DEFAULTS.read_text()
    new_txt = txt
    for old, name in renames:
        new_txt = re.sub(rf"\b{re.escape(old)}\b", name, new_txt)
    if new_txt != txt:
        WFB_DEFAULTS.write_text(new_txt)
        log(f"    poprawiono nazwy kart w {WFB_DEFAULTS}")


def ensure_nic_names():
    """Nadaje kartom stale nazwy z rola (plan_nic_names) zamiast wlanX. Zmiana
    nazwy nie powiedzie sie na pracujacym interfejsie, wiec na czas operacji
    zatrzymujemy usluge. Gdyby po zmianie wfb-nics przestalo widziec karty
    (jakas wersja szukajaca ich po nazwie "wlan*"), wycofujemy wszystko -
    dzialajace lacze jest wazniejsze niz ladna nazwa. Zwraca aktualna liste
    interfejsow."""
    nics = wfb_nics()
    if not nics:
        return nics

    by_anchor, per_nic = plan_nic_names(nics)
    write_name_rules(by_anchor)  # zeby przetrwalo reboot i ponowne wpiecie dongla
    ensure_hotplug_rule()  # zeby wypiecie jednej karty nie usypialo drugiej
    todo = [(nic, name) for nic, name in per_nic.items() if nic != name]
    if not todo:
        return nics

    was_active = run(["systemctl", "is-active", "--quiet", f"wifibroadcast@{ROLE}"])[0] == 0
    if was_active:
        run(["systemctl", "stop", f"wifibroadcast@{ROLE}"])

    # apply_nic_renames, a nie rename_nic po kolei: reguly mogly zamienic dwie
    # karty nazwami, a tego wprost jadro nie przepusci ("File exists")
    done = apply_nic_renames(dict(todo))

    nics = wfb_nics()
    if done and not nics:
        log("    wfb-nics nie widzi juz zadnej karty - cofam zmiane nazw")
        for old, name in done:
            rename_nic(name, old)
        try:
            UDEV_NAMES.unlink()
        except OSError:
            pass
        run(["udevadm", "control", "--reload-rules"])
        nics = wfb_nics()
    elif done:
        update_wfb_defaults(done)
        release_nics_from_network_stack(nics)  # wpisy NM/dhcpcd ida po nazwie

    if was_active:
        run(["systemctl", "start", f"wifibroadcast@{ROLE}"])
        time.sleep(2)
    remember_cards(nics)  # nazwy sa juz ustalone, wiec ewidencja zapisze te wlasciwe
    return nics


def _default_say(msg, status=None):
    """Domyslne 'gadanie' funkcji, ktore dzialaja i z TUI, i z konsoli - ekrany
    curses podaja wlasne say(tekst, status), instalator zostaje przy log()."""
    log(f"    {msg}")


def apply_nic_renames(wanted, say=_default_say):
    """wanted: {biezaca nazwa: docelowa}. Zamiana nazw miedzy dwiema kartami
    (TX <-> RX) nie moze isc wprost: jadro ani na moment nie pozwoli na dwa
    interfejsy o tej samej nazwie, wiec karta, ktorej nazwy ktos chce, idzie
    najpierw pod nazwe tymczasowa. Zwraca liste wykonanych par (stara, nowa)."""
    todo = {cur: tgt for cur, tgt in wanted.items() if cur != tgt}
    done, staged = [], {}

    for i, (cur, tgt) in enumerate(list(todo.items())):
        if tgt in todo:  # nazwe docelowa trzyma jeszcze inna przenoszona karta
            tmp = f"wfbswap{i}"
            ok, err = rename_nic(cur, tmp)
            if not ok:
                say(f"nie udalo sie zwolnic nazwy {cur}: {err}", "fail")
                return done
            staged[tmp] = tgt
            done.append((cur, tmp))
            del todo[cur]

    for cur, tgt in list(todo.items()) + list(staged.items()):
        ok, err = rename_nic(cur, tgt)
        if ok:
            # para z nazwa tymczasowa juz jest na liscie - podmieniamy ja na
            # docelowa, zeby update_wfb_defaults nie wpisalo do configu wfbswapN
            done = [(o, tgt if n == cur else n) for o, n in done]
            if not any(n == tgt for _, n in done):
                done.append((cur, tgt))
            say(f"nazwa karty: {cur} -> {tgt}")
        else:
            say(f"nie udalo sie przemianowac {cur} na {tgt}: {err}", "fail")
    return done


def assign_nic_role(nic, role, say=_default_say):
    """Ustawia karcie role ("tx", "rx" albo "txrx"), czyli nadaje jej pierwsza
    wolna nazwe tej roli (drone_TX, drone_TX2...). Pozostale karty zostaja bez
    zmian: kazda ma wlasna nazwe, wiec nic nie trzeba zamieniac, a kilka kart
    moze miec te sama role (np. dwie nadajace do porownania anten).

    Przypisanie zapisujemy w regulach udev (przypiete do MAC-a), wiec przezywa
    reboot i przelozenie dongla do innego portu USB. Zwraca (ok, komunikat)."""
    nics = wfb_nics()
    if nic not in nics:
        return False, f"karty {nic} juz nie ma"
    if role not in ROLE_TAGS:
        return False, f"nieznana rola {role}"
    if role_of_name(nic) == role:
        return True, f"{nic} juz ma role {role_txt(role, short=True)}"

    anchors = nic_anchors(nics)
    mine = anchors.get(nic)
    if not mine:
        return False, (f"{nic} nie ma ani czytelnego MAC-a, ani gniazda USB - "
                       "nie ma czego zakotwiczyc w regule udev")

    before = parse_name_rules()  # do wycofania, gdyby zmiana sie nie udala
    by_anchor = dict(before)
    try:
        present = {p.name for p in Path("/sys/class/net").iterdir()}
    except OSError:
        present = set(nics)
    # Nazwy kart wypietych tez sa zajete: ich regula dalej je trzyma, a dwie
    # reguly na jedna nazwe to po wpieciu karta, ktorej udev nie nazwie wcale.
    target = free_role_name(role, (set(by_anchor.values()) | present)
                            - {nic, by_anchor.get(mine)})
    if not target:
        return False, f"brak wolnej nazwy dla roli {role_txt(role, short=True)}"
    by_anchor[mine] = target

    was_active = service_active()
    if was_active:
        run(["systemctl", "stop", f"wifibroadcast@{ROLE}"])

    write_name_rules(by_anchor)
    done = apply_nic_renames({nic: target}, say)

    nics = wfb_nics()
    if not done or not nics:
        # Ten sam bezpiecznik co w ensure_nic_names: dzialajace lacze jest
        # wazniejsze niz przydzial rol, wiec cofamy wszystko - takze regule,
        # bo inaczej karta zmienilaby role dopiero po reboocie, niespodzianie.
        for old, new in reversed(done):
            rename_nic(new, old)
        write_name_rules(before)
        if was_active:
            run(["systemctl", "start", f"wifibroadcast@{ROLE}"])
        if not done:
            return False, f"nie udalo sie przemianowac {nic} - rola bez zmian"
        return False, "po zmianie nazwy wfb-nics nie widzi kart - wycofano"

    update_wfb_defaults(done)
    release_nics_from_network_stack(nics)
    remember_cards(nics)
    ensure_tx_split(nics)  # 'off' w wifi_txpower musi trafic na karty rx wg NOWYCH nazw

    if was_active:
        run(["systemctl", "start", f"wifibroadcast@{ROLE}"])
        time.sleep(3)
        if not service_active():
            return False, f"usluga nie wstala po zmianie: {service_state_txt()}"
    _nic_status_cache["val"] = None
    return True, f"{nic} -> {target} ({role_txt(role)})"


def step_config():
    log("==> [7/7] /etc/wifibroadcast.cfg i usluga")

    # Zawsze odswiezamy blackliste - niezaleznie od tego czy config juz byl,
    # bo nowsze jadra (6.x) maja WBUDOWANY sterownik rtw88_8812au, ktory
    # przechwytuje karte przy kazdym boocie zanim doda sie 88XXau_wfb.
    # Moc nadawania: zachowujemy juz ustawiona wartosc, a jesli jeszcze jej nie
    # bylo - domyslnie pulap (90% skali). Wartosc i tak przechodzi przez
    # clamp_tx_power, wiec starsza instalacja z 63 zjedzie tu do pulapu sama.
    tx_power = clamp_tx_power(parse_tx_power())
    write_modprobe_wfb(tx_power)
    apply_tx_power_live(tx_power)

    sysctl = Path("/etc/sysctl.conf")
    txt = sysctl.read_text() if sysctl.exists() else ""
    if "net.core.bpf_jit_enable = 1" not in txt:
        with sysctl.open("a") as f:
            f.write("net.core.bpf_jit_enable = 1\n")
    run(["sysctl", "-p"])

    release_nics_from_network_stack(wfb_nics())

    if not CFG_PATH.exists():
        CFG_PATH.write_text(build_config(DEFAULT_CHANNEL, DEFAULT_REGION))
    else:
        # Config juz jest (zwykle przynosi go pakiet wfb-ng, instalowany krok
        # wczesniej) - nie deptamy go, ale MUSIMY dopisac to, czego w nim nie
        # ma. Bez wifi_channel wfb-ng bierze swoja wartosc domyslna, czyli 161
        # = 5805 MHz, i caly link wstaje na 5.8 GHz zamiast na 2.4 GHz.
        log("    config juz istnieje, zostawiam (edytuj przez menu ponizej)")
        has_channel, has_region = cfg_has_common()
        if not has_channel:
            set_cfg_option("common", "wifi_channel", DEFAULT_CHANNEL)
            log(f"    dopisano brakujacy wifi_channel = {DEFAULT_CHANNEL}"
                f" ({channel_freq(DEFAULT_CHANNEL)} MHz) - bez tego wfb-ng")
            log("    uzylby swojego domyslnego kanalu 161, czyli 5.8 GHz")
        if not has_region:
            set_cfg_option("common", "wifi_region", f"'{DEFAULT_REGION}'")
            log(f"    dopisano brakujacy wifi_region = '{DEFAULT_REGION}'")

    run(["systemctl", "daemon-reload"])
    run(["systemctl", "enable", "--now", f"wifibroadcast@{ROLE}"])


def full_setup():
    log("################################################################")
    log(f"# WFB-NG setup - rola: {ROLE}")
    log("################################################################")
    step_packages()
    step_rfkill()
    step_driver()
    step_tun()
    step_wfb_ng_package()
    step_keys()
    step_config()
    log("")
    log("=== Instalacja zakonczona ===")


# ------------------------- autostart po reboocie -------------------------

def autostart_unit_text():
    """Jednostka systemd odpalajaca TEN plik z flaga --autostart. Sciezki
    (python i skrypt) wchodza do niej na sztywno, wiec po przeniesieniu pliku
    trzeba ja przepisac - robi to install_autostart() przy kazdym starcie
    z reki. Cudzyslowy, bo skrypt moze lezec w katalogu ze spacja."""
    return (
        "[Unit]\n"
        f"Description=WFB-NG {ROLE}: wykrywanie i naprawa kart po starcie systemu\n"
        f"After=wifibroadcast@{ROLE}.service\n"
        f"Wants=wifibroadcast@{ROLE}.service\n"
        "\n"
        "[Service]\n"
        # oneshot + RemainAfterExit: to nie demon, tylko jednorazowa robota po
        # boocie. Bez RemainAfterExit systemd pokazywalby ja jako "inactive",
        # czyli nie do odroznienia od "w ogole sie nie uruchomila".
        "Type=oneshot\n"
        "RemainAfterExit=yes\n"
        f'ExecStart="{sys.executable}" "{SCRIPT_PATH}" {AUTOSTART_FLAG}\n'
        # Przepiecie sterownika, udev i restart uslugi to kilkanascie sekund,
        # a przy niewykrytej karcie dochodzi jeszcze druga proba - domyslny
        # limit 90 s potrafi tu wejsc w droge.
        "TimeoutStartSec=300\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


def autostart_enabled():
    code, out = run(["systemctl", "is-enabled", AUTOSTART_UNIT_NAME])
    return code == 0 and out.strip() == "enabled"


def install_autostart():
    """Idempotentne: pisze jednostke tylko wtedy, gdy jej nie ma albo gdy
    wskazuje na inna kopie skryptu. Zwraca (ok, opis)."""
    want = autostart_unit_text()
    try:
        have = AUTOSTART_UNIT.read_text() if AUTOSTART_UNIT.exists() else None
    except OSError:
        have = None

    if have != want:
        try:
            AUTOSTART_UNIT.write_text(want)
        except OSError as e:
            return False, f"nie moge zapisac {AUTOSTART_UNIT}: {e}"
        run(["systemctl", "daemon-reload"])

    if not autostart_enabled():
        code, out = run(["systemctl", "enable", AUTOSTART_UNIT_NAME])
        if code != 0:
            return False, f"systemctl enable {AUTOSTART_UNIT_NAME}: {out.strip()[:90]}"

    return True, f"{AUTOSTART_UNIT_NAME} -> {SCRIPT_PATH}"


def autostart_status():
    """(status, szczegol) dla weryfikacji: czy po nastepnym reboocie ktokolwiek
    przepnie karty i poprawi usluge."""
    if not AUTOSTART_UNIT.exists():
        return "fail", (f"brak {AUTOSTART_UNIT_NAME} - po restarcie Pi nikt nie przepnie "
                        "kart ani nie poprawi uslugi")
    try:
        txt = AUTOSTART_UNIT.read_text()
    except OSError:
        txt = ""
    if str(SCRIPT_PATH) not in txt:
        return "warn", (f"{AUTOSTART_UNIT_NAME} uruchamia inna kopie skryptu niz ta "
                        f"({SCRIPT_PATH}) - uruchom ten plik raz z reki, przepisze wpis")
    if not autostart_enabled():
        return "fail", (f"{AUTOSTART_UNIT_NAME} istnieje, ale jest wylaczony - "
                        f"sudo systemctl enable {AUTOSTART_UNIT_NAME}")

    code, out = run(["systemctl", "show", AUTOSTART_UNIT_NAME, "-p", "Result", "--value"])
    result = out.strip() if code == 0 else ""
    if result and result != "success":
        return "warn", (f"wlaczony, ale ostatnie uruchomienie skonczylo sie na '{result}' - "
                        f"journalctl -u {AUTOSTART_UNIT_NAME}")
    return "ok", f"{AUTOSTART_UNIT_NAME} wlaczony -> {SCRIPT_PATH}"


# ------------------------- ochrona przed zla rola -------------------------

def peer_role_nics():
    """Interfejsy nazwane jak karty DRUGIEJ roli (np. gs_TXRX, drone_RX2) - ale
    tylko wtedy, gdy zadnego naszego tu nie ma. Nazwy sa przypiete do MAC-ow
    przez udev, wiec cudza nazwa na maszynie znaczy "to Pi bylo urzadzane jako
    druga strona", a nie "ktos przypadkiem tak nazwal interfejs". Gdy sa nazwy
    obu rol naraz, nie orzekamy niczego: to stan po recznym grzebaniu i lepiej
    puscic uzytkownika dalej, niz zablokowac mu jedyne narzedzie do posprzatania."""
    try:
        present = sorted(p.name for p in Path("/sys/class/net").iterdir())
    except OSError:
        return []
    if any(role_of_name(n) for n in present):
        return []
    return [n for n in present if (parse_nic_name(n) or ("",))[0] == PEER_NAME]


def refuse_wrong_role():
    """True = to Pi drugiej roli, konczymy bez dotykania czegokolwiek.

    Uruchomienie gs.py na dronie (albo odwrotnie) konczylo sie tym, ze skrypt
    uznawal cudze karty za swoje i startowal na nich wifibroadcast@<nasza
    rola>. Dwa serwery wfb-ng na tych samych interfejsach przestawiaja je
    nawzajem (ip link down, iw set monitor, iw set channel), wiec ktorys
    zawsze przegrywa i pada - a kazdy jego restart to NOWY klucz sesji, czyli
    link zrywajacy sie cyklicznie co kilkanascie sekund. Do tego zostawal po
    tym wpis w autostarcie i balagan wracal po kazdym reboocie.

    Dlatego to jest odmowa, a nie ostrzezenie, i musi zadzialac przed
    install_autostart() oraz przed czymkolwiek, co wola systemctl."""
    theirs = peer_role_nics()
    if not theirs:
        return False

    log(f"==> BLAD: to jest Pi roli '{PEER_NAME}', a uruchomiles {SCRIPT_PATH.name} (rola '{ROLE}').")
    log(f"    Karty tej maszyny: {', '.join(theirs)} - te nazwy naleza do '{PEER_NAME}'.")
    log(f"    Uruchom tutaj:  sudo python3 {SCRIPT_PATH.parent / (PEER_NAME + '.py')}")
    log("")
    log("    Nie ruszam niczego. Dwa serwery wfb-ng na tych samych kartach")
    log("    wywalaja sie nawzajem i usluga pada w kolko - link zrywa sie wtedy")
    log("    cyklicznie, mimo ze sygnal jest doskonaly.")

    # Slady po poprzednim takim uruchomieniu sprzatamy nie sami, tylko
    # podajemy gotowa komende: to jest cudza maszyna i decyzja nalezy do
    # uzytkownika, a nie do skryptu, ktory wlasnie przyznal sie do pomylki.
    mess = []
    if run(["systemctl", "is-active", "--quiet", f"wifibroadcast@{ROLE}"])[0] == 0:
        mess.append(f"wifibroadcast@{ROLE}")
    elif run(["systemctl", "is-enabled", "--quiet", f"wifibroadcast@{ROLE}"])[0] == 0:
        mess.append(f"wifibroadcast@{ROLE}")
    if AUTOSTART_UNIT.exists():
        mess.append(AUTOSTART_UNIT_NAME)
    if mess:
        log("")
        log(f"    UWAGA: zostaly tu slady roli '{ROLE}' z wczesniejszego uruchomienia.")
        log("    Skasuj je, inaczej wroca po reboocie:")
        log(f"      sudo systemctl disable --now {' '.join(mess)}")
    return True


def enforce_tx_power_cap():
    """Sciaga moc do pulapu, jesli gdzies zostala wyzsza. Wolane przy KAZDYM
    starcie, bo instalacje sprzed wprowadzenia pulapu maja w modprobe.d wpisane
    pelne 63 i nic samo tego nie obnizy - full_setup() juz sie tam nie odpali,
    a menu trzeba by odwiedzic recznie na obu Pi. Zwraca True, gdy cos ruszono."""
    saved, live = parse_tx_power(), read_tx_power_live()
    too_high = [v for v in (saved, live)
                if v is not None and v.isdigit() and int(v) > TX_POWER_CAP]
    if not too_high:
        return False

    log(f"    Moc TX: {'/'.join(too_high)} przekracza pulap {TX_POWER_CAP}"
        f" (90% z {TX_POWER_MAX}) - obnizam.")
    log("    Pelna moc 8812AU potrafi wylaczyc Pi poborem pradu z portu USB.")
    write_modprobe_wfb(saved)      # obie funkcje same przycinaja do pulapu
    if not apply_tx_power_live(saved):
        log("    (na zywo sie nie udalo - zadziala po przeladowaniu modulu albo reboocie)")
    return True


def wait_for_dongles(timeout=30):
    """Po boocie USB bywa jeszcze niepoliczone - multi-user.target nie czeka na
    dongle, a dwa 8812AU na zasilaniu przez hub potrafia zglosic sie kilkanascie
    sekund pozniej. Zamiast sztywnego sleepa czekamy, az pokaza sie w lsusb."""
    deadline = time.monotonic() + timeout
    while True:
        dongles = usb_rtl_dongles()
        if len(dongles) >= EXPECTED_NICS or time.monotonic() >= deadline:
            return dongles
        time.sleep(2)


def autostart_run():
    """Tryb bez TUI, odpalany przez systemd po kazdym boocie: to samo
    wykrywanie i te same naprawy, co przy starcie z reki. Setupu tu NIE
    puszczamy - apt-get i budowanie sterownika w trakcie bootu (czesto jeszcze
    bez sieci) to ostatnia rzecz, jakiej sie tu chce."""
    log(f"==> Autostart {ROLE} ({AUTOSTART_UNIT_NAME})")
    # Ta jednostka mogla zostac po pomylkowym uruchomieniu skryptu nie tej roli
    # na cudzym Pi. Wtedy budzi sie po kazdym boocie i psuje dzialajacy link,
    # a w journalu nie widac dlaczego - stad ten sam warunek co przy starcie
    # z reki, tylko wczesniej niz cokolwiek innego.
    if refuse_wrong_role():
        return 1

    if not setup_artifacts_present():
        log("    Setup nie jest skonczony - uruchom recznie:")
        log(f"    sudo python3 {SCRIPT_PATH}")
        return 1

    dongles = wait_for_dongles()
    log(f"    lsusb po starcie: {len(dongles)} z {EXPECTED_NICS} dongli")

    # Jadra 6.x maja WBUDOWANY rtw88_8812au i przy kazdym boocie potrafia
    # przejac karte, zanim ktokolwiek zaladuje nasz modul. Modprobe tutaj, zeby
    # detect_nics_startup mialo pod co przepinac.
    if not driver_loaded():
        log("    Modul 88XXau_wfb niezaladowany - modprobe")
        run(["modprobe", "88XXau_wfb"])

    detect_nics_startup()
    return 0


# ------------------------- wykrywanie kart przy starcie -------------------------

def detect_nics_startup():
    """Odpalane przy KAZDYM starcie, jeszcze przed TUI: czy sa wszystkie
    dongle, czy kazdy dostal interfejs pod naszym sterownikiem i czy usluga
    ich uzywa. Jesli czegos brakuje - proba naprawy (przepiecie sterownika,
    udev, restart uslugi), bo to sa dokladnie te trzy powody, dla ktorych
    druga karta "jest, a nie dziala"."""
    log(f"==> Wykrywanie kart RTL88xx (minimum: {EXPECTED_NICS})")

    dongles = usb_rtl_dongles()
    log(f"    lsusb: {len(dongles)} szt.")
    for d in dongles:
        log(f"      - {d}")

    nics = wfb_nics()
    if len(nics) < EXPECTED_NICS:
        log(f"    wfb-nics: {len(nics)} z {EXPECTED_NICS} - probuje przepiac reszte pod {TARGET_USB_DRIVER}...")
        rebind_to_wfb_driver()
        run(["udevadm", "trigger", "--action=add", "--subsystem-match=usb"])
        run(["udevadm", "settle"], timeout=15)
        time.sleep(2)
        nics = wfb_nics()

    nics = ensure_nic_names()

    # WFB_NICS moze nie zgadzac sie z tym, co podpiete (stary uklad dwoch kart,
    # stara nazwa karty) - sam restart nizej tego nie naprawi, bo znowu przeczyta
    # ten sam plik. sync_wfb_nics() poprawia liste kart PRZED restartem.
    sync_wfb_nics()

    for nic in nics:
        d = nic_details(nic)
        log(f"    {nic}{nic_role_txt(nic)}: {d['driver']} mac={d['mac']} tryb={d['mode']} kanal={d['channel']}")
        log(f"      gniazdo USB {nic_usb_txt(nic)}")

    for entry in missing_cards(nics):
        # Karty nie ma, wiec systemu nie ma o co pytac - to jedyne miejsce,
        # w ktorym po wypieciu dongla widac, KTORA karta zniknela.
        log(f"    BRAKUJE: {card_txt(entry)}")

    if not nics:
        log("    BLAD: zadna karta nie jest podpieta pod sterownik wfb.")
        log("    Sprawdz: lsusb | grep -i 88   oraz   dmesg | tail -50")
        return nics

    release_nics_from_network_stack(nics)
    enforce_tx_power_cap()

    channel, region = wfb_effective_common()
    freq = channel_freq(channel)
    log(f"    Radio: kanal {channel}" + (f" ({freq} MHz)" if freq else "") + f", region {region}")
    source = channel_source_note(channel)
    if source:
        log(f"    UWAGA: {source}")
    if freq and freq > 3000:
        log(f"    UWAGA: link stoi na {freq / 1000:.1f} GHz, a ten skrypt jest pisany pod")
        log(f"    2.4 GHz (kanal {DEFAULT_CHANNEL} = {channel_freq(DEFAULT_CHANNEL)} MHz).")
        log("    Zmien w menu: 'Kanal i czestotliwosc' - i tak samo po drugiej stronie.")

    mode, code = key_mode()
    if mode == "sparowane":
        log(f"    Klucze: sparowane kodem {format_pairing_code(code)}, odcisk {key_fingerprint(DRONE_KEY)}")
    elif mode == "wbudowane":
        log("    Klucze: wbudowane, te same po obu stronach - nic nie kopiujesz")
    elif mode == "wlasne":
        log(f"    Klucze: wlasne, odcisk drone.key={key_fingerprint(DRONE_KEY)} "
            f"gs.key={key_fingerprint(GS_KEY)} - musi byc IDENTYCZNY na dronie i gs")

    if len(nics) < EXPECTED_NICS:
        log(f"    UWAGA: dziala {len(nics)} z {EXPECTED_NICS} kart. Sprawdz port USB, kabel")
        log("    i zasilanie - 8812AU przy nadawaniu potrafi przeciazyc porty RPi.")

    if ensure_video_service_type(nics):
        log(f"    {CFG_PATH}: wideo przestawione na udp_proxy - domyslny tryb")
        log(f"    (udp_direct_tx) nie umie nadawac z {len(nics)} kart i zabijal usluge.")
        run(["systemctl", "restart", f"wifibroadcast@{ROLE}"])
        time.sleep(3)

    apply_tx_split(nics, lambda msg, _status=None: log(f"    {msg}"))

    # Dongiel wpiety po starcie uslugi nie zostanie uzyty sam z siebie.
    unused = set(nics) - service_nics(set(nics))
    if unused:
        log(f"    Usluga nie uzywa: {' '.join(sorted(unused))} - restartuje wifibroadcast@{ROLE}...")
        run(["systemctl", "restart", f"wifibroadcast@{ROLE}"])
        time.sleep(3)
        still = set(nics) - service_nics(set(nics))
        if not still:
            log("    OK - usluga uzywa wszystkich kart.")
        elif not service_active():
            # Nie chodzi o karty - usluga w ogole nie wstaje. Powod jest
            # w journalu, wiec pokazujemy go od razu.
            log(f"    USLUGA NIE DZIALA (status: {service_state_txt()}), karty sa tu bez winy.")
            log("    Ostatnie linie journala:")
            for ln in service_last_errors():
                log(f"      {ln}")
            log(f"    Wiecej: journalctl -u wifibroadcast@{ROLE} -n 50")
        else:
            log(f"    Nadal poza usluga: {' '.join(sorted(still))} - zobacz: journalctl -u wifibroadcast@{ROLE} -n 50")

    return nics


# ------------------------- weryfikacja -------------------------

def collect_checks():
    checks = []

    dongles = usb_rtl_dongles()
    if len(dongles) >= EXPECTED_NICS:
        checks.append(("Dongle USB RTL88xx", "ok", f"{len(dongles)} szt. w lsusb (minimum {EXPECTED_NICS})"))
    elif dongles:
        checks.append(("Dongle USB RTL88xx", "fail",
                       f"tylko {len(dongles)} z {EXPECTED_NICS} - sprawdz port USB, kabel i zasilanie"))
    else:
        checks.append(("Dongle USB RTL88xx", "fail", "nie widac zadnej karty 88xx w lsusb"))

    power = usb_power_issues()
    if power:
        checks.append(("Zasilanie / porty USB", "warn",
                       f"{len(power)} zdarzen w dmesg, ostatnie: {power[-1][:70]}"))
    else:
        checks.append(("Zasilanie / porty USB", "ok", "brak over-current / under-voltage w dmesg"))

    code, out = run_tool("rfkill", "list")
    if "Soft blocked: yes" in out or "Hard blocked: yes" in out:
        checks.append(("rfkill", "fail", "karta zablokowana - sudo rfkill unblock all"))
    elif code == 127:
        checks.append(("rfkill", "warn", "nie znaleziono polecenia rfkill"))
    else:
        checks.append(("rfkill", "ok", "brak blokady"))

    if driver_loaded():
        checks.append(("Sterownik 88XXau_wfb", "ok", "zaladowany (lsmod)"))
    elif driver_built():
        checks.append(("Sterownik 88XXau_wfb", "warn", "zainstalowany, ale niezaladowany"))
    else:
        checks.append(("Sterownik 88XXau_wfb", "fail", "brak - uruchom skrypt ponownie"))

    nics = wfb_nics()
    remember_cards(nics)
    if len(nics) >= EXPECTED_NICS:
        checks.append(("Interfejsy wfb", "ok", f"{len(nics)} (minimum {EXPECTED_NICS}): {' '.join(nics)}"))
    elif nics:
        checks.append(("Interfejsy wfb", "fail",
                       f"tylko {len(nics)} z {EXPECTED_NICS}: {' '.join(nics)} "
                       f"- reszta wisi na innym sterowniku niz {TARGET_USB_DRIVER}"))
    else:
        checks.append(("Interfejsy wfb", "fail", "wfb-nics nie zwraca zadnego interfejsu"))

    # Ktora karta zniknela i skad. Bez ewidencji zostaje samo "1 z 2" - a przy
    # rozdziale rol brak karty NADAWCZEJ to zupelnie inna awaria niz brak
    # odbiorczej i szuka sie jej w innym miejscu.
    gone = missing_cards(nics)
    if gone:
        checks.append(("Brakujace karty", "fail",
                       "; ".join(card_txt(e) for e in gone)))
    elif nics:
        checks.append(("Karty i gniazda USB", "ok",
                       "; ".join(f"{n}{nic_role_txt(n)} w gniezdzie {nic_usb_txt(n, short=True)}"
                                 for n in nics)))

    if nics and Path("/etc/NetworkManager").is_dir():
        code, out = run_tool("nmcli", "-t", "-f", "DEVICE,STATE", "device")
        managed = [ln.split(":")[0] + "=" + ln.split(":")[1] for ln in out.splitlines()
                   if code == 0 and len(ln.split(":")) >= 2
                   and ln.split(":")[0] in nics and ln.split(":")[1] != "unmanaged"]
        if managed:
            checks.append(("NetworkManager", "fail",
                           f"zarzadza kartami wfb: {' '.join(managed)} - popraw {NM_CONF}"))
        else:
            checks.append(("NetworkManager", "ok", "karty wfb sa unmanaged"))

    cfg_channel = wfb_effective_common()[0] if CFG_PATH.exists() else None
    used_by_service = service_nics(set(nics))
    traffic = nic_traffic(nics) if nics else {}
    quiet = muted_nics(nics)  # karty, ktore config naprawde wycisza

    for i, nic in enumerate(nics, 1):
        d = nic_details(nic)
        rx_pps, tx_pps = traffic.get(nic, (0.0, 0.0))
        detail = (f"{d['driver']} mac={d['mac']} usb={d['usb']} tryb={d['mode']} "
                  f"kanal={d['channel']} rx={rx_pps:.0f}/s tx={tx_pps:.0f}/s")

        if nic not in used_by_service:
            status, detail = "fail", detail + " - usluga tej karty NIE uzywa"
        elif d["mode"] != "monitor":
            status, detail = "fail", detail + " - powinien byc monitor"
        elif cfg_channel and d["channel"] not in ("?", cfg_channel):
            status, detail = "fail", detail + f" - config mowi {cfg_channel}"
        elif rx_pps == 0 and tx_pps == 0:
            status, detail = "fail", detail + " - brak jakiegokolwiek ruchu"
        elif tx_pps == 0 and nic in quiet:
            status, detail = "ok", detail + " - tylko odbior (rola RX)"
        elif tx_pps == 0:
            # przy dwoch kartach wfb_tx potrafi nadawac tylko przez jedna,
            # wiec sam brak TX przy dzialajacym RX to jeszcze nie awaria
            status, detail = "warn", detail + " - odbiera, ale nie nadaje"
        elif rx_pps == 0:
            status, detail = "warn", detail + " - nadaje, ale nic nie odbiera (druga strona wylaczona?)"
        else:
            status = "ok"
        checks.append((f"Karta {i}/{len(nics)}: {nic}", status, detail))

    code, out = run(["lsmod"])
    if "tun" in out:
        checks.append(("Modul tun", "ok", "zaladowany"))
    else:
        checks.append(("Modul tun", "warn", "niezaladowany - sudo modprobe tun"))

    if wfb_ng_installed():
        checks.append(("Pakiet wfb-ng", "ok", "wfb_keygen obecny"))
    else:
        checks.append(("Pakiet wfb-ng", "fail", "brak wfb_keygen - pakiet niezainstalowany"))

    mode, code = key_mode()
    if mode == "sparowane":
        checks.append(("Klucze /etc/*.key", "ok",
                       f"sparowane kodem {format_pairing_code(code)} "
                       f"(odcisk {key_fingerprint(DRONE_KEY)}) - porownaj z druga strona"))
    elif mode == "wbudowane":
        checks.append(("Klucze /etc/*.key", "ok",
                       f"wbudowane (odcisk {key_fingerprint(DRONE_KEY)}) - identyczne po obu stronach"))
    elif mode == "wlasne":
        # wfb_keygen na kazdym urzadzeniu robi INNA pare, wiec dwa "zielone"
        # konce i tak sie nie dogadaja. Odciski musza sie zgadzac.
        checks.append(("Klucze /etc/*.key", "warn",
                       f"wlasne: drone.key={key_fingerprint(DRONE_KEY)} "
                       f"gs.key={key_fingerprint(GS_KEY)} - porownaj z druga strona"))
    else:
        missing = [p.name for p in (DRONE_KEY, GS_KEY) if not p.exists()]
        checks.append(("Klucze /etc/*.key", "fail", f"brakuje: {', '.join(missing)}"))

    live_tx = read_tx_power_live()
    saved_tx = parse_tx_power()
    if live_tx is None:
        checks.append(("Moc nadawania (TX)", "warn", "modul niezaladowany - nie moge odczytac"))
    elif live_tx == "0":
        checks.append(("Moc nadawania (TX)", "warn", "override wylaczony (0) - uzywana kalibracja EEPROM"))
    elif live_tx != saved_tx:
        checks.append(("Moc nadawania (TX)", "warn",
                       f"na zywo={live_tx}/{TX_POWER_CAP}, zapisane={saved_tx}/{TX_POWER_CAP} (niezgodne)"))
    elif live_tx.isdigit() and int(live_tx) > TX_POWER_CAP:
        # Zostalo po starszej wersji skryptu, ktora pozwalala na pelne 63.
        # To jest dokladnie ta wartosc, przy ktorej dongiel potrafi wylaczyc
        # Pi poborem pradu z USB - wiec fail, a nie warn.
        checks.append(("Moc nadawania (TX)", "fail",
                       f"{live_tx} przekracza pulap {TX_POWER_CAP} (90% z {TX_POWER_MAX}) - "
                       "wejdz w 'Moc nadawania (TX)' i zapisz od nowa"))
    elif live_tx.isdigit() and int(live_tx) < 10:
        # Spojna, ale bardzo niska wartosc to typowy cichy zabojca zasiegu -
        # link "dziala na biurku" i pada kilka metrow dalej. Zostaje warn,
        # bo do testow w pomieszczeniu ustawia sie ja swiadomie.
        checks.append(("Moc nadawania (TX)", "warn",
                       f"{live_tx}/{TX_POWER_CAP} - bardzo nisko, zasieg bedzie zaden"))
    else:
        checks.append(("Moc nadawania (TX)", "ok",
                       f"{live_tx}/{TX_POWER_CAP} (pulap = 90% z {TX_POWER_MAX})"))

    # Moc per karta i limity: bez latki sterownika jest tylko moc wspolna
    # (CARD_TXPOWER_PATCH). Zgodnosc ze sterownikiem sprawdzamy na zywo - config
    # wfb-ng czyta tylko przy starcie, wiec sam wpis niczego nie dowodzi.
    card_state = driver_card_txpower()
    powers = card_powers(nics) if nics else {}
    limits = card_limits(nics) if nics else {}
    if card_state == "on":
        plan = card_power_plan(nics, live=True)
        live = {n: card_power_live(n) for n in plan}
        wrong = {n: v for n, v in live.items() if v is not None and v != plan[n]}
        if wrong:
            checks.append(("Moc per karta", "warn",
                           "; ".join(f"{n}: powinno byc {plan[n]}, sterownik ma {v}"
                                     for n, v in sorted(wrong.items()))
                           + " - ustaw ponownie w 'Karty na zywo' albo zrestartuj usluge"))
        else:
            bits = [f"{n}={p}/{TX_POWER_CAP}" + (f" (limit {limits[n]})" if n in limits else "")
                    for n, p in sorted(plan.items())]
            bits += [f"{n}: limit {lim}, nadaje z wspolnej" for n, lim in sorted(limits.items())
                     if n not in plan]
            checks.append(("Moc per karta", "ok",
                           ("osobno: " + "; ".join(bits)) if bits
                           else "sterownik z latka; wszystkie karty na mocy wspolnej"))
    elif card_state == "reload":
        checks.append(("Moc per karta", "warn",
                       "sterownik z latka zbudowany, ale zaladowany jest stary - 'Karty na zywo' -> P"))
    else:
        checks.append(("Moc per karta", "warn" if powers or limits else "ok",
                       "sterownik bez latki - moc tylko wspolna dla wszystkich kart"
                       + (", zapisane moce i limity NIE dzialaja" if powers or limits else "")
                       + " ('Karty na zywo' -> P)"))

    # Rozdzial rol sprawdzamy na LICZNIKACH KARTY, a nie w configu ani w linii
    # polecen wfb_tx: wfb-ng podaje procesowi wszystkie interfejsy i dopiero
    # w srodku pomija te oznaczone jako rx-only (rx_only_wlan_ids). Jedynym
    # wiarygodnym dowodem jest wiec to, czy z karty cokolwiek wychodzi -
    # a przy wzmacniaczu jednokierunkowym "nadaje nie ta karta" to zepsuty lot.
    if nics and len(rx_only_nics(nics)) == len(nics) and any(role_of_name(n) for n in nics):
        checks.append(("Rozdzial RX/TX", "warn",
                       "zadna karta nie ma roli TX ani TX+RX - bezpiecznik zostawia nadawanie"
                       " na wszystkich; ustaw role w 'Karty na zywo'"))
    rx_only = quiet
    if rx_only:
        traffic = nic_traffic(nics)
        sending = {n: traffic[n][1] for n in rx_only if traffic.get(n, (0, 0))[1] > 0}
        tx_pps = {n: traffic[n][1] for n in nics if n not in rx_only and traffic.get(n, (0, 0))[1] > 0}
        cfg_ok = (get_cfg_option("common", "wifi_txpower") or "").count("'off'") == len(rx_only)
        if sending:
            checks.append(("Rozdzial RX/TX", "fail",
                           "nadaje takze " + ", ".join(f"{n} ({pps:.0f} pkt/s)"
                                                       for n, pps in sorted(sending.items()))
                           + " - ta karta ma tylko odbierac"
                           + ("" if cfg_ok else "; brak wpisu wifi_txpower w [common]")))
        elif not tx_pps:
            checks.append(("Rozdzial RX/TX", "warn",
                           "zadna karta nic nie nadaje - nie da sie tego teraz sprawdzic"))
        else:
            checks.append(("Rozdzial RX/TX", "ok",
                           "nadaje: " + ", ".join(f"{n} ({pps:.0f} pkt/s)"
                                                  for n, pps in sorted(tx_pps.items()))
                           + f"   cisza na: {', '.join(sorted(rx_only))}"))

    if CFG_PATH.exists():
        ch, reg = wfb_effective_common()
        vtype = video_service_type()
        detail = f"kanal={ch} region={reg} rola={ROLE} wideo={vtype or '?'}"
        source = channel_source_note(ch)
        if len(nics) > 1 and vtype == "udp_direct_tx":
            checks.append(("wifibroadcast.cfg", "fail",
                           detail + f" - ten tryb nie umie {len(nics)} kart, usluga bedzie sie wywalac"))
        elif source:
            checks.append(("wifibroadcast.cfg", "warn", detail + " - " + source))
        else:
            checks.append(("wifibroadcast.cfg", "ok", detail))

        freq = channel_freq(ch)
        country, ranges = reg_domain_ranges()
        if freq and ranges:
            span = channel_span(freq)
            where = f"kanal {ch}: {freq} MHz, HT20 zajmuje {span[0]}-{span[1]} MHz"
            if any(lo <= span[0] and span[1] <= hi for lo, hi in ranges):
                checks.append(("Region vs kanal", "ok", f"{country}: {where} - caly w dozwolonym pasmie"))
            elif any(lo <= freq <= hi for lo, hi in ranges):
                # srodek lapie sie w przydziale, ale polowka kanalu z niego
                # wystaje - istotne przy krawedziach i przy pracy z PA
                checks.append(("Region vs kanal", "warn",
                               f"{country}: {where} - srodek w pasmie, ale kanal wystaje poza krawedz"))
            else:
                bands = ", ".join(f"{lo}-{hi}" for lo, hi in ranges)
                checks.append(("Region vs kanal", "fail",
                               f"{country} nie obejmuje {freq} MHz ({where}) - karta nie bedzie "
                               f"nadawac. Dozwolone [MHz]: {bands}"))
    else:
        checks.append(("wifibroadcast.cfg", "fail", "plik nie istnieje"))

    props = service_props()
    if service_active(props):
        checks.append((f"Usluga wifibroadcast@{ROLE}", "ok", f"aktywna ({service_state_txt(props)})"))
    else:
        # "activating" tez tu wpada: usluga w petli restartow wyglada na
        # wstajaca, a nie dziala. Ogon journala od razu obok, bo bez niego
        # ten check tylko stwierdza fakt, zamiast pokazac przyczyne.
        checks.append((f"Usluga wifibroadcast@{ROLE}", "fail",
                       f"status: {service_state_txt(props)} - ponizej ostatnie linie journala"))
        for ln in service_last_errors(5):
            checks.append(("  journal", "fail", ln[:110]))

    # Wszystko powyzej opisuje TERAZ - a po reboocie karty potrafia wrocic pod
    # sterownik z jadra i link nie wstaje. Ten check pilnuje, ze jest kto to
    # naprawic bez wchodzenia na Pi.
    status, detail = autostart_status()
    checks.append(("Autostart po restarcie Pi", status, detail))

    code, out = run(["ip", "-brief", "addr", "show", f"{ROLE}-wfb"])
    if code == 0 and out.strip():
        checks.append((f"Interfejs {ROLE}-wfb", "ok", out.strip()))
    else:
        checks.append((f"Interfejs {ROLE}-wfb", "fail", "brak interfejsu tunelu"))

    code, loss, avg = ping_stats(PEER_IP)
    if code == 0:
        detail = f"utrata {loss}%, srednio {avg} ms" if avg else f"utrata {loss}%"
        checks.append((f"Ping przez RTL (tunel, {PEER_IP})", "ok", detail))
    else:
        checks.append((f"Ping przez RTL (tunel, {PEER_IP})", "warn", f"brak odpowiedzi (utrata {loss}%)"))

    if check_ssh(PEER_IP):
        checks.append((f"SSH do {PEER_NAME} ({PEER_IP}:{SSH_PORT})", "ok", "port otwarty, SSH odpowiada"))
    else:
        checks.append((f"SSH do {PEER_NAME} ({PEER_IP}:{SSH_PORT})", "warn", "brak polaczenia na porcie 22"))

    # Zle sparowane klucze NIE zapalaja sie tu na czerwono: kazda strona widzi
    # swoje pliki jako poprawne i dopiero porownanie odciskow miedzy dronem
    # a gs cokolwiek mowi. Dlatego przy kazdym bledzie piszemy o tym wprost,
    # i to na samej gorze listy - inaczej szuka sie usterki w kartach, kanale
    # i konfigu, a wystarczy porownac osiem znakow kodu.
    if any(st == "fail" for _, st, _ in checks):
        kmode, kcode = key_mode()
        if kmode == "sparowane":
            here = f"sparowane kodem {format_pairing_code(kcode)}, odcisk {key_fingerprint(DRONE_KEY)}"
        elif kmode == "wbudowane":
            here = f"wbudowane, odcisk {key_fingerprint(DRONE_KEY)}"
        elif kmode == "wlasne":
            here = f"wlasne, odcisk {key_fingerprint(DRONE_KEY)}"
        else:
            here = "brak plikow kluczy - link nie ma prawa dzialac"
        checks[:0] = [
            ("Zanim zaczniesz szukac: PAROWANIE", "warn",
             "cos ponizej jest na czerwono - to moze byc zwyczajnie zle parowanie"),
            ("  parowanie", "warn", f"tutaj ({ROLE}): {here}"),
            ("  parowanie", "warn",
             f"na {PEER_NAME} odcisk MUSI byc taki sam - menu -> Klucze i parowanie"),
            ("  parowanie", "warn",
             "kazda strona widzi swoje klucze jako OK, wiec latwo o tym zapomniec"),
        ]

    return checks


# ------------------------- warstwa curses -------------------------

STATUS_ICON = {"ok": "[ OK ]", "warn": "[WARN]", "fail": "[BLAD]"}


def init_colors():
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_GREEN, -1)
    curses.init_pair(2, curses.COLOR_RED, -1)
    curses.init_pair(3, curses.COLOR_YELLOW, -1)
    curses.init_pair(4, curses.COLOR_BLACK, curses.COLOR_CYAN)   # naglowek
    curses.init_pair(5, curses.COLOR_BLACK, curses.COLOR_WHITE)  # zaznaczenie


def color_for(status):
    return curses.color_pair({"ok": 1, "warn": 3, "fail": 2}.get(status, 0))


def safe_addstr(win, y, x, text, attr=0):
    h, w = win.getmaxyx()
    if 0 <= y < h and 0 <= x < w:
        try:
            win.addstr(y, x, text[: max(0, w - x - 1)], attr)
        except curses.error:
            pass


def draw_header(stdscr, title):
    h, w = stdscr.getmaxyx()
    safe_addstr(stdscr, 0, 0, " " * w, curses.color_pair(4))
    safe_addstr(stdscr, 0, 2, title, curses.color_pair(4) | curses.A_BOLD)
    # Kod parowania (albo skad wziete sa klucze, gdy kodu nie ma) na kazdym
    # ekranie - zeby dalo sie go poredniczo porownac z drugim urzadzeniem bez
    # wchodzenia w menu "Klucze i parowanie".
    mode, code = key_mode()
    tag = f"kod parowania: {format_pairing_code(code)}" if code else f"klucze: {mode}"
    x = w - len(tag) - 2
    if x > len(title) + 4:
        safe_addstr(stdscr, 0, x, tag, curses.color_pair(4))


def pause(stdscr, msg="Nacisnij dowolny klawisz, aby wrocic..."):
    h, w = stdscr.getmaxyx()
    safe_addstr(stdscr, h - 1, 2, msg, curses.A_DIM)
    stdscr.refresh()
    stdscr.getch()


def scroll_view(stdscr, title, lines):
    """Prosty pager na liste (tekst, atrybut) - tresc bywa dluzsza niz ekran."""
    top = 0
    while True:
        stdscr.clear()
        draw_header(stdscr, title)
        h, _ = stdscr.getmaxyx()
        view = max(1, h - 3)

        for i, (text, attr) in enumerate(lines[top:top + view]):
            safe_addstr(stdscr, 2 + i, 2, text, attr)

        if len(lines) > view:
            hint = f"Strzalki = przewijanie ({top + 1}-{min(top + view, len(lines))}/{len(lines)}), q = powrot"
        else:
            hint = "Nacisnij dowolny klawisz, aby wrocic..."
        safe_addstr(stdscr, h - 1, 2, hint, curses.A_DIM)
        stdscr.refresh()

        key = stdscr.getch()
        if key in (curses.KEY_DOWN, ord("j")) and top + view < len(lines):
            top += 1
        elif key in (curses.KEY_UP, ord("k")) and top > 0:
            top -= 1
        elif key == curses.KEY_NPAGE:
            top = min(max(0, len(lines) - view), top + view)
        elif key == curses.KEY_PPAGE:
            top = max(0, top - view)
        else:
            break


def config_overview_lines():
    """Wszystko, co warto miec pod reka na jednym ekranie: adresy IP tego
    urzadzenia, parametry radia, karty, klucze, stan uslugi - a na koncu
    surowa tresc /etc/wifibroadcast.cfg. Wczesniej byl tu sam plik, przez co
    najprostsze pytania ("pod jakim IP jest ten Pi?", "jaka mam moc?")
    wymagaly wychodzenia do powloki."""
    lines = []

    def section(title):
        if lines:
            lines.append(("", 0))
        lines.append((title, curses.A_BOLD))

    def row(label, value, status=None):
        lines.append((f"  {label:<14}{value}", color_for(status) if status else 0))

    section("Urzadzenie")
    row("rola", ROLE)
    row("hostname", socket.gethostname())
    row("wfb-ng", wfb_ng_version())
    row("jadro", os.uname().release)

    section("Siec")
    addrs = ip_addresses()
    tunnel = f"{ROLE}-wfb"
    for nic, addr in addrs:
        row(nic, addr + ("   <- tunel wfb" if nic == tunnel else ""),
            "ok" if nic == tunnel else None)
    if not any(nic == tunnel for nic, _ in addrs):
        row(tunnel, "brak interfejsu tunelu - link nie stoi", "fail")
    row("druga strona", f"{PEER_IP}   (ping i ssh sprawdza weryfikacja)")

    section("Radio")
    # to, czego wfb-ng NAPRAWDE uzywa - nie to, co u nas w pliku (brak wpisu
    # oznacza kanal 161 z master.cfg, czyli 5.8 GHz, a nie nasze 13)
    ch, reg = wfb_effective_common()
    freq = channel_freq(ch)
    span = channel_span(freq)
    row("kanal", f"{ch}" + (f"   {freq} MHz, HT20 zajmuje {span[0]}-{span[1]} MHz" if freq else ""),
        "warn" if freq and freq > 3000 else None)
    source = channel_source_note(ch)
    if source:
        row("", source, "warn")
    country, ranges = reg_domain_ranges()
    in_band = bool(freq and ranges and any(lo <= span[0] and span[1] <= hi for lo, hi in ranges))
    row("region", f"{reg}   (w jadrze: {country or '?'})", "ok" if in_band else "warn")
    live_tx, saved_tx = read_tx_power_live(), parse_tx_power()
    row("moc TX", f"{live_tx or '?'}/{TX_POWER_CAP} na zywo, {saved_tx}/{TX_POWER_CAP} zapisane",
        "warn" if live_tx != saved_tx else None)
    row("tryb wideo", video_service_type() or "?")

    section("Karty")
    nics = wfb_nics()
    used = service_nics(set(nics)) if nics else set()
    traffic = nic_traffic(nics) if nics else {}
    remember_cards(nics)
    for nic in nics:
        d = nic_details(nic)
        rx_pps, tx_pps = traffic.get(nic, (0.0, 0.0))
        row(nic, f"mac={d['mac']}  {d['driver']} {d['mode']} "
                 f"kan={d['channel']}{nic_role_txt(nic)}",
            "ok" if nic in used else "fail")
        row("", f"gniazdo USB {nic_usb_txt(nic)}")
        row("", f"rx={rx_pps:.0f}/s tx={tx_pps:.0f}/s   w usludze={'tak' if nic in used else 'NIE'}"
                f"{'   <- przez ta karte leci nadawanie' if tx_pps > 0 else ''}",
            "ok" if tx_pps > 0 else None)
    if nics:
        row("", "(licznik tx > 0 wskazuje karte, ktora faktycznie nadaje)")
    else:
        row("(brak)", "wfb-nics nie zwraca zadnego interfejsu", "fail")
    for entry in missing_cards(nics):
        row("BRAKUJE", card_txt(entry), "fail")

    section("Klucze")
    mode, code = key_mode()
    row("tryb", f"sparowane kodem {format_pairing_code(code)}" if mode == "sparowane" else mode)
    row("odcisk", f"drone.key={key_fingerprint(DRONE_KEY)} gs.key={key_fingerprint(GS_KEY)}"
                  "   - musi byc taki sam po obu stronach")

    section("Usluga")
    props = service_props()
    row(f"wifibroadcast@{ROLE}", service_state_txt(props),
        "ok" if service_active(props) else "fail")

    section(f"Plik {CFG_PATH}")
    if CFG_PATH.exists():
        for raw in CFG_PATH.read_text().splitlines():
            # 'streams' bywa bardzo dluga linia - lamiemy, zeby nie uciekala
            # poza ekran i dalo sie ja przeczytac w calosci
            while len(raw) > 100:
                lines.append(("  " + raw[:100], 0))
                raw = "      " + raw[100:]
            lines.append(("  " + raw, 0))
    else:
        row("", "plik nie istnieje jeszcze", "fail")

    return lines


def show_config_screen(stdscr):
    stdscr.clear()
    draw_header(stdscr, f"WFB-NG [{ROLE}] - biezaca konfiguracja")
    safe_addstr(stdscr, 2, 2, "Zbieram dane...")
    stdscr.refresh()
    scroll_view(stdscr, f"WFB-NG [{ROLE}] - biezaca konfiguracja", config_overview_lines())


def prompt_line(stdscr, y, label, default):
    safe_addstr(stdscr, y, 2, f"{label} [{default}]: ")
    stdscr.refresh()
    curses.echo()
    curses.curs_set(1)
    try:
        raw = stdscr.getstr(y, 2 + len(f"{label} [{default}]: "), 30).decode().strip()
    except curses.error:
        raw = ""
    curses.noecho()
    curses.curs_set(0)
    return raw if raw else default


def region_screen(stdscr):
    """Region (CRDA). Kanalu sie tu NIE ustawia - jest od tego osobny ekran ze
    skanem pasma i trybem automatycznym, a moc nadawania ma wlasny ekran w
    menu glownym ('Moc nadawania (TX)'). Dwa miejsca do zmiany tej samej
    rzeczy to prosta droga do tego, ze jedno pokazuje co innego niz drugie.
    Ekran otwiera sie z listy kanalow klawiszem 'r'."""
    stdscr.clear()
    draw_header(stdscr, f"WFB-NG [{ROLE}] - region")

    channel, cur_region = wfb_effective_common()
    freq = channel_freq(channel)

    safe_addstr(stdscr, 2, 2, f"Puste pole = zostaw obecna wartosc (Enter). Rola: {ROLE}.")
    safe_addstr(stdscr, 3, 2, f"Kanal {channel}" + (f" ({freq} MHz)" if freq else "")
                + " zmienisz w ekranie 'Kanal i czestotliwosc'.", curses.A_DIM)

    region = prompt_line(stdscr, 5, "Region (CRDA)", cur_region)

    country, ranges = reg_domain_ranges()
    span = channel_span(freq)
    lines = [f"Region: {region}",
             f"Kanal zostaje: {channel}" + (f" ({freq} MHz)" if freq else ""),
             ""]
    # Kanal spoza pasma dozwolonego w regionie = karta w ogole nie nadaje,
    # a wyglada zdrowo. Lepiej powiedziec to PRZED zapisem niz szukac potem.
    if span and ranges and not any(lo <= span[0] and span[1] <= hi for lo, hi in ranges):
        lines.append(f"UWAGA: {span[0]}-{span[1]} MHz nie miesci sie w domenie {country}")
        if region != country:
            lines.append(f"(zapisujesz {region} - sprawdz potem w weryfikacji)")
        lines.append("")
    lines.append(f"Usluga wifibroadcast@{ROLE} zostanie zrestartowana.")

    if popup(stdscr, "Zapisac?", lines, buttons=("Tak", "Nie")) != 0:
        return

    save_common_config(channel, region)
    ensure_video_service_type(wfb_nics())  # gdyby config byl jeszcze sprzed migracji
    ensure_tx_split(wfb_nics())            # restart uslugi i tak jest ponizej
    _common_cache["val"] = None
    run(["systemctl", "daemon-reload"])
    code2, out2 = run(["systemctl", "enable", "--now", f"wifibroadcast@{ROLE}"])
    code3, out3 = run(["systemctl", "restart", f"wifibroadcast@{ROLE}"])

    if code2 == 0 and code3 == 0:
        popup(stdscr, "Zapisano", [f"wifibroadcast@{ROLE} uruchomiona."], status="ok")
    else:
        popup(stdscr, "Zapisano, ale usluga zglosila blad",
              [(out2 + " " + out3)[:70]], status="fail")


def tx_power_screen(stdscr):
    """Sama moc nadawania - bez regionu, ten ma wlasny ekran ('r' z listy
    kanalow). Zapis idzie od razu na zywo przez sysfs, wiec restart uslugi
    wifibroadcast tu nie jest potrzebny - patrz apply_tx_power_live."""
    stdscr.clear()
    draw_header(stdscr, f"WFB-NG [{ROLE}] - moc nadawania (TX)")

    cur_tx_power = parse_tx_power()

    safe_addstr(stdscr, 2, 2, f"Puste pole = zostaw obecna wartosc (Enter). Rola: {ROLE}.")

    # Pulap jest twardy juz na wejsciu, a nie dopiero przy zapisie: gdyby menu
    # przyjmowalo 63 i dopiero clamp_tx_power scinal to po cichu do 56,
    # uzytkownik widzialby w potwierdzeniu inna liczbe niz ta, ktora naprawde
    # trafia do sterownika.
    tx_power = ""
    def tx_ok(v):
        return v.isdigit() and 0 <= int(v) <= TX_POWER_CAP

    while not tx_ok(tx_power):
        tx_power = prompt_line(stdscr, 4,
                               f"Moc nadawania TX (0-{TX_POWER_CAP}, {TX_POWER_CAP}=max)",
                               cur_tx_power)
        if not tx_ok(tx_power):
            safe_addstr(stdscr, 5, 2,
                        f"Podaj liczbe 0-{TX_POWER_CAP} (0 = kalibracja EEPROM). Gorne "
                        f"{TX_POWER_MAX - TX_POWER_CAP} stopni skali jest zablokowane -",
                        color_for("fail"))
            safe_addstr(stdscr, 6, 2,
                        "dongiel na pelnej mocy potrafi wylaczyc Pi przez pobor pradu z USB.",
                        color_for("fail"))

    lines = [f"Moc TX: {tx_power}/{TX_POWER_CAP}"
             f" (pulap {TX_POWER_CAP} z {TX_POWER_MAX} = 90% skali)"]

    if popup(stdscr, "Zapisac?", lines, buttons=("Tak", "Nie")) != 0:
        return

    write_modprobe_wfb(tx_power)
    live_ok = apply_tx_power_live(tx_power)
    cards = reapply_card_powers()  # limity i wlasna moc kart wzgledem NOWEJ wspolnej

    if live_ok:
        popup(stdscr, "Zapisano", ["moc zastosowana natychmiast"] + cards, status="ok")
    else:
        popup(stdscr, "Zapisano",
              ["modul niezaladowany - moc zadziala po nast. zaladowaniu modulu"] + cards,
              status="warn")


def show_pairing_code_screen(stdscr, code):
    stdscr.clear()
    draw_header(stdscr, f"WFB-NG [{ROLE}] - kod parowania")
    safe_addstr(stdscr, 2, 2, "Przepisz ten kod na drugim urzadzeniu:")

    shown = f"  {format_pairing_code(code)}  "
    frame = "+" + "-" * len(shown) + "+"
    safe_addstr(stdscr, 4, 6, frame, curses.A_BOLD)
    safe_addstr(stdscr, 5, 6, "|", curses.A_BOLD)
    safe_addstr(stdscr, 5, 7, shown, curses.color_pair(5) | curses.A_BOLD)
    safe_addstr(stdscr, 5, 7 + len(shown), "|", curses.A_BOLD)
    safe_addstr(stdscr, 6, 6, frame, curses.A_BOLD)

    safe_addstr(stdscr, 8, 2, "Tam: menu -> Klucze i parowanie -> w (wpisz kod)")
    safe_addstr(stdscr, 10, 2, f"Odcisk kluczy tutaj: {key_fingerprint(DRONE_KEY)}", curses.A_BOLD)
    safe_addstr(stdscr, 11, 2, "Po sparowaniu odcisk musi byc taki sam po obu stronach.")
    safe_addstr(stdscr, 13, 2, f"Kod zapisany w {PAIRING_CODE_PATH} - da sie go tu podejrzec pozniej.")
    pause(stdscr)


def keys_screen(stdscr):
    while True:
        stdscr.clear()
        draw_header(stdscr, f"WFB-NG [{ROLE}] - klucze i parowanie")
        mode, code = key_mode()

        if mode == "sparowane":
            safe_addstr(stdscr, 2, 2, f"Stan: SPAROWANE kodem {format_pairing_code(code)}",
                        color_for("ok") | curses.A_BOLD)
            safe_addstr(stdscr, 3, 2, f"Odcisk kluczy: {key_fingerprint(DRONE_KEY)} "
                                      "- na drugiej stronie musi byc taki sam.")
        elif mode == "wbudowane":
            safe_addstr(stdscr, 2, 2, "Stan: KLUCZE WBUDOWANE (te same w kazdej kopii skryptu)",
                        color_for("warn") | curses.A_BOLD)
            safe_addstr(stdscr, 3, 2, "Dziala od razu, ale kto ma ten skrypt, ten slyszy transmisje.")
        elif mode == "wlasne":
            safe_addstr(stdscr, 2, 2, f"Stan: WLASNA PARA (drone.key={key_fingerprint(DRONE_KEY)} "
                                      f"gs.key={key_fingerprint(GS_KEY)})",
                        color_for("warn") | curses.A_BOLD)
            safe_addstr(stdscr, 3, 2, "Wymaga recznego skopiowania obu plikow na druga strone.")
        else:
            safe_addstr(stdscr, 2, 2, "Stan: BRAK KLUCZY", color_for("fail") | curses.A_BOLD)

        safe_addstr(stdscr, 5, 2, "n = nowy kod parowania (pokaze kod i od razu zastosuje tutaj)")
        safe_addstr(stdscr, 6, 2, "w = wpisz kod z drugiego urzadzenia")
        safe_addstr(stdscr, 7, 2, "b = wroc do kluczy wbudowanych")
        safe_addstr(stdscr, 8, 2, "q = powrot do menu")
        stdscr.refresh()

        key = stdscr.getch()

        if key in (ord("n"), ord("N")):
            code = new_pairing_code()
            apply_pairing_code(code)
            run(["systemctl", "restart", f"wifibroadcast@{ROLE}"])
            show_pairing_code_screen(stdscr, code)

        elif key in (ord("w"), ord("W")):
            raw = prompt_line(stdscr, 10, "Kod z drugiego urzadzenia", "")
            norm = normalize_pairing_code(raw)
            if norm is None:
                safe_addstr(stdscr, 12, 2, "Niepoprawny kod: 8 znakow, bez I, O, 0 i 1.",
                            color_for("fail") | curses.A_BOLD)
            else:
                apply_pairing_code(norm)
                run(["systemctl", "restart", f"wifibroadcast@{ROLE}"])
                safe_addstr(stdscr, 12, 2, f"Sparowano kodem {format_pairing_code(norm)}. "
                                           f"Odcisk: {key_fingerprint(DRONE_KEY)}",
                            color_for("ok") | curses.A_BOLD)
                safe_addstr(stdscr, 13, 2, "Odcisk musi zgadzac sie z tym na drugim urzadzeniu.")
            pause(stdscr)

        elif key in (ord("b"), ord("B")):
            write_builtin_keys()
            PAIRING_CODE_PATH.unlink(missing_ok=True)
            run(["systemctl", "restart", f"wifibroadcast@{ROLE}"])
            safe_addstr(stdscr, 10, 2, "Przywrocono klucze wbudowane. Zrob to samo na drugiej stronie.",
                        color_for("ok") | curses.A_BOLD)
            pause(stdscr)

        else:
            return


def redetect_screen(stdscr):
    """Ta sama naprawa co przy starcie skryptu, ale z poziomu TUI: po wpieciu
    brakujacego dongla nie trzeba wychodzic i uruchamiac wszystkiego od nowa."""
    stdscr.clear()
    draw_header(stdscr, f"WFB-NG [{ROLE}] - ponowne wykrywanie kart")
    row = 2

    def say(text, status=None):
        nonlocal row
        attr = (color_for(status) | curses.A_BOLD) if status else 0
        safe_addstr(stdscr, row, 2, text, attr)
        row += 1
        stdscr.refresh()

    dongles = usb_rtl_dongles()
    say(f"lsusb: {len(dongles)} dongli RTL88xx (minimum {EXPECTED_NICS})")

    def quietly(fn):
        """Funkcje z czesci instalacyjnej pisza przez log() na stdout, co
        rozjechaloby ekran curses - przechwytujemy i wypisujemy po swojemu."""
        buf, old_stdout = io.StringIO(), sys.stdout
        sys.stdout = buf
        try:
            result = fn()
        finally:
            sys.stdout = old_stdout
        for line in buf.getvalue().splitlines():
            if line.strip():
                say("  " + line.strip())
        return result

    nics = wfb_nics()
    if len(nics) < EXPECTED_NICS:
        say(f"wfb-nics: {len(nics)}/{EXPECTED_NICS} - przepinam pod {TARGET_USB_DRIVER}...")
        quietly(rebind_to_wfb_driver)
        run(["udevadm", "trigger", "--action=add", "--subsystem-match=usb"])
        run(["udevadm", "settle"], timeout=15)
        time.sleep(2)
        nics = wfb_nics()

    nics = quietly(ensure_nic_names)

    for nic in nics:
        d = nic_details(nic)
        say(f"  {nic}{nic_role_txt(nic)}: {d['driver']} mac={d['mac']} "
            f"tryb={d['mode']} kanal={d['channel']}")
        say(f"     gniazdo USB {nic_usb_txt(nic)}")

    for entry in missing_cards(nics):
        say(f"  BRAKUJE: {card_txt(entry)}", "fail")

    if nics:
        release_nics_from_network_stack(nics)
        if ensure_video_service_type(nics):
            say(f"config: wideo -> udp_proxy (domyslny tryb nie umie {len(nics)} kart)", "warn")
            run(["systemctl", "restart", f"wifibroadcast@{ROLE}"])
            time.sleep(3)
        apply_tx_split(nics, say)
        unused = set(nics) - service_nics(set(nics))
        if unused:
            say(f"usluga nie uzywa: {' '.join(sorted(unused))} - restartuje...", "warn")
            run(["systemctl", "restart", f"wifibroadcast@{ROLE}"])
            time.sleep(3)
            unused = set(nics) - service_nics(set(nics))
        if unused and not service_active():
            say(f"USLUGA NIE DZIALA (status: {service_state_txt()}) - karty sa tu bez winy", "fail")
            for ln in service_last_errors(4):
                say("  " + ln[:100])
            say(f"wiecej: journalctl -u wifibroadcast@{ROLE} -n 50")
        elif unused:
            say(f"nadal poza usluga: {' '.join(sorted(unused))}", "fail")
            say(f"zobacz: journalctl -u wifibroadcast@{ROLE} -n 50")

    row += 1
    _nic_status_cache["val"] = None  # wymus swiezy odczyt w menu
    status, txt = nic_status_summary()
    say(txt, status)
    if status == "fail" and len(nics) < EXPECTED_NICS:
        say("Sprawdz port USB, kabel i zasilanie - 8812AU mocno obciaza porty RPi.")

    pause(stdscr)


def nic_role_txt(nic):
    """Dopisek o roli karty - pusty, gdy rol nie rozdzielamy (gs ma jedna karte
    robiaca oba kierunki). Na dronie mowi, ktora karta nadaje: to do niej idzie
    wzmacniacz i to jej MAC trzeba znac, zeby nie pomylic dongli."""
    tag = role_tag(nic)
    return f"   {tag}" if tag else ""


def nic_snapshot():
    """{nazwa: (gniazdo USB, mac)} - lekko, bez wolania 'iw', bo ten ekran
    odpytuje karty dwa razy na sekunde."""
    # MAC przez nic_mac, a nie wlasnym odczytem pliku: to ten sam, malymi
    # literami zapisany adres, na ktorym wisza reguly udev i ewidencja kart -
    # dwa zapisy tego samego MAC-a myliłyby przy porownywaniu z regula.
    return {nic: (usb_port_path(nic) or "?", nic_mac(nic) or "?")
            for nic in wfb_nics()}


def nic_identify_screen(stdscr):
    """Zywy podglad kart: wypnij dongla, a ekran powie, ktora nazwa wlasnie
    zniknela. To najprostszy sposob dopasowania nazwy do konkretnej anteny,
    bo dongle 8812AU wygladaja identycznie i nie widac po nich, ktory
    siedzi w ktorym gniezdzie. Przy okazji licza sie liczniki rx/tx na zywo,
    wiec w tym samym miejscu widac, przez ktora karte leci nadawanie."""
    stdscr.timeout(500)  # getch wraca po 0.5 s, wiec petla sama sie odswieza

    known = nic_snapshot()
    remember_cards(list(known))
    prev_dongles = len(usb_rtl_dongles())
    counters = {nic: (*nic_counters(nic), time.monotonic()) for nic in known}
    events = []

    def note(text, status):
        events.insert(0, (time.strftime("%H:%M:%S"), text, status))
        del events[8:]

    try:
        while True:
            now = time.monotonic()
            current = nic_snapshot()
            dongles = len(usb_rtl_dongles())

            for nic in [n for n in known if n not in current]:
                slot, mac = known[nic]
                # Nazwa, rola i gniazdo od razu w komunikacie: po wypieciu nie da
                # sie ich juz nigdzie odczytac, bo interfejsu po prostu nie ma.
                note(f"WYPIETO: {nic}{nic_role_txt(nic)}"
                     f"   (gniazdo USB {slot}, mac {mac})", "fail")
                if dongles < prev_dongles:
                    note("   ... dongiel zniknal tez z lsusb - to fizyczne wypiecie", "warn")
                else:
                    note("   ... ale lsusb dalej go widzi - to nie kabel, tylko sterownik", "warn")

            for nic in [n for n in current if n not in known]:
                slot, mac = current[nic]
                note(f"WPIETO: {nic}{nic_role_txt(nic)}"
                     f"   (gniazdo USB {slot}, mac {mac})", "ok")
                note("   ... usluga uzyje jej dopiero po 'Wykryj karty ponownie'", "warn")

            if set(current) != set(known):
                remember_cards(list(current))  # ewidencja ma pamietac takze po wyjsciu z ekranu
            known, prev_dongles = current, dongles

            stdscr.clear()
            draw_header(stdscr, f"WFB-NG [{ROLE}] - identyfikacja kart")
            safe_addstr(stdscr, 2, 2,
                        "Wypnij jeden dongiel - ekran powie, ktora nazwa i rola zniknela.",
                        curses.A_BOLD)

            row = 4
            safe_addstr(stdscr, row, 2,
                        f"Karty: {count_txt(len(current))}    dongle w lsusb: {count_txt(dongles)}",
                        color_for("ok" if len(current) >= EXPECTED_NICS else "fail") | curses.A_BOLD)
            row += 2

            used = service_nics(set(current)) if current else set()
            for nic in sorted(current):
                slot, mac = current[nic]
                rx, tx = nic_counters(nic)
                prev = counters.get(nic)
                if prev and now > prev[2]:
                    dt = now - prev[2]
                    rx_pps = max(0.0, (rx - prev[0]) / dt)
                    tx_pps = max(0.0, (tx - prev[1]) / dt)
                else:
                    rx_pps = tx_pps = 0.0  # karta dopiero co wpieta, brak odniesienia
                counters[nic] = (rx, tx, now)

                # MAC na przodzie, bo to on jest teraz tozsamoscia karty (na nim
                # wisi nazwa) - a przy dwoch identycznych donglach to jedyna
                # rzecz, po ktorej odroznisz je w rece od tej w drugim porcie
                safe_addstr(stdscr, row, 2,
                            f"{nic:<12} mac={mac}{nic_role_txt(nic)}",
                            color_for("ok" if nic in used else "warn") | curses.A_BOLD)
                safe_addstr(stdscr, row + 1, 4, f"gniazdo USB {usb_port_txt(slot)}")
                safe_addstr(stdscr, row + 2, 4,
                            f"rx={rx_pps:6.0f}/s  tx={tx_pps:6.0f}/s   w usludze="
                            f"{'tak' if nic in used else 'NIE'}"
                            + ("   <- ta karta nadaje" if tx_pps > 0 else ""),
                            color_for("ok") if tx_pps > 0 else 0)
                row += 4

            gone = missing_cards(list(current))
            if gone:
                safe_addstr(stdscr, row, 2, "Brakuje (znane z ewidencji):",
                            color_for("fail") | curses.A_BOLD)
                row += 1
                for entry in gone:
                    safe_addstr(stdscr, row, 4, card_txt(entry), color_for("fail"))
                    row += 1
                row += 1

            if events:
                safe_addstr(stdscr, row, 2, "Zdarzenia:", curses.A_BOLD)
                row += 1
                for stamp, text, status in events:
                    safe_addstr(stdscr, row, 4, f"{stamp}  {text}", color_for(status))
                    row += 1

            h, _ = stdscr.getmaxyx()
            safe_addstr(stdscr, h - 1, 2,
                        "q = powrot" + ("   |   z = zapomnij brakujace karty (i ich role)" if gone else ""),
                        curses.A_DIM)
            stdscr.refresh()

            key = stdscr.getch()
            if key in (ord("q"), ord("Q"), 27):
                break
            if key in (ord("z"), ord("Z")) and gone:
                # Karta wymieniona na inna zostalaby w ewidencji na zawsze jako
                # "brakujaca" - to jest sposob, zeby powiedziec: juz jej nie ma
                # i nie ma po co jej szukac.
                for entry in gone:
                    forget_card(entry["key"])
                note("ewidencja wyczyszczona z brakujacych kart", "warn")
    finally:
        stdscr.timeout(-1)  # z powrotem na blokujace getch, inaczej menu zwariuje


def run_quietly(fn, say):
    """Funkcje instalatora pisza przez log() na stdout, co rozjechaloby ekran
    curses - przechwytujemy to i oddajemy przez say(). Zwraca wynik fn()."""
    buf, old_stdout = io.StringIO(), sys.stdout
    sys.stdout = buf
    try:
        return fn()
    finally:
        sys.stdout = old_stdout
        for line in buf.getvalue().splitlines():
            if line.strip():
                say("  " + line.strip())


def card_txpower_driver_screen(stdscr):
    """Sterownik z moca per karta: przebudowa (gdy latki nie ma) i przeladowanie
    modulu. Pytamy przed, bo przebudowa trwa kilka minut i potrzebuje internetu,
    a przeladowanie zrywa link na kilkanascie sekund - to trzeba wiedziec
    zawczasu, a nie w polowie lotu."""
    state = driver_card_txpower(max_age=0)
    if state == "on":
        popup(stdscr, "Moc per karta", ["Sterownik juz ma moc per karta - nic do zrobienia."], status="ok")
        return
    if state == "reload":
        lines = ["Sterownik z moca per karta jest zbudowany, ale w pamieci",
                 "siedzi jeszcze stary modul. Przeladuje go teraz -",
                 f"usluga wifibroadcast@{ROLE} stanie na kilkanascie sekund."]
    else:
        lines = ["Ten sterownik ma jedna moc dla wszystkich kart: 'iw set",
                 "txpower' na jednej karcie zmienia moc kazdej.",
                 "",
                 "Przebuduje go z latka mocy per karta:",
                 f"  - pobiore zrodla svpcom/rtl8812au {DRIVER_TAG} z GitHuba,",
                 "  - kompilacja trwa kilka minut, link w tym czasie dziala,",
                 "  - gdy sie nie skompiluje, zostaje stary sterownik,",
                 "  - na koniec przeladowanie modulu: usluga stoi kilkanascie sekund.",
                 "",
                 "Zrob to na OBU Pi - moc ustawia sie po kazdej stronie osobno."]
    if popup(stdscr, "Moc per karta", lines, ("Tak", "Nie"), status="warn", default=1) != 0:
        return

    title = f"WFB-NG [{ROLE}] - sterownik z moca per karta"
    stdscr.clear()
    draw_header(stdscr, title)
    row = 2

    def say(text, status=None):
        nonlocal row
        if row >= stdscr.getmaxyx()[0] - 2:  # przebudowa potrafi wypisac wiecej niz ekran
            stdscr.clear()
            draw_header(stdscr, title)
            row = 2
        safe_addstr(stdscr, row, 2, text, (color_for(status) | curses.A_BOLD) if status else 0)
        row += 1
        stdscr.refresh()

    if state == "":
        ok, msg = run_quietly(lambda: rebuild_driver_card_txpower(say), say)
        say(msg, "ok" if ok else "fail")
        if not ok:
            pause(stdscr)
            return
    ok, msg = run_quietly(lambda: reload_wfb_driver(say), say)
    say(msg, "ok" if ok else "fail")
    if ok:
        say("Moc kazdej karty ustawisz teraz na ekranie kart: -/+ moc, [ ] limit.")
    pause(stdscr)


# Kolejnosc jak na przelaczniku: od "tylko slucha" do "robi oba".
ROLE_SWITCH = (("rx", "RX"), ("tx", "TX"), ("txrx", "RXTX"))
POWER_STEP = 2  # o tyle indeksu zmienia jedno -/+ (moc) albo [ ] (limit) na ekranie kart


def cards_live_screen(stdscr):
    """Karty na zywo: kazdy dongiel Wi-Fi wpiety w USB, jego chip, nazwa
    urzadzenia (albo "generic") i przelacznik roli RX / TX / RXTX.

    Liste bierzemy z sysfs (usb_wifi_dongles), a nie z wfb-nics, wiec wpieta
    karta pojawia sie od razu - takze zanim dostanie nazwe i takze pod cudzym
    sterownikiem. Swieza karta nie ma jeszcze roli (wlanX), wiec nie nadaje,
    dopoki nie wybierzesz jej na przelaczniku. Urzadzenie jest na widoku, bo
    rozne dongle maja rozna moc - a od tego zalezy, ktora karta ma nadawac.

    Rola siedzi w nazwie karty przypietej udevem do MAC-a (assign_nic_role),
    wiec zostaje przy karcie takze po przelozeniu do innego gniazda. Wolne
    rzeczy (wfb-nics, usluga, ewidencja) liczymy tylko po zmianie w sysfs albo
    co kilka sekund; reszta to odczyty plikow, tanie przy odswiezaniu co 0.5 s."""
    stdscr.timeout(500)
    slow = {"t": 0.0, "sig": None, "wfb": [], "used": set(), "gone": [],
            "muted": set(), "card_state": "", "powers": {}, "limits": {}, "plan": {},
            "live_power": {}}
    known, counters, events = None, {}, []
    sel, cursor, flash, scroll = None, None, None, 0

    def note(text, status):
        events.insert(0, (time.strftime("%H:%M:%S"), text, status))
        del events[4:]

    def card_nic(card):
        # interfejs pod wfb, jesli karta ma ich kilka; inaczej jakikolwiek
        return next((n for n in card["nics"] if n in slow["wfb"]),
                    card["nics"][0] if card["nics"] else "")

    def set_role(nic, role):
        label = dict(ROLE_SWITCH)[role]
        if not nic or nic not in slow["wfb"]:
            return f"{nic or 'ta karta'} nie jest pod sterownikiem wfb - najpierw w (przepiecie)", "warn"
        if role_of_name(nic) == role:
            return f"{nic} juz ma role {label}", "ok"
        width = stdscr.getmaxyx()[1]

        def say(text, status=None):
            # assign_nic_role trzyma ekran kilka sekund - przebieg idzie w linijke komunikatu
            safe_addstr(stdscr, 4, 2, text.ljust(width),
                        (color_for(status) if status else 0) | curses.A_BOLD)
            stdscr.refresh()

        say(f"{nic} -> {label}: zatrzymuje usluge, zmieniam nazwe karty...", "warn")
        ok, msg = assign_nic_role(nic, role, say)
        note(("ROLA: " if ok else "BLAD ROLI: ") + msg, "ok" if ok else "fail")
        slow["sig"] = None  # nazwa i stan uslugi sie zmienily
        return msg, "ok" if ok else "fail"

    try:
        while True:
            now = time.monotonic()
            cards = usb_wifi_dongles()
            sig = tuple((p, c["driver"], tuple(c["nics"])) for p, c in cards.items())
            if sig != slow["sig"] or now - slow["t"] > 3.0:
                wfb = wfb_nics()
                remember_cards(wfb)  # zeby po wypieciu bylo czym nazwac brakujaca karte
                card_state = driver_card_txpower()
                plan = card_power_plan(wfb, live=True) if wfb and card_state == "on" else {}
                slow.update(t=now, sig=sig, wfb=wfb, gone=missing_cards(wfb),
                            used=service_nics(set(wfb)) if wfb else set(),
                            muted=muted_nics(wfb), card_state=card_state,
                            powers=card_powers(wfb) if wfb else {},
                            limits=card_limits(wfb) if wfb else {}, plan=plan,
                            live_power={n: card_power_live(n) for n in plan})

            if known is not None:
                for port in [p for p in cards if p not in known]:
                    note(f"WPIETO: gniazdo {port}   {usb_chip_txt(cards[port])[0]}"
                         f"   {usb_device_txt(cards[port])}", "ok")
                    sel, cursor = port, None  # nowa karta od razu pod kursorem
                for port in [p for p in known if p not in cards]:
                    note(f"WYPIETO: gniazdo {port}   "
                         f"{' '.join(known[port]['nics']) or 'bez interfejsu'}", "fail")
            known = cards

            ports = list(cards)
            if sel not in cards:
                sel, cursor = (ports[0] if ports else None), None
            idx = ports.index(sel) if ports else 0

            stdscr.erase()
            h, w = stdscr.getmaxyx()
            draw_header(stdscr, f"WFB-NG [{ROLE}] - karty na zywo: chip, urzadzenie, rola")
            live_tx = read_tx_power_live()
            safe_addstr(stdscr, 2, 2,
                        f"Karty Wi-Fi na USB: {len(cards)}"
                        + (f" (karta {idx + 1}/{len(ports)})" if ports else "")
                        + f"   pod wfb: {len(slow['wfb'])}   w usludze: {len(slow['used'])}"
                        f"   moc wspolna: {live_tx or '?'}/{TX_POWER_CAP}   moc per karta: "
                        + {"on": "tak", "reload": "po przeladowaniu sterownika (P)"}.get(
                            slow["card_state"], "nie - P = przebuduj sterownik"),
                        curses.A_BOLD)
            senders = [n for n in slow["wfb"] if role_of_name(n) in ("tx", "txrx")]
            if senders:
                safe_addstr(stdscr, 3, 2, f"Nadaja: {' '.join(senders)}", color_for("ok"))
            elif slow["wfb"]:
                # to samo, co robi txpower_cfg_value: bez karty nadawczej 'off' nie powstaje
                safe_addstr(stdscr, 3, 2,
                            "Zadna karta nie ma roli nadawczej - bezpiecznik: nadaja wszystkie.",
                            color_for("warn") | curses.A_BOLD)
            if flash and now < flash[2]:
                safe_addstr(stdscr, 4, 2, flash[0], color_for(flash[1]) | curses.A_BOLD)
            else:
                flash = None

            gone = slow["gone"]
            foot = 2 + (len(events) + 1 if events else 0) + (min(len(gone), 3) + 1 if gone else 0)
            top, block = 6, 5
            per_page = max(1, (h - top - foot) // block)
            scroll = min(max(scroll, idx - per_page + 1), idx)
            scroll = max(0, min(scroll, len(ports) - per_page))

            row = top
            if not ports:
                safe_addstr(stdscr, row, 2,
                            "Nie widac zadnej karty Wi-Fi na USB - wepnij dongla, pojawi sie tutaj od razu.",
                            color_for("warn") | curses.A_BOLD)
            for port in ports[scroll:scroll + per_page]:
                card = cards[port]
                nic = card_nic(card)
                in_wfb = nic in slow["wfb"]
                role = role_of_name(nic)
                chosen = port == sel

                rx_pps = tx_pps = 0.0
                if nic:
                    rx, tx = nic_counters(nic)
                    prev = counters.get(nic)
                    if prev and now > prev[2]:
                        rx_pps = max(0.0, (rx - prev[0]) / (now - prev[2]))
                        tx_pps = max(0.0, (tx - prev[1]) / (now - prev[2]))
                    counters[nic] = (rx, tx, now)

                # wiersz 1: nazwa karty i przelacznik roli
                safe_addstr(stdscr, row, 2, f"{'>' if chosen else ' '} {nic or '(bez interfejsu)':<16}",
                            (curses.color_pair(5) if chosen else 0) | curses.A_BOLD)
                x = 22
                for i, (r, label) in enumerate(ROLE_SWITCH):
                    cell = f"[{label}]" if r == role else f" {label} "
                    if chosen and cursor == i:
                        attr = curses.color_pair(5) | curses.A_BOLD
                    elif r == role and in_wfb:
                        attr = color_for("ok") | curses.A_BOLD
                    else:
                        attr = curses.A_DIM
                    safe_addstr(stdscr, row, x, cell, attr)
                    x += len(cell) + 1
                if not in_wfb:
                    state_txt, state = "rola niedostepna - karta nie jest pod wfb", "warn"
                elif chosen and cursor is not None and ROLE_SWITCH[cursor][0] != role:
                    state_txt, state = f"<- Enter = ustaw {ROLE_SWITCH[cursor][1]}", "warn"
                elif not role:
                    state_txt, state = "bez roli - nie nadaje, wybierz RX / TX / RXTX", "warn"
                elif nic in slow["used"]:
                    state_txt, state = "w usludze", "ok"
                else:
                    state_txt, state = "usluga jej jeszcze nie uzywa (w = restart)", "warn"
                safe_addstr(stdscr, row, x + 2, state_txt, color_for(state))

                # wiersz 2: chip i urzadzenie - od nich zalezy moc karty
                chip, source = usb_chip_txt(card)
                device = usb_device_txt(card)
                safe_addstr(stdscr, row + 1, 4,
                            f"chip: {chip}" + (f" ({source})" if source else "")
                            + f"   urzadzenie: {device}",
                            curses.A_BOLD if device != "generic" else 0)

                # wiersz 3: moc - rozne dongle przy tym samym indeksie daja rozna moc
                own = slow["powers"].get(nic)
                limit = slow["limits"].get(nic)
                planned = slow["plan"].get(nic)
                shared = int(live_tx) if live_tx and live_tx.isdigit() else None
                limit_txt = f"   limit {limit}" if limit else ""
                if not in_wfb:
                    power_txt, power_attr = "moc: -", curses.A_DIM
                elif nic in slow["muted"]:
                    power_txt, power_attr = "moc: nie nadaje (rola RX)" + limit_txt, curses.A_DIM
                elif own:
                    value = min(own, limit or TX_POWER_CAP)
                    power_txt = f"moc: {power_meter(value, limit)} {value}/{TX_POWER_CAP} wlasna" + limit_txt
                    power_attr = color_for("ok")
                elif planned:  # wspolna ponad limit - karta dostaje swoj limit osobno
                    power_txt = (f"moc: {power_meter(planned, limit)} {planned}/{TX_POWER_CAP}"
                                 f" wspolna {live_tx}, scieta limitem")
                    power_attr = color_for("ok")
                else:
                    power_txt = (f"moc: {power_meter(shared, limit)} {live_tx or '?'}/{TX_POWER_CAP} wspolna"
                                 + limit_txt)
                    power_attr = 0
                    if limit and shared and shared > limit:
                        power_txt += " - limit NIE dziala bez sterownika z latka (P)"
                        power_attr = color_for("warn")
                live = slow["live_power"].get(nic)
                if planned and nic not in slow["muted"] and live is not None and live != planned:
                    power_txt += f"   <- sterownik ma {live}! (-/+ ustawi ponownie)"
                    power_attr = color_for("warn") | curses.A_BOLD
                if chosen and in_wfb:
                    if slow["card_state"] == "on":
                        power_txt += "   -/+ = moc, [ ] = limit, 0 = wspolna"
                    else:
                        power_txt += ("   osobna moc i limit: P = "
                                      + ("przeladuj" if slow["card_state"] == "reload" else "przebuduj")
                                      + " sterownik")
                safe_addstr(stdscr, row + 2, 4, power_txt, power_attr)

                # wiersz 4: gdzie siedzi i co przez nia leci
                speed = usb_speed_txt(card["speed"])
                safe_addstr(stdscr, row + 3, 4,
                            f"gniazdo {port}" + (f" ({speed})" if speed else "")
                            + f"   USB {card['vid']}:{card['pid']}   sterownik {card['driver'] or 'BRAK'}"
                            + (f"   mac={nic_mac(nic) or '?'}   rx={rx_pps:.0f}/s tx={tx_pps:.0f}/s"
                               if nic else ""),
                            0 if chosen else curses.A_DIM)

                # wiersz 5: czemu karta nie moze pracowac w wfb
                if card["driver"] != TARGET_USB_DRIVER:
                    if chip != "nieznany" and not re.match(r"RTL88(11|12|14|21)AU", chip):
                        why = f"{chip} to nie rodzina 8812AU - {TARGET_USB_DRIVER} jej nie obsluzy"
                    else:
                        why = ((f"sterownik {card['driver']}" if card["driver"] else "karta bez sterownika")
                               + f" zamiast {TARGET_USB_DRIVER} - w = przepnij (Wykryj karty ponownie)")
                    safe_addstr(stdscr, row + 4, 4, why, color_for("fail"))
                row += block

            y = h - foot
            if gone:
                safe_addstr(stdscr, y, 2, "Brakuje (znane z ewidencji):", color_for("fail") | curses.A_BOLD)
                for i, entry in enumerate(gone[:3]):
                    safe_addstr(stdscr, y + 1 + i, 4, card_txt(entry), color_for("fail"))
                y += min(len(gone), 3) + 1
            if events:
                safe_addstr(stdscr, y, 2, "Zdarzenia:", curses.A_BOLD)
                for i, (stamp, text, status) in enumerate(events):
                    safe_addstr(stdscr, y + 1 + i, 4, f"{stamp}  {text}", color_for(status))
            safe_addstr(stdscr, h - 2, 2,
                        "gora/dol = karta   lewo/prawo + Enter albo 1/2/3 = RX/TX/RXTX"
                        "   -/+ = moc karty   [ ] = limit karty   0 = moc wspolna", curses.A_DIM)
            safe_addstr(stdscr, h - 1, 2,
                        "P = sterownik z moca per karta   w = przepnij pod sterownik wfb   "
                        + ("z = zapomnij brakujace   " if gone else "")
                        + "q = powrot   (TX i RXTX dzialaja w wfb-ng tak samo)", curses.A_DIM)
            stdscr.refresh()

            key = stdscr.getch()
            if key in (ord("q"), ord("Q"), 27):
                break
            card = cards.get(sel)
            nic = card_nic(card) if card else ""
            current = next((i for i, (r, _) in enumerate(ROLE_SWITCH) if r == role_of_name(nic)), None)
            if key in (curses.KEY_UP, ord("k")) and ports:
                sel, cursor = ports[(idx - 1) % len(ports)], None
            elif key in (curses.KEY_DOWN, ord("j")) and ports:
                sel, cursor = ports[(idx + 1) % len(ports)], None
            elif key in (curses.KEY_LEFT, ord("h")) and card:
                start = cursor if cursor is not None else current
                cursor = 0 if start is None else max(0, start - 1)
            elif key in (curses.KEY_RIGHT, ord("l")) and card:
                start = cursor if cursor is not None else current
                cursor = 0 if start is None else min(len(ROLE_SWITCH) - 1, start + 1)
            elif key in (10, 13, curses.KEY_ENTER) and card:
                if cursor is None:
                    flash = ("wybierz role strzalkami lewo/prawo albo klawiszem 1/2/3", "warn", now + 4)
                else:
                    msg, st = set_role(nic, ROLE_SWITCH[cursor][0])
                    flash, cursor = (msg, st, time.monotonic() + 8), None
            elif key in (ord("1"), ord("2"), ord("3")) and card:
                msg, st = set_role(nic, ROLE_SWITCH[key - ord("1")][0])
                flash, cursor = (msg, st, time.monotonic() + 8), None
            elif key in (ord("+"), ord("="), ord("-"), ord("_"), ord("0"), ord("["), ord("]")) and card:
                if slow["card_state"] != "on":
                    flash = ("osobna moc i limit karty wymagaja sterownika z latka - P = "
                             + ("przeladuj" if slow["card_state"] == "reload" else "przebuduj") + " sterownik",
                             "warn", now + 6)
                else:
                    limit = slow["limits"].get(nic, TX_POWER_CAP)
                    step = POWER_STEP if key in (ord("+"), ord("="), ord("]")) else -POWER_STEP
                    if key in (ord("["), ord("]")):
                        ok, msg = set_card_limit(nic, max(1, min(TX_POWER_CAP, limit + step)))
                    elif key == ord("0"):
                        ok, msg = set_card_power(nic, 0)
                    else:
                        shared = int(live_tx) if live_tx and live_tx.isdigit() else TX_POWER_CAP
                        current = min(slow["powers"].get(nic) or shared, limit)
                        ok, msg = set_card_power(nic, max(1, min(limit, current + step)))
                    flash = (msg, "ok" if ok else "fail", time.monotonic() + 6)
                    if not ok:
                        note("BLAD MOCY: " + msg, "fail")
                    slow["sig"] = None
            elif key in (ord("P"), ord("p")):
                stdscr.timeout(-1)
                card_txpower_driver_screen(stdscr)
                stdscr.timeout(500)
                slow["sig"] = None
            elif key in (ord("w"), ord("W")):
                stdscr.timeout(-1)
                redetect_screen(stdscr)
                stdscr.timeout(500)
                slow["sig"] = None
            elif key in (ord("z"), ord("Z")) and slow["gone"]:
                for entry in slow["gone"]:
                    forget_card(entry["key"])
                note("zapomniano brakujace karty (i ich role)", "warn")
                slow["sig"] = None
    finally:
        stdscr.timeout(-1)


def popup(stdscr, title, lines, buttons=("OK",), status=None, default=0):
    """Okienko na srodku ekranu z przyciskami na dole. Wybor strzalkami
    lewo/prawo, Enter zatwierdza, pierwsza litera przycisku dziala jak skrot,
    Esc zawsze wybiera ostatni przycisk (czyli "Nie"). Przy jednym przycisku
    okienko tylko informuje i zamyka sie dowolnym klawiszem. Zwraca indeks
    wybranego przycisku.

    Rysowane wprost po stdscr, jak reszta tego TUI - podokien nie uzywamy
    nigdzie indziej, a to co pod spodem i tak zaraz zostanie przerysowane."""
    labels = [f"[ {b} ]" for b in buttons]
    bar = "   ".join(labels)
    body = list(lines) + ["", " " * len(bar)]  # ostatni wiersz zajmuja przyciski
    h, w = stdscr.getmaxyx()
    inner = min(max(len(s) for s in [title] + body) + 2, max(8, w - 4))
    left = max(0, (w - inner - 2) // 2)
    top = max(0, (h - (len(body) + 4)) // 2)
    bar_y = top + 2 + len(body)
    bar_x = left + 1 + max(0, (inner - len(bar)) // 2)
    sel = default

    def frame(y):
        safe_addstr(stdscr, y, left, "+" + "-" * inner + "+", curses.A_BOLD)

    def line(y, text, attr=0):
        safe_addstr(stdscr, y, left, "|" + text[:inner].ljust(inner) + "|", attr)

    while True:
        frame(top)
        line(top + 1, " " + title, (color_for(status) if status else 0) | curses.A_BOLD)
        line(top + 2, "")
        for i, text in enumerate(body):
            line(top + 3 + i, " " + text)
        frame(top + 3 + len(body))

        x = bar_x
        for i, label in enumerate(labels):
            safe_addstr(stdscr, bar_y, x, label,
                        curses.color_pair(5) | curses.A_BOLD if i == sel else curses.A_BOLD)
            x += len(label) + 3
        stdscr.refresh()

        key = stdscr.getch()
        if key == curses.KEY_LEFT:
            sel = (sel - 1) % len(labels)
        elif key in (curses.KEY_RIGHT, 9):  # 9 = Tab
            sel = (sel + 1) % len(labels)
        elif key in (10, 13, curses.KEY_ENTER, ord(" ")):
            return sel
        elif key == 27:
            return len(labels) - 1
        else:
            for i, name in enumerate(buttons):
                if name and key in (ord(name[0].lower()), ord(name[0].upper())):
                    return i
            if len(labels) == 1:
                return 0


class Stat:
    """Min / srednia / max liczone na biezaco, bez trzymania probek.

    Zapis w tle potrafi chodzic godzinami, a do podsumowania i tak potrzebne
    sa tylko trzy liczby - lista wszystkich odczytow rosla by w nieskonczonosc
    w procesie, ktorego nikt nie oglada."""

    def __init__(self):
        self.n = 0
        self.total = 0.0
        self.lo = None
        self.hi = None

    def add(self, value):
        if value is None:
            return
        self.n += 1
        self.total += value
        self.lo = value if self.lo is None else min(self.lo, value)
        self.hi = value if self.hi is None else max(self.hi, value)

    def line(self, fmt="{:.1f}"):
        if not self.n:
            return "brak danych"
        return (f"min {fmt.format(self.lo)}   srednio {fmt.format(self.total / self.n)}"
                f"   max {fmt.format(self.hi)}")


class RunTotals:
    """Ile pakietow przeszlo i ile przepadlo OD POCZATKU TESTU - razem ze
    wspolczynnikiem bledu pakietow (PER).

    wfb-ng podaje sumy od startu uslugi, a nie od chwili, w ktorej zaczelismy
    patrzec. Typowy przypadek: test wlacza sie pierwszy, a nadawanie (np. wideo
    z innego programu) rusza chwile pozniej - liczniki uslugi maja wtedy juz
    jakas historie, ktora nie ma nic wspolnego z tym, co wlasnie mierzymy.
    Dlatego zapamietujemy stan z pierwszej probki i liczymy przyrost.

    Gdy usluga sie zrestartuje, jej liczniki lecą od zera i roznica wyszla by
    ujemna - wtedy zapamietujemy dotychczasowy dorobek i liczymy od nowego zera,
    zeby PER z calego przelotu sie nie zgubil."""

    FIELDS = {"rx": "rx_total", "lost": "lost_total",
              "fec": "fec_total", "bad": "bad_total"}

    def __init__(self):
        self.reset()

    def reset(self):
        self._base = None
        self._carry = {k: 0.0 for k in self.FIELDS}
        self.totals = {k: 0.0 for k in self.FIELDS}
        self.restarts = 0

    def update(self, metrics):
        now = {k: metrics.get(src) or 0.0 for k, src in self.FIELDS.items()}
        if self._base is None:
            self._base = now
        elif any(now[k] < self._base[k] for k in now):
            self._carry = dict(self.totals)
            self._base = {k: 0.0 for k in now}
            self.restarts += 1
        self.totals = {k: self._carry[k] + now[k] - self._base[k] for k in now}
        return self.totals

    @property
    def per(self):
        """Procent pakietow, ktore przepadly bezpowrotnie (po naprawie FEC).
        None, dopoki nic nie przyszlo - zero bylo by tu klamstwem."""
        seen = self.totals["rx"] + self.totals["lost"]
        return (100.0 * self.totals["lost"] / seen) if seen else None

    @property
    def per_before(self):
        """PER, jaki bylby BEZ naprawy FEC - czyli ile gubi samo radio. Ta sama
        podstawa co w 'per', wiec obie liczby stoja obok siebie uczciwie:
        roznica miedzy nimi to zasluga naprawy."""
        seen = self.totals["rx"] + self.totals["lost"]
        if not seen:
            return None
        return 100.0 * (self.totals["lost"] + self.totals["fec"]) / seen

    @property
    def saved_pct(self):
        """O ile punktow procentowych naprawa zbila straty na calym przebiegu."""
        before, after = self.per_before, self.per
        return None if before is None else before - after

    @property
    def fec_pct(self):
        """Ile procent ramek trzeba bylo odtworzyc z nadmiarowych - czyli ile
        gubilo sie w powietrzu, zanim FEC to naprawil."""
        rx = self.totals["rx"]
        return (100.0 * self.totals["fec"] / rx) if rx else None


# Cztery probki na sekunde. Statystyki z API wfb-ng przychodzia raz na sekunde,
# wiec kolumny sygnalu potrafia sie powtorzyc kilka razy pod rzad - ale
# liczniki kart i ping maja wlasne tempo, a przy szybkiej zmianie (obrot
# anteny, przelot za przeszkoda) 4 Hz lapie to, co 1 Hz gubi.
LOG_SAMPLE_HZ = 4
LOG_SAMPLE_PERIOD = 1.0 / LOG_SAMPLE_HZ

# Zapis nie chodzi w tym procesie, tylko w osobnym, odpietym od terminala -
# dzieki temu trwa dalej po wyjsciu z ekranu testu, a nawet po zamknieciu
# calego programu (typowy przypadek: test zasiegu, przy ktorym Pi zostaje
# wlaczone, a ekran sie zamyka). TUI dogaduje sie z nim przez dwa male pliki
# obok logu: stan (ile probek, jak duzy plik) i kolejke uwag do dopisania.
# Nazwy z kropka, zeby nie mieszaly sie z logami przy zwyklym 'ls'.
TEST_STATE = TEST_LOG_DIR / f".test-{ROLE}.stan"
TEST_NOTE = TEST_LOG_DIR / f".test-{ROLE}.uwagi"
RECORDER_FLAG = "--zapis-testu"

# Znacznik: jeden klawisz w trakcie testu zostawia w logu pionowa kreske "tu
# cos sie stalo" (obrot anteny, przelot za budynek, wlaczenie silnikow).
# Po locie nikt nie pamieta, o ktorej minucie to bylo, a na wykresie
# w podgladzie znaczniki widac jako czerwone linie i od razu wiadomo, ktore
# zalamanie sygnalu z czym zestawic. Numeruje je proces zapisu - TUI wysyla
# samo slowo, bo moze wystartowac i zniknac w srodku zapisu.
MARK_TEXT = "ZNACZNIK"

# Gorny limit rozmiaru logu. Przy 4 Hz to okolo miesiaca ciaglego zapisu, wiec
# nie chodzi o skracanie testu, tylko o to, zeby zapomniany zapis nie zapchal
# karty do zera - z pelna karta system przestaje dzialac, a nie tylko test.
TEST_MAX_BYTES = 1024 ** 3


def human_size(n):
    n = float(n or 0)
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.2f} GB"
    if n >= 1024 ** 2:
        return f"{n / 1024 ** 2:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.1f} kB"
    return f"{n:.0f} B"


def fmt_mmss(seconds):
    seconds = int(max(0, seconds or 0))
    if seconds >= 3600:
        return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def _pid_recording(pid):
    """Czy pod tym PID-em siedzi naprawde nasz proces zapisu. Samo sprawdzenie,
    ze proces zyje, nie wystarcza: numer moze juz nalezec do czegos innego."""
    if not pid:
        return False
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", "replace")
    except OSError:
        return False
    return RECORDER_FLAG in cmdline


def write_test_state(**fields):
    """Plik stanu podmieniany w calosci (os.replace), zeby TUI czytajace go
    kilka razy na sekunde nigdy nie trafilo na wersje zapisana w polowie."""
    tmp = TEST_STATE.with_name(TEST_STATE.name + ".tmp")
    try:
        tmp.write_text("".join(f"{k}={v}\n" for k, v in fields.items()), encoding="utf-8")
        os.replace(tmp, TEST_STATE)
    except OSError:
        pass  # zapisu testu nie warto przerywac przez plik pomocniczy


def test_state():
    """Stan zapisu w tle albo None, gdy zadnego nie ma.

    "trwa" jest prawda tylko wtedy, gdy proces faktycznie zyje - inaczej po
    zaniku zasilania albo zabiciu procesu w menu wisialby napis o trwajacym
    tescie, ktorego niczym nie da sie zamknac."""
    try:
        raw = TEST_STATE.read_text(encoding="utf-8")
    except OSError:
        return None

    st = {}
    for line in raw.splitlines():
        key, _, value = line.partition("=")
        st[key.strip()] = value.strip()
    if not st.get("plik"):
        return None

    for key in ("pid", "probek", "bajtow", "znacznikow"):
        try:
            st[key] = int(st.get(key) or 0)
        except ValueError:
            st[key] = 0
    try:
        st["czas"] = float(st.get("czas") or 0)
    except ValueError:
        st["czas"] = 0.0

    if st.get("stan") == "trwa" and not _pid_recording(st["pid"]):
        st["stan"] = "przerwany"
        st["powod"] = "proces zapisu zniknal (restart, brak zasilania?)"
    return st


def start_test_recorder(path):
    """Odpala zapis jako osobny proces w NOWEJ SESJI - inaczej zginalby razem
    z terminalem, w ktorym stoi TUI. Czeka chwile na pierwszy plik stanu, bo
    "uruchomilem i nie wiadomo, czy zyje" jest gorsze niz czytelny blad.
    Zwraca komunikat o bledzie albo None."""
    for helper in (TEST_STATE, TEST_NOTE):
        try:
            helper.unlink()
        except OSError:
            pass

    try:
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), RECORDER_FLAG, str(path)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True, cwd=str(TEST_LOG_DIR))
    except OSError as e:
        return str(e)

    for _ in range(30):  # ~3 s na otwarcie pliku i zgloszenie sie
        time.sleep(0.1)
        st = test_state()
        if st and st.get("stan") == "trwa":
            return None
        if st and st.get("stan") == "blad":
            try:
                TEST_STATE.unlink()  # nie ma czego pilnowac, nic nie ruszylo
            except OSError:
                pass
            return st.get("powod") or "nieznany blad"
    return "proces zapisu nie zglosil sie w ciagu 3 s"


def stop_test_recorder(timeout=5.0):
    """Grzeczne zatrzymanie sygnalem: proces sam dopisuje podsumowanie
    i zamyka plik. Zwraca stan po zatrzymaniu."""
    st = test_state()
    if not st or st.get("stan") != "trwa":
        return st
    try:
        os.kill(st["pid"], signal.SIGTERM)
    except OSError:
        return test_state()

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(0.1)
        st = test_state()
        if not st or st.get("stan") != "trwa":
            return st
    return test_state()


def note_test_recorder(text):
    """Uwaga do dopisania w logu. TUI nie ma tego pliku otwartego, wiec zostawia
    ja w kolejce - proces zapisu zabiera ja przy najblizszej probce."""
    try:
        with TEST_NOTE.open("a", encoding="utf-8") as fh:
            fh.write(text.replace("\n", " ") + "\n")
    except OSError:
        pass


def mark_test_recorder():
    """Znacznik "tu cos sie stalo" w miejscu, w ktorym stoi zapis. Numer nadaje
    proces zapisu, wiec z paru miejsc naraz (ekran testu, menu) nie da sie
    dostac dwoch znacznikow o tym samym numerze."""
    note_test_recorder(MARK_TEXT)


def take_test_notes():
    try:
        text = TEST_NOTE.read_text(encoding="utf-8")
        TEST_NOTE.unlink()
    except OSError:
        return []
    return [line.strip() for line in text.splitlines() if line.strip()]


class TestRecorder:
    """Zapis przebiegu testu do pliku: naglowek z cala konfiguracja, potem
    cztery wiersze na sekunde, na koniec podsumowanie. Uzywa go proces zapisu
    w tle (background_recorder), a nie ekran testu - plik zyje wlasnym zyciem
    i konczy sie dopiero na zadanie uzytkownika albo na limicie rozmiaru.

    Po co: przy sprawdzaniu zasiegu wyniku nie da sie ogladac na biezaco (jest
    sie kilkaset metrow od ekranu), a i tak trzeba go z czyms porownac - "przed"
    i "po" przestawieniu anteny albo zmianie kanalu. Wiersze sa rozdzielone
    srednikami, wiec plik otwiera sie tez w arkuszu."""

    # straty_przed_% / straty_% to ta sama chwila przed naprawa FEC i po niej,
    # liczone na tym samym mianowniku - roznica miedzy nimi to pakiety, ktore
    # naprawa uratowala. Tak samo per_przed_% wzgledem per_% dla calego testu.
    COLUMNS = ("czas", "sek", "rssi_best_dBm", "snr_best_dB", "rx_mcs", "rx_bw_MHz",
               "straty_przed_%", "straty_%", "uratowane_%", "per_przed_%", "per_%",
               "rx_pkt_s", "rx_Mbit_s", "fec_naprawil_s",
               "utracone_s", "ping_ms", "ping_utrata_%", "anteny_rssi")

    def __init__(self, path):
        self.path = path
        self.samples = 0
        self.size = 0
        self.marks = []      # (godzina, sekunda od startu) recznych znacznikow
        self._fh = None
        self._elapsed = 0.0  # czas ostatniej probki - do opisu znacznikow
        self._rssi = Stat()
        self._loss = Stat()
        self._loss_before = Stat()
        self._ping = Stat()
        # Sumy pingow od poczatku zapisu. Kolumna ping_utrata_% w wierszach to
        # tylko OSTATNIA proba (3 pakiety), wiec potrafi pokazac wylacznie
        # 0/33/67/100% - do oceny calego przebiegu nie nadaje sie zupelnie.
        self._ping_sent = 0
        self._ping_recv = 0
        self._mcs = {}
        # straty_% to chwila, per_% to caly test - przy szukaniu zasiegu liczy
        # sie to drugie, bo pojedyncza sekunda potrafi klamac w obie strony
        self._run = RunTotals()

    def open(self):
        """Moze rzucic OSError - wolajacy pokazuje to w okienku i test idzie
        dalej bez zapisu."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("w", encoding="utf-8")
        self._header()
        return self

    def _header(self):
        ch, reg = wfb_effective_common()
        freq = channel_freq(ch)
        mode, code = key_mode()
        w = self._fh.write
        w(f"# test polaczenia wfb-ng, rola: {ROLE}\n")
        w(f"# start: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        w(f"# host: {socket.gethostname()}   jadro: {os.uname().release}"
          f"   wfb-ng: {wfb_ng_version()}\n")
        w(f"# kanal: {ch}" + (f" ({freq} MHz)" if freq else "") + f"   region: {reg}"
          f"   moc TX: {read_tx_power_live() or '?'}/{TX_POWER_CAP}"
          f" (pulap 90% z {TX_POWER_MAX})\n")
        fingerprint = key_fingerprint(DRONE_KEY)
        w(f"# klucze: {mode}" + (f", kod {format_pairing_code(code)}" if code else "")
          + (f"   odcisk drone.key={fingerprint}" if fingerprint else "") + "\n")
        for nic in wfb_nics():
            d = nic_details(nic)
            w(f"# karta {nic}: mac={d['mac']} usb={d['usb']} tryb={d['mode']}"
              f" kanal={d['channel']}\n")
        for tx in tx_radio_params():
            main, extra = tx_modulation_txt(tx)
            w(f"# nadawanie (port {tx.get('port', '?')}): {main}   {extra}\n")
        fec = live_tunnel_fec()
        if fec:
            level = fec_level_of(*fec)
            w(f"# naprawa pakietow w tunelu: FEC {fec[0]}/{fec[1]}"
              f"   {fec_overhead(*fec):.2f}x pakietow"
              + (f"   ({FEC_LEVELS[level][2]})" if level is not None else "") + "\n")
        w(f"# druga strona: {PEER_NAME} {PEER_IP}\n")
        w(f"# probkowanie: {LOG_SAMPLE_HZ} Hz (co {LOG_SAMPLE_PERIOD:.2f} s);"
          " wfb-ng oddaje statystyki raz na sekunde,\n"
          "#   wiec kolumny sygnalu powtarzaja sie miedzy jego aktualizacjami\n")
        w(f"# limit rozmiaru: {human_size(TEST_MAX_BYTES)} - po nim zapis konczy sie sam\n#\n")
        w(";".join(self.COLUMNS) + "\n")
        self._fh.flush()
        self.size = self._fh.tell()

    def note(self, text):
        """Komentarz w srodku pliku - np. o wyzerowaniu licznikow, zeby przy
        czytaniu bylo widac, ze w tym miejscu cos sie zmienilo.

        Samo slowo MARK_TEXT to znacznik z klawisza: numer dopisujemy tutaj,
        bo tylko ten proces widzi caly zapis. TUI moze sie w miedzyczasie
        zamknac i otworzyc, a numeracja i tak idzie po kolei."""
        if not self._fh:
            return
        stamp = time.strftime("%H:%M:%S")
        if text.strip().upper() == MARK_TEXT:
            self.marks.append((stamp, self._elapsed))
            text = f"{MARK_TEXT} {len(self.marks)}"
        self._fh.write(f"# {stamp}  {text}\n")
        self._fh.flush()
        self.size = self._fh.tell()

    def sample(self, elapsed, metrics, ping):
        self._elapsed = elapsed
        rtt, last_loss = ping[0], ping[1]
        self._ping_sent, self._ping_recv = ping[3], ping[4]
        rssi, snr, loss = metrics["best_rssi"], metrics["best_snr"], metrics["loss"]
        ants = " ".join(f"{a['label'].replace(' ', ':')}={a['rssi'][1]:.0f}"
                        for a in metrics["ants"] if a["rssi"])
        self._run.update(metrics)

        def num(value, fmt="{:.1f}"):
            return fmt.format(value) if value is not None else ""

        self._fh.write(";".join([
            time.strftime("%H:%M:%S"), f"{elapsed:.2f}",  # przy 4 Hz sekundy
                                                          # musza miec ulamek
            num(rssi, "{:.0f}"), num(snr, "{:.0f}"),
            num(metrics["mcs"], "{:.0f}"), num(metrics["bw"], "{:.0f}"),
            num(metrics["loss_before"], "{:.2f}"), num(loss),
            num(metrics["saved_pct"], "{:.2f}"),
            num(self._run.per_before, "{:.2f}"), num(self._run.per, "{:.2f}"),
            f"{metrics['rx_pps']:.0f}", f"{mbit(metrics['rx_bytes']):.2f}",
            f"{metrics['fec']:.0f}", f"{metrics['lost']:.0f}",
            num(rtt[1] if rtt else None), num(last_loss, "{:.0f}"), ants,
        ]) + "\n")
        self._fh.flush()  # zeby po Ctrl+C albo zaniku zasilania zostalo to, co juz bylo
        self.samples += 1
        self.size = self._fh.tell()

        self._rssi.add(rssi)
        self._loss.add(loss)
        self._loss_before.add(metrics["loss_before"])
        self._ping.add(rtt[1] if rtt else None)
        if metrics["mcs"] is not None:
            key = (metrics["mcs"], metrics["bw"])
            self._mcs[key] = self._mcs.get(key, 0) + 1

    def close(self, reason, elapsed):
        if not self._fh:
            return
        w = self._fh.write
        w("#\n# --- podsumowanie ---\n")
        w(f"# koniec: {time.strftime('%Y-%m-%d %H:%M:%S')}   ({reason})\n")
        w(f"# czas testu: {int(elapsed) // 60} min {int(elapsed) % 60} s"
          f"   probek: {self.samples} ({LOG_SAMPLE_HZ} Hz)"
          f"   rozmiar: {human_size(self.size)}\n")
        # Znaczniki zebrane w jednym miejscu: w srodku pliku leza rozrzucone
        # miedzy tysiacami wierszy, a tutaj od razu widac, o ktorej minucie
        # testu cos sie dzialo - takze bez otwierania podgladu.
        for i, (stamp, when) in enumerate(self.marks, 1):
            w(f"# znacznik {i}: {stamp}   {int(when) // 60}:{int(when) % 60:02d}"
              " od startu zapisu\n")
        w(f"# RSSI [dBm]:  {self._rssi.line('{:.0f}')}\n")
        w(f"# straty przed naprawa [%]: {self._loss_before.line()}\n")
        w(f"# straty po naprawie [%]:   {self._loss.line()}\n")
        w(f"# ping [ms]:   {self._ping.line()}\n")
        if self._ping_sent:
            # Ta liczba bez komentarza wprowadza w blad przy zestawieniu z PER
            # nizej: PER opisuje JEDEN kierunek (to, co tu przyszlo), a ping
            # musi przejsc tam i z powrotem. Jesli ping gubi wyraznie wiecej
            # niz PER, to gubi kierunek przeciwny - ten, ktorego ten ekran
            # w ogole nie widzi, i trzeba go zmierzyc z drugiej strony.
            lost = self._ping_sent - self._ping_recv
            w(f"# ping: {lost} zgubionych z {self._ping_sent}"
              f" ({100.0 * lost / self._ping_sent:.2f}%) - strata W OBIE STRONY,\n"
              "#   wiec porownuj ja z PER ponizej, ktory liczy tylko odbior\n")
            if not self._ping_recv and self._rssi.lo is not None:
                # Odbior byl (jest RSSI), a nie wrocil ani jeden ping - to nie
                # jest "slaby link", tylko zerwany kierunek W GORE. Przy
                # czytaniu logu po locie sama kolumna pingu tego nie mowi.
                w("# UWAGA: przez caly test nie wrocil ANI JEDEN ping, a sygnal"
                  " byl odbierany -\n#   lacze dzialalo tylko W DOL, druga strona"
                  " nas nie slyszala\n")

        run, per = self._run.totals, self._run.per
        seen = run["rx"] + run["lost"]
        w(f"# blad pakietow (PER) z calego testu: "
          + (f"{per:.2f}% - {run['lost']:.0f} utraconych z {seen:.0f}"
             if per is not None else "brak danych - nic nie przyszlo") + "\n")
        if run["rx"]:
            # To jest liczba, dla ktorej warto bylo w ogole ustawiac FEC: ile
            # pakietow zgubilo sie w powietrzu, a mimo to doszlo.
            w(f"# uratowane przez naprawe: {run['fec']:.0f} pakietow"
              f" ({self._run.fec_pct:.2f}% odebranych)\n")
            if self._run.per_before is not None:
                w(f"# straty bez naprawy byly by {self._run.per_before:.2f}%,"
                  f" sa {per:.2f}% - naprawa zdjela {self._run.saved_pct:.2f}"
                  " punktu procentowego\n")
        if run["bad"]:
            w(f"# ramki bledne/nieodszyfrowane: {run['bad']:.0f}\n")
        if self._run.restarts:
            w(f"# usluga wfb-ng restartowala sie w trakcie: {self._run.restarts}x\n")
        for (mcs, bw), count in sorted(self._mcs.items(), key=lambda kv: -kv[1]):
            desc, rate = mcs_info(mcs, bw)
            w(f"# odbior: {desc}, {bw_mhz(bw)} MHz"
              + (f", ~{rate:.0f} Mbit/s PHY" if rate else "")
              + f" - w {count} z {self.samples} probek\n")
        if self._rssi.lo is not None:
            w(f"# najslabszy sygnal: {self._rssi.lo:.0f} dBm"
              f" (sila {rssi_grade(self._rssi.lo)[1]})\n")
        if self._loss.hi is not None:
            w(f"# najwieksze straty: {self._loss.hi:.1f}% ({loss_grade(self._loss.hi)[1]})\n")
        self._fh.flush()
        self.size = self._fh.tell()
        self._fh.close()
        self._fh = None


def background_recorder(path):
    """Proces zapisu testu: wlasne sondy (statystyki wfb-ng + ping), cztery
    probki na sekunde do pliku i raz na sekunde odswiezony plik stanu dla TUI.

    Odpalany przez ekran testu z flaga RECORDER_FLAG, w nowej sesji - dlatego
    zamkniecie ekranu testu ani calego programu go nie dotyka. Ekran testu ma
    wlasne sondy i tylko pokazuje, co ten proces zdazyl zapisac.

    Konczy sie na trzy sposoby: sygnalem (uzytkownik wybral "zakoncz"),
    po osiagnieciu TEST_MAX_BYTES albo na bledzie zapisu - w kazdym z nich
    dopisuje do pliku podsumowanie i zostawia powod w stanie."""
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda *_: stop.set())

    started_txt = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        recorder = TestRecorder(path).open()
    except OSError as e:
        write_test_state(stan="blad", pid=os.getpid(), plik=path,
                         start=started_txt, powod=str(e))
        return 1

    def save_state(stan, powod="", elapsed=0.0):
        write_test_state(stan=stan, pid=os.getpid(), plik=recorder.path,
                         start=started_txt, czas=f"{elapsed:.1f}",
                         probek=recorder.samples, bajtow=recorder.size,
                         znacznikow=len(recorder.marks),
                         limit=TEST_MAX_BYTES, powod=powod)

    save_state("trwa")
    stats = WfbStatsProbe().start()
    ping = PingProbe(PEER_IP).start()

    started = time.monotonic()
    next_nic_scan = started + 2.0
    next_state = started + 1.0
    next_sample = started
    nics = wfb_nics()
    reason = "zatrzymany przez uzytkownika"
    elapsed = 0.0

    try:
        while not stop.is_set():
            now = time.monotonic()
            elapsed = now - started
            # lista kart jest droga (wfb-nics), a zmienia sie rzadko - tak samo
            # jak na ekranie testu odswiezamy ja co dwie sekundy
            if now >= next_nic_scan:
                nics = wfb_nics()
                next_nic_scan = now + 2.0

            notes = take_test_notes()
            for note in notes:
                recorder.note(note)
            if notes:
                # Numer znacznika ma wrocic na ekran od razu, a nie przy
                # najblizszym odswiezeniu stanu - inaczej po nacisnieciu
                # klawisza przez sekunde nie wiadomo, czy w ogole trafil.
                next_state = now + 1.0
                save_state("trwa", elapsed=elapsed)

            metrics = link_metrics(stats.snapshot()[0], nics)
            try:
                recorder.sample(elapsed, metrics, ping.snapshot())
            except OSError as e:
                reason = f"blad zapisu: {e}"  # np. brak miejsca na karcie
                break
            if recorder.size >= TEST_MAX_BYTES:
                reason = f"osiagniety limit {human_size(TEST_MAX_BYTES)}"
                break

            if now >= next_state:
                next_state = now + 1.0
                save_state("trwa", elapsed=elapsed)

            # tempo liczone od stalej siatki, a nie "spij 0.25 s" - inaczej
            # czas kazdej probki podjadalby sie o tyle, ile trwalo jej liczenie.
            # Gdy siatka ucieknie o wiecej niz sekunde (zamulone wfb-nics,
            # obciazony Pi), zaczynamy ja od nowa zamiast nadrabiac w kolko.
            next_sample += LOG_SAMPLE_PERIOD
            if next_sample < time.monotonic() - 1.0:
                next_sample = time.monotonic()
            stop.wait(max(0.0, next_sample - time.monotonic()))
    finally:
        stats.close()
        ping.close()
        try:
            recorder.close(reason, elapsed)
        except OSError as e:
            reason = f"blad przy zamykaniu pliku: {e}"
        save_state("zakonczony", reason, elapsed)
    return 0


def meter(value, lo, hi, width=18):
    """Pasek postepu - w terminalu latwiej ocenic "ile brakuje" z paska niz
    z samej liczby, zwlaszcza gdy patrzy sie na ekran co chwile podczas
    chodzenia z antena. Pelny pasek zawsze znaczy "dobrze", wiec dla wartosci,
    ktore lepiej miec male (opoznienie), podaje sie lo/hi na odwrot."""
    if value is None:
        return "[" + "?" * width + "]"
    span = hi - lo
    frac = min(1.0, max(0.0, (value - lo) / span)) if span else 0.0
    filled = int(round(frac * width))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def link_metrics(msgs, nics):
    """Liczby wyluskane z wiadomosci API. Osobno od rysowania, bo dokladnie te
    same wartosci ida na ekran i do pliku z zapisem testu - liczymy je raz.

    Strumieni (wideo, mavlink, tunel) nie rozdzielamy. Przez to lacze idzie
    zwykly ruch IP i to, ktory strumien akurat go niesie, nic nie mowi o jakosci
    radia - a liczenie strat z jednego wybranego strumienia potrafilo pokazywac
    zero tylko dlatego, ze nikt nim nie nadawal. Liczniki sumujemy po wszystkich,
    bo dla radia to i tak jeden strumien ramek."""
    rx_msgs = {name: m for (kind, name), m in msgs.items() if kind == "rx"}
    tx_msgs = {name: m for (kind, name), m in msgs.items() if kind == "tx"}

    ants = []
    for name in sorted(rx_msgs):
        ants.extend(antenna_rows(rx_msgs[name], nics))

    # Modulacja odbieranych ramek. Zwykle jedna dla wszystkich anten, ale przy
    # zmianie ustawien po drugiej stronie potrafia sie chwilowo mieszac -
    # dlatego liczymy pakiety per (MCS, szerokosc) i bierzemy przewazajaca.
    mods = {}
    for a in ants:
        if a["mcs"] is not None:
            key = (a["mcs"], a["bw"])
            mods[key] = mods.get(key, 0) + a["count"]
    top_mod = max(mods, key=mods.get) if mods else (None, None)

    def total(name, idx=0):
        return sum(rx_packets(m, name)[idx] for m in rx_msgs.values())

    # 'all' to wszystko, co dotarlo, 'lost' - dziury wykryte po numerach
    # sekwencji. Pakiety odtworzone przez FEC nie sa strata: doszly, tylko
    # okrezna droga.
    got, lost = total("all"), total("lost")
    fec = total("fec_rec")

    # Straty PRZED naprawa i PO naprawie, liczone na tym samym mianowniku -
    # inaczej nie dalo by sie ich zestawic na jednym wykresie. 'lost' to dziury,
    # ktorych FEC juz nie odratowal, 'fec_rec' to te, ktore odratowal; razem
    # daja to, co naprawde zgubilo sie w powietrzu. Roznica miedzy krzywymi to
    # dokladnie zasluga FEC, czyli pakiety uratowane.
    seen = got + lost
    loss_after = (100.0 * lost / seen) if seen else None
    loss_before = (100.0 * (lost + fec) / seen) if seen else None

    return {
        "rx": rx_msgs,
        "tx": tx_msgs,
        "ants": ants,
        "mods": mods,
        "mcs": top_mod[0],
        "bw": top_mod[1],
        # Przy dywersyfikacji liczy sie NAJLEPSZA antena - wfb-ng i tak sklada
        # strumien z tej, ktora akurat slyszy lepiej.
        "best_rssi": max((a["rssi"][1] for a in ants if a["rssi"]), default=None),
        "best_snr": max((a["snr"][1] for a in ants if a["snr"]), default=None),
        "loss": loss_after,
        # to samo, ale gdyby FEC nie naprawil niczego - "ile gubi samo radio"
        "loss_before": loss_before,
        # ile punktow procentowych strat zdjal z nas FEC
        "saved_pct": (loss_before - loss_after) if seen else None,
        "rx_pps": got,
        "rx_bytes": total("out_bytes") or total("all_bytes"),
        "fec": fec,
        "bad": total("bad") + total("dec_err"),
        "lost": lost,
        # sumy od startu uslugi - same w sobie malo mowia, sluza do liczenia
        # przyrostu od poczatku testu (RunTotals)
        "rx_total": total("all", 1),
        "lost_total": total("lost", 1),
        "fec_total": total("fec_rec", 1),
        "bad_total": total("bad", 1) + total("dec_err", 1),
    }


def link_test_lines(metrics, api_error, nics, used, traffic, ping, worst, run, elapsed):
    """Cala tresc ekranu testu jako lista (tekst, atrybut) - budowana od nowa
    przy kazdym odswiezeniu, bo wszystkie liczby sa chwilowe. Wyjatkiem sa
    'worst' i 'run' - one pamietaja caly przebieg testu."""
    lines = []

    def blank():
        lines.append(("", 0))

    def section(title):
        if lines:
            blank()
        lines.append((title, curses.A_BOLD))

    def row(text, status=None, indent=2):
        lines.append((" " * indent + text, color_for(status) if status else 0))

    rx_msgs, tx_msgs, ants = metrics["rx"], metrics["tx"], metrics["ants"]
    best_rssi, best_snr = metrics["best_rssi"], metrics["best_snr"]
    loss, rx_pps_total = metrics["loss"], metrics["rx_pps"]

    rtt, last_loss, total_loss, sent, recv = ping

    if best_rssi is not None:
        worst["rssi"] = min(worst["rssi"], best_rssi) if worst["rssi"] is not None else best_rssi
    if loss is not None:
        worst["loss"] = max(worst["loss"], loss) if worst["loss"] is not None else loss

    rssi_st, rssi_txt = rssi_grade(best_rssi)
    loss_st, loss_txt = loss_grade(loss)
    snr_st, snr_txt = snr_grade(best_snr)
    ping_st, _ = loss_grade(last_loss)

    # Naglowek ocenia CALY przebieg, a nie ostatnia sekunde i ostatnie trzy
    # pingi. Powod jest arytmetyczny: przy 20 pkt/s jeden zgubiony pakiet to
    # 5%, a przy trzech pingach jeden zgubiony to od razu 33% - obie wartosci
    # wpadaja wtedy w prog "duze" i naglowek krzyczy ZLE, chociaz z calego
    # testu wychodzi ponizej procenta. Chwilowe wartosci zostaja przy swoich
    # wierszach nizej; tam sa na miejscu, bo mowia "co sie dzieje TERAZ".
    seen_run = run.totals["rx"] + run.totals["lost"]
    run_loss_st = loss_grade(run.per)[0] if seen_run >= GRADE_MIN_PACKETS else None
    run_ping_st = loss_grade(total_loss)[0] if sent >= GRADE_MIN_PINGS else None
    warming = run_loss_st is None and run_ping_st is None

    # "Lacze w jedna strone": slychac druga strone, ale zadna nasza wiadomosc
    # do niej nie dociera. Sam martwy ping tego nie mowi - wyglada identycznie
    # jak "wszystko padlo" - a to zupelnie inna usterka: radio W DOL jest
    # sprawne, nie dziala kierunek W GORE (jego odbior, jego antena albo nasze
    # nadawanie). Bez tego ekran pokazywal ZLE i kazal szukac problemu
    # w sygnale, ktory akurat byl w porzadku.
    #
    # "recv == 0 od poczatku testu", a nie z ostatniej proby: chodzi o "ani
    # jedna odpowiedz nie wrocila", a nie o chwilowy zanik w locie.
    heard = bool(ants) or rx_pps_total > 0
    # ten sam licznik, ktory nizej pokazuje sekcja "Nadawanie (TX)" - zeby
    # diagnoza nie mowila czegos innego niz liczba widoczna na ekranie;
    # liczniki jadra jako zapasowe zrodlo, gdy API akurat nie odpowiada
    injected = sum(rx_packets(m, "injected")[0] for m in tx_msgs.values())
    we_tx = injected > 0 or any(tx > 0 for _rx, tx in traffic.values())
    one_way = heard and sent >= GRADE_MIN_PINGS and recv == 0

    # "Slyszymy" cos, co pasuje do formatu ramki wfb-ng, ale ANI JEDNA sie nie
    # rozszyfrowala i ani jedna nie dala statystyk anteny (RSSI). To NIE jest
    # to samo, co "heard" powyzej - realny, choc slaby sygnal zawsze przepusci
    # czesc ramek i da choc jedno RSSI. Same bledne/nieodszyfrowane od poczatku
    # testu to podrecznikowy obraz niezgodnych kluczy/parowania, a nie zerwanego
    # kierunku - bez tego ekran kazal szukac anteny RX u drugiej strony, choc
    # naprawde nic tu sie nie deszyfruje.
    keys_mismatch = (not ants and run.totals["rx"] >= GRADE_MIN_PACKETS
                      and run.totals["bad"] >= run.totals["rx"])

    overall = worst_status([s for s in (rssi_st, run_loss_st, snr_st, run_ping_st) if s])
    if keys_mismatch:
        overall, overall_txt = "fail", "NIEZGODNE KLUCZE"
    elif not ants and rx_pps_total <= 0 and not rtt:
        overall, overall_txt = "fail", "BRAK ODBIORU"
    elif one_way:
        overall, overall_txt = "fail", ("TYLKO W DOL" if we_tx else "NIE NADAJEMY")
    else:
        overall_txt = {"ok": "DOBRE", "warn": "SLABE", "fail": "ZLE"}.get(overall, "?")
        # Prog ten sam co dawne "doskonaly" (-50 dBm), tylko wyrazony w skali:
        # 9/10 wypada dokladnie na -50 dBm. Porownanie po LICZBIE, a nie po
        # napisie - opis zmienia sie razem ze skala, prog nie ma prawa.
        if overall == "ok" and (rssi_score(best_rssi) or 0) >= 9:
            overall_txt = "DOSKONALE"

    head = f"Ocena lacza: {overall_txt}"
    parts = []
    if best_rssi is not None:
        parts.append(f"sygnal {rssi_txt}  ({best_rssi:.0f} dBm)")
    else:
        parts.append(f"sygnal: {rssi_txt}")
    if metrics["mcs"] is not None:
        parts.append(f"MCS {metrics['mcs']}")
    if loss is not None:
        parts.append(f"straty {loss:.1f}%")
    if run.per is not None:
        parts.append(f"PER {run.per:.2f}%")
    if rtt:
        parts.append(f"ping {rtt[1]:.1f} ms")
    parts.append(f"czas testu {int(elapsed) // 60:02d}:{int(elapsed) % 60:02d}")
    lines.append((f"{head}   " + "   ".join(parts), color_for(overall) | curses.A_BOLD))

    # Bez tego pierwsze kilkanascie sekund wygladaja na blad: ocena stoi na
    # samym sygnale i uparcie pokazuje DOSKONALE, chocby wlasnie lecialy straty.
    if warming and ants:
        row("(ocena strat wlaczy sie po kilkunastu sekundach - tyle trzeba, zeby "
            "liczby cokolwiek znaczyly)")

    if overall == "fail" and not ants and rx_pps_total <= 0 and not keys_mismatch:
        row("Nic nie przychodzi z drugiej strony. Sprawdz po obu stronach: ten sam kanal,", "fail")
        row("ten sam odcisk kluczy, wlaczona usluga i moc TX wieksza od zera.", "fail")
        # Bez kamery jedynym ruchem sa odpowiedzi na nasz ping, wiec zerwany
        # kierunek W GORE wyglada tu DOKLADNIE tak samo jak wylaczona druga
        # strona: ona nie dostaje pytania, wiec nie odpowiada i cisza jest po
        # obu stronach. Rozroznia je dopiero ruch, ktory tamta strona nadaje sama.
        row(f"Uwaga: bez kamery {PEER_NAME} nadaje tylko odpowiedzi na nasz ping, wiec")
        row("zerwany kierunek W GORE wyglada tak samo jak wylaczona druga strona.")
        row(f"Zeby je rozroznic, odpal na {PEER_NAME} test obciazeniowy - on nadaje")
        row("sam z siebie, nie musi niczego odbierac.")

    if keys_mismatch:
        row(f"Karty lapia ramki pasujace do formatu wfb-ng, ale ANI JEDNA sie nie"
            f" rozszyfrowala ({run.totals['bad']:.0f} z {run.totals['rx']:.0f}) i"
            " zadna nie dala statystyk anteny.", "fail")
        row(f"To nie slaby sygnal ani zerwany kierunek - klucze/parowanie miedzy"
            f" nami a {PEER_NAME} sie nie zgadzaja.", "fail")
        row("Sprawdz na OBU stronach: menu -> Klucze i parowanie -> odcisk kluczy")
        row("musi byc identyczny; jak nie jest - sparuj urzadzenia ponownie.")
    elif one_way and we_tx:
        row(f"Slychac {PEER_NAME}, ale nie wrocila ANI JEDNA nasza wiadomosc"
            f" ({recv} z {sent} pingow).", "fail")
        row("Lacze dziala TYLKO W DOL: to, co wysylamy stad, do niego nie dociera.", "fail")
        row(f"Sprawdz na {PEER_NAME}: czy odbior jest wlaczony (usluga wfb-ng), antene RX,")
        row("ten sam kanal i ten sam odcisk kluczy. U nas: moc TX > 0 i adres tunelu.")
    elif one_way:
        row(f"Slychac {PEER_NAME}, ale nasze karty nie wstrzykuja ani jednej ramki -", "fail")
        row("to TA strona nie nadaje. Sprawdz usluge wfb-ng i moc TX.", "fail")

    if api_error:
        section("Statystyki wfb-ng")
        row(api_error, "warn")
        row("Ping i liczniki kart ponizej dzialaja niezaleznie od API.")

    section("Modulacja")
    mods = metrics["mods"]
    if mods:
        for (mcs, bw), count in sorted(mods.items(), key=lambda kv: -kv[1]):
            desc, rate = mcs_info(mcs, bw)
            row(f"{'odbior':<11}{desc}   {bw_mhz(bw)} MHz"
                + (f"   ~{rate:.0f} Mbit/s (PHY)" if rate else "")
                # licznik tylko przy kilku modulacjach naraz - jako wskazowka,
                # ktora przewaza; osobno nie znaczy nic, bo te same ramki
                # licza sie na kazdej antenie z osobna
                + (f"   {count:.0f} pkt/s na antenach" if len(mods) > 1 else ""))
    else:
        row(f"{'odbior':<11}brak danych - nic nie przychodzi", "warn")
    tx_params = tx_radio_params()
    by_mod = {}
    for tx in tx_params:
        by_mod.setdefault(tx_modulation_txt(tx), []).append(str(tx.get("port", "?")))
    for (main, extra), ports in by_mod.items():
        # numery gniazd tylko wtedy, gdy nadajniki roznia sie ustawieniami -
        # przy jednakowych to zbedny szum, bo nadajemy wszystkim tak samo
        where = f"   (porty {', '.join(ports)})" if len(by_mod) > 1 else ""
        row(f"{'nadawanie':<11}{main}{where}")
        row(extra, indent=13)
    if not tx_params:
        row(f"{'nadawanie':<11}nie widac zadnego wfb_tx - usluga nie dziala?", "warn")
    row("(odbior = czym nadaje druga strona, nadawanie = czym nadajemy my;")
    row(" predkosc odbioru liczona przy dlugim GI, bo ramka jej nie niesie)")

    if ants:
        # Osobny wiersz na KAZDY tor odbiorczy (karta + antena), a nie tylko
        # zlozony wynik - przy dwoch oddzielnych kartach (np. RX-only i TX-only
        # na dronie) trzeba widziec sile sygnalu tam, gdzie faktycznie sie
        # sluchamy, a nie jedna uśrednioną liczbę. wfb-ng i tak sklada strumien
        # z toru, ktory akurat slyszy lepiej - ten ma dopisek "najlepsza".
        section(f"Sygnal odbierany z {PEER_NAME}")
        with_rssi = [a for a in ants if a["rssi"]]
        best = max(with_rssi, key=lambda a: a["rssi"][1], default=None)
        if not with_rssi:
            row("brak danych o sygnale - ramki przychodza bez statystyk anten", "warn")
        for a in ants:
            label = a["label"]
            if not a["rssi"]:
                row(f"{label:<16}brak statystyk RSSI", "warn")
                continue
            rssi, snr = a["rssi"], a["snr"]
            st, txt = rssi_grade(rssi[1])
            mark = "  <- najlepsza" if a is best and len(with_rssi) > 1 else ""
            row(f"{label:<16}RSSI {rssi[0]:>5.0f}/{rssi[1]:>5.0f}/{rssi[2]:>5.0f} dBm  "
                f"{meter(rssi[1], -90, -40)}  sila {txt}{mark}", st)
            if snr:
                sst, stxt = snr_grade(snr[1])
                row(f"{'':<16}SNR  {snr[0]:>5.0f}/{snr[1]:>5.0f}/{snr[2]:>5.0f} dB   "
                    f"{meter(snr[1], 0, 40)}  {stxt}", sst)
            # bez licznika ramek: statystyki anten przychodza osobno dla kazdego
            # strumienia, wiec liczba z jednego wiersza nie jest calym ruchem -
            # ten jest ponizej, w sekcji odbioru
            where = f"{a['freq']} MHz" if a["freq"] else ""
            if a["mcs"] is not None:
                where += ("   " if where else "") + f"MCS {a['mcs']}"
            if where:
                row(f"{'':<16}kanal  {where}")
        row("(min / srednia / max w ostatniej sekundzie)")

    # Bez podzialu na wideo / mavlink / tunel: to jedno lacze IP i moze nim isc
    # cokolwiek, wiec liczy sie suma. Nazwa strumienia mowi tylko, ktorym
    # gniazdem szedl pakiet, a nie jak zachowuje sie radio.
    if rx_msgs:
        section("Odbior (RX)")
        row(f"{'odebrane':<16}{rx_pps_total:>7.0f} pkt/s   "
            f"{mbit(metrics['rx_bytes']):>7.2f} Mbit/s", loss_st)
        row(f"FEC naprawil {metrics['fec']:.0f}/s   utracone {metrics['lost']:.0f}/s"
            + (f" ({loss:.1f}%)" if loss is not None else "")
            + f"   bledne {metrics['bad']:.0f}/s", loss_st, indent=4)
        row(f"od startu uslugi: odebrane {metrics['rx_total']:.0f}, "
            f"utracone {metrics['lost_total']:.0f}", indent=4)
        row("(FEC naprawil = pakiety odtworzone z nadmiarowych - doszly, ale link sie meczy)")

        # Te dwie liczby obok siebie odpowiadaja na pytanie "czy naprawa cos
        # daje": pierwsza to straty samego radia, druga to te, ktorych nie
        # udalo sie odratowac. Ich roznica to pakiety uratowane.
        if metrics["loss_before"] is not None:
            before, saved = metrics["loss_before"], metrics["saved_pct"] or 0.0
            row(f"straty przed naprawa {before:>5.2f}%  ->  po naprawie {loss:>5.2f}%"
                f"   (uratowane {saved:.2f} pkt proc.)",
                loss_grade(before)[0], indent=4)

        # To jest odpowiedz na "ile gubimy": pojedyncza sekunda potrafi pokazac
        # 0% albo 30% zaleznie od tego, kiedy sie spojrzy, a przy nadawaniu
        # z innego programu liczy sie caly przebieg.
        section("Blad pakietow (PER) od poczatku testu")
        totals, per = run.totals, run.per
        seen = totals["rx"] + totals["lost"]
        if per is None:
            row("nic jeszcze nie doszlo - PER policzy sie, gdy ruszy nadawanie", "warn")
        else:
            row(f"PER {per:>6.2f}%   {meter(per, 5, 0)}   "
                f"{totals['lost']:.0f} utraconych z {seen:.0f}", loss_grade(per)[0])
            if totals["rx"]:
                row(f"uratowane przez naprawe: {totals['fec']:.0f} pakietow "
                    f"({run.fec_pct:.2f}%) - zgubione w powietrzu, ale odtworzone",
                    "ok" if totals["fec"] else None, indent=4)
                if run.per_before is not None:
                    row(f"bez naprawy stracilibysmy {run.per_before:.2f}%, "
                        f"tracimy {per:.2f}%", indent=4)
            if totals["bad"]:
                row(f"bledne / nieodszyfrowane: {totals['bad']:.0f}", "warn", indent=4)
        if run.restarts:
            row(f"usluga wfb-ng restartowala sie {run.restarts}x - liczymy dalej",
                "warn", indent=4)
        row("(PER = pakiety, ktorych nie odratowal FEC, wzgledem wszystkich wyslanych;")
        row(" liczone od wejscia na ten ekran, klawisz 'z' zeruje)")

    if tx_msgs:
        section("Nadawanie (TX)")
        inj = sum(rx_packets(m, "injected")[0] for m in tx_msgs.values())
        dropped = sum(rx_packets(m, "dropped")[0] for m in tx_msgs.values())
        tx_bytes = sum(rx_packets(m, "injected_bytes")[0] for m in tx_msgs.values())
        row(f"{'nadane':<16}{inj:>7.0f} pkt/s   {mbit(tx_bytes):>7.2f} Mbit/s   "
            f"odrzucone {dropped:.0f}/s", "warn" if dropped > 0 else None)

        # liczniki kart tez sumujemy po strumieniach - karta jest jedna, nawet
        # gdy nadaje przez nia kilka gniazd naraz
        cards = {}
        for m in tx_msgs.values():
            for label, w_inj, w_drop, lat in tx_wlan_rows(m, nics):
                prev = cards.get(label, (0.0, 0.0, None))
                cards[label] = (prev[0] + w_inj, prev[1] + w_drop,
                                max(lat or 0.0, prev[2] or 0.0) or None)
        for label in sorted(cards):
            w_inj, w_drop, lat = cards[label]
            extra = f"   wstrzykiwanie {lat:.1f} ms" if lat else ""
            row(f"{label:<14}nadane {w_inj:.0f}   odrzucone {w_drop:.0f}{extra}",
                "warn" if w_drop else None, indent=4)

    section(f"Tunel do {PEER_NAME} ({PEER_IP}) - ping leci przez radio")
    if rtt:
        st = "ok" if rtt[1] < 50 else ("warn" if rtt[1] < 150 else "fail")
        row(f"RTT min/sr/max {rtt[0]:.1f}/{rtt[1]:.1f}/{rtt[2]:.1f} ms   {meter(rtt[1], 200, 0)}", st)
    elif heard:
        # Skoro slychac go na radiu, to na pewno NIE jest wylaczony - zostaje
        # jego odbior albo tunel po jego stronie.
        row(f"brak odpowiedzi, a {PEER_NAME} slychac na radiu - wiec on nas nie", "fail")
        row("odbiera albo nie ma po tamtej stronie tunelu", "fail")
    else:
        row("brak odpowiedzi - tunel nie stoi albo druga strona jest wylaczona", "fail")
    if last_loss is not None:
        st = loss_grade(last_loss)[0]
        row(f"utrata: ostatnia proba {last_loss:.0f}%"
            + (f"   od poczatku testu {total_loss:.1f}% ({recv}/{sent} pakietow)"
               if total_loss is not None else ""), st)

    section("Karty (liczniki jadra)")
    if nics:
        for nic in nics:
            rx_pps, tx_pps = traffic.get(nic, (0.0, 0.0))
            row(f"{nic:<14}rx={rx_pps:>7.0f}/s  tx={tx_pps:>7.0f}/s   "
                f"w usludze={'tak' if nic in used else 'NIE'}"
                + ("   <- ta karta nadaje" if tx_pps > 0 else ""),
                "ok" if nic in used else "fail")
    else:
        row("wfb-nics nie zwraca zadnego interfejsu", "fail")

    section("Najgorsze wartosci od poczatku testu")
    worst_rssi_st, worst_rssi_txt = rssi_grade(worst["rssi"])
    row(f"sygnal {worst_rssi_txt}  ({worst['rssi']:.0f} dBm)"
        if worst["rssi"] is not None else f"sygnal: {worst_rssi_txt}", worst_rssi_st)
    row(f"straty {worst['loss']:.1f}%" if worst["loss"] is not None else "straty ?",
        loss_grade(worst["loss"])[0])

    return lines


def test_state_line(state, key="t"):
    """Jedna linijka o zapisie w tle - ta sama na gorze menu i ekranu testu."""
    name = Path(state["plik"]).name
    size = f"{human_size(state['bajtow'])} / {human_size(TEST_MAX_BYTES)}"
    if state["stan"] == "trwa":
        marks = state.get("znacznikow") or 0
        return (f"TEST TRWA W TLE  {fmt_mmss(state['czas'])}   {name}   "
                f"{state['probek']} probek   {size}"
                + (f"   znacznikow: {marks}" if marks else "")
                + f"   (m = znacznik, {key} = zakoncz)")
    powod = state.get("powod") or "koniec"
    return f"ZAPIS TESTU ZAKONCZONY ({powod})   {name}   {size}   ({key} = szczegoly)"


def test_state_attr(state):
    return color_for("ok" if state["stan"] == "trwa" else "warn") | curses.A_BOLD


def test_result_popup(stdscr, state):
    """Podsumowanie zakonczonego zapisu. Sprzata przy okazji plik stanu, zeby
    napis o nim nie wisial w menu w nieskonczonosc."""
    if not state:
        return
    name = Path(state["plik"]).name
    lines = [f"Plik:     {state['plik']}",
             f"Probek:   {state['probek']}   ({LOG_SAMPLE_HZ} na sekunde)",
             f"Rozmiar:  {human_size(state['bajtow'])}",
             f"Czas:     {fmt_mmss(state['czas'])}"]
    if state.get("znacznikow"):
        lines.append(f"Znaczniki: {state['znacznikow']}"
                     "   (na wykresie czerwone kreski)")
    if state.get("powod"):
        lines += ["", f"Powod zakonczenia: {state['powod']}"]
    lines += ["",
              f"Podglad:      less {name}",
              f"Sciagniecie:  scp <user>@<ip>:{state['plik']} ."]
    popup(stdscr, "Zapis zakonczony", lines,
          status="ok" if state["stan"] == "zakonczony" else "warn")
    try:
        TEST_STATE.unlink()
    except OSError:
        pass


def stop_test_popup(stdscr):
    """Zatrzymanie zapisu w tle + pokazanie, co z niego wyszlo."""
    state = stop_test_recorder()
    if state and state["stan"] == "trwa":
        popup(stdscr, "Zapis nie zatrzymal sie",
              ["Proces zapisu nie odpowiedzial przez 5 sekund.",
               f"PID {state['pid']},  plik: {state['plik']}",
               "",
               "Sprobuj jeszcze raz albo zatrzymaj go recznie:",
               f"  sudo kill {state['pid']}"], status="fail")
        return
    test_result_popup(stdscr, state)


def background_test_popup(stdscr):
    """Okienko "co z zapisem w tle": trwajacy mozna stad zakonczyc, zakonczony
    pokazuje podsumowanie i znika z paska. Zwraca stan po tej rozmowie."""
    state = test_state()
    if not state:
        popup(stdscr, "Brak zapisu w tle",
              ["Zaden zapis testu nie jest w tej chwili uruchomiony.",
               "Uruchamia go ekran 'Test polaczenia'."])
        return None
    if state["stan"] != "trwa":
        test_result_popup(stdscr, state)
        return None

    if popup(stdscr, "Test trwa w tle",
             [f"Plik:     {state['plik']}",
              f"Zapisane: {state['probek']} probek   {human_size(state['bajtow'])}"
              f" z {human_size(TEST_MAX_BYTES)}",
              f"Czas:     {fmt_mmss(state['czas'])}   (PID {state['pid']})",
              "",
              "Zapis nie zalezy od tego programu - leci dalej po jego",
              "zamknieciu i sam stanie na limicie rozmiaru."],
             # bezpieczna odpowiedz na koncu: Esc zostawia zapis w spokoju
             buttons=("Przerwij zapis", "Zostaw"), status="ok", default=1) == 0:
        stop_test_popup(stdscr)
        return test_state()
    return state


def link_test_screen(stdscr):
    """Zywy test lacza: co widac po drugiej stronie, jak mocny jest sygnal,
    ile pakietow przepada i jak dlugo leci ping przez radio. Weryfikacja mowi
    "dziala / nie dziala", a to jest ekran do patrzenia w czasie rzeczywistym -
    przy ustawianiu anten, sprawdzaniu zasiegu albo szukaniu czystszego kanalu.

    Sam odswieza sie kilka razy na sekunde i mozna go zostawic wlaczonego -
    po restarcie uslugi podlaczy sie do niej z powrotem. Na wejsciu pyta, czy
    zapisywac przebieg do pliku. Zapis idzie osobnym procesem, wiec NIE konczy
    sie z wyjsciem stad - trwa dalej i widac go na gorze menu glownego."""
    stdscr.clear()
    draw_header(stdscr, f"WFB-NG [{ROLE}] - test polaczenia")

    state = test_state()
    if state and state["stan"] == "trwa":
        popup(stdscr, "Zapis testu juz trwa",
              [f"Plik:    {state['plik']}",
               f"Zapisane: {state['probek']} probek   {human_size(state['bajtow'])}"
               f"   czas {fmt_mmss(state['czas'])}",
               "",
               "Ten ekran tylko go podglada - zapis leci wlasnym tempem.",
               "Konczy go klawisz 't' - tutaj albo w menu glownym.",
               "Klawisz 'm' stawia w logu znacznik."],
              status="ok")
    else:
        if state:
            # poprzedni zapis skonczyl sie, gdy nikogo tu nie bylo (limit, blad,
            # restart) - pokazujemy podsumowanie i sprzatamy, zeby nie mieszalo
            # sie z tym, ktory zaraz ruszy
            test_result_popup(stdscr, state)
            state = None
        path = TEST_LOG_DIR / f"test-{ROLE}-{time.strftime('%Y%m%d-%H%M%S')}.log"
        if popup(stdscr, "Zapis testu do pliku",
                 ["Zapisywac przebieg tego testu do pliku?",
                  "",
                  f"Plik:  {path}",
                  f"{LOG_SAMPLE_HZ} wiersze na sekunde: sygnal, straty, ping.",
                  "",
                  "Zapis idzie osobnym procesem: trwa po wyjsciu z tego ekranu",
                  "i po zamknieciu programu. Konczy go klawisz 't' (tutaj albo",
                  f"w menu glownym); sam staje na {human_size(TEST_MAX_BYTES)}.",
                  "",
                  "W trakcie: klawisz 'm' zostawia w logu znacznik -",
                  "w podgladzie wykresu widac go jako czerwona pionowa kreske."],
                 buttons=("Tak", "Nie")) == 0:
            error = start_test_recorder(path)
            if error:
                popup(stdscr, "Nie udalo sie uruchomic zapisu",
                      [error, "Test ruszy bez zapisu."], status="fail")
            state = test_state()

    # 0.2 s: tempo odrysowywania ekranu. Zapis do pliku ma wlasne (4 Hz)
    # w osobnym procesie i nie zalezy od tego, co tu sie dzieje.
    stdscr.timeout(200)
    stats = WfbStatsProbe().start()
    ping = PingProbe(PEER_IP).start()

    nics = wfb_nics()
    used = service_nics(set(nics))
    counters = {nic: (*nic_counters(nic), time.monotonic()) for nic in nics}
    worst = {"rssi": None, "loss": None}
    run = RunTotals()  # PER i sumy od poczatku testu, nie od startu uslugi
    started = time.monotonic()
    next_nic_scan = started + 2.0
    next_state = started + 0.5
    elapsed = 0.0
    top = 0
    # Potwierdzenie znacznika w dolnej linii - znika samo po paru sekundach,
    # zeby nie zajmowac na stale miejsca podpowiedziom o klawiszach.
    flash = None  # (tekst, do kiedy, status)

    try:
        while True:
            now = time.monotonic()
            # Lista kart i to, ktore z nich siedza w usludze, zmienia sie rzadko,
            # a jest droga (wfb-nics, w gorszym razie journalctl) - ekran
            # odrysowuje sie duzo czesciej, wiec odswiezamy ja co dwie sekundy.
            if now >= next_nic_scan:
                nics = wfb_nics()
                used = service_nics(set(nics))
                next_nic_scan = now + 2.0

            traffic = {}
            for nic in nics:
                rx, tx = nic_counters(nic)
                prev = counters.get(nic)
                if prev and now > prev[2]:
                    dt = now - prev[2]
                    traffic[nic] = (max(0.0, (rx - prev[0]) / dt), max(0.0, (tx - prev[1]) / dt))
                else:
                    traffic[nic] = (0.0, 0.0)  # karta dopiero co wpieta, brak odniesienia
                counters[nic] = (rx, tx, now)

            msgs, api_error = stats.snapshot()
            ping_snap = ping.snapshot()
            metrics = link_metrics(msgs, nics)
            run.update(metrics)
            elapsed = now - started
            lines = link_test_lines(metrics, api_error, nics, used, traffic,
                                    ping_snap, worst, run, elapsed)

            # Stan zapisu czytamy z pliku, bo pisze go inny proces. Dwa razy
            # na sekunde wystarczy - on i tak odswieza go raz na sekunde.
            if now >= next_state:
                next_state = now + 0.5
                state = test_state()

            stdscr.erase()
            draw_header(stdscr, f"WFB-NG [{ROLE}] - test polaczenia")
            if state:
                safe_addstr(stdscr, 1, 2, test_state_line(state), test_state_attr(state))
            h, _ = stdscr.getmaxyx()
            view = max(1, h - 3)
            top = max(0, min(top, max(0, len(lines) - view)))
            for i, (text, attr) in enumerate(lines[top:top + view]):
                safe_addstr(stdscr, 2 + i, 2, text, attr)

            hint = "q = powrot, z = zeruj liczniki testu"
            if state and state["stan"] == "trwa":
                hint += ", m = znacznik, t = zakoncz zapis"
            if len(lines) > view:
                hint = (f"strzalki = przewijanie ({top + 1}-{min(top + view, len(lines))}"
                        f"/{len(lines)}), " + hint)
            if flash and now < flash[1]:
                safe_addstr(stdscr, h - 1, 2, flash[0],
                            color_for(flash[2]) | curses.A_BOLD)
            else:
                flash = None
                safe_addstr(stdscr, h - 1, 2, hint, curses.A_DIM)
            stdscr.refresh()

            key = stdscr.getch()
            if key in (ord("q"), ord("Q"), 27):
                break
            elif key in (curses.KEY_DOWN, ord("j")):
                top += 1
            elif key in (curses.KEY_UP, ord("k")):
                top -= 1
            elif key == curses.KEY_NPAGE:
                top += view
            elif key == curses.KEY_PPAGE:
                top -= view
            elif key in (ord("z"), ord("Z")):
                worst.update(rssi=None, loss=None)
                ping.reset()
                run.reset()
                started = now
                note_test_recorder("wyzerowano liczniki testu")
            elif key in (ord("m"), ord("M")):
                # TYLKO 'm' - spacja odpada celowo: to najlatwiejszy klawisz do
                # przypadkowego trafienia, a falszywy znacznik w logu jest
                # gorszy niz jego brak (szukalo by sie potem zdarzenia, ktorego
                # nie bylo). Numer nadaje proces zapisu, wiec tu tylko
                # potwierdzamy godzine; licznik w gornej linii dojdzie przy
                # najblizszym odczycie stanu (ponizej pol sekundy).
                if state and state["stan"] == "trwa":
                    mark_test_recorder()
                    flash = (f"ZNACZNIK zapisany o {time.strftime('%H:%M:%S')}"
                             " - w podgladzie bedzie czerwona kreska",
                             now + 3.0, "ok")
                    next_state = now  # licznik znacznikow ma sie odswiezyc od razu
                else:
                    flash = ("Zapis nie trwa - znacznik nie ma gdzie trafic",
                             now + 3.0, "warn")
            elif key in (ord("t"), ord("T")):
                stdscr.timeout(-1)  # okienko czeka na klawisz, nie na timeout
                state = background_test_popup(stdscr)
                stdscr.timeout(200)
                stdscr.clear()
    finally:
        stats.close()
        ping.close()
        stdscr.timeout(-1)  # z powrotem na blokujace getch, inaczej menu zwariuje

    # Wyjscie z ekranu NIE konczy zapisu - o tym trzeba powiedziec wprost,
    # bo do tej pory bylo odwrotnie.
    state = test_state()
    if state and state["stan"] == "trwa":
        if popup(stdscr, "Test nadal trwa",
                 [f"Plik:     {state['plik']}",
                  f"Zapisane: {state['probek']} probek   {human_size(state['bajtow'])}"
                  f"   czas {fmt_mmss(state['czas'])}",
                  "",
                  "Zapis leci dalej w tle - takze po zamknieciu programu.",
                  "W menu glownym widac go na gorze; 't' konczy go w kazdej chwili."],
                 # bezpieczna odpowiedz na koncu: Esc zostawia zapis w spokoju
                 buttons=("Przerwij zapis", "Zostaw w tle"), status="ok", default=1) == 0:
            stop_test_popup(stdscr)
    elif state:
        test_result_popup(stdscr, state)


def auto_channel_screen(stdscr, scanned):
    """Tryb automatyczny: sam pilnuje, zeby link stal na dzialajacym kanale.

    Dopoki jest dobrze, nie rusza niczego. Gdy straty rosna albo dane przestaja
    plynac, uzgadnia z druga strona skok na nastepny kanal z listy (najciszsze
    ze skanu na poczatku) i obie strony przeskakuja razem. Jesli po skoku link
    nie wstanie, kazda strona sama wraca na poprzedni kanal - to ratuje sytuacje,
    gdy potwierdzenie doszlo, a dane juz nie.

    Zeby to dzialalo, ten ekran musi byc otwarty PO OBU STRONACH. Decyzje
    podejmuje gs; dron je potwierdza i wykonuje, a przy zupelnej ciszy stoi na
    swoim kanale, zeby bylo gdzie go szukac."""
    nics = wfb_nics()
    if not nics:
        popup(stdscr, "Brak karty", ["wfb-nics nie zwraca zadnego interfejsu."], status="fail")
        return

    channel_txt, region = wfb_effective_common()
    channel = int(channel_txt) if str(channel_txt).isdigit() else int(DEFAULT_CHANNEL)
    candidates = auto_candidates(scanned, channel)

    if popup(stdscr, "Tryb automatyczny",
             ["Sam dobiera kanal, gdy link zaczyna sie sypac.",
              "",
              f"Kolejnosc prob: {', '.join(str(c) for c in candidates) or 'brak'}",
              f"Reaguje po {AUTO_BAD_SECONDS:.0f} s strat powyzej {AUTO_BAD_LOSS:.0f}%.",
              "",
              "WAZNE: ten ekran musi byc otwarty po obu stronach - kanal",
              "zmienia sie tylko po potwierdzeniu przez druga strone.",
              "Bez potwierdzenia nic sie nie rusza."],
             buttons=("Start", "Anuluj")) != 0:
        return

    peer = AutoPeer().start()
    stats = WfbStatsProbe().start()
    auto = AutoChannel(channel, candidates, now=time.monotonic())
    events = []
    hops = 0
    started = time.monotonic()

    def note(text, status=None):
        events.insert(0, (time.strftime("%H:%M:%S"), text, status))
        del events[10:]

    if peer.error:
        note(peer.error, "fail")

    stdscr.timeout(500)
    try:
        while True:
            now = time.monotonic()
            metrics = link_metrics(stats.snapshot()[0], nics)
            alive = metrics["rx_pps"] > 0
            loss = metrics["loss"]

            for action in auto.tick(now, alive, loss, peer.take()):
                kind = action[0]
                if kind == "send":
                    peer.send(action[1])
                elif kind == "note":
                    note(action[1])
                elif kind == "persist":
                    save_common_config(str(action[1]), region)
                    _common_cache["val"] = None
                    note(f"kanal {action[1]} zapisany w configu", "ok")
                elif kind == "hop":
                    hops += 1
                    ok = set_channel_live(action[1])
                    note(f"kanal -> {action[1]} ({channel_freq(action[1])} MHz): {action[2]}"
                         + ("" if ok else "   BLAD: karta nie przyjela kanalu"),
                         "ok" if ok else "fail")

            stdscr.erase()
            draw_header(stdscr, f"WFB-NG [{ROLE}] - automatyczny dobor kanalu")

            state_txt = {"ok": "pilnuje linku", "propose": "czekam na potwierdzenie",
                         "settle": "sprawdzam, czy link wstal",
                         "search": "szukam drugiej strony"}.get(auto.state, auto.state)
            freq = channel_freq(auto.channel)
            safe_addstr(stdscr, 2, 2, f"Kanal {auto.channel} ({freq} MHz)   {state_txt}",
                        color_for("ok" if alive else "fail") | curses.A_BOLD)
            safe_addstr(stdscr, 3, 2,
                        f"Link: {'jest ruch' if alive else 'CISZA'}"
                        + (f"   straty {loss:.1f}%" if loss is not None else "")
                        + (f"   sygnal {metrics['best_rssi']:.0f} dBm"
                           if metrics["best_rssi"] is not None else ""),
                        color_for("ok" if alive and (loss or 0) < AUTO_BAD_LOSS else "warn"))

            ago = peer.peer_seen_ago()
            safe_addstr(stdscr, 4, 2,
                        f"Druga strona ({PEER_NAME} {PEER_IP}): "
                        + (f"odezwala sie {ago:.0f} s temu" if ago is not None
                           else "jeszcze sie nie odezwala - czy tam tez wlaczony tryb auto?"),
                        color_for("ok" if ago is not None and ago < 10 else "warn"))
            safe_addstr(stdscr, 5, 2,
                        f"Rola: {'decyduje' if auto.initiator else 'wykonuje polecenia gs'}"
                        f"   skokow: {hops}   czas: {int(now - started) // 60:02d}:"
                        f"{int(now - started) % 60:02d}")
            safe_addstr(stdscr, 6, 2, "Kolejnosc prob: "
                        + ", ".join(str(c) for c in auto.candidates)
                        + (f"   odpadly: {', '.join(str(c) for c in sorted(auto.blacklist))}"
                           if auto.blacklist else ""))

            safe_addstr(stdscr, 8, 2, "Zdarzenia:", curses.A_BOLD)
            for i, (stamp, text, status) in enumerate(events):
                safe_addstr(stdscr, 9 + i, 4, f"{stamp}  {text}", color_for(status))

            h, _ = stdscr.getmaxyx()
            safe_addstr(stdscr, h - 1, 2, "q = wyjscie (kanal zostaje ten, na ktorym jestesmy)",
                        curses.A_DIM)
            stdscr.refresh()

            if stdscr.getch() in (ord("q"), ord("Q"), 27):
                break
    finally:
        peer.close()
        stats.close()
        stdscr.timeout(-1)
        _nic_status_cache["val"] = None

    if auto.channel != channel:
        save_common_config(str(auto.channel), region)
        _common_cache["val"] = None
        popup(stdscr, "Tryb automatyczny zakonczony",
              [f"Konczymy na kanale {auto.channel} ({channel_freq(auto.channel)} MHz).",
               "Zapisany w configu, wiec przetrwa restart.",
               "",
               "Sprawdz, czy druga strona ma ten sam kanal."], status="ok")


def channel_rows(scan_by_channel, current, ranges):
    """Wiersze listy kanalow: numer, czestotliwosc, pasmo, czy legalny w tym
    regionie i - po skanie - jak bardzo zajety."""
    rows = [(None, "Automatycznie - sam dobiera kanal, gdy link sie sypie"
                   "        (wymaga wlaczenia po obu stronach)", None, False)]
    for channel in CHANNELS_24 + CHANNELS_5:
        freq = channel_freq(channel)
        allowed = channel_allowed(freq, ranges)
        band = "2.4 GHz" if freq and freq < 3000 else "5 GHz"
        text = f"{channel:>4}  {freq:>5} MHz  {band:<8}"
        if allowed is False:
            text += "poza domena  "
            status = "fail"
        else:
            text += "             "
            status = None

        result = scan_by_channel.get(channel)
        if result and "error" in result:
            text += f"skan: {result['error']}"
            status = status or "warn"
        elif result:
            busy = result.get("busy")
            text += (f"zajete {busy:5.1f}%" if busy is not None else "zajete    ?  ")
            if result.get("noise") is not None:
                text += f"   szum {result['noise']:>4} dBm"
            if result.get("pps"):
                text += f"   obce ramki {result['pps']:.0f}/s"
            if status is None and busy is not None:
                status = "ok" if busy < 20 else ("warn" if busy < 50 else "fail")
        rows.append((channel, text, status, channel == current))
    return rows


def channel_screen(stdscr):
    """Wybor kanalu (czyli czestotliwosci) z podpowiedzia, ktory jest wolny.

    Skan przechodzi po kanalach i mierzy przez 'iw survey', ile czasu pasmo
    bylo zajete przez cudze transmisje - w trybie monitor to jedyny sposob,
    bo zwyklego skanowania sieci karta w tym trybie nie zrobi.

    Dwie rzeczy, o ktorych latwo zapomniec: przez caly skan karta jest poza
    kanalem linku (czyli nie ma polaczenia), a po zmianie kanalu link wroci
    dopiero wtedy, gdy ten sam kanal ustawi sie po DRUGIEJ stronie."""
    nics = wfb_nics()
    if not nics:
        popup(stdscr, "Brak karty", ["wfb-nics nie zwraca zadnego interfejsu -",
                                     "nie ma czym ani skanowac, ani nadawac."], status="fail")
        return

    nic = nics[0]
    scanned = {}
    ranges = reg_domain_ranges()[1]
    channel, region = wfb_effective_common()
    current = int(channel) if str(channel).isdigit() else None
    rows = channel_rows(scanned, current, ranges)
    idx = next((i for i, r in enumerate(rows) if r[3]), 0)
    top = 0

    while True:
        stdscr.clear()
        draw_header(stdscr, f"WFB-NG [{ROLE}] - kanal i czestotliwosc")

        freq = channel_freq(channel)
        safe_addstr(stdscr, 2, 2, f"Teraz: kanal {channel}"
                                  f"{f'  ({freq} MHz)' if freq else ''}   region {region}"
                                  f"   karta {nic}", curses.A_BOLD)
        note = channel_source_note(channel)
        safe_addstr(stdscr, 3, 2, note if note else f"kanal wpisany w {CFG_PATH}",
                    color_for("warn") if note else 0)

        h, _ = stdscr.getmaxyx()
        head, foot = 5, 4
        view = max(3, h - head - foot)
        top = max(0, min(top, len(rows) - view)) if len(rows) > view else 0
        idx = max(0, min(idx, len(rows) - 1))
        if idx < top:
            top = idx
        elif idx >= top + view:
            top = idx - view + 1

        for i, (_ch, text, status, is_current) in enumerate(rows[top:top + view]):
            line = ("* " if is_current else "  ") + text
            attr = curses.color_pair(5) if top + i == idx else (
                color_for(status) if status else 0)
            safe_addstr(stdscr, head + i, 2, line.ljust(76), attr)

        best = rank_channels(list(scanned.values()))
        if best:
            b = best[0]
            safe_addstr(stdscr, h - 3, 2,
                        f"Najlepszy ze zmierzonych: kanal {b['channel']} ({b['freq']} MHz)"
                        + (f", zajete {b['busy']:.1f}%" if b.get("busy") is not None else "")
                        + "   [n] ustaw go",
                        color_for("ok") | curses.A_BOLD)
        else:
            safe_addstr(stdscr, h - 3, 2, "* = kanal uzywany teraz. Skan zmierzy, "
                                          "na ktorym kanale jest najciszej.", curses.A_DIM)
        safe_addstr(stdscr, h - 1, 2, "Strzalki, Enter = ustaw zaznaczony, s = skanuj, "
                                      "r = region, q = powrot", curses.A_DIM)
        stdscr.refresh()

        key = stdscr.getch()
        if key in (curses.KEY_UP, ord("k")):
            idx -= 1
        elif key in (curses.KEY_DOWN, ord("j")):
            idx += 1
        elif key == curses.KEY_NPAGE:
            idx += view
        elif key == curses.KEY_PPAGE:
            idx -= view
        elif key in (ord("q"), ord("Q"), 27):
            return
        elif key in (ord("r"), ord("R")):
            region_screen(stdscr)
            channel, region = wfb_effective_common()
            ranges = reg_domain_ranges()[1]
            current = int(channel) if str(channel).isdigit() else None
            rows = channel_rows(scanned, current, ranges)
        elif key in (ord("s"), ord("S")):
            scanned = channel_scan_screen(stdscr, nic, scanned)
            channel, region = wfb_effective_common()
            current = int(channel) if str(channel).isdigit() else None
            rows = channel_rows(scanned, current, ranges)
            best = rank_channels(list(scanned.values()))
            if best:
                idx = next((i for i, r in enumerate(rows) if r[0] == best[0]["channel"]), idx)
        elif key in (10, 13, curses.KEY_ENTER, ord("n"), ord("N")):
            if key in (ord("n"), ord("N")):
                best = rank_channels(list(scanned.values()))
                if not best:
                    continue
                target = best[0]["channel"]
            else:
                target = rows[idx][0]
            if target is None:  # pierwsza pozycja listy - tryb automatyczny
                auto_channel_screen(stdscr, scanned)
                channel, region = wfb_effective_common()
                current = int(channel) if str(channel).isdigit() else None
                rows = channel_rows(scanned, current, ranges)
                continue
            if apply_channel(stdscr, target, region, ranges):
                channel, region = wfb_effective_common()
                current = int(channel) if str(channel).isdigit() else None
                rows = channel_rows(scanned, current, ranges)


def apply_channel(stdscr, channel, region, ranges):
    """Zapisuje kanal, restartuje usluge i sprawdza, na czym karta faktycznie
    stanela. Zwraca True, gdy cos zostalo zmienione."""
    freq = channel_freq(channel)
    allowed = channel_allowed(freq, ranges)
    lines = [f"Ustawic kanal {channel} ({freq} MHz)?",
             "",
             "Kanal MUSI byc taki sam po obu stronach - dopoki nie ustawisz",
             "tego samego na drugim urzadzeniu, linku NIE bedzie.",
             f"Usluga wifibroadcast@{ROLE} zostanie zrestartowana."]
    if allowed is False:
        lines.insert(1, f"UWAGA: {freq} MHz jest poza pasmem dozwolonym w tym regionie -")
        lines.insert(2, "karta moze w ogole nie nadawac.")
    if popup(stdscr, "Zmiana kanalu", lines, buttons=("Tak", "Nie")) != 0:
        return False

    if not CFG_PATH.exists():
        CFG_PATH.write_text(build_config(str(channel), region))
    else:
        save_common_config(str(channel), region)
    run(["systemctl", "restart", f"wifibroadcast@{ROLE}"])
    time.sleep(3)
    _common_cache["val"] = None
    _nic_status_cache["val"] = None

    effective = wfb_effective_common()[0]
    on_card = [f"{n}: kanal {nic_details(n)['channel']}" for n in wfb_nics()]
    if not service_active():
        popup(stdscr, "Zapisano, ale usluga nie dziala",
              [f"Status: {service_state_txt()}", "Ostatnie linie journala:"]
              + [ln[:70] for ln in service_last_errors(4)], status="fail")
    elif str(effective) != str(channel):
        popup(stdscr, "Zapisano, ale wfb-ng widzi co innego",
              [f"W configu {channel}, a wfb-ng uzywa {effective}.",
               f"Sprawdz [common] w {CFG_PATH}."], status="warn")
    else:
        popup(stdscr, "Kanal ustawiony",
              [f"Kanal {channel} ({freq} MHz)", "   ".join(on_card) or "brak kart",
               "", "Pamietaj o tym samym kanale po drugiej stronie."], status="ok")
    return True


def channel_scan_screen(stdscr, nic, previous):
    """Skan pasma z podgladem na zywo. Kanal wyjsciowy i usluga wracaja na
    swoje miejsce w kazdym przypadku - takze gdy skan sie wysypie."""
    choice = popup(stdscr, "Skanowanie kanalow",
                   ["Ktore pasmo przeskanowac?",
                    "",
                    f"2.4 GHz to {len(CHANNELS_24)} kanalow (~{len(CHANNELS_24) * 2} s),",
                    f"5 GHz - {len(CHANNELS_5)} kanalow bez DFS (~{len(CHANNELS_5) * 2} s).",
                    "",
                    "Przez caly skan karta jest poza kanalem linku, wiec obraz",
                    "i telemetria znikna. Jesli laczysz sie po SSH przez tunel",
                    "wfb, stracisz to polaczenie - skanuj z lokalnej konsoli."],
                   buttons=("2.4 GHz", "5 GHz", "Oba", "Anuluj"), default=0)
    channels = {0: CHANNELS_24, 1: CHANNELS_5, 2: CHANNELS_24 + CHANNELS_5}.get(choice)
    if not channels:
        return previous

    before = nic_details(nic)["channel"]
    results = dict(previous)
    stdscr.clear()
    draw_header(stdscr, f"WFB-NG [{ROLE}] - skanowanie kanalow")
    safe_addstr(stdscr, 2, 2, f"Karta {nic}, kanalow: {len(channels)}. "
                              "Nie przerywaj - na koncu wracam na kanal linku.",
                curses.A_BOLD)
    stdscr.refresh()

    h, _ = stdscr.getmaxyx()
    row_top = 4
    done = [0]

    def show(entry):
        done[0] += 1
        line = f"{entry['channel']:>4}  {entry['freq']:>5} MHz   "
        if "error" in entry:
            line += entry["error"]
            status = "warn"
        else:
            busy = entry.get("busy")
            line += (f"zajete {busy:5.1f}%" if busy is not None else "zajete    ?  ")
            if entry.get("noise") is not None:
                line += f"   szum {entry['noise']:>4} dBm"
            status = ("ok" if busy is not None and busy < 20 else
                      "warn" if busy is not None and busy < 50 else "fail")
        y = row_top + (done[0] - 1) % max(1, h - row_top - 2)
        safe_addstr(stdscr, y, 4, line.ljust(70), color_for(status))
        safe_addstr(stdscr, h - 1, 2, f"{done[0]}/{len(channels)} kanalow...", curses.A_DIM)
        stdscr.refresh()

    try:
        for entry in scan_channels(nic, channels, on_result=show):
            results[entry["channel"]] = entry
    finally:
        # zawsze, nawet po bledzie: karta na swoj kanal, usluga od nowa -
        # inaczej link zostaje na przypadkowej czestotliwosci
        if before and before.isdigit():
            set_nic_channel(nic, int(before))
        run(["systemctl", "restart", f"wifibroadcast@{ROLE}"])
        time.sleep(2)
        _nic_status_cache["val"] = None

    best = rank_channels(list(results.values()))
    lines = [f"Przeskanowano {len(channels)} kanalow.", ""]
    for entry in best[:5]:
        lines.append(f"kanal {entry['channel']:>4} ({entry['freq']} MHz):"
                     + (f"  zajete {entry['busy']:.1f}%" if entry.get("busy") is not None
                        else "  zajete ?")
                     + (f"  szum {entry['noise']} dBm" if entry.get("noise") is not None else ""))
    if not best:
        lines.append("Karta nie oddala zadnych pomiarow (brak 'iw survey'?).")
    else:
        lines += ["", "Klawisz [n] na liscie ustawia najlepszy kanal."]
    popup(stdscr, "Wynik skanowania", lines, status="ok" if best else "warn")
    return results


def live_mcs_txt(live):
    """Czym nadaja w tej chwili procesy wfb_tx - jedna linia do naglowka.
    Numery gniazd wychodza na wierzch dopiero wtedy, gdy nie wszystkie nadaja
    tak samo; w normalnej sytuacji jest to jedna wartosc dla calego radia."""
    if not live:
        return "brak dzialajacego wfb_tx"
    by_mcs = {}
    for tx in live:
        by_mcs.setdefault(str(tx.get("mcs", "?")), []).append(str(tx.get("port", "?")))
    if len(by_mcs) == 1:
        return f"MCS {next(iter(by_mcs))}"
    return ", ".join(f"MCS {mcs} (porty {', '.join(ports)})" for mcs, ports in by_mcs.items())


def modulation_screen(stdscr):
    """Wybor modulacji nadawania. "Automatycznie" nie ustawia niczego - zostaje
    to, co wybiera sam wfb-ng, czyli tak jak po swiezej instalacji. Wyzszy MCS
    to wiecej Mbit/s, ale potrzeba mocniejszego sygnalu, wiec zasieg krotszy.

    MCS nie musi byc taki sam po obu stronach: odbiornik odczytuje modulacje
    z naglowka kazdej ramki. Zgadzac musza sie kanal i szerokosc pasma, a tych
    ten ekran nie rusza.

    Ustawienie idzie do configu i wymaga restartu uslugi, wiec po zapisie
    odczytujemy z powrotem, czym FAKTYCZNIE nadaje wfb_tx - gdyby wpis nie
    zadzialal, widac to od razu, zamiast dowiadywac sie o tym w powietrzu."""
    sections = mcs_config_sections()
    options = [None] + sorted(MCS_TABLE)
    saved = current_mcs_setting(sections)
    live = tx_radio_params()
    idx = options.index(saved) if saved in options else 0
    note = None

    while True:
        stdscr.clear()
        draw_header(stdscr, f"WFB-NG [{ROLE}] - wybor modulacji (MCS)")

        safe_addstr(stdscr, 2, 2, f"Teraz nadajemy:  {live_mcs_txt(live)}", curses.A_BOLD)
        safe_addstr(stdscr, 3, 2, "W configu:       " +
                    ("automatycznie (brak wpisu)" if saved is None else f"MCS {saved}"))
        safe_addstr(stdscr, 4, 2, "Sekcje:          " +
                    ", ".join(sorted(set(sections.values()))))

        for i, opt in enumerate(options):
            if opt is None:
                text = "Automatycznie - zostawia to, co ustawia wfb-ng"
            else:
                desc, rate = mcs_info(opt)
                text = f"{desc:<24}{rate:>6.1f} Mbit/s"
                hint = MCS_HINTS.get(opt)
                if hint:
                    text += f"   {hint}"
            mark = "* " if opt == saved else "  "
            safe_addstr(stdscr, 6 + i, 2, (mark + text).ljust(70),
                        curses.color_pair(5) if i == idx else 0)

        row = 6 + len(options) + 1
        safe_addstr(stdscr, row, 2, "(* = zapisane w configu; predkosc PHY dla 20 MHz "
                                    "i dlugiego GI, bez narzutu FEC)", curses.A_DIM)
        safe_addstr(stdscr, row + 1, 2, "Modulacja nie musi byc taka sama po obu stronach "
                                        "- kanal i szerokosc juz tak.", curses.A_DIM)
        if note:
            safe_addstr(stdscr, row + 3, 2, note[0][:110], color_for(note[1]) | curses.A_BOLD)

        h, _ = stdscr.getmaxyx()
        safe_addstr(stdscr, h - 1, 2, "Strzalki gora/dol, Enter = ustaw i zrestartuj usluge, "
                                      "q = powrot", curses.A_DIM)
        stdscr.refresh()

        key = stdscr.getch()
        if key in (curses.KEY_UP, ord("k")):
            idx = (idx - 1) % len(options)
        elif key in (curses.KEY_DOWN, ord("j")):
            idx = (idx + 1) % len(options)
        elif key in (ord("q"), ord("Q"), 27):
            return
        elif key in (10, 13, curses.KEY_ENTER):
            choice = options[idx]
            what = "automatycznie (usuwamy wpis z configu)" if choice is None else mcs_info(choice)[0]
            if popup(stdscr, "Zmiana modulacji",
                     [f"Ustawic: {what}?",
                      "",
                      "Sekcje: " + ", ".join(sorted(set(sections.values()))),
                      f"Usluga wifibroadcast@{ROLE} zostanie zrestartowana,",
                      "wiec na kilka sekund znikna obraz i telemetria.",
                      "",
                      "Druga strona NIE musi miec tej samej modulacji."],
                     buttons=("Tak", "Nie")) != 0:
                continue

            if not CFG_PATH.exists():
                note = (f"Brak {CFG_PATH} - nie ma gdzie tego zapisac.", "fail")
                continue

            apply_mcs_setting(choice, sections)
            run(["systemctl", "restart", f"wifibroadcast@{ROLE}"])
            time.sleep(3)

            saved = current_mcs_setting(sections)
            _tx_params_cache["val"] = None  # po restarcie to nowe procesy
            live = tx_radio_params()
            idx = options.index(saved) if saved in options else 0

            got = {str(tx.get("mcs")) for tx in live}
            if not service_active():
                lines = [f"Usluga nie wstala (status: {service_state_txt()}).",
                         "Ostatnie linie journala:"] + [ln[:70] for ln in service_last_errors(4)]
                popup(stdscr, "Zapisano, ale usluga nie dziala", lines, status="fail")
            elif choice is not None and got and got != {str(choice)}:
                popup(stdscr, "Zapisano, ale wfb_tx nadaje inaczej",
                      [f"Chcielismy MCS {choice}, a wfb_tx uzywa: {live_mcs_txt(live)}.",
                       "Ta wersja wfb-ng moze czytac mcs_index z innej sekcji -",
                       f"zajrzyj do {CFG_PATH} i porownaj z master.cfg."],
                      status="warn")
            else:
                popup(stdscr, "Ustawione", [f"Nadajemy teraz: {live_mcs_txt(live)}"], status="ok")
            note = None


def restart_wfb_service(wait=3.0):
    """Restart uslugi + skasowanie cache parametrow nadawania. Po restarcie
    biegna nowe procesy wfb_tx, wiec stary odczyt z /proc opisywalby juz
    nieistniejace ustawienia."""
    code, _out = run(["systemctl", "restart", f"wifibroadcast@{ROLE}"])
    time.sleep(wait)
    _tx_params_cache["val"] = None
    return code == 0


def _write_fec_choice(choice, section):
    """Zapis wyboru do configu. 'choice' to numer poziomu albo None, czyli
    "usun nasz wpis i zostaw to, co ustawia wfb-ng"."""
    if choice is None:
        return apply_fec_setting(None, None, section)
    k, n, _name = FEC_LEVELS[choice]
    return apply_fec_setting(k, n, section)


def fec_choice_txt(choice):
    if choice is None:
        return "ustawienie wfb-ng (bez naszego wpisu)"
    return fec_level_txt(choice)


def apply_fec_choice(choice, section=None):
    """Ustawia naprawe (numer poziomu albo None = usun wpis) i restartuje
    usluge. Zwraca (ok, tekst).

    Przy niepowodzeniu WYCOFUJE sie do poprzedniego ustawienia i restartuje
    jeszcze raz. Bez tego jedna wartosc, ktorej ta wersja wfb-ng nie przyjmuje,
    zostawialaby martwa usluge - a na dronie oznacza to link do odzyskania
    dopiero na ziemi. Wycofanie jest do surowej pary (k, n), a nie do numeru
    poziomu, bo w configu moglo siedziec cos spoza drabinki."""
    section = section or fec_section()
    if not CFG_PATH.exists():
        return False, f"brak {CFG_PATH} - nie ma gdzie tego zapisac"

    previous = current_fec_setting(section)
    if _write_fec_choice(choice, section) is None and choice is not None:
        return False, f"nie udalo sie zapisac fec_k/fec_n w [{section}]"
    restart_wfb_service()

    problem = None
    if not service_active():
        problem = f"usluga nie wstala ({service_state_txt()})"
    elif choice is not None:
        k, n, _name = FEC_LEVELS[choice]
        live = live_tunnel_fec()
        if live and live != (k, n):
            problem = f"zapisalem {k}/{n}, a wfb_tx nadaje {live[0]}/{live[1]}"

    if problem is None:
        if choice is None:
            live = live_tunnel_fec()
            return True, ("tunel nadaje z ustawieniem wfb-ng"
                          + (f": FEC {live[0]}/{live[1]}" if live else ""))
        return True, f"tunel nadaje z {fec_level_txt(choice)}"

    # --- wycofanie ---
    if previous is None:
        apply_fec_setting(None, None, section)
        back = "ustawienia wfb-ng"
    else:
        apply_fec_setting(previous[0], previous[1], section)
        back = f"FEC {previous[0]}/{previous[1]}"
    restart_wfb_service()
    if service_active():
        return False, f"{problem} - wycofalem sie do {back}"
    return False, f"{problem}; wrocilem do {back}, ale usluga NADAL nie wstala"


def fec_status_lines(metrics, saved_total=None):
    """Wspolne wiersze o naprawie dla ekranu recznego i automatycznego:
    ile gubi samo radio, ile z tego wraca dzieki FEC i co zostaje."""
    before, after = metrics.get("loss_before"), metrics.get("loss")
    if before is None:
        return [("brak danych - nic jeszcze nie przyszlo", "warn")]
    saved = metrics.get("saved_pct") or 0.0
    st = loss_grade(after)[0]
    lines = [
        (f"gubi samo radio:   {before:>6.2f}%   {meter(before, 10, 0)}",
         loss_grade(before)[0]),
        (f"naprawione (FEC):  {saved:>6.2f} pkt proc.   {metrics['fec']:.0f} pkt/s "
         "wrocilo z nadmiarowosci", "ok" if saved > 0 else None),
        (f"zostaje utracone:  {after:>6.2f}%   {meter(after, 10, 0)}   "
         f"{metrics['lost']:.0f} pkt/s", st),
    ]
    if saved_total is not None:
        lines.append((f"od poczatku: naprawa zbila straty o {saved_total:.2f} "
                      "punktu procentowego", None))
    return lines


def auto_repair_screen(stdscr, level, section):
    """Tryb automatyczny naprawy: AutoFec dobiera nadmiarowosc sam.

    Tak jak przy kanale - zeby dzialalo jak nalezy, ekran powinien byc otwarty
    PO OBU STRONACH. Kazda strona ustawia wtedy swoje nadawanie pod raport tej
    drugiej, czyli pod to, co naprawde do niej dociera. Przy jednej stronie
    automat tez dziala, tylko ocenia po wlasnym odbiorze."""
    if popup(stdscr, "Automatyczna naprawa pakietow",
             ["Sam dobiera nadmiarowosc FEC w tunelu.",
              "",
              f"Dokłada, gdy mimo naprawy tracimy ponad {AUTO_FEC_BAD_LOSS:.1f}%",
              f"przez {AUTO_FEC_BAD_SECONDS:.0f} s. Zdejmuje po "
              f"{AUTO_FEC_GOOD_SECONDS:.0f} s czystego linku.",
              "",
              "KAZDA zmiana restartuje usluge, czyli na kilka sekund",
              f"znika obraz i telemetria (nie czesciej niz co {AUTO_FEC_COOLDOWN:.0f} s).",
              "",
              "Najlepiej wlaczyc po obu stronach - wtedy kazda ustawia",
              "sie pod to, co druga naprawde odbiera."],
             buttons=("Start", "Anuluj")) != 0:
        return level

    nics = wfb_nics()
    peer = AutoPeer().start()
    stats = WfbStatsProbe().start()
    auto = AutoFec(level, now=time.monotonic())
    run_totals = RunTotals()
    events = []
    started = time.monotonic()

    def note(text, status=None):
        events.insert(0, (time.strftime("%H:%M:%S"), text, status))
        del events[10:]

    if peer.error:
        note(peer.error, "fail")
    if level is None:
        note("start bez poziomu - FEC w configu jest spoza drabinki", "warn")
    else:
        note(f"start na poziomie: {fec_level_txt(level)}")

    stdscr.timeout(500)
    try:
        while True:
            now = time.monotonic()
            metrics = link_metrics(stats.snapshot()[0], nics)
            run_totals.update(metrics)

            for action in auto.tick(now, metrics["loss"], metrics["loss_before"],
                                    peer.take()):
                kind = action[0]
                if kind == "send":
                    peer.send(action[1])
                elif kind == "note":
                    note(action[1], "warn")
                elif kind == "fec":
                    new_level = action[1]
                    note(f"naprawa -> {fec_level_txt(new_level)}: {action[2]}")
                    ok, txt = apply_fec_choice(new_level, section)
                    note(txt, "ok" if ok else "fail")

            stdscr.erase()
            draw_header(stdscr, f"WFB-NG [{ROLE}] - automatyczna naprawa pakietow")

            live = live_tunnel_fec()
            safe_addstr(stdscr, 2, 2, "Nadajemy tunel z: "
                        + (f"FEC {live[0]}/{live[1]}" if live else "?")
                        + (f"   (poziom {auto.level + 1}/{len(FEC_LEVELS)}: "
                           f"{FEC_LEVELS[auto.level][2]})" if auto.level is not None else ""),
                        curses.A_BOLD)
            safe_addstr(stdscr, 3, 2, f"Zmian: {auto.changes}   czas: "
                        f"{int(now - started) // 60:02d}:{int(now - started) % 60:02d}"
                        f"   ocena wg: {auto.source or '-'}")

            ago = peer.peer_seen_ago()
            safe_addstr(stdscr, 4, 2,
                        f"Druga strona ({PEER_NAME} {PEER_IP}): "
                        + (f"raportuje {ago:.0f} s temu" if ago is not None
                           else "milczy - oceniam po wlasnym odbiorze"),
                        color_for("ok" if ago is not None and ago < AUTO_FEC_PEER_STALE
                                  else "warn"))
            if auto.peer_loss and auto.peer_fresh(now):
                safe_addstr(stdscr, 5, 2, f"Ona gubi OD NAS: {auto.peer_loss[0]:.2f}% "
                            f"po naprawie, {auto.peer_loss[1]:.2f}% przed naprawa")

            safe_addstr(stdscr, 7, 2, "Nasz odbior od drugiej strony:", curses.A_BOLD)
            for i, (text, status) in enumerate(fec_status_lines(metrics,
                                                                run_totals.saved_pct)):
                safe_addstr(stdscr, 8 + i, 4, text, color_for(status))

            safe_addstr(stdscr, 13, 2, "Zdarzenia:", curses.A_BOLD)
            for i, (stamp, text, status) in enumerate(events):
                safe_addstr(stdscr, 14 + i, 4, f"{stamp}  {text}", color_for(status))

            h, _ = stdscr.getmaxyx()
            safe_addstr(stdscr, h - 1, 2, "q = wyjscie (zostaje poziom, na ktorym "
                                          "jestesmy)", curses.A_DIM)
            stdscr.refresh()

            if stdscr.getch() in (ord("q"), ord("Q"), 27):
                break
    finally:
        peer.close()
        stats.close()
        stdscr.timeout(-1)

    return auto.level


def repair_screen(stdscr):
    """Naprawa pakietow utraconych w tunelu wfb-ng.

    Pakietu, ktory przepadl, nikt nie odtworzy po fakcie - dlatego wfb-ng
    zabezpiecza sie z gory: do kazdych k pakietow danych dokłada n-k
    nadmiarowych i z dowolnych k odebranych sklada cala paczke z powrotem.
    Ten ekran ustawia wlasnie to k/n dla tunelu i pokazuje, ile pakietow dzieki
    temu wraca (straty przed naprawa kontra po naprawie).

    Wpis idzie do sekcji tunelu w configu i wymaga restartu uslugi, wiec po
    zapisie sprawdzamy, czym wfb_tx nadaje NAPRAWDE - gdyby wpis nie zadzialal,
    widac to od razu.

    Ustawienie dotyczy tylko NASZEGO kierunku i nie musi byc takie samo po obu
    stronach: odbiornik czyta k/n z pakietu sesyjnego.

    Naprawe da sie wylaczyc na dwa sposoby i to sa DWIE ROZNE rzeczy:
    - poziom "wylaczona" (n = k) wpisuje do configu zero nadmiarowosci, czyli
      swiadomie nadajemy bez ochrony;
    - "zostaw ustawienie wfb-ng" kasuje nasz wpis, wiec wraca to, co wfb-ng
      ustawia samo (dla tunelu 1/2) - to jest wycofanie sie z ustawiania,
      a nie wylaczenie naprawy."""
    section = fec_section()
    saved = current_fec_setting(section)
    level = fec_level_of(*saved) if saved else None
    # None na poczatku listy to "bez naszego wpisu"; reszta to numery poziomow
    options = [None] + list(range(len(FEC_LEVELS)))
    if not saved:
        idx = options.index(None)          # nie mamy wpisu - kursor na tej pozycji
    elif level is not None:
        idx = options.index(level)         # wpis z drabinki
    else:
        idx = options.index(FEC_DEFAULT_LEVEL)  # wpis spoza drabinki - nie ma co zaznaczyc
    stats = WfbStatsProbe().start()
    nics = wfb_nics()
    note = None

    stdscr.timeout(500)
    try:
        while True:
            metrics = link_metrics(stats.snapshot()[0], nics)
            live = live_tunnel_fec()

            stdscr.erase()
            draw_header(stdscr, f"WFB-NG [{ROLE}] - naprawa utraconych pakietow (tunel)")

            safe_addstr(stdscr, 2, 2, "Tunel nadaje teraz:  "
                        + (f"FEC {live[0]}/{live[1]}   "
                           f"{fec_overhead(live[0], live[1]):.2f}x pakietow" if live
                           else "? (usluga nie chodzi albo brak procesu wfb_tx)"),
                        curses.A_BOLD)
            safe_addstr(stdscr, 3, 2, "W configu:           "
                        + (f"fec_k = {saved[0]}, fec_n = {saved[1]}" if saved
                           else "brak wpisu - zostaje ustawienie wfb-ng")
                        + f"   [{section}]")

            for i, (text, status) in enumerate(fec_status_lines(metrics)):
                safe_addstr(stdscr, 5 + i, 4, text, color_for(status))

            # Lista ma 9 pozycji, a nad nia stoja jeszcze trzy wiersze stanu -
            # na 24-wierszowym terminalu wychodzi co do wiersza, wiec pozycje sa
            # liczone od zmiennej, a nie wpisane na sztywno.
            top = 10
            safe_addstr(stdscr, top - 1, 2, "Ile nadmiarowosci nadawac:", curses.A_BOLD)
            for i, opt in enumerate(options):
                if opt is None:
                    text = "bez wpisu   zostaw ustawienie wfb-ng (kasuje nasz wpis)"
                    chosen = saved is None
                else:
                    k, n, name = FEC_LEVELS[opt]
                    text = (f"FEC {k}/{n:<3}{name:<18}{fec_overhead(k, n):.2f}x pakietow"
                            + ("   nic nie wroci" if fec_off(opt)
                               else f"   przezyje utrate {n - k} z {n}"))
                    chosen = level == opt
                mark = "* " if chosen else "  "
                safe_addstr(stdscr, top + i, 2, (mark + text).ljust(76),
                            curses.color_pair(5) if i == idx else 0)

            row = top + len(options) + 1
            safe_addstr(stdscr, row, 2, "(* = w configu; wiecej nadmiarowosci = mniej "
                                        "strat, ale wiecej pasma)", curses.A_DIM)
            safe_addstr(stdscr, row + 1, 2, "Nie musi byc takie samo po obu stronach.",
                        curses.A_DIM)
            if note:
                safe_addstr(stdscr, row + 2, 2, note[0][:74],
                            color_for(note[1]) | curses.A_BOLD)

            h, _ = stdscr.getmaxyx()
            safe_addstr(stdscr, h - 1, 2, "Strzalki, Enter = ustaw, a = automat, "
                                          "d = domyslne, w = wylacz, q = powrot",
                        curses.A_DIM)
            stdscr.refresh()

            key = stdscr.getch()
            if key == -1:
                continue  # timeout - tylko odswiezenie liczb
            if key in (curses.KEY_UP, ord("k")):
                idx = (idx - 1) % len(options)
            elif key in (curses.KEY_DOWN, ord("j")):
                idx = (idx + 1) % len(options)
            elif key in (ord("q"), ord("Q"), 27):
                return
            elif key in (ord("d"), ord("D")):
                idx = options.index(FEC_DEFAULT_LEVEL)
            elif key in (ord("w"), ord("W")):
                idx = options.index(FEC_OFF_LEVEL)
            elif key in (ord("a"), ord("A")):
                stdscr.timeout(-1)
                # Automat musi od czegos zaczac. Gdy nie mamy wpisu w configu,
                # bierzemy poziom z tego, czym wfb_tx NADAJE w tej chwili.
                start = level if level is not None else fec_level_of(*(live or (0, 0)))
                level = auto_repair_screen(stdscr, start, section)
                saved = current_fec_setting(section)
                if level is not None:
                    idx = options.index(level)
                stdscr.timeout(500)
                note = None
            elif key in (10, 13, curses.KEY_ENTER):
                stdscr.timeout(-1)
                choice = options[idx]
                if choice is None:
                    lines = ["Usunac nasz wpis fec_k/fec_n?",
                             "",
                             f"Sekcja: [{section}]",
                             "Zostanie to, co wfb-ng ustawia samo",
                             "(dla tunelu zwykle FEC 1/2).",
                             "",
                             "To NIE jest wylaczenie naprawy, tylko wycofanie",
                             "sie z jej ustawiania."]
                elif fec_off(choice):
                    k, n, _name = FEC_LEVELS[choice]
                    lines = ["WYLACZYC naprawe pakietow?",
                             "",
                             f"Sekcja: [{section}]",
                             f"fec_k = {k}, fec_n = {n} - zero nadmiarowosci.",
                             "",
                             "Kazdy pakiet zgubiony w powietrzu bedzie stracony",
                             "BEZPOWROTNIE - nie ma z czego go odtworzyc.",
                             "Na wykresie obie krzywe strat pokryja sie.",
                             "",
                             "Ma sens do pomiaru, ile gubi samo radio."]
                else:
                    k, n, _name = FEC_LEVELS[choice]
                    lines = [f"Ustawic {fec_level_txt(choice)}?",
                             "",
                             f"Sekcja: [{section}]",
                             f"Z kazdych {n} pakietow {k} niesie dane,"
                             f" {n - k} to nadmiarowosc."]
                lines += ["",
                          f"Usluga wifibroadcast@{ROLE} zostanie zrestartowana,",
                          "wiec na kilka sekund znikna obraz i telemetria."]

                title = ("Wylaczenie naprawy" if choice is not None and fec_off(choice)
                         else "Zmiana naprawy pakietow")
                if popup(stdscr, title, lines, buttons=("Tak", "Nie")) == 0:
                    ok, txt = apply_fec_choice(choice, section)
                    saved = current_fec_setting(section)
                    level = fec_level_of(*saved) if saved else None
                    if ok:
                        popup(stdscr, "Ustawione", [txt], status="ok")
                    else:
                        popup(stdscr, "Nie poszlo tak, jak mialo",
                              [txt, "", "Zajrzyj do " + str(CFG_PATH) + " i porownaj",
                               "z master.cfg - ta wersja wfb-ng moze czytac",
                               "fec_k/fec_n z innej sekcji albo nie przyjmowac",
                               "tej wartosci."], status="fail")
                    note = None
                stdscr.timeout(500)
    finally:
        stats.close()
        stdscr.timeout(-1)


def verification_screen(stdscr):
    stdscr.clear()
    draw_header(stdscr, f"WFB-NG [{ROLE}] - weryfikacja")
    safe_addstr(stdscr, 2, 2, "Sprawdzam...")
    stdscr.refresh()

    checks = collect_checks()

    # Kazdy check to dwa wiersze (nazwa + szczegol); przy dwoch kartach lista
    # nie miesci sie na 24-wierszowym terminalu, wiec przewijamy.
    lines = []
    for name, status, detail in checks:
        lines.append((status, name, True))
        lines.append((status, detail, False))

    top = 0
    while True:
        stdscr.clear()
        draw_header(stdscr, f"WFB-NG [{ROLE}] - weryfikacja")
        h, _ = stdscr.getmaxyx()
        view = max(1, h - 3)

        for i, (status, text, is_name) in enumerate(lines[top:top + view]):
            row = 2 + i
            if is_name:
                safe_addstr(stdscr, row, 2, STATUS_ICON[status], color_for(status) | curses.A_BOLD)
                safe_addstr(stdscr, row, 9, text, curses.A_BOLD)
            else:
                safe_addstr(stdscr, row, 11, text)

        if len(lines) > view:
            hint = f"Strzalki = przewijanie ({top + 1}-{min(top + view, len(lines))}/{len(lines)}), q = powrot"
        else:
            hint = "Nacisnij dowolny klawisz, aby wrocic..."
        safe_addstr(stdscr, h - 1, 2, hint, curses.A_DIM)
        stdscr.refresh()

        key = stdscr.getch()
        if key in (curses.KEY_DOWN, ord("j")) and top + view < len(lines):
            top += 1
        elif key in (curses.KEY_UP, ord("k")) and top > 0:
            top -= 1
        elif key == curses.KEY_NPAGE:
            top = min(max(0, len(lines) - view), top + view)
        elif key == curses.KEY_PPAGE:
            top = max(0, top - view)
        else:
            break


def main_menu(stdscr):
    curses.curs_set(0)
    if curses.has_colors():
        init_colors()

    items = [
        "Pokaz biezaca konfiguracje",
        "Wykryj karty ponownie (naprawa)",
        "Identyfikacja kart (wypnij dongla)",
        "Karty na zywo: chip, urzadzenie, rola RX/TX",
        "Klucze i parowanie",
        "Test polaczenia (sygnal, straty, ping)",
        "Test obciazeniowy (ruch jak wideo)",
        "Kanal i czestotliwosc (skan, tryb auto)",
        "Moc nadawania (TX)",
        "Wybor modulacji (MCS)",
        "Naprawa utraconych pakietow (FEC tunelu)",
        "Uruchom weryfikacje",
        "Wyjdz",
    ]
    idx = 0
    flash = None  # (tekst, do kiedy) - potwierdzenie postawionego znacznika

    while True:
        # erase(), a nie clear(): przy zapisie w tle menu odrysowuje sie samo co
        # sekunde, a pelne czyszczenie ekranu migalo by przy kazdym odswiezeniu
        stdscr.erase()
        draw_header(stdscr, f"WFB-NG [{ROLE.upper()}] - konfigurator i weryfikator")

        if not (DRONE_KEY.exists() and GS_KEY.exists()):
            safe_addstr(stdscr, 2, 2, "Brak kluczy - cos poszlo nie tak przy instalacji", color_for("fail"))

        nic_status, nic_txt = nic_status_summary()
        safe_addstr(stdscr, 3, 2, nic_txt, color_for(nic_status) | curses.A_BOLD)

        # Zapis testu chodzi w tle wlasnym procesem - bez tej linijki nie bylo
        # by po nim widac, ze cos jeszcze pisze do karty.
        state = test_state()
        if state:
            safe_addstr(stdscr, 4, 2, test_state_line(state), test_state_attr(state))

        for i, item in enumerate(items):
            attr = curses.color_pair(5) if i == idx else curses.A_NORMAL
            safe_addstr(stdscr, 5 + i, 4, item.ljust(50), attr)

        h, _ = stdscr.getmaxyx()
        hint = "Strzalki gora/dol, Enter = wybierz, r = odswiez, q = wyjscie"
        if state:
            hint += ", t = zapis w tle"
        if state and state["stan"] == "trwa":
            hint += ", m = znacznik"
        # To samo potwierdzenie co na ekranie testu: bez niego po nacisnieciu
        # 'm' przez sekunde nie wiadomo, czy znacznik gdzies poszedl - licznik
        # w linijce o zapisie dochodzi dopiero przy nastepnym odswiezeniu.
        if flash and time.monotonic() < flash[1]:
            safe_addstr(stdscr, h - 1, 2, flash[0], color_for("ok") | curses.A_BOLD)
        else:
            flash = None
            safe_addstr(stdscr, h - 1, 2, hint, curses.A_DIM)
        stdscr.refresh()

        # Przy zapisie w tle menu odswieza sie samo co sekunde, zeby licznik
        # probek i rozmiar pliku szly do przodu; bez niego czekamy na klawisz.
        stdscr.timeout(1000 if state and state["stan"] == "trwa" else -1)
        key = stdscr.getch()
        if key in (curses.KEY_UP, ord("k")):
            idx = (idx - 1) % len(items)
        elif key in (curses.KEY_DOWN, ord("j")):
            idx = (idx + 1) % len(items)
        elif key in (10, 13, curses.KEY_ENTER):
            if idx == 0:
                show_config_screen(stdscr)
            elif idx == 1:
                redetect_screen(stdscr)
            elif idx == 2:
                nic_identify_screen(stdscr)
            elif idx == 3:
                cards_live_screen(stdscr)
            elif idx == 4:
                keys_screen(stdscr)
            elif idx == 5:
                link_test_screen(stdscr)
            elif idx == 6:
                load_test_screen(stdscr)
            elif idx == 7:
                channel_screen(stdscr)
            elif idx == 8:
                tx_power_screen(stdscr)
            elif idx == 9:
                modulation_screen(stdscr)
            elif idx == 10:
                repair_screen(stdscr)
            elif idx == 11:
                verification_screen(stdscr)
            elif idx == 12:
                if confirm_exit(stdscr):
                    break
        elif key in (ord("r"), ord("R")):
            _nic_status_cache["val"] = None  # wpiety wlasnie dongiel bez czekania
        elif key in (ord("m"), ord("M")) and state and state["stan"] == "trwa":
            # Ten sam klawisz co na ekranie testu: zapis leci w tle, wiec
            # znacznik musi dac sie postawic takze stad, bez wchodzenia w test.
            mark_test_recorder()
            flash = (f"ZNACZNIK zapisany o {time.strftime('%H:%M:%S')}"
                     " - w podgladzie bedzie czerwona kreska",
                     time.monotonic() + 3.0)
        elif key in (ord("t"), ord("T")):
            stdscr.timeout(-1)  # okienko ma czekac na klawisz, nie na timeout
            background_test_popup(stdscr)
        elif key in (ord("q"), 27):
            if confirm_exit(stdscr):
                break


def load_test_screen(stdscr):
    """Test obciazeniowy: dron nadaje strumien udajacy wideo, gs go liczy.

    Musi byc otwarty PO OBU STRONACH - jedna generuje ruch, druga mierzy, co
    z niego doszlo. Sensowna kolejnosc to najpierw gs (zeby liczyl od zera),
    potem dron.

    Rozne strony pokazuja rozne liczby i tak ma byc: nadajnik wie tylko, ile
    wypchnal, a odbiornik - ile z tego wyszlo po naprawie FEC. Dopiero
    zestawienie obu daje odpowiedz, czy lacze udzwignie obraz."""
    nics = wfb_nics()
    # nie 'mbit' - ta nazwa nalezy do funkcji formatujacej przeplywnosc
    target_mbit = LOAD_DEFAULT_MBIT

    # O przeplywnosc pyta tylko nadajnik - odbiornik liczy to, co dojdzie.
    # Jedna wartosc nie wystarczy do niczego: dopiero przemiatanie od dolu
    # w gore pokazuje, przy jakim strumieniu lacze zaczyna gubic, a to jest
    # cala odpowiedz na pytanie "czy udzwignie obraz".
    if VIDEO_SENDS:
        stdscr.erase()
        draw_header(stdscr, f"WFB-NG [{ROLE}] - test obciazeniowy")
        safe_addstr(stdscr, 2, 2, "Przeplywnosc strumienia testowego.")
        safe_addstr(stdscr, 3, 2, "Zacznij nisko i podnos - szukamy progu, przy ktorym "
                                  "zaczynaja rosnac straty.", curses.A_DIM)
        while True:
            raw = prompt_line(stdscr, 5, "Mbit/s (0.5 - 40)", f"{LOAD_DEFAULT_MBIT:.1f}")
            try:
                target_mbit = float(raw.replace(",", "."))
            except ValueError:
                target_mbit = 0.0
            if 0.5 <= target_mbit <= 40.0:
                break
            safe_addstr(stdscr, 6, 2, "Podaj liczbe od 0.5 do 40.", color_for("fail"))

    if VIDEO_SENDS:
        prompt = [f"Ta strona ({ROLE}) BEDZIE NADAWAC strumien testowy",
                  f"{target_mbit:.1f} Mbit/s na UDP {VIDEO_UDP_PORT} - tam, gdzie",
                  "trafialby obraz z kamery.",
                  "",
                  f"Na drugiej stronie ({PEER_NAME}) otworz ten sam ekran -",
                  "to ona policzy, ile z tego doszlo.",
                  "",
                  "Ruch idzie tym samym portem radiowym i tym samym FEC,",
                  "co prawdziwe wideo, wiec wynik dotyczy wlasnie obrazu."]
    else:
        prompt = [f"Ta strona ({ROLE}) BEDZIE LICZYC strumien testowy",
                  f"odbierany na UDP {VIDEO_UDP_PORT}.",
                  "",
                  f"Ruch musi wygenerowac druga strona ({PEER_NAME}) -",
                  "otworz tam ten sam ekran.",
                  "",
                  "UWAGA: jesli na tym porcie slucha juz odtwarzacz obrazu,",
                  "zatrzymaj go - dwa programy nie odbiora tego samego strumienia."]

    if popup(stdscr, "Test obciazeniowy lacza", prompt, buttons=("Start", "Anuluj")) != 0:
        return

    sender = LoadSender(mbit=target_mbit).start() if VIDEO_SENDS else None
    receiver = None if VIDEO_SENDS else LoadReceiver().start()
    stats = WfbStatsProbe().start()
    run = RunTotals()
    started = time.monotonic()
    prev = {"t": started, "sent": 0, "got": 0, "bytes": 0}
    rate = {"pps": 0.0, "mbit": 0.0}

    stdscr.timeout(200)
    try:
        while True:
            now = time.monotonic()
            elapsed = now - started
            metrics = link_metrics(stats.snapshot()[0], nics)
            run.update(metrics)

            lines = []

            def row(text, status=None, indent=2):
                lines.append((" " * indent + text, color_for(status) if status else 0))

            if sender:
                sent, sbytes, late, err = sender.snapshot()
                if now - prev["t"] >= 0.5:
                    dt = now - prev["t"]
                    rate["pps"] = (sent - prev["sent"]) / dt
                    rate["mbit"] = (sbytes - prev["bytes"]) * 8.0 / dt / 1e6
                    prev.update(t=now, sent=sent, bytes=sbytes)
                lines.append((f"NADAJE strumien testowy   cel {target_mbit:.1f} Mbit/s",
                              curses.A_BOLD))
                row(f"wyslane {sent}   teraz {rate['pps']:.0f} pkt/s   "
                    f"{rate['mbit']:.2f} Mbit/s")
                if err:
                    row(err, "fail")
                elif late:
                    # Nie nadazamy z wypychaniem - wynik po drugiej stronie
                    # bedzie zanizony nie przez radio, tylko przez to Pi.
                    row(f"nie nadazam z tempem ({late}x) - zejdz z przeplywnoscia", "warn")

                # To rozstrzyga najwazniejsze pytanie tego testu: czy ramki,
                # ktorych nie ma po drugiej stronie, w ogole polecialy w eter.
                # Bez tego kazda strate zwalalo by sie na radio, a rownie dobrze
                # moze ich nigdy nie byc na antenie.
                tx_msgs = metrics["tx"]
                inj = sum(rx_packets(m, "injected")[0] for m in tx_msgs.values())
                drop = sum(rx_packets(m, "dropped")[0] for m in tx_msgs.values())
                lat = max((r[3] or 0.0 for m in tx_msgs.values()
                           for r in tx_wlan_rows(m, nics)), default=0.0)
                row(f"wfb_tx wstrzyknelo {inj:.0f} pkt/s   odrzucone {drop:.0f}/s"
                    + (f"   wstrzykiwanie {lat:.1f} ms" if lat else ""),
                    "fail" if drop else None)
                if drop:
                    row("odrzucone > 0 = to Pi nie nadaza wypchnac ramek w eter.",
                        "fail", indent=4)
                    row("Strat nie szukaj w radiu - zejdz z przeplywnoscia albo",
                        "fail", indent=4)
                    row("podnies MCS, zeby jedna ramka zajmowala mniej czasu.",
                        "fail", indent=4)
                row(f"wynik czytaj po drugiej stronie ({PEER_NAME})", indent=2)
            else:
                got, gbytes, lost, pct, reord, err = receiver.snapshot()
                if now - prev["t"] >= 0.5:
                    dt = now - prev["t"]
                    rate["pps"] = (got - prev["got"]) / dt
                    rate["mbit"] = (gbytes - prev["bytes"]) * 8.0 / dt / 1e6
                    prev.update(t=now, got=got, bytes=gbytes)
                st = loss_grade(pct)[0] if pct is not None else None
                lines.append((f"LICZE strumien testowy z {PEER_NAME}", curses.A_BOLD))
                if err:
                    row(err, "fail")
                elif not got:
                    row("nic jeszcze nie doszlo - czy druga strona ma otwarty ten ekran?",
                        "warn")
                else:
                    row(f"odebrane {got}   teraz {rate['pps']:.0f} pkt/s   "
                        f"{rate['mbit']:.2f} Mbit/s")
                    row(f"utracone {lost}"
                        + (f"   {pct:.2f}%" if pct is not None else "")
                        + (f"   przestawione {reord}" if reord else ""), st)
                    row("(dziury w numeracji = straty PO naprawie FEC, czyli to,")
                    row(" co zobaczylby dekoder obrazu)")

            lines.append(("", 0))
            lines.append(("Radio pod obciazeniem", curses.A_BOLD))
            if metrics["best_rssi"] is not None:
                rst, rtxt = rssi_grade(metrics["best_rssi"])
                row(f"sygnal {rtxt}  ({metrics['best_rssi']:.0f} dBm)", rst)
            if run.per is not None:
                row(f"PER {run.per:.2f}%   {run.totals['lost']:.0f} utraconych z "
                    f"{run.totals['rx'] + run.totals['lost']:.0f}", loss_grade(run.per)[0])
                if run.per_before is not None:
                    row(f"bez naprawy stracilibysmy {run.per_before:.2f}%", indent=4)
            row(f"odbior {metrics['rx_pps']:.0f} pkt/s   "
                f"{mbit(metrics['rx_bytes']):.2f} Mbit/s")
            row(f"czas: {int(elapsed) // 60:02d}:{int(elapsed) % 60:02d}")

            stdscr.erase()
            draw_header(stdscr, f"WFB-NG [{ROLE}] - test obciazeniowy")
            for i, (text, attr) in enumerate(lines):
                safe_addstr(stdscr, 2 + i, 2, text, attr)
            h, _ = stdscr.getmaxyx()
            safe_addstr(stdscr, h - 1, 2, "q = powrot, z = zeruj liczniki",
                        curses.A_DIM)
            stdscr.refresh()

            key = stdscr.getch()
            if key in (ord("q"), ord("Q"), 27):
                break
            if key in (ord("z"), ord("Z")):
                run.reset()
                if receiver:
                    receiver.reset()
                started = now
    finally:
        if sender:
            sender.close()
        if receiver:
            receiver.close()
        stats.close()


def confirm_exit(stdscr):
    """Wyjscie z programu nie zatrzymuje zapisu w tle - ale trzeba o tym
    powiedziec, inaczej latwo zostawic proces piszacy do skutku i przypomniec
    sobie o nim dopiero przy pelnej karcie."""
    stdscr.timeout(-1)
    state = test_state()
    if not state or state["stan"] != "trwa":
        return True

    choice = popup(stdscr, "Test nadal trwa w tle",
                   [f"Plik:     {state['plik']}",
                    f"Zapisane: {state['probek']} probek   {human_size(state['bajtow'])}"
                    f"   czas {fmt_mmss(state['czas'])}",
                    "",
                    "Zapis nie zalezy od tego programu - po wyjsciu leci dalej",
                    f"i sam stanie na {human_size(TEST_MAX_BYTES)}.",
                    "Zatrzymasz go, wchodzac tu ponownie i wybierajac 't'."],
                   buttons=("Zostaw w tle", "Przerwij zapis", "Anuluj"), status="warn")
    if choice == 2:
        return False
    if choice == 1:
        stop_test_popup(stdscr)
    return True


def main():
    # Tryb bez ekranu: sam zapis testu, odpalany przez ekran testu jako osobny
    # proces (patrz background_recorder). Nie ma tu ani setupu, ani menu.
    if len(sys.argv) >= 3 and sys.argv[1] == RECORDER_FLAG:
        require_root()
        sys.exit(background_recorder(Path(sys.argv[2])))

    # Tryb autostartu: odpala go systemd po kazdym boocie. Bez menu i bez
    # czekania na Enter - wszystko idzie do journala.
    if len(sys.argv) >= 2 and sys.argv[1] == AUTOSTART_FLAG:
        require_root()
        sys.exit(autostart_run())

    # Tryb wolany z reguly udev przy kazdym dodaniu/usunieciu karty <rola>_*
    # (patrz HOTPLUG_RULES) - ma byc szybki, wiec zadnego setupu
    # ani wykrywania sterownika, tylko WFB_NICS + restart.
    if len(sys.argv) >= 2 and sys.argv[1] == HOTPLUG_FLAG:
        require_root()
        sys.exit(hotplug_run())

    # PRZED require_root i przed autostartem: na cudzym Pi nie mamy tu nic do
    # roboty, a kazdy dalszy krok (install_autostart, setup, restart uslugi)
    # tylko robi szkode. Sama odmowa roota nie wymaga.
    if refuse_wrong_role():
        sys.exit(2)

    require_root()
    os.environ.setdefault("DEBIAN_FRONTEND", "noninteractive")

    # Autostart wpisujemy PRZED setupem: step_driver() potrafi zrestartowac Pi
    # w polowie instalacji i wtedy jednostka jest juz na miejscu. Kazde
    # uruchomienie z reki odswieza ten wpis, wiec wgranie skryptu w inne
    # miejsce naprawia sie samo i nie trzeba pamietac o systemd.
    ok, msg = install_autostart()
    log(("==> Autostart po reboocie: wlaczony, " if ok else "==> Autostart po reboocie NIE dziala: ") + msg)

    if not is_fully_installed():
        full_setup()
        # Jesli w trakcie instalacji byl restart, autostart odpalil sie na
        # niedokonczonym systemie i systemd zapamietal go jako failed. Setup
        # wlasnie sie skonczyl, wiec ten slad jest juz nieaktualny - bez tego
        # weryfikacja swiecilaby na zolto az do nastepnego bootu.
        run(["systemctl", "reset-failed", AUTOSTART_UNIT_NAME])

    detect_nics_startup()
    print()
    try:
        input("Nacisnij Enter, aby przejsc do konfiguratora/weryfikatora...")
    except EOFError:  # skrypt puszczony bez terminala (np. z potoku)
        pass

    curses.wrapper(main_menu)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        # Ctrl+C to normalne wyjscie, nie ma po co straszyc traceback'iem
        print("\nPrzerwane (Ctrl+C).")
        sys.exit(130)

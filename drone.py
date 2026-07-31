#!/usr/bin/env python3
"""WFB-NG - instalator + pseudo-graficzny (curses) TUI, rola: DRONE.

Pierwsze uruchomienie (na swiezym Raspberry Pi OS, z podlaczonymi kartami
RTL8812AU) robi caly setup: pakiety systemowe, sterownik karty, klucze
szyfrujace, /etc/wifibroadcast.cfg, usluge systemd. Kolejne uruchomienia
(setup juz gotowy) od razu otwieraja konfigurator/weryfikator.

Dron ma DWA dongle USB, gs jeden (EXPECTED_NICS). Wfb-ng uzywa wszystkich
kart zwroconych przez wfb-nics: odbiera z obu (dywersyfikacja - wygrywa ta
z lepszym sygnalem) i nadaje przez obie. Kazdy start sprawdza, czy obie
karty faktycznie sa widoczne, przepiete pod nasz sterownik i przepuszczaja
ruch.

Karty dostaja stale nazwy (NIC_NAMES: drone_RX, drone_TX) zamiast wlanX -
przypiete regula udev do MAC-a karty, wiec ta sama karta ma zawsze te sama
nazwe, niezaleznie od portu USB. To sa na razie tylko etykiety, a nie podzial
rol: obie karty i odbieraja, i nadaja.

Klucze szyfrujace sa wbudowane w oba skrypty (identyczne), wiec link wstaje
od razu, bez przenoszenia plikow. W menu jest parowanie: jedna strona pokazuje
8-znakowy kod, na drugiej sie go wpisuje i obie licza z niego te sama, prywatna
pare kluczy.

Pierwsze uruchomienie wpisuje skrypt do autostartu (wfb-drone-autostart.service),
wiec po kazdym reboocie powtarza sie to samo wykrywanie i te same naprawy kart -
bez wchodzenia na Pi. Weryfikacja pokazuje, czy ten autostart jest wlaczony.

Uzycie:
    sudo python3 drone.py              # setup + konfigurator/weryfikator
    sudo python3 drone.py --autostart  # tryb dla systemd: same naprawy, bez menu
"""

import ast
import base64
import curses
import hashlib
import io
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

ROLE = "drone"
PEER_IP = "10.5.0.1"  # adres drugiej strony (gs) w tunelu
PEER_NAME = "gs"
SSH_PORT = 22

EXPECTED_NICS = 2  # dron: dwa dongle USB (dywersyfikacja RX + nadawanie z obu)

DRIVER_TAG = "v5.2.20"
APT_RELEASE = "master"
# Link ma chodzic na 2.4 GHz. Kanal 13 (2472 MHz) - najwyzszy dozwolony w PL
# i w calym ETSI, zwykle mniej zatloczony niz standardowe 1/6/11. Region PL
# obejmuje 2400-2483, wiec kanaly 1-13 sa w nim legalne; pasma 5 GHz nie
# ruszamy (kanal 161 = 5805 MHz w PL w ogole nie istnieje i karta na nim nie
# nadaje). Zmiana obu wartosci jest w menu i MUSI byc taka sama na dronie i gs.
DEFAULT_CHANNEL = "13"
DEFAULT_REGION = "PL"
DEFAULT_TX_POWER = "63"  # 0-63, wg sterownika: 0 = wylaczone (EEPROM), 63 = max

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
REBOOT_MARKER = Path("/etc/.wfb-drone-reboot-attempted")

# Autostart: skrypt wpisuje sam siebie do systemd, zeby po KAZDYM restarcie Pi
# powtorzylo sie to, co robi uruchomienie z reki - przepiecie kart pod nasz
# sterownik, stale nazwy, rozdzial RX/TX i dopilnowanie, ze usluga faktycznie
# tych kart uzywa. Sama wifibroadcast@ tego nie robi, wiec bez tej jednostki
# link po zwyklym reboocie potrafi nie wstac, chociaz "usluga dziala".
AUTOSTART_UNIT_NAME = f"wfb-{ROLE}-autostart.service"
AUTOSTART_UNIT = Path("/etc/systemd/system") / AUTOSTART_UNIT_NAME
AUTOSTART_FLAG = "--autostart"

# Zamiast wlan1/wlan2 (numer zalezy od kolejnosci wykrycia i potrafi sie zamienic
# miedzy bootami) dajemy kartom stale, czytelne nazwy. Nazwa jest przypieta do
# MAC-a karty, wiec jedzie razem z donglem takze po przelozeniu go do innego
# portu USB - istotne, gdy do konkretnej karty przykrecony jest wzmacniacz.
# UWAGA: wfb-ng nadal odbiera z obu kart i przez obie nadaje - sama nazwa
# NIE dzieli rol, rozdzial trzeba wymusic w konfiguracji uslugi.
NIC_NAMES = ["drone_RX", "drone_TX"]

# Nazwy kart DRUGIEJ roli - po nich poznajemy, ze skrypt odpalono na cudzym Pi
# (patrz refuse_wrong_role). Nazwy sa przypiete do MAC-ow przez udev, wiec
# gs_wfb na maszynie znaczy "to Pi bylo urzadzane jako gs", a nie "ktos
# przypadkiem tak nazwal interfejs".
PEER_NIC_NAMES = ["gs_wfb"]

# Karty wylaczone z NADAWANIA. wfb-ng ma na to wartosc wifi_txpower = 'off'
# (w master.cfg opisana jako "special value for RX only cards"): taka karta
# jest inicjowana i odbiera, ale nie trafia na liste interfejsow wfb_tx.
# Potrzebne, gdy do jednej karty przykrecony jest JEDNOKIERUNKOWY wzmacniacz -
# nadawac ma wylacznie ona, a druga ma tylko sluchac. Bez tego wfb_tx rozklada
# pakiety miedzy obie karty (mirror jest domyslnie wylaczony), wiec czesc
# wideo wychodzilaby torem bez wzmacniacza.
RX_ONLY_NICS = ["drone_RX"]
UDEV_NAMES = Path("/etc/udev/rules.d/70-wfb-names.rules")
WFB_DEFAULTS = Path("/etc/default/wifibroadcast")

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

    txt = (f"Karty: {len(nics)}/{EXPECTED_NICS}"
           f"{' [' + ' '.join(nics) + ']' if nics else ''}"
           f"   USB: {dongles}/{EXPECTED_NICS}"
           f"   w usludze: {len(used)}/{len(nics)}")

    if len(nics) < EXPECTED_NICS:
        status = "fail"
        txt += "   <- BRAK KARTY" + (", dongiel wisi na innym sterowniku" if dongles > len(nics) else "")
    elif not service_active(props):
        # Karty moga byc idealne, a i tak 0/2 - bo usluga w ogole nie wstala.
        # Radzenie "zrestartuj usluge" byloby wtedy myleniem tropu.
        status = "fail"
        txt += f"   <- USLUGA NIE DZIALA ({service_state_txt(props)})"
    elif len(used) < len(nics):
        status = "warn"
        txt += "   <- zrestartuj usluge"
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


def txpower_cfg_value(nics):
    """Tresc wpisu wifi_txpower dla sekcji [common] albo None, gdy nie ma czego
    rozdzielac. 'off' = karta tylko do odbioru, None = moc wedlug sterownika
    (ustawiamy ja parametrem modulu, a nie tutaj - patrz TX_POWER_SYSFS)."""
    rx_only = [n for n in RX_ONLY_NICS if n in nics]
    if not rx_only or len(nics) < 2 or len(rx_only) >= len(nics):
        return None  # jedna karta albo same rx-only: nie bylo by czym nadawac
    entries = ", ".join(f"'{n}': " + ("'off'" if n in rx_only else "None")
                        for n in sorted(nics))
    return "{" + entries + "}"


def ensure_tx_split(nics):
    """Wymusza rozdzial rol kart: nadaje tylko karta spoza RX_ONLY_NICS.
    Zwraca True, gdy config zostal zmieniony - wolajacy restartuje usluge
    i sprawdza, czy wstala (patrz apply_tx_split)."""
    if not CFG_PATH.exists():
        return False
    want = txpower_cfg_value(nics)
    current = get_cfg_option("common", "wifi_txpower")

    if want is None:
        # Padla karta nadawcza i zostala sama rx-only: wpis 'off' odebralby
        # dronowi nadawanie W OGOLE. Kasujemy go - lepiej nadawac torem bez
        # wzmacniacza niz nie nadawac wcale. Ruszamy tylko wpis w formie
        # slownika, czyli ten, ktory sami piszemy.
        if (RX_ONLY_NICS and nics and current
                and current.startswith("{") and "'off'" in current):
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

    rx_only = ", ".join(n for n in RX_ONLY_NICS if n in nics)
    say(f"config: {rx_only} tylko do odbioru (wifi_txpower = 'off')", "warn")
    run(["systemctl", "restart", f"wifibroadcast@{ROLE}"])
    time.sleep(3)
    if service_active():
        return True

    drop_cfg_option("common", "wifi_txpower")
    run(["systemctl", "restart", f"wifibroadcast@{ROLE}"])
    time.sleep(3)
    say("ta wersja wfb-ng nie przyjela rozdzialu RX/TX - wycofano zmiane", "fail")
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
        f"options 88XXau_wfb rtw_tx_pwr_idx_override={tx_power}\n"
    )


def apply_tx_power_live(tx_power):
    """0-63: wymusza moc nadawania natychmiast, bez przeladowania modulu.
    Parametr modulu 88XXau_wfb jest zapisywalny na zywo przez sysfs."""
    if not TX_POWER_SYSFS.exists():
        return False
    try:
        TX_POWER_SYSFS.write_text(str(tx_power))
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
# + slownik msgpack, komplet raz na sekunde. Port stoi w configu wfb-ng,
# 8003 to wartosc domyslna.
WFB_CLI_PORT_DEFAULT = 8003
_cli_port_cache = {"val": None}


def wfb_cli_port():
    if _cli_port_cache["val"]:
        return _cli_port_cache["val"]
    port = WFB_CLI_PORT_DEFAULT
    code, out = run(["python3", "-c",
                     "from wfb_ng.conf import settings; print(settings.common.cli_port)"],
                    timeout=30)
    if code == 0:
        # run() sklei stdout ze stderr, wiec bierzemy ostatnia linie bedaca
        # sama liczba - ewentualne ostrzezenia importu nie podmienia portu
        for ln in reversed(out.splitlines()):
            if ln.strip().isdigit():
                port = int(ln.strip())
                break
    _cli_port_cache["val"] = port
    return port


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


# Progi z praktyki dla 8812AU: powyzej -50 dBm karty sa praktycznie obok
# siebie, ponizej -75 dBm zaczynaja sie zrywy obrazu.
def rssi_grade(rssi):
    if rssi is None:
        return None, "?"
    if rssi >= -50:
        return "ok", "doskonaly"
    if rssi >= -65:
        return "ok", "dobry"
    if rssi >= -75:
        return "warn", "slaby"
    return "fail", "na granicy zasiegu"


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
        run(["rm", "-rf", src_dir])
        log(f"    Klonuje sterownik ({DRIVER_TAG})...")
        code, out = run(
            ["git", "clone", "-b", DRIVER_TAG, "--depth", "1",
             "https://github.com/svpcom/rtl8812au.git", src_dir],
            timeout=120,
        )
        if code != 0:
            log("    BLAD klonowania sterownika:")
            log(out)
            return

        # Raspberry Pi OS (trixie+) dzieli naglowki jadra na common+wariant.
        # dkms.conf tego sterownika nie ustawia KBUILD_OUTPUT, wiec jego
        # Makefile przekazuje "O=''" do sub-make, co kasuje KBUILD_OUTPUT
        # wariantu i psuje build (blad: "auto.conf: No such file or
        # directory"). Wymuszamy poprawna wartosc.
        dkms_conf = Path(src_dir) / "dkms.conf"
        content = dkms_conf.read_text().replace(
            'KSRC=/lib/modules/${kernelver}/build"',
            'KSRC=/lib/modules/${kernelver}/build KBUILD_OUTPUT=/usr/src/linux-headers-${kernelver}"',
        )
        dkms_conf.write_text(content)

        # dkms-install.sh robi "cp -r $(pwd) /usr/src/rtl8812au-5.2.20.2" -
        # jesli ten katalog juz istnieje (np. po wczesniejszej nieudanej
        # probie), cp wklei tam nowe zrodla jako PODFOLDER zamiast nadpisac,
        # wiec dkms i tak przeczyta stary dkms.conf bez powyzszej poprawki.
        run(["rm", "-rf", "/usr/src/rtl8812au-5.2.20.2"])

        log("    Buduje modul (dkms) - to moze potrwac kilka minut...")
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


def plan_nic_names(nics):
    """Przydziela nazwy kartom. Raz ustalone przypisanie karta->nazwa zostaje
    (lezy w regulach udev), nowa karta dostaje pierwsza wolna nazwe. Dzieki
    temu przy jednej wypietej karcie druga NIE przejmuje jej nazwy - inaczej po
    kazdym przepieciu dongla nazwy mowilyby co innego niz poprzednio.
    Zwraca (mapa kotwica->nazwa, mapa interfejs->nazwa)."""
    by_anchor = parse_name_rules()
    anchors = nic_anchors(nics)
    slots = {nic: nic_usb_slot(nic) for nic in nics}
    live = {a for a in anchors.values() if a}

    # Przejscie ze starych regul (na gniazdo) na nowe (na MAC): karta, ktora ma
    # juz nazwe z gniazda, zabiera ja ze soba na swoj MAC. Bez tego pierwsze
    # uruchomienie nowej wersji przetasowalo by nazwy.
    for nic, anchor in anchors.items():
        old = ("slot", slots[nic])
        if anchor and anchor[0] == "mac" and anchor not in by_anchor and old in by_anchor:
            by_anchor[anchor] = by_anchor.pop(old)

    # Nieobecna karta nie moze w nieskonczonosc trzymac nazwy - inaczej po
    # wymianie dongla nowy zostawalby przy wlanX. Ale zwalniamy ja TYLKO gdy
    # jest jakas karta bez nazwy, czyli jest komu te nazwe oddac: sam chwilowy
    # brak dongla (zly kabel, port nie wstal po boocie) niczego nie przestawia
    # i po ponownym wpieciu karta wraca do swojej nazwy.
    if any(a not in by_anchor for a in live):
        for anchor in [a for a in by_anchor if a not in live]:
            del by_anchor[anchor]

    free = [n for n in NIC_NAMES if n not in by_anchor.values()]
    per_nic = {}
    for nic in sorted(nics, key=lambda n: (slots[n], n)):
        anchor = anchors[nic]
        if not anchor:
            continue  # nie ma czego zakotwiczyc w regule
        if anchor not in by_anchor:
            if not free:
                continue  # wiecej kart niz nazw - reszta zostaje przy wlanX
            by_anchor[anchor] = free.pop(0)
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
    """Nadaje kartom stale nazwy z NIC_NAMES zamiast wlanX. Zmiana nazwy nie
    powiedzie sie na pracujacym interfejsie, wiec na czas operacji zatrzymujemy
    usluge. Gdyby po zmianie wfb-nics przestalo widziec karty (jakas wersja
    szukajaca ich po nazwie "wlan*"), wycofujemy wszystko - dzialajace lacze
    jest wazniejsze niz ladna nazwa. Zwraca aktualna liste interfejsow."""
    nics = wfb_nics()
    if not nics:
        return nics

    by_anchor, per_nic = plan_nic_names(nics)
    write_name_rules(by_anchor)  # zeby przetrwalo reboot i ponowne wpiecie dongla
    todo = [(nic, name) for nic, name in per_nic.items() if nic != name]
    if not todo:
        return nics

    was_active = run(["systemctl", "is-active", "--quiet", f"wifibroadcast@{ROLE}"])[0] == 0
    if was_active:
        run(["systemctl", "stop", f"wifibroadcast@{ROLE}"])

    done = []
    for old, name in todo:
        ok, err = rename_nic(old, name)
        if ok:
            log(f"    nazwa karty: {old} -> {name}")
            done.append((old, name))
        else:
            log(f"    nie udalo sie przemianowac {old} na {name}: {err}")

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
    return nics


def step_config():
    log("==> [7/7] /etc/wifibroadcast.cfg i usluga")

    # Zawsze odswiezamy blackliste - niezaleznie od tego czy config juz byl,
    # bo nowsze jadra (6.x) maja WBUDOWANY sterownik rtw88_8812au, ktory
    # przechwytuje karte przy kazdym boocie zanim doda sie 88XXau_wfb.
    # Moc nadawania: zachowujemy juz ustawiona wartosc, a jesli jeszcze
    # jej nie bylo - domyslnie MAX (63/63), bo uzytkownik ma pozwolenie
    # radiowe i moc nie jest tu ograniczeniem.
    tx_power = parse_tx_power()
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
    """Interfejsy nalezace do DRUGIEJ roli - ale tylko wtedy, gdy zadnego
    naszego tu nie ma. Gdy sa obie nazwy naraz, nie orzekamy niczego: to stan
    po recznym grzebaniu i lepiej puscic uzytkownika dalej, niz zablokowac mu
    jedyne narzedzie do posprzatania."""
    try:
        present = {p.name for p in Path("/sys/class/net").iterdir()}
    except OSError:
        return []
    if any(n in present for n in NIC_NAMES):
        return []
    return [n for n in PEER_NIC_NAMES if n in present]


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
    log(f"==> Wykrywanie kart RTL88xx (oczekiwano: {EXPECTED_NICS})")

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

    for nic in nics:
        d = nic_details(nic)
        log(f"    {nic}: {d['driver']} mac={d['mac']} usb={d['usb']} tryb={d['mode']} kanal={d['channel']}")

    if not nics:
        log("    BLAD: zadna karta nie jest podpieta pod sterownik wfb.")
        log("    Sprawdz: lsusb | grep -i 88   oraz   dmesg | tail -50")
        return nics

    release_nics_from_network_stack(nics)

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
        log("    i zasilanie - dwa dongle 8812AU potrafia przeciazyc porty RPi.")

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
        checks.append(("Dongle USB RTL88xx", "ok", f"{len(dongles)} szt. w lsusb (oczekiwano {EXPECTED_NICS})"))
    elif dongles:
        checks.append(("Dongle USB RTL88xx", "fail",
                       f"tylko {len(dongles)} z {EXPECTED_NICS} - sprawdz drugi port USB, kabel i zasilanie"))
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
    if len(nics) >= EXPECTED_NICS:
        checks.append(("Interfejsy wfb", "ok", f"{len(nics)} z {EXPECTED_NICS}: {' '.join(nics)}"))
    elif nics:
        checks.append(("Interfejsy wfb", "fail",
                       f"tylko {len(nics)} z {EXPECTED_NICS}: {' '.join(nics)} "
                       f"- reszta wisi na innym sterowniku niz {TARGET_USB_DRIVER}"))
    else:
        checks.append(("Interfejsy wfb", "fail", "wfb-nics nie zwraca zadnego interfejsu"))

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
        checks.append(("Moc nadawania (TX)", "warn", f"na zywo={live_tx}/63, zapisane={saved_tx}/63 (niezgodne)"))
    elif live_tx.isdigit() and int(live_tx) < 10:
        # Spojna, ale bardzo niska wartosc to typowy cichy zabojca zasiegu -
        # link "dziala na biurku" i pada kilka metrow dalej. Zostaje warn,
        # bo do testow w pomieszczeniu ustawia sie ja swiadomie.
        checks.append(("Moc nadawania (TX)", "warn",
                       f"{live_tx}/63 - bardzo nisko, zasieg bedzie zaden (max = 63)"))
    else:
        checks.append(("Moc nadawania (TX)", "ok", f"{live_tx}/63"))

    # Rozdzial rol sprawdzamy na LICZNIKACH KARTY, a nie w configu ani w linii
    # polecen wfb_tx: wfb-ng podaje procesowi wszystkie interfejsy i dopiero
    # w srodku pomija te oznaczone jako rx-only (rx_only_wlan_ids). Jedynym
    # wiarygodnym dowodem jest wiec to, czy z karty cokolwiek wychodzi -
    # a przy wzmacniaczu jednokierunkowym "nadaje nie ta karta" to zepsuty lot.
    rx_only = {n for n in RX_ONLY_NICS if n in nics}
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
    row("moc TX", f"{live_tx or '?'}/63 na zywo, {saved_tx}/63 zapisane",
        "warn" if live_tx != saved_tx else None)
    row("tryb wideo", video_service_type() or "?")

    section("Karty")
    nics = wfb_nics()
    used = service_nics(set(nics)) if nics else set()
    traffic = nic_traffic(nics) if nics else {}
    for nic in nics:
        d = nic_details(nic)
        rx_pps, tx_pps = traffic.get(nic, (0.0, 0.0))
        row(nic, f"mac={d['mac']}  usb={d['usb']}  {d['driver']} {d['mode']} "
                 f"kan={d['channel']}{nic_role_txt(nic)}",
            "ok" if nic in used else "fail")
        row("", f"rx={rx_pps:.0f}/s tx={tx_pps:.0f}/s   w usludze={'tak' if nic in used else 'NIE'}"
                f"{'   <- przez ta karte leci nadawanie' if tx_pps > 0 else ''}",
            "ok" if tx_pps > 0 else None)
    if nics:
        row("", "(licznik tx > 0 wskazuje karte, ktora faktycznie nadaje)")
    else:
        row("(brak)", "wfb-nics nie zwraca zadnego interfejsu", "fail")

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


def radio_settings_screen(stdscr):
    """Region i moc nadawania. Kanalu sie tu NIE ustawia - jest od tego osobny
    ekran ze skanem pasma i trybem automatycznym. Dwa miejsca do zmiany tej
    samej rzeczy to prosta droga do tego, ze jedno pokazuje co innego niz
    drugie. Ekran otwiera sie z listy kanalow klawiszem 'r'."""
    stdscr.clear()
    draw_header(stdscr, f"WFB-NG [{ROLE}] - region i moc nadawania")

    channel, cur_region = wfb_effective_common()
    cur_tx_power = parse_tx_power()
    freq = channel_freq(channel)

    safe_addstr(stdscr, 2, 2, f"Puste pole = zostaw obecna wartosc (Enter). Rola: {ROLE}.")
    safe_addstr(stdscr, 3, 2, f"Kanal {channel}" + (f" ({freq} MHz)" if freq else "")
                + " zmienisz w ekranie 'Kanal i czestotliwosc'.", curses.A_DIM)

    region = prompt_line(stdscr, 5, "Region (CRDA)", cur_region)

    tx_power = ""
    while not (tx_power.isdigit() and 0 <= int(tx_power) <= 63):
        tx_power = prompt_line(stdscr, 7, "Moc nadawania TX (0-63, 63=max)", cur_tx_power)
        if not (tx_power.isdigit() and 0 <= int(tx_power) <= 63):
            safe_addstr(stdscr, 8, 2, "Podaj liczbe 0-63 (0 = wylaczone, uzyj kalibracji EEPROM).",
                        color_for("fail"))

    country, ranges = reg_domain_ranges()
    span = channel_span(freq)
    lines = [f"Region: {region}   moc TX: {tx_power}/63",
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
    write_modprobe_wfb(tx_power)
    live_ok = apply_tx_power_live(tx_power)
    ensure_video_service_type(wfb_nics())  # gdyby config byl jeszcze sprzed migracji
    ensure_tx_split(wfb_nics())            # restart uslugi i tak jest ponizej
    _common_cache["val"] = None
    run(["systemctl", "daemon-reload"])
    code2, out2 = run(["systemctl", "enable", "--now", f"wifibroadcast@{ROLE}"])
    code3, out3 = run(["systemctl", "restart", f"wifibroadcast@{ROLE}"])

    if code2 == 0 and code3 == 0:
        popup(stdscr, "Zapisano",
              [f"wifibroadcast@{ROLE} uruchomiona.",
               "moc zastosowana natychmiast" if live_ok
               else "moc zapisana, zadziala po nast. zaladowaniu modulu"],
              status="ok" if live_ok else "warn")
    else:
        popup(stdscr, "Zapisano, ale usluga zglosila blad",
              [(out2 + " " + out3)[:70]], status="fail")


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
    say(f"lsusb: {len(dongles)} dongli RTL88xx (oczekiwano {EXPECTED_NICS})")

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
        say(f"  {nic}: {d['driver']} mac={d['mac']} usb={d['usb']} tryb={d['mode']} kanal={d['channel']}")

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
        say("Sprawdz port USB, kabel i zasilanie - dwa dongle 8812AU obciazaja porty RPi.")

    pause(stdscr)


def nic_role_txt(nic):
    """Dopisek o roli karty - pusty, gdy rol nie rozdzielamy (gs). Na dronie
    mowi, ktora karta nadaje: to do niej idzie wzmacniacz i to jej MAC trzeba
    znac, zeby nie pomylic dongli."""
    if not RX_ONLY_NICS:
        return ""
    return "   [tylko odbior]" if nic in RX_ONLY_NICS else "   [nadaje]"


def nic_snapshot():
    """{nazwa: (gniazdo USB, mac)} - lekko, bez wolania 'iw', bo ten ekran
    odpytuje karty dwa razy na sekunde."""
    snap = {}
    for nic in wfb_nics():
        try:
            mac = (Path("/sys/class/net") / nic / "address").read_text().strip()
        except OSError:
            mac = "?"
        snap[nic] = (nic_usb_slot(nic) or "?", mac)
    return snap


def nic_identify_screen(stdscr):
    """Zywy podglad kart: wypnij dongla, a ekran powie, ktora nazwa wlasnie
    zniknela. To najprostszy sposob dopasowania nazwy do konkretnej anteny,
    bo dwa dongle 8812AU wygladaja identycznie i nie widac po nich, ktory
    siedzi w ktorym gniezdzie. Przy okazji licza sie liczniki rx/tx na zywo,
    wiec w tym samym miejscu widac, przez ktora karte leci nadawanie."""
    stdscr.timeout(500)  # getch wraca po 0.5 s, wiec petla sama sie odswieza

    known = nic_snapshot()
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
                note(f"WYPIETO: {nic}   (gniazdo {slot}, mac {mac})", "fail")
                if dongles < prev_dongles:
                    note(f"   ... dongiel zniknal tez z lsusb - to fizyczne wypiecie", "warn")
                else:
                    note("   ... ale lsusb dalej go widzi - to nie kabel, tylko sterownik", "warn")

            for nic in [n for n in current if n not in known]:
                slot, mac = current[nic]
                note(f"WPIETO: {nic}   (gniazdo {slot}, mac {mac})", "ok")
                note("   ... usluga uzyje jej dopiero po 'Wykryj karty ponownie'", "warn")

            known, prev_dongles = current, dongles

            stdscr.clear()
            draw_header(stdscr, f"WFB-NG [{ROLE}] - identyfikacja kart")
            safe_addstr(stdscr, 2, 2,
                        "Wypnij jeden dongiel - ekran powie, ktora nazwa zniknela.", curses.A_BOLD)

            row = 4
            safe_addstr(stdscr, row, 2,
                        f"Karty: {len(current)}/{EXPECTED_NICS}    dongle w lsusb: {dongles}/{EXPECTED_NICS}",
                        color_for("ok" if len(current) == EXPECTED_NICS else "fail") | curses.A_BOLD)
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
                            f"{nic:<12} mac={mac}   gniazdo={slot}{nic_role_txt(nic)}",
                            color_for("ok" if nic in used else "warn") | curses.A_BOLD)
                safe_addstr(stdscr, row + 1, 4,
                            f"rx={rx_pps:6.0f}/s  tx={tx_pps:6.0f}/s   w usludze="
                            f"{'tak' if nic in used else 'NIE'}"
                            + ("   <- ta karta nadaje" if tx_pps > 0 else ""),
                            color_for("ok") if tx_pps > 0 else 0)
                row += 3

            if events:
                safe_addstr(stdscr, row, 2, "Zdarzenia:", curses.A_BOLD)
                row += 1
                for stamp, text, status in events:
                    safe_addstr(stdscr, row, 4, f"{stamp}  {text}", color_for(status))
                    row += 1

            h, _ = stdscr.getmaxyx()
            safe_addstr(stdscr, h - 1, 2, "q = powrot", curses.A_DIM)
            stdscr.refresh()

            key = stdscr.getch()
            if key in (ord("q"), ord("Q"), 27):
                break
    finally:
        stdscr.timeout(-1)  # z powrotem na blokujace getch, inaczej menu zwariuje


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

    for key in ("pid", "probek", "bajtow"):
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
        self._fh = None
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
          f"   moc TX: {read_tx_power_live() or '?'}/63\n")
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
        czytaniu bylo widac, ze w tym miejscu cos sie zmienilo."""
        if self._fh:
            self._fh.write(f"# {time.strftime('%H:%M:%S')}  {text}\n")
            self._fh.flush()

    def sample(self, elapsed, metrics, ping):
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
            w(f"# najslabszy sygnal: {self._rssi.lo:.0f} dBm ({rssi_grade(self._rssi.lo)[1]})\n")
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

            for note in take_test_notes():
                recorder.note(note)

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

    overall = worst_status([s for s in (rssi_st, run_loss_st, snr_st, run_ping_st) if s])
    if not ants and rx_pps_total <= 0 and not rtt:
        overall, overall_txt = "fail", "BRAK ODBIORU"
    else:
        overall_txt = {"ok": "DOBRE", "warn": "SLABE", "fail": "ZLE"}.get(overall, "?")
        if overall == "ok" and rssi_txt == "doskonaly":
            overall_txt = "DOSKONALE"

    head = f"Ocena lacza: {overall_txt}"
    parts = []
    if best_rssi is not None:
        parts.append(f"sygnal {best_rssi:.0f} dBm")
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

    if overall == "fail" and not ants and rx_pps_total <= 0:
        row("Nic nie przychodzi z drugiej strony. Sprawdz po obu stronach: ten sam kanal,", "fail")
        row("ten sam odcisk kluczy, wlaczona usluga i moc TX wieksza od zera.", "fail")

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
        # Jedna linia zamiast wiersza na kazdy tor odbiorczy karty. RSSI
        # pojedynczego toru nie mowi nic o jakosci lacza: wfb-ng sklada strumien
        # z tego, ktory akurat slyszy lepiej, i tak samo liczona jest ocena na
        # gorze ekranu. Rozbicie na anteny zostaje w zapisie do pliku (kolumna
        # anteny_rssi) - tam przydaje sie przy ustawianiu anten.
        section(f"Sygnal odbierany z {PEER_NAME}")
        best = max((a for a in ants if a["rssi"]), key=lambda a: a["rssi"][1],
                   default=None)
        if not best:
            row("brak danych o sygnale - ramki przychodza bez statystyk anten", "warn")
        else:
            rssi, snr = best["rssi"], best["snr"]
            st, txt = rssi_grade(rssi[1])
            row(f"RSSI {rssi[0]:>5.0f}/{rssi[1]:>5.0f}/{rssi[2]:>5.0f} dBm  "
                f"{meter(rssi[1], -90, -40)}  {txt}", st)
            if snr:
                sst, stxt = snr_grade(snr[1])
                row(f"SNR  {snr[0]:>5.0f}/{snr[1]:>5.0f}/{snr[2]:>5.0f} dB   "
                    f"{meter(snr[1], 0, 40)}  {stxt}", sst)
            # bez licznika ramek: statystyki anten przychodza osobno dla kazdego
            # strumienia, wiec liczba z jednego wiersza nie jest calym ruchem -
            # ten jest ponizej, w sekcji odbioru
            where = f"{best['freq']} MHz" if best["freq"] else ""
            if best["mcs"] is not None:
                where += ("   " if where else "") + f"MCS {best['mcs']}"
            if where:
                row(f"kanal  {where}")
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
    row(f"sygnal {worst['rssi']:.0f} dBm" if worst["rssi"] is not None else "sygnal ?",
        rssi_grade(worst["rssi"])[0])
    row(f"straty {worst['loss']:.1f}%" if worst["loss"] is not None else "straty ?",
        loss_grade(worst["loss"])[0])

    return lines


def test_state_line(state, key="t"):
    """Jedna linijka o zapisie w tle - ta sama na gorze menu i ekranu testu."""
    name = Path(state["plik"]).name
    size = f"{human_size(state['bajtow'])} / {human_size(TEST_MAX_BYTES)}"
    if state["stan"] == "trwa":
        return (f"TEST TRWA W TLE  {fmt_mmss(state['czas'])}   {name}   "
                f"{state['probek']} probek   {size}   ({key} = zakoncz)")
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
               "Konczy go klawisz 't' - tutaj albo w menu glownym."],
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
                  f"w menu glownym); sam staje na {human_size(TEST_MAX_BYTES)}."],
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
                hint += ", t = zakoncz zapis"
            if len(lines) > view:
                hint = (f"strzalki = przewijanie ({top + 1}-{min(top + view, len(lines))}"
                        f"/{len(lines)}), " + hint)
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
                                      "r = region i moc TX, q = powrot", curses.A_DIM)
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
            radio_settings_screen(stdscr)
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
        "Klucze i parowanie",
        "Test polaczenia (sygnal, straty, ping)",
        "Kanal i czestotliwosc (skan, tryb auto)",
        "Wybor modulacji (MCS)",
        "Naprawa utraconych pakietow (FEC tunelu)",
        "Uruchom weryfikacje",
        "Wyjdz",
    ]
    idx = 0

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
                keys_screen(stdscr)
            elif idx == 4:
                link_test_screen(stdscr)
            elif idx == 5:
                channel_screen(stdscr)
            elif idx == 6:
                modulation_screen(stdscr)
            elif idx == 7:
                repair_screen(stdscr)
            elif idx == 8:
                verification_screen(stdscr)
            elif idx == 9:
                if confirm_exit(stdscr):
                    break
        elif key in (ord("r"), ord("R")):
            _nic_status_cache["val"] = None  # wpiety wlasnie dongiel bez czekania
        elif key in (ord("t"), ord("T")):
            stdscr.timeout(-1)  # okienko ma czekac na klawisz, nie na timeout
            background_test_popup(stdscr)
        elif key in (ord("q"), 27):
            if confirm_exit(stdscr):
                break


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

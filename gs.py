#!/usr/bin/env python3
"""WFB-NG - instalator + pseudo-graficzny (curses) TUI, rola: GS.

Pierwsze uruchomienie (na swiezym Raspberry Pi OS, z podlaczona karta
RTL8812AU) robi caly setup: pakiety systemowe, sterownik karty, klucze
szyfrujace, /etc/wifibroadcast.cfg, usluge systemd. Kolejne uruchomienia
(setup juz gotowy) od razu otwieraja konfigurator/weryfikator.

Gs ma JEDNA karte, dron dwa dongle (EXPECTED_NICS). Kazdy start sprawdza,
czy karta jest widoczna, przepieta pod nasz sterownik, w trybie monitor na
wlasciwym kanale i czy faktycznie przepuszcza ruch.

Karta dostaje stala nazwe (NIC_NAMES: gs_wfb) zamiast wlanX - przypieta regula
udev do gniazda USB, wiec ta sama karta w tym samym porcie ma zawsze te sama
nazwe. Jedna nazwa, bo gs ma jedna karte i ta sama karta odbiera wideo i nadaje
mavlink/RC w gore - nie ma tu podzialu na RX i TX.

Klucze szyfrujace sa wbudowane w oba skrypty (identyczne), wiec link wstaje
od razu, bez przenoszenia plikow. W menu jest parowanie: jedna strona pokazuje
8-znakowy kod, na drugiej sie go wpisuje i obie licza z niego te sama, prywatna
pare kluczy.

Uzycie:
    sudo python3 gs.py
"""

import base64
import curses
import hashlib
import io
import os
import re
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path

ROLE = "gs"
PEER_IP = "10.5.0.2"  # adres drugiej strony (drone) w tunelu
SSH_PORT = 22

EXPECTED_NICS = 1  # gs: jedna karta RTL (dron nadaje z dwoch, tu wystarczy jedna)

DRIVER_TAG = "v5.2.20"
APT_RELEASE = "master"
DEFAULT_CHANNEL = "161"
DEFAULT_REGION = "BO"
DEFAULT_TX_POWER = "63"  # 0-63, wg sterownika: 0 = wylaczone (EEPROM), 63 = max

MODPROBE_WFB = Path("/etc/modprobe.d/wfb.conf")
TX_POWER_SYSFS = Path("/sys/module/88XXau_wfb/parameters/rtw_tx_pwr_idx_override")

CFG_PATH = Path("/etc/wifibroadcast.cfg")
DRONE_KEY = Path("/etc/drone.key")
GS_KEY = Path("/etc/gs.key")
REBOOT_MARKER = Path("/etc/.wfb-gs-reboot-attempted")

# Zamiast wlanX (numer zalezy od kolejnosci wykrycia i potrafi sie zmienic
# miedzy bootami) dajemy karcie stala, czytelna nazwe. Nazwa jest przypieta do
# GNIAZDA USB, wiec po restarcie ta sama karta w tym samym porcie ma ta sama
# nazwe. Gs ma JEDNA karte i ta jedna karta robi oba kierunki (odbiera wideo,
# nadaje mavlink/RC w gore) - dlatego jedna nazwa bez RX/TX, bo nie ma tu
# czego rozrozniac. Na dronie, gdzie karty sa dwie, sa to drone_RX/drone_TX.
NIC_NAMES = ["gs_wfb"]
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
    tego, ktory dongiel w nim siedzi - dlatego to na nim wieszamy nazwy."""
    dev = Path("/sys/class/net") / nic / "device"
    try:
        return dev.resolve().name if dev.exists() else ""
    except OSError:
        return ""


def nic_details(nic):
    """Skad karta pochodzi i w jakim jest stanie: sterownik, MAC, fizyczny
    port USB (rozroznia dwa identyczne dongle), tryb pracy i kanal."""
    base = Path("/sys/class/net") / nic
    info = {"driver": "?", "mac": "?", "usb": "?", "mode": "?", "channel": "?"}

    try:
        info["mac"] = (base / "address").read_text().strip()
    except OSError:
        pass

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
        # Karta moze byc idealna, a i tak 0/1 - bo usluga w ogole nie wstala.
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


def service_last_errors(n=6):
    """Ogon journala uslugi. Gdy usluga nie wstaje, powod jest wlasnie tam -
    lepiej pokazac go od razu na ekranie niz odsylac do recznego journalctl."""
    code, out = run(["journalctl", "-u", f"wifibroadcast@{ROLE}", "-n", str(n),
                     "-o", "cat", "--no-pager"], timeout=15)
    if code != 0:
        return []
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


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
    ch = re.search(r"wifi_channel\s*=\s*(\d+)", txt)
    reg = re.search(r"wifi_region\s*=\s*'([^']*)'", txt)
    return (ch.group(1) if ch else DEFAULT_CHANNEL, reg.group(1) if reg else DEFAULT_REGION)


def video_section_bounds(txt):
    """Zakres sekcji [<rola>_video] w configu - zeby ruszac tylko ja."""
    header = f"[{ROLE}_video]"
    start = txt.find(header)
    if start == -1:
        return None
    end = txt.find("\n[", start + 1)
    return start, (len(txt) if end == -1 else end)


def video_service_type(txt):
    """Tryb uslugi wideo z configu albo None, gdy nie ustawiony - wtedy
    obowiazuje domyslny z master.cfg wfb-ng."""
    bounds = video_section_bounds(txt)
    if not bounds:
        return None
    m = re.search(r"^\s*service_type\s*=\s*'([^']*)'", txt[bounds[0]:bounds[1]], re.M)
    return m.group(1) if m else None


def ensure_video_service_type(nics):
    """Domyslny tryb wideo nie umie obsluzyc kilku kart naraz: serwer konczy
    sie wtedy bledem "udp_direct_tx doesn't supports diversity and/or rx-only
    wlans. Use udp_proxy for such case." i systemd restartuje go w kolko -
    z zewnatrz widac tylko status "activating". Przy wiecej niz jednej karcie
    wymuszamy udp_proxy. Na gs, z jedna karta, nie robi nic - ale kod jest
    wspolny z drone.py, gdzie karty sa dwie."""
    if len(nics) < 2 or not CFG_PATH.exists():
        return False

    txt = CFG_PATH.read_text()
    bounds = video_section_bounds(txt)
    if not bounds or video_service_type(txt) == "udp_proxy":
        return False

    start, end = bounds
    section = txt[start:end]
    if video_service_type(txt) is None:
        section = section.replace(f"[{ROLE}_video]",
                                  f"[{ROLE}_video]\nservice_type = 'udp_proxy'", 1)
    else:
        section = re.sub(r"^\s*service_type\s*=.*$", "service_type = 'udp_proxy'",
                         section, count=1, flags=re.M)
    CFG_PATH.write_text(txt[:start] + section + txt[end:])
    return True


def build_config(channel, region):
    return (
        "[common]\n"
        f"wifi_channel = {channel}\n"
        f"wifi_region = '{region}'\n\n"
        f"{ROLE_SECTION}"
    )


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


def ping_stats(ip, count=5, timeout=2):
    """Ping idzie przez tunel wfb, czyli fizycznie przez karte RTL8812AU."""
    code, out = run(["ping", "-c", str(count), "-W", str(timeout), ip], timeout=count * timeout + 5)
    loss_m = re.search(r"(\d+)% packet loss", out)
    rtt_m = re.search(r"=\s*[\d.]+/([\d.]+)/", out)
    loss = loss_m.group(1) if loss_m else "?"
    avg = rtt_m.group(1) if rtt_m else None
    return code, loss, avg


def check_ssh(ip, port=SSH_PORT, timeout=3):
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


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

def parse_name_rules():
    """{gniazdo USB: nazwa} z naszego pliku regul udev - czyli przypisania,
    ktore juz kiedys ustalilismy."""
    mapping = {}
    if not UDEV_NAMES.exists():
        return mapping
    for line in UDEV_NAMES.read_text().splitlines():
        m = re.search(r'KERNELS=="([^"]+)".*NAME="([^"]+)"', line)
        if m:
            mapping[m.group(1)] = m.group(2)
    return mapping


def plan_nic_names(nics):
    """Przydziela nazwy kartom. Raz ustalone przypisanie gniazdo->nazwa zostaje
    (lezy w regulach udev), nowe gniazdo dostaje pierwsza wolna nazwe. Dzieki
    temu przy jednej wypietej karcie druga NIE przejmuje jej nazwy - inaczej po
    kazdym przepieciu dongla nazwy mowilyby co innego niz poprzednio.
    Zwraca (mapa gniazdo->nazwa, mapa interfejs->nazwa)."""
    by_slot = parse_name_rules()
    slots = {nic: nic_usb_slot(nic) for nic in nics}
    live = {s for s in slots.values() if s}

    # Puste gniazdo nie moze w nieskonczonosc trzymac nazwy - inaczej dongiel
    # przelozony do innego portu zostawaly przy wlanX. Ale zwalniamy je TYLKO
    # gdy jest jakas karta bez nazwy, czyli jest komu te nazwe oddac: sam
    # chwilowy brak dongla (zly kabel, port nie wstal po boocie) niczego nie
    # przestawia i po ponownym wpieciu karta wraca do swojej nazwy.
    if any(s not in by_slot for s in live):
        for slot in [s for s in by_slot if s not in live]:
            del by_slot[slot]

    free = [n for n in NIC_NAMES if n not in by_slot.values()]
    per_nic = {}
    for nic in sorted(nics, key=lambda n: slots[n]):
        slot = slots[nic]
        if not slot:
            continue  # karta bez gniazda USB - nie ma czego zakotwiczyc w regule
        if slot not in by_slot:
            if not free:
                continue  # wiecej kart niz nazw - reszta zostaje przy wlanX
            by_slot[slot] = free.pop(0)
        per_nic[nic] = by_slot[slot]
    return by_slot, per_nic


def write_name_rules(by_slot):
    txt = ("# generowane przez skrypt wfb - nie edytuj recznie\n"
           "# stale nazwy kart RTL88xx; nazwa jest przypieta do GNIAZDA USB,\n"
           "# wiec zamiana dwoch dongli miejscami zamienia tez ich nazwy\n")
    for slot, name in sorted(by_slot.items()):
        txt += f'SUBSYSTEM=="net", ACTION=="add", KERNELS=="{slot}", NAME="{name}"\n'
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

    by_slot, per_nic = plan_nic_names(nics)
    write_name_rules(by_slot)  # zeby przetrwalo reboot i ponowne wpiecie dongla
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
        log("    config juz istnieje, pomijam (edytuj przez menu ponizej)")

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
        log(f"    {CFG_PATH}: [{ROLE}_video] przestawione na udp_proxy - domyslny")
        log(f"    tryb nie umie obsluzyc {len(nics)} kart i zabijal usluge przy starcie.")
        run(["systemctl", "restart", f"wifibroadcast@{ROLE}"])
        time.sleep(3)

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

    cfg_channel = parse_common(CFG_PATH.read_text())[0] if CFG_PATH.exists() else None
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

    if CFG_PATH.exists():
        txt = CFG_PATH.read_text()
        ch, reg = parse_common(txt)
        vtype = video_service_type(txt) or "domyslny"
        detail = f"kanal={ch} region={reg} rola={ROLE} wideo={vtype}"
        if len(nics) > 1 and video_service_type(txt) != "udp_proxy":
            checks.append(("wifibroadcast.cfg", "fail",
                           detail + f" - przy {len(nics)} kartach usluga sie wywali, potrzeba udp_proxy"))
        else:
            checks.append(("wifibroadcast.cfg", "ok", detail))
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
        checks.append((f"SSH do drone ({PEER_IP}:{SSH_PORT})", "ok", "port otwarty, SSH odpowiada"))
    else:
        checks.append((f"SSH do drone ({PEER_IP}:{SSH_PORT})", "warn", "brak polaczenia na porcie 22"))

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


def show_config_screen(stdscr):
    stdscr.clear()
    draw_header(stdscr, f"WFB-NG [{ROLE}] - biezaca konfiguracja")
    if CFG_PATH.exists():
        lines = CFG_PATH.read_text().splitlines()
    else:
        lines = [f"{CFG_PATH} nie istnieje jeszcze."]
    for i, line in enumerate(lines):
        safe_addstr(stdscr, 2 + i, 2, line)
    pause(stdscr)


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


def edit_config_screen(stdscr):
    stdscr.clear()
    draw_header(stdscr, f"WFB-NG [{ROLE}] - zmiana konfiguracji")

    cur_channel, cur_region = DEFAULT_CHANNEL, DEFAULT_REGION
    if CFG_PATH.exists():
        cur_channel, cur_region = parse_common(CFG_PATH.read_text())
    cur_tx_power = parse_tx_power()

    safe_addstr(stdscr, 2, 2, f"Puste pole = zostaw obecna wartosc (Enter). Rola jest stala: {ROLE}.")

    channel = ""
    while not channel.isdigit():
        channel = prompt_line(stdscr, 4, "Kanal WiFi", cur_channel)
        if not channel.isdigit():
            safe_addstr(stdscr, 5, 2, "Kanal musi byc liczba.", color_for("fail"))

    region = prompt_line(stdscr, 7, "Region (CRDA)", cur_region)

    tx_power = ""
    while not (tx_power.isdigit() and 0 <= int(tx_power) <= 63):
        tx_power = prompt_line(stdscr, 9, "Moc nadawania TX (0-63, 63=max)", cur_tx_power)
        if not (tx_power.isdigit() and 0 <= int(tx_power) <= 63):
            safe_addstr(stdscr, 10, 2, "Podaj liczbe 0-63 (0 = wylaczone, uzyj kalibracji EEPROM).",
                        color_for("fail"))

    safe_addstr(stdscr, 12, 2, f"Nowy kanal: {channel}   region: {region}   moc TX: {tx_power}/63")
    safe_addstr(stdscr, 14, 2, "Zapisac i zrestartowac usluge? [t/N]: ")
    stdscr.refresh()
    curses.echo()
    curses.curs_set(1)
    ans = stdscr.getstr(14, 38, 5).decode().strip().lower()
    curses.noecho()
    curses.curs_set(0)

    if ans != "t":
        safe_addstr(stdscr, 16, 2, "Anulowano.", color_for("warn"))
        pause(stdscr)
        return

    CFG_PATH.write_text(build_config(channel, region))
    write_modprobe_wfb(tx_power)
    live_ok = apply_tx_power_live(tx_power)
    run(["systemctl", "daemon-reload"])
    code2, out2 = run(["systemctl", "enable", "--now", f"wifibroadcast@{ROLE}"])
    code3, out3 = run(["systemctl", "restart", f"wifibroadcast@{ROLE}"])

    if code2 == 0 and code3 == 0:
        safe_addstr(stdscr, 16, 2, f"Zapisano, wifibroadcast@{ROLE} uruchomiona.", color_for("ok"))
        tx_note = "moc zastosowana natychmiast" if live_ok else "moc zapisana, zadziala po nast. zaladowaniu modulu"
        safe_addstr(stdscr, 17, 2, tx_note, color_for("ok" if live_ok else "warn"))
    else:
        safe_addstr(stdscr, 16, 2, "Zapisano, ale usluga zglosila blad:", color_for("fail"))
        safe_addstr(stdscr, 17, 2, (out2 + " " + out3)[:100])
    pause(stdscr)


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
            say(f"config: [{ROLE}_video] -> udp_proxy (domyslny tryb nie umie {len(nics)} kart)", "warn")
            run(["systemctl", "restart", f"wifibroadcast@{ROLE}"])
            time.sleep(3)
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
        "Zmien kanal / region i zapisz",
        "Wykryj karty ponownie (naprawa)",
        "Klucze i parowanie",
        "Uruchom weryfikacje",
        "Wyjdz",
    ]
    idx = 0

    while True:
        stdscr.clear()
        draw_header(stdscr, f"WFB-NG [{ROLE.upper()}] - konfigurator i weryfikator")

        if not (DRONE_KEY.exists() and GS_KEY.exists()):
            safe_addstr(stdscr, 2, 2, "Brak kluczy - cos poszlo nie tak przy instalacji", color_for("fail"))

        nic_status, nic_txt = nic_status_summary()
        safe_addstr(stdscr, 3, 2, nic_txt, color_for(nic_status) | curses.A_BOLD)

        for i, item in enumerate(items):
            attr = curses.color_pair(5) if i == idx else curses.A_NORMAL
            safe_addstr(stdscr, 5 + i, 4, item.ljust(50), attr)

        h, _ = stdscr.getmaxyx()
        safe_addstr(stdscr, h - 1, 2, "Strzalki gora/dol, Enter = wybierz, r = odswiez, q = wyjscie",
                    curses.A_DIM)
        stdscr.refresh()

        key = stdscr.getch()
        if key in (curses.KEY_UP, ord("k")):
            idx = (idx - 1) % len(items)
        elif key in (curses.KEY_DOWN, ord("j")):
            idx = (idx + 1) % len(items)
        elif key in (10, 13, curses.KEY_ENTER):
            if idx == 0:
                show_config_screen(stdscr)
            elif idx == 1:
                edit_config_screen(stdscr)
            elif idx == 2:
                redetect_screen(stdscr)
            elif idx == 3:
                keys_screen(stdscr)
            elif idx == 4:
                verification_screen(stdscr)
            elif idx == 5:
                break
        elif key in (ord("r"), ord("R")):
            _nic_status_cache["val"] = None  # wpiety wlasnie dongiel bez czekania
        elif key in (ord("q"), 27):
            break


def main():
    require_root()
    os.environ.setdefault("DEBIAN_FRONTEND", "noninteractive")

    if not is_fully_installed():
        full_setup()

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

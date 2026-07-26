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

import ast
import base64
import curses
import hashlib
import io
import os
import re
import secrets
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

ROLE = "gs"
PEER_IP = "10.5.0.2"  # adres drugiej strony (drone) w tunelu
PEER_NAME = "drone"
SSH_PORT = 22

EXPECTED_NICS = 1  # gs: jedna karta RTL (dron nadaje z dwoch, tu wystarczy jedna)

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
TEST_LOG_DIR = Path(__file__).resolve().parent
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
    ch = re.search(r"wifi_channel\s*=\s*(\d+)", txt)
    reg = re.search(r"wifi_region\s*=\s*'([^']*)'", txt)
    return (ch.group(1) if ch else DEFAULT_CHANNEL, reg.group(1) if reg else DEFAULT_REGION)


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
    """Domyslny tryb wideo (udp_direct_tx) nie umie obsluzyc kilku kart naraz:
    serwer konczy sie wtedy bledem "udp_direct_tx doesn't supports diversity
    and/or rx-only wlans. Use udp_proxy for such case." i systemd restartuje go
    w kolko - z zewnatrz widac tylko status "activating", a karty wygladaja na
    sprawne. Przy wiecej niz jednej karcie nadpisujemy w profilu [<rola>] cala
    liste 'streams' z podmienionym service_type dla wideo. Na gs, z jedna
    karta, nie robi nic - ale kod jest wspolny z drone.py, gdzie karty sa
    dwie."""
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


def rx_loss_pct(msg):
    """Procent pakietow, ktore przepadly bezpowrotnie. 'all' to wszystko, co
    dotarlo, 'lost' - dziury wykryte po numerach sekwencji. Pakiety odtworzone
    przez FEC nie sa strata: doszly, tylko okrezna droga."""
    got = rx_packets(msg, "all")[0]
    lost = rx_packets(msg, "lost")[0]
    total = got + lost
    return (100.0 * lost / total) if total else None


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


# Domyslne numery portow radiowych wfb-ng - sluza tylko do podpisania wiersza,
# sam numer i tak jest obok.
RADIO_PORT_NAMES = {"0": "video", "1": "mavlink", "2": "tunnel"}

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
        log(f"    {CFG_PATH}: wideo przestawione na udp_proxy - domyslny tryb")
        log(f"    (udp_direct_tx) nie umie obsluzyc {len(nics)} kart i zabijal usluge.")
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
        ch, reg = parse_common(CFG_PATH.read_text())
        vtype = video_service_type()
        detail = f"kanal={ch} region={reg} rola={ROLE} wideo={vtype or '?'}"
        if len(nics) > 1 and vtype == "udp_direct_tx":
            checks.append(("wifibroadcast.cfg", "fail",
                           detail + f" - ten tryb nie umie {len(nics)} kart, usluga bedzie sie wywalac"))
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
    ch, reg = (parse_common(CFG_PATH.read_text()) if CFG_PATH.exists()
               else (DEFAULT_CHANNEL, DEFAULT_REGION))
    freq = channel_freq(ch)
    span = channel_span(freq)
    row("kanal", f"{ch}" + (f"   {freq} MHz, HT20 zajmuje {span[0]}-{span[1]} MHz" if freq else ""))
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
        row(nic, f"{d['driver']} mac={d['mac']} usb={d['usb']} {d['mode']} kan={d['channel']}",
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

    freq = channel_freq(channel)
    country, ranges = reg_domain_ranges()
    span = channel_span(freq)
    band = f" ({freq} MHz, HT20: {span[0]}-{span[1]})" if freq else ""
    safe_addstr(stdscr, 12, 2,
                f"Nowy kanal: {channel}{band}   region: {region}   moc TX: {tx_power}/63")

    # Kanal spoza pasma dozwolonego w regionie = karta w ogole nie nadaje,
    # a wyglada zdrowo. Lepiej powiedziec to PRZED zapisem niz szukac potem.
    if freq and ranges and not any(lo <= span[0] and span[1] <= hi for lo, hi in ranges):
        note = f"UWAGA: {span[0]}-{span[1]} MHz nie miesci sie w domenie {country}"
        if region != country:
            note += f" (zapisujesz {region} - sprawdz po restarcie w weryfikacji)"
        safe_addstr(stdscr, 13, 2, note[:110], color_for("warn"))

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

    save_common_config(channel, region)
    write_modprobe_wfb(tx_power)
    live_ok = apply_tx_power_live(tx_power)
    ensure_video_service_type(wfb_nics())  # gdyby config byl jeszcze sprzed migracji
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
            say(f"config: wideo -> udp_proxy (domyslny tryb nie umie {len(nics)} kart)", "warn")
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
    bo dongle 8812AU wygladaja identycznie i nie widac po nich, ktory siedzi
    w ktorym gniezdzie. Przy okazji licza sie liczniki rx/tx na zywo, wiec
    w tym samym miejscu widac, przez ktora karte leci nadawanie."""
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

                safe_addstr(stdscr, row, 2, f"{nic:<12} gniazdo={slot:<10} mac={mac}",
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


def _stat_line(values, fmt="{:.1f}"):
    if not values:
        return "brak danych"
    return (f"min {fmt.format(min(values))}   srednio {fmt.format(sum(values) / len(values))}"
            f"   max {fmt.format(max(values))}")


class TestRecorder:
    """Zapis przebiegu testu do pliku: naglowek z cala konfiguracja, potem
    jeden wiersz na sekunde, na koniec podsumowanie. Plik zamyka sie z chwila
    wyjscia z ekranu testu.

    Po co: przy sprawdzaniu zasiegu wyniku nie da sie ogladac na biezaco (jest
    sie kilkaset metrow od ekranu), a i tak trzeba go z czyms porownac - "przed"
    i "po" przestawieniu anteny albo zmianie kanalu. Wiersze sa rozdzielone
    srednikami, wiec plik otwiera sie tez w arkuszu."""

    COLUMNS = ("czas", "sek", "rssi_best_dBm", "snr_best_dB", "rx_mcs", "rx_bw_MHz",
               "straty_%", "rx_pkt_s", "rx_Mbit_s", "fec_naprawil_s", "utracone_s",
               "ping_ms", "ping_utrata_%", "anteny_rssi")

    def __init__(self, path):
        self.path = path
        self.samples = 0
        self._fh = None
        self._rssi = []
        self._loss = []
        self._ping = []
        self._mcs = {}

    def open(self):
        """Moze rzucic OSError - wolajacy pokazuje to w okienku i test idzie
        dalej bez zapisu."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("w", encoding="utf-8")
        self._header()
        return self

    def _header(self):
        ch, reg = (parse_common(CFG_PATH.read_text()) if CFG_PATH.exists()
                   else (DEFAULT_CHANNEL, DEFAULT_REGION))
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
            port = str(tx.get("port", "?"))
            main, extra = tx_modulation_txt(tx)
            w(f"# nadawanie {RADIO_PORT_NAMES.get(port, 'port ' + port)} (port {port}):"
              f" {main}   {extra}\n")
        w(f"# druga strona: {PEER_NAME} {PEER_IP}\n#\n")
        w(";".join(self.COLUMNS) + "\n")
        self._fh.flush()

    def note(self, text):
        """Komentarz w srodku pliku - np. o wyzerowaniu licznikow, zeby przy
        czytaniu bylo widac, ze w tym miejscu cos sie zmienilo."""
        if self._fh:
            self._fh.write(f"# {time.strftime('%H:%M:%S')}  {text}\n")
            self._fh.flush()

    def sample(self, elapsed, metrics, ping):
        rtt, last_loss = ping[0], ping[1]
        rssi, snr, loss = metrics["best_rssi"], metrics["best_snr"], metrics["loss"]
        ants = " ".join(f"{a['label'].replace(' ', ':')}={a['rssi'][1]:.0f}"
                        for a in metrics["ants"] if a["rssi"])

        def num(value, fmt="{:.1f}"):
            return fmt.format(value) if value is not None else ""

        self._fh.write(";".join([
            time.strftime("%H:%M:%S"), f"{elapsed:.0f}",
            num(rssi, "{:.0f}"), num(snr, "{:.0f}"),
            num(metrics["mcs"], "{:.0f}"), num(metrics["bw"], "{:.0f}"), num(loss),
            f"{metrics['rx_pps']:.0f}", f"{mbit(metrics['rx_bytes']):.2f}",
            f"{metrics['fec']:.0f}", f"{metrics['lost']:.0f}",
            num(rtt[1] if rtt else None), num(last_loss, "{:.0f}"), ants,
        ]) + "\n")
        self._fh.flush()  # zeby po Ctrl+C albo zaniku zasilania zostalo to, co juz bylo
        self.samples += 1

        if rssi is not None:
            self._rssi.append(rssi)
        if loss is not None:
            self._loss.append(loss)
        if rtt:
            self._ping.append(rtt[1])
        if metrics["mcs"] is not None:
            key = (metrics["mcs"], metrics["bw"])
            self._mcs[key] = self._mcs.get(key, 0) + 1

    def close(self, worst, elapsed):
        if not self._fh:
            return
        w = self._fh.write
        w("#\n# --- podsumowanie ---\n")
        w(f"# koniec: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        w(f"# czas testu: {int(elapsed) // 60} min {int(elapsed) % 60} s"
          f"   probek: {self.samples}\n")
        w(f"# RSSI [dBm]:  {_stat_line(self._rssi, '{:.0f}')}\n")
        w(f"# straty [%]:  {_stat_line(self._loss)}\n")
        w(f"# ping [ms]:   {_stat_line(self._ping)}\n")
        for (mcs, bw), count in sorted(self._mcs.items(), key=lambda kv: -kv[1]):
            desc, rate = mcs_info(mcs, bw)
            w(f"# odbior: {desc}, {bw_mhz(bw)} MHz"
              + (f", ~{rate:.0f} Mbit/s PHY" if rate else "")
              + f" - w {count} z {self.samples} probek\n")
        if worst["rssi"] is not None:
            w(f"# najslabszy sygnal: {worst['rssi']:.0f} dBm ({rssi_grade(worst['rssi'])[1]})\n")
        if worst["loss"] is not None:
            w(f"# najwieksze straty: {worst['loss']:.1f}% ({loss_grade(worst['loss'])[1]})\n")
        self._fh.close()
        self._fh = None


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


STREAM_ORDER = ("video", "mavlink", "tunnel")


def stream_key(name):
    base = (name or "").split()[0]
    return (STREAM_ORDER.index(base) if base in STREAM_ORDER else len(STREAM_ORDER), name or "")


def link_metrics(msgs, nics):
    """Liczby wyluskane z wiadomosci API. Osobno od rysowania, bo dokladnie te
    same wartosci ida na ekran i do pliku z zapisem testu - liczymy je raz."""
    rx_msgs = {name: m for (kind, name), m in msgs.items() if kind == "rx"}
    tx_msgs = {name: m for (kind, name), m in msgs.items() if kind == "tx"}

    ants = []
    for name in sorted(rx_msgs, key=stream_key):
        ants.extend(antenna_rows(rx_msgs[name], nics))
    main_rx = next((rx_msgs[n] for n in sorted(rx_msgs, key=stream_key)), None)

    # Modulacja odbieranych ramek. Zwykle jedna dla wszystkich anten, ale przy
    # zmianie ustawien po drugiej stronie potrafia sie chwilowo mieszac -
    # dlatego liczymy pakiety per (MCS, szerokosc) i bierzemy przewazajaca.
    mods = {}
    for a in ants:
        if a["mcs"] is not None:
            key = (a["mcs"], a["bw"])
            mods[key] = mods.get(key, 0) + a["count"]
    top_mod = max(mods, key=mods.get) if mods else (None, None)

    return {
        "rx": rx_msgs,
        "tx": tx_msgs,
        "ants": ants,
        "main_rx": main_rx,
        "mods": mods,
        "mcs": top_mod[0],
        "bw": top_mod[1],
        # Przy dywersyfikacji liczy sie NAJLEPSZA antena - wfb-ng i tak sklada
        # strumien z tej, ktora akurat slyszy lepiej.
        "best_rssi": max((a["rssi"][1] for a in ants if a["rssi"]), default=None),
        "best_snr": max((a["snr"][1] for a in ants if a["snr"]), default=None),
        "loss": rx_loss_pct(main_rx) if main_rx else None,
        "rx_pps": sum(rx_packets(m, "all")[0] for m in rx_msgs.values()),
        "rx_bytes": ((rx_packets(main_rx, "out_bytes")[0]
                      or rx_packets(main_rx, "all_bytes")[0]) if main_rx else 0),
        "fec": rx_packets(main_rx, "fec_rec")[0] if main_rx else 0,
        "lost": rx_packets(main_rx, "lost")[0] if main_rx else 0,
        "lost_total": rx_packets(main_rx, "lost")[1] if main_rx else 0,
    }


def link_test_lines(metrics, api_error, nics, used, traffic, ping, worst, elapsed):
    """Cala tresc ekranu testu jako lista (tekst, atrybut) - budowana od nowa
    przy kazdym odswiezeniu, bo wszystkie liczby sa chwilowe."""
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
    worst["lost"] = max(worst["lost"], metrics["lost_total"])

    rssi_st, rssi_txt = rssi_grade(best_rssi)
    loss_st, loss_txt = loss_grade(loss)
    snr_st, snr_txt = snr_grade(best_snr)
    ping_st, _ = loss_grade(last_loss)

    overall = worst_status([s for s in (rssi_st, loss_st, snr_st, ping_st) if s])
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
    if rtt:
        parts.append(f"ping {rtt[1]:.1f} ms")
    parts.append(f"czas testu {int(elapsed) // 60:02d}:{int(elapsed) % 60:02d}")
    lines.append((f"{head}   " + "   ".join(parts), color_for(overall) | curses.A_BOLD))

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
    for tx in tx_params:
        port = str(tx.get("port", "?"))
        who = RADIO_PORT_NAMES.get(port, "port " + port)
        main, extra = tx_modulation_txt(tx)
        row(f"{'nadawanie':<11}{who} (port {port}): {main}")
        row(extra, indent=13)
    if not tx_params:
        row(f"{'nadawanie':<11}nie widac zadnego wfb_tx - usluga nie dziala?", "warn")
    row("(odbior = czym nadaje druga strona, nadawanie = czym nadajemy my;")
    row(" predkosc odbioru liczona przy dlugim GI, bo ramka jej nie niesie)")

    if ants:
        section("Sygnal (kazda antena osobno)")
        for a in ants:
            rssi, snr = a["rssi"], a["snr"]
            st, txt = rssi_grade(rssi[1] if rssi else None)
            where = f"{a['freq']} MHz" if a["freq"] else ""
            if a["mcs"] is not None:
                where += f"   MCS {a['mcs']}"
            row(f"{a['label']:<16}{a['count']:>7.0f} pkt/s   {where}", st)
            if rssi:
                row(f"RSSI {rssi[0]:>5.0f}/{rssi[1]:>5.0f}/{rssi[2]:>5.0f} dBm  "
                    f"{meter(rssi[1], -90, -40)}  {txt}", st, indent=4)
            if snr:
                sst, stxt = snr_grade(snr[1])
                row(f"SNR  {snr[0]:>5.0f}/{snr[1]:>5.0f}/{snr[2]:>5.0f} dB   "
                    f"{meter(snr[1], 0, 40)}  {stxt}", sst, indent=4)
        row("(min / srednia / max w ostatniej sekundzie)")

    if rx_msgs:
        section("Odbior (RX)")
        for name in sorted(rx_msgs, key=stream_key):
            m = rx_msgs[name]
            got, got_all = rx_packets(m, "all")
            fec = rx_packets(m, "fec_rec")[0]
            lost, lost_all = rx_packets(m, "lost")
            bad = rx_packets(m, "bad")[0] + rx_packets(m, "dec_err")[0]
            bytes_s = rx_packets(m, "out_bytes")[0] or rx_packets(m, "all_bytes")[0]
            pct = rx_loss_pct(m)
            st = loss_grade(pct)[0]
            row(f"{name:<16}{got:>7.0f} pkt/s   {mbit(bytes_s):>7.2f} Mbit/s", st)
            row(f"FEC naprawil {fec:.0f}/s   utracone {lost:.0f}/s"
                + (f" ({pct:.1f}%)" if pct is not None else "")
                + f"   bledne {bad:.0f}/s", st, indent=4)
            row(f"od startu uslugi: odebrane {got_all:.0f}, utracone {lost_all:.0f}", indent=4)
        row("(FEC naprawil = pakiety odtworzone z nadmiarowych - doszly, ale link sie meczy)")

    if tx_msgs:
        section("Nadawanie (TX)")
        for name in sorted(tx_msgs, key=stream_key):
            m = tx_msgs[name]
            inj = rx_packets(m, "injected")[0]
            dropped = rx_packets(m, "dropped")[0]
            bytes_s = rx_packets(m, "injected_bytes")[0]
            st = "warn" if dropped > 0 else None
            row(f"{name:<16}{inj:>7.0f} pkt/s   {mbit(bytes_s):>7.2f} Mbit/s   "
                f"odrzucone {dropped:.0f}/s", st)
            for label, w_inj, w_drop, lat in tx_wlan_rows(m, nics):
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
    if worst["lost"]:
        row(f"pakietow utraconych lacznie (od startu uslugi): {worst['lost']:.0f}")

    return lines


def link_test_screen(stdscr):
    """Zywy test lacza: co widac po drugiej stronie, jak mocny jest sygnal,
    ile pakietow przepada i jak dlugo leci ping przez radio. Weryfikacja mowi
    "dziala / nie dziala", a to jest ekran do patrzenia w czasie rzeczywistym -
    przy ustawianiu anten, sprawdzaniu zasiegu albo szukaniu czystszego kanalu.

    Sam odswieza sie kilka razy na sekunde i mozna go zostawic wlaczonego -
    po restarcie uslugi podlaczy sie do niej z powrotem. Na wejsciu pyta, czy
    zapisywac przebieg do pliku; zapis konczy sie z chwila wyjscia stad."""
    stdscr.clear()
    draw_header(stdscr, f"WFB-NG [{ROLE}] - test polaczenia")
    path = TEST_LOG_DIR / f"test-{ROLE}-{time.strftime('%Y%m%d-%H%M%S')}.log"
    recorder = None
    if popup(stdscr, "Zapis testu do pliku",
             ["Zapisywac przebieg tego testu do pliku?",
              "",
              f"Plik:  {path}",
              "Jeden wiersz na sekunde: sygnal, straty, ping.",
              "Zapis konczy sie w chwili wyjscia z ekranu testu."],
             buttons=("Tak", "Nie")) == 0:
        try:
            recorder = TestRecorder(path).open()
        except OSError as e:
            popup(stdscr, "Nie udalo sie otworzyc pliku", [str(e), "Test ruszy bez zapisu."],
                  status="fail")

    stdscr.timeout(400)  # getch wraca po 0.4 s, wiec petla sama sie odswieza
    stats = WfbStatsProbe().start()
    ping = PingProbe(PEER_IP).start()

    nics = wfb_nics()
    used = service_nics(set(nics))
    counters = {nic: (*nic_counters(nic), time.monotonic()) for nic in nics}
    worst = {"rssi": None, "loss": None, "lost": 0}
    started = time.monotonic()
    rec_started = started  # nie zeruje sie klawiszem 'z', wiec czas w pliku rosnie
    next_nic_scan = started + 2.0
    next_sample = started
    rec_error = None
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
            elapsed = now - started
            lines = link_test_lines(metrics, api_error, nics, used, traffic,
                                    ping_snap, worst, elapsed)

            if recorder and now >= next_sample:
                next_sample = now + 1.0
                try:
                    recorder.sample(now - rec_started, metrics, ping_snap)
                except OSError as e:
                    rec_error, recorder = str(e), None  # np. brak miejsca na karcie

            stdscr.erase()
            draw_header(stdscr, f"WFB-NG [{ROLE}] - test polaczenia")
            h, _ = stdscr.getmaxyx()
            view = max(1, h - 3)
            top = max(0, min(top, max(0, len(lines) - view)))
            for i, (text, attr) in enumerate(lines[top:top + view]):
                safe_addstr(stdscr, 2 + i, 2, text, attr)

            hint = "q = powrot, z = zeruj liczniki testu"
            if len(lines) > view:
                hint = (f"strzalki = przewijanie ({top + 1}-{min(top + view, len(lines))}"
                        f"/{len(lines)}), " + hint)
            if recorder:
                hint = f"ZAPIS: {recorder.samples} probek -> {recorder.path.name} | " + hint
            elif rec_error:
                hint = "ZAPIS PRZERWANY (blad pliku) | " + hint
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
                worst.update(rssi=None, loss=None, lost=0)
                ping.reset()
                started = now
                if recorder:
                    recorder.note("wyzerowano liczniki testu")
    finally:
        stats.close()
        ping.close()
        stdscr.timeout(-1)  # z powrotem na blokujace getch, inaczej menu zwariuje

    if recorder:
        try:
            recorder.close(worst, time.monotonic() - rec_started)
            popup(stdscr, "Zapis zakonczony",
                  [f"Plik:    {recorder.path}",
                   f"Probek:  {recorder.samples}   (co sekunde)",
                   "",
                   f"Podglad:      less {recorder.path.name}",
                   f"Sciagniecie:  scp <user>@<ip>:{recorder.path} ."],
                  status="ok")
        except OSError as e:
            popup(stdscr, "Blad przy zamykaniu pliku", [str(e)], status="fail")
    elif rec_error:
        popup(stdscr, "Zapis przerwany", [rec_error, "Czesc probek moze byc w pliku:",
                                          str(path)], status="fail")


def live_mcs_txt(live):
    """Czym nadaja w tej chwili procesy wfb_tx - jedna linia do naglowka."""
    if not live:
        return "brak dzialajacego wfb_tx"
    parts = []
    for tx in live:
        port = str(tx.get("port", "?"))
        parts.append(f"{RADIO_PORT_NAMES.get(port, 'port ' + port)} MCS {tx.get('mcs', '?')}")
    return ", ".join(parts)


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
        "Identyfikacja kart (wypnij dongla)",
        "Klucze i parowanie",
        "Test polaczenia (sygnal, straty, ping)",
        "Wybor modulacji (MCS)",
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
                nic_identify_screen(stdscr)
            elif idx == 4:
                keys_screen(stdscr)
            elif idx == 5:
                link_test_screen(stdscr)
            elif idx == 6:
                modulation_screen(stdscr)
            elif idx == 7:
                verification_screen(stdscr)
            elif idx == 8:
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

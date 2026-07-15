#!/usr/bin/env python3
"""WFB-NG - instalator + pseudo-graficzny (curses) TUI, rola: GS.

Pierwsze uruchomienie (na swiezym Raspberry Pi OS, z podlaczona karta
RTL8812AU) robi caly setup: pakiety systemowe, sterownik karty, klucze
szyfrujace, /etc/wifibroadcast.cfg, usluge systemd. Kolejne uruchomienia
(setup juz gotowy) od razu otwieraja konfigurator/weryfikator.

Uzycie:
    sudo python3 gs.py
"""

import curses
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

ROLE = "gs"
PEER_IP = "10.5.0.2"  # adres drugiej strony (drone) w tunelu
SSH_PORT = 22

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
        log("    juz obecne, pomijam")
        return
    run(["bash", "-c", "cd /etc && wfb_keygen"])
    log("")
    log(f"    !!! Wygenerowano NOWA pare kluczy NA TYM urzadzeniu (rola: {ROLE}).")
    log("    !!! Skopiuj OBA pliki na DRUGIE urzadzenie (nadpisz tam):")
    log("    !!!   scp /etc/drone.key /etc/gs.key <user>@<ip-drugiego-urzadzenia>:/tmp/")
    log("    !!!   # na drugim urzadzeniu:")
    log("    !!!   sudo mv /tmp/drone.key /tmp/gs.key /etc/")
    log("    !!! NIE generuj kluczy ponownie na zadnym z urzadzen - stracisz polaczenie.")
    log("")


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

    dhcpcd = Path("/etc/dhcpcd.conf")
    if dhcpcd.exists() and "denyinterfaces" not in dhcpcd.read_text():
        nics = wfb_nics()
        if nics:
            with dhcpcd.open("a") as f:
                f.write("denyinterfaces " + " ".join(nics) + "\n")

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


# ------------------------- weryfikacja -------------------------

def collect_checks():
    checks = []

    code, out = run(["lsusb"])
    if "8812" in out.lower():
        checks.append(("Karta RTL8812AU", "ok", "wykryta w lsusb"))
    else:
        checks.append(("Karta RTL8812AU", "fail", "nie widac karty 8812 w lsusb"))

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

    code, out = run(["lsmod"])
    if "tun" in out:
        checks.append(("Modul tun", "ok", "zaladowany"))
    else:
        checks.append(("Modul tun", "warn", "niezaladowany - sudo modprobe tun"))

    if wfb_ng_installed():
        checks.append(("Pakiet wfb-ng", "ok", "wfb_keygen obecny"))
    else:
        checks.append(("Pakiet wfb-ng", "fail", "brak wfb_keygen - pakiet niezainstalowany"))

    if DRONE_KEY.exists() and GS_KEY.exists():
        checks.append(("Klucze /etc/*.key", "ok", "obecne"))
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
    else:
        checks.append(("Moc nadawania (TX)", "ok", f"{live_tx}/63"))

    if CFG_PATH.exists():
        txt = CFG_PATH.read_text()
        ch, reg = parse_common(txt)
        checks.append(("wifibroadcast.cfg", "ok", f"kanal={ch} region={reg} rola={ROLE}"))
    else:
        checks.append(("wifibroadcast.cfg", "fail", "plik nie istnieje"))

    code, out = run(["systemctl", "is-active", f"wifibroadcast@{ROLE}"])
    if out.strip() == "active":
        checks.append((f"Usluga wifibroadcast@{ROLE}", "ok", "aktywna"))
    else:
        checks.append((f"Usluga wifibroadcast@{ROLE}", "fail", f"status: {out.strip()}"))

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


def verification_screen(stdscr):
    stdscr.clear()
    draw_header(stdscr, f"WFB-NG [{ROLE}] - weryfikacja")
    safe_addstr(stdscr, 2, 2, "Sprawdzam...")
    stdscr.refresh()

    checks = collect_checks()

    stdscr.clear()
    draw_header(stdscr, f"WFB-NG [{ROLE}] - weryfikacja")
    row = 2
    for name, status, detail in checks:
        icon = STATUS_ICON[status]
        safe_addstr(stdscr, row, 2, icon, color_for(status) | curses.A_BOLD)
        safe_addstr(stdscr, row, 9, name, curses.A_BOLD)
        row += 1
        safe_addstr(stdscr, row, 11, detail[:90])
        row += 2
    pause(stdscr)


def main_menu(stdscr):
    curses.curs_set(0)
    if curses.has_colors():
        init_colors()

    items = [
        "Pokaz biezaca konfiguracje",
        "Zmien kanal / region i zapisz",
        "Uruchom weryfikacje",
        "Wyjdz",
    ]
    idx = 0

    while True:
        stdscr.clear()
        draw_header(stdscr, f"WFB-NG [{ROLE.upper()}] - konfigurator i weryfikator")

        if not (DRONE_KEY.exists() and GS_KEY.exists()):
            safe_addstr(stdscr, 2, 2, "Brak kluczy - cos poszlo nie tak przy instalacji", color_for("fail"))

        for i, item in enumerate(items):
            attr = curses.color_pair(5) if i == idx else curses.A_NORMAL
            safe_addstr(stdscr, 5 + i, 4, item.ljust(50), attr)

        h, _ = stdscr.getmaxyx()
        safe_addstr(stdscr, h - 1, 2, "Strzalki gora/dol, Enter = wybierz, q = wyjscie", curses.A_DIM)
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
                verification_screen(stdscr)
            elif idx == 3:
                break
        elif key in (ord("q"), 27):
            break


def main():
    require_root()
    os.environ.setdefault("DEBIAN_FRONTEND", "noninteractive")

    if not is_fully_installed():
        full_setup()
        print()
        input("Nacisnij Enter, aby przejsc do konfiguratora/weryfikatora...")

    curses.wrapper(main_menu)


if __name__ == "__main__":
    main()

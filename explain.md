# Jak to działa — opis wszystkich funkcji

Dokumentacja trzech plików projektu: `gs.py`, `drone.py` i `podglad_testu.py`.
Opisuje **co robi każda funkcja** i **jak funkcje współpracują ze sobą**.

---

## Spis treści

1. [Z czego składa się projekt](#1-z-czego-składa-się-projekt)
2. [Przepływ danych — jak to działa w całości](#2-przepływ-danych--jak-to-działa-w-całości)
3. [Stałe modułowe](#3-stałe-modułowe)
4. [Pomocnicze — uruchamianie poleceń](#4-pomocnicze--uruchamianie-poleceń)
5. [Karty sieciowe](#5-karty-sieciowe)
6. [Usługa systemd](#6-usługa-systemd)
7. [Plik konfiguracyjny wfb-ng](#7-plik-konfiguracyjny-wfb-ng)
8. [Moc nadawania, kanały, częstotliwości](#8-moc-nadawania-kanały-częstotliwości)
9. [Sieć i diagnostyka](#9-sieć-i-diagnostyka)
10. [Statystyki wfb-ng — odczyt z API](#10-statystyki-wfb-ng--odczyt-z-api)
11. [Modulacja (MCS)](#11-modulacja-mcs)
12. [Naprawa utraconych pakietów (FEC)](#12-naprawa-utraconych-pakietów-fec)
13. [Sondy w tle](#13-sondy-w-tle)
14. [Oceny i formatowanie](#14-oceny-i-formatowanie)
15. [Skan kanałów](#15-skan-kanałów)
16. [Automaty: AutoPeer, AutoChannel, AutoFec](#16-automaty-autopeer-autochannel-autofec)
17. [Instalacja](#17-instalacja)
18. [Klucze i parowanie](#18-klucze-i-parowanie)
19. [Stałe nazwy kart](#19-stałe-nazwy-kart)
20. [Wykrywanie kart przy starcie i weryfikacja](#20-wykrywanie-kart-przy-starcie-i-weryfikacja)
21. [Warstwa curses](#21-warstwa-curses)
22. [Zapis testu do pliku](#22-zapis-testu-do-pliku)
23. [Metryki linku i ekran testu](#23-metryki-linku-i-ekran-testu)
24. [Ekrany TUI](#24-ekrany-tui)
25. [podglad_testu.py](#25-podglad_testupy)
26. [Gdzie co zmienić](#26-gdzie-co-zmienić)

---

## 1. Z czego składa się projekt

| Plik | Gdzie chodzi | Co robi |
|---|---|---|
| `gs.py` | Raspberry Pi na ziemi (**g**round **s**tation) | instalator + TUI (curses) |
| `drone.py` | Raspberry Pi w powietrzu | to samo, rola `drone` |
| `podglad_testu.py` | Windows / dowolny pulpit | rysuje wykresy z logów testu (tkinter) |

**`gs.py` i `drone.py` to ten sam program.** Różnią się wyłącznie ~117 liniami
konfiguracji roli — resztę trzymaj identyczną. Wszystkie opisy poniżej dotyczą
obu plików tak samo.

Co je różni:

| Stała | `gs.py` | `drone.py` |
|---|---|---|
| `ROLE` | `"gs"` | `"drone"` |
| `PEER_IP` | `10.5.0.2` | `10.5.0.1` |
| `PEER_NAME` | `"drone"` | `"gs"` |
| `EXPECTED_NICS` | `1` | `2` |
| `NIC_NAMES` | `["gs_wfb"]` | `["drone_RX", "drone_TX"]` |
| `RX_ONLY_NICS` | `[]` | `["drone_RX"]` |
| `ROLE_SECTION` | `connect://` (odbiera) | `listen://` (nadaje) |

> **Jak wprowadzać zmiany:** zmień `gs.py`, potem przenieś różnicę do `drone.py`:
> ```bash
> git diff -- gs.py | sed 's|/gs\.py|/drone.py|g' | git apply
> ```
> i sprawdź, że różnica między plikami nadal jest wyłącznie rolowa.

---

## 2. Przepływ danych — jak to działa w całości

### Skąd biorą się liczby

```
wfb-ng (usługa systemd)
   │
   │  gniazdo TCP 127.0.0.1:<cli_port>, ramki msgpack
   ▼
WfbStatsProbe._session()        ← wątek w tle, łączy się i czyta bez końca
   │  _consume() → _unpack_msg() → {"rx": {...}, "tx": {...}}
   ▼
WfbStatsProbe.snapshot()        ← migawka dla rysującego
   │
   ▼
link_metrics(msgs, nics)        ← surowe liczniki → wielkości fizyczne
   │  rx_packets(), antenna_rows(), tx_wlan_rows()
   │  liczy m.in. loss, loss_before, saved_pct
   │
   ├──► link_test_lines()   → ekran testu (curses)
   ├──► TestRecorder.sample() → plik test-<rola>-*.log
   ├──► AutoChannel.tick()  → decyzje o kanale
   └──► AutoFec.tick()      → decyzje o naprawie FEC
                                     │
                                     ▼
                            apply_fec_level() → config + restart usługi
```

### Jak liczone są straty

To jest sedno całej „naprawy pakietów". wfb-ng podaje trzy liczniki:

| Licznik | Co znaczy |
|---|---|
| `all` | pakiety, które **doszły** na radiu |
| `fec_rec` | pakiety **zgubione w powietrzu, ale odtworzone** z nadmiarowości |
| `lost` | dziury w numeracji, których FEC **nie dał rady** odtworzyć |

Z tego `link_metrics()` liczy dwie wartości na **wspólnym mianowniku**
(`seen = all + lost`) — i to jest kluczowe, bo tylko wtedy da się je zestawić
na jednym wykresie:

```
straty PRZED naprawą  =  100 × (lost + fec_rec) / seen     ← ile gubi samo radio
straty PO naprawie    =  100 ×  lost            / seen     ← ile zostaje na zawsze
uratowane             =  przed − po                        ← zasługa FEC
```

### Trzy niezależne procesy

1. **TUI** — to, co widać na ekranie; kończy się z terminalem.
2. **Zapis testu w tle** — osobny proces (`--zapis-testu`) w nowej sesji;
   przeżywa zamknięcie TUI. Dogaduje się przez dwa pliki: `.test-<rola>.stan`
   (ile próbek, jak duży plik) i `.test-<rola>.uwagi` (kolejka komentarzy).
3. **Usługa `wifibroadcast@<rola>`** — właściwy wfb-ng, restartowany przy
   zmianie konfiguracji.

---

## 3. Stałe modułowe

### Rola i sieć
| Stała | Znaczenie |
|---|---|
| `ROLE` | `"gs"` / `"drone"` — wchodzi w nazwy sekcji configu i usługi |
| `PEER_IP`, `PEER_NAME` | druga strona w tunelu (`10.5.0.1` ↔ `10.5.0.2`) |
| `SSH_PORT` | port sprawdzany przy teście dostępności drugiej strony |
| `EXPECTED_NICS` | ile kart RTL ma być — inaczej weryfikacja krzyczy |

### Radio
| Stała | Znaczenie |
|---|---|
| `DEFAULT_CHANNEL = "13"` | 2472 MHz — najwyższy legalny w PL/ETSI, zwykle mniej zatłoczony niż 1/6/11 |
| `DEFAULT_REGION = "PL"` | domena regulacyjna |
| `DEFAULT_TX_POWER = "63"` | 0–63; 0 = wartość z EEPROM, 63 = maksimum |
| `DRIVER_TAG`, `APT_RELEASE` | wersja sterownika RTL8812AU i gałąź apt wfb-ng |

### Ścieżki
| Stała | Plik |
|---|---|
| `CFG_PATH` | `/etc/wifibroadcast.cfg` |
| `DRONE_KEY`, `GS_KEY` | `/etc/drone.key`, `/etc/gs.key` |
| `MODPROBE_WFB` | `/etc/modprobe.d/wfb.conf` — blacklisty + moc nadawania |
| `TX_POWER_SYSFS` | parametr modułu do zmiany mocy na żywo |
| `UDEV_NAMES` | `/etc/udev/rules.d/70-wfb-names.rules` — stałe nazwy kart |
| `WFB_DEFAULTS` | `/etc/default/wifibroadcast` |
| `TEST_LOG_DIR` | katalog skryptu — tam lądują logi testu |
| `REBOOT_MARKER` | znacznik „próbowałem już restartu", żeby nie zapętlić |

### Klucze wbudowane
`DRONE_KEY_B64` / `GS_KEY_B64` — stała para kluczy, **identyczna w obu plikach**.
Dzięki temu nic nie trzeba kopiować między urządzeniami (`wfb_keygen` zrobiłby
na każdym Pi inną parę i strony by się nie dogadały).

> ⚠️ To **nie jest sekret** — kto ma skrypt, ten może podsłuchać transmisję.
> Menu ma opcję wygenerowania własnej pary albo sparowania kodem.

### Progi automatów
| Stała | Wartość | Do czego |
|---|---|---|
| `AUTO_BAD_LOSS` | 5.0 % | powyżej tego link uznajemy za zły → zmiana **kanału** |
| `AUTO_BAD_SECONDS` | 8 s | tyle musi być źle, żeby ruszyć kanał |
| `AUTO_ACK_SECONDS` | 3 s | czekanie na potwierdzenie od drugiej strony |
| `AUTO_SETTLE_SECONDS` | 8 s | czekanie, aż link wstanie po skoku |
| `AUTO_SEARCH_DWELL` | 4 s | nasłuch na kanale przy szukaniu drugiej strony |
| `AUTO_FEC_BAD_LOSS` | 1.0 % | powyżej tego dokładamy **nadmiarowości** |
| `AUTO_FEC_GOOD_LOSS` | 0.2 % | poniżej tego nadmiarowość jest zbędna |
| `AUTO_FEC_BAD_SECONDS` | 6 s | tyle musi być źle, żeby dołożyć |
| `AUTO_FEC_GOOD_SECONDS` | 90 s | tyle musi być dobrze, żeby zdjąć |
| `AUTO_FEC_COOLDOWN` | 45 s | minimalny odstęp między zmianami (każda = restart usługi) |
| `AUTO_FEC_REPORT_EVERY` | 2 s | jak często mówimy drugiej stronie, ile od niej gubimy |
| `AUTO_FEC_PEER_STALE` | 12 s | po tylu sekundach ciszy raporty są nieaktualne |

**Dlaczego próg FEC (1 %) jest niższy niż progu kanału (5 %):** naprawa jest
tania i ma zadziałać **zanim** link nadaje się już tylko do ucieczki na inny
kanał.

### Zapis testu
| Stała | Wartość |
|---|---|
| `LOG_SAMPLE_HZ` | 4 — cztery próbki na sekundę |
| `TEST_MAX_BYTES` | 1 GB — po tym zapis kończy się sam, żeby nie zapchać karty |
| `RECORDER_FLAG` | `--zapis-testu` — po tym rozpoznajemy proces zapisu |

---

## 4. Pomocnicze — uruchamianie poleceń

| Funkcja | Co robi |
|---|---|
| `log(msg="")` | `print` z `flush=True` — instalator ma pisać na bieżąco, a nie buforować |
| `run(cmd, timeout=None)` | uruchamia polecenie, zwraca `(kod, stdout+stderr sklejone)`. Brak polecenia → `127`, przekroczony czas → `124`. **Nigdy nie rzuca wyjątkiem** — cały kod woła to bez `try` |
| `run_tool(name, *args, timeout=10)` | jak `run()`, ale próbuje też `/usr/sbin` i `/sbin` — `iw`, `rfkill` i `wfb-nics` często tam leżą, a `sudo` nie zawsze ma je w `PATH` |
| `require_root()` | brak roota → komunikat i `sys.exit(1)` |

> **Uwaga o `run()`:** skleja `stdout` ze `stderr`. Dlatego wszędzie, gdzie
> parsujemy wynik (`wfb_streams()`, `wfb_cli_port()`), kod wycina konkretny
> fragment, zamiast ufać całości — ostrzeżenie importu nie może podmienić danych.

---

## 5. Karty sieciowe

| Funkcja | Co robi |
|---|---|
| `wfb_nics()` | lista interfejsów z `wfb-nics` — to, co wfb-ng uważa za swoje karty |
| `usb_rtl_dongles()` | linie z `lsusb` pasujące do RTL88xx — czyli co **fizycznie** wpięte |
| `nic_usb_slot(nic)` | gniazdo USB, np. `1-1:1.0`. Stałe dla portu niezależnie od karty |
| `nic_mac(nic)` | MAC małymi literami — **na nim wieszamy nazwy**, bo jedzie razem z donglem |
| `nic_details(nic)` | komplet: sterownik, MAC, gniazdo, tryb (monitor?), kanał |
| `nic_counters(nic)` | `(rx_packets, tx_packets)` z `/sys/class/net/<nic>/statistics` |
| `nic_traffic(nics, window=2.0)` | ile pakietów/s **faktycznie** przechodzi — dwa odczyty liczników w odstępie `window` |
| `nic_status_summary(max_age=2.0)` | jedna linia stanu do nagłówka menu, cache'owana |
| `packet_socket_nics(known)` | karty, na których ktoś trzyma otwarte `AF_PACKET` — czyli realnie używane |
| `proc_cmdlines()` | linie poleceń wszystkich procesów prosto z `/proc` (nie przez `ps` — ten obcina) |
| `service_log_nics(known)` | karty przejęte przez usługę w **bieżącym** uruchomieniu (z journala) |
| `service_nics(known)` | interfejsy, których usługa **naprawdę** używa — łączy trzy źródła wyżej |
| `rebind_to_wfb_driver()` | karta pod złym sterownikiem (np. wbudowany `rtw88_8812au`) → przepina pod `88XXau_wfb` |
| `driver_loaded()` | czy `88XXau_wfb` jest w `lsmod` |
| `driver_built()` | czy moduł w ogóle istnieje (`modinfo`) |
| `wfb_ng_installed()` | czy jest `wfb_keygen` — po tym poznajemy, że pakiet wfb-ng siedzi w systemie |
| `usb_power_issues()` | linie z `dmesg` świadczące o problemach z zasilaniem USB |

**Dlaczego tyle sposobów na „które karty":** moduł załadowany w jądrze to nie to
samo co karta skojarzona z interfejsem, a interfejs istniejący to nie to samo co
używany przez usługę. Każda z tych funkcji odpowiada na inne pytanie.

---

## 6. Usługa systemd

| Funkcja | Co robi |
|---|---|
| `service_props()` | `ActiveState`/`SubState` wprost z systemd |
| `service_active(props=None)` | czy `ActiveState == "active"` |
| `service_state_txt(props=None)` | `"active/running"` — do komunikatów |
| `service_last_errors(n=6, scan=300)` | linie z journala, które **coś mówią** — sam ogon zwykle zawiera szum |
| `restart_wfb_service(wait=3.0)` | restart + `time.sleep(wait)` + **skasowanie cache** `_tx_params_cache`. Po restarcie biegną nowe procesy `wfb_tx`, więc stary odczyt z `/proc` opisywałby nieistniejące ustawienia |

---

## 7. Plik konfiguracyjny wfb-ng

`/etc/wifibroadcast.cfg` jest w formacie INI. Trzy funkcje operują na nim
**chirurgicznie**, żeby nie zdeptać reszty pliku:

| Funkcja | Co robi |
|---|---|
| `set_cfg_option(section, key, value_txt)` | ustawia klucz; dopisuje sekcję, gdy jej nie ma; podmienia wartość, gdy klucz już jest |
| `get_cfg_option(section, key)` | wartość albo `None`. **Wycina sekcję dokładnie tak samo** jak dwie pozostałe — czyta więc to, co same zapisują |
| `drop_cfg_option(section, key)` | usuwa klucz; zwraca `True`, gdy coś faktycznie zniknęło |
| `backup_config_once()` | kopia `.bak` przed **pierwszą** naszą ingerencją |

Wyższy poziom:

| Funkcja | Co robi |
|---|---|
| `parse_common(txt)` | kanał i region **wpisane** do pliku |
| `cfg_has_common()` | czy stoją w pliku, czy tylko je zakładamy |
| `wfb_effective_common(max_age=5.0)` | kanał i region tak, jak widzi je wfb-ng **po scaleniu** `master.cfg` + `site.cfg` + naszego. To jest prawda, a nie nasz plik |
| `channel_source_note(channel)` | skąd wziął się kanał, na którym stoi link |
| `wfb_streams()` | lista strumieni profilu **od samego wfb-ng** (osobny interpreter, bo `wfb_ng.conf` cache'uje config przy imporcie) |
| `video_service_type(streams=None)` | tryb usługi wideo |
| `build_config(channel, region)` | treść świeżego configu |
| `save_common_config(channel, region)` | zapis kanału i regionu **bez deptania reszty** |
| `ensure_video_service_type(nics)` | `udp_direct_tx` nie obsłuży kilku kart — przy >1 karcie podmienia `service_type` na `udp_proxy`, inaczej usługa restartuje się w kółko |
| `txpower_cfg_value(nics)` | treść `wifi_txpower` dla `[common]` |
| `ensure_tx_split(nics)` | wymusza, że nadaje tylko karta spoza `RX_ONLY_NICS` |
| `apply_tx_split(nics, say)` | rozdział ról + restart **z wycofaniem**, gdy usługa nie wstanie |

---

## 8. Moc nadawania, kanały, częstotliwości

| Funkcja | Co robi |
|---|---|
| `parse_tx_power()` | zapisana moc z `modprobe.d` |
| `write_modprobe_wfb(tx_power)` | blacklisty konkurencyjnych sterowników + `rtw_tx_pwr_idx_override` |
| `apply_tx_power_live(tx_power)` | 0–63 **natychmiast**, bez przeładowania modułu (przez sysfs) |
| `read_tx_power_live()` | aktualna wartość z sysfs |
| `channel_freq(channel)` | numer kanału → MHz (2.4 GHz i 5 GHz) |
| `channel_span(freq)` | zakres zajmowany przez HT20 — to **on**, a nie sama częstotliwość, musi się zmieścić w domenie |
| `reg_domain_ranges()` | `(kraj, [(od, do), …])` z `iw reg get` |
| `channel_allowed(freq, ranges)` | czy **cały** kanał HT20 mieści się w dozwolonym paśmie |
| `set_nic_channel(nic, channel)` | przestawia jedną kartę (niektóre sterowniki przyjmują tylko część składni — funkcja próbuje wariantów) |
| `set_channel_live(channel)` | przestawia **wszystkie** karty przez `iw`, bez restartu usługi |

---

## 9. Sieć i diagnostyka

| Funkcja | Co robi |
|---|---|
| `ping_stats(ip, count=5, timeout=2)` | ping idzie **przez tunel wfb**, czyli fizycznie przez kartę RTL — mierzy więc realny link, a nie ethernet |
| `ip_addresses()` | `[(interfejs, adres/maska)]` — wszystkie IPv4 poza loopbackiem |
| `check_ssh(ip, port, timeout=3)` | czy da się otworzyć TCP do drugiej strony |
| `wfb_ng_version()` | wersja pakietu z `dpkg-query` |
| `wfb_cli_port()` | port API wfb-ng — pytamy bibliotekę, bierzemy **ostatnią linię będącą samą liczbą** (ostrzeżenia importu nie mogą podmienić portu). Cache'owany |

---

## 10. Statystyki wfb-ng — odczyt z API

wfb-ng wystawia statystyki na TCP jako **ramki msgpack** poprzedzone
4-bajtową długością. Format bywa różny między wersjami, stąd warstwa
pomocnicza, która wszystko znosi:

| Funkcja | Co robi |
|---|---|
| `_to_text(value)` | `bytes` → `str` |
| `_mget(mapping, name, default=None)` | wartość z rozpakowanego msgpacka — starsze wersje biblioteki oddają klucze jako `bytes`, nowsze jako `str` |
| `_num(value, default=0)` | liczba albo `default`; **`bool` odrzuca** (w Pythonie `True` to `int`) |
| `_flatten(value)` | rekurencyjnie spłaszcza zagnieżdżone listy/krotki |
| `_unpack_msg(payload)` | rozpakowanie z `use_list=False` — **istotne**, bo klucze statystyk anten to krotki, a listy nie są hashowalne |
| `rx_packets(msg, name)` | licznik jako `(w ostatniej sekundzie, łącznie)` |
| `bw_mhz(value, default=20)` | szerokość kanału w MHz — nowsze wfb-ng podaje wprost, starsze surowym kodem |
| `mcs_info(mcs, bandwidth=20, short_gi=False)` | `(opis modulacji, prędkość PHY Mbit/s)`. MCS 8–15 to te same modulacje co 0–7, tylko dwa strumienie |
| `antenna_rows(msg, nics)` | statystyki **każdej anteny**: etykieta, pakiety/s, RSSI i SNR jako (min, śr, max), częstotliwość, MCS. Klucz to `(freq, MCS, szerokość, id)`, a `id` koduje kartę w górnym bajcie i antenę w dolnym — stąd da się podpiąć nazwę interfejsu |
| `tx_wlan_rows(msg, nics)` | `[(karta, wstrzyknięte/s, odrzucone/s, opóźnienie ms)]` — czyli która karta nadaje i czy sterownik nadąża |
| `tx_radio_params(max_age=5.0)` | **czym naprawdę nadajemy**: flagi z linii poleceń działających `wfb_tx` (`-M` MCS, `-B` szerokość, `-G` GI, `-S` STBC, `-L` LDPC, `-k`/`-n` FEC, `-p` port). Config mógłby kłamać — zmiana w pliku działa dopiero po restarcie. Cache'owane, bo przejście po całym `/proc` jest drogie |

---

## 11. Modulacja (MCS)

| Funkcja | Co robi |
|---|---|
| `mcs_config_sections()` | `{strumień: sekcja configu}` — tylko dla strumieni, które ta rola **nadaje**. Bierze najbardziej szczegółowy profil (ostatni na liście), bo ten wygrywa przy scalaniu |
| `current_mcs_setting(sections)` | MCS wpisany do configu albo `None` (= „automatycznie"). `None` też wtedy, gdy sekcje mają **różne** wartości |
| `apply_mcs_setting(mcs, sections)` | zapisuje `mcs_index` we wszystkich nadawanych strumieniach; `mcs=None` kasuje wpis |
| `tx_modulation_txt(tx)` | opis nadawania rozbity na dwa kawałki (modulacja / kodowanie) — w jednym nie mieści się na 80 kolumnach |
| `live_mcs_txt(live)` | jedna linia „czym nadają teraz procesy `wfb_tx`" |

`MCS_HINTS` — krótkie podpisy przy skrajnych i środkowym MCS (0 = największy
zasięg, 7 = najwięcej danych).

---

## 12. Naprawa utraconych pakietów (FEC)

> **Podstawa, którą trzeba rozumieć:** pakietu, który przepadł w powietrzu,
> **nie da się naprawić po fakcie** — nikt go już nie ma. wfb-ng zabezpiecza się
> z góry: do każdych `k` pakietów danych dokłada `n−k` nadmiarowych i z
> **dowolnych `k`** odebranych odtwarza całą paczkę. Utracony pakiet wraca więc
> z nadmiarowości, o ile było jej dość.
>
> Ten moduł jest o dobieraniu tego „dość". Im gorszy link, tym więcej
> nadmiarowości trzeba wysyłać — ale każdy nadmiarowy pakiet zjada czas antenowy.

### Drabinka poziomów

`FEC_LEVELS` — lista `(k, n, nazwa)` od najtańszego do najmocniejszego.
Indeks na tej liście to **„poziom naprawy"**, którym rusza `AutoFec`:

| Poziom | k/n | Nazwa | Koszt | Przeżyje utratę |
|---:|---|---|---|---|
| 0 | 1/1 | **wyłączona** | 1.00× | — nic nie wróci |
| 1 | 8/9 | minimalna | 1.13× | 1 z 9 |
| 2 | 4/5 | oszczędna | 1.25× | 1 z 5 |
| 3 | 2/3 | średnia | 1.50× | 1 z 3 |
| 4 | 1/2 | **domyślna wfb-ng** | 2.00× | 1 z 2 |
| 5 | 1/3 | mocna | 3.00× | 2 z 3 |
| 6 | 1/4 | bardzo mocna | 4.00× | 3 z 4 |
| 7 | 1/5 | maksymalna | 5.00× | 4 z 5 |

| Stała | Wartość | Znaczenie |
|---|---|---|
| `FEC_OFF_LEVEL` | 0 | naprawa wyłączona (`n = k`, zero nadmiarowości) |
| `FEC_DEFAULT_LEVEL` | 4 | tyle ma tunel po świeżej instalacji wfb-ng |
| `AUTO_FEC_MIN_LEVEL` | 1 | **najniżej, jak wolno zejść automatowi** |

### Dwa różne „wyłączenia"

To są **dwie różne rzeczy** i ekran je rozróżnia:

| Wybór | Co robi | Skutek |
|---|---|---|
| poziom `wyłączona` (`n = k`) | wpisuje `fec_k = 1`, `fec_n = 1` | świadomie nadajemy **bez ochrony** — każdy zgubiony pakiet przepada bezpowrotnie, na wykresie obie krzywe strat leżą na sobie |
| `bez wpisu` | **kasuje** nasz `fec_k`/`fec_n` | wraca to, co wfb-ng ustawia samo (dla tunelu 1/2) — to wycofanie się z ustawiania, **a nie** wyłączenie naprawy |

**Automat nigdy sam nie wyłącza naprawy.** `AUTO_FEC_MIN_LEVEL = 1` jest podłogą
przy schodzeniu w dół: zdjęcie całej ochrony zamienia każdą następną dziurę w
bezpowrotną stratę, a w powietrzu nie ma jak tego cofnąć szybciej niż przez
restart usługi. **W górę z zera automat wyjdzie normalnie** — jeśli wyłączysz
naprawę ręcznie i włączysz tryb automatyczny, przy stratach sam ją podniesie.

Wyłączenie przydaje się do **zmierzenia, ile gubi samo radio** — przy zerowej
nadmiarowości „przed naprawą" i „po naprawie" to ta sama liczba.

### Funkcje

| Funkcja | Co robi |
|---|---|
| `fec_overhead(k, n)` | `n/k` — ile razy więcej pakietów trzeba wysłać niż danych, czyli **cena naprawy** |
| `fec_off(level)` | czy ten poziom to „bez naprawy" (`n ≤ k`) |
| `fec_level_txt(level)` | `"FEC 1/2 (domyslna wfb-ng, 2.00x pakietow)"`; dla wyłączonej mówi wprost „nic nie dokładamy, nic nie wróci" |
| `fec_choice_txt(choice)` | to samo, ale przyjmuje też `None` = „ustawienie wfb-ng (bez naszego wpisu)" |
| `fec_level_of(k, n)` | numer poziomu dla pary `(k, n)` albo `None`, gdy w configu siedzi coś spoza drabinki — wtedy automat nie ma od czego zacząć |
| `fec_section()` | sekcja configu ze strumieniem tunelu (`<rola>_tunnel`) |
| `current_fec_setting(section=None)` | `(k, n)` **wpisane do configu** albo `None` |
| `apply_fec_setting(k, n, section=None)` | zapisuje `fec_k`/`fec_n`; `k=None` kasuje wpis. **Samo zapisanie nie wystarczy** — `wfb_tx` czyta config przy starcie, więc wołający musi zrestartować usługę |
| `tunnel_tx_port()` | port radiowy tunelu (`stream_tx`) — służy do rozpoznania **właściwego** procesu `wfb_tx`, bo wideo ma zwykle zupełnie inne FEC |
| `live_tunnel_fec()` | `(k, n)` **faktycznie używane** przez `wfb_tx` tunelu. Czytane z `/proc`, nie z configu — żeby było widać, gdy wpis nie zadziałał. Bez listy strumieni nie zgaduje po kolejności portów (trafiłoby czasem w wideo) |
| `_write_fec_choice(choice, section)` | zapis wyboru: numer poziomu albo `None` = usuń wpis |
| `apply_fec_choice(choice, section=None)` | ustawia naprawę **i restartuje usługę**. Zwraca `(ok, tekst)`; `ok` jest prawdą tylko wtedy, gdy usługa wstała **i** `wfb_tx` faktycznie nadaje tym, co zapisaliśmy. **Przy niepowodzeniu wycofuje się** do poprzedniego ustawienia i restartuje jeszcze raz — bez tego jedna wartość, której ta wersja wfb-ng nie przyjmuje, zostawiałaby martwą usługę, a na dronie oznacza to link do odzyskania dopiero na ziemi. Wycofanie jest do surowej pary `(k, n)`, bo w configu mogło siedzieć coś spoza drabinki |
| `fec_status_lines(metrics, saved_total=None)` | wspólne wiersze o naprawie dla ekranu ręcznego i automatycznego: ile gubi radio → ile wraca → co zostaje |

### Skąd biorą się liczby o naprawie

W `link_metrics()`:
```python
seen        = got + lost
loss_after  = 100 * lost           / seen      # metrics["loss"]
loss_before = 100 * (lost + fec)   / seen      # metrics["loss_before"]
saved_pct   = loss_before - loss_after         # metrics["saved_pct"]
```

W `RunTotals` (narastająco od początku testu):
- `per` — straty po naprawie
- `per_before` — jakie byłyby **bez** naprawy
- `saved_pct` — różnica, czyli ile punktów procentowych zdjęła naprawa
- `fec_pct` — ile procent odebranych ramek trzeba było odtworzyć

---

## 13. Sondy w tle

Obie chodzą we **własnym wątku**, bo ekran odrysowuje się częściej, niż
przychodzą dane, i nie może na nie czekać.

### `WfbStatsProbe`

| Metoda | Co robi |
|---|---|
| `__init__()` | zamek, `Event` do zatrzymania, pusty słownik wiadomości, wątek `daemon` |
| `start()` | odpala wątek, zwraca `self` (żeby dało się `WfbStatsProbe().start()`) |
| `close()` | ustawia `_stop` |
| `snapshot()` | `(kopia wiadomości, tekst błędu)` — pod zamkiem |
| `_set_error(msg)` | podmiana komunikatu błędu pod zamkiem |
| `_loop()` | w kółko `_session()`, między próbami 2 s przerwy. Dzięki temu restart usługi nie zabija testu — wątek po prostu łączy się ponownie |
| `_session()` | sprawdza `msgpack`, łączy się z `127.0.0.1:<cli_port>`, czyta do skutku |
| `_consume(buf)` | rozbiera strumień na ramki (4 bajty długości + payload). Ramka >8 MB → to nie ten protokół. **Jedna zepsuta ramka nie może zabić testu** — jest pomijana |

### `PingProbe`

| Metoda | Co robi |
|---|---|
| `__init__(ip, count=3)` | jak wyżej + liczniki wysłanych/odebranych |
| `start()` / `close()` | jak wyżej |
| `reset()` | zeruje liczniki od początku (klawisz `z` na ekranie testu) |
| `snapshot()` | `(rtt, utrata w ostatniej próbie %, utrata od początku %, wysłane, odebrane)` |
| `_loop()` | woła `ping` i parsuje regexpem `packets transmitted` oraz `min/avg/max` |

---

## 14. Oceny i formatowanie

Wszystkie zwracają `(status, tekst)`, gdzie status to `"ok"` / `"warn"` /
`"fail"` / `None` — a `color_for()` zamienia go na kolor. **Te same progi są w
`podglad_testu.py`**, żeby ten sam sygnał nie był tu zielony, a tam słaby.

| Funkcja | Progi |
|---|---|
| `rssi_grade(rssi)` | ≥ −50 doskonały, ≥ −65 dobry, ≥ −75 słaby, niżej „na granicy zasięgu" |
| `loss_grade(pct)` | < 0.5 % znikome, < 3 % zauważalne, wyżej duże |
| `snr_grade(snr)` | ≥ 20 czysto, ≥ 10 „szum blisko sygnału", niżej „sygnał tonie w szumie" |
| `worst_status(statuses)` | najgorszy z podanych — do oceny zbiorczej |
| `mbit(bytes_per_s)` | bajty/s → Mbit/s |
| `meter(value, lo, hi, width=18)` | pasek `[####------]`. Dla wielkości, które **lepiej mieć małe** (straty, ping), podaje się `lo`/`hi` na odwrót — pełny pasek zawsze znaczy „dobrze" |
| `human_size(n)` | bajty → `B` / `kB` / `MB` / `GB` |
| `fmt_mmss(seconds)` | sekundy → `mm:ss` albo `h:mm:ss` |

---

## 15. Skan kanałów

| Funkcja | Co robi |
|---|---|
| `iw_survey(nic)` | `{MHz: (aktywny_ms, zajęty_ms, szum_dBm)}` z `iw survey dump`. W trybie monitor to **jedyny** sposób pomiaru — zwykłego skanowania sieci karta w tym trybie nie zrobi |
| `scan_channels(nic, channels, dwell=1.2, on_result=None)` | przechodzi po kanałach i mierzy, ile się na każdym dzieje. `on_result` pozwala pokazywać wyniki na żywo |
| `rank_channels(results)` | od najlepszego: najmniej zajęte pasmo, przy remisie niższy szum |
| `auto_candidates(scan_results, current, ranges=None)` | kolejność prób dla trybu automatycznego — najpierw najcichsze ze skanu |

> ⚠️ Przez **cały skan karta jest poza kanałem linku**, czyli nie ma połączenia.

---

## 16. Automaty: AutoPeer, AutoChannel, AutoFec

### Zasada projektowa

`AutoChannel` i `AutoFec` **niczego same nie dotykają** — dostają czas i pomiary,
oddają listę decyzji. Ekran je wykonuje. Dzięki temu:
- da się je sprawdzić bez sprzętu (są na to testy),
- pomyłka przy skoku kanału kosztuje cały link, więc logika musi być testowalna.

Decyzje to krotki: `("send", tekst)`, `("note", tekst)`, `("hop", kanał, powód)`,
`("persist", kanał)`, `("fec", poziom, powód)`.

### `AutoPeer` — transport

Małe datagramy UDP w tunelu wfb (port `AUTO_PORT = 14570`). **Nie szyfrujemy ich
osobno:** tunel jest już szyfrowany kluczami wfb-ng, a kto jest w środku, ten i
tak może więcej niż przestawić kanał.

| Metoda | Co robi |
|---|---|
| `start()` | otwiera gniazdo z `SO_REUSEADDR`, timeout 0.2 s; błąd ląduje w `self.error` zamiast wyjątku |
| `close()` | zatrzymuje wątek i zamyka gniazdo |
| `send(text)` | wysyła; `OSError` **celowo pomijany** — tunel właśnie nie działa i o to w tym trybie chodzi |
| `take()` | zabiera i czyści skrzynkę odbiorczą |
| `peer_seen_ago()` | ile sekund temu druga strona się odezwała (albo `None`) |
| `_loop()` | odbiera w kółko; skrzynka przycinana do 32 wiadomości |

### `AutoChannel` — dobór kanału

Zasady, które wynikają z fizyki:
- kanału **nie zmieniamy**, dopóki link jest dobry;
- **nie skaczemy bez potwierdzenia** — skok w ciemno to pewna utrata łączności;
- po skoku obie strony same wracają, jeśli link nie wstał;
- szuka **tylko gs**; dron zostaje na swoim kanale, żeby było gdzie go znaleźć.

Stany: `ok` → `propose` (czekam na `SWITCH-OK`) → `settle` (czy link wstał?) →
`ok`. Przy zupełnej ciszy `search` — obchód kanałów z nasłuchem.

| Metoda | Co robi |
|---|---|
| `_next_candidate()` | następny kanał spoza czarnej listy; gdy wszystko wypróbowane, lista się czyści |
| `_hop(target, reason, now, out)` | zapamiętuje poprzedni kanał i wchodzi w `settle` |
| `tick(now, alive, loss, messages=())` | cała logika stanów |
| `_on_message(text, now, out)` | `SWITCH` → odsyła `SWITCH-OK` i skacze; `SWITCH-OK` → skacze, jeśli zgadza się cel; `HELLO` → `HELLO-OK` |

> **Pułapka, na którą jest zabezpieczenie:** tuż po skoku `alive` opisuje jeszcze
> **stary** kanał. Bez `if self.state == "settle" and self.state_since == now`
> automat stwierdzałby „link wstał" natychmiast po skoku na martwy kanał i nigdy
> nie wracał na działający.

### `AutoFec` — dobór naprawy

> **Rzecz, którą najłatwiej zrobić źle:** straty mierzymy na **odbiorze**, a
> ustawiamy FEC **nadawania**. To dwa różne kierunki. Nasze `fec_k`/`fec_n`
> decyduje o tym, ile odratuje **druga strona**, a nie my — więc pytamy o to ją.

Każda strona nadaje raport `LOSS <po> <przed>` i dobiera nadmiarowość pod to, co
usłyszy z powrotem. Gdy druga strona milczy (nie ma tam włączonego ekranu albo
tunel leży), wracamy do własnego odbioru i zakładamy link symetryczny — gorsze
niż raport, ale znacznie lepsze niż nierobienie niczego.

| Metoda | Co robi |
|---|---|
| `__init__(level, role, now)` | poziom (indeks w `FEC_LEVELS` albo `None`), liczniki, znaczniki czasu |
| `peer_fresh(now)` | czy raport drugiej strony nie jest przeterminowany |
| `judged(now, loss_after, loss_before)` | `(po, przed, skąd)` — pomiar, na którym opieramy decyzję. **Raport drugiej strony ma pierwszeństwo**, bo opisuje właściwy kierunek |
| `_apply(level, reason, now, out)` | zmiana poziomu + reset liczników + `("fec", …)` |
| `tick(now, loss_after, loss_before, messages=())` | wysyła własny raport, ocenia, decyduje |
| `_on_message(text, now)` | parsuje `LOSS <po> <przed>` |

**Logika `tick()` po kolei:**
1. odbierz raporty drugiej strony;
2. co `AUTO_FEC_REPORT_EVERY` wyślij **swój** raport;
3. brak poziomu (FEC spoza drabinki) → ostrzeż **raz** i nic nie rób;
4. brak pomiaru → nic (cisza na linku to nie powód do zmian);
5. w cooldownie → nic (po restarcie liczniki są jeszcze zimne);
6. `po ≥ AUTO_FEC_BAD_LOSS` przez `AUTO_FEC_BAD_SECONDS` → **poziom w górę**
   (na szczycie drabinki: „to już na kanał albo antenę");
7. `przed ≤ AUTO_FEC_GOOD_LOSS` przez `AUTO_FEC_GOOD_SECONDS` → **poziom w dół**,
   ale **nigdy poniżej `AUTO_FEC_MIN_LEVEL`** — naprawę wyłącza się ręcznie.

**Dlaczego dwa różne wskaźniki do dwóch kierunków:**
- **w górę** patrzymy na straty **po** naprawie — to one bolą;
- **w dół** na straty **przed** naprawą, bo po naprawie zawsze jest zero i
  automat schodziłby w dół aż do pierwszej dziury.

Skutek: gdy radio gubi 6 %, ale FEC to nadrabia do 0 %, automat **zostaje na
swoim poziomie** — naprawa robi swoje i nie ma jej po co zabierać.

**Czas jednego kroku:** w górę `cooldown + 6 s` ≈ 51 s, w dół `cooldown + 90 s`
≈ 135 s. W górę szybko, w dół powoli — za mała nadmiarowość kosztuje utracone
pakiety od razu, za duża tylko trochę czasu antenowego.

---

## 17. Instalacja

Cała jest **idempotentna** — każdy krok najpierw sprawdza, czy nie jest już
zrobiony. Można ją puszczać wielokrotnie.

| Funkcja | Krok |
|---|---|
| `is_fully_installed()` | czy wszystko na miejscu: sterownik **załadowany i skojarzony z kartą**, wfb-ng, oba klucze, config |
| `step_packages()` | [1/7] `git`, `build-essential`, nagłówki jądra, `iw`, `rfkill`… |
| `step_rfkill()` | [2/7] `rfkill unblock all` |
| `step_driver()` | [3/7] sterownik RTL8812AU — klonuje i buduje, jeśli trzeba |
| `step_tun()` | [4/7] moduł `tun` + wpis w `/etc/modules` |
| `step_wfb_ng_package()` | [5/7] klucz GPG, repo apt, instalacja `wfb-ng` |
| `step_keys()` | [6/7] klucze — wbudowane, jeśli mają poprawny format |
| `step_config()` | [7/7] blacklisty (**zawsze odświeżane**, bo jądra 6.x mają wbudowany `rtw88_8812au`, który przechwytuje kartę przy każdym boocie), moc, sysctl, config, usługa |
| `full_setup()` | wszystkie siedem po kolei |
| `ensure_nm_unmanaged(nics)` | Raspberry Pi OS od bookworma używa NetworkManagera — karty wfb muszą być spod niego wyjęte |
| `ensure_dhcpcd_deny(nics)` | to samo dla dhcpcd, **per interfejs** |
| `release_nics_from_network_stack(nics)` | zbiorczo: zdejmij karty spod czegokolwiek, co zarządza siecią |

---

## 18. Klucze i parowanie

Parowanie kodem: obie strony wpisują ten sam 8-znakowy kod i **liczą z niego
identyczną parę kluczy** — bez przenoszenia plików między urządzeniami.

| Funkcja | Co robi |
|---|---|
| `_cswap(swap, a, b)` | zamiana warunkowa **bez rozgałęzienia** — element implementacji X25519 |
| `x25519(scalar, u_bytes=None)` | mnożenie skalarne na Curve25519 (`u_bytes=None` = punkt bazowy). Własna implementacja, żeby nie ciągnąć zależności |
| `new_pairing_code()` | 8 znaków z `PAIRING_ALPHABET`, przez `secrets` |
| `format_pairing_code(code)` | `ABCD-EFGH` |
| `normalize_pairing_code(text)` | wybacza małe litery, spacje i myślniki; zwraca 8 znaków albo `None` |
| `derive_keys_from_code(code)` | z jednego kodu **obie strony liczą tę samą parę** |
| `apply_pairing_code(code)` | zapisuje klucze i sam kod (żeby dało się go potem pokazać) |
| `read_pairing_code()` | odczyt zapisanego kodu |
| `key_mode()` | skąd pochodzą klucze w `/etc`: `(tryb, kod)` — `sparowane` / `wbudowane` / `własne` |
| `write_builtin_keys()` | zapisuje wbudowaną parę, `chmod 600` |
| `using_builtin_keys()` | czy w `/etc` leżą wbudowane |
| `builtin_keys_format_ok()` | czy mają układ taki jak z `wfb_keygen` (64 B = 32 B własny tajny + 32 B publiczny drugiej strony) |
| `generate_own_keys()` | własna, prywatna para — bezpieczniejsza, ale trzeba ją przenieść |
| `key_fingerprint(path)` | krótki odcisk do porównania **gołym okiem** między urządzeniami |

---

## 19. Stałe nazwy kart

Zamiast `wlanX` (numer zależy od kolejności wykrycia) karty dostają stałe nazwy
przypięte regułą udev do **MAC-a**, więc nazwa jedzie razem z donglem — także po
przełożeniu do innego portu USB.

| Funkcja | Co robi |
|---|---|
| `parse_name_rules()` | `{kotwica: nazwa}` z naszego pliku reguł |
| `nic_anchors(nics)` | `{interfejs: kotwica}` — domyślnie MAC, zapasowo gniazdo USB (dla kart bez czytelnego lub z powtórzonym MAC-iem) |
| `plan_nic_names(nics)` | przydziela nazwy. **Raz ustalone przypisanie zostaje** — inaczej po każdym boocie karty zamieniałyby się rolami |
| `write_name_rules(by_anchor)` | zapis pliku udev + `udevadm control --reload-rules`. Zwraca `False`, gdy treść się nie zmieniła |
| `rename_nic(old, new)` | zmiana nazwy — jądro pozwala **tylko interfejsowi w stanie DOWN** |
| `update_wfb_defaults(renames)` | podmiana nazw w `WFB_NICS`, jeśli plik wymienia karty wprost |
| `ensure_nic_names()` | całość: zaplanuj, zapisz reguły, przemianuj, popraw `/etc/default` |

---

## 20. Wykrywanie kart przy starcie i weryfikacja

| Funkcja | Co robi |
|---|---|
| `detect_nics_startup()` | odpalane przy **każdym** starcie, przed TUI: czy są wszystkie karty, czy pod właściwym sterownikiem, czy usługa je widzi. Potrafi przepiąć sterownik i zrestartować usługę; `REBOOT_MARKER` pilnuje, żeby nie zapętlić restartów |
| `collect_checks()` | lista `(nazwa, status, szczegół)` — dongle w `lsusb`, zasilanie USB, sterownik, tryb monitor, kanał, usługa, klucze, ruch na kartach, tunel, ping do drugiej strony |
| `config_overview_lines()` | wszystko, co warto mieć pod ręką na jednym ekranie: adresy IP, kanał, region, moc, klucze, karty, stan usługi |

---

## 21. Warstwa curses

| Funkcja | Co robi |
|---|---|
| `init_colors()` | pary: 1 zielony, 2 czerwony, 3 żółty, 4 nagłówek (czarny na cyjanie), 5 zaznaczenie (czarny na białym) |
| `color_for(status)` | `"ok"`/`"warn"`/`"fail"` → para kolorów |
| `safe_addstr(win, y, x, text, attr=0)` | pisanie **z przycinaniem do szerokości** i połykaniem `curses.error`. Bez tego program wywala się przy zmniejszeniu terminala |
| `draw_header(stdscr, title)` | pasek tytułu na całą szerokość |
| `pause(stdscr, msg)` | „naciśnij dowolny klawisz" |
| `scroll_view(stdscr, title, lines)` | prosty pager na listę `(tekst, atrybut)` — treść bywa dłuższa niż ekran |
| `popup(stdscr, title, lines, buttons=("OK",), status=None, default=0)` | okienko z przyciskami: strzałki lewo/prawo, Enter zatwierdza, pierwsza litera to skrót, **Esc zawsze wybiera ostatni przycisk** (czyli „Nie"). Zwraca indeks |
| `prompt_line(stdscr, y, label, default)` | wczytanie linii tekstu z wartością domyślną |

---

## 22. Zapis testu do pliku

### Dlaczego osobny proces

Przy sprawdzaniu zasięgu wyniku nie da się oglądać na bieżąco (jest się kilkaset
metrów od ekranu). Zapis chodzi więc **w osobnym procesie w nowej sesji** —
trwa dalej po wyjściu z ekranu testu, a nawet po zamknięciu całego programu.

TUI dogaduje się z nim przez dwa pliki obok logu (nazwy z kropką, żeby nie
mieszały się przy zwykłym `ls`):

| Funkcja | Co robi |
|---|---|
| `write_test_state(**fields)` | plik stanu podmieniany **w całości** przez `os.replace` — TUI czytające go kilka razy na sekundę nigdy nie trafi na wersję zapisaną w połowie |
| `test_state()` | stan albo `None`. `"trwa"` jest prawdą **tylko gdy proces faktycznie żyje** — inaczej po zaniku zasilania wisiałby napis o teście, którego niczym nie da się zamknąć |
| `_pid_recording(pid)` | czy pod tym PID-em siedzi naprawdę **nasz** proces (sprawdza `RECORDER_FLAG` w `cmdline`) — sam fakt, że proces żyje, nie wystarcza, bo numer może już należeć do czegoś innego |
| `start_test_recorder(path)` | odpala zapis w **nowej sesji** (`setsid`), inaczej zginąłby razem z terminalem |
| `stop_test_recorder(timeout)` | grzeczne zatrzymanie sygnałem — proces sam dopisuje podsumowanie |
| `note_test_recorder(text)` | dopisuje uwagę do kolejki (TUI nie ma logu otwartego) |
| `take_test_notes()` | zabiera kolejkę i kasuje plik |

### `Stat`

Min / średnia / max liczone na bieżąco, **bez trzymania próbek** (test może
trwać godzinami).

| Metoda | Co robi |
|---|---|
| `add(value)` | dorzuca próbkę; `None` pomija |
| `line(fmt)` | `"min 12.0   srednio 18.3   max 41.0"` albo `"brak danych"` |

### `RunTotals`

wfb-ng podaje sumy **od startu usługi**, a nie od chwili, w której zaczęliśmy
patrzeć. Typowy przypadek: test włącza się pierwszy, a nadawanie rusza chwilę
później — liczniki mają wtedy historię, która nie ma nic wspólnego z pomiarem.
Dlatego zapamiętujemy stan z pierwszej próbki i liczymy **przyrost**.

| Metoda / właściwość | Co robi |
|---|---|
| `reset()` | zeruje bazę, przeniesienie i sumy |
| `update(metrics)` | liczy przyrost od bazy. **Gdy usługa się zrestartuje**, liczniki lecą od zera i różnica wyszłaby ujemna — wtedy dotychczasowy dorobek idzie do `_carry`, baza się zeruje i `restarts` rośnie, żeby PER z całego przelotu się nie zgubił |
| `per` | straty **po** naprawie; `None`, dopóki nic nie przyszło (zero byłoby tu kłamstwem) |
| `per_before` | straty, jakie byłyby **bez** naprawy |
| `saved_pct` | różnica — ile punktów procentowych zdjęła naprawa |
| `fec_pct` | ile procent odebranych ramek trzeba było odtworzyć |

### `TestRecorder`

Plik rozdzielony średnikami, więc otwiera się i w notatniku, i w arkuszu.

| Metoda | Co robi |
|---|---|
| `open()` | tworzy plik i pisze nagłówek. Może rzucić `OSError` — wołający pokazuje to w okienku, a test idzie dalej bez zapisu |
| `_header()` | rola, czas startu, host, jądro, wersja wfb-ng, kanał, region, moc, klucze i odcisk, karty, parametry nadawania, **naprawa pakietów w tunelu (FEC)**, druga strona, tempo próbkowania, limit rozmiaru |
| `note(text)` | komentarz w środku pliku — widać, że w tym miejscu coś się zmieniło |
| `sample(elapsed, metrics, ping)` | jeden wiersz danych + dorzucenie do `Stat`-ów i `RunTotals` |
| `close(reason, elapsed)` | podsumowanie: czas, próbki, rozmiar, statystyki RSSI / strat przed / strat po / pingu, PER, **ile pakietów uratowała naprawa**, błędne ramki, restarty usługi, rozkład MCS |

**Kolumny logu** (`COLUMNS`):

| Kolumna | Znaczenie |
|---|---|
| `czas`, `sek` | zegar i sekunda od startu (z ułamkiem — przy 4 Hz musi być) |
| `rssi_best_dBm`, `snr_best_dB` | najlepsza antena |
| `rx_mcs`, `rx_bw_MHz` | modulacja odbioru |
| **`straty_przed_%`** | straty **przed** naprawą FEC |
| `straty_%` | straty **po** naprawie |
| **`uratowane_%`** | różnica — zasługa naprawy |
| **`per_przed_%`** | PER od początku testu, jaki byłby bez naprawy |
| `per_%` | PER od początku testu |
| `rx_pkt_s`, `rx_Mbit_s` | przepływ odbioru |
| `fec_naprawil_s` | pakiety/s odtworzone z nadmiarowości |
| `utracone_s` | pakiety/s stracone bezpowrotnie |
| `ping_ms`, `ping_utrata_%` | ping przez tunel |
| `anteny_rssi` | RSSI każdej anteny osobno |

> Kolumny **pogrubione** są nowe. Stare logi ich nie mają — podgląd wykrywa to
> sam (`TestLog.has()`) i po prostu pomija odpowiednie krzywe.

### `background_recorder(path)`

Proces zapisu: własne sondy (statystyki wfb-ng + ping), cztery próbki na
sekundę, obsługa sygnałów, dopisywanie uwag z kolejki, pilnowanie limitu
rozmiaru, aktualizacja pliku stanu.

---

## 23. Metryki linku i ekran testu

### `link_metrics(msgs, nics)`

Serce odczytu. Zamienia surowe wiadomości API na liczby. **Osobno od
rysowania**, bo dokładnie te same liczby idą na ekran, do pliku i do automatów.

Zwraca słownik m.in. z:

| Klucz | Znaczenie |
|---|---|
| `rx`, `tx` | surowe wiadomości |
| `ants` | wiersze anten |
| `mcs`, `bw` | **przeważająca** modulacja (liczona po pakietach, bo przy zmianie ustawień po drugiej stronie potrafią się chwilowo mieszać) |
| `best_rssi`, `best_snr` | **najlepsza** antena — wfb-ng i tak składa strumień z tej, która słyszy lepiej |
| `loss` | straty po naprawie [%] |
| `loss_before` | straty przed naprawą [%] |
| `saved_pct` | ile punktów procentowych zdjęła naprawa |
| `rx_pps`, `rx_bytes` | przepływ |
| `fec` | pakiety/s odtworzone |
| `lost`, `bad` | stracone i błędne/nieodszyfrowane |
| `rx_total`, `lost_total`, `fec_total`, `bad_total` | sumy od startu usługi (dla `RunTotals`) |

### `link_test_lines(...)`

Cała treść ekranu testu jako lista `(tekst, atrybut)`, budowana **od nowa przy
każdym odświeżeniu** — wszystkie liczby są chwilowe. Wyjątkiem są `worst` i
`run`, które pamiętają cały przebieg.

Sekcje: ocena zbiorcza → sygnał odbierany → odbiór (RX) z wierszem
**„straty przed naprawą → po naprawie (uratowane …)"** → PER od początku testu z
wierszem **„uratowane przez naprawę: N pakietów"** → nadawanie (TX) → karty →
ping → tunel.

---

## 24. Ekrany TUI

| Funkcja | Ekran |
|---|---|
| `main_menu(stdscr)` | menu główne. Przy zapisie w tle odświeża się **samo co sekundę**, żeby licznik próbek szedł do przodu; `erase()` zamiast `clear()`, bo pełne czyszczenie migałoby |
| `show_config_screen(stdscr)` | bieżąca konfiguracja (pager) |
| `redetect_screen(stdscr)` | ta sama naprawa co przy starcie, ale z menu — po wpięciu dongla |
| `nic_identify_screen(stdscr)` | **żywy podgląd kart**: wypnij dongla, a ekran powie, która nazwa właśnie zniknęła. Dwa identyczne dongle 8812AU wyglądają tak samo i inaczej nie da się ich rozróżnić |
| `nic_role_txt(nic)` / `nic_snapshot()` | pomocnicze do powyższego (lekko, bez wołania `iw`) |
| `keys_screen(stdscr)` | klucze i parowanie: stan, odcisk, wpisanie kodu, wygenerowanie własnych |
| `show_pairing_code_screen(stdscr, code)` | kod w ramce do przepisania |
| `radio_settings_screen(stdscr)` | region i moc nadawania (kanału **tu się nie ustawia**) |
| `link_test_screen(stdscr)` | żywy test łącza + start/stop zapisu w tle, `z` zeruje liczniki |
| `channel_screen(stdscr)` | wybór kanału z podpowiedzią, który wolny |
| `channel_rows(...)` | wiersze listy kanałów: numer, MHz, pasmo, legalność, zajętość ze skanu |
| `channel_scan_screen(stdscr, nic, previous)` | skan pasma z podglądem na żywo; kanał i usługa **wracają na swoje** po wyjściu |
| `apply_channel(stdscr, channel, region, ranges)` | zapis + restart + sprawdzenie, na czym karta **faktycznie** stoi |
| `auto_channel_screen(stdscr, scanned)` | tryb automatyczny kanału — pętla `AutoChannel` |
| `modulation_screen(stdscr)` | wybór MCS; po zapisie odczytuje z powrotem, czym `wfb_tx` **naprawdę** nadaje |
| **`repair_screen(stdscr)`** | **naprawa utraconych pakietów** — ręczny wybór poziomu FEC; `a` = tryb automatyczny, `d` = domyślne, **`w` = wyłącz** |
| **`auto_repair_screen(stdscr, level, section)`** | tryb automatyczny naprawy — pętla `AutoFec` + wymiana raportów z drugą stroną |
| `verification_screen(stdscr)` | wynik `collect_checks()` z przewijaniem |
| `background_test_popup(stdscr)` | co z zapisem w tle: zakończ / pokaż wynik |
| `stop_test_popup(stdscr)` / `test_result_popup(stdscr, state)` | zatrzymanie i podsumowanie |
| `test_state_line(state)` / `test_state_attr(state)` | jedna linijka o zapisie w tle — ta sama w menu i na ekranie testu |
| `confirm_exit(stdscr)` | wyjście **nie zatrzymuje** zapisu w tle — ekran musi o tym powiedzieć, inaczej łatwo zostawić proces piszący do skutku |
| `main()` | tryb `--zapis-testu` (bez ekranu) albo: root → instalacja, jeśli trzeba → wykrycie kart → menu |

### Pozycje menu

```
0 Pokaz biezaca konfiguracje          → show_config_screen
1 Wykryj karty ponownie (naprawa)     → redetect_screen
2 Identyfikacja kart (wypnij dongla)  → nic_identify_screen
3 Klucze i parowanie                  → keys_screen
4 Test polaczenia                     → link_test_screen
5 Kanal i czestotliwosc               → channel_screen
6 Wybor modulacji (MCS)               → modulation_screen
7 Naprawa utraconych pakietow (FEC)   → repair_screen      ← nowe
8 Uruchom weryfikacje                 → verification_screen
9 Wyjdz                               → confirm_exit
```

> Dodając pozycję, pamiętaj o **obu** listach: `items` i drabince `elif idx == …`.

---

## 25. `podglad_testu.py`

Program na pulpit (tkinter, **bez żadnych bibliotek do doinstalowania**).
Rysuje log testu na wykresach na wspólnej osi czasu.

### Stałe

| Stała | Znaczenie |
|---|---|
| `SERIES_COLORS` | 6 kolorów: niebieski, pomarańczowy, zielony, fioletowy, morski, czerwony |
| `RSSI_BANDS`, `LOSS_BANDS` | tła paneli — **te same progi co w ocenie w `gs.py`** |
| `PAD_L/R/T/B`, `PANEL_GAP`, `PANEL_MIN_H` | geometria; tytuł i legenda idą **nad** ramkę, bo w środku zasłaniałyby wykres |

### Funkcje pomocnicze

| Funkcja | Co robi |
|---|---|
| `_to_float(text)` | tekst → liczba albo `None` (puste pole w logu nie może być zerem) |
| `as_paths(value)` | jedna ścieżka albo kilka — **zawsze lista**. Bez tego pojedynczy napis rozsypałby się na pojedyncze znaki, bo `str` też jest iterowalny |
| `nice_step(span, target=5)` | odstęp między kreskami osi: 1, 2, 2.5, 5, 10 × potęga dziesiątki — żeby na osi stawały okrągłe liczby, a nie 3.7 albo 812 |
| `nice_time_step(span, target=8)` | to samo dla czasu, ale z listy `1, 2, 5, 10, 15, 30, 60, …` — „co 15 s" czyta się dużo lepiej niż „co 25 s", które wyszłoby z potęgi dziesiątki |
| `fmt_time(seconds)` | sekundy → `m:ss` |

### `TestLog` — czytanie pliku

| Metoda | Co robi |
|---|---|
| `_parse()` | rozkłada plik na nagłówek, próbki, zdarzenia i podsumowanie. Komentarz **przed** kolumnami to nagłówek, **między próbkami** to zdarzenie, **po `---`** to podsumowanie |
| `_parse_antennas(text)` | `"gs_wfb:ant0=-61"` → `{"gs_wfb ant0": -61.0}` |
| `span()` / `duration()` | zakres i długość testu |
| `series(column)` | `[(sekunda od startu, wartość)]` z pominięciem pustych |
| `antenna_series(name)` | to samo dla jednej anteny |
| `has(column)` | czy kolumna ma choć jedną wartość — **tym wykrywane są stare logi** bez nowych kolumn |
| `stat(column)` | `(min, średnia, max)` |
| **`integral(column)`** | **ile tego było ŁĄCZNIE** z kolumny podanej „na sekundę" — np. ile pakietów w sumie naprawił FEC. Liczy po odstępach między próbkami, a nie przez pomnożenie przez ich liczbę (zapis potrafi chodzić w innym tempie, niż deklaruje nagłówek). Przerwy >5 s są pomijane — po przerwie ostatnia znana wartość nie opisuje tego, co działo się w międzyczasie |

`t0` — czas liczony **od początku każdego testu**, a nie z zegara; inaczej dwa
przeloty zrobione o różnych porach leżałyby na wykresie obok siebie zamiast
jeden na drugim.

### `Panel` — jeden wykres

| Metoda | Co robi |
|---|---|
| `compute_range()` | zakres osi Y z 12 % zapasu; `y_floor` trzyma zero na dole (straty i ping nie schodzą poniżej) |
| `y_at(value)` | wartość → piksel |

### `ChartArea` — płótno

| Metoda | Co robi |
|---|---|
| `set_logs(logs)` | podmiana danych i przerysowanie |
| `log_color(index)` | kolor pliku przy porównaniu |
| `_build_panels()` | **buduje listę paneli** — patrz niżej |
| `redraw()` | układa panele jeden pod drugim wg wag; przy małym oknie płótno robi się wyższe niż widok i wchodzi suwak |
| `x_at(t)` | czas → piksel |
| `_draw_panel(panel, last)` | tło, pasma, siatka, znaczniki zdarzeń, krzywe, tytuł, legenda |
| `_line_coords(panel, points)` | współrzędne łamanej; `step=True` rysuje schodki (MCS) |
| `_on_motion(event)` | pionowy krzyżyk, kropki na krzywych i ramka z odczytem |
| `_readout_lines(picks)` | treść odczytu: przy jednym pliku wszystko po kolei, przy porównaniu jedna gęsta linia na plik (inaczej ramka zasłania pół wykresu) |
| `_draw_readout(picks, sx, sy)` | rysuje ramkę po tej stronie kursora, po której się mieści |

### Panele i naprawa pakietów

`_build_panels()` działa różnie zależnie od liczby plików, bo **kolor znaczy co
innego**: przy jednym pliku oznacza wielkość, przy kilku — plik.

**Jeden plik:**

| Panel | Krzywe |
|---|---|
| Sygnał RSSI | każda antena osobno + „najlepsza" na czarno |
| SNR | jedna |
| Modulacja | schodki |
| **Straty pakietów — przed naprawą i po naprawie** | **czerwona „przed naprawa"**, **niebieska „po naprawie"**, fioletowa „PER od początku" |
| **Uratowane przez naprawę [pkt/s]** | zielona, z `fec_naprawil_s` |
| Przepływ odbioru | jedna |
| Ping przez tunel | jedna |

> **Pole między czerwoną a niebieską krzywą to dokładnie pakiety uratowane
> przez FEC.** Bez tej pary z wykresu strat nie da się odczytać, czy link jest
> czysty, czy tylko dobrze łatany.

**Kilka plików (porównanie):** „przed naprawą" dostaje **osobny panel**, żeby na
każdym panelu została jedna krzywa na plik — dwie krzywe na plik by się zlały.

### `App` — okno

| Metoda | Co robi |
|---|---|
| `_build_menu()` / `_build_widgets()` | menu, pasek przycisków, panel boczny, płótno z suwakiem |
| `newest_log()` | bez argumentu otwiera **najświeższy** log z katalogu programu — żeby dało się po prostu kliknąć w plik |
| `_ask_files(title)` | okno wyboru pozwala zaznaczyć kilka plików naraz |
| `ask_open()` / `ask_add()` / `clear_extra()` / `reload()` | obsługa przycisków |
| `load(paths, add=False)` | wczytuje, pomija duplikaty, ostrzega o pustych |
| `_shorten_labels()` | obcina wspólny początek nazw — cięcie cofa się do najbliższego myślnika, żeby z `test-gs-20260727-2100` zostało `2100`, a nie `100` |
| `_fill_info()` | wypełnia panel boczny blokami |
| `_final(log, column)` | ostatnia wartość — PER liczy się narastająco, więc to jest wynik **całego** testu (średnia z takiej krzywej nic by nie znaczyła) |
| `_stat_lines(log)` | min/średnio/max dla `STAT_ROWS` + wiersze naprawy + lista MCS |
| **`_repair_lines(log)`** | **ile pakietów uratowała naprawa** — liczbowo, bo z wykresu da się odczytać tylko tempo: `NAPRAWIONE N`, `UTRACONE N`, „uratowane X % zgubionych w powietrzu", PER i „bez naprawy byłoby …" |
| `_comparison_lines()` | średnie z każdego pliku obok siebie + **`naprawione` / `utracone` / `PER bez %` / `PER %`** — to jest właściwa odpowiedź na „czy po tej zmianie gubimy mniej" |
| `main()` | ścieżki z linii poleceń; brak pliku → komunikat i na konsolę, i w okienku (po zmianie na `.pyw` konsoli po prostu nie ma) |

### Jak używać

- przeciągnij `test-gs-*.log` na `podglad_testu.py`, albo
- kliknij dwa razy — otworzy najnowszy log z katalogu, albo
- `python podglad_testu.py plik.log`
- kilka plików naraz = porównanie na wspólnych wykresach (Ctrl+D dodaje kolejne)

---

## 26. Gdzie co zmienić

| Chcę… | Idź do |
|---|---|
| zmienić domyślny kanał / region / moc | `DEFAULT_CHANNEL`, `DEFAULT_REGION`, `DEFAULT_TX_POWER` |
| dodać / zmienić poziom naprawy FEC | `FEC_LEVELS` (kolejność = poziomy, muszą rosnąć narzutem) |
| przestawić czułość automatu naprawy | `AUTO_FEC_*` |
| przestawić czułość automatu kanału | `AUTO_BAD_*`, `AUTO_ACK_*`, `AUTO_SETTLE_*` |
| dodać kolumnę do logu | `TestRecorder.COLUMNS` **i** `TestRecorder.sample()` |
| pokazać nową kolumnę na wykresie | tabela w `ChartArea._build_panels()` |
| dodać ją do odczytu pod kursorem | `ChartArea.READOUT` |
| dodać ją do panelu bocznego | `App.STAT_ROWS` lub `App._repair_lines()` |
| dodać pozycję menu | `main_menu()` — **`items` i drabinka `elif`** |
| zmienić progi oceny | `rssi_grade` / `loss_grade` / `snr_grade` **oraz** `RSSI_BANDS` / `LOSS_BANDS` w podglądzie |

### Zasady, które warto utrzymać

1. **`gs.py` i `drone.py` trzymaj identyczne** poza konfiguracją roli.
2. **Automaty niczego same nie dotykają** — `tick()` zwraca decyzje, ekran je
   wykonuje. Dzięki temu da się je przetestować bez radia.
3. **Nie ufaj configowi w sprawie tego, co się dzieje teraz** — `wfb_tx` czyta
   go przy starcie. Do stanu bieżącego jest `tx_radio_params()` i
   `live_tunnel_fec()`.
4. **`None` to nie zero.** Brak danych musi być widoczny jako `?`, a nie jako
   „0 % strat" — stąd `per` zwraca `None`, dopóki nic nie przyszło.
5. **Straty przed i po naprawie licz na wspólnym mianowniku**, inaczej nie da
   się ich zestawić na jednym wykresie.
6. **Każda zmiana FEC lub kanału to restart usługi**, czyli kilka sekund bez
   obrazu i telemetrii. Stąd cooldowny.

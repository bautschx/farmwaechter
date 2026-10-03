"""Farmwaechter 2: meldet per Telegram, wenn eine vfat-Position aus dem Preisbereich
laeuft oder wieder hineinkommt. Findet neue Positionen auf allen eingetragenen Chains
selbst, egal bei welcher Boerse und ob gestakt oder nicht.

Nur lesend. Keine Schluessel, keine Transaktionen. Nur Python-Standardbibliothek.

Ablauf je Lauf und Chain:
  1. Neue Positionen finden: jedes NFT, das neu fuer den Sickle hergestellt wird
     (Transfer von 0x0 an den Sickle, egal von welchem Vertrag).
  2. Je Position: ownerOf (gibt es sie noch?), positions (Grenzen, Menge),
     Pool ueber die Fabrik der Boerse, slot0 (aktueller Kurs).
  3. Mit dem letzten Lauf vergleichen und nur bei Aenderung melden.

Datenschutz: Das Repository ist oeffentlich. NFT-Nummern werden nur verschluesselt
gespeichert (Schluessel ist die geheime Sickle-Adresse), das Protokoll enthaelt
keine Nummern, Grenzen oder Kurse.
"""

import hashlib
import json
import os
import sys
import time
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

HIER = os.path.dirname(os.path.abspath(__file__))
KONFIG_DATEI = os.path.join(HIER, "farmen.json")
ZUSTAND_DATEI = os.path.join(HIER, "zustand.json")
WIEN = ZoneInfo("Europe/Vienna")

# Funktionskennungen (keccak256 der Signatur, erste 4 Bytes), nachgerechnet
SEL_POSITIONS = "0x99fbab88"        # positions(uint256)
SEL_OWNER_OF = "0x6352211e"         # ownerOf(uint256)
SEL_FACTORY = "0xc45a0155"          # factory()
SEL_GET_POOL_FEE = "0x1698ee82"     # getPool(address,address,uint24)   Uniswap-Art
SEL_GET_POOL_SPACING = "0x28af8d0b" # getPool(address,address,int24)    Slipstream-Art
SEL_SLOT0 = "0x3850c7bd"            # slot0()
SEL_TOKEN0 = "0x0dfe1681"           # token0()
SEL_DECIMALS = "0x313ce567"         # decimals()
SEL_SYMBOL = "0x95d89b41"           # symbol()
TOPIC_TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
NULL_ADRESSE = "0x" + "0" * 40

STABLES = {"USDC", "USDC.E", "USDBC", "USDT", "USDT0", "DAI", "USDS", "FRAX", "LUSD",
           "SUSD", "USD₮0", "USDE", "FDUSD", "PYUSD", "USD1"}

LOG_SCHRITT_START = 5000      # Bloecke je Suchabfrage, wird bei Ablehnung halbiert
LOG_SCHRITT_MIN = 50
MAX_ABFRAGEN_JE_LAUF = 40     # Rest wird im naechsten Lauf weitergesucht
ERSTLAUF_STUNDEN = 25         # beim allerersten Lauf so weit zurueck suchen
FEHLER_MELDEN_AB = 6          # so viele Fehlversuche hintereinander (ca. 30 Min.), dann Meldung
NAH_RAUS_FAKTOR = 1.6         # Vorwarnung endet erst bei 1,6-facher Schwelle (gegen Flattern)
WIEDER_REIN_ABSTAND = 0.03    # "wieder im Bereich" erst ab 3 % der Breite innerhalb


# ---------------------------------------------------------------- Chain lesen

class ChainFehler(Exception):
    pass


class Revert(ChainFehler):
    """Der Vertrag hat die Abfrage abgelehnt (kein Netzproblem)."""


class Chain:
    def __init__(self, name, rpcs):
        self.name = name
        self.rpcs = list(rpcs)

    def anfrage(self, methode, params, nur=None):
        letzter = None
        for url in ([nur] if nur else self.rpcs):
            for versuch in range(2):
                try:
                    daten = json.dumps({"jsonrpc": "2.0", "id": 1, "method": methode,
                                        "params": params}).encode()
                    req = urllib.request.Request(url, data=daten, headers={
                        "Content-Type": "application/json", "User-Agent": "farmwaechter"})
                    with urllib.request.urlopen(req, timeout=25) as r:
                        antwort = json.loads(r.read())
                    if "error" in antwort:
                        fehler = antwort["error"]
                        text = str(fehler.get("message", fehler)).lower() if isinstance(fehler, dict) else str(fehler).lower()
                        if methode == "eth_call" and ("revert" in text or "execution" in text):
                            raise Revert(text)
                        letzter = f"{url}: {fehler}"
                        break  # naechster Knoten
                    return antwort["result"]
                except ChainFehler:
                    raise
                except Exception as e:  # Netz, Zeitueberschreitung, Rate-Limit
                    letzter = f"{url}: {e}"
                    time.sleep(1 + versuch)
        raise ChainFehler(f"{self.name}: keine Antwort ({letzter})")

    def call(self, adresse, daten):
        ergebnis = self.anfrage("eth_call", [{"to": adresse, "data": daten}, "latest"])
        if ergebnis in (None, "0x"):
            raise Revert("leere Antwort")
        return ergebnis

    def block(self):
        return int(self.anfrage("eth_blockNumber", []), 16)

    def code_vorhanden(self, adresse):
        return self.anfrage("eth_getCode", [adresse, "latest"]) not in ("0x", "0x0", None)

    def logs(self, topics, von, bis):
        return self.anfrage("eth_getLogs", [{"topics": topics, "fromBlock": hex(von),
                                             "toBlock": hex(bis)}])


def wort(adresse_oder_zahl):
    if isinstance(adresse_oder_zahl, int):
        return format(adresse_oder_zahl % 2 ** 256, "064x")
    return adresse_oder_zahl.lower().replace("0x", "").rjust(64, "0")


def woerter(hexdaten):
    h = hexdaten[2:] if hexdaten.startswith("0x") else hexdaten
    return [h[i:i + 64] for i in range(0, len(h), 64)]


def als_int(w, signed=False):
    v = int(w, 16)
    if signed and v >= 2 ** 255:
        v -= 2 ** 256
    return v


def als_adresse(w):
    return "0x" + w[-40:]


# ---------------------------------------------------------- Verschluesselung

def schluessel(sickle, manager, token_id):
    return hashlib.sha256(f"{sickle}|{manager}|{token_id}".encode()).hexdigest()[:16]


def _maske(sickle, manager):
    return int(hashlib.sha256(f"farmwaechter|{sickle}|{manager}".encode()).hexdigest(), 16)


def verschluesseln(sickle, manager, token_id):
    return format(token_id ^ _maske(sickle, manager), "x")


def entschluesseln(sickle, manager, text):
    return int(text, 16) ^ _maske(sickle, manager)


# ------------------------------------------------------------ Positionen lesen

def position_lesen(chain, manager, token_id):
    """Liefert dict mit Grenzen usw., oder None wenn die Position nicht mehr existiert.
    Wirft Revert mit 'technik', wenn der Vertrag kein Uniswap-V3-artiger ist."""
    try:
        chain.call(manager, SEL_OWNER_OF + wort(token_id))
    except Revert:
        return None  # NFT verbrannt: Position aufgeloest
    try:
        w = woerter(chain.call(manager, SEL_POSITIONS + wort(token_id)))
    except Revert:
        raise Revert("technik")
    if len(w) < 12:
        raise Revert("technik")
    feld4 = als_int(w[4])
    if feld4 >= 2 ** 24:  # dort steht eine Adresse: Algebra-Art, noch nicht unterstuetzt
        raise Revert("technik")
    return {
        "token0": als_adresse(w[2]),
        "token1": als_adresse(w[3]),
        "feld4": als_int(w[4], True),   # Gebuehr (Uniswap-Art) oder Tick-Abstand (Slipstream)
        "unten": als_int(w[5], True),
        "oben": als_int(w[6], True),
        "liquiditaet": als_int(w[7]),
    }


def pool_finden(chain, manager, pos):
    fabrik = als_adresse(woerter(chain.call(manager, SEL_FACTORY))[0])
    argumente = wort(pos["token0"]) + wort(pos["token1"]) + wort(pos["feld4"])
    for sel in (SEL_GET_POOL_FEE, SEL_GET_POOL_SPACING):
        try:
            pool = als_adresse(woerter(chain.call(fabrik, sel + argumente))[0])
        except Revert:
            continue
        if pool == NULL_ADRESSE:
            continue
        try:  # Gegenprobe: gehoert der Pool wirklich zu diesem Paar?
            if als_adresse(woerter(chain.call(pool, SEL_TOKEN0))[0]) == pos["token0"]:
                return pool
        except Revert:
            continue
    raise Revert("technik")


def pool_tick(chain, pool):
    return als_int(woerter(chain.call(pool, SEL_SLOT0))[1], True)


def token_info(chain, token):
    dec = als_int(woerter(chain.call(token, SEL_DECIMALS))[0])
    try:
        w = woerter(chain.call(token, SEL_SYMBOL))
        if len(w) >= 3 and als_int(w[0]) == 32:      # normaler String
            laenge = als_int(w[1])
            sym = bytes.fromhex("".join(w[2:])[:laenge * 2]).decode()
        else:                                          # bytes32-Variante
            sym = bytes.fromhex(w[0]).rstrip(b"\0").decode()
    except Exception:
        sym = token[:8]
    return {"symbol": sym, "decimals": dec}


# ------------------------------------------------------------ Darstellung

def zahl(x):
    """4 gueltige Stellen, deutsches Komma."""
    if x == 0:
        return "0"
    stellen = max(0, 3 - int(f"{abs(x):e}".split("e")[1]))
    return f"{x:,.{stellen}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def preis(tick, t0, t1):
    """(Basis, in, Kurs): der Nicht-Stablecoin wird in Stablecoin ausgedrueckt."""
    p1je0 = 1.0001 ** tick * 10 ** (t0["decimals"] - t1["decimals"])
    if t0["symbol"].upper() in STABLES and t1["symbol"].upper() not in STABLES:
        return t1["symbol"], t0["symbol"], 1 / p1je0
    return t0["symbol"], t1["symbol"], p1je0


def bereichstext(pos, tick, t0, t1):
    basis, kurs_in, jetzt = preis(tick, t0, t1)
    lo, hi = sorted([preis(pos["unten"], t0, t1)[2], preis(pos["oben"], t0, t1)[2]])
    return (f"1 {basis} = {zahl(jetzt)} {kurs_in}\n"
            f"Bereich: {zahl(lo)} bis {zahl(hi)} {kurs_in}")


# ---------------------------------------------------------------- Bewertung

def status_berechnen(pos, tick, vorher, nah_schwelle):
    breite = pos["oben"] - pos["unten"]
    if not (pos["unten"] <= tick < pos["oben"]):
        return "aus"
    abstand = min(tick - pos["unten"], pos["oben"] - 1 - tick) / breite
    if vorher == "aus" and abstand < WIEDER_REIN_ABSTAND:
        return "aus"
    if nah_schwelle > 0:
        if abstand < nah_schwelle:
            return "nah"
        if vorher == "nah" and abstand < nah_schwelle * NAH_RAUS_FAKTOR:
            return "nah"
    return "im"


def welche_grenze(pos, tick, t0, t1):
    """'unteren' oder 'oberen' bezogen auf den angezeigten Preis."""
    naeher_tick = pos["unten"] if tick - pos["unten"] < pos["oben"] - tick else pos["oben"]
    _, _, p_grenze = preis(naeher_tick, t0, t1)
    _, _, p_jetzt = preis(tick, t0, t1)
    return "unteren" if p_grenze <= p_jetzt else "oberen"


# ---------------------------------------------------------------- Telegram

def telegram(text):
    token = os.environ["TELEGRAM_TOKEN"].strip()
    chat = os.environ["TELEGRAM_CHAT_ID"].strip()
    daten = json.dumps({"chat_id": chat, "text": text,
                        "disable_web_page_preview": True}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage",
                                 data=daten, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        if not json.loads(r.read()).get("ok"):
            raise RuntimeError("Telegram hat die Nachricht abgelehnt")


# ---------------------------------------------------------------- Hauptlauf

def laden(datei, standard):
    if os.path.exists(datei):
        with open(datei, encoding="utf-8") as f:
            return json.load(f)
    return standard


def neue_nfts_suchen(chain, cfg, sickle, zst):
    """Sucht neu hergestellte NFTs fuer den Sickle seit dem letzten Lauf."""
    aktuell = chain.block()
    if "letzter_block" in zst:
        start = zst["letzter_block"] + 1
    else:
        start = max(0, aktuell - int(ERSTLAUF_STUNDEN * 3600 / cfg["blockzeit"]))
    schritt = zst.get("log_schritt", LOG_SCHRITT_START)
    topics = [TOPIC_TRANSFER, "0x" + wort(NULL_ADRESSE), "0x" + wort(sickle)]
    funde = []
    abfragen = 0
    while start <= aktuell and abfragen < MAX_ABFRAGEN_JE_LAUF:
        ende = min(start + schritt - 1, aktuell)
        abfragen += 1
        try:
            logs = chain.logs(topics, start, ende)
        except ChainFehler:
            if schritt > LOG_SCHRITT_MIN:  # Bereich zu gross? halbieren und nochmal
                schritt = max(LOG_SCHRITT_MIN, schritt // 2)
                continue
            raise
        for log in logs:
            if len(log.get("topics", [])) == 4:  # NFT (bei Muenzen sind es 3)
                funde.append((log["address"].lower(), als_int(log["topics"][3])))
        zst["letzter_block"] = ende
        start = ende + 1
    zst["log_schritt"] = schritt
    for manager, tid in funde:
        k = schluessel(sickle, manager, tid)
        if k not in zst["nfts"]:
            zst["nfts"][k] = {"m": manager, "c": verschluesseln(sickle, manager, tid)}


def chain_pruefen(name, cfg, sickle, zst, tokens, nah_schwelle, meldungen, uebersicht):
    chain = Chain(name, cfg["rpc"])
    neue_nfts_suchen(chain, cfg, sickle, zst)

    neu, beendet = [], []   # je Eintrag: (paar, text)
    for k, e in list(zst["nfts"].items()):
        manager = e["m"]
        tid = entschluesseln(sickle, manager, e["c"])
        kopf_ohne = f"({name}, NFT {tid})"

        if e.get("art") == "unbekannt":
            continue
        try:
            pos = position_lesen(chain, manager, tid)
        except Revert:
            meldungen.append(f"❓ Neue Position auf {name} gefunden (NFT {tid}), aber diese "
                             f"Börsentechnik kann ich noch nicht lesen. Sie wird NICHT "
                             f"überwacht. Sag Claude Bescheid, dann wird sie eingebaut.")
            e["art"] = "unbekannt"
            continue
        if pos is None or pos["liquiditaet"] == 0:
            if "status" in e:
                beendet.append((e.get("paar", "?"), f"{e.get('paar', '?')} ({name})"))
            del zst["nfts"][k]
            continue

        if "pool" not in e:
            try:
                e["pool"] = pool_finden(chain, manager, pos)
            except Revert:
                meldungen.append(f"❓ Position auf {name} gefunden (NFT {tid}), aber ich finde "
                                 f"den Pool dazu nicht. Sie wird NICHT überwacht. "
                                 f"Sag Claude Bescheid.")
                e["art"] = "unbekannt"
                continue
        tick = pool_tick(chain, e["pool"])
        for t in (pos["token0"], pos["token1"]):
            if f"{name}:{t}" not in tokens:
                tokens[f"{name}:{t}"] = token_info(chain, t)
        t0, t1 = tokens[f"{name}:{pos['token0']}"], tokens[f"{name}:{pos['token1']}"]
        paar = f"{t0['symbol']}/{t1['symbol']}"
        kopf = f"{paar} {kopf_ohne}"
        alt = e.get("status")
        status = status_berechnen(pos, tick, alt, nah_schwelle)
        bereich = bereichstext(pos, tick, t0, t1)
        uebersicht.append((status, kopf, bereich, f"{paar} ({name})"))

        if alt is None:
            art = {"im": "im Bereich ✅", "nah": "im Bereich, aber nah an der Grenze ⚠️",
                   "aus": "AUSSERHALB des Bereichs 🚨"}[status]
            neu.append((paar, f"{kopf}\nStatus: {art}\n{bereich}"))
        elif alt != status:
            if status == "aus":
                meldungen.append(f"🚨 Außerhalb des Bereichs\n{kopf}\n"
                                 f"Bringt jetzt nichts mehr ein, bis du nachziehst.\n{bereich}")
            elif alt == "aus":
                meldungen.append(f"✅ Wieder im Bereich\n{kopf}\n{bereich}")
            elif status == "nah":
                g = welche_grenze(pos, tick, t0, t1)
                meldungen.append(f"⚠️ Vorwarnung: nah an der {g} Grenze\n{kopf}\n{bereich}")
        e["status"] = status
        e["paar"] = paar

    # Nachziehen = gleiches Paar beendet und neu im selben Lauf
    for paar, text in neu:
        treffer = next((b for b in beendet if b[0] == paar), None)
        if treffer:
            beendet.remove(treffer)
            meldungen.append("🔄 Position nachgezogen\n" + text)
        else:
            meldungen.append("🆕 Neue Position erkannt\n" + text)
    for _, wer in beendet:
        meldungen.append(f"🏁 Position beendet\n{wer} gibt es nicht mehr. "
                         f"Ich überwache sie nicht mehr.")


def diagnose(konfig, sickle_standard):
    """Prueft jede Chain-Verbindung einzeln. Gibt keine persoenlichen Daten aus."""
    for name, cfg in konfig["chains"].items():
        sickle = cfg.get("sickle", sickle_standard).lower()
        chain = Chain(name, cfg["rpc"])
        print(f"== {name}")
        for url in cfg["rpc"]:
            try:
                b = int(chain.anfrage("eth_blockNumber", [], nur=url), 16)
                chain.anfrage("eth_getLogs", [{"topics": [TOPIC_TRANSFER, "0x" + wort(NULL_ADRESSE),
                              "0x" + wort(sickle)], "fromBlock": hex(b - 500), "toBlock": hex(b)}], nur=url)
                print(f"   {url}: Block und Suche OK")
            except ChainFehler as e:
                print(f"   {url}: FEHLER {str(e)[:200]}")
        try:
            print(f"   Sickle auf dieser Chain vorhanden: {'ja' if chain.code_vorhanden(sickle) else 'nein'}")
        except ChainFehler:
            print("   Sickle-Pruefung nicht moeglich")


def main():
    konfig = laden(KONFIG_DATEI, None)
    if konfig is None:
        sys.exit("farmen.json fehlt")
    sickle_standard = os.environ["SICKLE_ADDRESS"].strip().lower()
    if os.environ.get("DIAGNOSE", "").lower() == "true":
        diagnose(konfig, sickle_standard)
        return

    zustand = laden(ZUSTAND_DATEI, {})
    if zustand.get("version") != 2:
        zustand = {"version": 2}
    zustand.setdefault("chains", {})
    zustand.setdefault("tokens", {})
    nah_schwelle = konfig.get("vorwarnung_prozent", 15) / 100

    jetzt = datetime.now(WIEN)
    heute = jetzt.date().isoformat()
    tagesbericht = (jetzt.hour >= konfig.get("tagesbericht_uhrzeit", 8)
                    and zustand.get("letzter_tagesbericht") != heute)

    meldungen, uebersicht, fehlerhaft, sickle_da = [], [], [], []
    for name, cfg in konfig["chains"].items():
        zst = zustand["chains"].setdefault(name, {})
        zst.setdefault("nfts", {})
        zst.setdefault("fehler", 0)
        sickle = cfg.get("sickle", sickle_standard).lower()
        try:
            chain_pruefen(name, cfg, sickle, zst, zustand["tokens"], nah_schwelle,
                          meldungen, uebersicht)
            if zst["fehler"] >= FEHLER_MELDEN_AB:
                meldungen.append(f"✅ {name}: Ich kann die Chain wieder lesen, "
                                 f"die Überwachung läuft.")
            zst["fehler"] = 0
            if tagesbericht and Chain(name, cfg["rpc"]).code_vorhanden(sickle):
                sickle_da.append(name)
        except ChainFehler as e:
            zst["fehler"] += 1
            fehlerhaft.append(name)
            print(f"FEHLER {name}: {str(e)[:300]}")
            if zst["fehler"] == FEHLER_MELDEN_AB:
                hat_pos = any("status" in x for x in zst["nfts"].values())
                if hat_pos:
                    meldungen.append(f"⚠️ {name}: Ich kann die Chain seit etwa 30 Minuten "
                                     f"nicht lesen. Bis das wieder geht, bekommst du für "
                                     f"Positionen dort keine Warnungen.")

    if tagesbericht:
        zustand["letzter_tagesbericht"] = heute
        zeichen = {"im": "✅", "nah": "⚠️", "aus": "🚨"}
        teile = [f"{zeichen[s]} {k}\n{b}" for s, k, b, _ in uebersicht]
        text = "☀️ Tagesbericht Farmwächter\n\n"
        text += "\n\n".join(teile) if teile else "Keine offene Position gefunden."
        text += f"\n\nÜberwachte Chains: {', '.join(konfig['chains'])}"
        text += f"\nDein Sickle ist vorhanden auf: {', '.join(sickle_da) or 'keiner'}"
        if fehlerhaft:
            text += f"\nGerade nicht lesbar: {', '.join(fehlerhaft)}"
        meldungen.append(text)

    # Protokoll ist oeffentlich: nur Paar und Status, keine Nummern oder Grenzen
    for m in meldungen:
        telegram(m)
    print(f"{len(meldungen)} Meldung(en) gesendet")
    for s, _, _, kurz in uebersicht:
        print("STAND:", kurz, s)

    with open(ZUSTAND_DATEI, "w", encoding="utf-8") as f:
        json.dump(zustand, f, indent=1, sort_keys=True, ensure_ascii=False)
        f.write("\n")


if __name__ == "__main__":
    main()

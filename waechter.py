"""Farmwaechter: meldet per Telegram, wenn eine vfat-Position (Velodrome/Aerodrome
Slipstream) aus dem Preisbereich laeuft oder wieder hineinkommt.

Nur lesend. Keine Schluessel, keine Transaktionen. Nur Python-Standardbibliothek.

Ablauf je Lauf und Chain:
  1. Neue Farmen finden: NFT-Uebertragungen vom Sickle in einen Gauge (eth_getLogs).
  2. Je bekanntem Gauge: stakedValues(Sickle) -> aktuelle NFT-Nummern.
  3. Je NFT: positions(NFT) -> Grenzen; Pool slot0() -> aktueller Tick.
  4. Zustand mit dem letzten Lauf vergleichen und nur bei Aenderung melden.
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
SEL_STAKED_VALUES = "0x4b937763"   # stakedValues(address)
SEL_POSITIONS = "0x99fbab88"       # positions(uint256)
SEL_SLOT0 = "0x3850c7bd"           # slot0()
SEL_POOL = "0x16f0115b"            # pool()
SEL_DECIMALS = "0x313ce567"        # decimals()
SEL_SYMBOL = "0x95d89b41"          # symbol()
TOPIC_TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

STABLES = {"USDC", "USDC.E", "USDBC", "USDT", "USDT0", "DAI", "USDS", "FRAX", "LUSD", "SUSD", "USD₮0"}

LOG_BLOCK_SCHRITT = 2000      # Bloecke je eth_getLogs-Abfrage
LOG_RUECKBLICK = 3600         # normaler Lauf: ca. 2 Stunden auf OP (2 s je Block)
LOG_RUECKBLICK_TAG = 45000    # einmal taeglich: ca. 25 Stunden
FEHLER_MELDEN_AB = 6          # so viele Fehlversuche hintereinander (ca. 30 Min.), dann Meldung
NAH_RAUS_FAKTOR = 1.6         # Vorwarnung endet erst bei 1,6-facher Schwelle (gegen Flattern)
WIEDER_REIN_ABSTAND = 0.03    # "wieder im Bereich" erst ab 3 % der Breite innerhalb


# ---------------------------------------------------------------- Chain lesen

class ChainFehler(Exception):
    pass


class Chain:
    def __init__(self, name, rpcs):
        self.name = name
        self.rpcs = list(rpcs)

    def _anfrage(self, methode, params):
        letzter = None
        for url in self.rpcs:
            for versuch in range(2):
                try:
                    daten = json.dumps({"jsonrpc": "2.0", "id": 1, "method": methode,
                                        "params": params}).encode()
                    req = urllib.request.Request(url, data=daten, headers={
                        "Content-Type": "application/json", "User-Agent": "farmwaechter"})
                    with urllib.request.urlopen(req, timeout=20) as r:
                        antwort = json.loads(r.read())
                    if "error" in antwort:
                        letzter = f"{url}: {antwort['error']}"
                        # Revert ist kein Netzproblem: nicht beim naechsten Knoten wiederholen
                        if methode == "eth_call":
                            raise ChainFehler(letzter)
                        break
                    return antwort["result"]
                except ChainFehler:
                    raise
                except Exception as e:  # Netz, Zeitueberschreitung, Rate-Limit
                    letzter = f"{url}: {e}"
                    time.sleep(1 + versuch)
        raise ChainFehler(f"{self.name}: keine Antwort ({letzter})")

    def call(self, adresse, daten):
        return self._anfrage("eth_call", [{"to": adresse, "data": daten}, "latest"])

    def block(self):
        return int(self._anfrage("eth_blockNumber", []), 16)

    def logs(self, adresse, topics, von, bis):
        return self._anfrage("eth_getLogs", [{"address": adresse, "topics": topics,
                                              "fromBlock": hex(von), "toBlock": hex(bis)}])


def wort(adresse_oder_zahl):
    if isinstance(adresse_oder_zahl, int):
        return format(adresse_oder_zahl, "064x")
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


def staked_values(chain, gauge, sickle):
    w = woerter(chain.call(gauge, SEL_STAKED_VALUES + wort(sickle)))
    if len(w) < 2:
        raise ChainFehler(f"{gauge}: unerwartete Antwort auf stakedValues")
    laenge = als_int(w[1])
    return [als_int(x) for x in w[2:2 + laenge]]


def position(chain, nft_manager, token_id):
    w = woerter(chain.call(nft_manager, SEL_POSITIONS + wort(token_id)))
    if len(w) < 12:
        raise ChainFehler(f"positions({token_id}): unerwartete Antwort")
    return {
        "token0": als_adresse(w[2]),
        "token1": als_adresse(w[3]),
        "tick_spacing": als_int(w[4], True),
        "unten": als_int(w[5], True),
        "oben": als_int(w[6], True),
        "liquiditaet": als_int(w[7]),
    }


def gauge_pool(chain, gauge):
    return als_adresse(woerter(chain.call(gauge, SEL_POOL))[0])


def pool_tick(chain, pool):
    w = woerter(chain.call(pool, SEL_SLOT0))
    return als_int(w[1], True)


def token_info(chain, token):
    dec = als_int(woerter(chain.call(token, SEL_DECIMALS))[0])
    roh = chain.call(token, SEL_SYMBOL)
    w = woerter(roh)
    try:
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


def preisansicht(tick, t0, t1):
    """Liefert (Preistext-Funktion, Basis, Kurs) so, dass der Nicht-Stable in Stable steht."""
    faktor = 10 ** (t0["decimals"] - t1["decimals"])
    p1je0 = 1.0001 ** tick * faktor  # token1 je token0
    if t0["symbol"].upper() in STABLES and t1["symbol"].upper() not in STABLES:
        return t1["symbol"], t0["symbol"], 1 / p1je0      # 1 token1 = x token0
    return t0["symbol"], t1["symbol"], p1je0              # 1 token0 = x token1


def bereichstext(pos, tick, t0, t1):
    basis, kurs_in, jetzt = preisansicht(tick, t0, t1)
    _, _, a = preisansicht(pos["unten"], t0, t1)
    _, _, b = preisansicht(pos["oben"], t0, t1)
    lo, hi = sorted([a, b])
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


def welche_grenze(pos, tick):
    return "unten" if tick - pos["unten"] < pos["oben"] - tick else "oben"


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

def schluessel(sickle, token_id):
    return hashlib.sha256(f"{sickle}:{token_id}".encode()).hexdigest()[:16]


def laden(datei, standard):
    if os.path.exists(datei):
        with open(datei, encoding="utf-8") as f:
            return json.load(f)
    return standard


def neue_gauges_finden(chain, cfg, sickle, zst, rueckblick, meldungen):
    aktuell = chain.block()
    von = max(0, aktuell - rueckblick)
    bekannt = {g.lower() for g in zst["gauges"]}
    ziele = set()
    start = von
    while start <= aktuell:
        ende = min(start + LOG_BLOCK_SCHRITT - 1, aktuell)
        for log in chain.logs(cfg["nft_manager"], [TOPIC_TRANSFER, "0x" + wort(sickle)], start, ende):
            ziel = als_adresse(log["topics"][2]).lower()
            if ziel != "0x" + "0" * 40 and ziel not in bekannt:
                ziele.add(ziel)
        start = ende + 1
    for ziel in sorted(ziele):
        try:
            if staked_values(chain, ziel, sickle):
                zst["gauges"].append(ziel)
                bekannt.add(ziel)
        except ChainFehler:
            pass  # kein Gauge (z. B. Uebertragung an einen anderen Vertrag)


def chain_pruefen(name, cfg, sickle, zst, nah_schwelle, rueckblick, meldungen, uebersicht):
    chain = Chain(name, cfg["rpc"])
    for g in cfg.get("gauges", []):
        if g.lower() not in [x.lower() for x in zst["gauges"]]:
            zst["gauges"].append(g.lower())

    neue_gauges_finden(chain, cfg, sickle, zst, rueckblick, meldungen)

    for gauge in list(zst["gauges"]):
        ids = staked_values(chain, gauge, sickle)
        gz = zst["positionen"].setdefault(gauge, {})
        # NFT-Nummern nur verschluesselt speichern: das Repository ist oeffentlich,
        # und ueber die Nummer kaeme man zum Sickle und zur Wallet.
        aktuelle = {schluessel(sickle, i) for i in ids}
        beendet = [k for k in gz if k not in aktuelle]
        pool = zst["pools"].get(gauge) or gauge_pool(chain, gauge)
        zst["pools"][gauge] = pool
        tick = pool_tick(chain, pool) if ids else None

        neu_text = []
        for tid in ids:
            pos = position(chain, cfg["nft_manager"], tid)
            for t in (pos["token0"], pos["token1"]):
                if t not in zst["tokens"]:
                    zst["tokens"][t] = token_info(chain, t)
            t0, t1 = zst["tokens"][pos["token0"]], zst["tokens"][pos["token1"]]
            paar = f"{t0['symbol']}/{t1['symbol']}"
            alt = gz.get(schluessel(sickle, tid))
            status = status_berechnen(pos, tick, alt["status"] if alt else None, nah_schwelle)
            bereich = bereichstext(pos, tick, t0, t1)
            kopf = f"{paar} ({name}, NFT {tid})"
            uebersicht.append((status, kopf, bereich, paar))

            if alt is None:
                art = {"im": "im Bereich ✅", "nah": "im Bereich, aber nah an der Grenze ⚠️",
                       "aus": "AUSSERHALB des Bereichs 🚨"}[status]
                neu_text.append(f"{kopf}\nStatus: {art}\n{bereich}")
            elif alt["status"] != status:
                if status == "aus":
                    meldungen.append(f"🚨 Außerhalb des Bereichs\n{kopf}\n"
                                     f"Keine Belohnung mehr, bis du nachziehst.\n{bereich}")
                elif alt["status"] == "aus":
                    meldungen.append(f"✅ Wieder im Bereich\n{kopf}\n{bereich}")
                elif status == "nah":
                    g = "unteren" if welche_grenze(pos, tick) == "unten" else "oberen"
                    meldungen.append(f"⚠️ Vorwarnung: nah an der {g} Grenze\n{kopf}\n{bereich}")
            gz[schluessel(sickle, tid)] = {"status": status, "paar": paar}

        if neu_text and beendet:
            meldungen.append("🔄 Position nachgezogen\n" + "\n\n".join(neu_text))
        elif neu_text:
            meldungen.append("🆕 Neue Position erkannt\n" + "\n\n".join(neu_text))
        elif beendet:
            for i in beendet:
                meldungen.append(f"🏁 Position beendet\n{gz[i]['paar']} ({name}) "
                                 f"ist nicht mehr gestakt. Ich überwache sie nicht mehr.")
        for i in beendet:
            del gz[i]
        if not gz:
            zst["positionen"].pop(gauge, None)


def main():
    konfig = laden(KONFIG_DATEI, None)
    if konfig is None:
        sys.exit("farmen.json fehlt")
    zustand = laden(ZUSTAND_DATEI, {})
    zustand.setdefault("chains", {})
    zustand.setdefault("fehler", 0)
    sickle_standard = os.environ["SICKLE_ADDRESS"].strip().lower()
    nah_schwelle = konfig.get("vorwarnung_prozent", 15) / 100

    jetzt = datetime.now(WIEN)
    heute = jetzt.date().isoformat()
    tagesbericht = (jetzt.hour >= konfig.get("tagesbericht_uhrzeit", 8)
                    and zustand.get("letzter_tagesbericht") != heute)
    rueckblick = LOG_RUECKBLICK_TAG if tagesbericht else LOG_RUECKBLICK

    meldungen, uebersicht, fehler = [], [], []
    for name, cfg in konfig["chains"].items():
        zst = zustand["chains"].setdefault(name, {})
        for k, std in (("gauges", []), ("positionen", {}), ("pools", {}), ("tokens", {})):
            zst.setdefault(k, std)
        sickle = cfg.get("sickle", sickle_standard).lower()
        try:
            chain_pruefen(name, cfg, sickle, zst, nah_schwelle, rueckblick, meldungen, uebersicht)
        except ChainFehler as e:
            fehler.append(str(e))

    if fehler:
        zustand["fehler"] += 1
        print("FEHLER:", *fehler, sep="\n  ")
        if zustand["fehler"] == FEHLER_MELDEN_AB:
            meldungen.append("⚠️ Ich kann die Chain seit etwa 30 Minuten nicht lesen. "
                             "Bis das wieder geht, bekommst du keine Warnungen.\n" + fehler[0][:300])
    else:
        if zustand["fehler"] >= FEHLER_MELDEN_AB:
            meldungen.append("✅ Ich kann die Chain wieder lesen, die Überwachung läuft.")
        zustand["fehler"] = 0

    if tagesbericht and not fehler:
        zustand["letzter_tagesbericht"] = heute
        if uebersicht:
            zeichen = {"im": "✅", "nah": "⚠️", "aus": "🚨"}
            teile = [f"{zeichen[s]} {k}\n{b}" for s, k, b, _ in uebersicht]
            meldungen.append("☀️ Tagesbericht Farmwächter\n\n" + "\n\n".join(teile))
        else:
            meldungen.append("☀️ Tagesbericht Farmwächter\nKeine gestakte Position gefunden.")

    # Protokoll ist oeffentlich: nur Paar und Status, keine Nummern oder Grenzen
    for m in meldungen:
        telegram(m)
    print(f"{len(meldungen)} Meldung(en) gesendet")
    for s, _, _, paar in uebersicht:
        print("STAND:", paar, s)

    with open(ZUSTAND_DATEI, "w", encoding="utf-8") as f:
        json.dump(zustand, f, indent=1, sort_keys=True, ensure_ascii=False)
        f.write("\n")

    if fehler and not uebersicht:
        sys.exit(1)


if __name__ == "__main__":
    main()

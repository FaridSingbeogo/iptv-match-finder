import ipaddress
import json
import re
import socket
import time
import unicodedata
from datetime import date, datetime, timedelta
from urllib.parse import urlencode, urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024

URL = "https://www.tv-direct.fr/sport-direct/foot/"
DUREE_CACHE = 3600
_cache = {"heure": 0, "matchs": []}

PARIS = ZoneInfo("Europe/Paris")
BURKINA = ZoneInfo("Africa/Ouagadougou")

JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
        "août", "septembre", "octobre", "novembre", "décembre"]

# Mots à repérer dans les noms de chaînes IPTV (import : tri large)
MOTS_CLES = {
    "Canal+": ["canal+", "canal +", "canal plus", "canalplus"],
    "beIN Sports": ["bein"],
    "DAZN": ["dazn"],
    "Ligue 1+": ["ligue 1", "ligue1"],
}

# Chaînes à repérer dans ta liste, par diffuseur
FAMILLES = {
    "Canal+": {"marque": r"canal\s*\+(?!\s*\d)|canal plus|canalplus", "pays": ["FR"]},
    "beIN Sports": {"marque": r"bein", "pays": ["FR"]},
    "DAZN": {"marque": r"dazn", "pays": ["FR"]},
    "Ligue 1+": {"marque": r"ligue ?1|ligue1", "pays": ["FR"]},
}

# À écarter toujours : sport féminin, autres sports, bars
EXCLURE = (
    r"\bf ?1\b|formul|fem[ei]n|women|ladies|\bbar\b|nba|nfl|nhl|mlb|ufc|boxing|boxe"
    r"|tennis|golf|rugby|basket|handball|volley|cricket|wwe|darts|cyclisme|motogp"
)

COUPES_EUROPE = {"diffuseur": "Canal+", "fort": r"sport|foot|live|champions"}
BEIN = {"diffuseur": "beIN Sports", "fort": r"sports?\s*\d|max"}

# Règles par compétition (d'après TV-Direct, à mettre à jour si les droits changent)
COMPETITIONS = {
    "uefa champions league": COUPES_EUROPE,
    "uefa europa league": COUPES_EUROPE,
    "uefa conference league": COUPES_EUROPE,
    "premier league": {"diffuseur": "Canal+", "fort": r"sport|foot|live|premier"},
    "ligue 1": {"diffuseur": "Ligue 1+", "fort": r"."},
    "ligue 2": BEIN,
    "bundesliga": BEIN,
    "primera division": {
        "diffuseur": "DAZN", "fort": r"la ?liga",
        "pays": ["FR", "ES"], "exclure": r"ligue ?1|serie",
    },
    "serie a": {
        "diffuseur": "DAZN", "fort": r"serie ?a",
        "pays": ["FR", "IT"], "exclure": r"ligue ?1|la ?liga",
    },
}


def norm(texte):
    """Minuscules et sans accents, pour comparer facilement."""
    t = unicodedata.normalize("NFD", texte.lower())
    return "".join(c for c in t if unicodedata.category(c) != "Mn")


def regle_pour(competition):
    c = COMPETITIONS.get(norm(competition))
    if not c:
        return None
    fam = FAMILLES[c["diffuseur"]]
    return {
        "diffuseur": c["diffuseur"],
        "marque": fam["marque"],
        "fort": c["fort"],
        "pays": c.get("pays", fam["pays"]),
        "exclure": EXCLURE + ("|" + c["exclure"] if c.get("exclure") else ""),
    }


TOUS_LES_MOTS = [mot for mots in MOTS_CLES.values() for mot in mots]
NUM_MOIS = {norm(nom): i + 1 for i, nom in enumerate(MOIS)}
RE_JOUR = re.compile(r"^(" + "|".join(JOURS) + r")\s+(\d{1,2})\s+([^\W\d_]+)", re.I)
RE_HEURE = re.compile(r"^\d{1,2}:\d{2}$")


# ---------- Programme des matchs ----------

def construire_date(jour, nom_mois):
    mois = NUM_MOIS.get(norm(nom_mois))
    if not mois:
        return None
    aujourdhui = date.today()
    try:
        d = date(aujourdhui.year, mois, int(jour))
        if d < aujourdhui - timedelta(days=180):
            d = date(aujourdhui.year + 1, mois, int(jour))
        return d
    except ValueError:
        return None


def lire_page(html):
    soup = BeautifulSoup(html, "html.parser")
    lignes = [l.strip() for l in soup.get_text("\n").split("\n")]
    lignes = [l for l in lignes if l]

    debut = next((i for i, l in enumerate(lignes) if l.lower() == "les prochains matchs"), 0)
    fin = next((i for i, l in enumerate(lignes) if l.startswith("Les dates et horaires")), len(lignes))
    lignes = lignes[debut:fin]

    etiquettes = ("prochain match", "en direct", "direct", "live")
    matchs = []
    jour_courant = None
    n = len(lignes)
    i = 0
    while i < n:
        ligne = lignes[i]

        m = RE_JOUR.match(ligne)
        if m:
            d = construire_date(m.group(2), m.group(3))
            if d:
                jour_courant = d
            i += 1
            continue

        # Un match : heure, équipe 1, tiret, équipe 2, [étiquette], compétition
        if (
            RE_HEURE.match(ligne)
            and jour_courant
            and i + 3 < n
            and lignes[i + 2] in ("–", "-", "—")
        ):
            equipe1 = lignes[i + 1]
            equipe2 = lignes[i + 3]
            j = i + 4
            while j < n and lignes[j].lower() in etiquettes:
                j += 1
            if j < n and not RE_HEURE.match(lignes[j]) and not RE_JOUR.match(lignes[j]):
                comp = lignes[j]
                i = j + 1
            else:
                comp = ""
                i = j
            matchs.append({
                "date": jour_courant,
                "heure": ligne,
                "match": f"{equipe1} – {equipe2}",
                "competition": comp,
            })
            continue

        i += 1
    return matchs


def charger_matchs():
    if _cache["matchs"] and time.time() - _cache["heure"] < DUREE_CACHE:
        return _cache["matchs"]
    r = requests.get(
        URL,
        headers={"User-Agent": "Mozilla/5.0 (compatible; BeogoIPTV/1.0)"},
        timeout=20,
    )
    r.raise_for_status()
    matchs = lire_page(r.text)
    _cache["matchs"] = matchs
    _cache["heure"] = time.time()
    return matchs


def vers_burkina(d, heure):
    h, mn = heure.split(":")
    paris = datetime(d.year, d.month, d.day, int(h), int(mn), tzinfo=PARIS)
    return paris.astimezone(BURKINA)


# ---------- Import des listes IPTV ----------

ENTETES = {"User-Agent": "Mozilla/5.0 (compatible; BeogoIPTV/1.0)"}
MAX_JSON = 30 * 1024 * 1024
MAX_M3U = 150 * 1024 * 1024
DUREE_MAX = 60
_limites = {}


def trop_de_requetes(ip, maxi=10, fenetre=3600):
    maintenant = time.time()
    recentes = [t for t in _limites.get(ip, []) if maintenant - t < fenetre]
    if len(recentes) >= maxi:
        _limites[ip] = recentes
        return True
    recentes.append(maintenant)
    _limites[ip] = recentes
    return False


def url_sure(url):
    """Refuse les adresses internes (localhost, réseau privé...)."""
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        return False
    try:
        infos = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80))
    except (socket.gaierror, ValueError):
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return False
    return True


def telecharger(url):
    for _ in range(4):
        if not url_sure(url):
            raise ValueError("Adresse non autorisée ou introuvable")
        r = requests.get(url, headers=ENTETES, timeout=(10, 40), stream=True, allow_redirects=False)
        if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("Location"):
            url = urljoin(url, r.headers["Location"])
            r.close()
            continue
        return r
    raise ValueError("Trop de redirections")


def lire_limite(r, maxi):
    morceaux, total = [], 0
    debut = time.time()
    for morceau in r.iter_content(65536):
        total += len(morceau)
        if total > maxi or time.time() - debut > DUREE_MAX:
            r.close()
            raise ValueError("Liste trop volumineuse ou trop lente")
        morceaux.append(morceau)
    return b"".join(morceaux)


def garder(nom):
    k = norm(nom)
    return any(mot in k for mot in TOUS_LES_MOTS)


def dedoublonner(noms, limite=600):
    vus, sortie = set(), []
    for nom in noms:
        cle = norm(nom)
        if nom and cle not in vus and garder(nom):
            vus.add(cle)
            sortie.append(nom)
            if len(sortie) >= limite:
                break
    return sortie


def noms_depuis_m3u(r):
    noms = []
    total = 0
    debut = time.time()
    for ligne in r.iter_lines(decode_unicode=False):
        total += len(ligne) + 1
        if total > MAX_M3U or time.time() - debut > DUREE_MAX:
            r.close()
            raise ValueError("Liste trop volumineuse ou trop lente")
        if ligne.startswith(b"#EXTINF"):
            nom = ligne.decode("utf-8", "ignore").rsplit(",", 1)[-1].strip()
            if nom and garder(nom):
                noms.append(nom)
    return noms


def origine(serveur):
    s = serveur.strip()
    if not s.startswith(("http://", "https://")):
        s = "http://" + s
    p = urlparse(s)
    if not p.hostname:
        raise ValueError("Adresse du serveur invalide")
    base = f"{p.scheme}://{p.hostname}"
    if p.port:
        base += f":{p.port}"
    return base


def noms_depuis_xtream(serveur, utilisateur, mot_de_passe):
    base = origine(serveur)
    url = base + "/player_api.php?" + urlencode({
        "username": utilisateur,
        "password": mot_de_passe,
        "action": "get_live_streams",
    })
    r = telecharger(url)
    if r.status_code != 200:
        raise ValueError("Le serveur a refusé la demande (identifiants ou adresse incorrects)")
    try:
        data = json.loads(lire_limite(r, MAX_JSON))
    except json.JSONDecodeError:
        raise ValueError("Réponse inattendue du serveur Xtream")
    if not isinstance(data, list):
        raise ValueError("Identifiants refusés ou serveur incompatible")
    return [str(x.get("name", "")) for x in data if isinstance(x, dict)]


# ---------- Routes ----------

@app.route("/")
def accueil():
    return render_template("index.html")


@app.route("/api/diffuseurs")
def api_diffuseurs():
    return jsonify(diffuseurs=MOTS_CLES)


@app.route("/api/iptv", methods=["POST"])
def api_iptv():
    ip = request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or request.remote_addr
    if trop_de_requetes(ip):
        return jsonify(erreur="Trop d'imports. Réessaie dans une heure."), 429

    d = request.get_json(silent=True) or {}
    mode = d.get("mode")
    try:
        if mode == "m3u_url":
            url = str(d.get("url", "")).strip()
            if not url.startswith(("http://", "https://")):
                url = "http://" + url
            r = telecharger(url)
            if r.status_code != 200:
                raise ValueError("Le lien M3U a refusé la demande")
            noms = noms_depuis_m3u(r)
        elif mode == "xtream":
            serveur = str(d.get("serveur", "")).strip()
            utilisateur = str(d.get("utilisateur", "")).strip()
            mot_de_passe = str(d.get("mot_de_passe", ""))
            if not (serveur and utilisateur and mot_de_passe):
                raise ValueError("Remplis le serveur, l'utilisateur et le mot de passe")
            noms = noms_depuis_xtream(serveur, utilisateur, mot_de_passe)
        else:
            raise ValueError("Type de liste inconnu")
    except ValueError as e:
        return jsonify(erreur=str(e)), 400
    except requests.RequestException:
        return jsonify(erreur="Serveur IPTV injoignable (il bloque peut-être ce site)"), 502

    return jsonify(chaines=dedoublonner(noms))


@app.route("/search")
def search():
    try:
        matchs = charger_matchs()
    except requests.RequestException:
        if _cache["matchs"]:
            matchs = _cache["matchs"]
        else:
            return jsonify(erreur="Impossible de lire le programme TV"), 502

    q = norm(request.args.get("q", "")).strip()
    jour_demande = request.args.get("date")
    mots = q.split()
    aujourdhui = datetime.now(BURKINA).date()

    tous = [(vers_burkina(m["date"], m["heure"]), m) for m in matchs]
    tous.sort(key=lambda x: x[0])

    resultats = []
    for dt, m in tous:
        d = dt.date()
        texte = norm(m["match"] + " " + m["competition"])

        if jour_demande:
            if d.isoformat() != jour_demande:
                continue
        elif not mots and d != aujourdhui:
            continue

        if mots and not all(mot in texte for mot in mots):
            continue

        regle = regle_pour(m["competition"])
        quand = (
            f"{JOURS[d.weekday()].capitalize()} {d.day} {MOIS[d.month - 1]} "
            f"à {dt.strftime('%H:%M')} (heure du Burkina)"
        )
        resultats.append({
            "match": m["match"],
            "heure": f"{m['competition']} · {quand}",
            "chaines": [regle["diffuseur"]] if regle else [],
            "regle": regle,
            "recherche": f"{m['match']} chaîne TV {d.day} {MOIS[d.month - 1]}",
        })

    return jsonify(resultats)

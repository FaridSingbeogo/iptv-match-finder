import re
import time
import unicodedata
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

URL = "https://www.tv-direct.fr/sport-direct/foot/"
DUREE_CACHE = 3600  # on relit la page au maximum une fois par heure
_cache = {"heure": 0, "matchs": []}

JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
        "août", "septembre", "octobre", "novembre", "décembre"]

# Diffuseur selon la compétition (d'après TV-Direct, à mettre à jour si les droits changent)
DIFFUSEURS = {
    "uefa champions league": "Canal+",
    "uefa europa league": "Canal+",
    "uefa conference league": "Canal+",
    "premier league": "Canal+",
    "ligue 1": "Ligue 1+",
    "ligue 2": "beIN Sports",
    "bundesliga": "beIN Sports",
    "primera division": "DAZN",
    "serie a": "DAZN",
}


def norm(texte):
    """Minuscules et sans accents, pour comparer facilement."""
    t = unicodedata.normalize("NFD", texte.lower())
    return "".join(c for c in t if unicodedata.category(c) != "Mn")


NUM_MOIS = {norm(nom): i + 1 for i, nom in enumerate(MOIS)}
RE_JOUR = re.compile(r"^(" + "|".join(JOURS) + r")\s+(\d{1,2})\s+([^\W\d_]+)", re.I)
RE_HEURE = re.compile(r"^\d{1,2}:\d{2}$")


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

    # On se limite à la zone du calendrier
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


@app.route("/")
def accueil():
    return render_template("index.html")


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

    resultats = []
    for m in sorted(matchs, key=lambda x: (x["date"], x["heure"])):
        texte = norm(m["match"] + " " + m["competition"])

        if jour_demande:
            if m["date"].isoformat() != jour_demande:
                continue
        elif not mots and m["date"] != date.today():
            continue  # sans recherche, on montre seulement aujourd'hui

        if mots and not all(mot in texte for mot in mots):
            continue

        diffuseur = DIFFUSEURS.get(norm(m["competition"]))
        d = m["date"]
        quand = f"{JOURS[d.weekday()].capitalize()} {d.day} {MOIS[d.month - 1]} à {m['heure']} (heure de Paris)"
        resultats.append({
            "match": m["match"],
            "heure": f"{m['competition']} · {quand}",
            "chaines": [diffuseur] if diffuseur else [],
        })

    return jsonify(resultats)


@app.route("/debug")
def debug():
    r = requests.get(
        URL,
        headers={"User-Agent": "Mozilla/5.0 (compatible; BeogoIPTV/1.0)"},
        timeout=20,
    )
    html = r.text
    soup = BeautifulSoup(html, "html.parser")
    lignes = [l.strip() for l in soup.get_text("\n").split("\n") if l.strip()]
    return jsonify(
        code=r.status_code,
        taille_html=len(html),
        titre=soup.title.string if soup.title else None,
        contient_prochains_matchs="prochains matchs" in html.lower(),
        contient_un_horaire=bool(re.search(r"\d{2}:\d{2}", html)),
        nombre_de_lignes=len(lignes),
        debut_du_texte=lignes[:15],
        nombre_de_matchs=len(lire_page(html)),
    )
@app.route("/debug2")
def debug2():
    from flask import Response

    r = requests.get(
        URL,
        headers={"User-Agent": "Mozilla/5.0 (compatible; BeogoIPTV/1.0)"},
        timeout=20,
    )
    html = r.text
    soup = BeautifulSoup(html, "html.parser")
    lignes = [l.strip() for l in soup.get_text("\n").split("\n") if l.strip()]

    pos = next((i for i, l in enumerate(lignes) if "prochains matchs" in l.lower()), None)
    if pos is None:
        haut = "Aucune ligne 'prochains matchs'"
    else:
        haut = "\n".join(f"{i}: {lignes[i]}" for i in range(pos, min(pos + 30, len(lignes))))

    heures = [i for i, l in enumerate(lignes) if re.search(r"\d{1,2}:\d{2}", l)][:6]
    exemples = "\n".join(f"[{i}] " + " | ".join(lignes[i:i + 3]) for i in heures)

    h = html.lower().find("prochains matchs")
    brut = html[h:h + 1000] if h >= 0 else "introuvable"

    texte = (
        "=== TEXTE APRES 'PROCHAINS MATCHS' ===\n" + haut
        + "\n\n=== LIGNES AVEC UN HORAIRE ===\n" + exemples
        + "\n\n=== HTML BRUT ===\n" + brut
    )
    return Response(texte, mimetype="text/plain; charset=utf-8")

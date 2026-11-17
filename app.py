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

        if RE_HEURE.match(ligne) and jour_courant and i + 1 < n:
            equipes = lignes[i + 1]
            if len(re.split(r"\s+[–-]\s+", equipes)) == 2:
                comp = lignes[i + 2] if i + 2 < n else ""
                if comp.lower().startswith("prochain match"):
                    reste = comp[len("prochain match"):].strip()
                    comp = reste or (lignes[i + 3] if i + 3 < n else "")
                matchs.append({
                    "date": jour_courant,
                    "heure": ligne,
                    "match": equipes,
                    "competition": comp,
                })
                i += 3
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
    try:
        matchs = charger_matchs()
    except requests.RequestException as e:
        return jsonify(erreur=str(e)), 502
    return jsonify(
        nombre_de_matchs=len(matchs),
        exemples=[
            {**m, "date": m["date"].isoformat()} for m in matchs[:3]
        ],
    )

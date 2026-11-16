import os
from datetime import date

import requests
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

TOKEN = os.environ.get("SPORTMONKS_TOKEN")
BASE = "https://api.sportmonks.com/v3/football"


@app.route("/")
def accueil():
    return render_template("index.html")


@app.route("/search")
def search():
    if not TOKEN:
        return jsonify(erreur="Token manquant sur Render"), 500

    q = request.args.get("q", "").lower().strip()
    jour = request.args.get("date", date.today().isoformat())

    r = requests.get(
        f"{BASE}/fixtures/date/{jour}",
        params={
            "api_token": TOKEN,
            "include": "participants;tvStations.tvStation;tvStations.country",
        },
        timeout=20,
    )
    if r.status_code != 200:
        return jsonify(erreur="Erreur Sportmonks", code=r.status_code), 502

    resultats = []
    for match in r.json().get("data", []):
        nom = match.get("name", "")
        if q and q not in nom.lower():
            continue

        chaines = []
        for t in match.get("tvstations", []):
            pays = (t.get("country") or {}).get("name", "")
            if pays.lower() == "france":
                chaines.append(t["tvstation"]["name"])

        resultats.append(
            {"match": nom, "heure": match.get("starting_at"), "chaines": chaines}
        )

    return jsonify(resultats)

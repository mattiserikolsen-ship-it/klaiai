#!/usr/bin/env python3
"""
NexOlsen Rapport Agent
Genererer professionelle AI-rapporter til klienter via Claude API
"""

import anthropic
import json
import sys
from datetime import datetime

client = anthropic.Anthropic()

def generer_rapport(klient: dict, rapport_type: str = "månedlig") -> str:
    """Genererer en professionel rapport for en klient via Claude."""

    dato = datetime.now().strftime("%d. %B %Y")
    maaned = datetime.now().strftime("%B %Y")

    prompt = f"""Du er en professionel AI-assistent der genererer forretningsrapporter for danske virksomheder.

Klient: {klient['navn']}
Branche: {klient.get('branche', 'Ikke angivet')}
Beskrivelse: {klient.get('beskrivelse', 'Ikke angivet')}
Produkter/ydelser: {klient.get('produkter', 'Ikke angivet')}
Rapport type: {rapport_type}
Dato: {dato}

Generer en professionel {rapport_type} rapport på dansk. Rapporten skal indeholde:

1. **Resumé** — kort overblik over perioden
2. **Nøgletal** — 3-5 relevante KPI'er for branchen (lav realistiske estimater baseret på virksomhedstypen)
3. **Hvad gik godt** — 3 positive punkter
4. **Opmærksomhedspunkter** — 2-3 ting der bør fokuseres på
5. **Anbefalinger** — 3 konkrete handlinger til næste måned
6. **Konklusion** — en motiverende afslutning

Skriv professionelt men forståeligt. Brug dansk. Rapporten skal føles personlig og relevant for netop denne virksomhed."""

    print(f"Genererer {rapport_type} rapport for {klient['navn']}...")
    print("-" * 50)

    rapport_tekst = ""

    with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}]
    ) as stream:
        for text in stream.text_stream:
            print(text, end="", flush=True)
            rapport_tekst += text

    print("\n" + "-" * 50)
    return rapport_tekst


def gem_rapport(klient_navn: str, rapport: str, rapport_type: str):
    """Gemmer rapporten som en tekstfil."""
    dato = datetime.now().strftime("%Y-%m-%d")
    filnavn = f"/Users/mattis/claude/klaiai/agents/rapporter/{klient_navn.replace(' ', '_')}_{rapport_type}_{dato}.txt"

    import os
    os.makedirs(os.path.dirname(filnavn), exist_ok=True)

    header = f"""NexOlsen — Automatisk genereret rapport
=====================================
Klient: {klient_navn}
Type: {rapport_type}
Dato: {datetime.now().strftime("%d/%m/%Y %H:%M")}
=====================================

"""
    with open(filnavn, 'w', encoding='utf-8') as f:
        f.write(header + rapport)

    print(f"\n✅ Rapport gemt: {filnavn}")
    return filnavn


if __name__ == "__main__":
    # Eksempel klient — i fremtiden hentes dette fra databasen
    eksempel_klient = {
        "navn": "Petersens Pool Service",
        "branche": "Pool & havebassiner",
        "beskrivelse": "Rengøring og vedligeholdelse af swimmingpools i Storkøbenhavn",
        "produkter": "Poolrengøring, kemikalier, vinterlukning, åbning af sæson"
    }

    # Hent klient fra kommandolinje hvis angivet
    if len(sys.argv) > 1:
        try:
            eksempel_klient = json.loads(sys.argv[1])
        except:
            print("Bruger standard-klient...")

    rapport_type = sys.argv[2] if len(sys.argv) > 2 else "månedlig"

    rapport = generer_rapport(eksempel_klient, rapport_type)
    gem_rapport(eksempel_klient['navn'], rapport, rapport_type)

    print("\n🤖 NexOlsen Rapport Agent færdig!")

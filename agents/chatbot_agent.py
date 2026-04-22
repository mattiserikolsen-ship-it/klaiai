#!/usr/bin/env python3
"""
KlarAI Chatbot Agent
Håndterer AI-chat for klienters hjemmesider via Claude API
"""

from flask import Flask, request, jsonify
from flask_cors import CORS
import anthropic
import json
import os

app = Flask(__name__)
CORS(app)

client = anthropic.Anthropic()

# Klient-konfigurationer (i fremtiden fra database)
KLIENTER = {
    "demo": {
        "navn": "Demo Virksomhed",
        "chatbot_navn": "Alma",
        "velkomst": "Hej! Jeg er Alma. Hvordan kan jeg hjælpe dig?",
        "info": """
Åbningstider: Mandag-fredag 8:00-17:00, lørdag 9:00-13:00
Kontakt: info@demo.dk | +45 12 34 56 78
Vi tilbyder: Eksempel ydelse 1, Eksempel ydelse 2
Priser: Fra 499 kr.
Levering: 2-3 hverdage
        """.strip(),
        "farve": "#0a2463"
    },
    "pool": {
        "navn": "Petersens Pool Service",
        "chatbot_navn": "Max",
        "velkomst": "Hej! Jeg er Max fra Petersens Pool Service 🏊 Kan jeg hjælpe dig med noget?",
        "info": """
Vi tilbyder: Poolrengøring, kemikalier, vinterlukning, sæsonåbning
Område: Storkøbenhavn (Gentofte, Lyngby, Hellerup, Charlottenlund)
Åbningstider: Man-fre 7:00-18:00, lør 8:00-14:00
Priser: Rengøring fra 895 kr., Sæsonpakke fra 3.995 kr./år
Kontakt: info@petersenspool.dk | +45 98 76 54 32
Booking: Vi kan oftest komme inden for 2-3 hverdage
        """.strip(),
        "farve": "#0077b6"
    }
}

def byg_system_prompt(klient: dict) -> str:
    return f"""Du er {klient['chatbot_navn']}, en hjælpsom AI-assistent for {klient['navn']}.

Din opgave er at besvare kundehenvendelser professionelt, venligt og præcist på dansk.

Information om virksomheden:
{klient['info']}

Regler:
- Svar altid på dansk
- Vær venlig og imødekommende
- Hvis du ikke ved svaret, bed kunden kontakte virksomheden direkte
- Hold svarene korte og præcise (max 3-4 sætninger)
- Tilbyd aldrig rabatter eller priser der ikke fremgår af info
- Afslut gerne med et opfølgningsspørgsmål eller tilbud om hjælp"""


@app.route('/chat', methods=['POST'])
def chat():
    data = request.json
    klient_id = data.get('client', 'demo')
    besked = data.get('message', '')
    historik = data.get('history', [])

    if not besked:
        return jsonify({'error': 'Ingen besked'}), 400

    klient = KLIENTER.get(klient_id, KLIENTER['demo'])

    # Byg besked-historik
    messages = []
    for msg in historik[-10:]:  # Max 10 tidligere beskeder
        messages.append({
            "role": msg['role'],
            "content": msg['content']
        })
    messages.append({"role": "user", "content": besked})

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",  # Hurtig og billig til chat
            max_tokens=500,
            system=byg_system_prompt(klient),
            messages=messages
        )

        svar = response.content[0].text
        return jsonify({
            'reply': svar,
            'chatbot_navn': klient['chatbot_navn']
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/widget/<klient_id>', methods=['GET'])
def widget_config(klient_id):
    """Returnerer konfiguration til chatbot widget"""
    klient = KLIENTER.get(klient_id, KLIENTER['demo'])
    return jsonify({
        'navn': klient['chatbot_navn'],
        'velkomst': klient['velkomst'],
        'farve': klient['farve']
    })


@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'klienter': list(KLIENTER.keys())})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    print(f"🤖 KlarAI Chatbot Agent kører på port {port}")
    print(f"   Klienter: {', '.join(KLIENTER.keys())}")
    app.run(host='0.0.0.0', port=port, debug=True)

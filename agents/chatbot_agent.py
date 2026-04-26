#!/usr/bin/env python3
"""
SittamTech Chatbot Agent
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

CONFIG_FILE = os.path.join(os.path.dirname(__file__), 'clients_config.json')

def load_klienter():
    """Indlæser klient-konfigurationer fra JSON-fil."""
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

def get_klient(klient_id: str) -> dict:
    klienter = load_klienter()
    return klienter.get(klient_id, klienter.get('demo', {}))

def byg_system_prompt(klient: dict) -> str:
    info = klient.get('info', {})
    info_tekst = '\n'.join([f"{k.capitalize()}: {v}" for k, v in info.items() if v])
    return f"""Du er {klient.get('chatbot_navn','Alma')}, en hjælpsom AI-assistent for {klient.get('navn','virksomheden')}.

Din opgave er at besvare kundehenvendelser professionelt, venligt og præcist på dansk.

Information om virksomheden:
{info_tekst}

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

    klient = get_klient(klient_id)

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
    klient = get_klient(klient_id)
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
    print(f"🤖 SittamTech Chatbot Agent kører på port {port}")
    print(f"   Klienter: {', '.join(KLIENTER.keys())}")
    app.run(host='0.0.0.0', port=port, debug=True)

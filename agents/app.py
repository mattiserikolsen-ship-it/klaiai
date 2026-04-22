#!/usr/bin/env python3
"""
KlarAI Agent Server
Samlet Flask-app med Chatbot Agent + Lead Agent
Klar til deployment på Render / Railway
"""

from flask import Flask, request, jsonify
from flask_cors import CORS
import anthropic
import json
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

app = Flask(__name__)
CORS(app)

ai = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))

CONFIG_FILE = os.path.join(os.path.dirname(__file__), 'clients_config.json')
GMAIL_USER = os.environ.get('GMAIL_USER', '')
GMAIL_APP_PASSWORD = os.environ.get('GMAIL_APP_PASSWORD', '')


# ── HELPERS ────────────────────────────────────────────

def load_klienter():
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except:
        return {}

def get_klient(klient_id):
    klienter = load_klienter()
    return klienter.get(klient_id, klienter.get('demo', {}))

def byg_chatbot_prompt(klient):
    info = klient.get('info', {})
    info_tekst = '\n'.join([f"{k.capitalize()}: {v}" for k, v in info.items() if v])
    return f"""Du er {klient.get('chatbot_navn','Alma')}, AI-assistent for {klient.get('navn','virksomheden')}.

Virksomhedsinfo:
{info_tekst}

Regler: Svar på dansk. Vær kort (max 3-4 sætninger). Vær venlig. Hvis du ikke ved svaret, henvis til kontaktinfo. Tilbyd aldrig priser der ikke fremgår af info."""


# ── CHATBOT ENDPOINTS ──────────────────────────────────

@app.route('/chat', methods=['POST'])
def chat():
    data = request.json
    klient_id = data.get('client', 'demo')
    besked = data.get('message', '')
    historik = data.get('history', [])

    if not besked:
        return jsonify({'error': 'Ingen besked'}), 400

    klient = get_klient(klient_id)
    messages = [{'role': m['role'], 'content': m['content']} for m in historik[-10:]]
    messages.append({'role': 'user', 'content': besked})

    try:
        response = ai.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=500,
            system=byg_chatbot_prompt(klient),
            messages=messages
        )
        return jsonify({
            'reply': response.content[0].text,
            'chatbot_navn': klient.get('chatbot_navn', 'Alma')
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/widget/<klient_id>', methods=['GET'])
def widget_config(klient_id):
    klient = get_klient(klient_id)
    return jsonify({
        'navn': klient.get('chatbot_navn', 'Alma'),
        'velkomst': klient.get('velkomst', 'Hej! Hvordan kan jeg hjælpe?'),
        'farve': klient.get('farve', '#0a2463')
    })


# ── LEAD ENDPOINTS ─────────────────────────────────────

@app.route('/lead', methods=['POST'])
def modtag_lead():
    """Modtager et nyt lead og genererer opfølgningsmails."""
    data = request.json
    lead = data.get('lead', {})
    klient_id = data.get('client', 'demo')
    send_nu = data.get('send', False)

    if not lead.get('email') and not lead.get('navn'):
        return jsonify({'error': 'Lead mangler email eller navn'}), 400

    klienter = load_klienter()
    klient = klienter.get(klient_id, {})
    klient_info = {
        'navn': klient.get('navn', 'Virksomheden'),
        'ydelser': klient.get('info', {}).get('ydelser', ''),
        'tilbud': klient.get('lead_tilbud', 'Gratis uforpligtende samtale'),
        'kontakt': klient.get('info', {}).get('kontakt', '')
    }

    mails = []
    for nr in [1, 2, 3]:
        mail = generer_lead_mail(lead, klient_info, nr)
        if send_nu and lead.get('email') and GMAIL_USER:
            sendt = send_mail(lead['email'], mail['emne'], mail['tekst'], klient_info['navn'])
            mail['sendt'] = sendt
        else:
            mail['sendt'] = False
        mails.append(mail)

    return jsonify({
        'success': True,
        'lead': lead.get('navn'),
        'mails_genereret': len(mails),
        'mails': mails
    })


def generer_lead_mail(lead, klient, mail_nr):
    instruktion = {
        1: 'Svar straks og tak for henvendelsen. Introducer virksomheden kort og tilbyd hjælp.',
        2: 'Venlig opfølgning. Spørg om de har haft mulighed for at kigge på tilbuddet.',
        3: 'Sidste opfølgning. Gør det personligt og lav et konkret tilbud.'
    }.get(mail_nr, '')

    response = ai.messages.create(
        model='claude-haiku-4-5-20251001',
        max_tokens=400,
        messages=[{'role': 'user', 'content': f"""Skriv opfølgningsmail #{mail_nr} til dette lead.

Lead: {lead.get('navn','der')} fra {lead.get('virksomhed','')}
Henvendelse: {lead.get('besked','Generel forespørgsel')}
Klient: {klient['navn']} — {klient.get('ydelser','')}
Tilbud: {klient.get('tilbud','')}
Kontakt: {klient.get('kontakt','')}

{instruktion}

Skriv kort (3-5 linjer), personlig, professionel dansk. Ingen emojis.

EMNE: <emnet>
TEKST:
<brødteksten>"""}]
    )

    tekst = response.content[0].text.strip()
    lines = tekst.split('\n')
    emne, body, i_body = '', [], False
    for line in lines:
        if line.startswith('EMNE:'):
            emne = line.replace('EMNE:', '').strip()
        elif line.startswith('TEKST:'):
            i_body = True
        elif i_body:
            body.append(line)

    return {'emne': emne or f'Opfølgning fra {klient["navn"]}', 'tekst': '\n'.join(body).strip(), 'mail_nr': mail_nr}


def send_mail(til, emne, tekst, fra_navn):
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        return False
    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = emne
        msg['From'] = f"{fra_navn} <{GMAIL_USER}>"
        msg['To'] = til
        msg.attach(MIMEText(tekst, 'plain', 'utf-8'))
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as s:
            s.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            s.sendmail(GMAIL_USER, til, msg.as_string())
        return True
    except:
        return False


# ── GENERAL ────────────────────────────────────────────

@app.route('/health', methods=['GET'])
def health():
    klienter = load_klienter()
    return jsonify({
        'status': 'ok',
        'klienter': list(klienter.keys()),
        'gmail': bool(GMAIL_USER)
    })

@app.route('/', methods=['GET'])
def index():
    return jsonify({'app': 'KlarAI Agent Server', 'version': '1.0', 'endpoints': ['/chat', '/lead', '/widget/<id>', '/health']})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    print(f"🤖 KlarAI Agent Server kører på port {port}")
    app.run(host='0.0.0.0', port=port, debug=False)

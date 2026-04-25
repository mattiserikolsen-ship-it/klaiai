#!/usr/bin/env python3
"""
KlarAI Agent Server
Samlet Flask-app med Chatbot Agent + Lead Agent
Klar til deployment på Render / Railway
"""

from flask import Flask, request, jsonify, Response
from flask_cors import CORS
import anthropic
import json
import os
import secrets
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail
from supabase import create_client
import functools

# ── TOKEN STORE (in-memory) ────────────────────────────
active_tokens = {}  # token -> {'role': 'admin'/'client', 'klient_id': ...}

app = Flask(__name__, static_folder='../app', static_url_path='/app')
CORS(app)

ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'klaiai2024')

def require_auth(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or auth.password != ADMIN_PASSWORD:
            return Response(
                'Adgang kræver login.',
                401,
                {'WWW-Authenticate': 'Basic realm="KlarAI Admin"'}
            )
        return f(*args, **kwargs)
    return decorated

ai = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))

def er_klient_aktiv(klient_id):
    """Returnerer True hvis klienten er aktiv, False hvis deaktiveret"""
    if not db:
        return True  # Ingen DB = tillad (fail open)
    try:
        res = db.table('klienter').select('aktiv').eq('id', klient_id).single().execute()
        if res.data:
            return res.data.get('aktiv', True)
    except:
        pass
    return True

CONFIG_FILE = os.path.join(os.path.dirname(__file__), 'clients_config.json')
SENDGRID_API_KEY = os.environ.get('SENDGRID_API_KEY', '')
SENDGRID_FROM = os.environ.get('SENDGRID_FROM', '')

SUPABASE_URL = os.environ.get('SUPABASE_URL', '')
SUPABASE_KEY = os.environ.get('SUPABASE_KEY', '')
db = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None


# ── HELPERS ────────────────────────────────────────────

def load_klienter():
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except:
        return {}

def get_klient(klient_id):
    # Prøv Supabase først
    if db:
        try:
            res = db.table('chatbot_config').select('*, klienter(*)').eq('klient_id', klient_id).single().execute()
            if res.data:
                cfg = res.data
                klient = cfg.get('klienter', {})
                return {
                    'navn': klient.get('navn', ''),
                    'chatbot_navn': cfg.get('chatbot_navn', 'Alma'),
                    'velkomst': cfg.get('velkomst', 'Hej! Hvordan kan jeg hjælpe?'),
                    'farve': cfg.get('farve', '#0a2463'),
                    'info': {
                        'åbningstider': cfg.get('aabningsider', ''),
                        'kontakt': cfg.get('kontakt', ''),
                        'ydelser': cfg.get('ydelser', ''),
                        'priser': cfg.get('priser', ''),
                        'adresse': cfg.get('adresse', ''),
                        'andet': cfg.get('andet', '')
                    },
                    'ekstra_viden': cfg.get('ekstra_viden', '')
                }
        except:
            pass
    # Fallback til JSON-fil
    klienter = load_klienter()
    return klienter.get(klient_id, klienter.get('demo', {}))

LEAD_TOOL = [
    {
        "name": "gem_lead",
        "description": "Gem kundens kontaktoplysninger som et lead. Kald denne funktion når kunden har givet navn og telefonnummer eller email.",
        "input_schema": {
            "type": "object",
            "properties": {
                "navn": {"type": "string", "description": "Kundens fulde navn"},
                "telefon": {"type": "string", "description": "Kundens telefonnummer"},
                "email": {"type": "string", "description": "Kundens email (hvis opgivet)"},
                "besked": {"type": "string", "description": "Kort beskrivelse af hvad kunden er interesseret i"}
            },
            "required": ["navn", "besked"]
        }
    }
]

def byg_chatbot_prompt(klient):
    info = klient.get('info', {})
    info_tekst = '\n'.join([f"{k.capitalize()}: {v}" for k, v in info.items() if v])
    ekstra = klient.get('ekstra_viden', '').strip()
    ekstra_sektion = f"\n\nEkstra viden fra dokumenter:\n{ekstra}" if ekstra else ""
    return f"""Du er {klient.get('chatbot_navn','Alma')}, AI-assistent for {klient.get('navn','virksomheden')}.

Virksomhedsinfo:
{info_tekst}{ekstra_sektion}

Regler:
- Svar på dansk. Vær kort (max 3-4 sætninger). Vær venlig.
- Tilbyd aldrig priser der ikke fremgår af info.
- Hvis du ikke ved svaret, henvis til kontaktinfo.

LEAD-OPSAMLING (vigtigt):
Når en kunde spørger om pris, tilbud, hvad noget koster, eller ønsker at blive kontaktet — svar kort på spørgsmålet og spørg derefter: "Må jeg få dit navn og telefonnummer, så vi kan kontakte dig med et tilbud?"
Når kunden giver navn og telefon (eller email), svar bekræftende OG kald gem_lead funktionen med kundens oplysninger."""


def gem_lead_i_db(klient_id, lead_data):
    """Gemmer lead i Supabase og sender notifikation til klient."""
    if db:
        try:
            db.table('leads').insert({
                'klient_id': klient_id,
                'navn': lead_data.get('navn', ''),
                'email': lead_data.get('email', ''),
                'telefon': lead_data.get('telefon', ''),
                'virksomhed': '',
                'besked': lead_data.get('besked', ''),
                'kilde': 'chatbot',
                'status': 'ny'
            }).execute()
        except Exception as e:
            print(f"Lead DB fejl: {e}")

    klient_info = get_klient(klient_id)
    kontakt = klient_info.get('info', {}).get('kontakt', '')
    notif_mail = kontakt.split('|')[-1].strip() if '|' in kontakt else kontakt.strip()
    if SENDGRID_API_KEY and notif_mail and '@' in notif_mail:
        emne = f"Nyt lead via chatbot — {lead_data.get('navn', 'Ukendt')}"
        tekst = f"""Nyt lead opsamlet via chatbotten!

Navn: {lead_data.get('navn', '')}
Telefon: {lead_data.get('telefon', '')}
Email: {lead_data.get('email', '')}
Interesse: {lead_data.get('besked', '')}

Log ind på din KlarAI portal for at se alle leads."""
        send_mail(notif_mail, emne, tekst, klient_info.get('navn', 'KlarAI'))


# ── GAP DETEKTION ──────────────────────────────────────

DEFLECTION_FRASER = [
    'kontakt os', 'ring til os', 'send os en mail', 'send en mail',
    'har desværre ikke', 'kan desværre ikke', 'ved desværre ikke',
    'ikke information om', 'ingen information om', 'kan ikke svare på',
    'ikke bekendt med', 'har ikke den information', 'kan ikke hjælpe med det',
    'ikke nok information', 'beklager, jeg ved ikke', 'desværre ikke i stand'
]

def er_deflection(svar: str) -> bool:
    svar_l = svar.lower()
    return any(f in svar_l for f in DEFLECTION_FRASER)

def log_gap(klient_id: str, spoergsmaal: str, bot_svar: str):
    if not db or klient_id == 'demo':
        return
    try:
        db.table('chatbot_gaps').insert({
            'klient_id': klient_id,
            'spoergsmaal': spoergsmaal,
            'bot_svar': bot_svar,
            'status': 'åben'
        }).execute()
    except Exception as e:
        print(f"Gap log fejl: {e}")


# ── CHATBOT ENDPOINTS ──────────────────────────────────

@app.route('/chat', methods=['POST'])
def chat():
    data = request.json
    klient_id = data.get('client', 'demo')
    besked = data.get('message', '')
    historik = data.get('history', [])

    if not besked:
        return jsonify({'error': 'Ingen besked'}), 400

    if klient_id != 'demo' and not er_klient_aktiv(klient_id):
        return jsonify({'svar': 'Denne chatbot er ikke tilgængelig i øjeblikket.'}), 403

    klient = get_klient(klient_id)
    messages = [{'role': m['role'], 'content': m['content']} for m in historik[-10:]]
    messages.append({'role': 'user', 'content': besked})

    try:
        response = ai.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=600,
            system=byg_chatbot_prompt(klient),
            tools=LEAD_TOOL,
            messages=messages
        )

        reply = ''
        lead_gemt = False

        for block in response.content:
            if block.type == 'text':
                reply += block.text
            elif block.type == 'tool_use' and block.name == 'gem_lead':
                gem_lead_i_db(klient_id, block.input)
                lead_gemt = True

        # Hvis AI'en kun kaldte tool og ikke gav tekst, hent et afsluttende svar
        if not reply.strip() and lead_gemt:
            messages.append({'role': 'assistant', 'content': response.content})
            messages.append({'role': 'user', 'content': '[system: lead er gemt, skriv en kort bekræftelse til kunden]'})
            followup = ai.messages.create(
                model='claude-haiku-4-5-20251001',
                max_tokens=200,
                system=byg_chatbot_prompt(klient),
                tools=LEAD_TOOL,
                messages=messages
            )
            for block in followup.content:
                if block.type == 'text':
                    reply += block.text

        reply_final = reply.strip()

        # Log gap hvis botten ikke kunne svare
        if reply_final and er_deflection(reply_final):
            log_gap(klient_id, besked, reply_final)

        return jsonify({
            'reply': reply_final,
            'chatbot_navn': klient.get('chatbot_navn', 'Alma'),
            'lead_gemt': lead_gemt
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/widget/<klient_id>', methods=['GET'])
def widget_config(klient_id):
    if not er_klient_aktiv(klient_id):
        return jsonify({'error': 'inaktiv'}), 403
    klient = get_klient(klient_id)
    info = klient.get('info', {})
    return jsonify({
        'navn': klient.get('chatbot_navn', 'Alma'),
        'velkomst': klient.get('velkomst', 'Hej! Hvordan kan jeg hjælpe?'),
        'farve': klient.get('farve', '#0a2463'),
        'ekstra_viden': klient.get('ekstra_viden', ''),
        'info': {
            'åbningstider': info.get('åbningstider', ''),
            'kontakt': info.get('kontakt', ''),
            'ydelser': info.get('ydelser', ''),
            'priser': info.get('priser', ''),
            'adresse': info.get('adresse', ''),
            'andet': info.get('andet', '')
        }
    })


# ── LEAD ENDPOINTS ─────────────────────────────────────

@app.route('/lead', methods=['POST'])
def modtag_lead():
    """Modtager et nyt lead og genererer opfølgningsmails."""
    data = request.json
    lead = data.get('lead', {})
    klient_id = data.get('client', 'demo')
    send_nu = data.get('send', False)

    if klient_id != 'demo' and not er_klient_aktiv(klient_id):
        return jsonify({'error': 'Denne service er ikke tilgængelig'}), 403

    if not lead.get('email') and not lead.get('navn'):
        return jsonify({'error': 'Lead mangler email eller navn'}), 400

    # Gem lead i Supabase
    if db:
        try:
            db.table('leads').insert({
                'klient_id': klient_id,
                'navn': lead.get('navn', ''),
                'email': lead.get('email', ''),
                'telefon': lead.get('telefon', ''),
                'virksomhed': lead.get('virksomhed', ''),
                'besked': lead.get('besked', ''),
                'status': 'ny'
            }).execute()
        except Exception as e:
            print(f"Lead DB fejl: {e}")

    # Hent klientinfo fra Supabase eller JSON
    klient = get_klient(klient_id)
    klient_info = {
        'navn': klient.get('navn', 'Virksomheden'),
        'ydelser': klient.get('info', {}).get('ydelser', ''),
        'tilbud': klient.get('lead_tilbud', 'Gratis uforpligtende samtale'),
        'kontakt': klient.get('info', {}).get('kontakt', '')
    }

    mails = []
    for nr in [1, 2, 3]:
        mail = generer_lead_mail(lead, klient_info, nr)
        mail['sendt'] = False
        mails.append(mail)

    # Tjek om klient har auto-godkend slået til
    auto_godkend = False
    lead_db_id = None
    if db:
        try:
            cfg = db.table('chatbot_config').select('auto_godkend_mails').eq('klient_id', klient_id).execute()
            if cfg.data:
                auto_godkend = cfg.data[0].get('auto_godkend_mails', False)
            # Hent lead id
            lead_res = db.table('leads').select('id').eq('klient_id', klient_id).eq('navn', lead.get('navn','')).order('oprettet', desc=True).limit(1).execute()
            if lead_res.data:
                lead_db_id = str(lead_res.data[0]['id'])
        except:
            pass

    if auto_godkend and lead.get('email') and SENDGRID_API_KEY:
        # Send straks
        for mail in mails:
            sendt = send_mail(lead['email'], mail['emne'], mail['tekst'], klient_info['navn'])
            mail['sendt'] = sendt
        if db and lead_db_id:
            try:
                for mail in mails:
                    db.table('lead_mails').insert({
                        'lead_id': lead_db_id, 'klient_id': klient_id,
                        'mail_nr': mail['mail_nr'], 'emne': mail['emne'],
                        'tekst': mail['tekst'], 'status': 'sendt'
                    }).execute()
            except: pass
    else:
        # Gem som "afventer godkendelse"
        if db and lead_db_id:
            try:
                for mail in mails:
                    db.table('lead_mails').insert({
                        'lead_id': lead_db_id, 'klient_id': klient_id,
                        'mail_nr': mail['mail_nr'], 'emne': mail['emne'],
                        'tekst': mail['tekst'], 'status': 'afventer'
                    }).execute()
            except: pass

    return jsonify({
        'success': True,
        'lead': lead.get('navn'),
        'mails_genereret': len(mails),
        'auto_godkendt': auto_godkend,
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
    if not SENDGRID_API_KEY or not SENDGRID_FROM:
        return False
    try:
        html = '<br>'.join(tekst.split('\n'))
        message = Mail(
            from_email=(SENDGRID_FROM, fra_navn),
            to_emails=til,
            subject=emne,
            plain_text_content=tekst,
            html_content=f'<div style="font-family:Arial,sans-serif;max-width:600px;padding:20px">{html}</div>'
        )
        sg = SendGridAPIClient(SENDGRID_API_KEY)
        response = sg.send(message)
        print(f"SendGrid status: {response.status_code}")
        return response.status_code in [200, 202]
    except Exception as e:
        print(f"SendGrid fejl: {e}")
        if hasattr(e, 'body'):
            print(f"SendGrid body: {e.body}")
        return False


# ── BOOKING ENDPOINTS ──────────────────────────────────

@app.route('/booking-config/<klient_id>', methods=['GET'])
def get_booking_config(klient_id):
    """Henter booking konfiguration for en klient"""
    if db:
        try:
            res = db.table('booking_config').select('*').eq('klient_id', klient_id).single().execute()
            if res.data:
                cfg = res.data
                return jsonify({
                    'titel': cfg.get('titel', 'Book en tid'),
                    'farve': cfg.get('farve', '#0a2463'),
                    'ydelser': cfg.get('ydelser', ['Konsultation']),
                    'dage': cfg.get('dage', [1,2,3,4,5]),
                    'start_tid': cfg.get('start_tid', '09:00'),
                    'slut_tid': cfg.get('slut_tid', '17:00'),
                    'varighed': cfg.get('varighed', 60),
                    'buffer': cfg.get('buffer', 0)
                })
        except:
            pass
    return jsonify({'titel': 'Book en tid', 'farve': '#0a2463', 'ydelser': ['Konsultation'], 'dage': [1,2,3,4,5], 'start_tid': '09:00', 'slut_tid': '17:00', 'varighed': 60, 'buffer': 0})


@app.route('/booking-config', methods=['POST'])
def gem_booking_config():
    """Gemmer booking konfiguration"""
    if not db:
        return jsonify({'error': 'Database ikke tilgængelig'}), 500
    data = request.json
    klient_id = data.get('klient_id')
    if not klient_id:
        return jsonify({'error': 'klient_id mangler'}), 400
    try:
        cfg = {
            'klient_id': klient_id,
            'titel': data.get('titel', 'Book en tid'),
            'farve': data.get('farve', '#0a2463'),
            'ydelser': data.get('ydelser', ['Konsultation']),
            'dage': data.get('dage', [1,2,3,4,5]),
            'start_tid': data.get('start_tid', '09:00'),
            'slut_tid': data.get('slut_tid', '17:00'),
            'varighed': data.get('varighed', 60),
            'buffer': data.get('buffer', 0)
        }
        res = db.table('booking_config').upsert(cfg, on_conflict='klient_id').execute()
        return jsonify({'success': True, 'config': res.data[0] if res.data else {}})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/booking-optaget/<klient_id>/<dato>', methods=['GET'])
def get_optaget(klient_id, dato):
    """Returnerer optagne tidspunkter for en given dato"""
    if not db:
        return jsonify({'optaget': []})
    try:
        res = db.table('bookinger').select('tid').eq('klient_id', klient_id).eq('dato', dato).eq('status', 'bekræftet').execute()
        tider = [r['tid'] for r in (res.data or [])]
        return jsonify({'optaget': tider})
    except Exception as e:
        return jsonify({'optaget': [], 'error': str(e)})


@app.route('/booking', methods=['POST'])
def modtag_booking():
    """Modtager en ny booking og sender bekræftelsesmail"""
    data = request.json
    booking = data.get('booking', {})
    klient_id = data.get('client', 'demo')

    if klient_id != 'demo' and not er_klient_aktiv(klient_id):
        return jsonify({'error': 'Denne service er ikke tilgængelig'}), 403

    if not booking.get('email') or not booking.get('navn'):
        return jsonify({'error': 'Booking mangler email eller navn'}), 400

    # Tjek dobbeltbooking
    dato = booking.get('dato', '')
    tid = booking.get('tid', '')
    if db and dato and tid:
        try:
            existing = db.table('bookinger').select('id').eq('klient_id', klient_id).eq('dato', dato).eq('tid', tid).eq('status', 'bekræftet').execute()
            if existing.data:
                return jsonify({'error': 'Dette tidspunkt er desværre allerede booket. Vælg venligst et andet tidspunkt.'}), 409
        except Exception as e:
            print(f"Dobbeltbooking-tjek fejl: {e}")

    # Gem i Supabase
    if db:
        try:
            db.table('bookinger').insert({
                'klient_id': klient_id,
                'navn': booking.get('navn', ''),
                'email': booking.get('email', ''),
                'telefon': booking.get('telefon', ''),
                'ydelse': booking.get('ydelse', ''),
                'dato': booking.get('dato', ''),
                'tid': booking.get('tid', ''),
                'besked': booking.get('besked', ''),
                'status': 'bekræftet'
            }).execute()
        except Exception as e:
            print(f"Booking DB fejl: {e}")

    # Hent klientnavn
    klient = get_klient(klient_id)
    klient_navn = klient.get('navn', 'Virksomheden')

    # Send bekræftelsesmail til kunden
    if SENDGRID_API_KEY and booking.get('email'):
        dato_str = booking.get('dato', '')
        tid_str = booking.get('tid', '')
        ydelse_str = booking.get('ydelse', '')
        emne = f"Bookingbekræftelse — {klient_navn}"
        tekst = f"""Hej {booking.get('navn', '')},

Din booking er bekræftet!

Dato: {dato_str}
Tidspunkt: {tid_str}
{f'Ydelse: {ydelse_str}' if ydelse_str else ''}

Vi glæder os til at se dig. Har du spørgsmål, er du altid velkommen til at kontakte os.

Med venlig hilsen
{klient_navn}"""
        send_mail(booking['email'], emne, tekst, klient_navn)

    return jsonify({'success': True, 'booking': booking.get('navn')})


# ── KLIENT PORTAL ENDPOINTS ────────────────────────────

@app.route('/leads/<klient_id>', methods=['GET'])
def get_leads(klient_id):
    """Henter leads for en klient"""
    if not db:
        return jsonify({'leads': []})
    try:
        res = db.table('leads').select('*').eq('klient_id', klient_id).order('oprettet', desc=True).execute()
        return jsonify({'leads': res.data or []})
    except Exception as e:
        return jsonify({'leads': [], 'error': str(e)})


@app.route('/bookinger/<klient_id>', methods=['GET'])
def get_bookinger(klient_id):
    """Henter bookinger for en klient"""
    if not db:
        return jsonify({'bookinger': []})
    try:
        res = db.table('bookinger').select('*').eq('klient_id', klient_id).order('oprettet', desc=True).execute()
        return jsonify({'bookinger': res.data or []})
    except Exception as e:
        return jsonify({'bookinger': [], 'error': str(e)})


# ── LEAD MAILS PREVIEW & GODKENDELSE ──────────────────

@app.route('/lead-mails/<klient_id>', methods=['GET'])
def get_lead_mails(klient_id):
    """Henter afventende lead-mails til preview"""
    if not db:
        return jsonify({'mails': []})
    try:
        res = db.table('lead_mails').select('*').eq('klient_id', klient_id).eq('status', 'afventer').order('oprettet', desc=True).execute()
        # Gruppér per lead_id
        from collections import defaultdict
        grupper = defaultdict(list)
        for m in (res.data or []):
            grupper[m['lead_id']].append(m)
        result = [{'lead_id': lid, 'mails': sorted(ms, key=lambda x: x['mail_nr'])} for lid, ms in grupper.items()]
        return jsonify({'grupper': result})
    except Exception as e:
        return jsonify({'grupper': [], 'error': str(e)})


@app.route('/godkend-mails', methods=['POST'])
def godkend_mails(klient_id=None):
    """Godkender (og sender) lead-mails. Kan også opdatere tekst inden afsendelse."""
    data = request.json
    lead_id = data.get('lead_id')
    klient_id = data.get('klient_id')
    mails = data.get('mails', [])  # [{'id': X, 'emne': ..., 'tekst': ...}]
    auto_fremadrettet = data.get('auto_fremadrettet', False)

    if not db or not klient_id:
        return jsonify({'error': 'Mangler data'}), 400

    # Hent lead email
    lead_email, lead_navn = '', ''
    try:
        lr = db.table('leads').select('email,navn').eq('id', lead_id).single().execute()
        if lr.data:
            lead_email = lr.data.get('email', '')
            lead_navn = lr.data.get('navn', '')
    except: pass

    klient = get_klient(klient_id)
    klient_navn = klient.get('navn', 'Virksomheden')
    sendt_count = 0

    for m in mails:
        mail_id = m.get('id')
        emne = m.get('emne', '')
        tekst = m.get('tekst', '')

        # Opdater tekst i DB
        try:
            db.table('lead_mails').update({'emne': emne, 'tekst': tekst, 'status': 'sendt'}).eq('id', mail_id).execute()
        except: pass

        # Send hvis email findes
        if lead_email and SENDGRID_API_KEY:
            sendt = send_mail(lead_email, emne, tekst, klient_navn)
            if sendt:
                sendt_count += 1

    # Sæt auto-godkend hvis valgt
    if auto_fremadrettet:
        try:
            existing = db.table('chatbot_config').select('klient_id').eq('klient_id', klient_id).execute()
            if existing.data:
                db.table('chatbot_config').update({'auto_godkend_mails': True}).eq('klient_id', klient_id).execute()
            else:
                db.table('chatbot_config').insert({'klient_id': klient_id, 'auto_godkend_mails': True}).execute()
        except: pass

    return jsonify({'success': True, 'sendt': sendt_count, 'til': lead_email})


@app.route('/afvis-mails/<lead_id>', methods=['POST'])
def afvis_mails(lead_id):
    """Afviser/sletter afventende mails for et lead"""
    if not db:
        return jsonify({'error': 'Ingen database'}), 500
    try:
        db.table('lead_mails').update({'status': 'afvist'}).eq('lead_id', lead_id).eq('status', 'afventer').execute()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── CHATBOT GAPS ───────────────────────────────────────

@app.route('/gaps/<klient_id>', methods=['GET'])
def get_gaps(klient_id):
    """Henter ubesvarede spørgsmål for en klient"""
    if not db:
        return jsonify({'gaps': []})
    try:
        res = db.table('chatbot_gaps').select('*').eq('klient_id', klient_id).eq('status', 'åben').order('oprettet', desc=True).limit(20).execute()
        return jsonify({'gaps': res.data or []})
    except Exception as e:
        return jsonify({'gaps': [], 'error': str(e)})


@app.route('/udfyld-gap/<klient_id>', methods=['POST'])
def udfyld_gap(klient_id):
    """Claude genererer forslag til hvad der mangler i chatbot-konfigurationen"""
    data = request.json
    gap_id = data.get('gap_id')
    spoergsmaal = data.get('spoergsmaal', '')

    klient = get_klient(klient_id)
    info = klient.get('info', {})

    prompt = f"""En kunde stillede dette spørgsmål til en chatbot, men chatbotten kunne ikke svare:

Spørgsmål: "{spoergsmaal}"

Chatbottens nuværende info:
Ydelser: {info.get('ydelser', 'ikke udfyldt')}
Priser: {info.get('priser', 'ikke udfyldt')}
Åbningstider: {info.get('åbningstider', 'ikke udfyldt')}
Kontakt: {info.get('kontakt', 'ikke udfyldt')}
Adresse: {info.get('adresse', 'ikke udfyldt')}
Ekstra viden: {klient.get('ekstra_viden', 'ikke udfyldt')[:300] if klient.get('ekstra_viden') else 'ikke udfyldt'}

Virksomhed: {klient.get('navn', '')}

Returner KUN dette JSON-format:
{{
  "felt": "priser" eller "ydelser" eller "åbningstider" eller "adresse" eller "kontakt" eller "ekstra_viden",
  "forklaring": "Kort forklaring på hvorfor dette felt skal opdateres (1 sætning)",
  "forslag": "Den præcise tekst der skal tilføjes til feltet"
}}"""

    try:
        response = ai.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=400,
            messages=[{'role': 'user', 'content': prompt}]
        )
        raw = response.content[0].text.strip()
        if '```' in raw:
            raw = raw.split('```')[1]
            if raw.startswith('json'): raw = raw[4:]
        result = json.loads(raw.strip())

        # Marker gap som "behandlet" hvis gap_id givet
        if gap_id and db:
            try:
                db.table('chatbot_gaps').update({'status': 'behandlet'}).eq('id', gap_id).execute()
            except:
                pass

        return jsonify({'success': True, **result})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/luk-gap/<gap_id>', methods=['POST'])
def luk_gap(gap_id):
    """Markerer et gap som lukket/ignoreret"""
    if not db:
        return jsonify({'error': 'Ingen database'}), 500
    try:
        db.table('chatbot_gaps').update({'status': 'ignoreret'}).eq('id', gap_id).execute()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── AI INSIGHTS & BRANCHE-RESEARCH ────────────────────

@app.route('/insights/<klient_id>', methods=['GET'])
def get_insights(klient_id):
    """Analyserer klientens opsætning og returnerer kritiske AI-forbedringer"""
    klient = get_klient(klient_id)
    leads, bookinger = [], []
    if db:
        try:
            leads = db.table('leads').select('kilde,oprettet,besked').eq('klient_id', klient_id).execute().data or []
            bookinger = db.table('bookinger').select('ydelse,oprettet').eq('klient_id', klient_id).execute().data or []
        except:
            pass

    info = klient.get('info', {})
    chatbot_navn = klient.get('chatbot_navn', 'Alma')
    velkomst = klient.get('velkomst', '')
    ekstra_viden = klient.get('ekstra_viden', '')

    chatbot = sum(1 for l in leads if l.get('kilde') == 'chatbot')
    formular = len(leads) - chatbot
    chatbot_pct = round(chatbot / len(leads) * 100) if leads else 0

    analyse_prompt = f"""Du er en skarp KlarAI-konsulent. Analyser denne klients AI-opsætning og returner præcis 4-5 kritiske, konkrete forbedringer i JSON.

KLIENT DATA:
Navn: {klient.get('navn', '')}
Hjemmeside: {klient.get('hjemmeside', klient.get('info', {}).get('adresse', ''))}
Ydelser: {info.get('ydelser', 'MANGLER')}
Priser: {info.get('priser', 'MANGLER')}
Åbningstider: {info.get('åbningstider', 'MANGLER')}
Kontakt: {info.get('kontakt', 'MANGLER')}
Adresse: {info.get('adresse', 'MANGLER')}
Chatbot-navn: {chatbot_navn}
Velkomstbesked: {velkomst or 'MANGLER'}
Ekstra viden: {'Udfyldt' if ekstra_viden else 'MANGLER'}

STATISTIK:
Leads i alt: {len(leads)}
Bookinger: {len(bookinger)}
Via chatbot: {chatbot} ({chatbot_pct}%)
Via formular: {formular}

Returner KUN dette JSON-format, intet andet:
{{
  "insights": [
    {{
      "id": "unik_id",
      "titel": "Kort, skarp titel",
      "problem": "Præcis beskrivelse af problemet og konsekvensen (1-2 sætninger)",
      "løsning": "Konkret handling der skal tages (1 sætning)",
      "alvor": "kritisk" eller "middel" eller "lav",
      "handling": "update_chatbot_config",
      "felt": "priser" eller "ydelser" eller "åbningstider" eller "velkomst" eller "adresse" eller "kontakt" eller "ekstra_viden",
      "forslag_vaerdi": "Den præcise tekst der skal indsættes"
    }}
  ]
}}

Regler:
- Kun "kritisk" hvis det direkte koster leads eller bookinger
- Forslag skal være SPECIFIKKE — ikke generiske råd
- Hvis et felt mangler, generer et realistisk forslag baseret på klientens branche
- "handling" er ALTID "update_chatbot_config"
- "forslag_vaerdi" er den PRÆCISE tekst der indsættes i feltet"""

    try:
        response = ai.messages.create(
            model='claude-sonnet-4-6',
            max_tokens=1500,
            messages=[{'role': 'user', 'content': analyse_prompt}]
        )
        raw = response.content[0].text.strip()
        # Udtræk JSON hvis pakket i markdown
        if '```' in raw:
            raw = raw.split('```')[1]
            if raw.startswith('json'): raw = raw[4:]
        result = json.loads(raw.strip())
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/apply-insight/<klient_id>', methods=['POST'])
def apply_insight(klient_id):
    """Implementerer et AI-indsigt ved at opdatere chatbot-config"""
    if not db:
        return jsonify({'error': 'Database ikke tilgængelig'}), 500

    data = request.json
    felt = data.get('felt')
    vaerdi = data.get('forslag_vaerdi')

    if not felt or not vaerdi:
        return jsonify({'error': 'felt og forslag_vaerdi er påkrævet'}), 400

    felt_map = {
        'priser': 'priser',
        'ydelser': 'ydelser',
        'åbningstider': 'aabningsider',
        'velkomst': 'velkomst',
        'adresse': 'adresse',
        'kontakt': 'kontakt',
        'ekstra_viden': 'ekstra_viden'
    }

    db_felt = felt_map.get(felt, felt)
    try:
        existing = db.table('chatbot_config').select('*').eq('klient_id', klient_id).execute()
        if existing.data:
            db.table('chatbot_config').update({db_felt: vaerdi}).eq('klient_id', klient_id).execute()
        else:
            db.table('chatbot_config').insert({'klient_id': klient_id, db_felt: vaerdi}).execute()
        return jsonify({'success': True, 'felt': felt, 'vaerdi': vaerdi})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/research-branche/<klient_id>', methods=['POST'])
def research_branche(klient_id):
    """Lader Claude researche branchen og beriger ekstra_viden automatisk"""
    if not db:
        return jsonify({'error': 'Database ikke tilgængelig'}), 500

    klient = get_klient(klient_id)
    info = klient.get('info', {})

    research_prompt = f"""Du er en brancheekspert og skal hjælpe en AI-chatbot med at forstå kundens branche bedre.

VIRKSOMHED:
Navn: {klient.get('navn', '')}
Ydelser: {info.get('ydelser', '')}
Priser: {info.get('priser', '')}
Adresse: {info.get('adresse', '')}

Generer en detaljeret brancheanalyse på dansk (500-700 ord) der inkluderer:

1. BRANCHEOVERBLIK: Hvad er normen i denne branche? Hvad forventer kunder?
2. TYPISKE KUNDESPØRGSMÅL: De 8-10 mest stillede spørgsmål kunder i denne branche stiller
3. KONKURRENCEFORDELE: Hvad adskiller de bedste virksomheder i branchen? Hvad værdsætter kunder?
4. PRISSÆTNING: Typiske priser og forretningsmodeller i branchen
5. SÆSONUDSVING: Er der sæsonmæssige mønstre? Hvornår er der mest efterspørgsel?
6. INDSIGELSER: Hvilke bekymringer/indsigelser har kunder typisk?

Skriv præcist og faktabaseret. Dette bruges til at træne en AI-chatbot til at svare bedre."""

    try:
        response = ai.messages.create(
            model='claude-sonnet-4-6',
            max_tokens=1200,
            messages=[{'role': 'user', 'content': research_prompt}]
        )
        research_tekst = response.content[0].text.strip()

        # Gem i chatbot_config ekstra_viden
        existing_cfg = db.table('chatbot_config').select('ekstra_viden').eq('klient_id', klient_id).execute()
        eksisterende = (existing_cfg.data[0].get('ekstra_viden', '') or '') if existing_cfg.data else ''
        ny_viden = f"{eksisterende}\n\n--- BRANCHE-RESEARCH (auto-genereret) ---\n{research_tekst}".strip()

        if existing_cfg.data:
            db.table('chatbot_config').update({'ekstra_viden': ny_viden}).eq('klient_id', klient_id).execute()
        else:
            db.table('chatbot_config').insert({'klient_id': klient_id, 'ekstra_viden': ny_viden}).execute()

        return jsonify({'success': True, 'research': research_tekst, 'tegn': len(research_tekst)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── RAPPORT ENDPOINTS ──────────────────────────────────

@app.route('/rapport/<klient_id>', methods=['GET'])
def get_rapport(klient_id):
    """Aggregeret rapport-data til grafer"""
    from datetime import datetime, timedelta

    if not db:
        return jsonify({'error': 'Database ikke tilgængelig'}), 500
    try:
        leads_res = db.table('leads').select('kilde,oprettet').eq('klient_id', klient_id).execute()
        leads = leads_res.data or []
        book_res = db.table('bookinger').select('dato,oprettet').eq('klient_id', klient_id).eq('status', 'bekræftet').execute()
        bookinger = book_res.data or []

        now = datetime.utcnow()

        # Seneste 8 uger
        uge_labels, leads_uge, book_uge = [], [], []
        for i in range(7, -1, -1):
            start = (now - timedelta(weeks=i + 1)).isoformat()
            slut  = (now - timedelta(weeks=i)).isoformat()
            label = (now - timedelta(weeks=i)).strftime('%-d. %b')
            uge_labels.append(label)
            leads_uge.append(sum(1 for l in leads if l.get('oprettet') and start <= l['oprettet'] < slut))
            book_uge.append(sum(1 for b in bookinger if b.get('oprettet') and start <= b['oprettet'] < slut))

        # Seneste 6 måneder
        mdr_labels, leads_mdr, book_mdr = [], [], []
        for i in range(5, -1, -1):
            mdr_start = (now.replace(day=1) - timedelta(days=i * 30)).replace(day=1)
            if i == 0:
                mdr_slut = now
            else:
                mdr_slut = (now.replace(day=1) - timedelta(days=(i - 1) * 30)).replace(day=1)
            label = mdr_start.strftime('%b')
            mdr_labels.append(label)
            leads_mdr.append(sum(1 for l in leads if l.get('oprettet') and mdr_start.isoformat() <= l['oprettet'] < mdr_slut.isoformat()))
            book_mdr.append(sum(1 for b in bookinger if b.get('oprettet') and mdr_start.isoformat() <= b['oprettet'] < mdr_slut.isoformat()))

        chatbot  = sum(1 for l in leads if l.get('kilde') == 'chatbot')
        formular = len(leads) - chatbot

        return jsonify({
            'total_leads': len(leads),
            'total_bookinger': len(bookinger),
            'kilde': {'chatbot': chatbot, 'formular': formular},
            'uger':     {'labels': uge_labels, 'leads': leads_uge, 'bookinger': book_uge},
            'maaneder': {'labels': mdr_labels, 'leads': leads_mdr, 'bookinger': book_mdr},
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def _hent_rapport_data(klient_id):
    """Henter leads + bookinger til rapport. Returnerer (leads, bookinger)."""
    leads, bookinger = [], []
    if db:
        try:
            leads = db.table('leads').select('*').eq('klient_id', klient_id).execute().data or []
            bookinger = db.table('bookinger').select('*').eq('klient_id', klient_id).eq('status', 'bekræftet').execute().data or []
        except:
            pass
    return leads, bookinger


def _byg_rapport_html(klient_id, klient_navn, leads, bookinger):
    """Bygger rapport HTML-streng."""
    from datetime import datetime
    chatbot  = sum(1 for l in leads if l.get('kilde') == 'chatbot')
    formular = len(leads) - chatbot
    dato_str = datetime.now().strftime('%-d. %B %Y')

    nye_leads_html = ''
    for l in leads[:5]:
        dato = l.get('oprettet', '')[:10] if l.get('oprettet') else '—'
        kilde_farve = '#4a6741' if l.get('kilde') == 'chatbot' else '#9a9590'
        kilde_tekst = 'Chatbot' if l.get('kilde') == 'chatbot' else 'Formular'
        nye_leads_html += f"""
        <tr>
          <td style="padding:10px 0;border-bottom:1px solid #f0efec;font-size:13px;color:#1a1918;font-weight:600">{l.get('navn','—')}</td>
          <td style="padding:10px 0;border-bottom:1px solid #f0efec;font-size:12px;color:#9a9590">{l.get('telefon') or l.get('email','—')}</td>
          <td style="padding:10px 0;border-bottom:1px solid #f0efec;font-size:11px;color:{kilde_farve};font-weight:700">{kilde_tekst}</td>
          <td style="padding:10px 0;border-bottom:1px solid #f0efec;font-size:11px;color:#9a9590;text-align:right">{dato}</td>
        </tr>"""

    if not nye_leads_html:
        nye_leads_html = '<tr><td colspan="4" style="padding:16px 0;font-size:13px;color:#9a9590;text-align:center">Ingen leads endnu</td></tr>'

    fornavn = klient_navn.split()[0] if klient_navn else 'der'
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"/><title>KlarAI Rapport — {dato_str}</title></head>
<body style="margin:0;padding:0;background:#f8f7f4;font-family:'Helvetica Neue',Arial,sans-serif">
<table width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:32px 16px">
<table width="600" cellpadding="0" cellspacing="0" style="max-width:600px">
  <tr><td style="background:#1a1918;border-radius:14px 14px 0 0;padding:28px 36px">
    <div style="color:#fff;font-size:22px;font-weight:800;letter-spacing:-0.5px">KlarAI</div>
    <div style="color:rgba(255,255,255,.4);font-size:10px;text-transform:uppercase;letter-spacing:1.5px;margin-top:3px">Klientrapport</div>
  </td></tr>
  <tr><td style="background:#fff;padding:28px 36px;border-left:1px solid #e5e3de;border-right:1px solid #e5e3de">
    <div style="font-size:18px;font-weight:700;color:#1a1918;margin-bottom:6px">Hej, {fornavn}!</div>
    <div style="font-size:13px;color:#9a9590;line-height:1.7">Her er din statusrapport fra KlarAI. Alt nedenfor er hvad dine AI-agenter har lavet for dig.</div>
    <div style="font-size:11px;color:#c5c2bc;margin-top:8px">{dato_str}</div>
  </td></tr>
  <tr><td style="background:#f8f7f4;padding:20px 36px;border-left:1px solid #e5e3de;border-right:1px solid #e5e3de">
    <table width="100%" cellpadding="0" cellspacing="0"><tr>
      <td width="33%" style="padding:4px">
        <div style="background:#fff;border:1px solid #e5e3de;border-radius:12px;padding:20px;text-align:center">
          <div style="font-size:36px;font-weight:800;color:#1a1918;letter-spacing:-2px">{len(leads)}</div>
          <div style="font-size:11px;color:#9a9590;margin-top:4px;font-weight:500">Leads i alt</div>
        </div>
      </td>
      <td width="33%" style="padding:4px">
        <div style="background:#fff;border:1px solid #e5e3de;border-radius:12px;padding:20px;text-align:center">
          <div style="font-size:36px;font-weight:800;color:#1a1918;letter-spacing:-2px">{len(bookinger)}</div>
          <div style="font-size:11px;color:#9a9590;margin-top:4px;font-weight:500">Bookinger</div>
        </div>
      </td>
      <td width="33%" style="padding:4px">
        <div style="background:#eef2ec;border:1px solid #c5d6c2;border-radius:12px;padding:20px;text-align:center">
          <div style="font-size:36px;font-weight:800;color:#4a6741;letter-spacing:-2px">{chatbot}</div>
          <div style="font-size:11px;color:#4a6741;margin-top:4px;font-weight:500">Via chatbot</div>
        </div>
      </td>
    </tr></table>
  </td></tr>
  <tr><td style="background:#fff;padding:28px 36px;border-left:1px solid #e5e3de;border-right:1px solid #e5e3de">
    <div style="font-size:13px;font-weight:700;color:#1a1918;margin-bottom:16px">Seneste leads</div>
    <table width="100%" cellpadding="0" cellspacing="0">
      <tr>
        <th style="font-size:10px;color:#9a9590;text-align:left;padding-bottom:8px;font-weight:600;text-transform:uppercase;letter-spacing:.5px">Navn</th>
        <th style="font-size:10px;color:#9a9590;text-align:left;padding-bottom:8px;font-weight:600;text-transform:uppercase;letter-spacing:.5px">Kontakt</th>
        <th style="font-size:10px;color:#9a9590;text-align:left;padding-bottom:8px;font-weight:600;text-transform:uppercase;letter-spacing:.5px">Kilde</th>
        <th style="font-size:10px;color:#9a9590;text-align:right;padding-bottom:8px;font-weight:600;text-transform:uppercase;letter-spacing:.5px">Dato</th>
      </tr>
      {nye_leads_html}
    </table>
  </td></tr>
  <tr><td style="background:#f8f7f4;padding:20px 36px;border-left:1px solid #e5e3de;border-right:1px solid #e5e3de">
    <div style="font-size:13px;font-weight:700;color:#1a1918;margin-bottom:12px">Leadkilde</div>
    <table width="100%" cellpadding="0" cellspacing="0"><tr>
      <td width="50%" style="padding:4px">
        <div style="background:#eef2ec;border-radius:10px;padding:14px 18px">
          <div style="font-size:11px;color:#4a6741;font-weight:700;text-transform:uppercase;letter-spacing:.5px">Chatbot</div>
          <div style="font-size:26px;font-weight:800;color:#4a6741;margin-top:4px">{chatbot}</div>
        </div>
      </td>
      <td width="50%" style="padding:4px">
        <div style="background:#fff;border:1px solid #e5e3de;border-radius:10px;padding:14px 18px">
          <div style="font-size:11px;color:#9a9590;font-weight:700;text-transform:uppercase;letter-spacing:.5px">Formular</div>
          <div style="font-size:26px;font-weight:800;color:#1a1918;margin-top:4px">{formular}</div>
        </div>
      </td>
    </tr></table>
  </td></tr>
  <tr><td style="background:#fff;padding:24px 36px;border:1px solid #e5e3de;border-radius:0 0 14px 14px;text-align:center">
    <a href="https://klaiai.dk/app/client.html?id={klient_id}" style="display:inline-block;background:#1a1918;color:#fff;text-decoration:none;font-size:13px;font-weight:700;padding:12px 28px;border-radius:9px">
      Se fuld portal →
    </a>
    <div style="font-size:11px;color:#c5c2bc;margin-top:16px">Drevet af KlarAI · klaiai.dk</div>
  </td></tr>
</table>
</td></tr></table>
</body></html>"""


@app.route('/preview-rapport/<klient_id>', methods=['GET'])
def preview_rapport(klient_id):
    """Returnerer rapport HTML direkte i browser — til forhåndsvisning"""
    from flask import Response
    klient = get_klient(klient_id)
    klient_navn = klient.get('navn', 'din virksomhed')
    leads, bookinger = _hent_rapport_data(klient_id)
    html = _byg_rapport_html(klient_id, klient_navn, leads, bookinger)
    return Response(html, mimetype='text/html')


@app.route('/send-rapport/<klient_id>', methods=['POST'])
def send_rapport(klient_id):
    """Sender en professionel HTML-rapport pr. mail"""
    klient = get_klient(klient_id)
    klient_navn = klient.get('navn', 'din virksomhed')
    kontakt = klient.get('info', {}).get('kontakt', '')
    mail_til = request.json.get('email') or (kontakt.split('|')[-1].strip() if '|' in kontakt else kontakt.strip())

    if not mail_til or '@' not in mail_til:
        return jsonify({'error': 'Ingen gyldig email fundet'}), 400

    leads, bookinger = _hent_rapport_data(klient_id)
    html = _byg_rapport_html(klient_id, klient_navn, leads, bookinger)

    from datetime import datetime
    dato_str = datetime.now().strftime('%-d. %B %Y')
    emne = f"Din KlarAI rapport — {dato_str}"
    if not SENDGRID_API_KEY or not SENDGRID_FROM:
        return jsonify({'success': False, 'error': 'Mail ikke konfigureret'}), 500

    try:
        message = Mail(
            from_email=(SENDGRID_FROM, 'KlarAI'),
            to_emails=mail_til,
            subject=emne,
            html_content=html
        )
        sg = SendGridAPIClient(SENDGRID_API_KEY)
        res = sg.send(message)
        return jsonify({'success': res.status_code in [200, 202], 'status': res.status_code, 'sendt_til': mail_til})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ── GENERAL ────────────────────────────────────────────

@app.route('/stats', methods=['GET'])
def stats():
    """Henter brugsstatistik fra Supabase"""
    if not db:
        return jsonify({'error': 'Database ikke tilgængelig'}), 500
    try:
        # Leads per klient
        leads_res = db.table('leads').select('klient_id, oprettet').execute()
        leads = leads_res.data or []

        # Bookinger per klient
        book_res = db.table('bookinger').select('klient_id, oprettet').execute()
        bookinger = book_res.data or []

        # Klienter
        klient_res = db.table('klienter').select('id, navn').execute()
        klienter = {k['id']: k['navn'] for k in (klient_res.data or [])}

        # Aggreger per klient
        from collections import defaultdict
        lead_count = defaultdict(int)
        book_count = defaultdict(int)

        for l in leads:
            lead_count[l['klient_id']] += 1
        for b in bookinger:
            book_count[b['klient_id']] += 1

        result = []
        for kid, navn in klienter.items():
            result.append({
                'klient_id': kid,
                'navn': navn,
                'leads': lead_count.get(kid, 0),
                'bookinger': book_count.get(kid, 0)
            })

        # Totaler
        return jsonify({
            'klienter': result,
            'total_leads': len(leads),
            'total_bookinger': len(bookinger),
            'total_klienter': len(klienter)
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/health', methods=['GET'])
def health():
    klienter = load_klienter()
    return jsonify({
        'status': 'ok',
        'klienter': list(klienter.keys()),
        'mail': bool(SENDGRID_API_KEY),
        'database': bool(db)
    })

@app.route('/klienter', methods=['GET'])
def hent_klienter():
    """Henter alle klienter fra Supabase"""
    if not db:
        return jsonify([])
    try:
        res = db.table('klienter').select('*').order('navn').execute()
        return jsonify(res.data or [])
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/klient-aktiv', methods=['POST'])
def opdater_klient_aktiv():
    """Aktiverer eller deaktiverer en klient"""
    if not db:
        return jsonify({'error': 'Database ikke tilgængelig'}), 500
    data = request.json
    klient_id = data.get('id')
    aktiv = data.get('aktiv', True)
    try:
        db.table('klienter').update({'aktiv': aktiv}).eq('id', klient_id).execute()
        return jsonify({'success': True, 'aktiv': aktiv})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/klient', methods=['POST'])
def opret_klient():
    """Opretter eller opdaterer en klient i Supabase"""
    if not db:
        return jsonify({'error': 'Database ikke tilgængelig'}), 500
    data = request.json
    try:
        # Upsert klient
        klient_data = {
            'id': data.get('id'),
            'navn': data.get('navn', ''),
            'email': data.get('email', ''),
            'kontakt': data.get('kontakt', ''),
            'telefon': data.get('telefon', ''),
            'hjemmeside': data.get('hjemmeside', ''),
            'beskrivelse': data.get('beskrivelse', ''),
            'startpris': int(data.get('startpris', 0) or 0),
            'mdpris': int(data.get('mdpris', 0) or 0),
            'status': data.get('status', 'opsætning'),
            'produkter': data.get('produkter', []),
            'password': data.get('password', '') or ''
        }
        res = db.table('klienter').upsert(klient_data).execute()
        return jsonify({'success': True, 'klient': res.data[0] if res.data else {}})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/chatbot-config', methods=['POST'])
def gem_chatbot_config():
    """Gemmer chatbot konfiguration for en klient"""
    if not db:
        return jsonify({'error': 'Database ikke tilgængelig'}), 500
    data = request.json
    klient_id = data.get('klient_id')
    if not klient_id:
        return jsonify({'error': 'klient_id mangler'}), 400
    try:
        cfg = {
            'klient_id': klient_id,
            'chatbot_navn': data.get('chatbot_navn', 'Alma'),
            'velkomst': data.get('velkomst', 'Hej! Hvordan kan jeg hjælpe?'),
            'farve': data.get('farve', '#0a2463'),
            'aabningsider': data.get('aabning', '') or data.get('åbningstider', ''),
            'kontakt': data.get('kontakt', ''),
            'ydelser': data.get('ydelser', ''),
            'priser': data.get('priser', ''),
            'adresse': data.get('adresse', ''),
            'andet': data.get('andet', ''),
            'ekstra_viden': data.get('ekstra_viden', ''),
            'opdateret': 'now()'
        }
        res = db.table('chatbot_config').upsert(cfg, on_conflict='klient_id').execute()
        return jsonify({'success': True, 'config': res.data[0] if res.data else {}})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/test-mail', methods=['GET'])
def test_mail():
    """Test SendGrid direkte"""
    til = request.args.get('email', '')
    if not til:
        return jsonify({'error': 'Tilføj ?email=din@email.dk'}), 400
    if not SENDGRID_API_KEY or not SENDGRID_FROM:
        return jsonify({'error': 'SENDGRID_API_KEY eller SENDGRID_FROM mangler', 'key_sat': bool(SENDGRID_API_KEY), 'from_sat': bool(SENDGRID_FROM)}), 500
    try:
        message = Mail(
            from_email=(SENDGRID_FROM, 'KlarAI Test'),
            to_emails=til,
            subject='KlarAI test mail',
            plain_text_content='Denne mail bekræfter at KlarAI mail-systemet virker.',
        )
        sg = SendGridAPIClient(SENDGRID_API_KEY)
        response = sg.send(message)
        return jsonify({'sendt': True, 'status': response.status_code, 'fra': SENDGRID_FROM, 'til': til})
    except Exception as e:
        return jsonify({'sendt': False, 'fejl': str(e), 'body': str(getattr(e, 'body', ''))}), 500

@app.route('/chatbot.js', methods=['GET'])
def serve_chatbot_js():
    from flask import send_from_directory
    js_dir = os.path.join(os.path.dirname(__file__), '..')
    return send_from_directory(js_dir, 'chatbot.js', mimetype='application/javascript')

@app.route('/lead-form.js', methods=['GET'])
def serve_lead_form_js():
    from flask import send_from_directory
    js_dir = os.path.join(os.path.dirname(__file__), '..')
    return send_from_directory(js_dir, 'lead-form.js', mimetype='application/javascript')

@app.route('/booking-widget.js', methods=['GET'])
def serve_booking_widget_js():
    from flask import send_from_directory
    js_dir = os.path.join(os.path.dirname(__file__), '..')
    return send_from_directory(js_dir, 'booking-widget.js', mimetype='application/javascript')

@app.route('/login', methods=['POST'])
def login():
    data = request.json
    email = (data.get('email') or '').strip().lower()
    password = data.get('password') or ''

    admin_email = os.environ.get('ADMIN_EMAIL', 'admin@klaiai.dk').lower()
    admin_pw = os.environ.get('ADMIN_PASSWORD', 'klaiai2024')

    # Admin login
    if email == admin_email and password == admin_pw:
        token = secrets.token_hex(32)
        active_tokens[token] = {'role': 'admin'}
        return jsonify({'token': token, 'role': 'admin'})

    # Klient login — tjek Supabase
    if db:
        try:
            res = db.table('klienter').select('id, navn, email, password, aktiv').eq('email', email).single().execute()
            if res.data:
                klient = res.data
                if klient.get('aktiv') == False:
                    return jsonify({'error': 'Adgang er deaktiveret. Kontakt KlarAI.'}), 403
                klient_pw = klient.get('password', '')
                if klient_pw and password == klient_pw:
                    token = secrets.token_hex(32)
                    active_tokens[token] = {'role': 'client', 'klient_id': klient['id']}
                    return jsonify({'token': token, 'role': 'client', 'klient_id': klient['id']})
        except:
            pass

    return jsonify({'error': 'Forkert email eller adgangskode'}), 401

@app.route('/', methods=['GET'])
def index():
    from flask import send_from_directory
    app_dir = os.path.join(os.path.dirname(__file__), '..', 'app')
    return send_from_directory(app_dir, 'index.html')

@app.route('/login', methods=['GET'])
def login_page():
    from flask import send_from_directory
    app_dir = os.path.join(os.path.dirname(__file__), '..', 'app')
    return send_from_directory(app_dir, 'login.html')

@app.route('/portal/<klient_id>', methods=['GET'])
def klient_portal(klient_id):
    from flask import send_from_directory, make_response
    app_dir = os.path.join(os.path.dirname(__file__), '..', 'app')
    with open(os.path.join(app_dir, 'client.html'), 'r', encoding='utf-8') as f:
        html = f.read()
    html = html.replace("params.get('id') || ''", f"'{klient_id}'")
    response = make_response(html)
    response.headers['Content-Type'] = 'text/html; charset=utf-8'
    return response


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    print(f"🤖 KlarAI Agent Server kører på port {port}")
    app.run(host='0.0.0.0', port=port, debug=False)

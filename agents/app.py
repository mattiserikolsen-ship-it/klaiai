#!/usr/bin/env python3
"""
NexOlsen Agent Server
Samlet Flask-app med Chatbot Agent + Lead Agent
Klar til deployment på Render / Railway
"""

from flask import Flask, request, jsonify, Response
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import anthropic
import json
import os
import secrets
import bcrypt
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail
from supabase import create_client
import functools
import stripe
import requests as http_requests
from bs4 import BeautifulSoup
import threading
import io
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
try:
    import pdfplumber
    HAR_PDFPLUMBER = True
except ImportError:
    HAR_PDFPLUMBER = False

# ── STRIPE ────────────────────────────────────────────
STRIPE_SECRET_KEY     = os.environ.get('STRIPE_SECRET_KEY', '')
STRIPE_WEBHOOK_SECRET = os.environ.get('STRIPE_WEBHOOK_SECRET', '')
if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

STRIPE_PRISER = {
    'starter': {'price_id': os.environ.get('STRIPE_PRICE_STARTER', ''), 'navn': 'Starter', 'pris': 799,  'produkter': ['chatbot', 'lead']},
    'pro':     {'price_id': os.environ.get('STRIPE_PRICE_PRO', ''),     'navn': 'Pro',     'pris': 1499, 'produkter': ['chatbot', 'lead', 'booking', 'rapport']},
    'vaekst':  {'price_id': os.environ.get('STRIPE_PRICE_VAEKST', ''),  'navn': 'Vækst',  'pris': 2499, 'produkter': ['chatbot', 'lead', 'booking', 'rapport', 'mail']},
}

# ── TOKEN STORE (Supabase-persistent) ─────────────────
import time as _time
TOKEN_EXPIRY = 30 * 24 * 3600  # 30 dage

# In-memory cache for hurtig opslag (fyldes fra Supabase)
active_tokens = {}   # token -> {'role': 'admin'/'client', 'klient_id': ..., 'created_at': float}

def _gem_token(token, info):
    """Gem token i RAM + Supabase"""
    active_tokens[token] = info
    if db:
        try:
            db.table('admin_sessions').upsert({
                'token': token,
                'role': info.get('role', ''),
                'klient_id': info.get('klient_id'),
                'created_at': _time.time(),
                'expires_at': _time.time() + TOKEN_EXPIRY
            }).execute()
        except:
            pass

def _token_ok(token, role=None):
    """Returnerer True hvis token er gyldigt (tjekker RAM + Supabase)"""
    if not token:
        return False
    # Tjek RAM-cache først
    info = active_tokens.get(token)
    if info:
        if _time.time() - info.get('created_at', 0) > TOKEN_EXPIRY:
            active_tokens.pop(token, None)
            return False
        if role and info.get('role') != role:
            return False
        return True
    # Ikke i RAM — hent fra Supabase
    if db:
        try:
            res = db.table('admin_sessions').select('*').eq('token', token).single().execute()
            if res.data:
                sess = res.data
                if sess.get('expires_at', 0) < _time.time():
                    return False
                # Læg i RAM-cache
                active_tokens[token] = {
                    'role': sess.get('role', ''),
                    'klient_id': sess.get('klient_id'),
                    'created_at': sess.get('created_at', _time.time())
                }
                if role and sess.get('role') != role:
                    return False
                return True
        except:
            pass
    return False

def _ryd_tokens():
    """Fjern udløbne tokens"""
    nu = _time.time()
    udlob = [t for t, info in list(active_tokens.items()) if nu - info.get('created_at', 0) > TOKEN_EXPIRY]
    for t in udlob:
        active_tokens.pop(t, None)
    if db:
        try:
            db.table('admin_sessions').delete().lt('expires_at', nu).execute()
        except:
            pass
demo_sessions = {}   # demo_id -> {'klient_config': {...}, 'url': '...', 'created_at': ...}
prospekter    = {}   # prospekt_id -> {'url', 'navn', 'beskrivelse', 'har_chatbot', 'email_udkast', 'status'}

app = Flask(__name__, static_folder='../app', static_url_path='/app')
CORS(app)

# ── RATE LIMITING ──────────────────────────────────────
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],
    storage_uri='memory://'
)

ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'klaiai2024')
# Sæt ADMIN_LOCAL_ONLY=true på Render for at blokere admin-adgang fra internettet
ADMIN_LOCAL_ONLY = os.environ.get('ADMIN_LOCAL_ONLY', 'false').lower() == 'true'

def _er_localhost():
    return request.remote_addr in ('127.0.0.1', '::1', 'localhost')

@app.before_request
def bloker_admin_fra_net():
    """Blokerer /app/admin.html og /app/login.html fra ikke-localhost når ADMIN_LOCAL_ONLY=true"""
    if ADMIN_LOCAL_ONLY and not _er_localhost():
        path = request.path.lower()
        if path in ('/app/admin.html', '/app/login.html'):
            return Response(
                '<h1>403 – Admin er kun tilgængeligt lokalt</h1>'
                '<p>Tilgå admin-panelet via din lokale Mac i stedet.</p>',
                403, {'Content-Type': 'text/html'}
            )

def require_auth(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or auth.password != ADMIN_PASSWORD:
            return Response(
                'Adgang kræver login.',
                401,
                {'WWW-Authenticate': 'Basic realm="NexOlsen Admin"'}
            )
        return f(*args, **kwargs)
    return decorated

def require_admin(f):
    """Kræver gyldigt admin Bearer token — bruges på alle følsomme admin-endpoints"""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        raw = request.headers.get('Authorization', '')
        token = raw.replace('Bearer ', '').strip()
        if not _token_ok(token, role='admin'):
            return jsonify({'error': 'Adgang krævet — log ind igen'}), 401
        return f(*args, **kwargs)
    return decorated

def require_token(f):
    """Kræver gyldigt token (admin eller klient)"""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        raw = request.headers.get('Authorization', '')
        token = raw.replace('Bearer ', '').strip()
        if not _token_ok(token):
            return jsonify({'error': 'Adgang krævet — log ind igen'}), 401
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
ADMIN_EMAIL = os.environ.get('ADMIN_EMAIL', '')


SUPABASE_URL = os.environ.get('SUPABASE_URL', '')
SUPABASE_KEY = os.environ.get('SUPABASE_KEY', '')
SERVER_URL = os.environ.get('SERVER_URL', 'https://klaiai.onrender.com')
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
            cfg_res = db.table('chatbot_config').select('*').eq('klient_id', klient_id).single().execute()
            if cfg_res.data:
                cfg = cfg_res.data
                # Hent klient navn separat
                klient_navn = ''
                try:
                    k_res = db.table('klienter').select('navn').eq('id', klient_id).single().execute()
                    if k_res.data:
                        klient_navn = k_res.data.get('navn', '')
                except:
                    pass
                # Hent hjemmeside separat
                klient_hjemmeside = ''
                try:
                    hj_res = db.table('klienter').select('hjemmeside').eq('id', klient_id).single().execute()
                    if hj_res.data:
                        klient_hjemmeside = hj_res.data.get('hjemmeside', '')
                except:
                    pass
                return {
                    'navn': klient_navn,
                    'chatbot_navn': cfg.get('chatbot_navn', 'Alma'),
                    'velkomst': cfg.get('velkomst', 'Hej! Hvordan kan jeg hjælpe?'),
                    'farve': cfg.get('farve', '#0a2463'),
                    'hjemmeside': klient_hjemmeside,
                    'info': {
                        'åbningstider': cfg.get('aabningsider', ''),
                        'kontakt': cfg.get('kontakt', ''),
                        'ydelser': cfg.get('ydelser', ''),
                        'priser': cfg.get('priser', ''),
                        'adresse': cfg.get('adresse', ''),
                        'andet': cfg.get('andet', '')
                    },
                    'ekstra_viden': cfg.get('ekstra_viden', ''),
                    'mail_image_url': cfg.get('mail_image_url', '') or None,
                    'auto_godkend_mails': cfg.get('auto_godkend_mails', False)
                }
        except Exception as e:
            print(f"get_klient fejl: {e}")
    # Fallback til JSON-fil
    klienter = load_klienter()
    return klienter.get(klient_id, klienter.get('demo', {}))

LEAD_TOOL = [
    {
        "name": "gem_lead",
        "description": "Gem kundens kontaktoplysninger som et lead. Kald denne funktion så snart kunden har givet navn + telefon ELLER navn + email. Vent ikke på at få begge dele — gem straks med det du har.",
        "input_schema": {
            "type": "object",
            "properties": {
                "navn": {"type": "string", "description": "Kundens fulde navn"},
                "telefon": {"type": "string", "description": "Kundens telefonnummer"},
                "email": {"type": "string", "description": "Kundens email-adresse"},
                "besked": {"type": "string", "description": "Hvad er kunden interesseret i — opsummer behovet kort og præcist"}
            },
            "required": ["navn", "besked"]
        }
    }
]

def byg_chatbot_prompt(klient):
    info = klient.get('info', {})
    info_tekst = '\n'.join([f"{k.capitalize()}: {v}" for k, v in info.items() if v])
    ekstra = klient.get('ekstra_viden', '').strip()
    if len(ekstra) > 150000:
        ekstra = ekstra[:150000] + '\n\n[... resten er afkortet pga. længde]'
    ekstra_sektion = f"\n\nEkstra viden om produkter, priser og kategorier:\n{ekstra}" if ekstra else ""
    navn = klient.get('chatbot_navn', 'Alma')
    virksomhed = klient.get('navn', 'virksomheden')
    return f"""Du hedder {navn} og er salgsassistent hos {virksomhed}. Du er ikke en robot — du er et rigtigt menneske der elsker at hjælpe folk med at finde den rigtige løsning. Du skriver som en venlig, klog kollega — ikke som en corporate chatbot.

── VIRKSOMHEDSINFO ──
{info_tekst}{ekstra_sektion}

── DIN PERSONLIGHED ──
- Varm, nysgerrig og engageret — du er genuint interesseret i hvad kunden har brug for
- Du bruger naturligt dansk — ikke formelt, ikke slang. Som en god ekspedient i en butik.
- Du må godt bruge "Fedt!", "Godt valg!", "Det lyder perfekt til det" — det føles menneskeligt
- Du stiller ét spørgsmål ad gangen og lytter til svaret
- Du er aldrig sælger-agtig eller presserende — du hjælper, det sælger sig selv

── SAMTALESTRATEGI ──
Trin 1 — FORSTÅ: Stil ét konkret spørgsmål for at forstå kundens situation.
  Fx: "Whereabouts vil du have den stående — have eller terrasse?" / "Hvad er I — familie med børn eller mest voksne?"

Trin 2 — ANBEFAL: Kom med én konkret anbefaling med en kort menneskelig begrundelse.
  Fx: "Til det der lyder **[model]** som det oplagte valg — den er populær fordi den [fordel der matcher deres svar]."

Trin 3 — BYGG INTERESSE: Nævn 1-2 ting der gør produktet særligt for netop dem.
  Brug produktlinks fra ekstra viden hvis de findes: [Se den her](URL)

Trin 4 — OPSAML LEAD naturligt og uforpligtende:
  Fx: "Vil du have en mail med lidt mere info og priser? Så sender jeg det direkte til dig — hvad er dit navn og email?"
  Eller: "Skal jeg sende dig vores størrelsesguide? Kræver bare din email 😊"

── LEAD-OPSAMLING ──
- Forsøg at få navn + email i HVER samtale hvor kunden viser interesse
- Gør det let og uforpligtende — aldrig presset
- Så snart du har navn + email ELLER navn + telefon → kald gem_lead med det samme
- Bekræft: "Perfekt, [navn]! Du hører fra os snart 🙌"

── REGLER ──
- Skriv ALTID på dansk
- Max 2-3 sætninger per svar — kor og kontant, ikke lange vægge af tekst
- Brug **fed** til produktnavne og priser
- Gæt aldrig på priser eller specifikationer — brug kun info fra ekstra viden
- Stil aldrig mere end ét spørgsmål ad gangen
- Hvis du ikke ved svaret → vær ærlig og opsaml lead: "Det ved jeg faktisk ikke med sikkerhed — men jeg kan få nogen til at kontakte dig med det præcise svar. Hvad er dit navn og email?"
- Undgå bullets og lange lister — tal som et menneske"""


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
    klient_navn = klient_info.get('navn', 'Virksomheden')
    kontakt = klient_info.get('info', {}).get('kontakt', '')
    notif_mail = kontakt.split('|')[-1].strip() if '|' in kontakt else kontakt.strip()

    lead_navn = lead_data.get('navn', 'Ukendt')
    lead_tlf = lead_data.get('telefon', '')
    lead_email = lead_data.get('email', '')
    lead_besked = lead_data.get('besked', '')

    emne_klient = f"Nyt lead via chatbot — {lead_navn}"
    tekst_klient = f"""Nyt lead opsamlet via chatbotten!

Navn: {lead_navn}
Telefon: {lead_tlf}
Email: {lead_email}
Interesse: {lead_besked}

Log ind på din NexOlsen portal for at se alle leads."""

    # Send notifikation til klient
    if SENDGRID_API_KEY and notif_mail and '@' in notif_mail:
        sendt = send_mail(notif_mail, emne_klient, tekst_klient, klient_navn)
        if sendt:
            _log_agent('lead_notif', klient_id, lead_navn, f"Lead notifikation sendt til klient: {lead_navn} ({lead_tlf})")
    else:
        _log_agent('lead_notif', klient_id, lead_navn, f"Nyt lead opsamlet: {lead_navn} — {lead_besked[:60]}")

    # Send notifikation til admin (NexOlsen)
    if SENDGRID_API_KEY and ADMIN_EMAIL and '@' in ADMIN_EMAIL:
        emne_admin = f"[NexOlsen] Nyt lead hos {klient_navn} — {lead_navn}"
        tekst_admin = f"""Nyt lead opsamlet via chatbot!

Klient: {klient_navn} ({klient_id})
Lead: {lead_navn}
Telefon: {lead_tlf}
Email: {lead_email}
Interesse: {lead_besked}

Log ind på admin-panelet for at se detaljer:
https://klaiai.onrender.com/app/admin.html"""
        send_mail(ADMIN_EMAIL, emne_admin, tekst_admin, 'NexOlsen')

    # Send automatisk bekræftelses-email til leaden
    if SENDGRID_API_KEY and lead_email and '@' in lead_email:
        try:
            fornavn = lead_navn.split()[0] if lead_navn and lead_navn != 'Ukendt' else 'der'
            emne_lead = f"Tak for din henvendelse til {klient_navn} 👋"
            html_lead = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"/></head>
<body style="margin:0;padding:0;background:#f8f7f4;font-family:'Helvetica Neue',Arial,sans-serif">
<table width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:32px 16px">
<table width="560" cellpadding="0" cellspacing="0" style="max-width:560px">
  <tr><td style="background:#0a2463;border-radius:14px 14px 0 0;padding:24px 32px">
    <div style="color:#fff;font-size:20px;font-weight:800">{klient_navn}</div>
  </td></tr>
  <tr><td style="background:#fff;padding:28px 32px;border-left:1px solid #e5e3de;border-right:1px solid #e5e3de">
    <div style="font-size:18px;font-weight:700;color:#1a1918;margin-bottom:12px">Hej {fornavn}! 👋</div>
    <div style="font-size:14px;color:#555;line-height:1.7;margin-bottom:16px">
      Tak for din henvendelse. Vi har modtaget din besked og vender tilbage til dig hurtigst muligt.
    </div>
    <div style="background:#f0f4ff;border-left:3px solid #0a2463;border-radius:0 10px 10px 0;padding:14px 18px;margin-bottom:16px">
      <div style="font-size:12px;font-weight:700;color:#0a2463;text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px">Din besked</div>
      <div style="font-size:13px;color:#333;line-height:1.6">{lead_besked or '(Ingen besked)'}</div>
    </div>
    <div style="font-size:13px;color:#888">Med venlig hilsen,<br><strong>{klient_navn}</strong></div>
  </td></tr>
  <tr><td style="background:#f8f7f4;padding:16px 32px;border:1px solid #e5e3de;border-radius:0 0 14px 14px;text-align:center">
    <div style="font-size:11px;color:#bbb">Drevet af NexOlsen</div>
  </td></tr>
</table>
</td></tr></table>
</body></html>"""
            from sendgrid.helpers.mail import Mail as SGMail
            msg = SGMail(
                from_email=(SENDGRID_FROM, klient_navn),
                to_emails=lead_email,
                subject=emne_lead,
                html_content=html_lead
            )
            sg = SendGridAPIClient(SENDGRID_API_KEY)
            sg.send(msg)
            _log_agent('lead_bekræftelse', klient_id, lead_navn, f"Bekræftelses-email sendt til lead: {lead_email}")
        except Exception as e:
            print(f"Lead bekræftelses-email fejl: {e}")


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
@limiter.limit("30 per minute; 200 per hour")
def chat():
    data = request.json
    klient_id = data.get('client', 'demo')
    demo_id   = data.get('demo_id')
    besked = data.get('message', '')
    historik = data.get('history', [])

    if not besked:
        return jsonify({'error': 'Ingen besked'}), 400

    # Demo-session: brug genereret config
    if demo_id and demo_id in demo_sessions:
        klient = demo_sessions[demo_id]['klient_config']
    else:
        if klient_id != 'demo' and not er_klient_aktiv(klient_id):
            return jsonify({'svar': 'Denne chatbot er ikke tilgængelig i øjeblikket.'}), 403
        klient = get_klient(klient_id)
    messages = [{'role': m['role'], 'content': m['content']} for m in historik[-10:]]
    messages.append({'role': 'user', 'content': besked})

    try:
        response = ai.messages.create(
            model='claude-sonnet-4-6',
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
                model='claude-sonnet-4-6',
                max_tokens=200,
                system=byg_chatbot_prompt(klient),
                tools=LEAD_TOOL,
                messages=messages
            )
            for block in followup.content:
                if block.type == 'text':
                    reply += block.text

        reply_final = reply.strip()

        # Tilføj booking URL hvis spørgsmålet handler om møde/booking og klienten har en booking_url
        booking_ord = ['møde', 'book', 'tid', 'konsultation', 'aftale']
        if any(ord in besked.lower() for ord in booking_ord) and db and klient_id != 'demo':
            try:
                k_res = db.table('klienter').select('booking_url').eq('id', klient_id).single().execute()
                booking_url = k_res.data.get('booking_url', '') if k_res.data else ''
                if booking_url:
                    reply_final += f'\n\n📅 Book et møde direkte her: {booking_url}'
            except:
                pass

        # Log chat session ved første besked i konversationen
        if not historik and klient_id != 'demo' and db:
            try:
                db.table('chat_sessions').insert({'klient_id': klient_id}).execute()
            except:
                pass

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
    result = {
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
        },
        'mail_image_url': klient.get('mail_image_url', ''),
        'auto_godkend_mails': klient.get('auto_godkend_mails', False)
    }
    return jsonify(result)


# ── LEAD ENDPOINTS ─────────────────────────────────────

@app.route('/lead', methods=['POST'])
@limiter.limit("5 per minute; 20 per hour")
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

    # Tjek om klient har auto-godkend slået til + hent hero-billede URL
    auto_godkend = False
    mail_image_url = None
    klient_hjemmeside = klient.get('hjemmeside', '')
    lead_db_id = None
    if db:
        try:
            cfg = db.table('chatbot_config').select('auto_godkend_mails,mail_image_url').eq('klient_id', klient_id).execute()
            if cfg.data:
                auto_godkend = cfg.data[0].get('auto_godkend_mails', False)
                mail_image_url = cfg.data[0].get('mail_image_url') or None
            # Hent lead id
            lead_res = db.table('leads').select('id').eq('klient_id', klient_id).eq('navn', lead.get('navn','')).order('created_at', desc=True).limit(1).execute()
            if lead_res.data:
                lead_db_id = str(lead_res.data[0]['id'])
        except:
            pass

    if auto_godkend and lead.get('email') and SENDGRID_API_KEY:
        # Send straks med professionel HTML-mail
        for mail in mails:
            html = byg_html_mail(
                lead_navn=lead.get('navn', ''),
                tekst=mail['tekst'],
                klient_navn=klient_info['navn'],
                klient_hjemmeside=klient_hjemmeside,
                hero_image_url=mail_image_url,
                cta_tekst='Besøg vores hjemmeside',
                cta_url=klient_hjemmeside or None
            )
            sendt = send_mail(lead['email'], mail['emne'], mail['tekst'], klient_info['navn'], html_content=html)
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
        1: 'Dette er den første kontakt. Vær varm og imødekommende. Tak for henvendelsen og beskriv kort hvad virksomheden tilbyder. Afslut med at de er velkomne til at skrive på virksomhedens email eller besøge hjemmesiden hvis de har spørgsmål.',
        2: 'Opfølgning dag 3. Spørg venligt om de har haft mulighed for at kigge nærmere. Fremhæv én konkret fordel ved produktet/ydelsen. Afslut med at de kan skrive eller gå ind på hjemmesiden.',
        3: 'Sidste opfølgning. Gør det personligt og varmt. Afslut positivt — sig at de altid er velkomne til at skrive eller besøge hjemmesiden hvis de på et tidspunkt får lyst.'
    }.get(mail_nr, '')

    response = ai.messages.create(
        model='claude-sonnet-4-6',
        max_tokens=600,
        messages=[{'role': 'user', 'content': f"""Du er en professionel dansk salgsekspert. Skriv opfølgningsmail #{mail_nr} til dette lead.

Lead-navn: {lead.get('navn','der')}
Henvendelse: {lead.get('besked','Generel forespørgsel')}
Virksomhed: {klient['navn']}
Ydelser: {klient.get('ydelser','')}
Kontakt email: {klient.get('kontakt','')}
Hjemmeside: {klient.get('hjemmeside','')}

Instrukser: {instruktion}

Krav til mailen:
- Personlig tiltale med leaddets fornavn
- Professionel men venlig tone
- ALDRIG opfordr til at booke møde, ringe eller tage en snak — kun til at skrive på email eller besøge hjemmesiden
- 3-5 linjer brødtekst
- Ingen emojis
- Dansk

Returner KUN dette format:
EMNE: <emnelinjen>
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


def byg_html_mail(lead_navn, tekst, klient_navn, klient_hjemmeside, hero_image_url=None, cta_tekst='Se vores produkter', cta_url=None):
    """Bygger en professionel HTML-mail med valgfrit hero-billede."""
    paragraphs = ''.join(f'<p style="margin:0 0 14px 0;line-height:1.7;color:#374151">{l}</p>' for l in tekst.split('\n') if l.strip())
    hero_html = ''
    if hero_image_url:
        hero_html = f'<img src="{hero_image_url}" alt="{klient_navn}" style="width:100%;max-height:280px;object-fit:cover;display:block;border-radius:8px 8px 0 0"/>'
    cta_url = cta_url or klient_hjemmeside or '#'
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/></head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:'Helvetica Neue',Arial,sans-serif">
<table width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:32px 16px">
<table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,0.08)">

  <!-- HERO BILLEDE -->
  {f'<tr><td>{hero_html}</td></tr>' if hero_html else ''}

  <!-- HEADER -->
  <tr><td style="background:#0a1a3a;padding:24px 36px">
    <div style="color:#ffffff;font-size:20px;font-weight:700;letter-spacing:-0.3px">{klient_navn}</div>
  </td></tr>

  <!-- INDHOLD -->
  <tr><td style="padding:32px 36px">
    <div style="font-size:22px;font-weight:700;color:#111827;margin-bottom:20px;letter-spacing:-0.3px">Hej {lead_navn.split()[0] if lead_navn else 'der'},</div>
    {paragraphs}
  </td></tr>

  <!-- CTA KNAP -->
  <tr><td style="padding:0 36px 32px;text-align:center">
    <a href="{cta_url}" style="display:inline-block;background:#0a1a3a;color:#ffffff;text-decoration:none;font-size:15px;font-weight:700;padding:14px 32px;border-radius:8px;letter-spacing:0.2px">{cta_tekst} →</a>
  </td></tr>

  <!-- FOOTER -->
  <tr><td style="background:#f9fafb;padding:20px 36px;border-top:1px solid #e5e7eb;text-align:center">
    <div style="font-size:12px;color:#9ca3af">Du modtager denne mail fordi du har henvendt dig til {klient_navn}.</div>
  </td></tr>

</table>
</td></tr></table>
</body></html>"""


def send_mail(til, emne, tekst, fra_navn, html_content=None):
    if not SENDGRID_API_KEY or not SENDGRID_FROM:
        return False
    try:
        if not html_content:
            html = '<br>'.join(tekst.split('\n'))
            html_content = f'<div style="font-family:Arial,sans-serif;max-width:600px;padding:20px">{html}</div>'
        message = Mail(
            from_email=(SENDGRID_FROM, fra_navn),
            to_emails=til,
            subject=emne,
            plain_text_content=tekst,
            html_content=html_content
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

@app.route('/booking-link/<klient_id>', methods=['GET'])
def get_booking_link(klient_id):
    """Returnerer klientens booking URL"""
    if not db:
        return jsonify({'booking_url': ''}), 200
    try:
        res = db.table('klienter').select('booking_url').eq('id', klient_id).single().execute()
        booking_url = res.data.get('booking_url', '') if res.data else ''
        return jsonify({'booking_url': booking_url or ''})
    except Exception as e:
        return jsonify({'booking_url': '', 'error': str(e)}), 200


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
        res = db.table('leads').select('*').eq('klient_id', klient_id).order('created_at', desc=True).execute()
        return jsonify({'leads': res.data or []})
    except Exception as e:
        return jsonify({'leads': [], 'error': str(e)})


@app.route('/bookinger/<klient_id>', methods=['GET'])
def get_bookinger(klient_id):
    """Henter bookinger for en klient"""
    if not db:
        return jsonify({'bookinger': []})
    try:
        res = db.table('bookinger').select('*').eq('klient_id', klient_id).order('created_at', desc=True).execute()
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
        res = db.table('lead_mails').select('*').eq('klient_id', klient_id).eq('status', 'afventer').order('created_at', desc=True).execute()
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
    klient_hjemmeside = klient.get('hjemmeside', '')
    sendt_count = 0

    # Hent hero-billede URL fra chatbot_config
    mail_image_url = None
    if db:
        try:
            cfg = db.table('chatbot_config').select('mail_image_url').eq('klient_id', klient_id).execute()
            if cfg.data:
                mail_image_url = cfg.data[0].get('mail_image_url') or None
        except: pass

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
            html = byg_html_mail(
                lead_navn=lead_navn,
                tekst=tekst,
                klient_navn=klient_navn,
                klient_hjemmeside=klient_hjemmeside,
                hero_image_url=mail_image_url,
                cta_tekst='Besøg vores hjemmeside',
                cta_url=klient_hjemmeside or None
            )
            sendt = send_mail(lead_email, emne, tekst, klient_navn, html_content=html)
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

    analyse_prompt = f"""Du er en skarp NexOlsen-konsulent. Analyser denne klients AI-opsætning og returner præcis 4-5 kritiske, konkrete forbedringer i JSON.

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

        chat_sessions_total = 0
        try:
            cs = db.table('chat_sessions').select('id', count='exact').eq('klient_id', klient_id).execute()
            chat_sessions_total = cs.count or 0
        except:
            pass

        return jsonify({
            'total_leads': len(leads),
            'total_bookinger': len(bookinger),
            'chat_sessions': chat_sessions_total,
            'kilde': {'chatbot': chatbot, 'formular': formular},
            'uger':     {'labels': uge_labels, 'leads': leads_uge, 'bookinger': book_uge},
            'maaneder': {'labels': mdr_labels, 'leads': leads_mdr, 'bookinger': book_mdr},
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def _hent_rapport_data(klient_id):
    """Henter leads, bookinger, gaps og chat-sessions til rapport."""
    leads, bookinger, gaps, chat_count = [], [], [], 0
    if db:
        try:
            leads = db.table('leads').select('*').eq('klient_id', klient_id).order('oprettet', desc=True).execute().data or []
            bookinger = db.table('bookinger').select('*').eq('klient_id', klient_id).eq('status', 'bekræftet').execute().data or []
        except:
            pass
        try:
            gaps = db.table('chatbot_gaps').select('spoergsmaal, oprettet').eq('klient_id', klient_id).eq('status', 'åben').order('oprettet', desc=True).limit(5).execute().data or []
        except:
            pass
        try:
            sessions_res = db.table('chat_sessions').select('id', count='exact').eq('klient_id', klient_id).execute()
            chat_count = sessions_res.count or 0
        except:
            pass
    return leads, bookinger, gaps, chat_count


def _byg_rapport_html(klient_id, klient_navn, leads, bookinger, gaps=None, chat_count=0, maaned=None):
    """Bygger rapport HTML-streng med måned-over-måned og CRM-tragt."""
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)

    # Afgræns til denne måned og sidste måned
    if maaned:
        mdr_start = maaned
    else:
        mdr_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    forrige_mdr_start = (mdr_start - timedelta(days=1)).replace(day=1)

    def i_maaned(l, start, slut):
        ts = l.get('oprettet', '')
        if not ts: return False
        try:
            t = datetime.fromisoformat(ts.replace('Z', '+00:00'))
            return start <= t < slut
        except: return False

    leads_denne = [l for l in leads if i_maaned(l, mdr_start, now)]
    leads_forrige = [l for l in leads if i_maaned(l, forrige_mdr_start, mdr_start)]

    # CRM konvertering
    crm_ny = sum(1 for l in leads_denne if (l.get('status') or 'ny') == 'ny')
    crm_kontaktet = sum(1 for l in leads_denne if l.get('status') == 'kontaktet')
    crm_møde = sum(1 for l in leads_denne if l.get('status') == 'møde')
    crm_lukket = sum(1 for l in leads_denne if l.get('status') == 'lukket')

    total_denne = len(leads_denne)
    total_forrige = len(leads_forrige)
    vækst = total_denne - total_forrige
    vækst_pct = round((vækst / total_forrige * 100)) if total_forrige > 0 else 0
    vækst_farve = '#16a34a' if vækst >= 0 else '#dc2626'
    vækst_tegn = '+' if vækst >= 0 else ''

    chatbot = sum(1 for l in leads if l.get('kilde') == 'chatbot')
    formular = len(leads) - chatbot
    dato_str = now.strftime('%-d. %B %Y')
    mdr_navn = mdr_start.strftime('%B %Y')

    # AI-anbefaling baseret på data
    if crm_lukket > 0 and total_denne > 0:
        konv = round(crm_lukket / total_denne * 100)
        anbefaling = f"Din konverteringsrate er {konv}% denne måned — {crm_lukket} ud af {total_denne} leads er lukket. {'Flot arbejde!' if konv >= 20 else 'Der er potentiale i de ' + str(crm_ny) + ' leads der stadig afventer svar.'}"
    elif crm_ny > 2:
        anbefaling = f"Du har {crm_ny} leads der stadig er markeret som 'Ny' og afventer opfølgning. Kontakt dem hurtigt — leads konverterer 80% bedre inden for 24 timer."
    elif total_denne > total_forrige:
        anbefaling = f"Godt momentum! Leads er steget med {vækst_tegn}{vækst} denne måned. Overvej at følge op på alle åbne leads i CRM-panelet."
    else:
        anbefaling = "Hold chatbotten aktiv og sørg for at følge op på alle nye leads inden for 24 timer for at maksimere konvertering."

    # Konverteringstragt HTML
    tragt_max = max(total_denne, 1)
    def bar(n): return f'<div style="height:6px;background:#0a2463;border-radius:3px;width:{min(100,round(n/tragt_max*100))}%;margin-top:4px"></div>'

    nye_leads_html = ''
    for l in leads_denne[:5]:
        dato = l.get('oprettet', '')[:10] if l.get('oprettet') else '—'
        status = l.get('status') or 'ny'
        status_farver = {'ny':'#dbeafe','kontaktet':'#fef3c7','møde':'#ede9fe','lukket':'#dcfce7'}
        status_tekst = {'ny':'Ny','kontaktet':'Kontaktet','møde':'Møde','lukket':'Lukket ✓'}
        sf = status_farver.get(status,'#f3f4f6')
        st = status_tekst.get(status, status)
        nye_leads_html += f"""
        <tr>
          <td style="padding:10px 0;border-bottom:1px solid #f0efec;font-size:13px;color:#1a1918;font-weight:600">{l.get('navn','—')}</td>
          <td style="padding:10px 0;border-bottom:1px solid #f0efec;font-size:12px;color:#9a9590">{l.get('email') or l.get('telefon','—')}</td>
          <td style="padding:10px 0;border-bottom:1px solid #f0efec"><span style="background:{sf};padding:2px 8px;border-radius:99px;font-size:11px;font-weight:700">{st}</span></td>
          <td style="padding:10px 0;border-bottom:1px solid #f0efec;font-size:11px;color:#9a9590;text-align:right">{dato}</td>
        </tr>"""

    if not nye_leads_html:
        nye_leads_html = '<tr><td colspan="4" style="padding:16px 0;font-size:13px;color:#9a9590;text-align:center">Ingen leads denne måned</td></tr>'

    # Gaps sektion — ubesvarede spørgsmål
    if gaps is None:
        gaps = []
    if gaps:
        gap_rækker = ''.join(
            f'<li style="padding:6px 0;border-bottom:1px solid #fef2f2;font-size:12px;color:#7f1d1d">'
            f'{g.get("spoergsmaal", "")[:120]}</li>'
            for g in gaps
        )
        gaps_sektion = f"""
  <!-- GAPS -->
  <tr><td style="background:#fff;padding:24px 36px;border-left:1px solid #e5e3de;border-right:1px solid #e5e3de">
    <div style="font-size:13px;font-weight:700;color:#1a1918;margin-bottom:12px">Spørgsmål chatbotten ikke kunne besvare</div>
    <div style="background:#fef2f2;border:1px solid #fecaca;border-radius:10px;padding:16px 20px">
      <div style="font-size:11px;color:#7f1d1d;font-weight:700;margin-bottom:10px">
        {len(gaps)} ubesvarede spørgsmål · Tilføj svar i din portal for at forbedre chatbotten
      </div>
      <ul style="margin:0;padding:0 0 0 16px;list-style:disc">
        {gap_rækker}
      </ul>
      <a href="https://klaiai.dk/app/client.html?id={klient_id}" style="display:inline-block;margin-top:14px;font-size:12px;font-weight:700;color:#dc2626;text-decoration:none">
        → Udfyld manglende svar i portalen
      </a>
    </div>
  </td></tr>"""
    else:
        gaps_sektion = """
  <!-- GAPS (ingen) -->
  <tr><td style="background:#fff;padding:20px 36px;border-left:1px solid #e5e3de;border-right:1px solid #e5e3de">
    <div style="background:#f0fdf4;border:1px solid #bbf7d0;border-radius:10px;padding:14px 18px;font-size:12px;color:#166534">
      ✓ Chatbotten bevarede alle spørgsmål denne periode — ingen videnshuller fundet.
    </div>
  </td></tr>"""

    fornavn = klient_navn.split()[0] if klient_navn else 'der'
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"/><title>NexOlsen Rapport — {mdr_navn}</title></head>
<body style="margin:0;padding:0;background:#f8f7f4;font-family:'Helvetica Neue',Arial,sans-serif">
<table width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:32px 16px">
<table width="600" cellpadding="0" cellspacing="0" style="max-width:600px">

  <!-- HEADER -->
  <tr><td style="background:#0a2463;border-radius:14px 14px 0 0;padding:28px 36px">
    <div style="color:#fff;font-size:22px;font-weight:800;letter-spacing:-0.5px">NexOlsen</div>
    <div style="color:rgba(255,255,255,.4);font-size:10px;text-transform:uppercase;letter-spacing:1.5px;margin-top:3px">Månedlig klientrapport · {mdr_navn}</div>
  </td></tr>

  <!-- INTRO -->
  <tr><td style="background:#fff;padding:28px 36px;border-left:1px solid #e5e3de;border-right:1px solid #e5e3de">
    <div style="font-size:18px;font-weight:700;color:#1a1918;margin-bottom:6px">Hej, {fornavn}!</div>
    <div style="font-size:13px;color:#9a9590;line-height:1.7">Her er din månedlige rapport fra NexOlsen for <strong>{mdr_navn}</strong>. Her er hvad dine AI-agenter har lavet for dig.</div>
  </td></tr>

  <!-- TOP STATS -->
  <tr><td style="background:#f8f7f4;padding:20px 36px;border-left:1px solid #e5e3de;border-right:1px solid #e5e3de">
    <table width="100%" cellpadding="0" cellspacing="0"><tr>
      <td width="25%" style="padding:4px">
        <div style="background:#fff;border:1px solid #e5e3de;border-radius:12px;padding:16px;text-align:center">
          <div style="font-size:30px;font-weight:800;color:#7c3aed;letter-spacing:-2px">{chat_count}</div>
          <div style="font-size:10px;color:#9a9590;margin-top:4px;font-weight:500">Chatsamtaler</div>
          <div style="font-size:10px;color:#7c3aed;font-weight:700;margin-top:4px">i alt</div>
        </div>
      </td>
      <td width="25%" style="padding:4px">
        <div style="background:#fff;border:1px solid #e5e3de;border-radius:12px;padding:16px;text-align:center">
          <div style="font-size:30px;font-weight:800;color:#0a2463;letter-spacing:-2px">{total_denne}</div>
          <div style="font-size:10px;color:#9a9590;margin-top:4px;font-weight:500">Leads</div>
          <div style="font-size:10px;color:{vækst_farve};font-weight:700;margin-top:4px">{vækst_tegn}{vækst} vs. sidst</div>
        </div>
      </td>
      <td width="25%" style="padding:4px">
        <div style="background:#fff;border:1px solid #e5e3de;border-radius:12px;padding:16px;text-align:center">
          <div style="font-size:30px;font-weight:800;color:#16a34a;letter-spacing:-2px">{crm_lukket}</div>
          <div style="font-size:10px;color:#9a9590;margin-top:4px;font-weight:500">Lukkede leads</div>
          <div style="font-size:10px;color:#16a34a;font-weight:700;margin-top:4px">{round(crm_lukket/max(total_denne,1)*100)}% konvertering</div>
        </div>
      </td>
      <td width="25%" style="padding:4px">
        <div style="background:#fff;border:1px solid #e5e3de;border-radius:12px;padding:16px;text-align:center">
          <div style="font-size:30px;font-weight:800;color:#1a1918;letter-spacing:-2px">{len(bookinger)}</div>
          <div style="font-size:10px;color:#9a9590;margin-top:4px;font-weight:500">Bookinger</div>
          <div style="font-size:10px;color:#9a9590;font-weight:500;margin-top:4px">{chatbot} via chatbot</div>
        </div>
      </td>
    </tr></table>
  </td></tr>

  <!-- KONVERTERINGSTRAGT -->
  <tr><td style="background:#fff;padding:24px 36px;border-left:1px solid #e5e3de;border-right:1px solid #e5e3de">
    <div style="font-size:13px;font-weight:700;color:#1a1918;margin-bottom:16px">Konverteringstragt — {mdr_navn}</div>
    <table width="100%" cellpadding="0" cellspacing="0">
      <tr>
        <td style="padding:6px 0"><div style="font-size:12px;color:#9a9590;font-weight:600">Nye leads</div>{bar(total_denne)}<div style="font-size:11px;color:#0a2463;font-weight:800;margin-top:2px">{total_denne}</div></td>
      </tr>
      <tr>
        <td style="padding:6px 0"><div style="font-size:12px;color:#9a9590;font-weight:600">Kontaktet</div>{bar(crm_kontaktet)}<div style="font-size:11px;color:#0a2463;font-weight:800;margin-top:2px">{crm_kontaktet}</div></td>
      </tr>
      <tr>
        <td style="padding:6px 0"><div style="font-size:12px;color:#9a9590;font-weight:600">Møde aftalt</div>{bar(crm_møde)}<div style="font-size:11px;color:#0a2463;font-weight:800;margin-top:2px">{crm_møde}</div></td>
      </tr>
      <tr>
        <td style="padding:6px 0"><div style="font-size:12px;color:#9a9590;font-weight:600">Lukket ✓</div>{bar(crm_lukket)}<div style="font-size:11px;color:#16a34a;font-weight:800;margin-top:2px">{crm_lukket}</div></td>
      </tr>
    </table>
  </td></tr>

  <!-- SENESTE LEADS -->
  <tr><td style="background:#f8f7f4;padding:24px 36px;border-left:1px solid #e5e3de;border-right:1px solid #e5e3de">
    <div style="font-size:13px;font-weight:700;color:#1a1918;margin-bottom:16px">Leads denne måned</div>
    <table width="100%" cellpadding="0" cellspacing="0">
      <tr>
        <th style="font-size:10px;color:#9a9590;text-align:left;padding-bottom:8px;font-weight:600;text-transform:uppercase;letter-spacing:.5px">Navn</th>
        <th style="font-size:10px;color:#9a9590;text-align:left;padding-bottom:8px;font-weight:600;text-transform:uppercase;letter-spacing:.5px">Email/Tlf</th>
        <th style="font-size:10px;color:#9a9590;text-align:left;padding-bottom:8px;font-weight:600;text-transform:uppercase;letter-spacing:.5px">Status</th>
        <th style="font-size:10px;color:#9a9590;text-align:right;padding-bottom:8px;font-weight:600;text-transform:uppercase;letter-spacing:.5px">Dato</th>
      </tr>
      {nye_leads_html}
    </table>
  </td></tr>

  {gaps_sektion}

  <!-- AI ANBEFALING -->
  <tr><td style="background:#fff;padding:24px 36px;border-left:1px solid #e5e3de;border-right:1px solid #e5e3de">
    <div style="background:#f0f4ff;border-left:3px solid #0a2463;border-radius:0 10px 10px 0;padding:16px 20px">
      <div style="font-size:11px;font-weight:700;color:#0a2463;text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px">💡 NexOlsen anbefaler</div>
      <div style="font-size:13px;color:#1a1918;line-height:1.7">{anbefaling}</div>
    </div>
  </td></tr>

  <!-- FOOTER -->
  <tr><td style="background:#fff;padding:24px 36px;border:1px solid #e5e3de;border-radius:0 0 14px 14px;text-align:center">
    <a href="https://klaiai.onrender.com/portal/{klient_id}" style="display:inline-block;background:#0a2463;color:#fff;text-decoration:none;font-size:13px;font-weight:700;padding:12px 28px;border-radius:9px">
      Se din portal →
    </a>
    <div style="font-size:11px;color:#c5c2bc;margin-top:16px">Drevet af NexOlsen · Rapport for {mdr_navn}</div>
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
    leads, bookinger, gaps, chat_count = _hent_rapport_data(klient_id)
    html = _byg_rapport_html(klient_id, klient_navn, leads, bookinger, gaps=gaps, chat_count=chat_count)
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

    leads, bookinger, gaps, chat_count = _hent_rapport_data(klient_id)
    html = _byg_rapport_html(klient_id, klient_navn, leads, bookinger, gaps=gaps, chat_count=chat_count)

    from datetime import datetime
    dato_str = datetime.now().strftime('%-d. %B %Y')
    emne = f"Din NexOlsen rapport — {dato_str}"
    if not SENDGRID_API_KEY or not SENDGRID_FROM:
        return jsonify({'success': False, 'error': 'Mail ikke konfigureret'}), 500

    try:
        message = Mail(
            from_email=(SENDGRID_FROM, 'NexOlsen'),
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
@require_admin
def stats():
    """Henter brugsstatistik fra Supabase"""
    if not db:
        return jsonify({'error': 'Database ikke tilgængelig'}), 500
    try:
        from collections import defaultdict
        from datetime import datetime, timezone, timedelta

        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = today_start - timedelta(days=7)

        # Leads (alle) med navn og email for aktivitetsfeed
        leads_res = db.table('leads').select('klient_id, oprettet, navn, email').order('oprettet', desc=True).execute()
        leads = leads_res.data or []

        # Bookinger per klient
        book_res = db.table('bookinger').select('klient_id, oprettet').execute()
        bookinger = book_res.data or []

        # Klienter
        klient_res = db.table('klienter').select('id, navn').execute()
        klienter = {k['id']: k['navn'] for k in (klient_res.data or [])}

        # Aggreger per klient
        lead_count = defaultdict(int)
        leads_today = defaultdict(int)
        leads_week = defaultdict(int)
        book_count = defaultdict(int)
        seneste_lead = {}  # klient_id -> ISO string

        for l in leads:
            lead_count[l['klient_id']] += 1
            oprettet_str = l.get('oprettet') or ''
            if oprettet_str:
                try:
                    ts = datetime.fromisoformat(oprettet_str.replace('Z', '+00:00'))
                    if ts >= today_start:
                        leads_today[l['klient_id']] += 1
                    if ts >= week_start:
                        leads_week[l['klient_id']] += 1
                    if l['klient_id'] not in seneste_lead:
                        seneste_lead[l['klient_id']] = oprettet_str
                except Exception:
                    pass

        for b in bookinger:
            book_count[b['klient_id']] += 1

        result = []
        for kid, navn in klienter.items():
            sl = seneste_lead.get(kid)
            # Health: grøn < 7 dage, gul 7-14 dage, rød > 14 dage eller ingen leads
            if sl:
                try:
                    ts = datetime.fromisoformat(sl.replace('Z', '+00:00'))
                    dage = (now - ts).days
                    health = 'green' if dage < 7 else ('yellow' if dage < 14 else 'red')
                except Exception:
                    health = 'gray'
            else:
                health = 'gray'

            result.append({
                'klient_id': kid,
                'navn': navn,
                'leads': lead_count.get(kid, 0),
                'leads_i_dag': leads_today.get(kid, 0),
                'leads_uge': leads_week.get(kid, 0),
                'bookinger': book_count.get(kid, 0),
                'seneste_lead': sl,
                'health': health,
            })

        # Sorter efter seneste aktivitet
        result.sort(key=lambda x: x['seneste_lead'] or '', reverse=True)

        # Aktivitetsfeed: seneste 20 leads på tværs
        feed = []
        for l in leads[:20]:
            kid = l.get('klient_id')
            feed.append({
                'klient': klienter.get(kid, kid),
                'navn': l.get('navn', 'Ukendt'),
                'email': l.get('email', ''),
                'oprettet': l.get('oprettet', ''),
                'type': 'lead'
            })

        total_leads_i_dag = sum(leads_today.values())
        total_leads_uge = sum(leads_week.values())

        return jsonify({
            'klienter': result,
            'total_leads': len(leads),
            'total_leads_i_dag': total_leads_i_dag,
            'total_leads_uge': total_leads_uge,
            'total_bookinger': len(bookinger),
            'total_klienter': len(klienter),
            'feed': feed,
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
@require_admin
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
@require_admin
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

@app.route('/klient/<klient_id>', methods=['PATCH'])
@require_admin
def opdater_klient_felt(klient_id):
    """Opdaterer specifikke felter på en klient (branding, indstillinger mv.)"""
    if not db:
        return jsonify({'error': 'Database ikke tilgængelig'}), 500
    data = request.get_json() or {}
    # Kun tilladte felter — aldrig id, stripe_*, plan
    tilladte = {'tilbud_stil', 'tilbud_farve', 'navn', 'hjemmeside', 'email', 'telefon', 'kontaktperson'}
    opdater  = {k: v for k, v in data.items() if k in tilladte}
    if not opdater:
        return jsonify({'error': 'Ingen gyldige felter at opdatere'}), 400
    try:
        db.table('klienter').update(opdater).eq('id', klient_id).execute()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/klient', methods=['POST'])
@require_admin
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
            'password': bcrypt.hashpw(data['password'].encode(), bcrypt.gensalt()).decode() if data.get('password') else '',
            'google_place_id': data.get('google_place_id', '') or '',
            'sms_aktiv': bool(data.get('sms_aktiv', False)),
            'booking_url': data.get('booking_url', '') or ''
        }
        res = db.table('klienter').upsert(klient_data).execute()
        return jsonify({'success': True, 'klient': res.data[0] if res.data else {}})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/send-velkomst/<klient_id>', methods=['POST'])
@require_admin
def send_velkomst(klient_id):
    """Sender onboarding-email til klienten med login-credentials og portal-link"""
    if not db:
        return jsonify({'error': 'Database ikke tilgængelig'}), 500
    try:
        res = db.table('klienter').select('*').eq('id', klient_id).single().execute()
        if not res.data:
            return jsonify({'error': 'Klient ikke fundet'}), 404
        k = res.data
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    navn     = k.get('navn', 'kunde')
    email    = k.get('email', '')
    password = k.get('password', '')
    if not email or '@' not in email:
        return jsonify({'error': 'Klient har ingen gyldig email'}), 400

    portal_url = f'https://klaiai.onrender.com/portal/{klient_id}'
    login_url  = 'https://klaiai.onrender.com/login'
    fornavn    = navn.split()[0] if navn else 'der'

    html = f"""
<div style="font-family:'Inter',Arial,sans-serif;max-width:600px;margin:0 auto;color:#1a1918;background:#f8f7f4;padding:2rem 1rem">
  <div style="background:#0a2463;border-radius:14px 14px 0 0;padding:2.5rem 2rem;text-align:center">
    <div style="font-size:1.8rem;font-weight:900;color:#fff;letter-spacing:-1px">NexOlsen</div>
    <div style="color:rgba(255,255,255,.6);font-size:.9rem;margin-top:.4rem">Din AI-portal er klar 🎉</div>
  </div>

  <div style="background:#fff;border-radius:0 0 14px 14px;padding:2rem;border:1px solid #e5e3de;border-top:none">
    <p style="font-size:1rem;font-weight:700;margin-bottom:.5rem">Hej {fornavn}!</p>
    <p style="color:#4a4845;line-height:1.7;margin-bottom:1.5rem">
      Din NexOlsen-portal er nu klar. Her kan du følge med i dine leads, bookinger og din chatbots aktivitet — alt på ét sted.
    </p>

    <!-- LOGIN BOKS -->
    <div style="background:#f0f4f8;border-radius:12px;padding:1.5rem;margin-bottom:1.75rem;border:1px solid #e2e8f0">
      <div style="font-size:.75rem;font-weight:700;text-transform:uppercase;letter-spacing:.8px;color:#9a9590;margin-bottom:1rem">Dine loginoplysninger</div>
      <div style="display:flex;flex-direction:column;gap:.6rem">
        <div>
          <span style="font-size:.78rem;color:#9a9590;display:block;margin-bottom:.15rem">Email</span>
          <span style="font-weight:700;font-size:.95rem">{email}</span>
        </div>
        <div>
          <span style="font-size:.78rem;color:#9a9590;display:block;margin-bottom:.15rem">Adgangskode</span>
          <span style="font-weight:700;font-size:.95rem;font-family:monospace;background:#e5e3de;padding:.2rem .5rem;border-radius:5px">{password if password else '(kontakt NexOlsen)'}</span>
        </div>
      </div>
    </div>

    <!-- CTA KNAP -->
    <div style="text-align:center;margin-bottom:1.75rem">
      <a href="{login_url}" style="display:inline-block;background:#0a2463;color:#fff;text-decoration:none;font-size:.95rem;font-weight:700;padding:1rem 2.5rem;border-radius:10px;letter-spacing:-.2px">
        Log ind på din portal →
      </a>
    </div>

    <!-- HVAD KAN DU GØRe -->
    <div style="border-top:1px solid #e5e3de;padding-top:1.5rem;margin-bottom:1.5rem">
      <div style="font-size:.78rem;font-weight:700;text-transform:uppercase;letter-spacing:.8px;color:#9a9590;margin-bottom:1rem">Hvad kan du se i portalen?</div>
      <div style="display:flex;flex-direction:column;gap:.6rem">
        <div style="display:flex;gap:.75rem;align-items:flex-start">
          <span style="font-size:1rem;width:24px">🎯</span>
          <div><strong>Leads</strong> — se hvem der har henvendt sig via din chatbot, inkl. navn, email og besked</div>
        </div>
        <div style="display:flex;gap:.75rem;align-items:flex-start">
          <span style="font-size:1rem;width:24px">📅</span>
          <div><strong>Bookinger</strong> — oversigt over alle bekræftede bookinger</div>
        </div>
        <div style="display:flex;gap:.75rem;align-items:flex-start">
          <span style="font-size:1rem;width:24px">📊</span>
          <div><strong>Rapport</strong> — ugentlige og månedlige statistikker over din chatbots præstation</div>
        </div>
        <div style="display:flex;gap:.75rem;align-items:flex-start">
          <span style="font-size:1rem;width:24px">⚙️</span>
          <div><strong>Indstillinger</strong> — tilpas din chatbots svar, farve og åbningstider selv</div>
        </div>
      </div>
    </div>

    <!-- INSTALL WIDGET -->
    <div style="background:#eef2ec;border-radius:10px;padding:1.25rem;margin-bottom:1.5rem">
      <div style="font-size:.82rem;font-weight:700;color:#4a6741;margin-bottom:.6rem">📌 Installer din chatbot</div>
      <div style="font-size:.82rem;color:#3d5636;line-height:1.6;margin-bottom:.75rem">
        Indsæt denne ene linje kode på din hjemmeside, lige over <code style="background:#fff;padding:.1rem .3rem;border-radius:3px">&lt;/body&gt;</code>:
      </div>
      <div style="background:#1a1918;border-radius:8px;padding:.85rem;font-family:monospace;font-size:.78rem;color:#a8d8a8;word-break:break-all">
        &lt;script src="https://klaiai.onrender.com/widget.js" data-id="{klient_id}"&gt;&lt;/script&gt;
      </div>
      <div style="font-size:.75rem;color:#4a6741;margin-top:.5rem">Der er en trin-for-trin guide til alle platforme inde i portalen under "Kom i gang".</div>
    </div>

    <p style="color:#9a9590;font-size:.8rem;text-align:center;line-height:1.6">
      Spørgsmål? Skriv til <a href="mailto:support@nexolsen.dk" style="color:#0a2463">support@nexolsen.dk</a><br/>
      NexOlsen · AI-agenter til din virksomhed
    </p>
  </div>
</div>"""

    try:
        send_mail(email, f'Din NexOlsen-portal er klar, {fornavn}! 🎉', html, 'NexOlsen')
        return jsonify({'success': True, 'sendt_til': email})
    except Exception as e:
        return jsonify({'error': f'Email fejlede: {str(e)}'}), 500


@app.route('/chatbot-config', methods=['POST'])
def gem_chatbot_config():
    """Gemmer chatbot konfiguration for en klient"""
    if not db:
        return jsonify({'error': 'Database ikke tilgængelig'}), 500
    data = request.json
    klient_id = data.get('klient_id')

    # Valider token — admin kan redigere alt, klient kun sit eget
    raw = request.headers.get('Authorization', '')
    token = raw.replace('Bearer ', '').strip()
    if not _token_ok(token):
        return jsonify({'error': 'Adgang krævet — log ind igen'}), 401
    token_info = active_tokens.get(token, {})
    if token_info.get('role') == 'client' and token_info.get('klient_id') != klient_id:
        return jsonify({'error': 'Ingen adgang'}), 403

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
            'mail_image_url': data.get('mail_image_url', '') or None,
            'auto_godkend_mails': bool(data.get('auto_godkend_mails', False)),
            'opdateret': 'now()'
        }
        res = db.table('chatbot_config').upsert(cfg, on_conflict='klient_id').execute()
        return jsonify({'success': True, 'config': res.data[0] if res.data else {}})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ══════════════════════════════════════════════════════════════
#  STRIPE ENDPOINTS
# ══════════════════════════════════════════════════════════════

@app.route('/stripe/checkout', methods=['POST'])
def stripe_checkout():
    """Opret Stripe Checkout session for et abonnement"""
    if not STRIPE_SECRET_KEY:
        return jsonify({'error': 'Stripe ikke konfigureret'}), 500
    data = request.json
    klient_id = data.get('klient_id')
    plan = data.get('plan', 'starter')
    success_url = data.get('success_url', 'https://klaiai.dk/onboarding/succes?id=' + (klient_id or ''))
    cancel_url = data.get('cancel_url', 'https://klaiai.dk/onboarding')

    pris = STRIPE_PRISER.get(plan, STRIPE_PRISER['starter'])
    price_id = pris['price_id']
    if not price_id:
        return jsonify({'error': f'Stripe price_id for {plan} mangler i env vars'}), 400

    try:
        # Hent eller opret Stripe customer
        stripe_customer_id = None
        if klient_id and db:
            try:
                kr = db.table('klienter').select('stripe_customer_id, email, navn').eq('id', klient_id).single().execute()
                if kr.data:
                    stripe_customer_id = kr.data.get('stripe_customer_id')
                    if not stripe_customer_id:
                        cust = stripe.Customer.create(
                            email=kr.data.get('email', ''),
                            name=kr.data.get('navn', ''),
                            metadata={'klient_id': klient_id}
                        )
                        stripe_customer_id = cust.id
                        db.table('klienter').update({'stripe_customer_id': stripe_customer_id}).eq('id', klient_id).execute()
            except: pass

        session_params = {
            'mode': 'subscription',
            'line_items': [{'price': price_id, 'quantity': 1}],
            'success_url': success_url,
            'cancel_url': cancel_url,
            'subscription_data': {'trial_period_days': 14},
            'metadata': {'klient_id': klient_id or '', 'plan': plan},
            'client_reference_id': klient_id or '',
        }
        if stripe_customer_id:
            session_params['customer'] = stripe_customer_id

        session = stripe.checkout.Session.create(**session_params)
        return jsonify({'url': session.url, 'session_id': session.id})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/stripe/webhook', methods=['POST'])
def stripe_webhook():
    """Håndter Stripe webhook events"""
    payload = request.get_data()
    sig_header = request.headers.get('Stripe-Signature', '')
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except stripe.error.SignatureVerificationError:
        return jsonify({'error': 'Ugyldig signatur'}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 400

    etype = event['type']
    obj = event['data']['object']

    if etype == 'checkout.session.completed':
        klient_id = obj.get('client_reference_id') or obj.get('metadata', {}).get('klient_id')
        plan = obj.get('metadata', {}).get('plan', 'starter')
        sub_id = obj.get('subscription')
        customer_id = obj.get('customer')
        sub_status = 'trialing' if obj.get('subscription') else 'active'
        if klient_id and db:
            produkter = STRIPE_PRISER.get(plan, STRIPE_PRISER['starter'])['produkter']
            try:
                db.table('klienter').update({
                    'stripe_customer_id': customer_id,
                    'stripe_subscription_id': sub_id,
                    'plan': plan,
                    'subscription_status': sub_status,
                    'aktiv': True,
                    'status': 'aktiv',
                    'produkter': produkter
                }).eq('id', klient_id).execute()
                # Send velkomstmail med opsætningsmanual
                try:
                    kr = db.table('klienter').select('email, navn, platform').eq('id', klient_id).single().execute()
                    if kr.data:
                        _send_opsaetningsmanual(kr.data, klient_id, plan, produkter)
                except Exception as e:
                    print(f"Velkomstmail fejl: {e}")
            except Exception as e:
                print(f"Webhook DB fejl: {e}")

    elif etype == 'invoice.payment_succeeded':
        sub_id = obj.get('subscription')
        if sub_id and db:
            try:
                db.table('klienter').update({'aktiv': True, 'subscription_status': 'active'}).eq('stripe_subscription_id', sub_id).execute()
            except: pass

    elif etype == 'invoice.payment_failed':
        sub_id = obj.get('subscription')
        if sub_id and db:
            try:
                db.table('klienter').update({'subscription_status': 'past_due'}).eq('stripe_subscription_id', sub_id).execute()
                # Send advarsel
                kr = db.table('klienter').select('email, navn').eq('stripe_subscription_id', sub_id).single().execute()
                if kr.data:
                    send_mail(kr.data['email'], 'Betalingsproblem med dit NexOlsen abonnement',
                        f"Hej {kr.data['navn']},\n\nVi kunne ikke trække betaling for dit abonnement. Opdater din betalingsmetode inden 7 dage for at undgå deaktivering.\n\nhttps://klaiai.dk/login\n\nNexOlsen", 'NexOlsen')
            except: pass

    elif etype == 'customer.subscription.deleted':
        sub_id = obj.get('id')
        if sub_id and db:
            try:
                db.table('klienter').update({'aktiv': False, 'subscription_status': 'canceled', 'status': 'inaktiv'}).eq('stripe_subscription_id', sub_id).execute()
            except: pass

    elif etype == 'customer.subscription.updated':
        sub_id = obj.get('id')
        new_status = obj.get('status', '')
        if sub_id and db:
            try:
                db.table('klienter').update({
                    'subscription_status': new_status,
                    'aktiv': new_status in ('active', 'trialing')
                }).eq('stripe_subscription_id', sub_id).execute()
            except: pass

    return jsonify({'received': True})


@app.route('/stripe/portal', methods=['POST'])
def stripe_portal():
    """Opret Stripe Customer Portal session"""
    if not STRIPE_SECRET_KEY:
        return jsonify({'error': 'Stripe ikke konfigureret'}), 500
    data = request.json
    klient_id = data.get('klient_id')
    if not klient_id or not db:
        return jsonify({'error': 'klient_id mangler'}), 400
    try:
        kr = db.table('klienter').select('stripe_customer_id').eq('id', klient_id).single().execute()
        customer_id = kr.data.get('stripe_customer_id') if kr.data else None
        if not customer_id:
            return jsonify({'error': 'Ingen Stripe kunde fundet'}), 404
        portal = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=f'https://klaiai.dk/portal/{klient_id}'
        )
        return jsonify({'url': portal.url})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/stripe/status/<klient_id>', methods=['GET'])
def stripe_status(klient_id):
    """Hent abonnementsstatus for en klient"""
    if not db:
        return jsonify({'plan': 'ingen', 'subscription_status': 'inactive'})
    try:
        kr = db.table('klienter').select('plan, subscription_status, stripe_customer_id').eq('id', klient_id).single().execute()
        if kr.data:
            return jsonify({
                'plan': kr.data.get('plan', 'ingen'),
                'subscription_status': kr.data.get('subscription_status', 'inactive'),
                'har_stripe': bool(kr.data.get('stripe_customer_id'))
            })
        return jsonify({'plan': 'ingen', 'subscription_status': 'inactive'})
    except:
        return jsonify({'plan': 'ingen', 'subscription_status': 'inactive'})


# ── ONBOARDING ────────────────────────────────────────

@app.route('/onboarding/opret', methods=['POST'])
def onboarding_opret():
    """Opret klient + chatbot config fra onboarding-wizard og returner Stripe checkout URL"""
    data = request.json
    plan = data.get('plan', 'starter')

    # Generer klient ID
    klient_id = str(int(__import__('time').time() * 1000))[-13:]

    produkter = STRIPE_PRISER.get(plan, STRIPE_PRISER['starter'])['produkter']

    # Gem klient
    if db:
        try:
            db.table('klienter').insert({
                'id': klient_id,
                'navn': data.get('virksomhed_navn', ''),
                'email': data.get('email', ''),
                'telefon': data.get('telefon', ''),
                'hjemmeside': data.get('hjemmeside', ''),
                'beskrivelse': data.get('beskrivelse', ''),
                'password': data.get('password', ''),
                'branche': data.get('branche', ''),
                'platform': data.get('platform', ''),
                'status': 'afventer_betaling',
                'produkter': produkter,
                'aktiv': False,
                'plan': plan,
                'subscription_status': 'inactive'
            }).execute()
        except Exception as e:
            return jsonify({'error': f'Kunne ikke oprette klient: {e}'}), 500

        try:
            # Byg ekstra_viden fra booking-info
            ekstra = data.get('ekstra_viden', '')
            if data.get('booking_ydelser'):
                ekstra += f"\n\nBooking - hvad kan bookes: {data.get('booking_ydelser')}"
            if data.get('booking_noter'):
                ekstra += f"\nBooking - noter: {data.get('booking_noter')}"
            if data.get('lead_trigger'):
                ekstra += f"\nLead trigger sætning: {data.get('lead_trigger')}"

            db.table('chatbot_config').upsert({
                'klient_id': klient_id,
                'chatbot_navn': data.get('chatbot_navn', 'Alma'),
                'velkomst': data.get('velkomst', 'Hej! Hvordan kan jeg hjælpe dig?'),
                'farve': data.get('farve', '#0a2463'),
                'ydelser': data.get('ydelser', ''),
                'priser': data.get('priser', ''),
                'kontakt': data.get('kontakt', ''),
                'aabningsider': data.get('aabning', ''),
                'andet': data.get('andet', ''),
                'ekstra_viden': ekstra.strip(),
                'lead_email': data.get('lead_email', data.get('email', '')),
                'booking_email': data.get('booking_email', data.get('email', '')),
            }, on_conflict='klient_id').execute()
        except Exception as e:
            print(f"Chatbot config fejl: {e}")

    # Opret Stripe checkout session
    if STRIPE_SECRET_KEY:
        price_id = STRIPE_PRISER.get(plan, STRIPE_PRISER['starter'])['price_id']
        try:
            cust = stripe.Customer.create(
                email=data.get('email', ''),
                name=data.get('virksomhed_navn', ''),
                metadata={'klient_id': klient_id}
            )
            if db:
                db.table('klienter').update({'stripe_customer_id': cust.id}).eq('id', klient_id).execute()

            session = stripe.checkout.Session.create(
                customer=cust.id,
                mode='subscription',
                line_items=[{'price': price_id, 'quantity': 1}],
                success_url=f'https://klaiai.dk/app/onboarding.html?succes=1&id={klient_id}',
                cancel_url='https://klaiai.dk/app/onboarding.html',
                subscription_data={'trial_period_days': 14},
                metadata={'klient_id': klient_id, 'plan': plan},
                client_reference_id=klient_id,
            )
            return jsonify({'klient_id': klient_id, 'checkout_url': session.url})
        except Exception as e:
            return jsonify({'klient_id': klient_id, 'checkout_url': None, 'stripe_fejl': str(e)})

    # Uden Stripe – aktiver direkte (test mode)
    if db:
        db.table('klienter').update({'aktiv': True, 'status': 'aktiv'}).eq('id', klient_id).execute()
    return jsonify({'klient_id': klient_id, 'checkout_url': None})


# ── SCAN JOBS (in-memory) ─────────────────────────────
scan_jobs = {}  # job_id -> {'status': 'running'/'done'/'error', 'data': ..., 'meta': ...}

HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; KlarAI-scanner/1.0)'}

# Nøgleord → info-sider
INFO_SIDER = [
    'ydelser', 'services', 'service', 'priser', 'pris',
    'om-os', 'om-mig', 'about', 'kontakt', 'contact', 'behandlinger',
    'behandling', 'menu', 'hvad-vi-tilbyder', 'arbejde', 'projekter',
    'cases', 'faq', 'team', 'medarbejdere', 'booking', 'book', 'tilbud', 'pakker'
]

# Nøgleord → produkt/shop-sider
SHOP_SIDER = [
    'shop', 'butik', 'webshop', 'varer', 'produkter', 'products',
    'collections', 'kategori', 'categories', 'sortiment', 'udvalg',
    'webbutik', 'bestil', 'køb', 'store'
]


from urllib.parse import urljoin, urlparse, urlencode, parse_qs, urlunparse

UØNSKEDE_EXTENSIONS = {'.pdf','.jpg','.jpeg','.png','.gif','.zip','.docx','.xlsx','.mp4','.svg','.webp'}
UØNSKEDE_PATH_DELE = {'login','logout','konto','account','kurv','cart','checkout','betaling','wishlist','favoritter','sign-in','register','wp-admin','wp-login','sitemap','robots'}


HEADERS_LISTE = [
    # Standard browser
    {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36', 'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8', 'Accept-Language': 'da,en;q=0.9'},
    # Mac Safari
    {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15', 'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'},
    # Googlebot
    {'User-Agent': 'Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)'},
    # Original
    HEADERS,
]


def hent_raa_soup(url, timeout=10):
    """Henter en side — prøver direkte, derefter Google Cache som fallback"""
    # Forsøg 1-4: direkte med forskellige user-agents
    for headers in HEADERS_LISTE:
        try:
            resp = http_requests.get(
                url, timeout=timeout, headers=headers,
                allow_redirects=True, verify=False
            )
            if resp.status_code < 400:
                resp.encoding = resp.apparent_encoding or 'utf-8'
                soup = BeautifulSoup(resp.text, 'html.parser')
                # Tjek at vi fik reel indhold (ikke en bot-blokeringsside)
                tekst = soup.get_text()
                if len(tekst) > 200:
                    return soup
        except Exception:
            continue

    # Forsøg 5: Google Cache
    try:
        cache_url = f"https://webcache.googleusercontent.com/search?q=cache:{url}"
        resp = http_requests.get(cache_url, timeout=12, headers=HEADERS_LISTE[0], verify=False)
        if resp.status_code < 400 and len(resp.text) > 500:
            resp.encoding = resp.apparent_encoding or 'utf-8'
            return BeautifulSoup(resp.text, 'html.parser')
    except Exception:
        pass

    return None


def hent_side_tekst(url, max_tegn=3000):
    """Henter én side og returnerer renset tekst + soup"""
    soup = hent_raa_soup(url)
    if not soup:
        return '', None
    for tag in soup(['script', 'style', 'noscript', 'iframe', 'svg', 'img']):
        tag.decompose()
    tekst = ' '.join(soup.get_text(separator=' ').split())
    return tekst[:max_tegn], soup


def er_intern_url(full_url, base_netloc):
    """Returnerer True hvis URL er intern og ikke fildownload/uønsket"""
    try:
        parsed = urlparse(full_url)
        if parsed.netloc != base_netloc:
            return False
        path_lower = parsed.path.lower()
        if any(path_lower.endswith(ext) for ext in UØNSKEDE_EXTENSIONS):
            return False
        path_dele = set(path_lower.strip('/').split('/'))
        if path_dele & UØNSKEDE_PATH_DELE:
            return False
        return True
    except:
        return False


def find_alle_interne_links(soup, base_url, max_antal=200):
    """Finder alle interne links på en side"""
    base = urlparse(base_url)
    fundne = []
    seen = set()
    if not soup:
        return fundne
    for a in soup.find_all('a', href=True):
        href = a['href'].strip()
        if not href or href.startswith('#') or href.startswith('mailto:') or href.startswith('tel:'):
            continue
        full = urljoin(base_url, href)
        # Normaliser — fjern query og fragment for dedup
        parsed = urlparse(full)
        norm = f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip('/')
        if norm in seen:
            continue
        if not er_intern_url(full, base.netloc):
            continue
        seen.add(norm)
        fundne.append(full)
        if len(fundne) >= max_antal:
            break
    return fundne


def find_links_med_noegleord(soup, base_url, noegleord, max_antal=10):
    """Finder interne links hvis sti matcher nøgleordsliste"""
    alle = find_alle_interne_links(soup, base_url, max_antal=500)
    fundne = []
    seen = set()
    for full in alle:
        path = urlparse(full).path.lower().strip('/')
        if path in seen:
            continue
        if any(kw in path for kw in noegleord):
            seen.add(path)
            fundne.append(full)
            if len(fundne) >= max_antal:
                break
    return fundne


def udtræk_produkter_fra_side(soup, side_url, seen_urls):
    """
    Udtrækker produkter fra en listeside.
    Returnerer liste af dict: {navn, pris, url}
    Opdaterer seen_urls in-place.
    """
    base = urlparse(side_url)
    fundne = []

    # Selectors fra mest specifik til mindst — stop ved første hit med 3+ elementer
    kandidat_selectors = [
        # Shopify
        '.product-card', '.product-item', '.grid__item', '.product-grid-item',
        # WooCommerce
        'li.product', '.wc-block-grid__product', '.product-type-simple',
        # DanDomain / Prestashop / Magento
        '.product_list li', '.product-miniature', '.ajax_block_product',
        # Generiske
        '.product', '.product-tile', '.product-box', '.product-wrapper',
        '[class*="product-card"]', '[class*="product-item"]',
        'article.card', '.catalog-product-item',
    ]

    for sel in kandidat_selectors:
        elementer = soup.select(sel)
        if len(elementer) < 2:
            continue
        for el in elementer[:100]:
            # Find link
            a_tag = el.find('a', href=True)
            if not a_tag:
                continue
            full_url = urljoin(side_url, a_tag['href'])
            if not er_intern_url(full_url, base.netloc):
                continue
            norm_url = urlparse(full_url).scheme + '://' + urlparse(full_url).netloc + urlparse(full_url).path.rstrip('/')
            if norm_url in seen_urls:
                continue

            # Navn — prøv i rækkefølge
            navn = ''
            for kilde in [
                el.find(['h1','h2','h3','h4'], True),
                el.find(class_=lambda c: c and any(x in ' '.join(c) for x in ['name','title','navn']) if c else False),
                a_tag,
            ]:
                if kilde:
                    navn = kilde.get_text(strip=True) if hasattr(kilde, 'get_text') else str(kilde)
                    navn = navn.strip()[:100]
                    if len(navn) > 2:
                        break

            if not navn or len(navn) < 2:
                continue

            # Pris
            pris = ''
            for pris_el in el.find_all(string=True):
                if any(x in pris_el for x in ['kr', 'Kr', 'DKK', ',-', '€']):
                    kandidat = pris_el.strip()
                    if len(kandidat) < 40 and any(c.isdigit() for c in kandidat):
                        pris = kandidat
                        break

            seen_urls.add(norm_url)
            fundne.append({'navn': navn, 'pris': pris, 'url': full_url})

        if fundne:
            break  # Fandt produkter med denne selector — stop

    # Fallback: alle interne links med path-dybde >= 2 der ikke allerede er fundet
    if not fundne:
        for a in soup.find_all('a', href=True):
            full_url = urljoin(side_url, a['href'])
            if not er_intern_url(full_url, base.netloc):
                continue
            parsed = urlparse(full_url)
            norm_url = parsed.scheme + '://' + parsed.netloc + parsed.path.rstrip('/')
            if norm_url in seen_urls:
                continue
            if parsed.path.count('/') < 2:
                continue
            tekst = a.get_text(strip=True)
            if tekst and len(tekst) > 3:
                seen_urls.add(norm_url)
                fundne.append({'navn': tekst[:100], 'pris': '', 'url': full_url})
            if len(fundne) >= 50:
                break

    return fundne


def find_paginering(soup, base_url, nuvaerende_url):
    """Finder næste side i pagination"""
    base = urlparse(base_url)
    for a in soup.find_all('a', href=True):
        tekst = a.get_text(strip=True).lower()
        href = a['href']
        if any(x in tekst for x in ['næste', 'next', '›', '»', 'side 2', 'page 2']):
            full = urljoin(nuvaerende_url, href)
            if er_intern_url(full, base.netloc) and full != nuvaerende_url:
                return full
        # Tjek også rel="next"
        if a.get('rel') and 'next' in a.get('rel', []):
            full = urljoin(nuvaerende_url, href)
            if er_intern_url(full, base.netloc):
                return full
    return None


def formater_produkter_til_tekst(alle_produkter):
    """Formaterer produktliste til tekst chatbotten kan bruge"""
    if not alle_produkter:
        return ''
    linjer = ['PRODUKTKATALOG — brug disse links når kunder spørger om specifikke produkter eller kategorier:']
    for p in alle_produkter:
        linje = f"- {p['navn']}"
        if p.get('pris'):
            linje += f" — {p['pris']}"
        linje += f" | URL: {p['url']}"
        linjer.append(linje)
    return '\n'.join(linjer)


def udtræk_pdf_tekst(pdf_url, max_tegn=3000):
    """Downloader og udtrækker tekst fra en PDF-fil"""
    if not HAR_PDFPLUMBER:
        return ''
    try:
        resp = http_requests.get(pdf_url, timeout=15, headers=HEADERS, stream=True)
        if resp.status_code >= 400:
            return ''
        indhold = resp.content
        if len(indhold) > 15 * 1024 * 1024:  # Max 15 MB
            return ''
        with pdfplumber.open(io.BytesIO(indhold)) as pdf:
            sider = []
            for side in pdf.pages[:20]:  # Max 20 sider
                tekst = side.extract_text()
                if tekst:
                    sider.append(tekst.strip())
            return '\n'.join(sider)[:max_tegn]
    except Exception as e:
        print(f"PDF fejl ({pdf_url}): {e}")
        return ''


def find_pdf_links(soup, base_url, max_antal=8):
    """Finder PDF-links på en side"""
    base = urlparse(base_url)
    fundne = []
    seen = set()
    if not soup:
        return fundne
    for a in soup.find_all('a', href=True):
        href = a['href'].strip()
        full = urljoin(base_url, href)
        if full in seen:
            continue
        parsed = urlparse(full)
        # Kun PDF'er fra samme domæne eller absolutte links
        if not parsed.path.lower().endswith('.pdf'):
            continue
        seen.add(full)
        navn = a.get_text(strip=True) or parsed.path.split('/')[-1]
        fundne.append({'url': full, 'navn': navn[:80]})
        if len(fundne) >= max_antal:
            break
    return fundne


def kombiner_scan_resultater(resultater):
    """
    Kombinerer data fra flere URL-scanninger til ét samlet resultat.
    Første URL er primær — dens virksomhedsnavn/beskrivelse/kontakt bruges.
    Øvrige URLs' ekstra_viden, produkter og ydelser tilføjes.
    """
    if not resultater:
        return {}
    primær = resultater[0].copy()

    alle_ydelser = [primær.get('data', {}).get('ydelser', '')]
    alle_ekstra = [primær.get('data', {}).get('ekstra_viden', '')]
    alle_produkter = primær.get('produkter_fundet', 0)

    for r in resultater[1:]:
        d = r.get('data', {})
        kilde = urlparse(r.get('url', '')).netloc
        if d.get('ydelser'):
            alle_ydelser.append(f"[Fra {kilde}]: {d['ydelser']}")
        if d.get('ekstra_viden'):
            alle_ekstra.append(f"\n--- Fra {kilde} ---\n{d['ekstra_viden']}")
        alle_produkter += r.get('produkter_fundet', 0)

    primær['data']['ydelser'] = ' | '.join(filter(None, alle_ydelser))
    primær['data']['ekstra_viden'] = '\n'.join(filter(None, alle_ekstra))
    primær['produkter_fundet'] = alle_produkter
    primær['sider_skannet'] = sum([r.get('sider_skannet', []) for r in resultater], [])
    return primær


def _kør_scanning(job_id, url):
    """Kører komplet scanning i baggrundstråd"""
    try:
        scan_jobs[job_id]['status'] = 'running'
        base_netloc = urlparse(url).netloc

        # ── Trin 1: Forside ───────────────────────────────
        forside_tekst, forside_soup = hent_side_tekst(url, max_tegn=4000)
        if not forside_tekst:
            scan_jobs[job_id] = {'status': 'error', 'error': 'Kunne ikke hente siden'}
            return

        scan_jobs[job_id]['fremgang'] = 'Forside hentet — kortlægger alle sider...'
        sider_skannet = ['forside']
        undersider_tekst = ''

        # ── Trin 2: Find alle links på forsiden ───────────
        alle_links = find_alle_interne_links(forside_soup, url, max_antal=300)

        # Info-sider (ydelser, kontakt, om os, faq osv.)
        info_links = [l for l in alle_links if any(kw in urlparse(l).path.lower() for kw in INFO_SIDER)][:8]
        for link in info_links:
            tekst, _ = hent_side_tekst(link, max_tegn=2000)
            if tekst:
                side_navn = urlparse(link).path.rstrip('/').split('/')[-1] or 'underside'
                undersider_tekst += f"\n\n--- {side_navn} ---\n{tekst}"
                sider_skannet.append(side_navn)

        scan_jobs[job_id]['fremgang'] = f'{len(sider_skannet)} info-sider skannet — scanner produktkatalog...'

        # ── Trin 3: Byg komplet produktkatalog ───────────
        alle_produkter = []
        seen_produkt_urls = set()

        # Find shop/kategori-sider — både via nøgleord OG ved at kigge på link-strukturen
        shop_kandidater = []

        # Nøgleords-baserede shop-links
        shop_via_kw = [l for l in alle_links if any(kw in urlparse(l).path.lower() for kw in SHOP_SIDER)]
        shop_kandidater.extend(shop_via_kw[:8])

        # Tilføj forsiden selv (kan være en webshop)
        if url not in shop_kandidater:
            shop_kandidater.insert(0, url)

        sider_med_produkter = set()

        for shop_url in shop_kandidater[:10]:
            if shop_url in sider_med_produkter:
                continue
            shop_soup = hent_raa_soup(shop_url)
            if not shop_soup:
                continue

            # Udtræk produkter fra denne side
            prod = udtræk_produkter_fra_side(shop_soup, shop_url, seen_produkt_urls)
            alle_produkter.extend(prod)
            sider_med_produkter.add(shop_url)

            scan_jobs[job_id]['fremgang'] = f'Scanner produkter... ({len(alle_produkter)} fundet så langt)'

            # Paginering — hent næste sider
            naeste = find_paginering(shop_soup, url, shop_url)
            pag_count = 0
            while naeste and pag_count < 5:
                pag_soup = hent_raa_soup(naeste)
                if not pag_soup:
                    break
                prod = udtræk_produkter_fra_side(pag_soup, naeste, seen_produkt_urls)
                alle_produkter.extend(prod)
                sider_med_produkter.add(naeste)
                naeste = find_paginering(pag_soup, url, naeste)
                pag_count += 1
                scan_jobs[job_id]['fremgang'] = f'Scanner side {pag_count+1}... ({len(alle_produkter)} produkter fundet)'

            # Find kategori-undersider på shop-siden
            shop_links_local = find_alle_interne_links(shop_soup, url, max_antal=100)
            kat_links = [l for l in shop_links_local
                        if any(kw in urlparse(l).path.lower() for kw in SHOP_SIDER + ['collections','kategori','category'])
                        and l not in sider_med_produkter][:6]

            for kat_url in kat_links:
                kat_soup = hent_raa_soup(kat_url)
                if not kat_soup:
                    continue
                prod = udtræk_produkter_fra_side(kat_soup, kat_url, seen_produkt_urls)
                alle_produkter.extend(prod)
                sider_med_produkter.add(kat_url)

                # Paginering på kategori-sider
                naeste = find_paginering(kat_soup, url, kat_url)
                pag_count2 = 0
                while naeste and pag_count2 < 3:
                    pag_soup = hent_raa_soup(naeste)
                    if not pag_soup:
                        break
                    prod = udtræk_produkter_fra_side(pag_soup, naeste, seen_produkt_urls)
                    alle_produkter.extend(prod)
                    naeste = find_paginering(pag_soup, url, naeste)
                    pag_count2 += 1

                scan_jobs[job_id]['fremgang'] = f'Scanner kategorier... ({len(alle_produkter)} produkter fundet)'

            if len(alle_produkter) >= 500:
                break

        scan_jobs[job_id]['fremgang'] = f'Fandt {len(alle_produkter)} produkter — scanner PDF-filer...'

        # ── Trin 4: PDF-filer ────────────────────────────
        pdf_tekst_samlet = ''
        pdf_links = find_pdf_links(forside_soup, url, max_antal=8)
        # Find også PDFs på info-sider
        for info_link in info_links[:3]:
            info_s = hent_raa_soup(info_link)
            if info_s:
                pdf_links += find_pdf_links(info_s, url, max_antal=4)

        # Dedupliker
        seen_pdf = set()
        unikke_pdfs = []
        for p in pdf_links:
            if p['url'] not in seen_pdf:
                seen_pdf.add(p['url'])
                unikke_pdfs.append(p)

        for pdf in unikke_pdfs[:8]:
            scan_jobs[job_id]['fremgang'] = f"Læser PDF: {pdf['navn'][:40]}..."
            pdf_t = udtræk_pdf_tekst(pdf['url'])
            if pdf_t:
                pdf_tekst_samlet += f"\n\n--- PDF: {pdf['navn']} ({pdf['url']}) ---\n{pdf_t}"

        if pdf_tekst_samlet:
            undersider_tekst += pdf_tekst_samlet
            sider_skannet.append(f"PDFs ({len(unikke_pdfs)} filer)")

        scan_jobs[job_id]['fremgang'] = f'Fandt {len(alle_produkter)} produkter — analyserer med AI...'

        produkt_tekst = formater_produkter_til_tekst(alle_produkter)
        samlet_tekst = forside_tekst + undersider_tekst

        # Opdater sider_skannet med shop-info
        if sider_med_produkter:
            sider_skannet.append(f"produktsider ({len(sider_med_produkter)} skannet, {len(alle_produkter)} produkter)")

        # ── Trin 4: AI-analyse ────────────────────────────
        prompt = f"""Du er en assistent der hjælper med at opsætte en AI-chatbot for en dansk virksomhed.

Analyser denne tekst fra virksomhedens hjemmeside ({len(sider_skannet)} sider skannet) og udtræk ALT tilgængelig information.
Vær grundig — hellere for meget end for lidt.
Svar KUN med valid JSON i præcis dette format — ingen tekst udenfor JSON:

{{
  "virksomhed_navn": "Virksomhedens fulde navn",
  "beskrivelse": "2-4 sætninger om hvad virksomheden laver, hvem kunderne er og hvad der gør dem særlige",
  "ydelser": "Detaljeret liste over ALLE ydelser/produktkategorier/behandlinger — adskilt med komma. Vær meget specifik.",
  "priser": "Alle priser der er nævnt, med beskrivelse. Tom streng hvis ingen priser.",
  "aabning": "Åbningstider for alle dage. Tom streng hvis ikke nævnt.",
  "kontakt": "Telefon, email og adresse hvis fundet",
  "chatbot_navn": "Et venligt danskklingende fornavn til chatbotten (ikke virksomhedsnavnet)",
  "velkomst": "En imødekommende velkomsttekst på dansk til chatbotten (max 20 ord)",
  "branche": "Én branche: frisør/tandlæge/håndværker/restaurant/ejendomsmægler/rengøring/webshop/pool_spa/andet",
  "ekstra_viden": "Alt andet nyttigt: garantier, certifikater, FAQ-svar, geografisk område, leveringsbetingelser, returpolitik, specielle tilbud, betalingsmetoder osv. Punktform med linjeskift."
}}

Hjemmesidetekst:
{samlet_tekst[:12000]}"""

        msg = ai.messages.create(
            model='claude-sonnet-4-6',
            max_tokens=2000,
            messages=[{'role': 'user', 'content': prompt}]
        )
        raw = msg.content[0].text.strip()
        if raw.startswith('```'):
            raw = raw.split('```')[1]
            if raw.startswith('json'):
                raw = raw[4:]
        resultat = json.loads(raw)

        if produkt_tekst:
            eksisterende = resultat.get('ekstra_viden', '')
            resultat['ekstra_viden'] = (eksisterende + '\n\n' + produkt_tekst).strip()

        scan_jobs[job_id] = {
            'status': 'done',
            'data': resultat,
            'url': url,
            'sider_skannet': sider_skannet,
            'produkter_fundet': len(alle_produkter),
        }
    except Exception as e:
        import traceback
        scan_jobs[job_id] = {'status': 'error', 'error': str(e), 'trace': traceback.format_exc()[-500:]}


@app.route('/scan-hjemmeside', methods=['POST'])
@require_admin
def scan_hjemmeside():
    """Starter asynkron scanning — returnerer job_id med det samme"""
    data = request.json
    url = (data.get('url') or '').strip()
    if not url:
        return jsonify({'error': 'URL mangler'}), 400
    if not url.startswith('http'):
        url = 'https://' + url

    job_id = secrets.token_hex(8)
    scan_jobs[job_id] = {'status': 'running', 'fremgang': 'Starter scanning...'}
    threading.Thread(target=_kør_scanning, args=(job_id, url), daemon=True).start()
    return jsonify({'job_id': job_id, 'status': 'running'})


@app.route('/scan-status/<job_id>', methods=['GET'])
def scan_status(job_id):
    """Tjek status på en igangværende scanning"""
    job = scan_jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job ikke fundet'}), 404
    return jsonify(job)


def _kør_multi_scanning(job_id, urls):
    """Scanner flere URLs og kombinerer resultaterne"""
    resultater = []
    total = len(urls)

    for i, url in enumerate(urls):
        sub_id = f"{job_id}_sub_{i}"
        scan_jobs[sub_id] = {'status': 'running', 'fremgang': 'Starter...'}
        scan_jobs[job_id]['fremgang'] = f'Scanner {i+1}/{total}: {url[:50]}...'
        scan_jobs[job_id]['sub_jobs'] = [f"{job_id}_sub_{j}" for j in range(total)]

        _kør_scanning(sub_id, url)

        sub_job = scan_jobs.get(sub_id, {})
        if sub_job.get('status') == 'done':
            resultater.append(sub_job)
            scan_jobs[job_id]['fremgang'] = f'✅ {i+1}/{total} sider scannet — fortsætter...'
        else:
            scan_jobs[job_id]['fremgang'] = f'⚠️ {url[:40]} kunne ikke scannes — fortsætter...'

    if not resultater:
        scan_jobs[job_id] = {'status': 'error', 'error': 'Ingen af URLs kunne scannes'}
        return

    # Kombiner alle resultater
    kombineret = kombiner_scan_resultater(resultater)
    kombineret['status'] = 'done'
    scan_jobs[job_id] = kombineret


@app.route('/hent-pdf-links', methods=['POST'])
def hent_pdf_links_endpoint():
    """Henter og udtrækker tekst fra en liste af PDF-URLs"""
    data = request.json
    urls = data.get('urls', [])
    urls = [u.strip() for u in urls if u and u.strip()]
    if not urls:
        return jsonify({'error': 'Ingen URLs angivet'}), 400

    samlet = []
    antal_ok = 0
    for url in urls[:10]:
        if not url.startswith('http'):
            url = 'https://' + url
        tekst = udtræk_pdf_tekst(url, max_tegn=10000)
        if tekst:
            filnavn = url.rstrip('/').split('/')[-1]
            samlet.append(f"--- PDF: {filnavn} ---\n{tekst}")
            antal_ok += 1
        else:
            # Prøv også som webside (nogle PDF-links er HTML-sider der indeholder PDF)
            side_tekst, _ = hent_side_tekst(url, max_tegn=3000)
            if side_tekst:
                samlet.append(f"--- Side: {url.rstrip('/').split('/')[-1]} ---\n{side_tekst}")
                antal_ok += 1

    if not samlet:
        return jsonify({'error': 'Kunne ikke hente indhold fra nogen af linkene'}), 400

    return jsonify({'success': True, 'tekst': '\n\n'.join(samlet), 'antal': antal_ok})


@app.route('/scan-multi', methods=['POST'])
@require_admin
def scan_multi():
    """Scanner flere URLs og kombinerer data — returnerer job_id"""
    data = request.json
    urls = data.get('urls', [])
    urls = [u.strip() for u in urls if u and u.strip()]
    urls = [('https://' + u if not u.startswith('http') else u) for u in urls]
    if not urls:
        return jsonify({'error': 'Ingen URLs angivet'}), 400
    if len(urls) > 5:
        urls = urls[:5]

    job_id = secrets.token_hex(8)
    scan_jobs[job_id] = {'status': 'running', 'fremgang': f'Starter scanning af {len(urls)} hjemmesider...'}
    threading.Thread(target=_kør_multi_scanning, args=(job_id, urls), daemon=True).start()
    return jsonify({'job_id': job_id, 'status': 'running', 'antal_urls': len(urls)})


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
            from_email=(SENDGRID_FROM, 'NexOlsen Test'),
            to_emails=til,
            subject='NexOlsen test mail',
            plain_text_content='Denne mail bekræfter at NexOlsen mail-systemet virker.',
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

@app.route('/widget.js', methods=['GET'])
def serve_widget_js():
    """Alias for chatbot.js — bruges i widget-kode genereret til klienter"""
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

@app.route('/ping', methods=['GET'])
def ping():
    return jsonify({'status': 'ok'})


@app.route('/login', methods=['POST'])
@limiter.limit("10 per minute; 30 per hour")
def login():
    data = request.json
    email = (data.get('email') or '').strip().lower()
    password = data.get('password') or ''

    admin_email = os.environ.get('ADMIN_EMAIL', 'admin@klaiai.dk').lower()
    admin_pw = os.environ.get('ADMIN_PASSWORD', 'klaiai2024')

    # Admin login
    if email == admin_email and password == admin_pw:
        token = secrets.token_hex(32)
        _gem_token(token, {'role': 'admin', 'created_at': _time.time()})
        return jsonify({'token': token, 'role': 'admin'})

    # Klient login — tjek Supabase
    if db:
        try:
            res = db.table('klienter').select('id, navn, email, password, aktiv').eq('email', email).single().execute()
            if res.data:
                klient = res.data
                if klient.get('aktiv') == False:
                    return jsonify({'error': 'Adgang er deaktiveret. Kontakt NexOlsen.'}), 403
                klient_pw = klient.get('password', '')
                if not klient_pw:
                    return jsonify({'error': 'Forkert email eller adgangskode'}), 401
                # Understøt både bcrypt-hash og klartekst (bagudkompatibilitet)
                pw_ok = False
                if klient_pw.startswith('$2b$') or klient_pw.startswith('$2a$'):
                    try:
                        pw_ok = bcrypt.checkpw(password.encode(), klient_pw.encode())
                    except:
                        pw_ok = False
                else:
                    pw_ok = (password == klient_pw)
                    # Opgrader klartekst til hash ved første login
                    if pw_ok:
                        try:
                            ny_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
                            db.table('klienter').update({'password': ny_hash}).eq('id', klient['id']).execute()
                        except:
                            pass
                if pw_ok:
                    token = secrets.token_hex(32)
                    _gem_token(token, {'role': 'client', 'klient_id': klient['id'], 'created_at': _time.time()})
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

def _ryd_gamle_sessions():
    """Fjern demo_sessions ældre end 2 timer, udløbne tokens og prospekter ældre end 7 dage"""
    _ryd_tokens()
    import datetime as _dt
    nu = _dt.datetime.now()
    for did in list(demo_sessions.keys()):
        try:
            oprettet = _dt.datetime.fromisoformat(demo_sessions[did].get('created_at', ''))
            if (nu - oprettet).total_seconds() > 7200:  # 2 timer
                del demo_sessions[did]
        except:
            del demo_sessions[did]

# ══════════════════════════════════════════════════════════════
#  DEMO FRA URL
# ══════════════════════════════════════════════════════════════

@app.route('/demo', methods=['GET'])
def demo_page():
    from flask import send_from_directory
    app_dir = os.path.join(os.path.dirname(__file__), '..', 'app')
    return send_from_directory(app_dir, 'demo.html')

@app.route('/demo/scan', methods=['POST'])
def demo_scan():
    """Scanner en hjemmeside-URL og genererer en personlig chatbot-config"""
    import uuid, re, datetime as _dt
    _ryd_gamle_sessions()  # Ryd gamle sessioner
    data = request.json or {}
    raw_url = (data.get('url') or '').strip()
    if not raw_url:
        return jsonify({'error': 'URL mangler'}), 400
    if not raw_url.startswith('http'):
        raw_url = 'https://' + raw_url

    # Hent hjemmeside
    try:
        resp = http_requests.get(raw_url, timeout=10, verify=False,
            headers={'User-Agent': 'Mozilla/5.0 (compatible; NexOlsen-Demo/1.0)'})
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')
    except Exception as e:
        return jsonify({'error': f'Kunne ikke hente siden: {str(e)}'}), 400

    # Tjek om de allerede har en chatbot
    html_lower = resp.text.lower()
    chatbot_vendors = ['intercom', 'zendesk', 'drift.com', 'hubspot', 'tidio',
                       'freshchat', 'crisp.chat', 'tawk.to', 'chatbot', 'livechat',
                       'klaiai', 'widget.js', 'chat-widget']
    har_chatbot = any(v in html_lower for v in chatbot_vendors)

    # Udtræk tekst
    for tag in soup(['script', 'style', 'nav', 'footer', 'header']):
        tag.decompose()
    tekst = soup.get_text(separator=' ', strip=True)
    tekst = re.sub(r'\s+', ' ', tekst)[:4000]

    # Udtræk primærfarve fra CSS (første hex-farve der ikke er sort/hvid)
    css_farver = re.findall(r'#([0-9a-fA-F]{3,6})', resp.text)
    primær_farve = '#0a2463'
    for f in css_farver:
        if len(f) in (3, 6):
            if f not in ('fff', 'ffffff', '000', '000000', 'FFF', 'FFFFFF'):
                primær_farve = '#' + f
                break

    # Udtræk titel / virksomhedsnavn
    title = soup.find('title')
    virk_navn = title.text.strip().split('|')[0].split('–')[0].strip() if title else raw_url

    # Generer chatbot-config med Claude
    try:
        prompt_resp = ai.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=600,
            messages=[{'role': 'user', 'content': f"""Baseret på denne hjemmesidetekst, generér en chatbot-konfiguration på dansk.

Hjemmeside: {raw_url}
Virksomhedsnavn: {virk_navn}
Tekst fra siden:
{tekst}

Svar KUN med gyldig JSON (ingen forklaring) i dette format:
{{
  "chatbot_navn": "fornuftigt navn (fx Alma, Mia, Emil)",
  "velkomst": "personlig velkomstbesked der nævner virksomheden og hvad de tilbyder (maks 20 ord)",
  "ydelser": "kommasepareret liste af de vigtigste ydelser/produkter fra siden",
  "beskrivelse": "1-2 sætninger om hvad virksomheden laver"
}}"""}]
        )
        cfg_raw = prompt_resp.content[0].text.strip()
        # Udtræk JSON
        json_match = re.search(r'\{.*\}', cfg_raw, re.DOTALL)
        ai_cfg = json.loads(json_match.group()) if json_match else {}
    except Exception as e:
        print(f"Demo scan AI fejl: {e}")
        ai_cfg = {}

    demo_id = str(uuid.uuid4())[:12]
    klient_config = {
        'navn': virk_navn,
        'chatbot_navn': ai_cfg.get('chatbot_navn', 'Alma'),
        'velkomst': ai_cfg.get('velkomst', f'Hej! Jeg er {virk_navn}s AI-assistent. Hvordan kan jeg hjælpe dig?'),
        'farve': primær_farve,
        'ekstra_viden': ai_cfg.get('ydelser', ''),
        'info': {
            'åbningstider': '',
            'kontakt': '',
            'ydelser': ai_cfg.get('ydelser', ''),
            'priser': '',
            'adresse': '',
            'andet': ai_cfg.get('beskrivelse', '')
        }
    }
    demo_sessions[demo_id] = {
        'klient_config': klient_config,
        'url': raw_url,
        'har_chatbot': har_chatbot,
        'created_at': _dt.datetime.now().isoformat()
    }

    return jsonify({
        'demo_id': demo_id,
        'virk_navn': virk_navn,
        'chatbot_navn': klient_config['chatbot_navn'],
        'velkomst': klient_config['velkomst'],
        'farve': primær_farve,
        'har_chatbot': har_chatbot,
        'beskrivelse': ai_cfg.get('beskrivelse', ''),
        'ydelser': ai_cfg.get('ydelser', '')
    })

@app.route('/demo/config/<demo_id>', methods=['GET'])
def demo_config(demo_id):
    """Returnerer chatbot-config for en demo-session"""
    if demo_id not in demo_sessions:
        return jsonify({'error': 'Demo-session udløbet — prøv igen'}), 404
    cfg = demo_sessions[demo_id]['klient_config']
    return jsonify({
        'navn': cfg['chatbot_navn'],
        'velkomst': cfg['velkomst'],
        'farve': cfg['farve'],
        'info': cfg['info'],
        'ekstra_viden': cfg.get('ekstra_viden', '')
    })

@app.route('/demo/tilmeld', methods=['POST'])
def demo_tilmeld():
    """Gemmer email fra demo-siden som et prospekt-lead"""
    data = request.json or {}
    email   = (data.get('email') or '').strip()
    demo_id = data.get('demo_id') or ''
    url     = ''
    navn    = ''
    if demo_id in demo_sessions:
        url  = demo_sessions[demo_id].get('url', '')
        navn = demo_sessions[demo_id]['klient_config'].get('navn', '')

    if not email or '@' not in email:
        return jsonify({'error': 'Ugyldig email'}), 400

    # Gem som prospekt i Supabase (i lead-tabellen under admin-klient)
    if db:
        try:
            db.table('leads').insert({
                'klient_id': 'demo',
                'navn': navn or email.split('@')[0],
                'email': email,
                'besked': f'Demo-interesse via {url}',
                'kilde': 'demo',
                'status': 'ny'
            }).execute()
        except Exception as e:
            print(f"Demo tilmeld DB fejl: {e}")

    # Send bekræftelsesmail
    if SENDGRID_API_KEY and email:
        html = f"""
<div style="font-family:Inter,Arial,sans-serif;max-width:560px;margin:0 auto;color:#1a1918">
  <div style="background:#0a2463;padding:2rem;border-radius:12px 12px 0 0;text-align:center">
    <div style="font-size:1.5rem;font-weight:900;color:#fff">Tak for din interesse! 🎉</div>
  </div>
  <div style="background:#fff;padding:2rem;border-radius:0 0 12px 12px;border:1px solid #e5e3de;border-top:none">
    <p>Vi har registreret din interesse og vender tilbage inden for 24 timer med et personligt tilbud til <strong>{navn or url}</strong>.</p>
    <p>Mens du venter kan du se mere på <a href="https://nexolsen.dk" style="color:#0a2463">nexolsen.dk</a>.</p>
    <p style="color:#9a9590;font-size:.85rem">NexOlsen · support@nexolsen.dk</p>
  </div>
</div>"""
        send_mail(email, 'Vi vender tilbage inden for 24 timer 👋', html, 'NexOlsen')

    # Notifier admin
    if SENDGRID_API_KEY and ADMIN_EMAIL:
        send_mail(ADMIN_EMAIL, f'NY DEMO-INTERESSE: {email} ({navn or url})',
            f'Email: {email}\nVirksomhed: {navn}\nURL: {url}\n\nFølg op!', 'NexOlsen System')

    return jsonify({'success': True})


# ══════════════════════════════════════════════════════════════
#  OUTBOUND SALGSMASKINE
# ══════════════════════════════════════════════════════════════

def _hent_prospekt(pid):
    if not db:
        return prospekter.get(pid)
    try:
        r = db.table('prospekter').select('*').eq('id', pid).single().execute()
        return r.data
    except:
        return None

def _gem_prospekt(p):
    if db:
        try:
            db.table('prospekter').upsert(p).execute()
        except Exception as e:
            print(f"Prospekt gem fejl: {e}")
    prospekter[p['id']] = p

@app.route('/prospekt/liste', methods=['GET'])
@require_admin
def prospekt_liste():
    if db:
        try:
            r = db.table('prospekter').select('*').order('created_at', desc=True).execute()
            return jsonify({'prospekter': r.data or []})
        except Exception as e:
            print(f"Prospekt liste fejl: {e}")
    return jsonify({'prospekter': list(prospekter.values())})

@app.route('/prospekt/tilfoej', methods=['POST'])
@require_admin
def prospekt_tilfoej():
    import uuid, re
    data = request.json or {}
    urls_raw = data.get('urls', '')
    urls = [u.strip() for u in re.split(r'[,\n;]+', urls_raw) if u.strip()]
    tilfoejede = []
    for url in urls:
        if not url.startswith('http'):
            url = 'https://' + url
        pid = str(uuid.uuid4())[:10]
        p = {
            'id': pid, 'url': url, 'navn': '', 'beskrivelse': '',
            'smertepunkt': '', 'har_chatbot': None, 'email': '',
            'telefon': '', 'email_udkast': '', 'status': 'ny', 'noter': ''
        }
        _gem_prospekt(p)
        tilfoejede.append(pid)
    return jsonify({'success': True, 'tilfoejede': len(tilfoejede), 'ids': tilfoejede})

@app.route('/prospekt/scan/<pid>', methods=['POST'])
@require_admin
def prospekt_scan(pid):
    import re
    p = _hent_prospekt(pid)
    if not p:
        return jsonify({'error': 'Prospekt ikke fundet'}), 404

    url = p['url']
    try:
        resp = http_requests.get(url, timeout=10, verify=False,
            headers={'User-Agent': 'Mozilla/5.0 (compatible; NexOlsen/1.0)'})
        soup = BeautifulSoup(resp.text, 'html.parser')
    except Exception as e:
        p['status'] = 'scan-fejl'
        _gem_prospekt(p)
        return jsonify({'error': str(e)}), 400

    html_lower = resp.text.lower()
    chatbot_vendors = ['intercom', 'zendesk', 'drift', 'hubspot', 'tidio',
                       'freshchat', 'crisp', 'tawk.to', 'livechat', 'klaiai',
                       'widget.js', 'chat-widget', 'chatbase', 'botpress']
    p['har_chatbot'] = any(v in html_lower for v in chatbot_vendors)

    emails = re.findall(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+', resp.text)
    tlf    = re.findall(r'(?:\+45[\s]?)?(?:\d{2}[\s]?){4}', resp.text)
    p['email']   = emails[0] if emails else ''
    p['telefon'] = tlf[0].strip() if tlf else ''

    for tag in soup(['script', 'style', 'nav', 'footer']):
        tag.decompose()
    tekst = re.sub(r'\s+', ' ', soup.get_text(separator=' ', strip=True))[:3000]

    title = soup.find('title')
    p['navn'] = title.text.strip().split('|')[0].split('–')[0].strip() if title else url

    try:
        ai_resp = ai.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=700,
            messages=[{'role': 'user', 'content': f"""Du er salgskonsulent hos NexOlsen, der sælger AI-chatbots til danske virksomheder.

Analysér denne hjemmeside og svar KUN med JSON:

URL: {url}
Virksomhedsnavn: {p['navn']}
Tekst: {tekst}
Har allerede chatbot: {p['har_chatbot']}

JSON-format:
{{
  "beskrivelse": "1 sætning om hvad virksomheden laver",
  "smertepunkt": "den vigtigste grund til at de har brug for en AI-chatbot (leads, bookinger, FAQ...)",
  "email_emne": "fængende emne til cold email (maks 8 ord)",
  "email_tekst": "personlig cold email på 4-5 linjer dansk. Nævn specifikt hvad de sælger, og hvad de mister ved ikke at have AI. Afslut med en konkret CTA. Underskriv som 'Mattis fra NexOlsen'. INGEN emojis."
}}"""}]
        )
        raw = ai_resp.content[0].text.strip()
        jm  = re.search(r'\{.*\}', raw, re.DOTALL)
        ai_data = json.loads(jm.group()) if jm else {}
    except Exception as e:
        print(f"Prospekt scan AI fejl: {e}")
        ai_data = {}

    p['beskrivelse']  = ai_data.get('beskrivelse', '')
    p['smertepunkt']  = ai_data.get('smertepunkt', '')
    p['email_udkast'] = f"Emne: {ai_data.get('email_emne', '')}\n\n{ai_data.get('email_tekst', '')}"
    p['status'] = 'scannet'
    _gem_prospekt(p)
    return jsonify({'success': True, 'prospekt': p})

@app.route('/prospekt/send-email/<pid>', methods=['POST'])
@require_admin
def prospekt_send_email(pid):
    p = _hent_prospekt(pid)
    if not p:
        return jsonify({'error': 'Prospekt ikke fundet'}), 404
    data = request.json or {}
    til_email = data.get('email', '').strip() or p.get('email', '')
    if not til_email or '@' not in til_email:
        return jsonify({'error': 'Ingen gyldig email — indsæt manuelt'}), 400

    udkast = p.get('email_udkast', '')
    linjer = udkast.split('\n')
    emne   = linjer[0].replace('Emne:', '').strip() if linjer else f"AI-chatbot til {p['navn']}"
    tekst  = '\n'.join(linjer[2:]).strip() if len(linjer) > 2 else udkast

    html = f"""<div style="font-family:Arial,sans-serif;max-width:560px;padding:20px;color:#1a1918">
{('<br>'.join(tekst.split(chr(10))))}
<br><br>
<a href="https://klaiai.onrender.com/demo" style="display:inline-block;background:#0a2463;color:#fff;text-decoration:none;padding:10px 24px;border-radius:8px;font-weight:700;font-size:.9rem">
  Se din gratis AI-demo →
</a>
<br><br>
<span style="font-size:.8rem;color:#9a9590">NexOlsen · AI-agenter til din virksomhed · <a href="mailto:support@nexolsen.dk" style="color:#9a9590">support@nexolsen.dk</a></span>
</div>"""

    ok = send_mail(til_email, emne, html, 'Mattis fra NexOlsen')
    if ok:
        p['status'] = 'email-sendt'
        p['email']  = til_email
        _gem_prospekt(p)
        return jsonify({'success': True, 'sendt_til': til_email})
    return jsonify({'error': 'Email fejlede — tjek SendGrid'}), 500

@app.route('/prospekt/opdater/<pid>', methods=['PATCH'])
@require_admin
def prospekt_opdater(pid):
    p = _hent_prospekt(pid)
    if not p:
        return jsonify({'error': 'Ikke fundet'}), 404
    data = request.json or {}
    for felt in ('email', 'email_udkast', 'noter', 'status'):
        if felt in data:
            p[felt] = data[felt]
    _gem_prospekt(p)
    return jsonify({'success': True, 'prospekt': p})

@app.route('/prospekt/slet/<pid>', methods=['DELETE'])
@require_admin
def prospekt_slet(pid):
    if db:
        try:
            db.table('prospekter').delete().eq('id', pid).execute()
        except:
            pass
    prospekter.pop(pid, None)
    return jsonify({'success': True})

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


# ══════════════════════════════════════════════════════════════
#  SCHEDULED AGENTS
# ══════════════════════════════════════════════════════════════
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timedelta

scheduler = BackgroundScheduler(daemon=True)

def _log_agent(agent_navn, klient_id, reference_id, besked):
    if not db:
        return
    try:
        db.table('agent_log').insert({
            'agent': agent_navn,
            'klient_id': klient_id,
            'reference_id': str(reference_id) if reference_id else None,
            'besked': besked
        }).execute()
    except Exception as e:
        print(f"Agent log fejl: {e}")

def _allerede_sendt(agent_navn, reference_id):
    """Tjek om agenten allerede har behandlet denne reference"""
    if not db:
        return False
    try:
        res = db.table('agent_log')\
            .select('id')\
            .eq('agent', agent_navn)\
            .eq('reference_id', str(reference_id))\
            .execute()
        return len(res.data) > 0
    except:
        return False

def _send_opsaetningsmanual(klient, klient_id, plan, produkter):
    """Generer og send skræddersyet opsætningsmanual via Claude"""
    platform = klient.get('platform', 'anden')
    navn = klient.get('navn', 'kunde')
    email = klient.get('email', '')
    if not email:
        return

    platform_navne = {
        'shopify': 'Shopify', 'wordpress': 'WordPress', 'wix': 'Wix',
        'squarespace': 'Squarespace', 'webflow': 'Webflow',
        'dandomain': 'DanDomain', 'one.com': 'One.com',
        'html': 'statisk HTML', 'anden': 'din hjemmeside'
    }
    platform_navn = platform_navne.get(platform, 'din hjemmeside')
    plan_navn = STRIPE_PRISER.get(plan, STRIPE_PRISER['starter'])['navn']

    prompt = f"""Du er NexOlsen's tekniske support. Skriv en komplet opsætningsmanual på dansk til en ny kunde.

Kundeinfo:
- Virksomhed: {navn}
- Platform: {platform_navn}
- Plan: {plan_navn}
- Aktive produkter: {', '.join(produkter)}
- Klient-ID: {klient_id}

Manualen skal indeholde:

1. **Velkomsthilsen** (kort og personlig)

2. **Chatbot installation på {platform_navn}**
   - Præcis trin-for-trin guide til at indsætte dette script på {platform_navn}:
   ```html
   <script src="https://klaiai.onrender.com/chatbot.js" data-client="{klient_id}" data-color="#0a2463"></script>
   ```
   - Tilpas guiden til {platform_navn}'s interface (fx Theme Editor, Custom HTML widget, etc.)

3. **Login til admin-panel**
   - URL: https://klaiai.dk/login
   - Forklaring af hvad de kan gøre i panelet

{'4. **Lead-opsamling** - Forklaring af hvordan leads vises og godkendes' if 'lead' in produkter else ''}
{'5. **Bookingsystem** - Kort intro til booking-funktionen' if 'booking' in produkter else ''}
{'6. **Ugentlige AI-rapporter** - Hvad de modtager og hvornår' if 'rapport' in produkter else ''}

Afslut med kontaktinfo: support@nexolsen.dk

Skriv i en venlig, professionel tone. Brug markdown-formatering med overskrifter og punktlister."""

    try:
        resp = client.messages.create(
            model='claude-opus-4-8',
            max_tokens=2000,
            messages=[{'role': 'user', 'content': prompt}]
        )
        manual_tekst = resp.content[0].text

        # Konverter markdown til simpel HTML
        html_body = manual_tekst \
            .replace('**', '<strong>', 1)
        # Simpel markdown→html konvertering
        lines = manual_tekst.split('\n')
        html_lines = []
        for line in lines:
            line = line.strip()
            if line.startswith('# '):
                html_lines.append(f'<h1 style="color:#0a2463">{line[2:]}</h1>')
            elif line.startswith('## '):
                html_lines.append(f'<h2 style="color:#0a2463; margin-top:1.5rem">{line[3:]}</h2>')
            elif line.startswith('### '):
                html_lines.append(f'<h3>{line[4:]}</h3>')
            elif line.startswith('- ') or line.startswith('* '):
                html_lines.append(f'<li>{line[2:]}</li>')
            elif line.startswith('```'):
                pass
            elif line:
                # Fed tekst
                import re
                line = re.sub(r'\*\*(.*?)\*\*', r'<strong>\1</strong>', line)
                html_lines.append(f'<p>{line}</p>')
            else:
                html_lines.append('<br/>')

        html_content = f"""
        <div style="font-family: 'Inter', Arial, sans-serif; max-width: 640px; margin: 0 auto; color: #1a1918;">
          <div style="background:#0a2463; padding:2rem; border-radius:12px 12px 0 0; text-align:center;">
            <h1 style="color:#fff; margin:0; font-size:1.5rem">Velkommen til NexOlsen</h1>
            <p style="color:rgba(255,255,255,.7); margin:.5rem 0 0">Din opsætningsguide til {platform_navn}</p>
          </div>
          <div style="background:#fff; padding:2rem; border-radius:0 0 12px 12px; border:1px solid #e5e3de; border-top:none;">
            {''.join(html_lines)}
            <hr style="border:none;border-top:1px solid #e5e3de; margin:2rem 0"/>
            <p style="color:#9a9590; font-size:.85rem; text-align:center">
              NexOlsen · support@nexolsen.dk · <a href="https://klaiai.dk/login" style="color:#0a2463">Log ind her</a>
            </p>
          </div>
        </div>"""

        send_mail(email, f'🚀 Kom i gang med NexOlsen – din guide til {platform_navn}', html_content, 'NexOlsen')
        print(f"Opsætningsmanual sendt til {email}")
    except Exception as e:
        print(f"Manual generering fejl: {e}")
        # Fallback: send simpel velkomstmail
        send_mail(email, 'Velkommen til NexOlsen 🎉',
            f"""<p>Hej {navn}!</p>
            <p>Din konto er nu aktiv. Log ind på <a href="https://klaiai.dk/login">klaiai.dk/login</a></p>
            <p>Din chatbot-kode:<br><code>&lt;script src="https://klaiai.onrender.com/chatbot.js" data-client="{klient_id}"&gt;&lt;/script&gt;</code></p>
            <p>Med venlig hilsen,<br>NexOlsen</p>""", 'NexOlsen')


def _gem_til_godkendelse(klient_id, lead_id, emne, html, agent_navn, reference_id, mail_nr=1):
    """Gem agent-mail til godkendelse i stedet for at sende direkte"""
    if not db:
        return False
    try:
        db.table('lead_mails').insert({
            'lead_id': str(lead_id) if lead_id else None,
            'klient_id': klient_id,
            'mail_nr': mail_nr,
            'emne': emne,
            'tekst': html,
            'status': 'afventer'
        }).execute()
        _log_agent(agent_navn, klient_id, reference_id, f"Mail gemt til godkendelse: {emne}")
        print(f"  ✅ Gemt til godkendelse: {emne}")
        return True
    except Exception as e:
        print(f"  ❌ Gem til godkendelse fejl: {e}")
        return False

def kør_reminder_agent():
    """🔔 Sender påmindelses-mail til leads med booking i morgen"""
    if not db:
        return
    print("🔔 Påmindelses-agent kører...")
    i_morgen = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')
    try:
        bookinger = db.table('bookinger')\
            .select('*, leads(navn, email, klient_id)')\
            .eq('dato', i_morgen)\
            .execute()
        if not bookinger.data:
            _log_agent('reminder', None, None, '🔔 Ingen bookinger i morgen — ingen påmindelser sendt')
        for b in bookinger.data:
            lead_info = b.get('leads') or {}
            lead_id = b.get('lead_id')
            if not lead_id or _allerede_sendt('reminder', b['id']):
                continue
            navn = lead_info.get('navn', 'kunde')
            email = lead_info.get('email')
            klient_id = lead_info.get('klient_id')
            if not email:
                continue
            # Hent klient navn
            klient_navn = klient_id
            try:
                kr = db.table('chatbot_config').select('virksomhed_navn').eq('klient_id', klient_id).execute()
                if kr.data:
                    klient_navn = kr.data[0].get('virksomhed_navn', klient_id)
            except:
                pass
            dato_fmt = b.get('dato', i_morgen)
            tidspunkt = b.get('tidspunkt', '')
            emne = f"Påmindelse: Din booking hos {klient_navn} i morgen"
            html = f"""
            <div style="font-family:Arial,sans-serif;max-width:600px;margin:auto">
              <h2 style="color:#7c3aed">Hej {navn} 👋</h2>
              <p>Dette er en venlig påmindelse om din kommende booking:</p>
              <div style="background:#f3f0ff;border-left:4px solid #7c3aed;padding:16px;border-radius:8px;margin:16px 0">
                <strong>📅 Dato:</strong> {dato_fmt}<br>
                <strong>🕐 Tidspunkt:</strong> {tidspunkt or 'Se din bekræftelse'}<br>
                <strong>🏢 Virksomhed:</strong> {klient_navn}
              </div>
              <p>Vi glæder os til at se dig! Hvis du har spørgsmål, er du altid velkommen til at kontakte os.</p>
              <p style="color:#888;font-size:12px">Med venlig hilsen, {klient_navn}</p>
            </div>
            """
            _gem_til_godkendelse(klient_id, lead_id, emne, html, 'reminder', b['id'])
    except Exception as e:
        print(f"Påmindelses-agent fejl: {e}")

def kør_review_agent():
    """⭐ Sender anmeldelsesanmodning til leads med booking i går"""
    if not db:
        return
    print("⭐ Review-agent kører...")
    i_gaar = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
    try:
        bookinger = db.table('bookinger')\
            .select('*, leads(navn, email, klient_id)')\
            .eq('dato', i_gaar)\
            .execute()
        for b in bookinger.data:
            lead_info = b.get('leads') or {}
            lead_id = b.get('lead_id')
            if not lead_id or _allerede_sendt('review', b['id']):
                continue
            navn = lead_info.get('navn', 'kunde')
            email = lead_info.get('email')
            klient_id = lead_info.get('klient_id')
            if not email:
                continue
            klient_navn = klient_id
            review_link = ''
            try:
                kr = db.table('chatbot_config').select('virksomhed_navn, google_review_link').eq('klient_id', klient_id).execute()
                if kr.data:
                    klient_navn = kr.data[0].get('virksomhed_navn', klient_id)
                    review_link = kr.data[0].get('google_review_link', '')
            except:
                pass
            emne = f"Tak for dit besøg hos {klient_navn} – giv os en anmeldelse 🌟"
            anmeld_knap = f'<a href="{review_link}" style="display:inline-block;background:#7c3aed;color:white;padding:12px 24px;border-radius:8px;text-decoration:none;font-weight:bold">⭐ Skriv en anmeldelse</a>' if review_link else '<p>Vi håber, du vil anbefale os til andre!</p>'
            html = f"""
            <div style="font-family:Arial,sans-serif;max-width:600px;margin:auto">
              <h2 style="color:#7c3aed">Tak for dit besøg, {navn}! 🙏</h2>
              <p>Vi håber, du havde en god oplevelse hos {klient_navn} i går.</p>
              <p>Din mening betyder meget for os – og for andre, der overvejer at bruge vores ydelser.</p>
              <div style="text-align:center;margin:32px 0">
                {anmeld_knap}
              </div>
              <p>Det tager kun 1 minut, og det hjælper os enormt meget! 😊</p>
              <p style="color:#888;font-size:12px">Med venlig hilsen, {klient_navn}</p>
            </div>
            """
            _gem_til_godkendelse(klient_id, lead_id, emne, html, 'review', b['id'])
    except Exception as e:
        print(f"Review-agent fejl: {e}")

def kør_genopvarmning_agent():
    """🧊 Genopvarmer leads der er 14+ dage gamle og stadig 'ny'"""
    if not db:
        return
    print("🧊 Genopvarmnings-agent kører...")
    cutoff = (datetime.now() - timedelta(days=14)).isoformat()
    try:
        leads = db.table('leads')\
            .select('*')\
            .eq('status', 'ny')\
            .lt('oprettet', cutoff)\
            .execute()
        if not leads.data:
            _log_agent('genopvarmning', None, None, '🧊 Ingen kolde leads fundet — alle leads er under 14 dage gamle')
        for lead in leads.data:
            lead_id = lead['id']
            if _allerede_sendt('genopvarmning', lead_id):
                continue
            navn = lead.get('navn', 'kunde')
            email = lead.get('email')
            klient_id = lead.get('klient_id')
            if not email:
                continue
            klient_navn = klient_id
            ydelse = lead.get('ydelse_interesse', 'vores ydelser')
            try:
                kr = db.table('chatbot_config').select('virksomhed_navn, ydelser, tone_of_voice').eq('klient_id', klient_id).execute()
                if kr.data:
                    klient_navn = kr.data[0].get('virksomhed_navn', klient_id)
            except:
                pass
            # Brug Claude til at generere personlig genopvarmnings-mail
            try:
                ai = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY', ''))
                prompt = f"""Du er en venlig salgskonsulent for {klient_navn}.

Skriv en kort, personlig genopvarmnings-mail til {navn} som viste interesse for {ydelse} for 2 uger siden men endnu ikke har booket.

Regler:
- Vær varm og uformel, ikke pushy
- Maks 4-5 sætninger
- Tilbyd hjælp eller stil et åbent spørgsmål
- Afslut med opfordring til at booke
- Returner KUN HTML til mail-body (ingen <html>/<body> tags)
- Brug #7c3aed som accentfarve"""

                resp = ai.messages.create(
                    model='claude-opus-4-8',
                    max_tokens=500,
                    messages=[{'role': 'user', 'content': prompt}]
                )
                mail_html = resp.content[0].text
            except Exception as e:
                print(f"  ⚠️ Claude fejl, bruger standard skabelon: {e}")
                mail_html = f"""
                <p>Hej {navn},</p>
                <p>Vi har ikke hørt fra dig i et stykke tid, og vi tænkte på dig! 😊</p>
                <p>Du viste tidligere interesse for {ydelse} – er det stadig noget, du overvejer?</p>
                <p>Vi er klar til at hjælpe dig videre. Bare svar på denne mail eller book et møde direkte.</p>
                """
            html = f"""
            <div style="font-family:Arial,sans-serif;max-width:600px;margin:auto">
              <h2 style="color:#7c3aed">Hej {navn} 👋</h2>
              {mail_html}
              <p style="color:#888;font-size:12px;margin-top:32px">Med venlig hilsen, {klient_navn}</p>
            </div>
            """
            _gem_til_godkendelse(klient_id, lead_id, f"Vi tænker stadig på dig, {navn} 💜", html, 'genopvarmning', lead_id)
    except Exception as e:
        print(f"Genopvarmnings-agent fejl: {e}")

def _byg_uge_status_html(klient_navn, leads_uge, chat_uge, gaps_uge, portal_url, leads_forrige=0, chat_forrige=0):
    """Bygger professionel ugentlig statusmail med logo, grafer og sammenligning."""
    import datetime as _dt
    uge_nr = _dt.datetime.now().isocalendar()[1]
    dato_str = _dt.datetime.now().strftime('%d. %B %Y')
    ugedage = ['Man','Tir','Ons','Tor','Fre','Lør','Søn']

    # Trend-pile
    def trend(nu, før):
        if før == 0 and nu > 0: return '↑', '#16a34a'
        if nu > før: return f'↑ +{nu-før}', '#16a34a'
        if nu < før: return f'↓ {nu-før}', '#dc2626'
        return '→ uændret', '#6b7280'

    chat_trend, chat_trend_farve = trend(chat_uge, chat_forrige)
    lead_trend, lead_trend_farve = trend(leads_uge, leads_forrige)

    # CSS bar-graf — simulerer 7 dages aktivitet visuelt
    # Lav en simpel bar der repræsenterer denne uges niveau vs maks
    def bar_html(værdi, maks, farve, label):
        pct = min(int((værdi / maks) * 100), 100) if maks > 0 else 5
        pct = max(pct, 5)
        return f"""
        <td style="padding:0 8px;text-align:center;vertical-align:bottom">
          <div style="font-size:11px;font-weight:700;color:{farve};margin-bottom:4px">{værdi}</div>
          <div style="width:32px;background:#e5e7eb;border-radius:4px 4px 0 0;overflow:hidden;display:inline-block;vertical-align:bottom">
            <div style="width:32px;height:{pct}px;background:{farve};border-radius:4px 4px 0 0"></div>
          </div>
          <div style="font-size:10px;color:#9ca3af;margin-top:4px">{label}</div>
        </td>"""

    # Byg graf-rækken (forrige uge vs denne uge visuelt)
    maks_chat = max(chat_uge, chat_forrige, 1)
    maks_lead = max(leads_uge, leads_forrige, 1)
    chat_graf = f"""
    <table cellpadding="0" cellspacing="0" style="margin:0 auto">
      <tr style="vertical-align:bottom;height:60px">
        {bar_html(chat_forrige, maks_chat, '#cbd5e1', 'Forrige')}
        {bar_html(chat_uge, maks_chat, '#2563eb', 'Denne')}
      </tr>
    </table>"""
    lead_graf = f"""
    <table cellpadding="0" cellspacing="0" style="margin:0 auto">
      <tr style="vertical-align:bottom;height:60px">
        {bar_html(leads_forrige, maks_lead, '#cbd5e1', 'Forrige')}
        {bar_html(leads_uge, maks_lead, '#16a34a', 'Denne')}
      </tr>
    </table>"""

    # Gaps sektion
    gaps_html = ''
    if gaps_uge:
        gap_items = ''.join(f"""
        <tr>
          <td style="padding:10px 14px;border-bottom:1px solid #f3f4f6">
            <table width="100%" cellpadding="0" cellspacing="0"><tr>
              <td style="width:6px;padding-right:10px;vertical-align:top">
                <div style="width:6px;height:6px;background:#f59e0b;border-radius:50%;margin-top:5px"></div>
              </td>
              <td style="font-size:13px;color:#374151;line-height:1.5">{g}</td>
            </tr></table>
          </td>
        </tr>""" for g in gaps_uge[:3])
        gaps_html = f"""
  <tr><td style="padding:0 32px 24px">
    <div style="background:#fffbeb;border:1px solid #fde68a;border-radius:10px;overflow:hidden">
      <div style="padding:12px 14px;background:#fef3c7;border-bottom:1px solid #fde68a">
        <span style="font-size:13px;font-weight:700;color:#92400e">⚠️ Spørgsmål chatbotten ikke kunne besvare</span>
        <span style="font-size:11px;color:#b45309;margin-left:8px">Log ind og udfyld svarene</span>
      </div>
      <table width="100%" cellpadding="0" cellspacing="0">{gap_items}</table>
    </div>
  </td></tr>"""

    ingen = chat_uge == 0 and leads_uge == 0
    hero_tekst = "Stille uge — chatbotten venter på besøgende" if ingen else f"{chat_uge} samtaler og {leads_uge} nye leads denne uge"
    hero_sub = "Ingen aktivitet endnu — chatbotten er klar og aktiv." if ingen else "Dine AI-agenter har arbejdet for dig i baggrunden."

    return f"""<!DOCTYPE html>
<html lang="da">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Ugerapport uge {uge_nr}</title>
</head>
<body style="margin:0;padding:0;background:#eef2f7;font-family:'Helvetica Neue',Arial,sans-serif">
<table width="100%" cellpadding="0" cellspacing="0">
<tr><td align="center" style="padding:40px 16px">
<table width="580" cellpadding="0" cellspacing="0" style="max-width:580px">

  <!-- LOGO HEADER -->
  <tr><td style="padding-bottom:20px;text-align:center">
    <table cellpadding="0" cellspacing="0" style="margin:0 auto">
      <tr>
        <td style="background:#0a1a3a;border-radius:10px;padding:10px 20px">
          <span style="color:#fff;font-size:18px;font-weight:900;letter-spacing:-0.5px">NexOlsen</span>
          <span style="color:rgba(255,255,255,.4);font-size:11px;margin-left:8px;letter-spacing:1px;text-transform:uppercase">AI</span>
        </td>
      </tr>
    </table>
    <div style="font-size:12px;color:#9ca3af;margin-top:8px">Ugentlig AI-rapport · Uge {uge_nr} · {dato_str}</div>
  </td></tr>

  <!-- MAIN CARD -->
  <tr><td style="background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,0.08)">

    <!-- HERO -->
    <tr><td style="background:linear-gradient(135deg,#0a1a3a 0%,#1e3a6e 100%);padding:32px 36px">
      <div style="font-size:11px;color:rgba(255,255,255,.5);text-transform:uppercase;letter-spacing:2px;margin-bottom:10px">Til {klient_navn}</div>
      <div style="font-size:24px;font-weight:800;color:#fff;line-height:1.3;margin-bottom:8px">{hero_tekst}</div>
      <div style="font-size:14px;color:rgba(255,255,255,.6)">{hero_sub}</div>
    </td></tr>

    <!-- KPI KORT -->
    <tr><td style="padding:28px 32px 20px">
      <table width="100%" cellpadding="0" cellspacing="0">
        <tr>
          <!-- Chat -->
          <td width="47%" style="background:#f8faff;border:1px solid #e0e7ff;border-radius:12px;padding:20px;text-align:center">
            <div style="font-size:11px;color:#6366f1;font-weight:700;text-transform:uppercase;letter-spacing:1px;margin-bottom:12px">💬 Chatsamtaler</div>
            <div style="font-size:48px;font-weight:900;color:#1e40af;line-height:1">{chat_uge}</div>
            <div style="font-size:12px;color:{chat_trend_farve};font-weight:600;margin-top:6px">{chat_trend} vs forrige uge</div>
            <div style="margin-top:14px">{chat_graf}</div>
          </td>
          <td width="6%"></td>
          <!-- Leads -->
          <td width="47%" style="background:#f0fdf4;border:1px solid #bbf7d0;border-radius:12px;padding:20px;text-align:center">
            <div style="font-size:11px;color:#16a34a;font-weight:700;text-transform:uppercase;letter-spacing:1px;margin-bottom:12px">🎯 Nye leads</div>
            <div style="font-size:48px;font-weight:900;color:#15803d;line-height:1">{leads_uge}</div>
            <div style="font-size:12px;color:{lead_trend_farve};font-weight:600;margin-top:6px">{lead_trend} vs forrige uge</div>
            <div style="margin-top:14px">{lead_graf}</div>
          </td>
        </tr>
      </table>
    </td></tr>

    <!-- DIVIDER -->
    <tr><td style="padding:0 32px"><div style="height:1px;background:#f3f4f6"></div></td></tr>

    <!-- HVAD SKETE DER -->
    <tr><td style="padding:20px 32px">
      <table width="100%" cellpadding="0" cellspacing="0">
        <tr>
          <td width="30%" style="padding-right:16px;vertical-align:top">
            <div style="font-size:11px;color:#9ca3af;font-weight:600;text-transform:uppercase;letter-spacing:1px;margin-bottom:8px">Chatbot status</div>
            <div style="display:flex;align-items:center;gap:6px">
              <div style="width:8px;height:8px;background:#22c55e;border-radius:50%;display:inline-block"></div>
              <span style="font-size:13px;color:#374151;font-weight:600">Aktiv 24/7</span>
            </div>
            <div style="font-size:12px;color:#9ca3af;margin-top:4px">Svarer automatisk</div>
          </td>
          <td width="1%" style="background:#f3f4f6"></td>
          <td width="30%" style="padding:0 16px;vertical-align:top">
            <div style="font-size:11px;color:#9ca3af;font-weight:600;text-transform:uppercase;letter-spacing:1px;margin-bottom:8px">Lead-mails</div>
            <div style="font-size:13px;color:#374151;font-weight:600">{leads_uge * 3} mails</div>
            <div style="font-size:12px;color:#9ca3af;margin-top:4px">Sendt automatisk</div>
          </td>
          <td width="1%" style="background:#f3f4f6"></td>
          <td width="30%" style="padding-left:16px;vertical-align:top">
            <div style="font-size:11px;color:#9ca3af;font-weight:600;text-transform:uppercase;letter-spacing:1px;margin-bottom:8px">Konvertering</div>
            <div style="font-size:13px;color:#374151;font-weight:600">{'%.1f' % (leads_uge/chat_uge*100 if chat_uge > 0 else 0)}%</div>
            <div style="font-size:12px;color:#9ca3af;margin-top:4px">Samtaler → leads</div>
          </td>
        </tr>
      </table>
    </td></tr>

    {gaps_html}

    <!-- DIVIDER -->
    <tr><td style="padding:0 32px"><div style="height:1px;background:#f3f4f6"></div></td></tr>

    <!-- CTA -->
    <tr><td style="padding:28px 32px;text-align:center">
      <div style="font-size:15px;color:#374151;margin-bottom:16px">Se alle leads, samtaler og indsigter i din portal</div>
      <a href="{portal_url}" style="display:inline-block;background:linear-gradient(135deg,#0a1a3a,#1e3a6e);color:#fff;text-decoration:none;font-size:15px;font-weight:700;padding:14px 36px;border-radius:10px;letter-spacing:0.2px">Åbn din portal →</a>
    </td></tr>

  </td></tr>

  <!-- FOOTER -->
  <tr><td style="padding:20px 0;text-align:center">
    <div style="font-size:11px;color:#9ca3af;line-height:1.8">
      Denne rapport sendes automatisk hver mandag morgen<br>
      <strong style="color:#6b7280">NexOlsen AI</strong> · support@nexolsen.dk
    </div>
  </td></tr>

</table>
</td></tr>
</table>
</body></html>"""


def kør_ugerapport_agent():
    """📊 Sender kort ugentlig statusmail til alle aktive klienter (mandag morgen)"""
    if not db:
        return
    print("📊 Ugentlig statusrapport kører...")
    try:
        uge_nr = datetime.now().isocalendar()[1]
        uge_ref = f"uge-{uge_nr}-{datetime.now().year}"

        # Hent alle aktive klienter med email
        klienter_res = db.table('klienter').select('id, navn, email').eq('aktiv', True).execute()
        for k in (klienter_res.data or []):
            klient_id = k['id']
            klient_email = k.get('email', '')
            klient_navn = k.get('navn', klient_id)
            if not klient_email or '@' not in klient_email:
                continue
            if _allerede_sendt('ugerapport', f"{klient_id}-{uge_ref}"):
                continue

            # Tæl chat-sessions denne uge og forrige uge
            nu = datetime.utcnow()
            uge_start = (nu - timedelta(days=7)).isoformat()
            forrige_start = (nu - timedelta(days=14)).isoformat()

            chat_uge, chat_forrige = 0, 0
            try:
                cs = db.table('chat_sessions').select('id', count='exact').eq('klient_id', klient_id).gte('created_at', uge_start).execute()
                chat_uge = cs.count or 0
                cs2 = db.table('chat_sessions').select('id', count='exact').eq('klient_id', klient_id).gte('created_at', forrige_start).lt('created_at', uge_start).execute()
                chat_forrige = cs2.count or 0
            except: pass

            # Tæl leads denne uge og forrige uge
            leads_uge, leads_forrige = 0, 0
            try:
                lr = db.table('leads').select('id', count='exact').eq('klient_id', klient_id).gte('created_at', uge_start).execute()
                leads_uge = lr.count or 0
                lr2 = db.table('leads').select('id', count='exact').eq('klient_id', klient_id).gte('created_at', forrige_start).lt('created_at', uge_start).execute()
                leads_forrige = lr2.count or 0
            except: pass

            # Hent op til 3 ubesvarede gaps
            gaps_uge = []
            try:
                gr = db.table('chatbot_gaps').select('spoergsmaal').eq('klient_id', klient_id).eq('status', 'ubesvaret').order('oprettet', desc=True).limit(3).execute()
                gaps_uge = [g['spoergsmaal'] for g in (gr.data or [])]
            except: pass

            portal_url = f"https://klaiai.onrender.com/portal/{klient_id}"
            html = _byg_uge_status_html(klient_navn, leads_uge, chat_uge, gaps_uge, portal_url, leads_forrige=leads_forrige, chat_forrige=chat_forrige)

            sendt = send_mail(klient_email, f"Din ugerapport — {leads_uge} leads, {chat_uge} samtaler denne uge", '', klient_navn, html_content=html)
            if sendt:
                _log_agent('ugerapport', klient_id, f"{klient_id}-{uge_ref}", f"Ugerapport sendt til {klient_email}: {leads_uge} leads, {chat_uge} samtaler")
                print(f"  ✅ {klient_navn}: {leads_uge} leads, {chat_uge} samtaler → {klient_email}")
            else:
                print(f"  ❌ Kunne ikke sende til {klient_email}")
    except Exception as e:
        print(f"Ugerapport-agent fejl: {e}")

# Planlæg jobs
def kør_billing_agent():
    """Deaktiver kunder der har haft past_due status i over 7 dage"""
    if not db:
        return
    print("💳 Billing agent kører...")
    try:
        grænse = (datetime.utcnow() - timedelta(days=7)).isoformat()
        # Find klienter der er past_due og hvor updated_at er for mere end 7 dage siden
        res = db.table('klienter').select('id, navn, email, subscription_status, updated_at').eq('subscription_status', 'past_due').eq('aktiv', True).execute()
        if not res.data:
            return
        deaktiverede = 0
        for k in res.data:
            updated = k.get('updated_at', '')
            if not updated:
                continue
            # Konverter til datetime for sammenligning
            try:
                updated_dt = datetime.fromisoformat(updated.replace('Z', '+00:00').replace('+00:00', ''))
                if updated_dt > datetime.utcnow() - timedelta(days=7):
                    continue  # Ikke 7 dage endnu
            except:
                continue

            # Deaktiver konto
            db.table('klienter').update({
                'aktiv': False,
                'status': 'inaktiv',
                'subscription_status': 'canceled'
            }).eq('id', k['id']).execute()

            # Send email til kunden
            if k.get('email'):
                send_mail(
                    k['email'],
                    'Din NexOlsen konto er deaktiveret',
                    f"""<p>Hej {k.get('navn', '')},</p>
                    <p>Vi har desværre måttet deaktivere din NexOlsen konto da vi ikke har kunnet trække betaling i over 7 dage.</p>
                    <p>Hvis du ønsker at genaktivere din konto, kan du opdatere din betalingsmetode ved at kontakte os på
                    <a href="mailto:support@nexolsen.dk">support@nexolsen.dk</a>.</p>
                    <p>Med venlig hilsen,<br>NexOlsen</p>""",
                    'NexOlsen'
                )

            _log_agent('billing_agent', k['id'], k['id'], f"Konto deaktiveret efter 7 dage med manglende betaling")
            deaktiverede += 1

        print(f"💳 Billing agent færdig: {deaktiverede} konto(er) deaktiveret")
    except Exception as e:
        print(f"Billing agent fejl: {e}")


def kør_mail_flow_agent():
    """Sender automatiske mail-flow emails til nye leads baseret på gemte flows."""
    if not db:
        return
    try:
        # Hent alle aktive mail flows
        flows_res = db.table('mail_flows').select('*').eq('aktiv', True).execute()
        if not flows_res.data:
            return

        nu = datetime.utcnow()

        for flow in flows_res.data:
            klient_id = flow['klient_id']
            steps = flow.get('steps', [])
            if not steps:
                continue

            # Hent klientens leads (med email)
            leads_res = db.table('leads').select('*').eq('klient_id', klient_id).neq('email', '').execute()
            if not leads_res.data:
                continue

            # Hent klientens navn til afsender
            try:
                k_res = db.table('klienter').select('navn').eq('id', klient_id).single().execute()
                klient_navn = k_res.data.get('navn', 'NexOlsen') if k_res.data else 'NexOlsen'
            except:
                klient_navn = 'NexOlsen'

            for lead in leads_res.data:
                if not lead.get('email'):
                    continue
                lead_id = lead['id']
                # Tidspunkt for lead-oprettelse
                oprettet_str = lead.get('created_at') or lead.get('oprettet_at')
                if not oprettet_str:
                    continue
                try:
                    oprettet = datetime.fromisoformat(oprettet_str.replace('Z', '+00:00')).replace(tzinfo=None)
                except:
                    continue

                for i, step in enumerate(steps):
                    delay_timer = step.get('delay_timer', 0)
                    send_tidspunkt = oprettet + timedelta(hours=delay_timer)

                    # Endnu ikke tid til denne mail
                    if nu < send_tidspunkt:
                        continue

                    agent_key = f"mail_flow_{flow['id']}_step_{i}"
                    ref_key = f"{lead_id}"

                    if _allerede_sendt(agent_key, ref_key):
                        continue

                    # Erstat {navn} i emne og tekst
                    lead_navn = lead.get('navn', 'dig')
                    emne = step.get('emne', '').replace('{navn}', lead_navn)
                    tekst = step.get('tekst', '').replace('{navn}', lead_navn)

                    sendt = send_mail(lead['email'], emne, tekst, klient_navn)
                    if sendt:
                        _log_agent(agent_key, klient_id, ref_key, f"Mail sendt til {lead['email']}: {emne}")
                        print(f"📧 Mail flow: {emne} → {lead['email']}")

        print(f"📧 Mail flow agent færdig: {nu.strftime('%H:%M')}")
    except Exception as e:
        print(f"Mail flow agent fejl: {e}")


# ── MAIL FLOW ENDPOINTS ────────────────────────────────
@app.route('/mail-flow/<klient_id>', methods=['POST'])
@require_auth
def gem_mail_flow(klient_id):
    """Gemmer et mail flow for en klient"""
    data = request.json
    steps = data.get('steps', [])
    flow_type = data.get('flow_type', 'custom')
    if not steps:
        return jsonify({'error': 'Ingen steps'}), 400
    if not db:
        return jsonify({'error': 'Ingen database'}), 500
    try:
        # Slet evt. eksisterende flow for denne klient
        db.table('mail_flows').delete().eq('klient_id', klient_id).execute()
        # Gem nyt flow
        res = db.table('mail_flows').insert({
            'klient_id': klient_id,
            'flow_type': flow_type,
            'steps': steps,
            'aktiv': True
        }).execute()
        return jsonify({'success': True, 'id': res.data[0]['id'] if res.data else None})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/mail-flow/<klient_id>', methods=['GET'])
@require_auth
def hent_mail_flow(klient_id):
    """Henter aktivt mail flow for en klient"""
    if not db:
        return jsonify({}), 200
    try:
        res = db.table('mail_flows').select('*').eq('klient_id', klient_id).eq('aktiv', True).maybe_single().execute()
        return jsonify(res.data or {})
    except Exception as e:
        return jsonify({}), 200

@app.route('/mail-flow/<klient_id>', methods=['DELETE'])
@require_auth
def slet_mail_flow(klient_id):
    """Deaktiverer mail flow for en klient"""
    if not db:
        return jsonify({'error': 'Ingen database'}), 500
    try:
        db.table('mail_flows').delete().eq('klient_id', klient_id).execute()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/klient-cockpit/<klient_id>', methods=['GET'])
@require_auth
def klient_cockpit(klient_id):
    """Samler al data om én klient til cockpit-visningen."""
    if not db:
        return jsonify({'error': 'Ingen database'}), 500
    try:
        nu = datetime.utcnow()
        syv_dage_siden = nu - timedelta(days=7)

        # ── Leads ──
        leads_res = db.table('leads').select('*').eq('klient_id', klient_id).order('created_at', desc=True).execute()
        leads = leads_res.data or []

        # Leads per dag (sidste 7 dage) — sammenlign kun dato-delen (YYYY-MM-DD)
        leads_per_dag = {}
        for i in range(7):
            dag = (nu - timedelta(days=6-i)).strftime('%Y-%m-%d')
            leads_per_dag[dag] = 0
        denne_uge_count = 0
        for lead in leads:
            ts = lead.get('created_at') or lead.get('oprettet') or ''
            if ts:
                dag = ts[:10]  # tag kun dato-delen, ignorer tidszone
                if dag in leads_per_dag:
                    leads_per_dag[dag] += 1
                    denne_uge_count += 1

        # ── Bookinger ──
        book_res = db.table('bookinger').select('*').eq('klient_id', klient_id).order('created_at', desc=True).limit(20).execute()
        bookinger = book_res.data or []
        book_i_dag = sum(1 for b in bookinger if b.get('dato') == nu.strftime('%Y-%m-%d'))

        # ── Mail flow ──
        flow_res = db.table('mail_flows').select('*').eq('klient_id', klient_id).eq('aktiv', True).maybe_single().execute()
        mail_flow = flow_res.data if flow_res else None

        # Mail flow status per lead (hvilke steps er sendt)
        lead_flow_status = []
        if mail_flow and leads:
            steps = mail_flow.get('steps', [])
            for lead in leads[:10]:  # top 10 leads
                if not lead.get('email'):
                    continue
                sendt = []
                for i, step in enumerate(steps):
                    agent_key = f"mail_flow_{mail_flow['id']}_step_{i}"
                    log_res = db.table('agent_log').select('id').eq('agent', agent_key).eq('reference_id', str(lead['id'])).execute()
                    sendt.append(len(log_res.data or []) > 0)
                lead_flow_status.append({
                    'navn': lead.get('navn', 'Ukendt'),
                    'email': lead.get('email', ''),
                    'steps_sendt': sendt,
                    'total_steps': len(steps)
                })

        # ── Chat sessions ──
        chat_sessions_count = 0
        chat_sessions_uge = 0
        try:
            cs_res = db.table('chat_sessions').select('created_at', count='exact').eq('klient_id', klient_id).execute()
            chat_sessions_count = cs_res.count or 0
            uge_grænse = syv_dage_siden.isoformat()
            cs_uge_res = db.table('chat_sessions').select('id', count='exact').eq('klient_id', klient_id).gte('created_at', uge_grænse).execute()
            chat_sessions_uge = cs_uge_res.count or 0
        except:
            pass

        # ── Agent log ──
        log_res = db.table('agent_log').select('*').eq('klient_id', klient_id).order('created_at', desc=True).limit(25).execute()
        agent_log = log_res.data or []

        # ── Chatbot config ──
        chatbot = get_klient(klient_id)
        chatbot_info = {
            'navn': chatbot.get('chatbot_navn', 'Alma'),
            'farve': chatbot.get('farve', '#0a2463'),
            'ydelser': chatbot.get('info', {}).get('ydelser', ''),
            'ekstra_viden_tegn': len(chatbot.get('ekstra_viden', '')),
        }

        return jsonify({
            'leads': {
                'total': len(leads),
                'denne_uge': denne_uge_count,
                'per_dag': leads_per_dag,
                'seneste': leads[:5]
            },
            'bookinger': {
                'total': len(bookinger),
                'i_dag': book_i_dag,
                'seneste': bookinger[:5]
            },
            'mail_flow': {
                'aktiv': bool(mail_flow),
                'flow_type': mail_flow.get('flow_type', '') if mail_flow else '',
                'antal_steps': len(mail_flow.get('steps', [])) if mail_flow else 0,
                'lead_status': lead_flow_status
            },
            'agent_log': agent_log,
            'chatbot': chatbot_info,
            'chat_sessions': {
                'total': chat_sessions_count,
                'denne_uge': chat_sessions_uge
            },
            'hentet': nu.isoformat()
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def kør_ubesvarede_leads_reminder():
    """Send reminder til Mattis hvis leads har ligget i 'ny' i 24+ timer"""
    if not db or not ADMIN_EMAIL or '@' not in ADMIN_EMAIL:
        return
    try:
        from datetime import datetime, timezone, timedelta
        grænse = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

        # Hent leads der er 'ny' og over 24 timer gamle
        res = db.table('leads').select('id, navn, email, klient_id, oprettet').eq('status', 'ny').lt('oprettet', grænse).order('oprettet').execute()
        leads = res.data or []

        if not leads:
            return

        # Gruppér per klient
        klient_res = db.table('klienter').select('id, navn').execute()
        klient_map = {k['id']: k['navn'] for k in (klient_res.data or [])}

        from collections import defaultdict
        per_klient = defaultdict(list)
        for l in leads:
            per_klient[l['klient_id']].append(l)

        linjer = []
        for kid, kl in per_klient.items():
            kl_navn = klient_map.get(kid, kid)
            ældst = kl[0]['oprettet']
            try:
                ts = datetime.fromisoformat(ældst.replace('Z', '+00:00'))
                timer = int((datetime.now(timezone.utc) - ts).total_seconds() / 3600)
            except Exception:
                timer = '?'
            linjer.append(f"• {kl_navn}: {len(kl)} ubesvarede lead{'s' if len(kl)>1 else ''} (ældste: {timer} timer)")

        emne = f"[NexOlsen] ⚠️ {len(leads)} ubesvarede leads afventer svar"
        tekst = f"""Hej Mattis,

Du har leads der har ligget i 'Ny' i over 24 timer uden at blive kontaktet:

{chr(10).join(linjer)}

Gå ind i CRM-panelet og følg op:
https://klaiai.onrender.com/app/admin.html

Mvh NexOlsen
"""
        send_mail(ADMIN_EMAIL, emne, tekst, 'NexOlsen')
        print(f"✅ Ubesvarede leads reminder sendt: {len(leads)} leads")
    except Exception as e:
        print(f"❌ Fejl i ubesvarede_leads_reminder: {e}")


def kør_månedlig_rapport():
    """Send månedlig rapport til alle aktive klienter den 1. i måneden"""
    if not db:
        return
    try:
        from datetime import datetime, timezone, timedelta
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import Mail

        if not SENDGRID_API_KEY or not SENDGRID_FROM:
            print("⚠️ månedlig_rapport: SendGrid ikke konfigureret")
            return

        klienter_res = db.table('klienter').select('*').eq('aktiv', True).execute()
        klienter = klienter_res.data or []

        now = datetime.now(timezone.utc)
        mdr_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        sendt = 0
        for k in klienter:
            email = k.get('email', '')
            if not email or '@' not in email:
                continue
            try:
                leads, bookinger, gaps, chat_count = _hent_rapport_data(k['id'])
                html = _byg_rapport_html(k['id'], k.get('navn',''), leads, bookinger, gaps=gaps, chat_count=chat_count, maaned=mdr_start)
                mdr_navn = mdr_start.strftime('%B %Y')
                message = Mail(
                    from_email=(SENDGRID_FROM, 'NexOlsen'),
                    to_emails=email,
                    subject=f"Din NexOlsen rapport — {mdr_navn}",
                    html_content=html
                )
                sg = SendGridAPIClient(SENDGRID_API_KEY)
                sg.send(message)
                sendt += 1
            except Exception as e:
                print(f"❌ rapport fejl for {k.get('navn')}: {e}")

        print(f"✅ Månedlig rapport sendt til {sendt} klienter")
    except Exception as e:
        print(f"❌ Fejl i månedlig_rapport: {e}")


def kør_anmeldelse_agent():
    """Send anmeldelses-email til lukkede leads de seneste 3 dage for klienter med google_place_id"""
    if not db or not SENDGRID_API_KEY:
        return
    try:
        from datetime import datetime, timezone, timedelta
        grænse = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()

        # Hent klienter med google_place_id
        klienter_res = db.table('klienter').select('id, navn, google_place_id').neq('google_place_id', '').execute()
        klienter = [k for k in (klienter_res.data or []) if k.get('google_place_id')]

        if not klienter:
            return

        sendt = 0
        for k in klienter:
            kid = k['id']
            klient_navn = k.get('navn', 'Virksomheden')
            place_id = k.get('google_place_id', '')

            try:
                leads_res = db.table('leads').select('id, navn, email, noter, lukket_dato').eq('klient_id', kid).eq('status', 'lukket').gt('oprettet', grænse).execute()
                leads = leads_res.data or []
            except Exception as e:
                print(f"❌ anmeldelse_agent leads fejl for {klient_navn}: {e}")
                continue

            for l in leads:
                noter = l.get('noter') or ''
                if '[anmeldelse_sendt]' in noter:
                    continue
                lead_email = l.get('email', '')
                if not lead_email or '@' not in lead_email:
                    continue
                lead_navn = l.get('navn', 'kunde')
                review_link = f'https://search.google.com/local/writereview?placeid={place_id}'
                emne = f"Var du tilfreds med {klient_navn}? 🌟"
                tekst = f"""Hej {lead_navn},

Tak fordi du valgte {klient_navn}!

Vi håber, at du var tilfreds med vores service. Har du et øjeblik til at dele din oplevelse?

Det ville betyde enormt meget for os, hvis du ville skrive en kort anmeldelse her:
{review_link}

Det tager kun 1-2 minutter og hjælper andre med at finde os.

Mange tak!
Mvh {klient_navn}"""
                try:
                    sendt_ok = send_mail(lead_email, emne, tekst, klient_navn)
                    if sendt_ok:
                        # Marker lead med [anmeldelse_sendt] i noter
                        ny_noter = noter + ' [anmeldelse_sendt]'
                        db.table('leads').update({'noter': ny_noter}).eq('id', l['id']).execute()
                        sendt += 1
                except Exception as e:
                    print(f"❌ anmeldelse mail fejl for {lead_navn}: {e}")

        print(f"✅ Anmeldelse-emails sendt: {sendt}")
    except Exception as e:
        print(f"❌ Fejl i anmeldelse_agent: {e}")


def kør_tilbud_followup():
    """Sender automatisk opfølgning på tilbud der ikke er besvaret inden 5 dage"""
    if not db:
        return
    from datetime import datetime, timedelta
    deadline = (datetime.now() - timedelta(days=5)).isoformat()
    try:
        res = db.table('tilbud').select('*').eq('status', 'sendt').eq('followup_sendt', False).lt('sendt_dato', deadline).execute()
    except Exception as e:
        print(f'Tilbud followup DB fejl: {e}')
        return

    for t in (res.data or []):
        try:
            k = db.table('klienter').select('navn,hjemmeside').eq('id', t['klient_id']).single().execute()
            klient = k.data or {}
            fra_navn = klient.get('navn', 'Virksomheden')
            kunde_navn  = t.get('kunde_navn', '')
            kunde_email = t.get('kunde_email', '')
            titel       = t.get('titel', 'Tilbud')
            accept_token = t.get('accept_token', '')
            if not kunde_email or '@' not in kunde_email:
                continue

            accept_url = f'{SERVER_URL}/tilbud/godkend/{t["id"]}/{accept_token}' if accept_token else ''
            accept_knap = (
                f'<div style="text-align:center;margin:24px 0">'
                f'<a href="{accept_url}" style="display:inline-block;background:#16a34a;color:#fff;text-decoration:none;font-size:14px;font-weight:700;padding:12px 32px;border-radius:8px">✓ Godkend tilbud</a>'
                f'</div>'
            ) if accept_url else ''

            followup_html = f"""<!DOCTYPE html>
<html lang="da"><head><meta charset="UTF-8"/></head>
<body style="margin:0;padding:0;background:#f7f8fc;font-family:'Helvetica Neue',Arial,sans-serif">
<table width="100%" cellpadding="0" cellspacing="0" style="padding:40px 20px"><tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 4px 20px rgba(0,0,0,.08)">
  <tr><td style="background:#0a1a3a;padding:22px 36px"><div style="font-size:17px;font-weight:700;color:#fff">{fra_navn}</div></td></tr>
  <tr><td style="padding:32px 36px">
    <div style="font-size:21px;font-weight:700;color:#111;margin-bottom:14px">Hej {kunde_navn.split()[0] if kunde_navn else 'der'},</div>
    <div style="font-size:14px;color:#374151;line-height:1.8;margin-bottom:16px">Vi ville blot følge op på tilbuddet vi sendte dig vedrørende <strong>{titel}</strong>.</div>
    <div style="font-size:14px;color:#374151;line-height:1.8;margin-bottom:16px">Har du haft mulighed for at kigge på det? Vi er klar til at svare på spørgsmål eller justere tilbuddet efter dine ønsker.</div>
    {accept_knap}
  </td></tr>
  <tr><td style="background:#f9fafb;padding:18px 36px;text-align:center;border-top:1px solid #f0f0f0">
    <div style="font-size:12px;color:#9ca3af">{fra_navn} · Vi ser frem til at høre fra dig</div>
  </td></tr>
</table>
</td></tr></table>
</body></html>"""

            send_mail(kunde_email, f'Opfølgning: {titel}', f'Hej {kunde_navn},\n\nVi ville følge op på tilbuddet vi sendte dig.', fra_navn=fra_navn, html_content=followup_html)
            db.table('tilbud').update({'followup_sendt': True, 'followup_dato': datetime.now().isoformat()}).eq('id', t['id']).execute()
            print(f'Tilbud followup sendt til {kunde_email}')
        except Exception as e:
            print(f'Tilbud followup fejl ({t.get("id")}): {e}')


scheduler.add_job(kør_månedlig_rapport, 'cron', day=1, hour=8, minute=0, id='månedlig_rapport')
scheduler.add_job(kør_ubesvarede_leads_reminder, 'cron', hour=9, minute=30, id='ubesvarede_leads')
scheduler.add_job(kør_ubesvarede_leads_reminder, 'cron', hour=17, minute=0, id='ubesvarede_leads_aften')
scheduler.add_job(kør_reminder_agent,   'cron', hour=9,  minute=0, id='reminder')
scheduler.add_job(kør_review_agent,     'cron', hour=10, minute=0, id='review')
scheduler.add_job(kør_genopvarmning_agent, 'cron', hour=11, minute=0, id='genopvarmning')
scheduler.add_job(kør_ugerapport_agent, 'cron', hour=7,  minute=0, id='ugerapport', day_of_week='mon')
scheduler.add_job(kør_billing_agent,    'cron', hour=8,  minute=0, id='billing')
scheduler.add_job(kør_tilbud_followup, 'cron', hour=10, minute=15, id='tilbud_followup')
scheduler.add_job(kør_mail_flow_agent,  'interval', hours=1, id='mail_flow')
scheduler.add_job(kør_anmeldelse_agent, 'cron', hour=10, minute=30, id='anmeldelse')
scheduler.start()
print("⏰ APScheduler startet med 6 agenter")

# ── Agent endpoints ────────────────────────────────────────────

@app.route('/kør-agent/<navn>', methods=['POST'])
@require_admin
def kør_agent_manuelt(navn):
    """Manuel trigger af en agent fra admin-panelet"""
    agenter = {
        'reminder': kør_reminder_agent,
        'review': kør_review_agent,
        'genopvarmning': kør_genopvarmning_agent,
        'ugerapport': kør_ugerapport_agent,
        'billing': kør_billing_agent,
        'ubesvarede_leads': kør_ubesvarede_leads_reminder,
        'månedlig_rapport': kør_månedlig_rapport,
    }
    agentnavne = {
        'reminder': 'Påmindelsesagent',
        'review': 'Review-agent',
        'genopvarmning': 'Genopvarmningsagent',
        'ugerapport': 'Ugentlig rapport-agent',
        'billing': 'Billing-agent',
        'ubesvarede_leads': 'Ubesvarede leads-agent',
        'månedlig_rapport': 'Månedlig rapport-agent',
    }
    if navn not in agenter:
        return jsonify({'error': f'Ukendt agent: {navn}'}), 400
    try:
        agenter[navn]()
        # Hent seneste log-entry for denne agent
        besked = f'{agentnavne.get(navn, navn)} kørt'
        if db:
            try:
                log = db.table('agent_log').select('besked').eq('agent', navn).order('created_at', desc=True).limit(1).execute()
                if log.data:
                    besked = log.data[0].get('besked', besked)
            except:
                pass
        return jsonify({'ok': True, 'besked': besked})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/agent-log', methods=['GET'])
@require_admin
def hent_agent_log():
    """Hent seneste agent-kørsler"""
    klient_id = request.args.get('klient_id')
    limit = int(request.args.get('limit', 50))
    if not db:
        return jsonify([])
    try:
        q = db.table('agent_log').select('*').order('created_at', desc=True).limit(limit)
        if klient_id:
            q = q.eq('klient_id', klient_id)
        res = q.execute()
        return jsonify(res.data)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/crm/leads', methods=['GET'])
@require_admin
def crm_leads():
    """Hent alle leads med klientnavn til CRM"""
    if not db:
        return jsonify([]), 200
    try:
        klient_id = request.args.get('klient_id')
        q = db.table('leads').select('id, klient_id, navn, email, telefon, besked, status, noter, oprettet').order('oprettet', desc=True)
        if klient_id:
            q = q.eq('klient_id', klient_id)
        leads = q.execute().data or []

        # Hent klientnavne
        klient_res = db.table('klienter').select('id, navn').execute()
        klient_map = {k['id']: k['navn'] for k in (klient_res.data or [])}

        for l in leads:
            l['klient_navn'] = klient_map.get(l.get('klient_id'), 'Ukendt')
            l['status'] = l.get('status') or 'ny'
            l['noter'] = l.get('noter') or ''

        return jsonify(leads)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/crm/lead/<lead_id>', methods=['PATCH'])
@require_admin
def crm_opdater_lead(lead_id):
    """Opdater status og/eller noter på et lead"""
    if not db:
        return jsonify({'error': 'Ingen database'}), 500
    try:
        data = request.get_json() or {}
        update = {}
        if 'status' in data:
            update['status'] = data['status']
        if 'noter' in data:
            update['noter'] = data['noter']
        if not update:
            return jsonify({'error': 'Ingen data'}), 400
        db.table('leads').update(update).eq('id', lead_id).execute()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ══════════════════════════════════════════════════════════════
#  TILBUDS-AI
# ══════════════════════════════════════════════════════════════

def _byg_tilbud_html(klient_navn, klient_hjemmeside, kunde_navn, kunde_email,
                      titel, intro, linjer, betingelser, win_temaer,
                      konkurrent_opsummering, rabat=0, tema='standard', primær_farve='#0a1a3a',
                      kunde_adresse='', kunde_postnummer='', forbehold=''):
    """Genererer et professionelt HTML-tilbud — understøtter 3 temaer: standard, eksklusiv, professionel"""
    from datetime import datetime, timedelta
    dato       = datetime.now().strftime('%-d. %B %Y')
    gyldigt_til = (datetime.now() + timedelta(days=14)).strftime('%-d. %B %Y')

    rabat    = float(rabat or 0)
    primær_farve = primær_farve or '#0a1a3a'

    # Mixed VAT calculation
    sum_inkl    = sum(l.get('total', 0) for l in linjer if l.get('moms_inkluderet', False))
    sum_ekskl   = sum(l.get('total', 0) for l in linjer if not l.get('moms_inkluderet', False))
    has_inkl    = any(l.get('moms_inkluderet', False) for l in linjer)
    moms_beloeb = round(sum_ekskl * 0.25, 2)

    if has_inkl:
        total_foer_rabat = sum_inkl + sum_ekskl + moms_beloeb
        subtotal         = sum_inkl + sum_ekskl  # for rabat display
    else:
        subtotal         = sum_ekskl
        total_foer_rabat = subtotal  # moms shown separately in template
        moms_beloeb      = round(subtotal * 0.25, 2)

    total = max(0, total_foer_rabat - rabat)

    # ── FÆLLES: win-temaer og konkurrent-info ──────────────
    if win_temaer:
        punkter = ''.join(f'<li style="margin-bottom:6px">{w}</li>' for w in win_temaer)
        win_html = (
            '<div style="background:#f0f4ff;border-radius:10px;padding:20px 24px;margin-bottom:28px">'
            '<div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:1px;color:#3b4eb8;margin-bottom:10px">Hvorfor vælge os</div>'
            f'<ul style="margin:0;padding-left:18px;font-size:13px;color:#374151;line-height:1.7">{punkter}</ul></div>'
        )
    else:
        win_html = ''

    konkurrent_html = (
        '<div style="background:#fff8ed;border-left:3px solid #f59e0b;border-radius:0 10px 10px 0;padding:16px 20px;margin-bottom:28px">'
        '<div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:1px;color:#b45309;margin-bottom:8px">Markedsindsigt</div>'
        f'<div style="font-size:12px;color:#78350f;line-height:1.6">{konkurrent_opsummering}</div></div>'
    ) if konkurrent_opsummering else ''

    # ══════════════════════════════════════════════════════
    # TEMA: EKSKLUSIV — sort/hvid, minimalistisk
    # ══════════════════════════════════════════════════════
    if tema == 'eksklusiv':
        rækker = ''
        for l in linjer:
            rækker += (
                '<tr>'
                f'<td style="padding:13px 0;border-bottom:1px solid #f0f0f0;font-size:13px;color:#1a1a1a;font-weight:300">{l.get("beskrivelse","")} <span style="font-size:10px;color:#bbb;font-weight:400">{"inkl. moms" if l.get("moms_inkluderet") else "ekskl. moms"}</span></td>'
                f'<td style="padding:13px 0;border-bottom:1px solid #f0f0f0;font-size:13px;color:#999;text-align:center;white-space:nowrap">{l.get("antal","1")} {l.get("enhed","stk")}</td>'
                f'<td style="padding:13px 0;border-bottom:1px solid #f0f0f0;font-size:13px;color:#999;text-align:right;white-space:nowrap">{int(l.get("enhedspris",0)):,} kr.</td>'
                f'<td style="padding:13px 0;border-bottom:1px solid #f0f0f0;font-size:13px;font-weight:700;color:#111;text-align:right;white-space:nowrap">{int(l.get("total",0)):,} kr.</td>'
                '</tr>'
            )
        rabat_rækker = ''
        if rabat > 0:
            rabat_rækker = (
                f'<tr><td colspan="3" style="padding:8px 0;font-size:11px;color:#bbb;text-align:right">Subtotal</td>'
                f'<td style="padding:8px 0;font-size:11px;color:#bbb;text-align:right">{int(subtotal):,} kr.</td></tr>'
                f'<tr><td colspan="3" style="padding:6px 0;font-size:12px;color:#16a34a;font-weight:700;text-align:right">Rabat</td>'
                f'<td style="padding:6px 0;font-size:12px;color:#16a34a;font-weight:700;text-align:right">&#8722;{int(rabat):,} kr.</td></tr>'
            )

        if has_inkl:
            ekskl_total_rækker = (
                (f'<tr><td colspan="3" style="padding:8px 0;font-size:11px;color:#bbb;text-align:right">Varer (inkl. moms)</td><td style="padding:8px 0;font-size:11px;color:#bbb;text-align:right">{int(sum_inkl):,} kr.</td></tr>' if sum_inkl > 0 else '') +
                (f'<tr><td colspan="3" style="padding:8px 0;font-size:11px;color:#bbb;text-align:right">Arbejde/kørsel (ekskl. moms)</td><td style="padding:8px 0;font-size:11px;color:#bbb;text-align:right">{int(sum_ekskl):,} kr.</td></tr>' if sum_ekskl > 0 else '') +
                (f'<tr><td colspan="3" style="padding:8px 0;font-size:11px;color:#bbb;text-align:right">Moms 25% af ydelser</td><td style="padding:8px 0;font-size:11px;color:#bbb;text-align:right">{int(moms_beloeb):,} kr.</td></tr>' if moms_beloeb > 0 else '') +
                rabat_rækker
            )
        else:
            ekskl_total_rækker = (
                f'<tr><td colspan="3" style="padding:8px 0;font-size:11px;color:#bbb;text-align:right">Moms 25%</td><td style="padding:8px 0;font-size:11px;color:#bbb;text-align:right">{int(moms_beloeb):,} kr.</td></tr>'
                + rabat_rækker
            )

        pris_tabel_html = f"""<div style="margin-bottom:40px">
<div style="font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:2.5px;color:#bbb;margin-bottom:16px;padding-bottom:12px;border-bottom:2px solid #111">Indhold &amp; priser</div>
<table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse">
  <tr>
    <th style="padding:0 0 12px;font-size:9px;font-weight:700;color:#bbb;text-transform:uppercase;letter-spacing:1.5px;text-align:left">Ydelse</th>
    <th style="padding:0 0 12px;font-size:9px;font-weight:700;color:#bbb;text-transform:uppercase;letter-spacing:1.5px;text-align:center">Antal</th>
    <th style="padding:0 0 12px;font-size:9px;font-weight:700;color:#bbb;text-transform:uppercase;letter-spacing:1.5px;text-align:right">Enhedspris</th>
    <th style="padding:0 0 12px;font-size:9px;font-weight:700;color:#bbb;text-transform:uppercase;letter-spacing:1.5px;text-align:right">Total</th>
  </tr>
  {rækker}
  {ekskl_total_rækker}
  <tr style="border-top:2px solid #111">
    <td colspan="3" style="padding:18px 0 6px;font-size:10px;font-weight:300;color:#aaa;text-align:right;text-transform:uppercase;letter-spacing:1.5px">Total inkl. moms</td>
    <td style="padding:18px 0 6px;font-size:22px;font-weight:900;color:#111;text-align:right;line-height:1">{int(total):,} kr.</td>
  </tr>
</table>
</div>"""

        return f"""<!DOCTYPE html>
<html lang="da"><head><meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Tilbud — {titel}</title></head>
<body style="margin:0;padding:0;background:#f5f5f5;font-family:'Helvetica Neue',Helvetica,Arial,sans-serif">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f5f5;padding:40px 20px">
<tr><td align="center">
<table width="680" cellpadding="0" cellspacing="0" style="max-width:680px">

  <!-- HEADER: solid black -->
  <tr><td style="background:#111;border-radius:16px 16px 0 0;padding:40px 44px">
    <table width="100%" cellpadding="0" cellspacing="0"><tr>
      <td>
        <div style="font-size:11px;font-weight:900;color:#fff;letter-spacing:4px;text-transform:uppercase">{klient_navn}</div>
        <div style="font-size:11px;color:rgba(255,255,255,.3);margin-top:4px;letter-spacing:1px">{klient_hjemmeside}</div>
      </td>
      <td style="text-align:right;vertical-align:top">
        <div style="font-size:9px;color:rgba(255,255,255,.35);text-transform:uppercase;letter-spacing:2px">Tilbud · {dato}</div>
        <div style="font-size:9px;color:rgba(255,255,255,.25);margin-top:5px;letter-spacing:1px">Gyldigt til {gyldigt_til}</div>
      </td>
    </tr></table>
    <div style="margin-top:36px;padding-top:36px;border-top:1px solid rgba(255,255,255,.1)">
      <div style="font-size:26px;font-weight:300;color:#fff;letter-spacing:.5px;line-height:1.3">{titel}</div>
      <div style="font-size:12px;color:rgba(255,255,255,.35);margin-top:10px;letter-spacing:.5px">Til: {kunde_navn} &middot; {kunde_email}{(' &middot; ' + kunde_adresse + (', ' + kunde_postnummer if kunde_postnummer else '')) if kunde_adresse else ''}</div>
    </div>
  </td></tr>

  <!-- BODY -->
  <tr><td style="background:#fff;padding:44px">
    <div style="font-size:14px;color:#333;line-height:1.9;margin-bottom:36px;font-weight:300">{intro}</div>
    {win_html}

    <!-- Prisliste -->
    {pris_tabel_html}

    {konkurrent_html}

    {'<div style="background:#fffbf0;border-left:2px solid #d97706;border-radius:0 8px 8px 0;padding:16px 20px;margin-bottom:28px"><div style="font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:2px;color:#92400e;margin-bottom:8px">Forbehold</div><div style="font-size:12px;color:#78350f;line-height:1.7;font-weight:300">' + forbehold.replace(chr(10),'<br>') + '</div></div>' if forbehold else ''}

    <div style="border-top:1px solid #f0f0f0;padding-top:24px;margin-bottom:32px">
      <div style="font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:2px;color:#bbb;margin-bottom:10px">Betingelser</div>
      <div style="font-size:12px;color:#888;line-height:1.7;font-weight:300">{betingelser}</div>
    </div>

    <div style="text-align:center;padding:8px 0">
      <div style="font-size:13px;color:#888;margin-bottom:8px;font-weight:300">Spørgsmål? Vi er klar til at hjælpe.</div>
      <div style="font-size:11px;font-weight:900;color:#111;letter-spacing:2px;text-transform:uppercase">{klient_navn}</div>
    </div>
  </td></tr>

  <!-- FOOTER -->
  <tr><td style="background:#111;border-radius:0 0 16px 16px;padding:18px 44px;text-align:center">
    <div style="font-size:10px;color:rgba(255,255,255,.25);letter-spacing:1px">Tilbud genereret af NexOlsen AI &middot; Gyldigt i 14 dage</div>
  </td></tr>

</table></td></tr></table>
</body></html>"""

    # ══════════════════════════════════════════════════════
    # TEMA: STANDARD + PROFESSIONEL (fælles skabelon)
    # professionel bruger primær_farve, standard bruger navy
    # ══════════════════════════════════════════════════════
    header_bg = (
        f'background:{primær_farve}' if tema == 'professionel'
        else 'background:linear-gradient(135deg,#0a1a3a 0%,#1e3a6e 100%)'
    )
    accent = primær_farve if tema == 'professionel' else '#0a1a3a'

    rækker = ''
    for l in linjer:
        rækker += (
            '<tr>'
            f'<td style="padding:12px 16px;border-bottom:1px solid #f0f0f0;font-size:13px;color:#1a1a2e">{l.get("beskrivelse","")} <span style="font-size:10px;color:#9ca3af">{"inkl. moms" if l.get("moms_inkluderet") else "ekskl. moms"}</span></td>'
            f'<td style="padding:12px 16px;border-bottom:1px solid #f0f0f0;font-size:13px;color:#6b7280;text-align:center">{l.get("antal","1")} {l.get("enhed","stk")}</td>'
            f'<td style="padding:12px 16px;border-bottom:1px solid #f0f0f0;font-size:13px;color:#6b7280;text-align:right">{int(l.get("enhedspris",0)):,} kr.</td>'
            f'<td style="padding:12px 16px;border-bottom:1px solid #f0f0f0;font-size:13px;font-weight:600;color:#1a1a2e;text-align:right">{int(l.get("total",0)):,} kr.</td>'
            '</tr>'
        )
    rabat_rækker = ''
    if rabat > 0:
        rabat_rækker = (
            f'<tr><td colspan="3" style="padding:8px 16px;font-size:12px;color:#9ca3af;text-align:right">Subtotal</td>'
            f'<td style="padding:8px 16px;font-size:12px;color:#9ca3af;text-align:right">{int(subtotal):,} kr.</td></tr>'
            f'<tr><td colspan="3" style="padding:8px 16px;font-size:13px;color:#16a34a;font-weight:700;text-align:right">Rabat</td>'
            f'<td style="padding:8px 16px;font-size:13px;color:#16a34a;font-weight:700;text-align:right">&#8722;{int(rabat):,} kr.</td></tr>'
        )

    if has_inkl:
        std_total_rækker = (
            (f'<tr style="background:#f9fafb"><td colspan="3" style="padding:10px 16px;font-size:12px;color:#9ca3af;text-align:right">Varer (inkl. moms)</td><td style="padding:10px 16px;font-size:12px;color:#9ca3af;text-align:right">{int(sum_inkl):,} kr.</td></tr>' if sum_inkl > 0 else '') +
            (f'<tr><td colspan="3" style="padding:10px 16px;font-size:12px;color:#9ca3af;text-align:right">Arbejde/kørsel (ekskl. moms)</td><td style="padding:10px 16px;font-size:12px;color:#9ca3af;text-align:right">{int(sum_ekskl):,} kr.</td></tr>' if sum_ekskl > 0 else '') +
            (f'<tr><td colspan="3" style="padding:10px 16px;font-size:12px;color:#9ca3af;text-align:right">Moms 25% af ydelser</td><td style="padding:10px 16px;font-size:12px;color:#9ca3af;text-align:right">{int(moms_beloeb):,} kr.</td></tr>' if moms_beloeb > 0 else '') +
            rabat_rækker +
            f'<tr style="background:{accent}"><td colspan="3" style="padding:14px 16px;font-size:14px;font-weight:700;color:#fff;text-align:right">Total inkl. moms</td><td style="padding:14px 16px;font-size:18px;font-weight:900;color:#fff;text-align:right">{int(total):,} kr.</td></tr>'
        )
    else:
        std_total_rækker = (
            rabat_rækker +
            f'<tr style="background:#f9fafb"><td colspan="3" style="padding:14px 16px;font-size:13px;font-weight:700;color:#1a1a2e;text-align:right">Total ekskl. moms</td><td style="padding:14px 16px;font-size:16px;font-weight:900;color:{accent};text-align:right">{int(total):,} kr.</td></tr>'
            f'<tr><td colspan="3" style="padding:10px 16px;font-size:12px;color:#9ca3af;text-align:right">Moms (25%)</td><td style="padding:10px 16px;font-size:12px;color:#9ca3af;text-align:right">{int(moms_beloeb):,} kr.</td></tr>'
            f'<tr style="background:{accent}"><td colspan="3" style="padding:14px 16px;font-size:14px;font-weight:700;color:#fff;text-align:right">Total inkl. moms</td><td style="padding:14px 16px;font-size:18px;font-weight:900;color:#fff;text-align:right">{int(total*1.25 if not has_inkl else total):,} kr.</td></tr>'
        )

    return f"""<!DOCTYPE html>
<html lang="da"><head><meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Tilbud — {titel}</title></head>
<body style="margin:0;padding:0;background:#f7f8fc;font-family:'Helvetica Neue',Arial,sans-serif">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f7f8fc;padding:40px 20px">
<tr><td align="center">
<table width="680" cellpadding="0" cellspacing="0" style="max-width:680px">

  <!-- HEADER -->
  <tr><td style="{header_bg};border-radius:16px 16px 0 0;padding:36px 40px">
    <table width="100%" cellpadding="0" cellspacing="0"><tr>
      <td>
        <div style="font-size:22px;font-weight:900;color:#fff;letter-spacing:-0.5px">{klient_navn}</div>
        <div style="font-size:12px;color:rgba(255,255,255,.5);margin-top:2px">{klient_hjemmeside}</div>
      </td>
      <td style="text-align:right;vertical-align:top">
        <div style="font-size:10px;color:rgba(255,255,255,.4);text-transform:uppercase;letter-spacing:1px">Tilbud</div>
        <div style="font-size:12px;color:rgba(255,255,255,.6);margin-top:4px">{dato}</div>
        <div style="font-size:10px;color:rgba(255,255,255,.35);margin-top:2px">Gyldigt til {gyldigt_til}</div>
      </td>
    </tr></table>
    <div style="margin-top:24px;padding-top:24px;border-top:1px solid rgba(255,255,255,.1)">
      <div style="font-size:20px;font-weight:700;color:#fff;line-height:1.3">{titel}</div>
      <div style="font-size:13px;color:rgba(255,255,255,.55);margin-top:6px">Til: {kunde_navn} &middot; {kunde_email}{(' &middot; ' + kunde_adresse + (', ' + kunde_postnummer if kunde_postnummer else '')) if kunde_adresse else ''}</div>
    </div>
  </td></tr>

  <!-- BODY -->
  <tr><td style="background:#fff;padding:36px 40px">
    <div style="font-size:14px;color:#374151;line-height:1.8;margin-bottom:28px">{intro}</div>
    {win_html}

    <!-- PRIS TABEL -->
    <div style="margin-bottom:28px">
      <div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:1px;color:#9ca3af;margin-bottom:12px">Indhold &amp; priser</div>
      <table width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #e5e7eb;border-radius:10px;overflow:hidden">
        <tr style="background:#f9fafb">
          <th style="padding:10px 16px;font-size:11px;font-weight:700;color:#9ca3af;text-transform:uppercase;letter-spacing:.5px;text-align:left">Ydelse</th>
          <th style="padding:10px 16px;font-size:11px;font-weight:700;color:#9ca3af;text-transform:uppercase;letter-spacing:.5px;text-align:center">Antal</th>
          <th style="padding:10px 16px;font-size:11px;font-weight:700;color:#9ca3af;text-transform:uppercase;letter-spacing:.5px;text-align:right">Enhedspris</th>
          <th style="padding:10px 16px;font-size:11px;font-weight:700;color:#9ca3af;text-transform:uppercase;letter-spacing:.5px;text-align:right">Total</th>
        </tr>
        {rækker}
        {std_total_rækker}
      </table>
    </div>

    {konkurrent_html}

    {'<div style="background:#fffbeb;border:1px solid #fde68a;border-radius:10px;padding:16px 20px;margin-bottom:28px"><div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:1px;color:#92400e;margin-bottom:8px">⚠️ Forbehold</div><div style="font-size:12px;color:#78350f;line-height:1.7">' + forbehold.replace(chr(10),'<br>') + '</div></div>' if forbehold else ''}

    <div style="background:#f9fafb;border-radius:10px;padding:16px 20px;margin-bottom:28px">
      <div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:1px;color:#9ca3af;margin-bottom:8px">Betingelser</div>
      <div style="font-size:12px;color:#6b7280;line-height:1.6">{betingelser}</div>
    </div>

    <div style="text-align:center;padding:8px 0">
      <div style="font-size:14px;color:#374151;margin-bottom:16px">Har du spørgsmål? Kontakt os gerne.</div>
      <div style="font-size:13px;font-weight:600;color:{accent}">{klient_navn}</div>
    </div>
  </td></tr>

  <!-- FOOTER -->
  <tr><td style="background:#f0f4f8;border-radius:0 0 16px 16px;padding:20px 40px;text-align:center">
    <div style="font-size:11px;color:#9ca3af">Tilbud genereret af NexOlsen AI &middot; Tilbuddet er gyldigt i 14 dage fra udstedelsesdato</div>
  </td></tr>

</table></td></tr></table>
</body></html>"""


@app.route('/priskatalog/<klient_id>', methods=['GET'])
@require_admin
def hent_priskatalog(klient_id):
    if not db: return jsonify([])
    try:
        res = db.table('priskatalog').select('*').eq('klient_id', klient_id).order('kategori').order('navn').execute()
        return jsonify(res.data or [])
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/priskatalog', methods=['POST'])
@require_admin
def gem_prispost():
    if not db: return jsonify({'error': 'Ingen database'}), 500
    data = request.json or {}
    import uuid as _uuid
    try:
        post = {
            'id': str(_uuid.uuid4()),
            'klient_id': data['klient_id'],
            'kategori': data.get('kategori', 'Generelt'),
            'navn': data['navn'],
            'beskrivelse': data.get('beskrivelse', ''),
            'enhedspris': float(data.get('enhedspris', 0)),
            'enhed': data.get('enhed', 'stk'),
            'aktiv': True,
            'moms_inkluderet': bool(data.get('moms_inkluderet', False)),
        }
        db.table('priskatalog').insert(post).execute()
        return jsonify({'ok': True, 'id': post['id']})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/priskatalog/<post_id>', methods=['PATCH'])
@require_admin
def opdater_prispost(post_id):
    if not db: return jsonify({'error': 'Ingen database'}), 500
    data = request.json or {}
    try:
        db.table('priskatalog').update(data).eq('id', post_id).execute()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/priskatalog/<post_id>', methods=['DELETE'])
@require_admin
def slet_prispost(post_id):
    if not db: return jsonify({'error': 'Ingen database'}), 500
    try:
        db.table('priskatalog').delete().eq('id', post_id).execute()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/tilbud/generer', methods=['POST'])
@require_token
def generer_tilbud():
    """Genererer et AI-tilbud med valgfri konkurrentanalyse"""
    if not db:
        return jsonify({'error': 'Database ikke tilgængelig'}), 500
    raw = request.headers.get('Authorization', '')
    _tok = raw.replace('Bearer ', '').strip()
    _tok_info = active_tokens.get(_tok, {})
    data = request.json or {}
    klient_id       = data.get('klient_id', '')
    # Klient-token må kun generere for sit eget klient_id
    if _tok_info.get('role') == 'client':
        klient_id = _tok_info.get('klient_id', klient_id)
    kunde_navn      = data.get('kunde_navn', '')
    kunde_email     = data.get('kunde_email', '')
    kunde_adresse   = data.get('kunde_adresse', '')
    kunde_postnummer= data.get('kunde_postnummer', '')
    forbehold       = data.get('forbehold', '')
    opgave          = data.get('opgave', '')
    noter           = data.get('noter', '')
    kør_konkurrent  = data.get('konkurrent_analyse', False)

    # Hent klient info
    klient_navn = 'Virksomheden'
    klient_hjemmeside = ''
    klient_ydelser = ''
    klient_andet = ''
    klient_tilbud_stil  = 'standard'
    klient_tilbud_farve = '#0a1a3a'
    try:
        k = db.table('klienter').select('navn, hjemmeside, tilbud_stil, tilbud_farve').eq('id', klient_id).single().execute()
        if k.data:
            klient_navn       = k.data.get('navn', klient_navn)
            klient_hjemmeside = k.data.get('hjemmeside', '')
            klient_tilbud_stil  = k.data.get('tilbud_stil', 'standard') or 'standard'
            klient_tilbud_farve = k.data.get('tilbud_farve', '#0a1a3a') or '#0a1a3a'
        cfg = db.table('chatbot_config').select('ydelser, priser, andet, kontakt').eq('klient_id', klient_id).single().execute()
        if cfg.data:
            klient_ydelser = cfg.data.get('ydelser', '')
            klient_andet   = cfg.data.get('andet', '')
    except:
        pass

    # Hent priskatalog
    priskatalog_tekst = ''
    try:
        kat = db.table('priskatalog').select('*').eq('klient_id', klient_id).eq('aktiv', True).order('kategori').execute()
        if kat.data:
            linjer = []
            for p in kat.data:
                moms_note = 'inkl. moms' if p.get('moms_inkluderet') else 'ekskl. moms'
                linje = f"- {p['navn']}: {int(p['enhedspris']):,} kr. per {p.get('enhed','stk')} ({moms_note})"
                if p.get('beskrivelse'):
                    linje += f" — {p['beskrivelse']}"
                linjer.append(linje)
            priskatalog_tekst = '\n'.join(linjer)
    except:
        pass

    katalog_sektion = f"""
PRISKATALOG (brug disse EKSAKTE priser — afveg ikke):
{priskatalog_tekst}
""" if priskatalog_tekst else """
(Intet priskatalog opsat — prissæt ud fra dansk markedspris for opgaven)
"""

    # Claude genererer tilbud-indhold
    prompt = f"""Du er en erfaren dansk salgskonsulent. Generer et professionelt tilbud på dansk for virksomheden "{klient_navn}".

KLIENTOPLYSNINGER:
- Virksomhed: {klient_navn}
- Hjemmeside: {klient_hjemmeside}
- Ydelser de tilbyder: {klient_ydelser}
- Andet info: {klient_andet}
{katalog_sektion}
TILBUD TIL:
- Kundenavn: {kunde_navn}
- Kundens opgave/behov: {opgave}
- Ekstra noter: {noter}

Returner KUN valid JSON (ingen markdown, ingen forklaring) med denne præcise struktur:
{{
  "titel": "kort og præcis tilbudstitel",
  "intro": "2-3 sætninger der adresserer kundens specifikke behov og skaber tillid. Personlig og specifik.",
  "linjer": [
    {{"beskrivelse": "ydelse 1", "antal": 1, "enhed": "stk", "enhedspris": 15000, "total": 15000}},
    {{"beskrivelse": "ydelse 2", "antal": 3, "enhed": "timer", "enhedspris": 850, "total": 2550}}
  ],
  "betingelser": "Betalingsbetingelser: 50% ved ordreafgivelse, 50% ved levering. Levering inden 14 arbejdsdage. Priser ekskl. moms.",
  "win_temaer": [
    "konkret fordel 1 baseret på klientens ydelser",
    "konkret fordel 2",
    "konkret fordel 3"
  ]
}}

Vælg de mest relevante ydelser fra priskataloget til opgaven. Beregn total korrekt (antal × enhedspris). Win-temaer skal være specifikke og relevante — ikke generiske."""

    try:
        resp = ai.messages.create(
            model='claude-sonnet-4-6',
            max_tokens=1500,
            messages=[{'role': 'user', 'content': prompt}]
        )
        raw = resp.content[0].text.strip()
        # Rens JSON
        if '```' in raw:
            raw = raw.split('```')[1]
            if raw.startswith('json'):
                raw = raw[4:]
        tilbud_data = json.loads(raw)
    except Exception as e:
        return jsonify({'error': f'AI-generering fejlede: {str(e)}'}), 500

    # Konkurrentanalyse (hvis aktiveret)
    konkurrent_opsummering = ''
    if kør_konkurrent:
        try:
            konkurrent_opsummering = _kør_konkurrentanalyse(klient_navn, klient_ydelser, opgave)
        except:
            konkurrent_opsummering = ''

    # Byg HTML
    html = _byg_tilbud_html(
        klient_navn=klient_navn,
        klient_hjemmeside=klient_hjemmeside,
        kunde_navn=kunde_navn,
        kunde_email=kunde_email,
        titel=tilbud_data.get('titel', 'Tilbud'),
        intro=tilbud_data.get('intro', ''),
        linjer=tilbud_data.get('linjer', []),
        betingelser=tilbud_data.get('betingelser', ''),
        win_temaer=tilbud_data.get('win_temaer', []),
        konkurrent_opsummering=konkurrent_opsummering,
        tema=klient_tilbud_stil,
        primær_farve=klient_tilbud_farve,
        kunde_adresse=kunde_adresse,
        kunde_postnummer=kunde_postnummer,
        forbehold=forbehold
    )

    total = sum(l.get('total', 0) for l in tilbud_data.get('linjer', []))

    # Gem i Supabase
    tilbud_id = None
    try:
        import uuid as _uuid
        tilbud_id = str(_uuid.uuid4())
        db.table('tilbud').insert({
            'id': tilbud_id,
            'klient_id': klient_id,
            'kunde_navn': kunde_navn,
            'kunde_email': kunde_email,
            'kunde_adresse': kunde_adresse,
            'kunde_postnummer': kunde_postnummer,
            'forbehold': forbehold,
            'titel': tilbud_data.get('titel', 'Tilbud'),
            'html_indhold': html,
            'linjer': tilbud_data.get('linjer', []),
            'tilbud_meta': {
                'intro':      tilbud_data.get('intro', ''),
                'betingelser': tilbud_data.get('betingelser', ''),
                'win_temaer': tilbud_data.get('win_temaer', [])
            },
            'total_pris': int(total),
            'status': 'udkast',
            'konkurrent_analyse': konkurrent_opsummering
        }).execute()
    except Exception as e:
        print(f"Tilbud gem fejl: {e}")

    return jsonify({
        'ok': True,
        'tilbud_id': tilbud_id,
        'titel': tilbud_data.get('titel'),
        'html': html,
        'total': int(total),
        'linjer': tilbud_data.get('linjer', []),
        'konkurrent_analyse': konkurrent_opsummering
    })


def _kør_konkurrentanalyse(klient_navn, ydelser, opgave):
    """Henter og analyserer konkurrentdata fra nettet"""
    søgeresultater = []

    # Søg på Trustpilot
    trustpilot_sider = [
        'https://www.trustpilot.com/search?query=' + http_requests.utils.quote(ydelser[:50] + ' danmark')
    ]

    # Prøv at hente data fra konkurrenters Trustpilot-sider via Google
    try:
        søg_url = f"https://www.google.dk/search?q={http_requests.utils.quote(ydelser[:40] + ' tilbud pris Danmark konkurrenter')}&num=5"
        headers = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'}
        r = http_requests.get(søg_url, headers=headers, timeout=8)
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, 'html.parser')
            for snippet in soup.select('.VwiC3b, .s3v9rd, .st')[:5]:
                tekst = snippet.get_text(strip=True)
                if tekst and len(tekst) > 30:
                    søgeresultater.append(tekst[:200])
    except:
        pass

    # Claude analyserer og genererer konkurrent-indsigt
    analyse_prompt = f"""Du er en dansk markedsanalytiker. Analyser konkurrencesituationen for en virksomhed inden for: {ydelser}

Opgave der skal laves: {opgave}

Søgeresultater fra nettet (kan være begrænsede):
{chr(10).join(søgeresultater) if søgeresultater else 'Ingen direkte søgeresultater — brug din viden om det danske marked.'}

Skriv 2-3 korte sætninger (max 80 ord total) der beskriver:
- Hvad markedsprisen typisk er for denne type opgave i Danmark
- Hvad konkurrenter typisk tilbyder/ikke tilbyder
- En konkret positioneringsfordel {klient_navn} kan fremhæve

Vær specifik og faktabaseret. Skriv på dansk. Kun den rå tekst, ingen overskrifter."""

    resp = ai.messages.create(
        model='claude-haiku-4-5-20251001',
        max_tokens=200,
        messages=[{'role': 'user', 'content': analyse_prompt}]
    )
    return resp.content[0].text.strip()


@app.route('/tilbud/liste/<klient_id>', methods=['GET'])
@require_admin
def hent_tilbud_liste(klient_id):
    """Henter alle tilbud for en klient"""
    if not db:
        return jsonify([])
    try:
        res = db.table('tilbud').select('id,kunde_navn,kunde_email,kunde_adresse,kunde_postnummer,titel,total_pris,status,oprettet,godkendt_dato').eq('klient_id', klient_id).order('oprettet', desc=True).execute()
        return jsonify(res.data or [])
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/portal/overblik/<klient_id>', methods=['GET'])
@require_token
def portal_overblik(klient_id):
    """Klientportal: ROI-data + aktivitetsfeed"""
    from datetime import datetime, timezone
    raw = request.headers.get('Authorization', '')
    token = raw.replace('Bearer ', '').strip()
    info = active_tokens.get(token, {})
    if info.get('role') == 'client' and info.get('klient_id') != klient_id:
        return jsonify({'error': 'Ingen adgang'}), 403
    if not db:
        return jsonify({'roi': {}, 'aktivitet': []})

    try:
        # Månedsstart
        nu = datetime.now(timezone.utc)
        maaned_start = nu.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()

        # Hent abonnement
        plan_priser = {'starter': 799, 'pro': 1499, 'vaekst': 2499}
        abo_pris = 799
        try:
            k = db.table('klienter').select('plan').eq('id', klient_id).single().execute()
            abo_pris = plan_priser.get(k.data.get('plan', 'starter'), 799)
        except: pass

        # Hent accepterede tilbud i alt og denne måned
        tilbud_res = db.table('tilbud').select('id,total_pris,status,godkendt_dato,oprettet,kunde_navn,titel,sendt_dato').eq('klient_id', klient_id).order('oprettet', desc=True).execute()
        tilbud_alle = tilbud_res.data or []

        accepterede_i_alt = [t for t in tilbud_alle if t.get('status') == 'accepteret']
        accepterede_måned = [t for t in accepterede_i_alt if t.get('godkendt_dato', '') >= maaned_start]
        afventende       = [t for t in tilbud_alle if t.get('status') == 'sendt']

        roi_måned  = sum(t.get('total_pris', 0) or 0 for t in accepterede_måned)
        roi_i_alt  = sum(t.get('total_pris', 0) or 0 for t in accepterede_i_alt)
        roi_faktor = round(roi_måned / abo_pris, 1) if abo_pris > 0 else 0

        # Leads
        leads_res = db.table('leads').select('id,navn,oprettet,kilde,email').eq('klient_id', klient_id).order('oprettet', desc=True).limit(50).execute()
        leads_alle = leads_res.data or []
        leads_måned = [l for l in leads_alle if l.get('oprettet', '') >= maaned_start]

        # Bookinger
        book_res = db.table('bookinger').select('id,navn,oprettet,dato').eq('klient_id', klient_id).order('oprettet', desc=True).limit(50).execute()
        book_alle = book_res.data or []
        book_måned = [b for b in book_alle if b.get('oprettet', '') >= maaned_start]

        # Byg aktivitetsfeed — kombiner events fra alle tabeller
        events = []
        for l in leads_alle[:15]:
            events.append({
                'type': 'lead',
                'ikon': '◎',
                'farve': '#2563eb',
                'bg': '#eff6ff',
                'titel': f"Ny lead: {l.get('navn','—')}",
                'sub': l.get('kilde', 'hjemmeside').capitalize(),
                'tid': l.get('oprettet', '')
            })
        for b in book_alle[:10]:
            events.append({
                'type': 'booking',
                'ikon': '▦',
                'farve': '#7c3aed',
                'bg': '#f5f3ff',
                'titel': f"Ny booking: {b.get('navn','—')}",
                'sub': f"Dato: {b.get('dato','—')}",
                'tid': b.get('oprettet', '')
            })
        for t in tilbud_alle[:20]:
            if t.get('status') == 'accepteret':
                events.append({
                    'type': 'tilbud_accepteret',
                    'ikon': '🎉',
                    'farve': '#15803d',
                    'bg': '#f0fdf4',
                    'titel': f"Tilbud accepteret — {int(t.get('total_pris',0) or 0):,} kr".replace(',','.'),
                    'sub': t.get('kunde_navn', '—'),
                    'tid': t.get('godkendt_dato', '') or t.get('oprettet', '')
                })
            elif t.get('status') == 'sendt' and t.get('sendt_dato'):
                events.append({
                    'type': 'tilbud_sendt',
                    'ikon': '✉',
                    'farve': '#0369a1',
                    'bg': '#f0f9ff',
                    'titel': f"Tilbud sendt: {t.get('titel','Tilbud')}",
                    'sub': t.get('kunde_navn', '—'),
                    'tid': t.get('sendt_dato', '')
                })
            elif t.get('status') == 'udkast':
                events.append({
                    'type': 'tilbud_oprettet',
                    'ikon': '📄',
                    'farve': '#6b7280',
                    'bg': '#f9fafb',
                    'titel': f"Tilbud oprettet: {t.get('titel','Tilbud')}",
                    'sub': t.get('kunde_navn', '—'),
                    'tid': t.get('oprettet', '')
                })

        events.sort(key=lambda e: e.get('tid', ''), reverse=True)

        return jsonify({
            'roi': {
                'abo_pris': abo_pris,
                'accepteret_maaned': roi_måned,
                'accepteret_i_alt': roi_i_alt,
                'roi_faktor': roi_faktor,
                'leads_maaned': len(leads_måned),
                'leads_i_alt': len(leads_alle),
                'bookinger_maaned': len(book_måned),
                'bookinger_i_alt': len(book_alle),
                'tilbud_accepteret_maaned': len(accepterede_måned),
                'tilbud_afventende': len(afventende),
            },
            'aktivitet': events[:25]
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/portal/tilbud/<klient_id>', methods=['GET'])
@require_token
def portal_tilbud_liste(klient_id):
    """Klientportal: henter tilbud for klienten (klient-token eller admin)"""
    raw = request.headers.get('Authorization', '')
    token = raw.replace('Bearer ', '').strip()
    info = active_tokens.get(token, {})
    if info.get('role') == 'client' and info.get('klient_id') != klient_id:
        return jsonify({'error': 'Ingen adgang'}), 403
    if not db:
        return jsonify([])
    try:
        res = db.table('tilbud').select('id,kunde_navn,kunde_email,kunde_adresse,kunde_postnummer,titel,total_pris,status,oprettet,godkendt_dato,sendt_dato').eq('klient_id', klient_id).order('oprettet', desc=True).execute()
        return jsonify(res.data or [])
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/tilbud/<tilbud_id>', methods=['GET'])
@require_admin
def hent_tilbud(tilbud_id):
    """Henter et specifikt tilbud"""
    if not db:
        return jsonify({'error': 'Ingen database'}), 500
    try:
        res = db.table('tilbud').select('*').eq('id', tilbud_id).single().execute()
        return jsonify(res.data or {})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/portal/tilbud/vis/<tilbud_id>', methods=['GET'])
def portal_vis_tilbud(tilbud_id):
    """Klientportal: vis tilbud HTML direkte i browser (token via query param)"""
    token = request.args.get('token', '').strip()
    auto_print = request.args.get('pdf') == '1'
    if not _token_ok(token):
        return '<html><body style="font-family:sans-serif;padding:2rem;color:#c0392b">Adgang krævet — log ind igen.</body></html>', 401
    if not db:
        return '<html><body style="font-family:sans-serif;padding:2rem">Database ikke tilgængelig.</body></html>', 500
    try:
        res = db.table('tilbud').select('html_indhold,klient_id,titel,kunde_navn').eq('id', tilbud_id).single().execute()
        t = res.data
        if not t:
            return '<html><body style="font-family:sans-serif;padding:2rem">Tilbud ikke fundet.</body></html>', 404
        info = active_tokens.get(token, {})
        if info.get('role') == 'client' and info.get('klient_id') != t['klient_id']:
            return '<html><body style="font-family:sans-serif;padding:2rem;color:#c0392b">Ingen adgang til dette tilbud.</body></html>', 403

        titel = t.get('titel', 'Tilbud')
        kunde = t.get('kunde_navn', '')

        toolbar = f"""
<style>
  #nexo-toolbar {{
    position: fixed; top: 0; left: 0; right: 0; z-index: 9999;
    background: #1a1918; color: #fff;
    display: flex; align-items: center; justify-content: space-between;
    padding: 0 1.5rem; height: 52px;
    font-family: -apple-system, BlinkMacSystemFont, 'Inter', sans-serif;
    box-shadow: 0 2px 12px rgba(0,0,0,.3);
  }}
  #nexo-toolbar .tb-left {{ font-size: .82rem; color: rgba(255,255,255,.55); }}
  #nexo-toolbar .tb-left strong {{ color: #fff; font-size: .9rem; }}
  #nexo-toolbar .tb-btns {{ display: flex; gap: .6rem; }}
  #nexo-toolbar button {{
    font-family: inherit; font-size: .8rem; font-weight: 600;
    padding: .45rem 1.1rem; border-radius: 7px; cursor: pointer; border: none;
  }}
  #nexo-toolbar .btn-back {{
    background: rgba(255,255,255,.1); color: rgba(255,255,255,.7);
  }}
  #nexo-toolbar .btn-back:hover {{ background: rgba(255,255,255,.18); color: #fff; }}
  #nexo-toolbar .btn-pdf {{
    background: #fff; color: #1a1918;
  }}
  #nexo-toolbar .btn-pdf:hover {{ background: #f0f0f0; }}
  body {{ padding-top: 52px !important; }}
  @media print {{
    #nexo-toolbar {{ display: none !important; }}
    body {{ padding-top: 0 !important; }}
    @page {{ margin: 10mm 12mm; }}
    table {{ page-break-inside: avoid; }}
  }}
</style>
<div id="nexo-toolbar">
  <div class="tb-left">
    <strong>{titel}</strong>
    {(' &middot; ' + kunde) if kunde else ''}
  </div>
  <div class="tb-btns">
    <button class="btn-back" onclick="window.close()">← Luk</button>
    <button class="btn-pdf" onclick="window.print()">⬇ Download som PDF</button>
  </div>
</div>
{'<script>window.addEventListener("load",function(){{window.print();}});</script>' if auto_print else ''}
"""
        # Inject toolbar right after <body>
        html = t['html_indhold']
        if '<body' in html:
            insert_at = html.index('<body')
            body_end = html.index('>', insert_at) + 1
            html = html[:body_end] + toolbar + html[body_end:]
        else:
            html = toolbar + html

        return Response(html, mimetype='text/html')
    except Exception as e:
        return f'<html><body style="font-family:sans-serif;padding:2rem">Fejl: {str(e)}</body></html>', 500


@app.route('/tilbud/<tilbud_id>', methods=['PATCH'])
@require_admin
def opdater_tilbud(tilbud_id):
    """Opdaterer et tilbud med redigerede linjer og rabat"""
    if not db:
        return jsonify({'error': 'Ingen database'}), 500
    data = request.get_json() or {}
    linjer   = data.get('linjer', [])
    rabat    = float(data.get('rabat', 0) or 0)
    forbehold = data.get('forbehold', None)  # None = don't change; '' = clear

    try:
        res = db.table('tilbud').select('*').eq('id', tilbud_id).single().execute()
        t = res.data
        if not t:
            return jsonify({'error': 'Tilbud ikke fundet'}), 404

        # Recalculate line totals
        for l in linjer:
            l['total'] = round(float(l.get('antal', 1)) * float(l.get('enhedspris', 0)), 2)

        sum_inkl    = sum(l.get('total', 0) for l in linjer if l.get('moms_inkluderet', False))
        sum_ekskl   = sum(l.get('total', 0) for l in linjer if not l.get('moms_inkluderet', False))
        has_inkl    = any(l.get('moms_inkluderet', False) for l in linjer)
        moms_beloeb = round(sum_ekskl * 0.25, 2)
        total_foer_rabat = sum_inkl + sum_ekskl + moms_beloeb if has_inkl else (sum_ekskl * 1.25)
        total_efter_rabat = max(0, total_foer_rabat - rabat)

        # Fetch klient info for header + branding
        klient_res = db.table('klienter').select('navn,hjemmeside,tilbud_stil,tilbud_farve').eq('id', t['klient_id']).single().execute()
        klient = klient_res.data or {}

        # Use stored meta (intro, betingelser, win_temaer)
        meta = t.get('tilbud_meta') or {}
        forbehold_final = forbehold if forbehold is not None else (t.get('forbehold') or '')

        html = _byg_tilbud_html(
            klient_navn=klient.get('navn', ''),
            klient_hjemmeside=klient.get('hjemmeside', ''),
            kunde_navn=t['kunde_navn'],
            kunde_email=t['kunde_email'],
            titel=t['titel'],
            intro=meta.get('intro', ''),
            linjer=linjer,
            betingelser=meta.get('betingelser', ''),
            win_temaer=meta.get('win_temaer', []),
            konkurrent_opsummering=t.get('konkurrent_analyse', ''),
            rabat=rabat,
            tema=klient.get('tilbud_stil', 'standard') or 'standard',
            primær_farve=klient.get('tilbud_farve', '#0a1a3a') or '#0a1a3a',
            kunde_adresse=t.get('kunde_adresse', '') or '',
            kunde_postnummer=t.get('kunde_postnummer', '') or '',
            forbehold=forbehold_final
        )

        update_payload = {
            'html_indhold': html,
            'linjer': linjer,
            'total_pris': int(total_efter_rabat),
            'forbehold': forbehold_final
        }
        db.table('tilbud').update(update_payload).eq('id', tilbud_id).execute()

        return jsonify({'ok': True, 'html': html, 'total': int(total_efter_rabat), 'linjer': linjer})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/tilbud/send/<tilbud_id>', methods=['POST'])
@require_admin
def send_tilbud(tilbud_id):
    """Sender tilbud til kunden via email"""
    if not db:
        return jsonify({'error': 'Ingen database'}), 500
    try:
        res = db.table('tilbud').select('*').eq('id', tilbud_id).single().execute()
        tilbud = res.data
        if not tilbud:
            return jsonify({'error': 'Tilbud ikke fundet'}), 404

        kunde_email = tilbud.get('kunde_email', '')
        kunde_navn  = tilbud.get('kunde_navn', '')
        titel       = tilbud.get('titel', 'Tilbud')
        html        = tilbud.get('html_indhold', '')
        klient_id   = tilbud.get('klient_id', '')

        if not kunde_email or '@' not in kunde_email:
            return jsonify({'error': 'Ingen gyldig email på tilbuddet'}), 400

        # Hent klientens navn + hjemmeside til afsender og links
        fra_navn = 'NexOlsen'
        klient_hjemmeside = ''
        klient_email = ''
        try:
            k = db.table('klienter').select('navn,hjemmeside,email').eq('id', klient_id).single().execute()
            if k.data:
                fra_navn = k.data.get('navn', fra_navn)
                klient_hjemmeside = k.data.get('hjemmeside', '')
                klient_email = k.data.get('email', '')
        except:
            pass

        # Generer unik accept-token og gem i DB
        import uuid as _uuid
        accept_token = str(_uuid.uuid4()).replace('-', '')
        try:
            db.table('tilbud').update({'accept_token': accept_token}).eq('id', tilbud_id).execute()
        except:
            pass

        accept_url = f'{SERVER_URL}/tilbud/godkend/{tilbud_id}/{accept_token}'

        # Byg Trustpilot-URL fra hjemmeside-domæne
        domain = klient_hjemmeside.replace('https://','').replace('http://','').replace('www.','').rstrip('/')
        trustpilot_url = f'https://dk.trustpilot.com/review/{domain}' if domain else 'https://dk.trustpilot.com'

        # Fjern konkurrent-sektion fra kunde-email (kun intern brug)
        import re as _re
        html_til_kunde = _re.sub(r'<!-- KONKURRENT.*?KONKURRENT -->', '', html, flags=_re.DOTALL)

        # Indsæt accept-knap + Trustpilot-sektion inden footeren
        ekstra_blok = f"""
  <tr><td style="background:#fff;padding:8px 40px 32px;text-align:center">
    <div style="font-size:15px;color:#374151;margin-bottom:16px;font-weight:600">Klar til at komme i gang?</div>
    <a href="{accept_url}" style="display:inline-block;background:#16a34a;color:#fff;text-decoration:none;font-size:16px;font-weight:700;padding:15px 44px;border-radius:10px;letter-spacing:.3px">&#10003;&nbsp;&nbsp;Godkend tilbud</a>
    <div style="font-size:11px;color:#9ca3af;margin-top:12px;line-height:1.7">
      Ved at klikke bekræfter du at acceptere dette tilbud bindende i henhold til dansk Aftaleloven.<br>
      Du modtager straks en skriftlig bekræftelse på denne email-adresse.
    </div>
  </td></tr>
  <tr><td style="background:#f9fafb;padding:28px 40px;text-align:center;border-top:1px solid #f0f0f0">
    <div style="font-size:20px;margin-bottom:10px">&#11088;</div>
    <div style="font-size:14px;font-weight:700;color:#111;margin-bottom:8px">Har du haft en god oplevelse?</div>
    <div style="font-size:13px;color:#6b7280;line-height:1.7;margin-bottom:16px">Vi går meget op i vores kunders tilfredshed.<br>En Trustpilot-anmeldelse ville betyde alverden for os.</div>
    <a href="{trustpilot_url}" style="display:inline-block;background:#00b67a;color:#fff;text-decoration:none;font-size:13px;font-weight:700;padding:10px 24px;border-radius:8px">Skriv en anmeldelse &#8594;</a>
  </td></tr>"""

        html_til_kunde = html_til_kunde.replace('<!-- FOOTER -->', ekstra_blok + '\n  <!-- FOOTER -->')

        send_mail(kunde_email, f'Tilbud: {titel}', f'Hej {kunde_navn},\n\nSe tilbuddet herunder.', fra_navn=fra_navn, html_content=html_til_kunde)

        db.table('tilbud').update({'status': 'sendt', 'sendt_dato': datetime.now().isoformat()}).eq('id', tilbud_id).execute()

        return jsonify({'ok': True, 'sendt_til': kunde_email})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/tilbud/godkend/<tilbud_id>/<token>', methods=['GET'])
def godkend_tilbud(tilbud_id, token):
    """Offentligt endpoint — kunden klikker 'Godkend tilbud' i mailen"""
    if not db:
        return '<h2>Systemfejl — kontakt os direkte</h2>', 500
    try:
        res = db.table('tilbud').select('*').eq('id', tilbud_id).single().execute()
        t = res.data
        if not t:
            return _godkend_side('Tilbud ikke fundet', 'Vi kunne ikke finde dette tilbud. Kontakt os direkte.', fejl=True), 404
        if t.get('accept_token') != token:
            return _godkend_side('Ugyldigt link', 'Dette link er ikke gyldigt. Kontakt os direkte.', fejl=True), 403
        if t.get('status') == 'accepteret':
            godkendt_dato = t.get('godkendt_dato', '')[:19].replace('T', ' kl. ') if t.get('godkendt_dato') else '—'
            return _godkend_side('Allerede bekræftet',
                f'Dit tilbud "{t.get("titel","")}" er allerede godkendt den {godkendt_dato}. '
                f'Du har modtaget en bekræftelse på {t.get("kunde_email","din email")}.',
                fejl=False), 200

        from datetime import datetime
        ip_adresse = request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0].strip()
        godkendt_dato = datetime.now()

        db.table('tilbud').update({
            'status': 'accepteret',
            'godkendt_dato': godkendt_dato.isoformat(),
            'godkendt_ip': ip_adresse
        }).eq('id', tilbud_id).execute()

        kunde_navn  = t.get('kunde_navn', '')
        kunde_email = t.get('kunde_email', '')
        titel       = t.get('titel', 'Tilbud')
        total_pris  = int(t.get('total_pris', 0))
        ref_nr      = tilbud_id[:8].upper()
        dato_str    = godkendt_dato.strftime('%-d. %B %Y kl. %H:%M')

        # Hent klient info til kontaktoplysninger
        klient_navn    = ''
        klient_email   = ''
        klient_telefon = ''
        try:
            k = db.table('klienter').select('navn,email,telefon').eq('id', t['klient_id']).single().execute()
            if k.data:
                klient_navn    = k.data.get('navn', '')
                klient_email   = k.data.get('email', '')
                klient_telefon = k.data.get('telefon', '')
        except:
            pass

        # Send juridisk bekræftelsesmail til KUNDEN
        bekræftelse_html = f"""<!DOCTYPE html>
<html lang="da"><head><meta charset="UTF-8"/></head>
<body style="margin:0;padding:0;background:#f0fdf4;font-family:'Helvetica Neue',Arial,sans-serif">
<table width="100%" cellpadding="0" cellspacing="0" style="padding:40px 20px"><tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 4px 20px rgba(0,0,0,.08)">

  <tr><td style="background:#15803d;padding:24px 36px">
    <div style="font-size:24px;font-weight:900;color:#fff">&#10003; Tilbud godkendt</div>
    <div style="font-size:13px;color:rgba(255,255,255,.7);margin-top:4px">Skriftlig bekræftelse i henhold til dansk Aftaleloven</div>
  </td></tr>

  <tr><td style="padding:32px 36px">
    <div style="font-size:15px;color:#374151;line-height:1.8;margin-bottom:24px">
      Hej <strong>{kunde_navn}</strong>,<br><br>
      Dette er din officielle bekræftelse på at du har accepteret følgende tilbud:
    </div>

    <table width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #d1fae5;border-radius:10px;overflow:hidden;margin-bottom:24px">
      <tr style="background:#f0fdf4">
        <td colspan="2" style="padding:12px 16px;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:1px;color:#15803d">Tilbudsdetaljer</td>
      </tr>
      <tr style="border-top:1px solid #d1fae5">
        <td style="padding:10px 16px;font-size:13px;color:#6b7280;width:40%">Tilbud</td>
        <td style="padding:10px 16px;font-size:13px;font-weight:600;color:#111">{titel}</td>
      </tr>
      <tr style="border-top:1px solid #f0f0f0">
        <td style="padding:10px 16px;font-size:13px;color:#6b7280">Beløb</td>
        <td style="padding:10px 16px;font-size:13px;font-weight:700;color:#15803d">{total_pris:,} kr. ekskl. moms</td>
      </tr>
      <tr style="border-top:1px solid #f0f0f0">
        <td style="padding:10px 16px;font-size:13px;color:#6b7280">Referencenr.</td>
        <td style="padding:10px 16px;font-size:13px;font-weight:600;color:#111;font-family:monospace">{ref_nr}</td>
      </tr>
      <tr style="border-top:1px solid #f0f0f0">
        <td style="padding:10px 16px;font-size:13px;color:#6b7280">Accepteret den</td>
        <td style="padding:10px 16px;font-size:13px;font-weight:600;color:#111">{dato_str}</td>
      </tr>
      <tr style="border-top:1px solid #f0f0f0">
        <td style="padding:10px 16px;font-size:13px;color:#6b7280">Din email</td>
        <td style="padding:10px 16px;font-size:13px;color:#111">{kunde_email}</td>
      </tr>
    </table>

    <div style="background:#fefce8;border:1px solid #fde68a;border-radius:8px;padding:14px 16px;margin-bottom:24px">
      <div style="font-size:12px;color:#92400e;line-height:1.7">
        <strong>Juridisk note:</strong> Din accept er registreret elektronisk og er bindende i henhold til dansk Aftaleloven §1 og E-handelsloven. Denne email tjener som dit skriftlige bevis på accept. Gem den til fremtidig reference.
      </div>
    </div>

    <div style="font-size:14px;color:#374151;line-height:1.8">
      <strong>{klient_navn}</strong> vil kontakte dig hurtigst muligt for at aftale de næste skridt.
      {'<br>Email: ' + klient_email if klient_email else ''}
      {'<br>Telefon: ' + klient_telefon if klient_telefon else ''}
    </div>
  </td></tr>

  <tr><td style="background:#f9fafb;padding:18px 36px;text-align:center;border-top:1px solid #f0f0f0">
    <div style="font-size:11px;color:#9ca3af">Reference: {ref_nr} &middot; Genereret af NexOlsen AI</div>
  </td></tr>

</table></td></tr></table>
</body></html>"""

        try:
            send_mail(kunde_email, f'Bekræftelse: Du har godkendt "{titel}"', f'Hej {kunde_navn},\n\nDette er din bekræftelse på at du har accepteret tilbuddet "{titel}" ({total_pris:,} kr.).\n\nReference: {ref_nr}', fra_navn=klient_navn or 'NexOlsen', html_content=bekræftelse_html)
        except Exception as e:
            print(f'Bekræftelsesmail fejl: {e}')

        # Notificér klienten (virksomheden)
        try:
            if klient_email:
                notif_html = (
                    f'<div style="font-family:sans-serif;padding:20px">'
                    f'<h2 style="color:#15803d">&#9989; Tilbud accepteret!</h2>'
                    f'<p><strong>{kunde_navn}</strong> har accepteret tilbuddet <strong>"{titel}"</strong>.</p>'
                    f'<table style="border-collapse:collapse;width:100%;max-width:400px">'
                    f'<tr><td style="padding:8px;color:#666;border-bottom:1px solid #eee">Beløb</td><td style="padding:8px;font-weight:700;border-bottom:1px solid #eee">{total_pris:,} kr.</td></tr>'
                    f'<tr><td style="padding:8px;color:#666;border-bottom:1px solid #eee">Tidspunkt</td><td style="padding:8px;border-bottom:1px solid #eee">{dato_str}</td></tr>'
                    f'<tr><td style="padding:8px;color:#666">Reference</td><td style="padding:8px;font-family:monospace">{ref_nr}</td></tr>'
                    f'</table></div>'
                )
                send_mail(klient_email, f'✅ Tilbud accepteret: {titel}', f'{kunde_navn} har accepteret tilbuddet "{titel}" ({total_pris:,} kr.).', fra_navn='NexOlsen AI', html_content=notif_html)
        except:
            pass

        return _godkend_side(
            'Tilbud godkendt!',
            f'Tak, {kunde_navn.split()[0] if kunde_navn else ""}! Vi har modtaget din accept og sender dig straks en bekræftelse på {kunde_email}. {klient_navn} kontakter dig snarest for at sætte projektet i gang.',
            ref_nr=ref_nr,
            dato_str=dato_str,
            fejl=False
        ), 200
    except Exception as e:
        return _godkend_side('Fejl', str(e), fejl=True), 500


def _godkend_side(overskrift, besked, fejl=False, ref_nr='', dato_str=''):
    farve = '#dc2626' if fejl else '#15803d'
    bg    = '#fff5f5' if fejl else '#f0fdf4'
    ikon  = '❌' if fejl else '✅'
    ekstra = ''
    if not fejl and ref_nr:
        ekstra = f"""
  <div style="margin-top:24px;background:#fff;border:1px solid #d1fae5;border-radius:10px;padding:16px 20px;text-align:left;display:inline-block;min-width:260px">
    <div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:1px;color:#15803d;margin-bottom:10px">Din accept er registreret</div>
    <div style="font-size:13px;color:#374151;margin-bottom:6px">&#128337; {dato_str}</div>
    <div style="font-size:13px;color:#374151">&#128196; Reference: <strong style="font-family:monospace">{ref_nr}</strong></div>
  </div>
  <div style="margin-top:16px;font-size:12px;color:#9ca3af">En bekræftelse er sendt til din email</div>"""
    return f"""<!DOCTYPE html>
<html lang="da"><head><meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{overskrift}</title></head>
<body style="margin:0;padding:0;background:{bg};font-family:'Helvetica Neue',Arial,sans-serif;min-height:100vh;display:flex;align-items:center;justify-content:center">
<div style="text-align:center;max-width:520px;padding:40px 24px">
  <div style="font-size:72px;margin-bottom:16px">{ikon}</div>
  <h1 style="font-size:28px;font-weight:900;color:{farve};margin:0 0 12px">{overskrift}</h1>
  <p style="font-size:15px;color:#374151;line-height:1.8;margin:0">{besked}</p>
  {ekstra}
</div>
</body></html>"""


@app.route('/tilbud/status/<tilbud_id>', methods=['PATCH'])
@require_admin
def opdater_tilbud_status(tilbud_id):
    """Opdaterer status på et tilbud"""
    if not db:
        return jsonify({'error': 'Ingen database'}), 500
    data = request.json or {}
    ny_status = data.get('status', '')
    if ny_status not in ('udkast', 'sendt', 'accepteret', 'afvist'):
        return jsonify({'error': 'Ugyldig status'}), 400
    try:
        db.table('tilbud').update({'status': ny_status}).eq('id', tilbud_id).execute()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    print(f"🤖 NexOlsen Agent Server kører på port {port}")
    app.run(host='0.0.0.0', port=port, debug=False)

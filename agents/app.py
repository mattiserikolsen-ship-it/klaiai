#!/usr/bin/env python3
"""
Nordolsen Agent Server
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
import hmac
import hashlib
import base64
import struct
import html
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
import urllib.parse
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
                # Under-bruger-felter (None for ejer-/Nordolsen-admin-login)
                'bruger_id': info.get('bruger_id'),
                'bruger_rolle': info.get('bruger_rolle'),
                'adgang': info.get('adgang'),
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
                    'bruger_id': sess.get('bruger_id'),
                    'bruger_rolle': sess.get('bruger_rolle'),
                    'adgang': sess.get('adgang'),
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

def _slet_brugers_sessions(bruger_id):
    """Log en under-bruger ud overalt ved at fjerne alle deres tokens.

    Bruges naar chefen aendrer en brugers rolle/adgang, deaktiverer eller
    sletter dem — saa aendringen faar oejeblikkelig effekt (brugeren tvinges
    til at logge ind igen med den nye adgang, i stedet for at vente paa
    token-udloeb)."""
    if not bruger_id:
        return
    for t, info in list(active_tokens.items()):
        if str(info.get('bruger_id')) == str(bruger_id):
            active_tokens.pop(t, None)
    if db:
        try:
            db.table('admin_sessions').delete().eq('bruger_id', str(bruger_id)).execute()
        except:
            pass

# ── PORTAL-SEKTIONER + ROLLE-ADGANG ────────────────────
# Sektioner en medarbejder kan tildeles adgang til (chefen saetter flueben).
# 'overblik' er altid tilladt og er derfor ikke en valgbar checkbox.
PORTAL_SEKTIONER = [
    'forretning', 'leads', 'indbakke', 'bookinger', 'tilbud',
    'mails', 'pipeline', 'gaps', 'agenter', 'rapport'
]
# Sektioner der ALTID kun er for ejer/admin — kan ikke uddelegeres til en
# medarbejder (foelsomme: fakturering, indstillinger, brugerstyring, opsaetning).
ADMIN_SEKTIONER = ['abonnement', 'indstillinger', 'kom-i-gang', 'brugere']

def _sti_sektion(path):
    """Mapper en request-sti til den portal-sektion den tilhoerer.

    Kun de sensitive/sektions-specifikke ruter mappes. Uafhaengige eller
    offentlige ruter (chat, widget, lead-intake-webhook, login, health ...)
    returnerer None => tillades altid. Tenant-isolationen (_ingen_adgang)
    beskytter stadig paa tvaers af virksomheder uanset denne mapping."""
    p = (path or '').lower()
    # Kun-ejer/admin foerst (haard laas)
    if p.startswith('/portal/brugere'):
        return 'brugere'
    if p.startswith('/stripe/'):
        return 'abonnement'
    if p.startswith('/chatbot-config') or p.startswith('/econ/connect') or p.startswith('/econ/disconnect'):
        return 'indstillinger'
    # Medarbejder-tildelbare sektioner
    if p.startswith('/portal/indbakke') or p.startswith('/portal/inbound-adresse'):
        return 'indbakke'
    if p.startswith('/portal/mail-config') or p.startswith('/portal/mail-preview'):
        return 'mails'
    if p.startswith('/portal/crm') or p.startswith('/crm/') or p.startswith('/markeds-analyse'):
        return 'pipeline'
    if (p.startswith('/rapport/') or p.startswith('/preview-rapport') or p.startswith('/send-rapport')
            or p.startswith('/insights') or p.startswith('/apply-insight')):
        return 'rapport'
    if p.startswith('/gaps/') or p.startswith('/udfyld-gap') or p.startswith('/luk-gap'):
        return 'gaps'
    if (p.startswith('/portal/tilbud') or p.startswith('/tilbud/') or p.startswith('/priskatalog')
            or p.startswith('/tale/') or p.startswith('/materialer/') or p.startswith('/econ/sync-tilbud')):
        return 'tilbud'
    if p.startswith('/bookinger/') or p.startswith('/portal/bookinger'):
        return 'bookinger'
    if (p.startswith('/leads/') or p.startswith('/lead-mails/') or p.startswith('/godkend-mails')
            or p.startswith('/afvis-mails')):
        return 'leads'
    if p.startswith('/research-branche'):
        return 'forretning'
    if p.startswith('/agent-log'):
        return 'agenter'
    return None

demo_sessions = {}   # demo_id -> {'klient_config': {...}, 'url': '...', 'created_at': ...}
prospekter    = {}   # prospekt_id -> {'url', 'navn', 'beskrivelse', 'har_chatbot', 'email_udkast', 'status'}

app = Flask(__name__, static_folder='../app', static_url_path='/app')

# ── CORS ───────────────────────────────────────────────
# Offentlige widget-endpoints indlejres på vilkaarlige kundesider => alle origins.
# Alt andet (admin, portal, login) => kun vores egne domaener.
# "null" er med, fordi admin-panelet aabnes lokalt som fil (file://), der sender
# Origin: null. Adgang er alligevel beskyttet af Bearer-token (ikke cookies),
# saa CORS er ikke sikkerhedsgraensen her — tokenet er.
_app_origins = [o.strip() for o in os.environ.get(
    'APP_ORIGINS',
    'https://klaiai.onrender.com,https://klaai.dk,https://www.klaai.dk,null'
).split(',') if o.strip()]
CORS(app, resources={
    r"/chat": {"origins": "*"},
    r"/lead": {"origins": "*"},
    r"/widget/*": {"origins": "*"},
    r"/booking": {"origins": "*"},
    r"/booking-config/*": {"origins": "*"},
    r"/booking-optaget/*": {"origins": "*"},
    r"/chatbot.js": {"origins": "*"},
    r"/lead-form.js": {"origins": "*"},
    r"/booking-widget.js": {"origins": "*"},
    r"/*": {"origins": _app_origins},
})

# ── RATE LIMITING ──────────────────────────────────────
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],
    storage_uri='memory://'
)

ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', '')  # INGEN fallback — skal sættes i miljøet
if not ADMIN_PASSWORD:
    print("ADVARSEL: ADMIN_PASSWORD er ikke sat — admin-adgang er blokeret indtil den sættes.")
# 2FA (TOTP) for admin-login. Base32-secret sat i miljoeet (ADMIN_TOTP_SECRET).
# Er den TOM => 2FA er slaaet fra (bagudkompatibelt, laaser ikke ude foer enrollment).
# Er den SAT => admin-login kraever bade password OG en gyldig 6-cifret kode.
ADMIN_TOTP_SECRET = os.environ.get('ADMIN_TOTP_SECRET', '').strip()
# Sæt ADMIN_LOCAL_ONLY=true på Render for at blokere admin-adgang fra internettet
ADMIN_LOCAL_ONLY = os.environ.get('ADMIN_LOCAL_ONLY', 'false').lower() == 'true'

def _totp_verify(secret, code, window=1, step=30, digits=6):
    """Verificerer en TOTP-kode (RFC 6238) mod en base32-secret.

    window=1 accepterer ogsaa forrige/naeste 30s-interval, saa smaa ur-forskelle
    mellem server og telefon ikke afviser en ellers korrekt kode. Bruger kun
    stdlib (hmac/hashlib/base64/struct) — ingen ekstern afhaengighed."""
    if not secret or not code:
        return False
    code = str(code).strip().replace(' ', '')
    if not (code.isdigit() and len(code) == digits):
        return False
    try:
        s = secret.strip().replace(' ', '').upper()
        s += '=' * ((8 - len(s) % 8) % 8)
        key = base64.b32decode(s)
    except Exception:
        return False
    counter = int(_time.time() // step)
    for drift in range(-window, window + 1):
        msg = struct.pack('>Q', counter + drift)
        h = hmac.new(key, msg, hashlib.sha1).digest()
        offset = h[-1] & 0x0F
        bincode = struct.unpack('>I', h[offset:offset + 4])[0] & 0x7FFFFFFF
        otp = str(bincode % (10 ** digits)).zfill(digits)
        if hmac.compare_digest(otp, code):
            return True
    return False

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

@app.before_request
def haandhaev_sektions_adgang():
    """Server-side håndhævelse af rolle/sektions-adgang for under-brugere.

    UI'et skjuler menupunkter en medarbejder ikke må se — men skjul er ikke
    sikkerhed. Denne hook blokerer selve API-kaldet, så en medarbejder ikke
    bare kan gå udenom UI'et og ramme et endpoint direkte.

    Logik:
      - Ingen/ugyldigt token         => spring over (endpointets egen decorator afviser).
      - Nordolsen-admin (role=admin) => fuld adgang.
      - Ejer-login (intet bruger_id) => fuld adgang.
      - Under-bruger med rolle ejer/admin => fuld adgang.
      - Medarbejder => kun sektioner i deres 'adgang'. Admin-only-sektioner
        (fakturering, indstillinger, opsætning, brugerstyring) er altid spærret.
    Uafhængige/offentlige ruter mappes ikke til en sektion => tillades.
    """
    if request.method == 'OPTIONS':
        return
    raw = request.headers.get('Authorization', '')
    token = raw.replace('Bearer ', '').strip()
    if not token or not _token_ok(token):
        return  # lad endpointets egen @require_token/@require_admin afgøre
    info = active_tokens.get(token, {})
    if info.get('role') == 'admin':
        return  # Nordolsen-superadmin
    if not info.get('bruger_id'):
        return  # virksomhedens ejer (klienter-login)
    if info.get('bruger_rolle') in ('ejer', 'admin'):
        return  # udnævnt admin i virksomheden
    # Herfra: medarbejder med begrænset adgang.
    sektion = _sti_sektion(request.path)
    if sektion is None:
        return  # ikke en sektions-gated rute
    if sektion in ADMIN_SEKTIONER:
        return jsonify({'error': 'Kun administratorer har adgang til dette'}), 403
    adgang = info.get('adgang') or []
    if sektion not in adgang:
        return jsonify({'error': 'Du har ikke adgang til denne sektion'}), 403

def require_auth(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not ADMIN_PASSWORD or not auth or auth.password != ADMIN_PASSWORD:
            return Response(
                'Adgang kræver login.',
                401,
                {'WWW-Authenticate': 'Basic realm="Nordolsen Admin"'}
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
        _info = active_tokens.get(token, {})
        request.user_klient_id = _info.get('klient_id')
        request.user_role = _info.get('role')
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
        _info = active_tokens.get(token, {})
        request.user_klient_id = _info.get('klient_id')
        request.user_role = _info.get('role')
        return f(*args, **kwargs)
    return decorated

def _ingen_adgang(klient_id):
    """Returnerer True hvis den aktuelle bruger IKKE må tilgå denne klients data.

    Admin må tilgå alt. En klient må kun tilgå sit eget klient_id.
    Brug efter @require_token (som har valideret token og fyldt active_tokens-cachen).
    """
    raw = request.headers.get('Authorization', '')
    token = raw.replace('Bearer ', '').strip()
    info = active_tokens.get(token, {})
    return info.get('role') == 'client' and str(info.get('klient_id')) != str(klient_id)

def _log_fejl(e, besked='Der opstod en teknisk fejl'):
    """Logger den raa fejl server-side og returnerer en generisk besked til klienten.

    Undgaar at laekke interne detaljer (stacktrace, tabelnavne, noegler) i API-svar.
    """
    print(f"FEJL: {e}")
    return besked

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

def hent_eller_opret_inbound_token(klient_id):
    """Returnerer klientens unikke mailbox-token (local-part i deres inbound-adresse).
    Genereres doven foerste gang portalen beder om adressen — ingen backfill noedvendig.
    Token er kort + ikke-gaettelig saa adresser ikke kan enumereres."""
    if not db or str(klient_id) == 'demo':
        return None
    try:
        r = db.table('klienter').select('inbound_token').eq('id', klient_id).single().execute()
        if r.data and r.data.get('inbound_token'):
            return r.data['inbound_token']
    except Exception as e:
        print(f"inbound_token opslag fejl: {e}")
    # Ingen token endnu — generer og gem (retry ved sjaelden kollision paa unikt indeks)
    for _ in range(5):
        token = secrets.token_hex(5)  # 10 hex-tegn, fx 'a3f9c1b2e0'
        try:
            db.table('klienter').update({'inbound_token': token}).eq('id', klient_id).execute()
            return token
        except Exception as e:
            print(f"inbound_token generering fejl: {e}")
    return None

def inbound_adresse(klient_id):
    """Fuld videresendelses-adresse kunden skal sende til, fx a3f9c1b2e0@leads.klaai.dk."""
    token = hent_eller_opret_inbound_token(klient_id)
    return f"{token}@{INBOUND_DOMAIN}" if token else None

def klient_id_fra_inbound_token(token):
    """Slaar en indkommen mailbox-token op -> klient_id. None hvis ukendt."""
    if not db or not token:
        return None
    try:
        r = db.table('klienter').select('id').eq('inbound_token', token).single().execute()
        if r.data:
            return r.data.get('id')
    except Exception as e:
        print(f"klient_id_fra_inbound_token fejl: {e}")
    return None

CONFIG_FILE = os.path.join(os.path.dirname(__file__), 'clients_config.json')
SENDGRID_API_KEY = os.environ.get('SENDGRID_API_KEY', '')
SENDGRID_FROM = os.environ.get('SENDGRID_FROM', '')
ADMIN_EMAIL = os.environ.get('ADMIN_EMAIL', '')
GATEWAYAPI_TOKEN = os.environ.get('GATEWAYAPI_TOKEN', '')
# Subdomaene som kunders info@ videresendes til. Kunde-noeglen ligger i
# local-part: {inbound_token}@{INBOUND_DOMAIN}. Kraever MX -> mx.sendgrid.net
# + SendGrid Inbound Parse konfigureret til dette domaene.
INBOUND_DOMAIN = os.environ.get('INBOUND_DOMAIN', 'leads.klaai.dk')


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


def opsaml_kontakt(klient_id, email, navn='', telefon='', adresse='', postnummer=''):
    """Rygraden: sikrer at personen findes som central CRM-kontakt.

    Email er den fælles nøgle alle moduler (leads, tilbud, bookinger) binder sammen på.
    Udfylder kun tomme felter — overskriver aldrig data kunden allerede har rettet.
    """
    if not db or str(klient_id) == 'demo':
        return
    email = (email or '').strip().lower()
    if not email or '@' not in email:
        return  # uden email-nøgle kan kontakten ikke bindes til rygraden
    try:
        from datetime import datetime as _dt
        nu = _dt.utcnow().isoformat()
        eksisterende = db.table('crm_kontakter').select('navn,telefon,adresse,postnummer') \
            .eq('klient_id', str(klient_id)).eq('email', email).maybe_single().execute()
        if eksisterende and eksisterende.data:
            d = eksisterende.data
            patch = {'sidst_opdateret': nu}
            if navn and not d.get('navn'): patch['navn'] = navn
            if telefon and not d.get('telefon'): patch['telefon'] = telefon
            if adresse and not d.get('adresse'): patch['adresse'] = adresse
            if postnummer and not d.get('postnummer'): patch['postnummer'] = postnummer
            db.table('crm_kontakter').update(patch) \
                .eq('klient_id', str(klient_id)).eq('email', email).execute()
        else:
            db.table('crm_kontakter').insert({
                'klient_id': str(klient_id), 'email': email,
                'navn': navn, 'telefon': telefon,
                'adresse': adresse, 'postnummer': postnummer,
                'status': 'ny', 'sidst_opdateret': nu
            }).execute()
    except Exception as e:
        print(f"Kontakt-opsamling fejl: {e}")


def log_aktivitet(klient_id, type, titel, kontakt_email='', beloeb=None, reference_id='', modul=''):
    """Skriver én linje i den fælles tidslinje på tværs af moduler.

    Hver hændelse (lead, tilbud sendt/godkendt, booking) logges her, så kundekortet
    og forretnings-overblikket kan vise alt samlet. Fejler aldrig hovedflowet.
    """
    if not db or str(klient_id) == 'demo':
        return
    try:
        db.table('aktivitet').insert({
            'klient_id': str(klient_id),
            'kontakt_email': (kontakt_email or '').strip().lower(),
            'type': type,
            'titel': titel,
            'beloeb': beloeb,
            'reference_id': str(reference_id) if reference_id else None,
            'modul': modul
        }).execute()
    except Exception as e:
        print(f"Aktivitet-log fejl: {e}")


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
            opsaml_kontakt(klient_id, lead_data.get('email', ''), lead_data.get('navn', ''), lead_data.get('telefon', ''))
            log_aktivitet(klient_id, 'lead', f"Nyt lead via chatbot — {lead_data.get('navn','') or 'ukendt'}", lead_data.get('email', ''), modul='leads')
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

Log ind på din Nordolsen portal for at se alle leads."""

    # Send notifikation til klient
    if SENDGRID_API_KEY and notif_mail and '@' in notif_mail:
        sendt = send_mail(notif_mail, emne_klient, tekst_klient, klient_navn)
        if sendt:
            _log_agent('lead_notif', klient_id, lead_navn, f"Lead notifikation sendt til klient: {lead_navn} ({lead_tlf})")
    else:
        _log_agent('lead_notif', klient_id, lead_navn, f"Nyt lead opsamlet: {lead_navn} — {lead_besked[:60]}")

    # Send notifikation til admin (Nordolsen)
    if SENDGRID_API_KEY and ADMIN_EMAIL and '@' in ADMIN_EMAIL:
        emne_admin = f"[Nordolsen] Nyt lead hos {klient_navn} — {lead_navn}"
        tekst_admin = f"""Nyt lead opsamlet via chatbot!

Klient: {klient_navn} ({klient_id})
Lead: {lead_navn}
Telefon: {lead_tlf}
Email: {lead_email}
Interesse: {lead_besked}

Log ind på admin-panelet for at se detaljer:
https://klaiai.onrender.com/app/admin.html"""
        send_mail(ADMIN_EMAIL, emne_admin, tekst_admin, 'Nordolsen')

    # Send SMS-notifikation til klient
    if GATEWAYAPI_TOKEN:
        try:
            k_sms = db.table('klienter').select('telefon,sms_aktiv').eq('id', klient_id).single().execute()
            if k_sms.data and k_sms.data.get('sms_aktiv') and k_sms.data.get('telefon'):
                sms = f"Nyt lead: {lead_navn}"
                if lead_tlf: sms += f" · {lead_tlf}"
                elif lead_email: sms += f" · {lead_email}"
                if lead_besked: sms += f"\n{lead_besked[:60]}"
                send_sms(k_sms.data['telefon'], sms)
        except Exception as _e:
            print(f"SMS-notif fejl: {_e}")

    # Auto-generer og send opfølgningsmails hvis klienten har aktiveret det
    try:
        k_auto = db.table('chatbot_config').select('auto_godkend_mails').eq('klient_id', klient_id).single().execute()
        if k_auto.data and k_auto.data.get('auto_godkend_mails'):
            # Generer og gem lead-mails straks — de sendes af mail_flow_agent
            _log_agent('auto_lead_mail', klient_id, lead_data.get('navn',''), 'Auto-godkendelse aktiv — lead-mails genereres')
    except:
        pass

    # Send automatisk bekræftelses-email til leaden
    if SENDGRID_API_KEY and lead_email and '@' in lead_email:
        try:
            fornavn = lead_navn.split()[0] if lead_navn and lead_navn != 'Ukendt' else 'der'
            fornavn_h     = html.escape(fornavn)
            klient_navn_h = html.escape(klient_navn)
            lead_besked_h = html.escape(lead_besked)
            emne_lead = f"Tak for din henvendelse til {klient_navn} 👋"
            html_lead = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"/></head>
<body style="margin:0;padding:0;background:#f8f7f4;font-family:'Helvetica Neue',Arial,sans-serif">
<table width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:32px 16px">
<table width="560" cellpadding="0" cellspacing="0" style="max-width:560px">
  <tr><td style="background:#0a2463;border-radius:14px 14px 0 0;padding:24px 32px">
    <div style="color:#fff;font-size:20px;font-weight:800">{klient_navn_h}</div>
  </td></tr>
  <tr><td style="background:#fff;padding:28px 32px;border-left:1px solid #e5e3de;border-right:1px solid #e5e3de">
    <div style="font-size:18px;font-weight:700;color:#1a1918;margin-bottom:12px">Hej {fornavn_h}! 👋</div>
    <div style="font-size:14px;color:#555;line-height:1.7;margin-bottom:16px">
      Tak for din henvendelse. Vi har modtaget din besked og vender tilbage til dig hurtigst muligt.
    </div>
    <div style="background:#f0f4ff;border-left:3px solid #0a2463;border-radius:0 10px 10px 0;padding:14px 18px;margin-bottom:16px">
      <div style="font-size:12px;font-weight:700;color:#0a2463;text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px">Din besked</div>
      <div style="font-size:13px;color:#333;line-height:1.6">{lead_besked_h or '(Ingen besked)'}</div>
    </div>
    <div style="font-size:13px;color:#888">Med venlig hilsen,<br><strong>{klient_navn_h}</strong></div>
  </td></tr>
  <tr><td style="background:#f8f7f4;padding:16px 32px;border:1px solid #e5e3de;border-radius:0 0 14px 14px;text-align:center">
    <div style="font-size:11px;color:#bbb">Drevet af Nordolsen</div>
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
        return jsonify({'error': _log_fejl(e)}), 500


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

    # Idempotens: widget'en gensender leads (retry + lokal ko naar backend er nede).
    # dedup_id sikrer at samme henvendelse aldrig bliver til to leads / to mail-flows.
    dedup_id = lead.get('dedup_id')
    if db and dedup_id:
        try:
            allerede = db.table('leads').select('id').eq('klient_id', klient_id).eq('dedup_id', dedup_id).limit(1).execute()
            if allerede.data:
                return jsonify({'success': True, 'duplikat': True, 'lead': lead.get('navn')})
        except Exception as e:
            print(f"Lead dedup-tjek fejl: {e}")

    # Gem lead i Supabase — dette er den kritiske fangst. Fejler den, skal
    # widget'en vide det (503) saa den gemmer lokalt og gensender. ALDRIG sluge fejlen.
    if db:
        try:
            db.table('leads').insert({
                'klient_id': klient_id,
                'navn': lead.get('navn', ''),
                'email': lead.get('email', ''),
                'telefon': lead.get('telefon', ''),
                'virksomhed': lead.get('virksomhed', ''),
                'besked': lead.get('besked', ''),
                'status': 'ny',
                'dedup_id': dedup_id
            }).execute()
        except Exception as e:
            print(f"Lead DB insert fejl: {e}")
            alarm('Lead kunne ikke gemmes',
                  f"Et lead fra '{lead.get('navn','ukendt')}' ({lead.get('email','')}) kunne ikke gemmes i databasen. Widget gensender, men tjek Supabase straks. Fejl: {e}",
                  noegle='lead_insert', klient_id=klient_id)
            return jsonify({'error': 'Kunne ikke gemme lead lige nu', 'retry': True}), 503
        # Sekundaere handlinger maa ikke vaelte fangsten — leadet er allerede gemt.
        try:
            opsaml_kontakt(klient_id, lead.get('email', ''), lead.get('navn', ''), lead.get('telefon', ''))
            log_aktivitet(klient_id, 'lead', f"Nyt lead via formular — {lead.get('navn','') or 'ukendt'}", lead.get('email', ''), modul='leads')
        except Exception as e:
            print(f"Lead opsaml/log fejl: {e}")

    # Mail-generering kalder Claude 3x (langsomt: 10-30s). Det maa ALDRIG holde
    # widget-svaret op — leadet er allerede fanget. Kør det i baggrunden, saa
    # den besoegende faar straks kvittering, og mailene laves bagefter.
    threading.Thread(
        target=_generer_lead_mails,
        args=(klient_id, dict(lead), dedup_id),
        daemon=True
    ).start()

    return jsonify({
        'success': True,
        'lead': lead.get('navn'),
        'mails_i_kø': True
    })


@app.route('/inbound-mail', methods=['POST'])
def inbound_mail():
    """SendGrid Inbound Parse webhook. Kundens info@ videresendes til
    {token}@INBOUND_DOMAIN; token'en i modtager-adressen router til rette klient.
    Zero-loss: mailen gemmes SYNKRONT (fejler det -> 503 saa SendGrid gensender),
    kategorisering + evt. lead-oprettelse koerer i baggrunden."""
    import re, hashlib
    from email.utils import parseaddr

    form = request.form
    # Faktisk modtager: envelope.to er mest paalidelig (RCPT TO), ellers To-headeren.
    modtager = ''
    try:
        env = json.loads(form.get('envelope', '{}'))
        to_liste = env.get('to') or []
        modtager = to_liste[0] if to_liste else ''
    except Exception:
        pass
    if not modtager:
        modtager = form.get('to', '')

    _, modt_adr = parseaddr(modtager)
    token = modt_adr.split('@')[0].strip().lower() if '@' in modt_adr else ''
    klient_id = klient_id_fra_inbound_token(token)
    if not klient_id:
        # Ukendt/forkert-konfigureret adresse: kvittér 200 (undgaa evige gensendinger),
        # men alarmér saa en fejl-opsat videresendelse opdages.
        alarm('Mail til ukendt inbound-adresse',
              f"Modtog en videresendt mail til '{modt_adr}' som ikke matcher nogen klient-token. Tjek kundens videresendelse/Inbound Parse.",
              noegle='inbound_ukendt')
        return jsonify({'ignored': True, 'reason': 'ukendt token'}), 200

    fra_navn, fra_email = parseaddr(form.get('from', ''))
    emne = form.get('subject', '') or ''
    besked = form.get('text', '') or ''
    if not besked:
        # Kun HTML? Fald tilbage til en simpel tekstudgave.
        html = form.get('html', '') or ''
        besked = re.sub(r'<[^>]+>', ' ', html)
        besked = re.sub(r'\s+', ' ', besked).strip()

    # Idempotens: Message-ID hvis muligt, ellers hash af afsender+emne+uddrag.
    dedup_kilde = ''
    try:
        headers = form.get('headers', '') or ''
        m = re.search(r'(?im)^Message-ID:\s*(.+)$', headers)
        dedup_kilde = m.group(1).strip() if m else ''
    except Exception:
        pass
    if not dedup_kilde:
        dedup_kilde = f"{fra_email}|{emne}|{besked[:200]}"
    dedup_id = hashlib.sha256(dedup_kilde.encode('utf-8', 'ignore')).hexdigest()[:32]

    if db:
        # Dedup-tjek: gensendt webhook laver ikke en dublet.
        try:
            findes = (db.table('indbakke_mails').select('id')
                        .eq('klient_id', str(klient_id)).eq('dedup_id', dedup_id)
                        .limit(1).execute())
            if findes.data:
                return jsonify({'success': True, 'duplikat': True}), 200
        except Exception as e:
            print(f"inbound dedup-tjek fejl: {e}")

        # KRITISK FANGST — gem foer alt andet. Fejler den, gensend (503).
        try:
            ny = db.table('indbakke_mails').insert({
                'klient_id': str(klient_id),
                'fra_email': fra_email, 'fra_navn': fra_navn,
                'emne': emne, 'besked': besked,
                'kategori': 'til_gennemsyn', 'status': 'ny',
                'dedup_id': dedup_id
            }).execute()
            row_id = ny.data[0]['id'] if ny.data else None
        except Exception as e:
            print(f"inbound insert fejl: {e}")
            alarm('Videresendt mail kunne ikke gemmes',
                  f"En mail til klient {klient_id} fra {fra_email} kunne ikke gemmes. SendGrid gensender. Fejl: {e}",
                  noegle='inbound_insert', klient_id=klient_id)
            return jsonify({'error': 'kunne ikke gemme', 'retry': True}), 503

        log_aktivitet(str(klient_id), 'indbakke', f"Ny mail modtaget — {fra_navn or fra_email}", fra_email, modul='indbakke')

        # Kategorisering + evt. lead-flow i baggrunden — holder ikke webhooket op.
        threading.Thread(
            target=_behandl_inbound_mail,
            args=(row_id, str(klient_id), fra_navn, fra_email, emne, besked, dedup_id),
            daemon=True
        ).start()

    return jsonify({'success': True}), 200


def _generer_lead_mails(klient_id, lead, dedup_id):
    """Genererer (og evt. sender) de 3 opfoelgningsmails i baggrunden.
    Leadet er allerede gemt foer denne kaldes — fejl her taber ikke leadet."""
    try:
        klient = get_klient(klient_id)
        klient_info = {
            'navn': klient.get('navn', 'Virksomheden'),
            'ydelser': klient.get('info', {}).get('ydelser', ''),
            'tilbud': klient.get('lead_tilbud', 'Gratis uforpligtende samtale'),
            'kontakt': klient.get('info', {}).get('kontakt', '')
        }

        mail_cfg = hent_mail_config(klient_id)
        mails = []
        for nr in [1, 2, 3]:
            mail = generer_lead_mail(lead, klient_info, nr, mail_cfg)
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
                # Hent lead id — praecist via dedup_id naar muligt, ellers fald tilbage til navn
                lead_q = db.table('leads').select('id').eq('klient_id', klient_id)
                lead_q = lead_q.eq('dedup_id', dedup_id) if dedup_id else lead_q.eq('navn', lead.get('navn',''))
                lead_res = lead_q.order('oprettet', desc=True).limit(1).execute()
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
    except Exception as e:
        print(f"Lead mail-generering fejl (lead er gemt): {e}")
        alarm('Lead-mails kunne ikke genereres',
              f"Leadet er gemt, men opfoelgningsmailene fejlede (Claude/SendGrid?). Lead: {lead.get('navn','')} ({lead.get('email','')}). Fejl: {e}",
              noegle='lead_mailgen', klient_id=klient_id)


def hent_mail_config(klient_id):
    """Henter klientens mail-skabelon/stemme-felter. Tomme vaerdier = systemets
    standard bruges, saa mails altid virker uden opsaetning (selvkoerende)."""
    felter = ('mail_stemme', 'mail_signatur', 'lead_mail_fokus', 'tilbud_mail_tekst', 'tilbud_mail_emne')
    tom = {f: '' for f in felter}
    if not db or not klient_id:
        return tom
    try:
        r = db.table('chatbot_config').select(','.join(felter)).eq('klient_id', klient_id).limit(1).execute()
        if r.data:
            return {f: (r.data[0].get(f) or '') for f in felter}
    except Exception as e:
        print(f"hent_mail_config fejl: {e}")
    return tom


def render_tilbud_mailtekst(tekst, kunde_navn, fra_navn):
    """Erstatter pladsholdere i kundens egen tilbuds-mailtekst."""
    return (tekst or '').replace('{kunde_navn}', kunde_navn or 'der').replace('{firma}', fra_navn or '')


def generer_lead_mail(lead, klient, mail_nr, mail_cfg=None):
    mail_cfg = mail_cfg or {}
    stemme   = (mail_cfg.get('mail_stemme') or '').strip()
    signatur = (mail_cfg.get('mail_signatur') or '').strip()
    fokus    = (mail_cfg.get('lead_mail_fokus') or '').strip()

    instruktion = {
        1: 'Dette er den første kontakt. Vær varm og imødekommende. Tak for henvendelsen og beskriv kort hvad virksomheden tilbyder. Afslut med at de er velkomne til at skrive på virksomhedens email eller besøge hjemmesiden hvis de har spørgsmål.',
        2: 'Opfølgning dag 3. Spørg venligt om de har haft mulighed for at kigge nærmere. Fremhæv én konkret fordel ved produktet/ydelsen. Afslut med at de kan skrive eller gå ind på hjemmesiden.',
        3: 'Sidste opfølgning. Gør det personligt og varmt. Afslut positivt — sig at de altid er velkomne til at skrive eller besøge hjemmesiden hvis de på et tidspunkt får lyst.'
    }.get(mail_nr, '')

    # Kundens egen brand-stemme styrer AI'en. Tom = systemets neutrale standard.
    stemme_blok = (f"Skriv i DENNE virksomheds stemme og tone — følg den nøje:\n{stemme}"
                   if stemme else "Tone: professionel men venlig.")
    fokus_blok = f"\nLæg særlig vægt på: {fokus}" if fokus else ""
    signatur_krav = (f"- Afslut mailen med præcis denne underskrift:\n{signatur}"
                     if signatur else "- Afslut naturligt med virksomhedens navn")

    response = ai.messages.create(
        model='claude-sonnet-4-6',
        max_tokens=600,
        messages=[{'role': 'user', 'content': f"""Du skriver opfølgningsmail #{mail_nr} på vegne af en dansk virksomhed.

{stemme_blok}

Lead-navn: {lead.get('navn','der')}
Henvendelse: {lead.get('besked','Generel forespørgsel')}
Virksomhed: {klient['navn']}
Ydelser: {klient.get('ydelser','')}
Kontakt email: {klient.get('kontakt','')}
Hjemmeside: {klient.get('hjemmeside','')}

Instrukser: {instruktion}{fokus_blok}

Krav til mailen:
- Personlig tiltale med leaddets fornavn
- ALDRIG opfordr til at booke møde, ringe eller tage en snak — kun til at skrive på email eller besøge hjemmesiden
- 3-5 linjer brødtekst
- Ingen emojis
- Dansk
{signatur_krav}

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


# ── EMAIL-INGESTION: KATEGORISERING + BEHANDLING ──────────────
INDBAKKE_KATEGORIER = ('nyt_lead', 'booking', 'eksisterende_kunde', 'faktura', 'erhverv', 'spam')

def kategoriser_mail(emne, besked, fra_email):
    """Klassificerer en indkommen mail i ét autonomt spor + to signaler.
    Fail-safe: enhver fejl -> 'til_gennemsyn' + kraever_svar, saa ejeren ser
    mailen og intet nogensinde tabes stille. Bruger Haiku (billigt ved volumen)."""
    fallback = {'kategori': 'til_gennemsyn', 'hot': False, 'kraever_svar': True}
    try:
        resp = ai.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=120,
            messages=[{'role': 'user', 'content': f"""Klassificér denne mail til en dansk virksomheds info-indbakke.

Fra: {fra_email}
Emne: {emne}
Tekst: {(besked or '')[:1500]}

Vælg PRÆCIS én kategori:
- nyt_lead: potentiel NY kunde — interesse, forespørgsel, prisønske
- booking: vil bestille tid, besøg eller aftale
- eksisterende_kunde: support/spørgsmål/reklamation fra en de allerede handler med
- faktura: betaling, faktura, rykker, økonomi
- erhverv: B2B, leverandør, samarbejde, salgshenvendelse TIL virksomheden
- spam: nyhedsbrev, cold sales, irrelevant, automatisk/no-reply

Vurdér også:
- hot: true hvis mailen haster, nævner pris/beløb, eller lyder klar til at købe
- kraever_svar: true hvis afsenderen venter et konkret svar fra virksomheden

Returnér KUN gyldig JSON, intet andet:
{{"kategori":"...","hot":true/false,"kraever_svar":true/false}}"""}]
        )
        raw = resp.content[0].text.strip()
        start, slut = raw.find('{'), raw.rfind('}')
        data = json.loads(raw[start:slut+1]) if start >= 0 else {}
        kat = data.get('kategori', 'til_gennemsyn')
        if kat not in INDBAKKE_KATEGORIER:
            kat = 'til_gennemsyn'
        return {'kategori': kat, 'hot': bool(data.get('hot')), 'kraever_svar': bool(data.get('kraever_svar'))}
    except Exception as e:
        print(f"kategoriser_mail fejl: {e}")
        return fallback


def _behandl_inbound_mail(row_id, klient_id, fra_navn, fra_email, emne, besked, dedup_id):
    """Baggrund: kategorisér mailen, opdatér indbakke-posten, og for lead/booking
    opret et rigtigt lead (kilde=email) saa hele det autonome flow fyrer af."""
    try:
        res = kategoriser_mail(emne, besked, fra_email)
        opdatering = {'kategori': res['kategori'], 'hot': res['hot'], 'kraever_svar': res['kraever_svar']}

        if res['kategori'] in ('nyt_lead', 'booking') and db:
            lead = {
                'navn': fra_navn or (fra_email.split('@')[0] if fra_email else 'Ukendt'),
                'email': fra_email or '',
                'telefon': '',
                'virksomhed': '',
                'besked': (f"{emne}\n\n{besked}" if emne else besked or '').strip(),
                'dedup_id': f"inbound_{dedup_id}"
            }
            try:
                db.table('leads').insert({
                    'klient_id': str(klient_id),
                    'navn': lead['navn'], 'email': lead['email'], 'telefon': '',
                    'virksomhed': '', 'besked': lead['besked'],
                    'kilde': 'email', 'status': 'ny', 'dedup_id': lead['dedup_id']
                }).execute()
                # Find lead-id og link tilbage til indbakke-posten
                lq = (db.table('leads').select('id')
                        .eq('klient_id', str(klient_id)).eq('dedup_id', lead['dedup_id'])
                        .limit(1).execute())
                if lq.data:
                    opdatering['lead_id'] = lq.data[0]['id']
                opsaml_kontakt(str(klient_id), lead['email'], lead['navn'], '')
                log_aktivitet(str(klient_id), 'lead', f"Nyt lead via email — {lead['navn']}", lead['email'], modul='leads')
                # Autonomt opfoelgnings-flow (vi er allerede i baggrundstraad)
                _generer_lead_mails(str(klient_id), dict(lead), lead['dedup_id'])
            except Exception as e:
                print(f"inbound lead-oprettelse fejl: {e}")

        if db:
            db.table('indbakke_mails').update(opdatering).eq('id', row_id).execute()
    except Exception as e:
        print(f"_behandl_inbound_mail fejl: {e}")
        alarm('Indbakke-mail kunne ikke behandles',
              f"En videresendt mail for klient {klient_id} blev gemt men ikke kategoriseret. Fejl: {e}",
              noegle='inbound_behandl', klient_id=klient_id)


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


def generer_tilbud_pdf(html_indhold):
    """Konverterer tilbuds-HTML til PDF bytes via WeasyPrint"""
    try:
        from weasyprint import HTML, CSS
        # Tilføj print-venlig CSS
        ekstra_css = CSS(string="""
            @page { size: A4; margin: 0; }
            body { margin: 0; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
            * { box-sizing: border-box; }
        """)
        pdf_bytes = HTML(string=html_indhold, base_url=None).write_pdf(stylesheets=[ekstra_css])
        return pdf_bytes
    except Exception as e:
        print(f"WeasyPrint PDF fejl: {e}")
        return None


def send_mail(til, emne, tekst, fra_navn, html_content=None, pdf_vedhæft=None, pdf_filnavn='tilbud.pdf'):
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
        if pdf_vedhæft:
            import base64
            from sendgrid.helpers.mail import Attachment, FileContent, FileName, FileType, Disposition
            encoded = base64.b64encode(pdf_vedhæft).decode()
            attachment = Attachment(
                FileContent(encoded),
                FileName(pdf_filnavn),
                FileType('application/pdf'),
                Disposition('attachment')
            )
            message.attachment = attachment
        sg = SendGridAPIClient(SENDGRID_API_KEY)
        response = sg.send(message)
        print(f"SendGrid status: {response.status_code}")
        return response.status_code in [200, 202]
    except Exception as e:
        print(f"SendGrid fejl: {e}")
        if hasattr(e, 'body'):
            print(f"SendGrid body: {e.body}")
        return False


# ── OBSERVABILITY: alarm ved kritiske fejl ──────────────────────
# Et enkeltsystem taaler ikke tavs nedetid — vi skal vide besked FOER kunden
# klager. alarm() sender en mail til driftsansvarlig (ADMIN_EMAIL) naar noget
# kritisk fejler (fx et lead der ikke kan gemmes). Throttlet pr. noegle saa en
# vedvarende fejl ikke spammer indbakken, og aldrig kastende — en alarm der
# fejler maa aldrig vaelte den handling den overvaager.
_alarm_sidst = {}
ALARM_COOLDOWN = 1800  # 30 min pr. noegle

def alarm(emne, detaljer='', noegle=None, klient_id=''):
    """Send driftsalarm til ADMIN_EMAIL. Throttlet, fejlsikker, ikke-blokerende."""
    try:
        print(f"🚨 ALARM: {emne} — {detaljer}")
        noegle = noegle or emne
        nu = _time.time()
        sidst = _alarm_sidst.get(noegle, 0)
        undertrykt = nu - sidst < ALARM_COOLDOWN
        _alarm_sidst[noegle] = nu

        # Persistér altid til historik (også når mailen throttles).
        if db:
            try:
                db.table('agent_log').insert({
                    'agent': 'alarm',
                    'klient_id': klient_id or 'system',
                    'reference_id': None,
                    'besked': f"{emne} :: {detaljer}"[:800]
                }).execute()
            except Exception as e:
                print(f"Alarm-log fejl: {e}")

        if undertrykt:
            return  # samme fejl er allerede meldt inden for cooldown
        if not (SENDGRID_API_KEY and ADMIN_EMAIL and '@' in ADMIN_EMAIL):
            return
        tekst = (
            f"Driftsalarm fra Nordolsen\n\n{emne}\n\n{detaljer}\n\n"
            f"Klient: {klient_id or '-'}\n"
            f"Tidspunkt (UTC): {datetime.utcnow().isoformat(timespec='seconds')}\n\n"
            f"Yderligere alarmer med samme aarsag undertrykkes i {ALARM_COOLDOWN//60} min."
        )
        send_mail(ADMIN_EMAIL, f"[ALARM] {emne}", tekst, 'Nordolsen Drift')
    except Exception as e:
        print(f"Alarm-udsendelse fejl: {e}")


def send_sms(til_nummer, besked):
    """Sender SMS via GatewayAPI (dansk SMS-gateway, ingen ekstra pakker)."""
    if not GATEWAYAPI_TOKEN or not til_nummer:
        return False
    try:
        nummer = ''.join(c for c in str(til_nummer) if c.isdigit())
        if len(nummer) == 8:
            nummer = '45' + nummer
        if len(nummer) < 10:
            return False
        import requests as _req
        resp = _req.post(
            'https://gatewayapi.com/rest/mtsms',
            auth=(GATEWAYAPI_TOKEN, ''),
            json={
                'sender': 'Nordolsen',
                'message': besked[:160],
                'recipients': [{'msisdn': int(nummer)}]
            },
            timeout=10
        )
        print(f"SMS sendt til {nummer}: {resp.status_code}")
        return resp.status_code in [200, 201, 202]
    except Exception as e:
        print(f"SMS fejl: {e}")
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
        return jsonify({'booking_url': '', 'error': _log_fejl(e)}), 200


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
@require_token
def gem_booking_config():
    """Gemmer booking konfiguration"""
    if not db:
        return jsonify({'error': 'Database ikke tilgængelig'}), 500
    data = request.json
    klient_id = data.get('klient_id')
    if not klient_id:
        return jsonify({'error': 'klient_id mangler'}), 400
    if _ingen_adgang(klient_id):
        return jsonify({'error': 'Ingen adgang'}), 403
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
        return jsonify({'error': _log_fejl(e)}), 500


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
        return jsonify({'optaget': [], 'error': _log_fejl(e)})


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
            opsaml_kontakt(klient_id, booking.get('email', ''), booking.get('navn', ''), booking.get('telefon', ''))
            log_aktivitet(klient_id, 'booking', f"Booking — {booking.get('ydelse','') or 'tid'} {booking.get('dato','')} {booking.get('tid','')}".strip(), booking.get('email', ''), modul='bookinger')
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

    # Send SMS-notifikation til klient
    if GATEWAYAPI_TOKEN:
        try:
            k_sms = db.table('klienter').select('telefon,sms_aktiv').eq('id', klient_id).single().execute()
            if k_sms.data and k_sms.data.get('sms_aktiv') and k_sms.data.get('telefon'):
                dato_vis = booking.get('dato', '')
                tid_vis = booking.get('tid', '')
                navn_vis = booking.get('navn', '—')
                tlf_vis = booking.get('telefon', '')
                sms = f"Ny booking: {navn_vis} · {dato_vis} {tid_vis}"
                if tlf_vis: sms += f" · {tlf_vis}"
                send_sms(k_sms.data['telefon'], sms)
        except Exception as _e:
            print(f"SMS-booking fejl: {_e}")

    return jsonify({'success': True, 'booking': booking.get('navn')})


# ── KLIENT PORTAL ENDPOINTS ────────────────────────────

@app.route('/leads/<klient_id>', methods=['GET'])
@require_token
def get_leads(klient_id):
    """Henter leads for en klient"""
    if _ingen_adgang(klient_id):
        return jsonify({'error': 'Ingen adgang'}), 403
    if not db:
        return jsonify({'leads': []})
    try:
        res = db.table('leads').select('*').eq('klient_id', klient_id).order('oprettet', desc=True).execute()
        return jsonify({'leads': res.data or []})
    except Exception as e:
        print(f"get_leads fejl: {e}")
        return jsonify({'leads': [], 'error': 'Kunne ikke hente leads'})


@app.route('/bookinger/<klient_id>', methods=['GET'])
@require_token
def get_bookinger(klient_id):
    """Henter bookinger for en klient"""
    if _ingen_adgang(klient_id):
        return jsonify({'error': 'Ingen adgang'}), 403
    if not db:
        return jsonify({'bookinger': []})
    try:
        res = db.table('bookinger').select('*').eq('klient_id', klient_id).order('dato', desc=False).execute()
        return jsonify({'bookinger': res.data or []})
    except Exception as e:
        print(f"get_bookinger fejl: {e}")
        return jsonify({'bookinger': [], 'error': 'Kunne ikke hente bookinger'})


@app.route('/portal/bookinger', methods=['POST'])
@require_token
def portal_opret_booking():
    """Klientportal: opretter en manuel aftale"""
    body = request.get_json() or {}
    klient_id = str(request.user_klient_id)
    if not body.get('navn') or not body.get('dato'):
        return jsonify({'error': 'navn og dato er påkrævet'}), 400
    if not db:
        return jsonify({'error': 'Ingen database'}), 500
    try:
        row = {
            'klient_id':     klient_id,
            'navn':          body.get('navn', ''),
            'email':         body.get('email', ''),
            'telefon':       body.get('telefon', ''),
            'ydelse':        body.get('ydelse', ''),
            'dato':          body.get('dato', ''),
            'tid':           body.get('tid', ''),
            'besked':        body.get('besked', ''),
            'noter':         body.get('noter', ''),
            'status':        'bekræftet',
            'portal_status': body.get('portal_status', 'bekræftet'),
        }
        if body.get('tilbud_id'):
            row['tilbud_id'] = body['tilbud_id']
        res = db.table('bookinger').insert(row).execute()
        return jsonify({'ok': True, 'booking': res.data[0] if res.data else {}})
    except Exception as e:
        return jsonify({'error': _log_fejl(e)}), 500


@app.route('/portal/bookinger/<booking_id>/status', methods=['PATCH'])
@require_token
def portal_opdater_booking_status(booking_id):
    """Klientportal: opdaterer portal_status på en booking"""
    body = request.get_json() or {}
    ny_status = body.get('portal_status', '')
    gyldige = {'bekræftet', 'afventer_opstart', 'igangværende', 'afsluttet'}
    if ny_status not in gyldige:
        return jsonify({'error': 'Ugyldig status'}), 400
    klient_id = str(request.user_klient_id)
    if not db:
        return jsonify({'error': 'Ingen database'}), 500
    try:
        b = db.table('bookinger').select('klient_id').eq('id', booking_id).maybe_single().execute()
        if not b.data:
            return jsonify({'error': 'Booking ikke fundet'}), 404
        if b.data['klient_id'] != klient_id:
            return jsonify({'error': 'Ingen adgang'}), 403
        db.table('bookinger').update({'portal_status': ny_status}).eq('id', booking_id).execute()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': _log_fejl(e)}), 500


# ── LEAD MAILS PREVIEW & GODKENDELSE ──────────────────

@app.route('/lead-mails/<klient_id>', methods=['GET'])
@require_token
def get_lead_mails(klient_id):
    """Henter afventende lead-mails til preview"""
    if _ingen_adgang(klient_id):
        return jsonify({'error': 'Ingen adgang'}), 403
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
        return jsonify({'grupper': [], 'error': _log_fejl(e)})


@app.route('/godkend-mails', methods=['POST'])
@require_token
def godkend_mails(klient_id=None):
    """Godkender (og sender) lead-mails. Kan også opdatere tekst inden afsendelse."""
    data = request.json
    lead_id = data.get('lead_id')
    klient_id = data.get('klient_id')
    mails = data.get('mails', [])  # [{'id': X, 'emne': ..., 'tekst': ...}]
    auto_fremadrettet = data.get('auto_fremadrettet', False)

    if not db or not klient_id:
        return jsonify({'error': 'Mangler data'}), 400
    if _ingen_adgang(klient_id):
        return jsonify({'error': 'Ingen adgang'}), 403

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
@require_token
def afvis_mails(lead_id):
    """Afviser/sletter afventende mails for et lead"""
    if not db:
        return jsonify({'error': 'Ingen database'}), 500
    try:
        _ejer = db.table('leads').select('klient_id').eq('id', lead_id).single().execute()
        if _ejer.data and _ingen_adgang(_ejer.data.get('klient_id')):
            return jsonify({'error': 'Ingen adgang'}), 403
    except Exception:
        return jsonify({'error': 'Ingen adgang'}), 403
    try:
        db.table('lead_mails').update({'status': 'afvist'}).eq('lead_id', lead_id).eq('status', 'afventer').execute()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': _log_fejl(e)}), 500


# ── CHATBOT GAPS ───────────────────────────────────────

@app.route('/gaps/<klient_id>', methods=['GET'])
@require_token
def get_gaps(klient_id):
    """Henter ubesvarede spørgsmål for en klient"""
    if _ingen_adgang(klient_id):
        return jsonify({'error': 'Ingen adgang'}), 403
    if not db:
        return jsonify({'gaps': []})
    try:
        res = db.table('chatbot_gaps').select('*').eq('klient_id', klient_id).eq('status', 'åben').order('oprettet', desc=True).limit(20).execute()
        return jsonify({'gaps': res.data or []})
    except Exception as e:
        return jsonify({'gaps': [], 'error': _log_fejl(e)})


@app.route('/udfyld-gap/<klient_id>', methods=['POST'])
@require_token
def udfyld_gap(klient_id):
    """Claude genererer forslag til hvad der mangler i chatbot-konfigurationen"""
    if _ingen_adgang(klient_id):
        return jsonify({'error': 'Ingen adgang'}), 403
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
        return jsonify({'error': _log_fejl(e)}), 500


@app.route('/luk-gap/<gap_id>', methods=['POST'])
@require_token
def luk_gap(gap_id):
    """Markerer et gap som lukket/ignoreret"""
    if not db:
        return jsonify({'error': 'Ingen database'}), 500
    try:
        _ejer = db.table('chatbot_gaps').select('klient_id').eq('id', gap_id).single().execute()
        if _ejer.data and _ingen_adgang(_ejer.data.get('klient_id')):
            return jsonify({'error': 'Ingen adgang'}), 403
    except Exception:
        return jsonify({'error': 'Ingen adgang'}), 403
    try:
        db.table('chatbot_gaps').update({'status': 'ignoreret'}).eq('id', gap_id).execute()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': _log_fejl(e)}), 500


# ── AI INSIGHTS & BRANCHE-RESEARCH ────────────────────

@app.route('/insights/<klient_id>', methods=['GET'])
@require_token
def get_insights(klient_id):
    """Analyserer klientens opsætning og returnerer kritiske AI-forbedringer"""
    if _ingen_adgang(klient_id):
        return jsonify({'error': 'Ingen adgang'}), 403
    klient = get_klient(klient_id)
    leads, bookinger = [], []
    if db:
        try:
            leads = db.table('leads').select('oprettet,besked').eq('klient_id', klient_id).execute().data or []
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

    analyse_prompt = f"""Du er en skarp Nordolsen-konsulent. Analyser denne klients AI-opsætning og returner præcis 4-5 kritiske, konkrete forbedringer i JSON.

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
        return jsonify({'error': _log_fejl(e)}), 500


@app.route('/apply-insight/<klient_id>', methods=['POST'])
@require_token
def apply_insight(klient_id):
    """Implementerer et AI-indsigt ved at opdatere chatbot-config"""
    if _ingen_adgang(klient_id):
        return jsonify({'error': 'Ingen adgang'}), 403
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
        return jsonify({'error': _log_fejl(e)}), 500


@app.route('/research-branche/<klient_id>', methods=['POST'])
@require_token
def research_branche(klient_id):
    """Lader Claude researche branchen og beriger ekstra_viden automatisk"""
    if _ingen_adgang(klient_id):
        return jsonify({'error': 'Ingen adgang'}), 403
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
        return jsonify({'error': _log_fejl(e)}), 500


# ── RAPPORT ENDPOINTS ──────────────────────────────────

@app.route('/rapport/<klient_id>', methods=['GET'])
@require_token
def get_rapport(klient_id):
    """Aggregeret rapport-data til grafer"""
    from datetime import datetime, timedelta

    if _ingen_adgang(klient_id):
        return jsonify({'error': 'Ingen adgang'}), 403
    if not db:
        return jsonify({'error': 'Database ikke tilgængelig'}), 500
    try:
        leads_res = db.table('leads').select('oprettet').eq('klient_id', klient_id).execute()
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
        return jsonify({'error': _log_fejl(e)}), 500


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
<html><head><meta charset="UTF-8"/><title>Nordolsen Rapport — {mdr_navn}</title></head>
<body style="margin:0;padding:0;background:#f8f7f4;font-family:'Helvetica Neue',Arial,sans-serif">
<table width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:32px 16px">
<table width="600" cellpadding="0" cellspacing="0" style="max-width:600px">

  <!-- HEADER -->
  <tr><td style="background:#0a2463;border-radius:14px 14px 0 0;padding:28px 36px">
    <div style="color:#fff;font-size:22px;font-weight:800;letter-spacing:-0.5px">Nordolsen</div>
    <div style="color:rgba(255,255,255,.4);font-size:10px;text-transform:uppercase;letter-spacing:1.5px;margin-top:3px">Månedlig klientrapport · {mdr_navn}</div>
  </td></tr>

  <!-- INTRO -->
  <tr><td style="background:#fff;padding:28px 36px;border-left:1px solid #e5e3de;border-right:1px solid #e5e3de">
    <div style="font-size:18px;font-weight:700;color:#1a1918;margin-bottom:6px">Hej, {fornavn}!</div>
    <div style="font-size:13px;color:#9a9590;line-height:1.7">Her er din månedlige rapport fra Nordolsen for <strong>{mdr_navn}</strong>. Her er hvad dit system har lavet for dig.</div>
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
      <div style="font-size:11px;font-weight:700;color:#0a2463;text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px">💡 Nordolsen anbefaler</div>
      <div style="font-size:13px;color:#1a1918;line-height:1.7">{anbefaling}</div>
    </div>
  </td></tr>

  <!-- FOOTER -->
  <tr><td style="background:#fff;padding:24px 36px;border:1px solid #e5e3de;border-radius:0 0 14px 14px;text-align:center">
    <a href="https://klaiai.onrender.com/portal/{klient_id}" style="display:inline-block;background:#0a2463;color:#fff;text-decoration:none;font-size:13px;font-weight:700;padding:12px 28px;border-radius:9px">
      Se din portal →
    </a>
    <div style="font-size:11px;color:#c5c2bc;margin-top:16px">Drevet af Nordolsen · Rapport for {mdr_navn}</div>
  </td></tr>

</table>
</td></tr></table>
</body></html>"""


@app.route('/preview-rapport/<klient_id>', methods=['GET'])
def preview_rapport(klient_id):
    """Returnerer rapport HTML direkte i browser — til forhåndsvisning.

    Kan aabnes i nyt vindue, saa token accepteres baade via Authorization-header
    og som query-param (frontend henter via fetch+blob, saa token ikke laekker i URL)."""
    from flask import Response
    raw = request.headers.get('Authorization', '') or ('Bearer ' + (request.args.get('token') or ''))
    token = raw.replace('Bearer ', '').strip()
    if not _token_ok(token):
        return jsonify({'error': 'Adgang krævet — log ind igen'}), 401
    _info = active_tokens.get(token, {})
    if _info.get('role') == 'client' and str(_info.get('klient_id')) != str(klient_id):
        return jsonify({'error': 'Ingen adgang'}), 403
    klient = get_klient(klient_id)
    klient_navn = klient.get('navn', 'din virksomhed')
    leads, bookinger, gaps, chat_count = _hent_rapport_data(klient_id)
    html = _byg_rapport_html(klient_id, klient_navn, leads, bookinger, gaps=gaps, chat_count=chat_count)
    return Response(html, mimetype='text/html')


@app.route('/send-rapport/<klient_id>', methods=['POST'])
@require_token
def send_rapport(klient_id):
    """Sender en professionel HTML-rapport pr. mail"""
    if _ingen_adgang(klient_id):
        return jsonify({'error': 'Ingen adgang'}), 403
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
    emne = f"Din Nordolsen rapport — {dato_str}"
    if not SENDGRID_API_KEY or not SENDGRID_FROM:
        return jsonify({'success': False, 'error': 'Mail ikke konfigureret'}), 500

    try:
        message = Mail(
            from_email=(SENDGRID_FROM, 'Nordolsen'),
            to_emails=mail_til,
            subject=emne,
            html_content=html
        )
        sg = SendGridAPIClient(SENDGRID_API_KEY)
        res = sg.send(message)
        return jsonify({'success': res.status_code in [200, 202], 'status': res.status_code, 'sendt_til': mail_til})
    except Exception as e:
        return jsonify({'success': False, 'error': _log_fejl(e)}), 500


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
        return jsonify({'error': _log_fejl(e)}), 500


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
        return jsonify({'error': _log_fejl(e)}), 500

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
        return jsonify({'error': _log_fejl(e)}), 500

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
        return jsonify({'error': _log_fejl(e)}), 500


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
        return jsonify({'error': _log_fejl(e)}), 500


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
        return jsonify({'error': _log_fejl(e)}), 500

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
    <div style="font-size:1.8rem;font-weight:900;color:#fff;letter-spacing:-1px">Nordolsen</div>
    <div style="color:rgba(255,255,255,.6);font-size:.9rem;margin-top:.4rem">Din portal er klar 🎉</div>
  </div>

  <div style="background:#fff;border-radius:0 0 14px 14px;padding:2rem;border:1px solid #e5e3de;border-top:none">
    <p style="font-size:1rem;font-weight:700;margin-bottom:.5rem">Hej {fornavn}!</p>
    <p style="color:#4a4845;line-height:1.7;margin-bottom:1.5rem">
      Din Nordolsen-portal er nu klar. Her kan du følge med i dine leads, bookinger og din chatbots aktivitet — alt på ét sted.
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
          <span style="font-weight:700;font-size:.95rem;font-family:monospace;background:#e5e3de;padding:.2rem .5rem;border-radius:5px">{password if password else '(kontakt Nordolsen)'}</span>
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
      Nordolsen · Din samlede IT-løsning
    </p>
  </div>
</div>"""

    try:
        send_mail(email, f'Din Nordolsen-portal er klar, {fornavn}! 🎉', html, 'Nordolsen')
        return jsonify({'success': True, 'sendt_til': email})
    except Exception as e:
        return jsonify({'error': _log_fejl(e, 'Email kunne ikke sendes')}), 500


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
        return jsonify({'error': _log_fejl(e)}), 500


# ── MAIL-SKABELONER (kundens egen stemme) ─────────────────────

@app.route('/portal/mail-config/<klient_id>', methods=['GET'])
@require_token
def portal_hent_mail_config(klient_id):
    """Klientportal: hent kundens mail-skabelon/stemme-felter."""
    raw = request.headers.get('Authorization', '')
    token = raw.replace('Bearer ', '').strip()
    info = active_tokens.get(token, {})
    if info.get('role') == 'client' and info.get('klient_id') != klient_id:
        return jsonify({'error': 'Ingen adgang'}), 403
    return jsonify(hent_mail_config(klient_id))


@app.route('/portal/mail-config', methods=['POST'])
@require_token
def portal_gem_mail_config():
    """Klientportal: gem mail-skabeloner. Partial upsert — roerer ikke chatbot-felterne."""
    if not db:
        return jsonify({'error': 'Ingen database'}), 500
    data = request.json or {}
    klient_id = data.get('klient_id')
    raw = request.headers.get('Authorization', '')
    token = raw.replace('Bearer ', '').strip()
    info = active_tokens.get(token, {})
    if info.get('role') == 'client' and info.get('klient_id') != klient_id:
        return jsonify({'error': 'Ingen adgang'}), 403
    if not klient_id:
        return jsonify({'error': 'klient_id mangler'}), 400
    try:
        cfg = {
            'klient_id': klient_id,
            'mail_stemme':       (data.get('mail_stemme') or '').strip() or None,
            'mail_signatur':     (data.get('mail_signatur') or '').strip() or None,
            'lead_mail_fokus':   (data.get('lead_mail_fokus') or '').strip() or None,
            'tilbud_mail_tekst': (data.get('tilbud_mail_tekst') or '').strip() or None,
            'tilbud_mail_emne':  (data.get('tilbud_mail_emne') or '').strip() or None,
            'opdateret': 'now()'
        }
        db.table('chatbot_config').upsert(cfg, on_conflict='klient_id').execute()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': _log_fejl(e)}), 500


@app.route('/portal/mail-preview', methods=['POST'])
@require_token
def portal_mail_preview():
    """Live forhaandsvisning af en mail i kundens egen stemme — bruger ogsaa
    ugemte felter fra formularen, saa kunden ser effekten med det samme."""
    data = request.json or {}
    klient_id = data.get('klient_id')
    raw = request.headers.get('Authorization', '')
    token = raw.replace('Bearer ', '').strip()
    info = active_tokens.get(token, {})
    if info.get('role') == 'client' and info.get('klient_id') != klient_id:
        return jsonify({'error': 'Ingen adgang'}), 403
    mtype = (data.get('type') or 'lead').strip()
    mail_cfg = {
        'mail_stemme':       data.get('mail_stemme', ''),
        'mail_signatur':     data.get('mail_signatur', ''),
        'lead_mail_fokus':   data.get('lead_mail_fokus', ''),
        'tilbud_mail_tekst': data.get('tilbud_mail_tekst', ''),
        'tilbud_mail_emne':  data.get('tilbud_mail_emne', ''),
    }
    try:
        klient = get_klient(klient_id) if klient_id else {}
        fra_navn = klient.get('navn', 'din virksomhed')
        if mtype == 'tilbud':
            std = f'Hej Anders,\n\nHermed dit tilbud fra {fra_navn}.\n\nTilbuddet er vedhæftet som PDF.'
            tekst = render_tilbud_mailtekst(mail_cfg['tilbud_mail_tekst'], 'Anders', fra_navn) or std
            emne_raw = (mail_cfg['tilbud_mail_emne'] or '').strip()
            emne = emne_raw.replace('{titel}', 'Tagrens 120 m2').replace('{firma}', fra_navn) if emne_raw else 'Tilbud: Tagrens 120 m2'
            return jsonify({'emne': emne, 'tekst': tekst})
        # Lead-mail: generér mail #1 live i kundens stemme med et eksempel-lead.
        sample_lead = {
            'navn': 'Anders Nielsen',
            'besked': 'Hej, jeg vil gerne høre mere om jeres priser og muligheder.',
            'email': 'anders@eksempel.dk'
        }
        klient_info = {
            'navn': fra_navn,
            'ydelser': klient.get('info', {}).get('ydelser', ''),
            'kontakt': klient.get('info', {}).get('kontakt', '')
        }
        mail = generer_lead_mail(sample_lead, klient_info, 1, mail_cfg)
        return jsonify({'emne': mail.get('emne', ''), 'tekst': mail.get('tekst', '')})
    except Exception as e:
        return jsonify({'error': _log_fejl(e)}), 500


def _portal_ejer(klient_id):
    """True hvis den kaldende token ejer klient_id (eller er admin)."""
    raw = request.headers.get('Authorization', '')
    token = raw.replace('Bearer ', '').strip()
    info = active_tokens.get(token, {})
    return not (info.get('role') == 'client' and str(info.get('klient_id')) != str(klient_id))


@app.route('/portal/inbound-adresse/<klient_id>', methods=['GET'])
@require_token
def portal_inbound_adresse(klient_id):
    """Klientportal: den unikke adresse kundens info@ skal videresendes til."""
    if not _portal_ejer(klient_id):
        return jsonify({'error': 'Ingen adgang'}), 403
    return jsonify({'adresse': inbound_adresse(klient_id), 'domain': INBOUND_DOMAIN})


@app.route('/portal/indbakke/<klient_id>', methods=['GET'])
@require_token
def portal_indbakke(klient_id):
    """Klientportal: alle indkomne mails, kategoriseret. Hot + nyeste foerst."""
    if not _portal_ejer(klient_id):
        return jsonify({'error': 'Ingen adgang'}), 403
    if not db:
        return jsonify({'mails': []})
    try:
        r = (db.table('indbakke_mails')
               .select('id,fra_email,fra_navn,emne,besked,kategori,hot,kraever_svar,lead_id,status,modtaget')
               .eq('klient_id', str(klient_id))
               .neq('status', 'arkiveret')
               .order('hot', desc=True)
               .order('modtaget', desc=True)
               .limit(200)
               .execute())
        return jsonify({'mails': r.data or []})
    except Exception as e:
        return jsonify({'error': _log_fejl(e), 'mails': []}), 500


@app.route('/portal/indbakke/<mail_id>/status', methods=['POST'])
@require_token
def portal_indbakke_status(mail_id):
    """Klientportal: markér en indbakke-mail som laest/arkiveret."""
    if not db:
        return jsonify({'error': 'Ingen database'}), 500
    ny_status = (request.json or {}).get('status', 'laest')
    if ny_status not in ('ny', 'laest', 'arkiveret'):
        return jsonify({'error': 'Ugyldig status'}), 400
    try:
        r = db.table('indbakke_mails').select('klient_id').eq('id', mail_id).single().execute()
        if not r.data:
            return jsonify({'error': 'Ikke fundet'}), 404
        if not _portal_ejer(r.data.get('klient_id')):
            return jsonify({'error': 'Ingen adgang'}), 403
        db.table('indbakke_mails').update({'status': ny_status}).eq('id', mail_id).execute()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': _log_fejl(e)}), 500


# ══════════════════════════════════════════════════════════════
#  BRUGERSTYRING — flere brugere pr. virksomhed med roller/adgang
#  Ejer (klienter-login) og admin-under-brugere kan oprette/styre brugere.
#  Medarbejdere ser kun de sektioner chefen har givet dem (haandhaeves
#  server-side i haandhaev_sektions_adgang() ovenfor).
# ══════════════════════════════════════════════════════════════

def _token_info():
    """Token-info for det aktuelle request (rolle, klient_id, bruger_id ...)."""
    raw = request.headers.get('Authorization', '')
    token = raw.replace('Bearer ', '').strip()
    return active_tokens.get(token, {})

def _er_virksomheds_admin(klient_id):
    """True hvis den kaldende bruger må administrere brugere for klient_id:
    Nordolsen-admin, virksomhedens ejer, eller en admin-under-bruger."""
    info = _token_info()
    if info.get('role') == 'admin':
        return True  # Nordolsen-superadmin
    if str(info.get('klient_id')) != str(klient_id):
        return False
    if not info.get('bruger_id'):
        return True  # ejer-login (klienter)
    return info.get('bruger_rolle') in ('ejer', 'admin')

def _aktuel_email():
    """Bedste bud på den kaldende brugers email (til oprettet_af-audit)."""
    info = _token_info()
    if info.get('role') == 'admin':
        return os.environ.get('ADMIN_EMAIL', '')
    try:
        if info.get('bruger_id'):
            r = db.table('portal_brugere').select('email').eq('id', info['bruger_id']).single().execute()
            return (r.data or {}).get('email', '')
        r = db.table('klienter').select('email').eq('id', info.get('klient_id')).single().execute()
        return (r.data or {}).get('email', '')
    except:
        return ''

@app.route('/portal/mig', methods=['GET'])
@require_token
def portal_mig():
    """Frontend spørger 'hvem er jeg' for at kunne skjule sektioner uden adgang.
    adgang='alle' = fuld adgang (ejer/admin); ellers en liste af sektions-nøgler."""
    info = _token_info()
    if info.get('role') == 'admin':
        return jsonify({'rolle': 'ejer', 'adgang': 'alle', 'navn': 'Nordolsen', 'email': '', 'klient_id': info.get('klient_id')})
    bruger_id = info.get('bruger_id')
    if not bruger_id:
        navn, email = '', ''
        try:
            r = db.table('klienter').select('navn, email').eq('id', info.get('klient_id')).single().execute()
            if r.data:
                navn, email = r.data.get('navn', ''), r.data.get('email', '')
        except:
            pass
        return jsonify({'rolle': 'ejer', 'adgang': 'alle', 'navn': navn, 'email': email, 'klient_id': info.get('klient_id')})
    # Under-bruger — hent live så nyeste rolle/adgang bruges
    try:
        r = db.table('portal_brugere').select('navn, email, rolle, adgang, aktiv').eq('id', bruger_id).single().execute()
        b = r.data or {}
        rolle = b.get('rolle', 'medarbejder')
        adgang = 'alle' if rolle in ('ejer', 'admin') else (b.get('adgang') or [])
        return jsonify({'rolle': rolle, 'adgang': adgang, 'navn': b.get('navn', ''), 'email': b.get('email', ''), 'klient_id': info.get('klient_id')})
    except:
        return jsonify({'rolle': 'medarbejder', 'adgang': info.get('adgang') or [], 'navn': '', 'email': '', 'klient_id': info.get('klient_id')})

@app.route('/portal/brugere/<klient_id>', methods=['GET'])
@require_token
def portal_brugere_liste(klient_id):
    """Alle brugere i virksomheden: ejeren (klienter-login) + under-brugere."""
    if not _er_virksomheds_admin(klient_id):
        return jsonify({'error': 'Kun administratorer har adgang'}), 403
    brugere = []
    try:
        r = db.table('klienter').select('navn, email').eq('id', klient_id).single().execute()
        if r.data:
            brugere.append({'id': 'ejer', 'navn': r.data.get('navn') or 'Ejer', 'email': r.data.get('email', ''),
                            'rolle': 'ejer', 'adgang': 'alle', 'aktiv': True, 'kan_redigeres': False})
    except:
        pass
    try:
        r = (db.table('portal_brugere')
               .select('id, navn, email, rolle, adgang, aktiv, oprettet')
               .eq('klient_id', str(klient_id)).order('oprettet').execute())
        for b in (r.data or []):
            brugere.append({'id': b['id'], 'navn': b.get('navn', ''), 'email': b.get('email', ''),
                            'rolle': b.get('rolle', 'medarbejder'), 'adgang': b.get('adgang') or [],
                            'aktiv': b.get('aktiv', True), 'kan_redigeres': True})
    except Exception as e:
        return jsonify({'error': _log_fejl(e), 'brugere': brugere}), 500
    return jsonify({'brugere': brugere, 'sektioner': PORTAL_SEKTIONER})

@app.route('/portal/brugere', methods=['POST'])
@require_token
def portal_brugere_opret():
    """Opret en ny under-bruger. Kun ejer/admin."""
    if not db:
        return jsonify({'error': 'Ingen database'}), 500
    data = request.json or {}
    klient_id = str(data.get('klient_id') or '')
    if not _er_virksomheds_admin(klient_id):
        return jsonify({'error': 'Kun administratorer har adgang'}), 403
    navn = (data.get('navn') or '').strip()
    email = (data.get('email') or '').strip().lower()
    password = data.get('password') or ''
    rolle = data.get('rolle', 'medarbejder')
    if rolle not in ('admin', 'medarbejder'):
        rolle = 'medarbejder'
    adgang = [s for s in (data.get('adgang') or []) if s in PORTAL_SEKTIONER] if rolle == 'medarbejder' else []
    if not email or '@' not in email:
        return jsonify({'error': 'Ugyldig email'}), 400
    if len(password) < 6:
        return jsonify({'error': 'Adgangskoden skal være mindst 6 tegn'}), 400
    # Email må ikke kollidere med en ejer-login eller en anden under-bruger.
    try:
        if db.table('klienter').select('id').eq('email', email).execute().data:
            return jsonify({'error': 'Emailen er allerede i brug'}), 409
        if db.table('portal_brugere').select('id').eq('email', email).execute().data:
            return jsonify({'error': 'Emailen er allerede i brug'}), 409
    except:
        pass
    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    try:
        db.table('portal_brugere').insert({
            'klient_id': klient_id, 'navn': navn, 'email': email, 'password': pw_hash,
            'rolle': rolle, 'adgang': adgang, 'aktiv': True, 'oprettet_af': _aktuel_email()
        }).execute()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': _log_fejl(e)}), 500

def _hent_bruger_ejet(bruger_id):
    """Hent en under-bruger og verificér at kalderen må administrere den.
    Returnerer (bruger_row, None) ved ok, ellers (None, (json, status))."""
    if not db:
        return None, (jsonify({'error': 'Ingen database'}), 500)
    try:
        r = db.table('portal_brugere').select('id, klient_id, email, rolle, adgang, aktiv').eq('id', bruger_id).single().execute()
    except:
        return None, (jsonify({'error': 'Ikke fundet'}), 404)
    if not r.data:
        return None, (jsonify({'error': 'Ikke fundet'}), 404)
    if not _er_virksomheds_admin(r.data.get('klient_id')):
        return None, (jsonify({'error': 'Kun administratorer har adgang'}), 403)
    return r.data, None

@app.route('/portal/brugere/<bruger_id>', methods=['PATCH'])
@require_token
def portal_brugere_opdater(bruger_id):
    """Redigér navn/rolle/adgang/aktiv for en under-bruger. Kun ejer/admin.
    Aktive sessioner for brugeren fjernes, så ændringen får øjeblikkelig effekt."""
    bruger, fejl = _hent_bruger_ejet(bruger_id)
    if fejl:
        return fejl
    data = request.json or {}
    opdat = {}
    if 'navn' in data:
        opdat['navn'] = (data.get('navn') or '').strip()
    if 'rolle' in data:
        rolle = data.get('rolle')
        if rolle in ('admin', 'medarbejder'):
            opdat['rolle'] = rolle
            if rolle == 'admin':
                opdat['adgang'] = []  # admin har fuld adgang, adgangsliste er irrelevant
    if 'adgang' in data:
        # Kun relevant for medarbejdere; filtrér til gyldige sektioner.
        maal_rolle = opdat.get('rolle', bruger.get('rolle'))
        if maal_rolle == 'medarbejder':
            opdat['adgang'] = [s for s in (data.get('adgang') or []) if s in PORTAL_SEKTIONER]
    if 'aktiv' in data:
        opdat['aktiv'] = bool(data.get('aktiv'))
    if not opdat:
        return jsonify({'success': True})
    try:
        db.table('portal_brugere').update(opdat).eq('id', bruger_id).execute()
        _slet_brugers_sessions(bruger_id)  # tving genlogin med ny adgang
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': _log_fejl(e)}), 500

@app.route('/portal/brugere/<bruger_id>/kode', methods=['POST'])
@require_token
def portal_brugere_nulstil_kode(bruger_id):
    """Sæt en ny adgangskode for en under-bruger. Kun ejer/admin."""
    bruger, fejl = _hent_bruger_ejet(bruger_id)
    if fejl:
        return fejl
    password = (request.json or {}).get('password') or ''
    if len(password) < 6:
        return jsonify({'error': 'Adgangskoden skal være mindst 6 tegn'}), 400
    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    try:
        db.table('portal_brugere').update({'password': pw_hash}).eq('id', bruger_id).execute()
        _slet_brugers_sessions(bruger_id)  # log ud af gamle sessioner
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': _log_fejl(e)}), 500

@app.route('/portal/brugere/<bruger_id>', methods=['DELETE'])
@require_token
def portal_brugere_slet(bruger_id):
    """Slet en under-bruger. Kun ejer/admin."""
    bruger, fejl = _hent_bruger_ejet(bruger_id)
    if fejl:
        return fejl
    try:
        db.table('portal_brugere').delete().eq('id', bruger_id).execute()
        _slet_brugers_sessions(bruger_id)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': _log_fejl(e)}), 500


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
        return jsonify({'error': _log_fejl(e)}), 500


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
        return jsonify({'error': _log_fejl(e)}), 400

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
                    send_mail(kr.data['email'], 'Betalingsproblem med dit Nordolsen abonnement',
                        f"Hej {kr.data['navn']},\n\nVi kunne ikke trække betaling for dit abonnement. Opdater din betalingsmetode inden 7 dage for at undgå deaktivering.\n\nhttps://klaiai.dk/login\n\nNordolsen", 'Nordolsen')
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
        return jsonify({'error': _log_fejl(e)}), 500


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
            return jsonify({'error': _log_fejl(e, 'Kunne ikke oprette klient')}), 500

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
            return jsonify({'klient_id': klient_id, 'checkout_url': None, 'stripe_fejl': _log_fejl(e)})

    # Uden Stripe – aktiver direkte (test mode)
    if db:
        db.table('klienter').update({'aktiv': True, 'status': 'aktiv'}).eq('id', klient_id).execute()
    return jsonify({'klient_id': klient_id, 'checkout_url': None})


# ── SCAN JOBS (in-memory) ─────────────────────────────
scan_jobs = {}  # job_id -> {'status': 'running'/'done'/'error', 'data': ..., 'meta': ...}

HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; Nordolsen-scanner/1.0)'}

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


def _sikker_ekstern_url(url):
    """SSRF-værn: True kun hvis url er en offentlig http/https-adresse.

    Afviser interne/loopback/link-local/metadata-adresser (fx 169.254.169.254,
    localhost, 10.x, 192.168.x, 172.16-31.x) så scan-funktionerne ikke kan narres
    til at hente interne tjenester eller cloud-metadata. Slår ALLE IP'er op for
    værtsnavnet og afviser hvis blot én er intern.
    """
    import ipaddress as _ipaddr, socket as _socket
    from urllib.parse import urlparse as _urlparse
    try:
        p = _urlparse(url)
    except Exception:
        return False
    if p.scheme not in ('http', 'https') or not p.hostname:
        return False
    try:
        infos = _socket.getaddrinfo(p.hostname, None)
    except Exception:
        return False
    for info in infos:
        ip_str = info[4][0]
        try:
            ip = _ipaddr.ip_address(ip_str)
        except Exception:
            return False
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return False
    return True


def hent_raa_soup(url, timeout=10):
    """Henter en side — prøver direkte, derefter Google Cache som fallback"""
    if not _sikker_ekstern_url(url):
        return None
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
    if not _sikker_ekstern_url(pdf_url):
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
        print(f"scan_job fejl: {traceback.format_exc()[-800:]}")
        scan_jobs[job_id] = {'status': 'error', 'error': 'Skanningen fejlede'}


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
@limiter.limit("10 per minute; 40 per hour")
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
@limiter.limit("10 per minute; 40 per hour")
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
            from_email=(SENDGRID_FROM, 'Nordolsen Test'),
            to_emails=til,
            subject='Nordolsen test mail',
            plain_text_content='Denne mail bekræfter at Nordolsen mail-systemet virker.',
        )
        sg = SendGridAPIClient(SENDGRID_API_KEY)
        response = sg.send(message)
        return jsonify({'sendt': True, 'status': response.status_code, 'fra': SENDGRID_FROM, 'til': til})
    except Exception as e:
        print(f"test-mail fejl: {e} | body: {getattr(e, 'body', '')}")
        return jsonify({'sendt': False, 'fejl': 'Test-mail kunne ikke sendes'}), 500

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
    admin_pw = ADMIN_PASSWORD  # INGEN fallback — tom => admin-login umuligt

    # Admin login (kun muligt hvis ADMIN_PASSWORD er sat i miljøet)
    if admin_pw and email == admin_email and password == admin_pw:
        # 2FA: er ADMIN_TOTP_SECRET sat, kraeves ogsaa en gyldig 6-cifret kode.
        # Password er allerede verificeret her, saa 'totp_required' laekker kun
        # at password var korrekt — angriberen mangler stadig koden.
        if ADMIN_TOTP_SECRET:
            totp = (data.get('totp') or '').strip()
            if not totp:
                return jsonify({'totp_required': True}), 200
            if not _totp_verify(ADMIN_TOTP_SECRET, totp):
                return jsonify({'error': 'Forkert 2FA-kode', 'totp_required': True}), 401
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
                    return jsonify({'error': 'Adgang er deaktiveret. Kontakt Nordolsen.'}), 403
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

        # Under-bruger login — medarbejdere/admins oprettet af virksomhedens ejer.
        # Samme portal som ejeren (role='client'), men token bærer rolle + adgang.
        try:
            bres = db.table('portal_brugere').select(
                'id, klient_id, navn, email, password, rolle, adgang, aktiv'
            ).eq('email', email).single().execute()
            if bres.data:
                bruger = bres.data
                if not bruger.get('aktiv', True):
                    return jsonify({'error': 'Din adgang er deaktiveret. Kontakt din chef.'}), 403
                # Er virksomheden selv aktiv?
                if not er_klient_aktiv(bruger['klient_id']):
                    return jsonify({'error': 'Adgang er deaktiveret. Kontakt Nordolsen.'}), 403
                bruger_pw = bruger.get('password', '')
                b_ok = False
                if bruger_pw and (bruger_pw.startswith('$2b$') or bruger_pw.startswith('$2a$')):
                    try:
                        b_ok = bcrypt.checkpw(password.encode(), bruger_pw.encode())
                    except:
                        b_ok = False
                if b_ok:
                    token = secrets.token_hex(32)
                    _gem_token(token, {
                        'role': 'client',
                        'klient_id': bruger['klient_id'],
                        'bruger_id': bruger['id'],
                        'bruger_rolle': bruger.get('rolle', 'medarbejder'),
                        'adgang': bruger.get('adgang') or [],
                        'created_at': _time.time()
                    })
                    return jsonify({'token': token, 'role': 'client', 'klient_id': bruger['klient_id']})
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

@app.route('/nexolsen-admin', methods=['GET'])
def admin_login_page():
    """Separat admin-login URL — ikke linket fra forsiden"""
    from flask import send_from_directory
    app_dir = os.path.join(os.path.dirname(__file__), '..', 'app')
    return send_from_directory(app_dir, 'admin.html')

# ══════════════════════════════════════════════════════════════════
#  E-CONOMIC INTEGRATION
#  REST API: https://restapi.e-conomic.com
#  Auth: X-AppSecretToken (vores app) + X-AgreementGrantToken (klientens)
#
#  SQL migration (kør én gang i Supabase):
#  CREATE TABLE IF NOT EXISTS klient_integrationer (
#    id SERIAL PRIMARY KEY,
#    klient_id TEXT NOT NULL UNIQUE,
#    economic_token TEXT,
#    economic_navn TEXT,
#    opdateret TIMESTAMPTZ DEFAULT NOW()
#  );
#  ALTER TABLE tilbud ADD COLUMN IF NOT EXISTS economic_faktura_nr INTEGER;
#  ALTER TABLE tilbud ADD COLUMN IF NOT EXISTS economic_synced TIMESTAMPTZ;
# ══════════════════════════════════════════════════════════════════

ECONOMIC_APP_SECRET = os.environ.get('ECONOMIC_APP_SECRET', '')
ECONOMIC_BASE = 'https://restapi.e-conomic.com'

def _economic_headers(agreement_token):
    return {
        'X-AppSecretToken': ECONOMIC_APP_SECRET,
        'X-AgreementGrantToken': agreement_token,
        'Content-Type': 'application/json'
    }

def _hent_economic_token(klient_id):
    try:
        res = supabase.table('klient_integrationer')\
            .select('economic_token')\
            .eq('klient_id', str(klient_id))\
            .maybe_single()\
            .execute()
        return res.data.get('economic_token') if res.data else None
    except:
        return None

@app.route('/econ/connect/<klient_id>', methods=['POST'])
@require_token
def econ_connect(klient_id):
    """Gem og valider e-conomic Agreement Grant Token"""
    if _ingen_adgang(klient_id):
        return jsonify({'error': 'Ikke adgang'}), 403
    data = request.json or {}
    token = data.get('token', '').strip()
    if not token:
        return jsonify({'error': 'Token mangler'}), 400
    if not ECONOMIC_APP_SECRET:
        return jsonify({'error': 'e-conomic App Secret ikke konfigureret på serveren'}), 503
    # Validér token mod e-conomic
    try:
        r = http_requests.get(f'{ECONOMIC_BASE}/self',
            headers=_economic_headers(token), timeout=8)
        if r.status_code != 200:
            return jsonify({'error': 'Ugyldigt token — tjek dit Agreement Grant Token i e-conomic'}), 400
        info = r.json()
        virksomhed = info.get('company', {}).get('name', '')
    except Exception as e:
        return jsonify({'error': _log_fejl(e, 'Kunne ikke forbinde')}), 502
    from datetime import datetime as _dt
    supabase.table('klient_integrationer').upsert({
        'klient_id': str(klient_id),
        'economic_token': token,
        'economic_navn': virksomhed,
        'opdateret': _dt.utcnow().isoformat()
    }, on_conflict='klient_id').execute()
    return jsonify({'ok': True, 'virksomhed': virksomhed})

@app.route('/econ/status/<klient_id>', methods=['GET'])
@require_token
def econ_status(klient_id):
    """Check om e-conomic er forbundet og token virker"""
    if _ingen_adgang(klient_id):
        return jsonify({'error': 'Ikke adgang'}), 403
    token = _hent_economic_token(klient_id)
    if not token:
        return jsonify({'forbundet': False})
    try:
        r = http_requests.get(f'{ECONOMIC_BASE}/self',
            headers=_economic_headers(token), timeout=8)
        if r.status_code == 200:
            info = r.json()
            return jsonify({'forbundet': True,
                'virksomhed': info.get('company', {}).get('name', '')})
    except:
        pass
    return jsonify({'forbundet': False, 'fejl': 'Token virker ikke — tilslut igen'})

@app.route('/econ/disconnect/<klient_id>', methods=['DELETE'])
@require_token
def econ_disconnect(klient_id):
    """Fjern e-conomic forbindelse"""
    if _ingen_adgang(klient_id):
        return jsonify({'error': 'Ikke adgang'}), 403
    supabase.table('klient_integrationer').delete()\
        .eq('klient_id', str(klient_id)).execute()
    return jsonify({'ok': True})

@app.route('/econ/sync-tilbud/<tilbud_id>', methods=['POST'])
@require_token
def econ_sync_tilbud(tilbud_id):
    """Synkroniser et tilbud til e-conomic som kladde-faktura"""
    from datetime import datetime as _dt
    # Hent tilbud
    t_res = supabase.table('tilbud').select('*').eq('id', tilbud_id).maybe_single().execute()
    if not t_res.data:
        return jsonify({'error': 'Tilbud ikke fundet'}), 404
    t = t_res.data
    klient_id = t.get('klient_id')
    if _ingen_adgang(klient_id):
        return jsonify({'error': 'Ikke adgang'}), 403
    token = _hent_economic_token(klient_id)
    if not token:
        return jsonify({'error': 'e-conomic ikke forbundet — gå til Indstillinger'}), 400
    hdrs = _economic_headers(token)
    # Find eller opret kunde
    kunde_email = t.get('email', '')
    kunde_navn  = t.get('kunde_navn', 'Ukendt kunde')
    econ_kunde_nr = None
    if kunde_email:
        _email_q = urllib.parse.quote(kunde_email, safe='')
        s = http_requests.get(f'{ECONOMIC_BASE}/customers?filter=email$eq:{_email_q}',
            headers=hdrs, timeout=8)
        if s.status_code == 200:
            kol = s.json().get('collection', [])
            if kol:
                econ_kunde_nr = kol[0].get('customerNumber')
    if not econ_kunde_nr:
        ny = {'name': kunde_navn, 'email': kunde_email,
              'customerGroup': {'customerGroupNumber': 1},
              'currency': 'DKK',
              'vatZone': {'vatZoneNumber': 1},
              'paymentTerms': {'paymentTermsNumber': 1}}
        cr = http_requests.post(f'{ECONOMIC_BASE}/customers', headers=hdrs, json=ny, timeout=8)
        if cr.status_code in (200, 201):
            econ_kunde_nr = cr.json().get('customerNumber')
        else:
            return jsonify({'error': f'Kunne ikke oprette kunde i e-conomic: {cr.text}'}), 502
    # Byg faktura-linjer
    linjer_raw = t.get('linjer') or t.get('poster') or []
    if isinstance(linjer_raw, str):
        try: linjer_raw = json.loads(linjer_raw)
        except: linjer_raw = []
    faktura_linjer = []
    for i, linje in enumerate(linjer_raw):
        beskr = linje.get('beskrivelse') or linje.get('navn') or linje.get('ydelse', 'Ydelse')
        antal = float(linje.get('antal') or linje.get('mængde') or 1)
        epris = float(linje.get('enhedspris') or linje.get('pris') or 0)
        faktura_linjer.append({'lineNumber': i+1, 'description': beskr,
            'quantity': antal, 'unitNetPrice': epris, 'unit': {'unitNumber': 1}})
    if not faktura_linjer:
        total = float(t.get('total_ekskl_moms') or t.get('beloeb') or 0)
        faktura_linjer = [{'lineNumber': 1,
            'description': t.get('opgave_beskrivelse') or 'Se vedhæftet tilbud',
            'quantity': 1, 'unitNetPrice': total, 'unit': {'unitNumber': 1}}]
    # Opret kladde-faktura
    kladde = {
        'date': _dt.utcnow().strftime('%Y-%m-%d'),
        'currency': 'DKK',
        'paymentTerms': {'paymentTermsNumber': 1},
        'customer': {'customerNumber': econ_kunde_nr},
        'recipient': {'name': kunde_navn, 'vatZone': {'vatZoneNumber': 1}},
        'notes': {'heading': f"Tilbud {t.get('tilbud_nr', tilbud_id[:8].upper())}",
                  'textLine1': t.get('opgave_beskrivelse', '')},
        'lines': faktura_linjer
    }
    resp = http_requests.post(f'{ECONOMIC_BASE}/invoices/drafts',
        headers=hdrs, json=kladde, timeout=10)
    if resp.status_code in (200, 201):
        faktura_nr = resp.json().get('draftInvoiceNumber')
        supabase.table('tilbud').update({
            'economic_faktura_nr': faktura_nr,
            'economic_synced': _dt.utcnow().isoformat()
        }).eq('id', tilbud_id).execute()
        return jsonify({'ok': True, 'faktura_nr': faktura_nr})
    return jsonify({'error': f'e-conomic fejl: {resp.text}'}), 502

@app.route('/admin/impersonate/<klient_id>', methods=['POST'])
@require_admin
def admin_impersonate(klient_id):
    """Admin: generer et midlertidigt portal-token og returner login-URL"""
    if not db:
        return jsonify({'error': 'Ingen database'}), 500
    try:
        k = db.table('klienter').select('id,navn,aktiv').eq('id', klient_id).maybe_single().execute()
        if not k.data:
            return jsonify({'error': 'Klient ikke fundet'}), 404
        import secrets
        token = secrets.token_urlsafe(32)
        _gem_token(token, {
            'role': 'client',
            'klient_id': klient_id,
            'created_at': _time.time(),
            'impersonated': True,
        })
        url = f'/portal?id={klient_id}&token={token}'
        return jsonify({'ok': True, 'url': url, 'token': token, 'klient_id': klient_id})
    except Exception as e:
        return jsonify({'error': _log_fejl(e)}), 500


@app.route('/admin/health-scores', methods=['GET'])
@require_admin
def admin_health_scores():
    """Admin: beregn health score per klient baseret på aktivitet"""
    if not db:
        return jsonify([])
    import datetime as _dt
    nu = _dt.datetime.utcnow()
    dag14 = (nu - _dt.timedelta(days=14)).isoformat()
    dag30 = (nu - _dt.timedelta(days=30)).isoformat()
    dag7  = (nu - _dt.timedelta(days=7)).isoformat()

    try:
        klienter_res = db.table('klienter').select('id,navn,aktiv,email').order('navn').execute()
        klienter = klienter_res.data or []

        # Hent aktivitet på tværs af alle klienter
        tilbud_res    = db.table('tilbud').select('klient_id,oprettet,status').gte('oprettet', dag30).execute()
        leads_res     = db.table('leads').select('klient_id,oprettet').gte('oprettet', dag30).execute()
        bookinger_res = db.table('bookinger').select('klient_id,oprettet').gte('oprettet', dag30).execute()

        # Gruppér per klient
        from collections import defaultdict
        tilbud_pr_klient   = defaultdict(list)
        leads_pr_klient    = defaultdict(list)
        bookinger_pr_klient = defaultdict(list)

        for t in (tilbud_res.data or []):
            tilbud_pr_klient[t['klient_id']].append(t)
        for l in (leads_res.data or []):
            leads_pr_klient[l['klient_id']].append(l)
        for b in (bookinger_res.data or []):
            bookinger_pr_klient[b['klient_id']].append(b)

        result = []
        for k in klienter:
            kid = k['id']
            tilbud   = tilbud_pr_klient[kid]
            leads    = leads_pr_klient[kid]
            bookinger = bookinger_pr_klient[kid]

            # Find seneste aktivitet (proxy for "sidst logget ind")
            alle_tidsstempler = (
                [t['oprettet'] for t in tilbud if t.get('oprettet')] +
                [l['oprettet'] for l in leads if l.get('oprettet')] +
                [b['oprettet'] for b in bookinger if b.get('oprettet')]
            )
            sidst_aktiv = max(alle_tidsstempler) if alle_tidsstempler else None

            # Dage siden sidst aktiv
            if sidst_aktiv:
                sidst_dt = _dt.datetime.fromisoformat(sidst_aktiv.replace('Z',''))
                dage_siden = (nu - sidst_dt).days
            else:
                dage_siden = 999

            # Tilbud seneste 7 dage
            tilbud_7d = [t for t in tilbud if t.get('oprettet', '') >= dag7]
            tilbud_14d = [t for t in tilbud if t.get('oprettet', '') >= dag14]
            leads_7d  = [l for l in leads if l.get('oprettet', '') >= dag7]

            # Health score beregning (0-100)
            score = 100

            # Inaktivitet
            if dage_siden > 30:   score -= 40
            elif dage_siden > 14: score -= 25
            elif dage_siden > 7:  score -= 10

            # Ingen tilbud seneste 30 dage
            if not tilbud:        score -= 20
            elif not tilbud_14d:  score -= 10

            # Ingen leads seneste 14 dage (proxy: chatbot aktiv?)
            if not leads:         score -= 15
            elif not leads_7d:    score -= 5

            # Klient inaktiv i systemet
            if not k.get('aktiv'): score = max(score - 20, 0)

            score = max(0, min(100, score))

            # Kategori
            if score >= 75:   kategori = 'sund'
            elif score >= 45: kategori = 'advarsel'
            else:             kategori = 'risiko'

            # Churn signal
            churn_signal = dage_siden > 21 and len(tilbud) == 0

            result.append({
                'klient_id':    kid,
                'navn':         k.get('navn', ''),
                'email':        k.get('email', ''),
                'aktiv':        k.get('aktiv', True),
                'score':        score,
                'kategori':     kategori,
                'churn_signal': churn_signal,
                'dage_siden':   dage_siden if dage_siden < 999 else None,
                'sidst_aktiv':  sidst_aktiv,
                'tilbud_30d':   len(tilbud),
                'tilbud_7d':    len(tilbud_7d),
                'leads_30d':    len(leads),
                'leads_7d':     len(leads_7d),
                'bookinger_30d': len(bookinger),
            })

        # Sorter: risiko øverst, derefter advarsel, sund sidst
        sort_order = {'risiko': 0, 'advarsel': 1, 'sund': 2}
        result.sort(key=lambda x: (sort_order.get(x['kategori'], 3), -(x['score'] * -1)))
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': _log_fejl(e)}), 500


@app.route('/admin/onboarding-status', methods=['GET'])
@require_admin
def admin_onboarding_status():
    """Admin: vis onboarding-fremgang per klient baseret på DB-signaler"""
    if not db:
        return jsonify([])
    try:
        klienter_res = db.table('klienter').select('id,navn,email,hjemmeside,aktiv,oprettet').order('oprettet', desc=True).execute()
        klienter = klienter_res.data or []

        # Hent alle tilbud, leads, bookinger (kun id + klient_id + status)
        tilbud_res    = db.table('tilbud').select('klient_id,status,oprettet').execute()
        leads_res     = db.table('leads').select('klient_id,oprettet').execute()
        bookinger_res = db.table('bookinger').select('klient_id,oprettet').execute()

        from collections import defaultdict
        tilbud_pr = defaultdict(list)
        leads_pr  = defaultdict(list)
        book_pr   = defaultdict(list)

        for t in (tilbud_res.data or []):
            tilbud_pr[t['klient_id']].append(t)
        for l in (leads_res.data or []):
            leads_pr[l['klient_id']].append(l)
        for b in (bookinger_res.data or []):
            book_pr[b['klient_id']].append(b)

        result = []
        for k in klienter:
            kid = k['id']
            tilbud    = tilbud_pr[kid]
            leads     = leads_pr[kid]
            bookinger = book_pr[kid]

            sendt_tilbud = [t for t in tilbud if t.get('status') in ('sendt', 'accepteret', 'afvist')]

            trin = {
                'klient_oprettet':    True,
                'hjemmeside_sat':     bool(k.get('hjemmeside')),
                'widget_installeret': len(leads) > 0,
                'forste_lead':        len(leads) > 0,
                'forste_tilbud':      len(tilbud) > 0,
                'tilbud_sendt':       len(sendt_tilbud) > 0,
                'forste_booking':     len(bookinger) > 0,
            }

            done  = sum(1 for v in trin.values() if v)
            total = len(trin)
            pct   = round(done / total * 100)

            result.append({
                'klient_id':  kid,
                'navn':       k.get('navn', ''),
                'email':      k.get('email', ''),
                'aktiv':      k.get('aktiv', True),
                'oprettet':   k.get('oprettet', ''),
                'trin':       trin,
                'done':       done,
                'total':      total,
                'pct':        pct,
                'leads':      len(leads),
                'tilbud':     len(tilbud),
                'bookinger':  len(bookinger),
            })

        # Sorter: lavest pct øverst (dem der mangler mest hjælp)
        result.sort(key=lambda x: (x['pct'], x['navn']))
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': _log_fejl(e)}), 500


@app.route('/econ/admin/oversigt', methods=['GET'])
@require_admin
def econ_admin_oversigt():
    """Admin: se hvilke klienter der har e-conomic forbundet"""
    try:
        res = supabase.table('klient_integrationer').select('*').execute()
        return jsonify(res.data or [])
    except Exception as e:
        return jsonify({'error': _log_fejl(e)}), 500

@app.route('/econ/vaerker', methods=['GET'])
@require_token
def econ_hent_vaerker():
    """Hent tilgængelige betalingsbetingelser og grupper fra e-conomic"""
    klient_id = request.user_klient_id
    token = _hent_economic_token(klient_id)
    if not token:
        return jsonify({'error': 'Ikke forbundet'}), 400
    hdrs = _economic_headers(token)
    try:
        pt = http_requests.get(f'{ECONOMIC_BASE}/payment-terms', headers=hdrs, timeout=8)
        cg = http_requests.get(f'{ECONOMIC_BASE}/customer-groups', headers=hdrs, timeout=8)
        return jsonify({
            'betalingsbetingelser': pt.json().get('collection', []) if pt.status_code == 200 else [],
            'kundegrupper': cg.json().get('collection', []) if cg.status_code == 200 else []
        })
    except Exception as e:
        return jsonify({'error': _log_fejl(e)}), 502

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
    if not _sikker_ekstern_url(raw_url):
        return jsonify({'error': 'Ugyldig eller ikke-tilladt URL'}), 400

    # Hent hjemmeside
    try:
        resp = http_requests.get(raw_url, timeout=10,
            headers={'User-Agent': 'Mozilla/5.0 (compatible; Nordolsen-Demo/1.0)'})
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')
    except Exception as e:
        print(f"demo_scan hentefejl: {e}")
        return jsonify({'error': 'Kunne ikke hente siden'}), 400

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
        'velkomst': ai_cfg.get('velkomst', f'Hej! Jeg er {virk_navn}s digitale assistent. Hvordan kan jeg hjælpe dig?'),
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
    <p style="color:#9a9590;font-size:.85rem">Nordolsen · support@nexolsen.dk</p>
  </div>
</div>"""
        send_mail(email, 'Vi vender tilbage inden for 24 timer 👋', html, 'Nordolsen')

    # Notifier admin
    if SENDGRID_API_KEY and ADMIN_EMAIL:
        send_mail(ADMIN_EMAIL, f'NY DEMO-INTERESSE: {email} ({navn or url})',
            f'Email: {email}\nVirksomhed: {navn}\nURL: {url}\n\nFølg op!', 'Nordolsen System')

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
    if not _sikker_ekstern_url(url):
        p['status'] = 'scan-fejl'
        _gem_prospekt(p)
        return jsonify({'error': 'Ugyldig eller ikke-tilladt URL'}), 400
    try:
        resp = http_requests.get(url, timeout=10,
            headers={'User-Agent': 'Mozilla/5.0 (compatible; Nordolsen/1.0)'})
        soup = BeautifulSoup(resp.text, 'html.parser')
    except Exception as e:
        print(f"prospekt_scan hentefejl: {e}")
        p['status'] = 'scan-fejl'
        _gem_prospekt(p)
        return jsonify({'error': 'Kunne ikke hente siden'}), 400

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
            messages=[{'role': 'user', 'content': f"""Du er salgskonsulent hos Nordolsen, der sælger AI-chatbots til danske virksomheder.

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
  "email_tekst": "personlig cold email på 4-5 linjer dansk. Nævn specifikt hvad de sælger, og hvad de mister ved ikke at have AI. Afslut med en konkret CTA. Underskriv som 'Mattis fra Nordolsen'. INGEN emojis."
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
    emne   = linjer[0].replace('Emne:', '').strip() if linjer else f"Digitalt system til {p['navn']}"
    tekst  = '\n'.join(linjer[2:]).strip() if len(linjer) > 2 else udkast

    html = f"""<div style="font-family:Arial,sans-serif;max-width:560px;padding:20px;color:#1a1918">
{('<br>'.join(tekst.split(chr(10))))}
<br><br>
<a href="https://klaiai.onrender.com/demo" style="display:inline-block;background:#0a2463;color:#fff;text-decoration:none;padding:10px 24px;border-radius:8px;font-weight:700;font-size:.9rem">
  Se din gratis demo →
</a>
<br><br>
<span style="font-size:.8rem;color:#9a9590">Nordolsen · Din samlede IT-løsning · <a href="mailto:support@nexolsen.dk" style="color:#9a9590">support@nexolsen.dk</a></span>
</div>"""

    ok = send_mail(til_email, emne, html, 'Mattis fra Nordolsen')
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

@app.route('/portal', methods=['GET'])
def klient_portal_query():
    """Portal via query-parametre: /portal?id=<klient_id>&token=<token>
    Bruges bl.a. af admin-impersonation. client.html laeser selv id + token fra URL'en."""
    from flask import make_response
    app_dir = os.path.join(os.path.dirname(__file__), '..', 'app')
    with open(os.path.join(app_dir, 'client.html'), 'r', encoding='utf-8') as f:
        html = f.read()
    response = make_response(html)
    response.headers['Content-Type'] = 'text/html; charset=utf-8'
    return response

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

    prompt = f"""Du er Nordolsen's tekniske support. Skriv en komplet opsætningsmanual på dansk til en ny kunde.

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
            <h1 style="color:#fff; margin:0; font-size:1.5rem">Velkommen til Nordolsen</h1>
            <p style="color:rgba(255,255,255,.7); margin:.5rem 0 0">Din opsætningsguide til {platform_navn}</p>
          </div>
          <div style="background:#fff; padding:2rem; border-radius:0 0 12px 12px; border:1px solid #e5e3de; border-top:none;">
            {''.join(html_lines)}
            <hr style="border:none;border-top:1px solid #e5e3de; margin:2rem 0"/>
            <p style="color:#9a9590; font-size:.85rem; text-align:center">
              Nordolsen · support@nexolsen.dk · <a href="https://klaiai.dk/login" style="color:#0a2463">Log ind her</a>
            </p>
          </div>
        </div>"""

        send_mail(email, f'🚀 Kom i gang med Nordolsen – din guide til {platform_navn}', html_content, 'Nordolsen')
        print(f"Opsætningsmanual sendt til {email}")
    except Exception as e:
        print(f"Manual generering fejl: {e}")
        # Fallback: send simpel velkomstmail
        send_mail(email, 'Velkommen til Nordolsen 🎉',
            f"""<p>Hej {navn}!</p>
            <p>Din konto er nu aktiv. Log ind på <a href="https://klaiai.dk/login">klaiai.dk/login</a></p>
            <p>Din chatbot-kode:<br><code>&lt;script src="https://klaiai.onrender.com/chatbot.js" data-client="{klient_id}"&gt;&lt;/script&gt;</code></p>
            <p>Med venlig hilsen,<br>Nordolsen</p>""", 'Nordolsen')


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
    hero_sub = "Ingen aktivitet endnu — chatbotten er klar og aktiv." if ingen else "Dit system har arbejdet for dig i baggrunden."

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
          <span style="color:#fff;font-size:18px;font-weight:900;letter-spacing:-0.5px">Nordolsen</span>
        </td>
      </tr>
    </table>
    <div style="font-size:12px;color:#9ca3af;margin-top:8px">Ugentlig rapport · Uge {uge_nr} · {dato_str}</div>
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
      <strong style="color:#6b7280">Nordolsen</strong> · support@nexolsen.dk
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
                lr = db.table('leads').select('id', count='exact').eq('klient_id', klient_id).gte('oprettet', uge_start).execute()
                leads_uge = lr.count or 0
                lr2 = db.table('leads').select('id', count='exact').eq('klient_id', klient_id).gte('oprettet', forrige_start).lt('oprettet', uge_start).execute()
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

# ADMIN: Kør denne SQL i Supabase for at oprette tabellen:
# CREATE TABLE IF NOT EXISTS markeds_priser (
#   id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
#   klient_id text NOT NULL,
#   branche text,
#   analyse text,
#   konkurrenter jsonb DEFAULT '[]',
#   oprettet timestamptz DEFAULT now(),
#   opdateret timestamptz DEFAULT now()
# );

def kør_markeds_overvågning():
    """📊 Scanner markedspriser for alle aktive klienter ugentligt"""
    if not db:
        return
    print("📊 Markedsovervågning kører...")
    try:
        klienter_res = db.table('klienter').select('id, navn, hjemmeside').eq('aktiv', True).execute()
        for k in (klienter_res.data or []):
            klient_id = k['id']
            klient_navn = k.get('navn', '')
            klient_hjemmeside = k.get('hjemmeside', '')
            if not klient_hjemmeside:
                continue
            try:
                # Hent klientens ydelser og branche fra chatbot_config
                cfg = db.table('chatbot_config').select('ydelser, branche, andet').eq('klient_id', klient_id).single().execute()
                ydelser = ''
                branche = ''
                if cfg.data:
                    ydelser = cfg.data.get('ydelser', '')
                    branche = cfg.data.get('branche', '') or cfg.data.get('andet', '')

                # Byg søgeforespørgsel baseret på klientens branche
                søge_emne = branche or ydelser[:60] if (branche or ydelser) else klient_navn

                # Brug Claude til at analysere markedspriser via web_search
                prompt = f"""Du er en markedsprisanalytiker. Analyser markedspriser for virksomheder der sælger: {søge_emne}

Virksomhed vi analyserer for: {klient_navn} ({klient_hjemmeside})

Giv en kort, konkret analyse på dansk med:
1. Typiske markedspriser for de vigtigste ydelser i denne branche (konkrete tal)
2. Prisniveau: er {klient_navn} forventeligt dyr, middel eller billig ift. markedet?
3. 2-3 konkrete anbefalinger til prissætning

Hold analysen under 300 ord og fokuser på handlingsrettede indsigter."""

                svar = ai.messages.create(
                    model='claude-haiku-4-5-20251001',
                    max_tokens=600,
                    messages=[{'role': 'user', 'content': prompt}]
                )
                analyse_tekst = svar.content[0].text.strip()

                # Gem/opdater i databasen
                eksisterende = db.table('markeds_priser').select('id').eq('klient_id', klient_id).execute()
                if eksisterende.data:
                    db.table('markeds_priser').update({
                        'analyse_tekst': analyse_tekst,
                        'branche': søge_emne[:100],
                        'opdateret': datetime.utcnow().isoformat()
                    }).eq('klient_id', klient_id).execute()
                else:
                    import uuid as _uuid2
                    db.table('markeds_priser').insert({
                        'id': str(_uuid2.uuid4()),
                        'klient_id': klient_id,
                        'branche': søge_emne[:100],
                        'analyse_tekst': analyse_tekst,
                        'konkurrenter': []
                    }).execute()
                print(f"  ✅ Markedsanalyse opdateret for {klient_navn}")
            except Exception as e:
                print(f"  ❌ Markedsanalyse fejl ({klient_navn}): {e}")
    except Exception as e:
        print(f"Markedsovervågning fejl: {e}")

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
                    'Din Nordolsen konto er deaktiveret',
                    f"""<p>Hej {k.get('navn', '')},</p>
                    <p>Vi har desværre måttet deaktivere din Nordolsen konto da vi ikke har kunnet trække betaling i over 7 dage.</p>
                    <p>Hvis du ønsker at genaktivere din konto, kan du opdatere din betalingsmetode ved at kontakte os på
                    <a href="mailto:support@nexolsen.dk">support@nexolsen.dk</a>.</p>
                    <p>Med venlig hilsen,<br>Nordolsen</p>""",
                    'Nordolsen'
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
                klient_navn = k_res.data.get('navn', 'Nordolsen') if k_res.data else 'Nordolsen'
            except:
                klient_navn = 'Nordolsen'

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
        alarm('Mail-flow agent fejlede',
              f"Den automatiske mail-flow agent (koerer hver time) fejlede. Planlagte opfoelgningsmails er muligvis ikke sendt. Fejl: {e}",
              noegle='mail_flow_agent')


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
        return jsonify({'error': _log_fejl(e)}), 500

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
        return jsonify({'error': _log_fejl(e)}), 500


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
        leads_res = db.table('leads').select('*').eq('klient_id', klient_id).order('oprettet', desc=True).execute()
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
        book_res = db.table('bookinger').select('*').eq('klient_id', klient_id).order('oprettet', desc=True).limit(20).execute()
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
        return jsonify({'error': _log_fejl(e)}), 500


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

        emne = f"[Nordolsen] ⚠️ {len(leads)} ubesvarede leads afventer svar"
        tekst = f"""Hej Mattis,

Du har leads der har ligget i 'Ny' i over 24 timer uden at blive kontaktet:

{chr(10).join(linjer)}

Gå ind i CRM-panelet og følg op:
https://klaiai.onrender.com/app/admin.html

Mvh Nordolsen
"""
        send_mail(ADMIN_EMAIL, emne, tekst, 'Nordolsen')
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
                    from_email=(SENDGRID_FROM, 'Nordolsen'),
                    to_emails=email,
                    subject=f"Din Nordolsen rapport — {mdr_navn}",
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


def _byg_followup_html(fra_navn, klient_hjemmeside, kunde_navn, titel, accept_knap, trin, total_pris=0):
    """Bygger en professionel opfølgnings-email — 3 varianter baseret på trin (1/2/3)"""
    fornavn = kunde_navn.split()[0] if kunde_navn else 'der'
    pris_str = f'{int(total_pris):,} kr.'.replace(',', '.') if total_pris else ''

    if trin == 1:
        emne_linje = f'{fornavn} — har du haft mulighed for at kigge på vores tilbud?'
        indhold = f"""
    <div style="font-size:22px;font-weight:800;color:#111;letter-spacing:-.5px;margin-bottom:18px;line-height:1.3">Hej {fornavn},<br>nåede det frem til dig?</div>
    <div style="font-size:14px;color:#374151;line-height:1.85;margin-bottom:16px">
      For et par dage siden sendte vi dig vores tilbud på <strong>{titel}</strong>. Vi ville blot høre, om du har haft mulighed for at kigge på det.
    </div>
    <div style="font-size:14px;color:#374151;line-height:1.85;margin-bottom:16px">
      Har du spørgsmål til prisen, tidsplanen eller opgavens omfang, er du meget velkommen til at svare direkte på denne mail — vi tilpasser gerne.
    </div>
    {f'<div style="background:#f8faff;border-left:3px solid #2563eb;border-radius:0 8px 8px 0;padding:14px 18px;margin-bottom:20px;font-size:13px;color:#374151"><strong>Tilbuddets samlede pris:</strong> {pris_str}</div>' if pris_str else ''}"""
    elif trin == 2:
        emne_linje = f'{fornavn} — tilbuddet gælder stadig, og vi er klar'
        indhold = f"""
    <div style="font-size:22px;font-weight:800;color:#111;letter-spacing:-.5px;margin-bottom:18px;line-height:1.3">Tilbuddet gælder stadig, {fornavn}</div>
    <div style="font-size:14px;color:#374151;line-height:1.85;margin-bottom:16px">
      Vi vil ikke forstyrre dig — men vi er nødt til at minde dig om at vores tilbud på <strong>{titel}</strong> stadig er åbent og klar til at gå i gang.
    </div>
    <div style="background:#f0fdf4;border-radius:10px;padding:18px 20px;margin-bottom:20px">
      <div style="font-size:13px;font-weight:700;color:#15803d;margin-bottom:8px">Hvorfor vælge os?</div>
      <div style="font-size:13px;color:#166534;line-height:1.7">
        ✓ &nbsp;Professionel udførelse med garanti<br>
        ✓ &nbsp;Hurtig opstart — vi er klar inden for kort tid<br>
        ✓ &nbsp;Alt inkluderet i tilbuddet, ingen skjulte priser
      </div>
    </div>
    <div style="font-size:14px;color:#374151;line-height:1.85;margin-bottom:16px">
      Klik nedenfor for at godkende — så kontakter vi dig med det samme for at aftale opstart.
    </div>"""
    else:
        emne_linje = f'Sidste chance, {fornavn} — vi lukker tilbuddet om lidt'
        indhold = f"""
    <div style="font-size:22px;font-weight:800;color:#111;letter-spacing:-.5px;margin-bottom:18px;line-height:1.3">Hej {fornavn},<br>dette er vores sidste besked</div>
    <div style="font-size:14px;color:#374151;line-height:1.85;margin-bottom:16px">
      Vi har sendt dig et tilbud på <strong>{titel}</strong>, og vi har forsøgt at følge op et par gange.
    </div>
    <div style="font-size:14px;color:#374151;line-height:1.85;margin-bottom:16px">
      Vi forstår at tidspunktet måske ikke er det rette nu — og det er fuldstændig okay. Tilbuddet er stadig gyldigt hvis du ønsker at gå videre.
    </div>
    <div style="background:#fffbeb;border:1px solid #fde68a;border-radius:10px;padding:16px 20px;margin-bottom:20px;font-size:13px;color:#92400e">
      <strong>Bemærk:</strong> Vi vil ikke sende dig flere mails om dette tilbud herefter. Du er altid velkommen til at kontakte os direkte.
    </div>"""

    return f"""<!DOCTYPE html>
<html lang="da"><head><meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/></head>
<body style="margin:0;padding:0;background:#f0f2f5;font-family:'Helvetica Neue',Arial,sans-serif">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f0f2f5;padding:40px 20px">
<tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0" style="max-width:600px">

  <!-- HEADER -->
  <tr><td style="background:linear-gradient(135deg,#0a1a3a 0%,#1e3a6e 100%);border-radius:14px 14px 0 0;padding:28px 40px">
    <div style="font-size:18px;font-weight:900;color:#fff;letter-spacing:-.3px">{fra_navn}</div>
    {f'<div style="font-size:12px;color:rgba(255,255,255,.4);margin-top:3px">{klient_hjemmeside}</div>' if klient_hjemmeside else ''}
    <div style="margin-top:16px;font-size:11px;color:rgba(255,255,255,.35);text-transform:uppercase;letter-spacing:1.5px">Opfølgning · {titel}</div>
  </td></tr>

  <!-- BODY -->
  <tr><td style="background:#ffffff;padding:36px 40px">
    {indhold}
    {accept_knap}
  </td></tr>

  <!-- FOOTER -->
  <tr><td style="background:#f8f9fa;border-radius:0 0 14px 14px;padding:20px 40px;text-align:center;border-top:1px solid #e9ecef">
    <div style="font-size:12px;color:#9ca3af;line-height:1.7">
      Du modtager denne mail fordi du har modtaget et tilbud fra {fra_navn}.<br>
      Svar blot på denne mail hvis du har spørgsmål.
    </div>
  </td></tr>

</table>
</td></tr></table>
</body></html>""", emne_linje


def kør_tilbud_followup():
    """3-trins automatisk opfølgningssekvens på ubesvarede tilbud"""
    if not db:
        return
    from datetime import datetime, timedelta
    nu = datetime.now()
    try:
        res = db.table('tilbud').select('*').eq('status', 'sendt').execute()
    except Exception as e:
        print(f'Tilbud followup DB fejl: {e}')
        return

    for t in (res.data or []):
        try:
            kunde_email = t.get('kunde_email', '')
            if not kunde_email or '@' not in kunde_email:
                continue
            sendt_dato_str = t.get('sendt_dato') or t.get('oprettet', '')
            if not sendt_dato_str:
                continue

            # Parse dato
            try:
                sendt = datetime.fromisoformat(sendt_dato_str.replace('Z', '+00:00')).replace(tzinfo=None)
            except:
                continue

            dage_siden = (nu - sendt).days
            followup_nr = t.get('followup_nr', 0) or 0  # 0=ingen sendt, 1=første sendt, osv

            # Trin 1: dag 3 · Trin 2: dag 7 · Trin 3: dag 14
            skema = {1: 3, 2: 7, 3: 14}
            næste_trin = followup_nr + 1
            if næste_trin not in skema:
                continue
            if dage_siden < skema[næste_trin]:
                continue

            k = db.table('klienter').select('navn,hjemmeside').eq('id', t['klient_id']).single().execute()
            klient = k.data or {}
            fra_navn = klient.get('navn', 'Virksomheden')
            klient_hjemmeside = klient.get('hjemmeside', '')
            kunde_navn  = t.get('kunde_navn', '')
            titel       = t.get('titel', 'Tilbud')
            total_pris  = t.get('total_pris', 0) or 0
            accept_token = t.get('accept_token', '')

            accept_url = f'{SERVER_URL}/tilbud/godkend/{t["id"]}/{accept_token}' if accept_token else ''
            accept_knap = (
                f'<div style="text-align:center;margin:28px 0">'
                f'<a href="{accept_url}" style="display:inline-block;background:#16a34a;color:#fff;text-decoration:none;font-size:15px;font-weight:700;padding:14px 40px;border-radius:10px;letter-spacing:.3px">&#10003;&nbsp; Godkend tilbud</a>'
                f'<div style="font-size:11px;color:#9ca3af;margin-top:10px">Klik for at acceptere tilbuddet digitalt og bindende</div>'
                f'</div>'
            ) if accept_url else ''

            html, emne = _byg_followup_html(fra_navn, klient_hjemmeside, kunde_navn, titel, accept_knap, næste_trin, total_pris)

            send_mail(kunde_email, emne, f'Opfølgning på dit tilbud fra {fra_navn}', fra_navn=fra_navn, html_content=html)
            db.table('tilbud').update({
                'followup_sendt': True,
                'followup_dato': nu.isoformat(),
                'followup_nr': næste_trin
            }).eq('id', t['id']).execute()
            print(f'Tilbud followup trin {næste_trin} sendt til {kunde_email}')
        except Exception as e:
            print(f'Tilbud followup fejl ({t.get("id")}): {e}')


def kør_tilbud_udløb():
    """Markerer tilbud som udløbet efter 30 dage uden svar og sender sidst-chance mail dag 25"""
    if not db:
        return
    from datetime import datetime, timedelta
    nu = datetime.now()
    print("⏰ Tilbud udløb-check kører...")
    try:
        res = db.table('tilbud').select('*').eq('status', 'sendt').execute()
        for t in (res.data or []):
            try:
                sendt_str = t.get('sendt_dato') or t.get('oprettet', '')
                if not sendt_str:
                    continue
                sendt = datetime.fromisoformat(sendt_str.replace('Z', '+00:00')).replace(tzinfo=None)
                dage_siden = (nu - sendt).days

                # Dag 25 — sidst chance mail
                if dage_siden == 25 and not t.get('sidst_chance_sendt'):
                    kunde_email = t.get('kunde_email', '')
                    if kunde_email and '@' in kunde_email:
                        k = db.table('klienter').select('navn').eq('id', t['klient_id']).single().execute()
                        fra_navn = k.data.get('navn', 'Virksomheden') if k.data else 'Virksomheden'
                        accept_token = t.get('accept_token', '')
                        accept_url = f'{SERVER_URL}/tilbud/godkend/{t["id"]}/{accept_token}' if accept_token else ''
                        html = f"""<!DOCTYPE html><html lang="da"><head><meta charset="UTF-8"/></head>
<body style="margin:0;padding:0;background:#fff8ed;font-family:'Helvetica Neue',Arial,sans-serif">
<table width="100%" cellpadding="0" cellspacing="0" style="padding:40px 20px"><tr><td align="center">
<table width="580" cellpadding="0" cellspacing="0" style="max-width:580px;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 4px 20px rgba(0,0,0,.08)">
  <tr><td style="background:linear-gradient(135deg,#92400e,#d97706);padding:28px 36px">
    <div style="font-size:20px;font-weight:800;color:#fff">⏰ Dit tilbud udløber om 5 dage</div>
    <div style="font-size:13px;color:rgba(255,255,255,.7);margin-top:4px">Tilbud fra {fra_navn}</div>
  </td></tr>
  <tr><td style="padding:28px 36px">
    <p style="font-size:15px;color:#374151;line-height:1.7">Hej {t.get('kunde_navn','')},<br><br>
    Dit tilbud <strong>"{t.get('titel','Tilbud')}"</strong> på <strong>{int(t.get('total_pris',0)):,} kr.</strong> udløber om 5 dage.<br><br>
    Ønsker du at gå videre, skal du acceptere inden da.</p>
    {f'<div style="text-align:center;margin:24px 0"><a href="{accept_url}" style="background:#d97706;color:#fff;text-decoration:none;font-size:15px;font-weight:700;padding:14px 40px;border-radius:10px;display:inline-block">Acceptér tilbud →</a></div>' if accept_url else ''}
    <p style="font-size:13px;color:#6b7280">Har du spørgsmål er du altid velkommen til at kontakte os.<br><br>Med venlig hilsen<br><strong>{fra_navn}</strong></p>
  </td></tr>
</table></td></tr></table>
</body></html>"""
                        send_mail(kunde_email, f"⏰ Dit tilbud udløber om 5 dage — {t.get('titel','')}", '', fra_navn, html_content=html)
                        db.table('tilbud').update({'sidst_chance_sendt': True}).eq('id', t['id']).execute()
                        print(f"  ⏰ Sidst-chance mail sendt til {kunde_email}")

                # Dag 30+ — markér som udløbet
                elif dage_siden >= 30:
                    db.table('tilbud').update({'status': 'udløbet'}).eq('id', t['id']).execute()
                    print(f"  📅 Tilbud {t['id'][:8]} markeret som udløbet ({dage_siden} dage)")
            except Exception as e:
                print(f"  Udløb-fejl ({t.get('id','')}): {e}")
    except Exception as e:
        print(f"Tilbud udløb fejl: {e}")


# ── Keep-alive: undgaa cold-starts ──────────────────────────────
# Render spinner web-servicen ned efter ~15 min uden indgaaende trafik. En
# koldstart giver 30-60s ventetid for foerste besoegende — en troværdigheds-
# draeber. Vi pinger vores egen offentlige URL hvert 10. min, saa idle-timeren
# aldrig naar 15 min og serveren altid er varm. Koerer kun i produktion
# (RENDER_EXTERNAL_URL saettes automatisk af Render), ikke lokalt.
KEEP_ALIVE_URL = os.environ.get('RENDER_EXTERNAL_URL')

def kør_keep_alive():
    if not KEEP_ALIVE_URL:
        return
    try:
        r = http_requests.get(f"{KEEP_ALIVE_URL}/health", timeout=20)
        if r.status_code != 200:
            alarm('Health-check gav uventet svar',
                  f"/health returnerede {r.status_code}. Serveren svarer men er muligvis usund.",
                  noegle='health_status')
    except Exception as e:
        # Kan ikke naa egen /health — netvaerk eller server i knibe. Canary.
        print(f"Keep-alive ping fejl: {e}")
        alarm('Kan ikke naa egen /health',
              f"Keep-alive kunne ikke ramme {KEEP_ALIVE_URL}/health. Muligt netvaerks- eller serverproblem. Fejl: {e}",
              noegle='health_unreachable')

if KEEP_ALIVE_URL:
    scheduler.add_job(kør_keep_alive, 'interval', minutes=10, id='keep_alive')
    print(f"🔥 Keep-alive aktiv mod {KEEP_ALIVE_URL}/health (hvert 10. min)")

scheduler.add_job(kør_månedlig_rapport, 'cron', day=1, hour=8, minute=0, id='månedlig_rapport')
scheduler.add_job(kør_ubesvarede_leads_reminder, 'cron', hour=9, minute=30, id='ubesvarede_leads')
scheduler.add_job(kør_ubesvarede_leads_reminder, 'cron', hour=17, minute=0, id='ubesvarede_leads_aften')
scheduler.add_job(kør_reminder_agent,   'cron', hour=9,  minute=0, id='reminder')
scheduler.add_job(kør_review_agent,     'cron', hour=10, minute=0, id='review')
scheduler.add_job(kør_genopvarmning_agent, 'cron', hour=11, minute=0, id='genopvarmning')
scheduler.add_job(kør_ugerapport_agent, 'cron', hour=7,  minute=0, id='ugerapport', day_of_week='mon')
scheduler.add_job(kør_markeds_overvågning, 'cron', day_of_week='fri', hour=6, minute=0, id='markeds_overvågning')
scheduler.add_job(kør_billing_agent,    'cron', hour=8,  minute=0, id='billing')
scheduler.add_job(kør_tilbud_followup, 'cron', hour=10, minute=15, id='tilbud_followup')
scheduler.add_job(kør_tilbud_udløb, 'cron', hour=8, minute=30, id='tilbud_udløb')
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
        return jsonify({'error': _log_fejl(e)}), 500

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
        return jsonify({'error': _log_fejl(e)}), 500


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
        return jsonify({'error': _log_fejl(e)}), 500


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
        return jsonify({'error': _log_fejl(e)}), 500


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
    <div style="font-size:10px;color:rgba(255,255,255,.25);letter-spacing:1px">Tilbud genereret af Nordolsen &middot; Gyldigt i 14 dage</div>
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
    <div style="font-size:11px;color:#9ca3af">Tilbud genereret af Nordolsen &middot; Tilbuddet er gyldigt i 14 dage fra udstedelsesdato</div>
  </td></tr>

</table></td></tr></table>
</body></html>"""


@app.route('/priskatalog/<klient_id>', methods=['GET'])
@require_token
def hent_priskatalog(klient_id):
    # Klient-token må kun se eget katalog
    raw = request.headers.get('Authorization', '')
    _tok = raw.replace('Bearer ', '').strip()
    _info = active_tokens.get(_tok, {})
    if _info.get('role') == 'client' and _info.get('klient_id') != klient_id:
        return jsonify({'error': 'Ikke tilladt'}), 403
    if not db: return jsonify([])
    try:
        res = db.table('priskatalog').select('*').eq('klient_id', klient_id).order('kategori').order('navn').execute()
        return jsonify(res.data or [])
    except Exception as e:
        return jsonify({'error': _log_fejl(e)}), 500

@app.route('/priskatalog/<klient_id>', methods=['POST'])
@require_token
def gem_prispost(klient_id):
    raw = request.headers.get('Authorization', '')
    _tok = raw.replace('Bearer ', '').strip()
    _info = active_tokens.get(_tok, {})
    if _info.get('role') == 'client' and _info.get('klient_id') != klient_id:
        return jsonify({'error': 'Ikke tilladt'}), 403
    if not db: return jsonify({'error': 'Ingen database'}), 500
    data = request.json or {}
    import uuid as _uuid
    try:
        post = {
            'id': str(_uuid.uuid4()),
            'klient_id': klient_id,
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
        return jsonify({'error': _log_fejl(e)}), 500

@app.route('/priskatalog/<klient_id>/<post_id>', methods=['PUT'])
@require_token
def opdater_prispost(klient_id, post_id):
    raw = request.headers.get('Authorization', '')
    _tok = raw.replace('Bearer ', '').strip()
    _info = active_tokens.get(_tok, {})
    if _info.get('role') == 'client' and _info.get('klient_id') != klient_id:
        return jsonify({'error': 'Ikke tilladt'}), 403
    if not db: return jsonify({'error': 'Ingen database'}), 500
    data = request.json or {}
    try:
        db.table('priskatalog').update({
            'navn': data.get('navn'),
            'enhedspris': float(data.get('enhedspris', 0)),
            'enhed': data.get('enhed', 'stk'),
            'kategori': data.get('kategori', 'Generelt'),
            'beskrivelse': data.get('beskrivelse', ''),
            'moms_inkluderet': bool(data.get('moms_inkluderet', False)),
        }).eq('id', post_id).eq('klient_id', klient_id).execute()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': _log_fejl(e)}), 500

@app.route('/priskatalog/<klient_id>/<post_id>', methods=['DELETE'])
@require_token
def slet_prispost(klient_id, post_id):
    raw = request.headers.get('Authorization', '')
    _tok = raw.replace('Bearer ', '').strip()
    _info = active_tokens.get(_tok, {})
    if _info.get('role') == 'client' and _info.get('klient_id') != klient_id:
        return jsonify({'error': 'Ikke tilladt'}), 403
    if not db: return jsonify({'error': 'Ingen database'}), 500
    try:
        db.table('priskatalog').delete().eq('id', post_id).eq('klient_id', klient_id).execute()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': _log_fejl(e)}), 500

# Behold admin-kompatibilitet for PATCH (bruges af admin.html)
@app.route('/priskatalog/<post_id>', methods=['PATCH'])
@require_admin
def opdater_prispost_admin(post_id):
    if not db: return jsonify({'error': 'Ingen database'}), 500
    data = request.json or {}
    try:
        db.table('priskatalog').update(data).eq('id', post_id).execute()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': _log_fejl(e)}), 500

# Behold admin DELETE compat
@app.route('/priskatalog/slet/<post_id>', methods=['DELETE'])
@require_admin
def slet_prispost_admin(post_id):
    if not db: return jsonify({'error': 'Ingen database'}), 500
    try:
        db.table('priskatalog').delete().eq('id', post_id).execute()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': _log_fejl(e)}), 500

@app.route('/markeds-analyse/<klient_id>', methods=['GET'])
@require_token
def hent_markeds_analyse(klient_id):
    """Hent seneste markedsanalyse for en klient — tilgængelig for klient-token (intern brug)"""
    raw = request.headers.get('Authorization', '')
    _tok = raw.replace('Bearer ', '').strip()
    _info = active_tokens.get(_tok, {})
    if _info.get('role') == 'client' and _info.get('klient_id') != klient_id:
        return jsonify({'analyse': None}), 403
    if not db:
        return jsonify({'analyse': None})
    try:
        res = db.table('markeds_priser').select('analyse_tekst,branche,opdateret').eq('klient_id', klient_id).order('opdateret', desc=True).limit(1).execute()
        if res.data:
            row = res.data[0]
            return jsonify({'analyse': row.get('analyse_tekst'), 'branche': row.get('branche'), 'opdateret': row.get('opdateret')})
        return jsonify({'analyse': None})
    except Exception as e:
        return jsonify({'analyse': None, 'error': _log_fejl(e)})


@app.route('/markeds-analyse/<klient_id>', methods=['POST'])
@require_admin
def kør_markeds_analyse_nu(klient_id):
    """Kør markedsanalyse nu for én klient (manuel trigger)"""
    if not db:
        return jsonify({'error': 'Ingen database'}), 500
    try:
        k = db.table('klienter').select('navn, hjemmeside').eq('id', klient_id).single().execute()
        if not k.data:
            return jsonify({'error': 'Klient ikke fundet'}), 404
        klient_navn = k.data.get('navn', '')
        klient_hjemmeside = k.data.get('hjemmeside', '')
        cfg = db.table('chatbot_config').select('ydelser, branche, andet').eq('klient_id', klient_id).single().execute()
        ydelser = ''
        branche = ''
        if cfg.data:
            ydelser = cfg.data.get('ydelser', '')
            branche = cfg.data.get('branche', '') or cfg.data.get('andet', '')
        søge_emne = branche or ydelser[:60] if (branche or ydelser) else klient_navn
        prompt = f"""Du er en markedsprisanalytiker. Analyser markedspriser for virksomheder der sælger: {søge_emne}

Virksomhed vi analyserer for: {klient_navn} ({klient_hjemmeside})

Giv en kort, konkret analyse på dansk med:
1. Typiske markedspriser for de vigtigste ydelser i denne branche (konkrete tal)
2. Prisniveau: er {klient_navn} forventeligt dyr, middel eller billig ift. markedet?
3. 2-3 konkrete anbefalinger til prissætning

Hold analysen under 300 ord og fokuser på handlingsrettede indsigter."""
        svar = ai.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=600,
            messages=[{'role': 'user', 'content': prompt}]
        )
        analyse_tekst = svar.content[0].text.strip()
        from datetime import datetime
        import uuid as _uuid3
        eksisterende = db.table('markeds_priser').select('id').eq('klient_id', klient_id).execute()
        if eksisterende.data:
            db.table('markeds_priser').update({
                'analyse_tekst': analyse_tekst,
                'branche': søge_emne[:100],
                'opdateret': datetime.utcnow().isoformat()
            }).eq('klient_id', klient_id).execute()
        else:
            db.table('markeds_priser').insert({
                'id': str(_uuid3.uuid4()),
                'klient_id': klient_id,
                'branche': søge_emne[:100],
                'analyse_tekst': analyse_tekst,
                'konkurrenter': []
            }).execute()
        return jsonify({'ok': True, 'analyse': analyse_tekst})
    except Exception as e:
        return jsonify({'error': _log_fejl(e)}), 500


@app.route('/priskatalog/upload-pdf', methods=['POST'])
@require_admin
def upload_priskatalog_pdf():
    """Udtræk ydelser og priser fra en PDF-prisliste via Claude"""
    import pdfplumber, io
    klient_id = request.form.get('klient_id', '')
    if not klient_id:
        return jsonify({'error': 'klient_id mangler'}), 400
    if 'fil' not in request.files:
        return jsonify({'error': 'Ingen fil uploadet'}), 400
    fil = request.files['fil']
    if not fil.filename.lower().endswith('.pdf'):
        return jsonify({'error': 'Kun PDF-filer understøttes'}), 400
    try:
        pdf_bytes = fil.read()
        tekst = ''
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for side in pdf.pages[:10]:
                tekst += (side.extract_text() or '') + '\n'
        if not tekst.strip():
            return jsonify({'error': 'Kunne ikke læse tekst fra PDF'}), 400
        # Brug Claude til at parse ydelser og priser
        svar = ai.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=2000,
            messages=[{
                'role': 'user',
                'content': f"""Udtræk alle ydelser og priser fra denne prisliste og returnér dem som JSON-array.

Format: [{{"navn": "...", "enhedspris": 1234, "enhed": "stk", "kategori": "...", "beskrivelse": "..."}}]

Regler:
- enhedspris skal være et tal (ingen kr. eller komma — brug punktum)
- enhed: typisk "stk", "time", "m²", "m" eller "dag"
- kategori: grup ydelserne logisk (fx "Installation", "Service", "Materialer")
- beskrivelse: kort forklaring hvis relevant, ellers tom streng
- Returner KUN JSON-arrayet, ingen forklaring

Prisliste:
{tekst[:3000]}"""
            }]
        )
        import json as _json
        raw = svar.content[0].text.strip()
        # Fjern markdown code blocks hvis Claude pakker det ind
        if raw.startswith('```'):
            raw = raw.split('```')[1]
            if raw.startswith('json'):
                raw = raw[4:]
        poster = _json.loads(raw.strip())
        if not isinstance(poster, list):
            return jsonify({'error': 'Uventet svar fra AI'}), 500
        return jsonify({'poster': poster, 'antal': len(poster)})
    except Exception as e:
        print(f"PDF upload fejl: {e}")
        return jsonify({'error': _log_fejl(e, 'Fejl ved behandling af fil')}), 500

@app.route('/tale/pris', methods=['POST'])
@require_token
def parse_tale_pris():
    """Parser en dikteret pris og udtrækker katalogdata via Claude"""
    data = request.get_json() or {}
    transskription = data.get('tekst', '').strip()
    if not transskription:
        return jsonify({'error': 'Ingen tekst'}), 400
    try:
        prompt = f"""En dansk håndværker har dikteret en pris til sit priskatalog:
"{transskription}"

Udtræk disse oplysninger og returner KUN valid JSON:
{{
  "navn": "navnet på ydelsen eller produktet",
  "enhedspris": 0.0,
  "enhed": "stk",
  "kategori": "kategori hvis nævnt, ellers 'Generelt'",
  "beskrivelse": "kort beskrivelse hvis nævnt, ellers tom streng"
}}

Enhed skal være én af: stk, m², m, time, dag, opgave, pakke
Hvis der nævnes "pr time", "per time" → enhed = "time"
Hvis der nævnes "pr kvadratmeter", "per m2" → enhed = "m²"
Hvis der nævnes "pr meter" → enhed = "m"
Enhedspris skal være et tal i DKK (ignorer "kr", "kroner" osv.).
Kun JSON, ingen forklaring."""

        svar = ai.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=300,
            messages=[{'role': 'user', 'content': prompt}]
        )
        tekst = svar.content[0].text.strip()
        import re as _re3, json as _json3
        match = _re3.search(r'\{[\s\S]*\}', tekst)
        if not match:
            return jsonify({'error': 'Kunne ikke parse'}), 500
        parsed = _json3.loads(match.group(0))
        return jsonify({'ok': True, 'data': parsed})
    except Exception as e:
        print(f"Tale pris fejl: {e}")
        return jsonify({'error': _log_fejl(e)}), 500


@app.route('/tale/parse', methods=['POST'])
@require_token
def parse_tale():
    """Parser en dansk stemmeoptagelse og udtrækker tilbudsdata via Claude"""
    data = request.get_json() or {}
    transskription = data.get('tekst', '').strip()
    if not transskription:
        return jsonify({'error': 'Ingen tekst'}), 400
    try:
        prompt = f"""Du er en assistent der hjælper danske håndværkere med at lave tilbud.

En håndværker har dikteret følgende noter ude hos en kunde:
"{transskription}"

Udtræk disse oplysninger og returner KUN valid JSON:
{{
  "kunde_navn": "fuldt navn hvis nævnt, ellers tom streng",
  "kunde_email": "email hvis nævnt, ellers tom streng",
  "kunde_adresse": "vejnavn og husnummer hvis nævnt, ellers tom streng",
  "kunde_postnummer": "postnummer eller by hvis nævnt, ellers tom streng",
  "opgave": "detaljeret beskrivelse af opgaven på dansk — beskriv hvad der skal laves, mål, materialer nævnt, særlige forhold osv.",
  "noter": "eventuelle ekstra noter, ønsker eller forbehold der er nævnt"
}}

Opgave-feltet skal være en fyldig, professionel beskrivelse der kan bruges direkte i et tilbud.
Kun JSON, ingen forklaring."""

        svar = ai.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=800,
            messages=[{'role': 'user', 'content': prompt}]
        )
        tekst = svar.content[0].text.strip()
        import re as _re2, json as _json2
        match = _re2.search(r'\{[\s\S]*\}', tekst)
        if not match:
            return jsonify({'error': 'Kunne ikke parse tale'}), 500
        parsed = _json2.loads(match.group(0))
        return jsonify({'ok': True, 'data': parsed, 'transskription': transskription})
    except Exception as e:
        print(f"Tale parse fejl: {e}")
        return jsonify({'error': _log_fejl(e)}), 500


@app.route('/materialer/soeg', methods=['POST'])
@require_token
def soeg_materialer():
    """Søg efter materialepriser fra danske byggemarkeder via Claude web_search"""
    data = request.get_json() or {}
    soegning = data.get('soegning', '').strip()
    if not soegning:
        return jsonify({'error': 'Ingen søgning'}), 400
    try:
        prompt = f"""Søg efter aktuelle priser på "{soegning}" fra danske byggemarkeder.

Tjek disse butikker: Bauhaus (bauhaus.dk), Silvan (silvan.dk), Stark (stark.dk), XL-Byg (xl-byg.dk), Bygma (bygma.dk).

Returner KUN et JSON array med max 6 resultater i dette format:
[
  {{"produkt": "Præcist produktnavn inkl. størrelse/mål", "pris": 149.00, "enhed": "stk", "butik": "Bauhaus", "bemærkning": "evt. rabat/tilbud info"}}
]

Enhed skal være en af: stk, m², m, liter, kg, pakke, pose, rulle, sæk
Pris skal være et tal (DKK ekskl. moms).
Kun JSON array, ingen tekst før eller efter."""

        svar = ai.messages.create(
            model='claude-sonnet-4-6',
            max_tokens=1200,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
            messages=[{'role': 'user', 'content': prompt}]
        )

        # Find tekstindhold i svaret (efter tool use)
        tekst = ''
        for blok in svar.content:
            if hasattr(blok, 'text'):
                tekst += blok.text

        # Parse JSON fra svaret
        import re as _re
        json_match = _re.search(r'\[[\s\S]*\]', tekst)
        if not json_match:
            return jsonify({'resultater': [], 'besked': 'Ingen priser fundet for denne søgning'})

        import json as _json
        resultater = _json.loads(json_match.group(0))
        if not isinstance(resultater, list):
            resultater = []

        return jsonify({'resultater': resultater, 'soegning': soegning})
    except Exception as e:
        print(f"Materialer søg fejl: {e}")
        return jsonify({'error': _log_fejl(e)}), 500


@app.route('/tilbud/generer', methods=['POST'])
@limiter.limit("10 per minute; 40 per hour")
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
    valgte_ydelser  = data.get('valgte_ydelser', [])
    materialer      = data.get('materialer', [])

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

    valgte_tekst = ''
    if valgte_ydelser:
        linjer = [f"- {y['navn']}: {int(y['enhedspris']):,} kr. per {y.get('enhed','stk')}" for y in valgte_ydelser]
        valgte_tekst = "\n\nKUNDEN HAR VALGT DISSE SPECIFIKKE YDELSER (inkludér dem alle i tilbuddet med disse PRÆCISE priser):\n" + '\n'.join(linjer)

    materialer_tekst = ''
    if materialer:
        mat_linjer = []
        mat_total = 0
        for m in materialer:
            linje_pris = m.get('enhedspris', 0) * m.get('antal', 1)
            mat_total += linje_pris
            mat_linjer.append(f"- {m['antal']} × {m['produkt']} ({m.get('butik','')}) à {m.get('enhedspris',0):,.2f} kr./{m.get('enhed','stk')} = {linje_pris:,.2f} kr.")
        materialer_tekst = f"\n\nMATERIALER (håndværkeren har hentet disse aktuelle priser — inkludér dem som en separat post i tilbuddet):\n" + '\n'.join(mat_linjer) + f"\nTotal materialeomkostning: {mat_total:,.2f} kr. ekskl. moms"

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
{valgte_tekst}{materialer_tekst}
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
        return jsonify({'error': _log_fejl(e, 'AI-generering fejlede')}), 500

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
        return jsonify({'error': _log_fejl(e)}), 500


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
        leads_res = db.table('leads').select('id,navn,telefon,email,besked,status,oprettet').eq('klient_id', klient_id).order('oprettet', desc=True).limit(50).execute()
        leads_alle = leads_res.data or []
        leads_måned = [l for l in leads_alle if l.get('oprettet', '') >= maaned_start]

        # Bookinger
        book_res = db.table('bookinger').select('id,kunde_navn,oprettet,dato,tid,ydelse,portal_status').eq('klient_id', klient_id).order('dato', desc=False).limit(100).execute()
        book_alle = book_res.data or []
        book_måned = [b for b in book_alle if b.get('oprettet', '') >= maaned_start]
        # Næste kommende booking
        import datetime as _dt
        i_dag = _dt.date.today().isoformat()
        kommende = [b for b in book_alle if b.get('dato', '') >= i_dag and b.get('portal_status') != 'afsluttet']
        naeste_booking = kommende[0] if kommende else None

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
                'titel': f"Ny booking: {b.get('kunde_navn','—')}",
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

        # Autonome opfoelgningsmails som systemet har sendt paa kundens vegne.
        # Det staerkeste "selvkoerende"-signal: systemet svarede uden at ejeren
        # roerte noget. Kun status='sendt' (dvs. auto-godkendt og faktisk afsendt).
        try:
            lead_navne = {l.get('id'): l.get('navn', 'en henvendelse') for l in leads_alle}
            mails_res = db.table('lead_mails').select('lead_id,mail_nr,emne,created_at') \
                .eq('klient_id', klient_id).eq('status', 'sendt') \
                .order('created_at', desc=True).limit(30).execute()
            for m in (mails_res.data or []):
                navn = lead_navne.get(m.get('lead_id'), 'en henvendelse')
                titel = (f"System besvarede {navn} automatisk"
                         if m.get('mail_nr') == 1
                         else f"System sendte opfoelgning til {navn}")
                events.append({
                    'type': 'auto_mail',
                    'ikon': '🤖',
                    'farve': '#7c3aed',
                    'bg': '#f5f3ff',
                    'titel': titel,
                    'sub': m.get('emne', 'Opfoelgningsmail'),
                    'tid': m.get('created_at', '')
                })
        except Exception as _e:
            print(f"Auto-mail events fejl (ikke kritisk): {_e}")

        events.sort(key=lambda e: e.get('tid', ''), reverse=True)

        # ── Personlig besparelse: goer ROI konkret i TIMER + REDDEDE kunder ──
        # Autonome opfoelgningsmails afsendt denne maaned (rent selvkoerende arbejde).
        auto_mails_maaned = 0
        try:
            am = db.table('lead_mails').select('id', count='exact') \
                .eq('klient_id', klient_id).eq('status', 'sendt') \
                .gte('created_at', maaned_start).execute()
            auto_mails_maaned = am.count or 0
        except Exception:
            auto_mails_maaned = 0

        # Konservativt tidsestimat pr. handling systemet klarede for ejeren.
        # Minutter — bevidst lavt sat, saa vi aldrig oversaelger besparelsen.
        MIN_PR_LEAD, MIN_PR_MAIL, MIN_PR_BOOKING = 6, 10, 8
        sparet_min = (len(leads_måned) * MIN_PR_LEAD
                      + auto_mails_maaned * MIN_PR_MAIL
                      + len(book_måned) * MIN_PR_BOOKING)
        timer_sparet = round(sparet_min / 60, 1)

        # "Reddede" kunder: leads der landede UDEN for normal aabningstid
        # (hverdage 8-17, dansk tid). Uden systemet ville de sandsynligvis vaere
        # gaaet kolde inden nogen svarede — nu fik de oejeblikkeligt svar.
        try:
            from zoneinfo import ZoneInfo
            _dk = ZoneInfo('Europe/Copenhagen')
        except Exception:
            _dk = None
        reddet_udenfor = 0
        for l in leads_måned:
            ts = l.get('oprettet') or ''
            if not ts:
                continue
            try:
                dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
                if _dk:
                    dt = dt.astimezone(_dk)
                if dt.weekday() >= 5 or dt.hour < 8 or dt.hour >= 17:
                    reddet_udenfor += 1
            except Exception:
                continue

        leads_recent = []
        for l in leads_alle[:6]:
            leads_recent.append({
                'id': l.get('id', ''),
                'navn': l.get('navn', '—'),
                'telefon': l.get('telefon', ''),
                'email': l.get('email', ''),
                'besked': (l.get('besked') or '')[:80],
                'status': l.get('status', 'ny'),
                'oprettet': l.get('oprettet', '')
            })

        return jsonify({
            'leads_recent': leads_recent,
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
                'timer_sparet_maaned': timer_sparet,
                'reddet_maaned': reddet_udenfor,
                'auto_mails_maaned': auto_mails_maaned,
            },
            'aktivitet': events[:25],
            'naeste_booking': naeste_booking,
        })
    except Exception as e:
        return jsonify({'error': _log_fejl(e)}), 500


@app.route('/portal/siden-sidst/<klient_id>', methods=['GET'])
@require_token
def portal_siden_sidst(klient_id):
    """"Siden sidst"-resume: hvad systemet ordnede AUTONOMT mens brugeren var vaek.

    Server-side baseline pr. PERSON (ikke pr. enhed) — saa chefen der tjekker paa
    mobil om morgenen ser resumeet, selvom han normalt bruger desktop. Tallene er
    PRAECISE (count='exact', ikke cappet), saa en travl nat ikke undertaelles.

    Kaldes EN gang ved login/init (ikke i 30-sek-pollet), for baseline flyttes
    frem ved hvert visning.
    """
    from datetime import datetime, timezone, timedelta
    info = _token_info()
    if info.get('role') == 'client' and str(info.get('klient_id')) != str(klient_id):
        return jsonify({'error': 'Ingen adgang'}), 403
    if not db:
        return jsonify({'vis': False})

    # Identitet foelger personen: under-bruger = bruger_id, ellers ejeren.
    identitet = info.get('bruger_id') or f"ejer:{klient_id}"

    try:
        nu = datetime.now(timezone.utc)
        # Hent tidligere baseline for denne person.
        gammel = None
        try:
            r = db.table('portal_sidst_set').select('sidst_set').eq('identitet', identitet).single().execute()
            if r.data and r.data.get('sidst_set'):
                gammel = r.data['sidst_set']
        except Exception:
            gammel = None

        def _skriv_baseline():
            db.table('portal_sidst_set').upsert({
                'identitet': identitet, 'klient_id': str(klient_id),
                'sidst_set': nu.isoformat()
            }, on_conflict='identitet').execute()

        # Foerste besoeg: ingen baseline at sammenligne med — saet den bare.
        if not gammel:
            _skriv_baseline()
            return jsonify({'vis': False})

        # Parse baseline og maal hvor laenge de var vaek.
        try:
            gammel_dt = datetime.fromisoformat(gammel.replace('Z', '+00:00'))
        except Exception:
            _skriv_baseline()
            return jsonify({'vis': False})

        # For kort tid siden (aktiv session / hurtig genindlaesning): roer ikke
        # baseline, saa en raekke refreshes ikke skubber vinduet frem.
        if (nu - gammel_dt) < timedelta(hours=2):
            return jsonify({'vis': False})

        b = gammel_dt.isoformat()

        def _count(bygger):
            try:
                return bygger().count or 0
            except Exception:
                return 0

        # AUTONOMT arbejde foerst — det systemet klarede uden at ejeren roerte noget.
        auto_mails = _count(lambda: db.table('lead_mails').select('id', count='exact')
                            .eq('klient_id', klient_id).eq('status', 'sendt').gt('created_at', b).execute())
        leads = _count(lambda: db.table('leads').select('id', count='exact')
                       .eq('klient_id', klient_id).gt('oprettet', b).execute())
        bookinger = _count(lambda: db.table('bookinger').select('id', count='exact')
                           .eq('klient_id', klient_id).gt('oprettet', b).execute())
        tilbud_sendt = _count(lambda: db.table('tilbud').select('id', count='exact')
                              .eq('klient_id', klient_id).gt('sendt_dato', b).execute())
        tilbud_accept = _count(lambda: db.table('tilbud').select('id', count='exact')
                               .eq('klient_id', klient_id).eq('status', 'accepteret').gt('godkendt_dato', b).execute())

        # Vinduet er passeret — flyt baseline frem uanset om der var noget at vise,
        # saa vi ikke gentager samme periode ved naeste login.
        _skriv_baseline()

        if not (auto_mails or leads or bookinger or tilbud_sendt or tilbud_accept):
            return jsonify({'vis': False})

        return jsonify({
            'vis': True,
            'sidst': b,
            'auto_mails': auto_mails,
            'leads': leads,
            'bookinger': bookinger,
            'tilbud_sendt': tilbud_sendt,
            'tilbud_accepteret': tilbud_accept,
        })
    except Exception as e:
        # Aldrig laad et resume-fejl braekke overblikket.
        print(f"siden-sidst fejl (ikke kritisk): {e}")
        return jsonify({'vis': False})


@app.route('/portal/overblik-total/<klient_id>', methods=['GET'])
@require_token
def portal_overblik_total(klient_id):
    """Forretnings-overblik på tværs af moduler: tragt, vundne penge og fælles tidslinje.

    Tallene beregnes fra kildetabellerne (fuld historik, virker straks).
    Tidslinjen læses fra aktivitet-tabellen (fylder op efterhånden som hændelser sker).
    """
    from datetime import datetime, timezone
    raw = request.headers.get('Authorization', '')
    token = raw.replace('Bearer ', '').strip()
    info = active_tokens.get(token, {})
    if info.get('role') == 'client' and info.get('klient_id') != klient_id:
        return jsonify({'error': 'Ingen adgang'}), 403
    if not db:
        return jsonify({'maaned': {}, 'i_alt': {}, 'tidslinje': []})

    try:
        nu = datetime.now(timezone.utc)
        maaned_start = nu.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()

        # ── Tragten: samme person hele vejen, beregnet fra kildetabeller ──
        leads = db.table('leads').select('oprettet').eq('klient_id', klient_id).execute().data or []
        tilbud = db.table('tilbud').select('total_pris,status,sendt_dato,godkendt_dato').eq('klient_id', klient_id).execute().data or []
        bookinger = db.table('bookinger').select('oprettet').eq('klient_id', klient_id).execute().data or []

        def i_maaned(rows, felt):
            return [r for r in rows if (r.get(felt) or '') >= maaned_start]

        tilbud_sendt = [t for t in tilbud if t.get('sendt_dato')]
        tilbud_vundet = [t for t in tilbud if t.get('status') == 'accepteret']

        tilbud_sendt_m = i_maaned(tilbud_sendt, 'sendt_dato')
        tilbud_vundet_m = i_maaned(tilbud_vundet, 'godkendt_dato')

        def sum_beloeb(rows):
            return sum(int(t.get('total_pris', 0) or 0) for t in rows)

        def tragt(leads_n, sendt_n, vundet_n):
            return {
                'leads': leads_n,
                'tilbud_sendt': sendt_n,
                'tilbud_vundet': vundet_n,
                'konvertering': round(vundet_n / sendt_n * 100) if sendt_n else 0,
            }

        maaned = {
            **tragt(len(i_maaned(leads, 'oprettet')), len(tilbud_sendt_m), len(tilbud_vundet_m)),
            'bookinger': len(i_maaned(bookinger, 'oprettet')),
            'vundet_beloeb': sum_beloeb(tilbud_vundet_m),
        }
        i_alt = {
            **tragt(len(leads), len(tilbud_sendt), len(tilbud_vundet)),
            'bookinger': len(bookinger),
            'vundet_beloeb': sum_beloeb(tilbud_vundet),
        }

        # ── Fælles tidslinje fra aktivitet-tabellen (graceful hvis tom/mangler) ──
        tidslinje = []
        try:
            akt = db.table('aktivitet').select('type,titel,beloeb,kontakt_email,modul,oprettet') \
                .eq('klient_id', klient_id).order('oprettet', desc=True).limit(30).execute()
            tidslinje = akt.data or []
        except Exception as e:
            print(f"Aktivitet-laesning fejl (koer migrations/aktivitet.sql?): {e}")

        return jsonify({'maaned': maaned, 'i_alt': i_alt, 'tidslinje': tidslinje})
    except Exception as e:
        return jsonify({'error': _log_fejl(e)}), 500


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
        res = db.table('tilbud').select('id,kunde_navn,kunde_email,kunde_adresse,kunde_postnummer,titel,total_pris,status,oprettet,godkendt_dato,sendt_dato,åbnet_dato').eq('klient_id', klient_id).order('oprettet', desc=True).execute()
        return jsonify(res.data or [])
    except Exception as e:
        return jsonify({'error': _log_fejl(e)}), 500


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
        return jsonify({'error': _log_fejl(e)}), 500


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
        print(f"tilbud-preview fejl: {e}")
        return '<html><body style="font-family:sans-serif;padding:2rem">Der opstod en teknisk fejl.</body></html>', 500


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
        return jsonify({'error': _log_fejl(e)}), 500


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
        fra_navn = 'Nordolsen'
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

        # Generer PDF og vedhæft mailen
        pdf_bytes = generer_tilbud_pdf(html_til_kunde)
        sikkert_filnavn = ''.join(c for c in titel if c.isalnum() or c in ' -_')[:40].strip() or 'tilbud'
        pdf_filnavn = f'{sikkert_filnavn}.pdf'

        # Kundens egen tilbuds-mailtekst/emne hvis sat, ellers systemets standard.
        _mcfg = hent_mail_config(klient_id)
        _emne_raw = (_mcfg.get('tilbud_mail_emne') or '').strip()
        _emne = _emne_raw.replace('{titel}', titel).replace('{firma}', fra_navn) if _emne_raw else f'Tilbud: {titel}'
        _std_tekst = f'Hej {kunde_navn},\n\nHermed dit tilbud fra {fra_navn}.\n\nTilbuddet er vedhæftet som PDF — du kan godkende det via knappen i mailen.'
        _tekst = render_tilbud_mailtekst(_mcfg.get('tilbud_mail_tekst'), kunde_navn, fra_navn) or _std_tekst

        send_mail(
            kunde_email,
            _emne,
            _tekst,
            fra_navn=fra_navn,
            html_content=html_til_kunde,
            pdf_vedhæft=pdf_bytes,
            pdf_filnavn=pdf_filnavn
        )

        db.table('tilbud').update({'status': 'sendt', 'sendt_dato': datetime.now().isoformat()}).eq('id', tilbud_id).execute()
        log_aktivitet(tilbud.get('klient_id',''), 'tilbud_sendt', f"Tilbud sendt — {titel}", kunde_email, beloeb=tilbud.get('total_pris'), reference_id=tilbud_id, modul='tilbud')

        return jsonify({'ok': True, 'sendt_til': kunde_email, 'pdf_vedhæftet': pdf_bytes is not None})
    except Exception as e:
        return jsonify({'error': _log_fejl(e)}), 500


@app.route('/portal/tilbud/send/<tilbud_id>', methods=['POST'])
@require_token
def portal_send_tilbud(tilbud_id):
    """Klient sender sit eget tilbud til kunden — med valgfri email-tekst override"""
    if not db:
        return jsonify({'error': 'Ingen database'}), 500
    try:
        res = db.table('tilbud').select('*').eq('id', tilbud_id).single().execute()
        tilbud = res.data
        if not tilbud:
            return jsonify({'error': 'Tilbud ikke fundet'}), 404

        klient_id = tilbud.get('klient_id', '')
        if _ingen_adgang(klient_id):
            return jsonify({'error': 'Ikke adgang til dette tilbud'}), 403

        kunde_email = tilbud.get('kunde_email', '')
        kunde_navn  = tilbud.get('kunde_navn', '')
        titel       = tilbud.get('titel', 'Tilbud')
        html        = tilbud.get('html_indhold', '')

        if not kunde_email or '@' not in kunde_email:
            return jsonify({'error': 'Ingen gyldig email på tilbuddet'}), 400

        body = request.json or {}
        email_emne_override   = body.get('email_emne', '').strip()
        aktiver_followup      = body.get('aktiver_followup', True)

        fra_navn = 'Nordolsen'
        klient_hjemmeside = ''
        try:
            k = db.table('klienter').select('navn,hjemmeside,email').eq('id', klient_id).single().execute()
            if k.data:
                fra_navn = k.data.get('navn', fra_navn)
                klient_hjemmeside = k.data.get('hjemmeside', '')
        except:
            pass

        import uuid as _uuid
        accept_token = str(_uuid.uuid4()).replace('-', '')
        db.table('tilbud').update({'accept_token': accept_token}).eq('id', tilbud_id).execute()

        accept_url = f'{SERVER_URL}/tilbud/godkend/{tilbud_id}/{accept_token}'
        domain = klient_hjemmeside.replace('https://','').replace('http://','').replace('www.','').rstrip('/')
        trustpilot_url = f'https://dk.trustpilot.com/review/{domain}' if domain else 'https://dk.trustpilot.com'

        import re as _re
        html_til_kunde = _re.sub(r'<!-- KONKURRENT.*?KONKURRENT -->', '', html, flags=_re.DOTALL)

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
    <a href="{trustpilot_url}" style="display:inline-block;background:#00b67a;color:#fff;text-decoration:none;font-size:13px;font-weight:700;padding:10px 24px;border-radius:8px">Skriv en anmeldelse &#8594;</a>
  </td></tr>"""

        html_til_kunde = html_til_kunde.replace('<!-- FOOTER -->', ekstra_blok + '\n  <!-- FOOTER -->')

        pdf_bytes = generer_tilbud_pdf(html_til_kunde)
        sikkert_filnavn = ''.join(c for c in titel if c.isalnum() or c in ' -_')[:40].strip() or 'tilbud'

        # Emne: manuel override (fra send-dialogen) vinder; ellers kundens
        # gemte standard-emne; ellers systemets fallback.
        _mcfg = hent_mail_config(klient_id)
        _emne_raw = (_mcfg.get('tilbud_mail_emne') or '').strip()
        _emne_std = _emne_raw.replace('{titel}', titel).replace('{firma}', fra_navn) if _emne_raw else f'Tilbud: {titel}'
        emne = email_emne_override or _emne_std
        _std_tekst = f'Hej {kunde_navn},\n\nHermed dit tilbud fra {fra_navn}.\n\nTilbuddet er vedhæftet som PDF.'
        _tekst = render_tilbud_mailtekst(_mcfg.get('tilbud_mail_tekst'), kunde_navn, fra_navn) or _std_tekst

        send_mail(
            kunde_email, emne,
            _tekst,
            fra_navn=fra_navn,
            html_content=html_til_kunde,
            pdf_vedhæft=pdf_bytes,
            pdf_filnavn=f'{sikkert_filnavn}.pdf'
        )

        from datetime import datetime as _dt2
        db.table('tilbud').update({
            'status': 'sendt',
            'sendt_dato': _dt2.now().isoformat(),
            'followup_aktiveret': aktiver_followup
        }).eq('id', tilbud_id).execute()
        log_aktivitet(klient_id, 'tilbud_sendt', f"Tilbud sendt — {titel}", kunde_email, beloeb=tilbud.get('total_pris'), reference_id=tilbud_id, modul='tilbud')

        # Auto-upsert CRM kontakt
        try:
            supabase.table('crm_kontakter').upsert({
                'klient_id': str(klient_id),
                'email': kunde_email,
                'navn': kunde_navn,
                'sidst_opdateret': _dt2.utcnow().isoformat()
            }, on_conflict='klient_id,email').execute()
        except:
            pass

        return jsonify({'ok': True, 'sendt_til': kunde_email})
    except Exception as e:
        return jsonify({'error': _log_fejl(e)}), 500


@app.route('/portal/tilbud/followup-preview', methods=['POST'])
@require_token
def portal_followup_preview():
    """Returnerer preview-HTML for opfølgningsmail trin 1"""
    body = request.json or {}
    fornavn   = body.get('fornavn', 'kunde')
    titel     = body.get('titel', 'tilbuddet')
    total     = body.get('total_pris', 0)
    fra_navn  = body.get('fra_navn', 'os')
    hjemmeside = body.get('hjemmeside', '')
    accept_knap = '<div style="text-align:center;margin:20px 0"><a href="#" style="display:inline-block;background:#16a34a;color:#fff;text-decoration:none;font-size:15px;font-weight:700;padding:14px 40px;border-radius:10px">✓ Godkend tilbud</a></div>'
    kunde_navn = fornavn
    html, emne = _byg_followup_html(fra_navn, hjemmeside, kunde_navn, titel, accept_knap, 1, total)
    # Udtræk plain text intro
    tekst = f'Hej {fornavn},\n\nJeg ville høre om du har haft mulighed for at kigge på tilbuddet på {titel}?\n\nHar du spørgsmål er du meget velkommen til at svare direkte på denne mail.\n\nMed venlig hilsen\n{fra_navn}'
    return jsonify({'ok': True, 'html': html, 'emne': emne, 'tekst': tekst})


# ── CRM KONTAKTER (portal) ──────────────────────────────
@app.route('/portal/crm/upsert', methods=['POST'])
@require_token
def portal_crm_upsert():
    """Gem eller opdater kontaktinfo på en kunde"""
    body = request.json or {}
    klient_id = str(request.user_klient_id)
    email = body.get('email', '').strip().lower()
    if not email:
        return jsonify({'error': 'Email mangler'}), 400
    from datetime import datetime as _dt2
    try:
        supabase.table('crm_kontakter').upsert({
            'klient_id': klient_id,
            'email': email,
            'navn': body.get('navn', ''),
            'telefon': body.get('telefon', ''),
            'adresse': body.get('adresse', ''),
            'postnummer': body.get('postnummer', ''),
            'sidst_opdateret': _dt2.utcnow().isoformat()
        }, on_conflict='klient_id,email').execute()
        # Tilføj automatisk note hvis der er en
        note = body.get('note', '').strip()
        if note:
            existing = supabase.table('crm_kontakter').select('noter').eq('klient_id', klient_id).eq('email', email).maybe_single().execute()
            noter = existing.data.get('noter') or [] if existing.data else []
            if isinstance(noter, str):
                import json as _j; noter = _j.loads(noter)
            noter.insert(0, {'tekst': note, 'dato': _dt2.utcnow().isoformat()})
            supabase.table('crm_kontakter').update({'noter': noter}).eq('klient_id', klient_id).eq('email', email).execute()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': _log_fejl(e)}), 500


@app.route('/portal/crm/note', methods=['POST'])
@require_token
def portal_crm_note():
    """Tilføj note til en kontakt"""
    body = request.json or {}
    klient_id = str(request.user_klient_id)
    email = body.get('email', '').strip().lower()
    tekst = body.get('tekst', '').strip()
    if not email or not tekst:
        return jsonify({'error': 'Email og tekst kræves'}), 400
    from datetime import datetime as _dt2
    try:
        existing = supabase.table('crm_kontakter').select('noter').eq('klient_id', klient_id).eq('email', email).maybe_single().execute()
        noter = []
        if existing.data:
            noter = existing.data.get('noter') or []
            if isinstance(noter, str):
                import json as _j; noter = _j.loads(noter)
        noter.insert(0, {'tekst': tekst, 'dato': _dt2.utcnow().isoformat()})
        supabase.table('crm_kontakter').upsert({
            'klient_id': klient_id, 'email': email, 'noter': noter,
            'sidst_opdateret': _dt2.utcnow().isoformat()
        }, on_conflict='klient_id,email').execute()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': _log_fejl(e)}), 500


@app.route('/portal/crm/status', methods=['PATCH'])
@require_token
def portal_crm_status():
    """Opdater kontaktstatus (ny/kontaktet/møde/kunde)"""
    body = request.json or {}
    klient_id = str(request.user_klient_id)
    email = body.get('email', '').strip().lower()
    status = body.get('status', 'ny')
    if status not in ('ny', 'kontaktet', 'møde', 'kunde'):
        return jsonify({'error': 'Ugyldig status'}), 400
    from datetime import datetime as _dt2
    try:
        supabase.table('crm_kontakter').upsert({
            'klient_id': klient_id, 'email': email, 'status': status,
            'sidst_opdateret': _dt2.utcnow().isoformat()
        }, on_conflict='klient_id,email').execute()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': _log_fejl(e)}), 500


@app.route('/portal/crm/<klient_id>/<email>', methods=['GET'])
@require_token
def portal_crm_hent(klient_id, email):
    """Hent CRM kontaktdata for én email (status + noter)"""
    if _ingen_adgang(klient_id):
        return jsonify({'error': 'Ingen adgang'}), 403
    email = email.strip().lower()
    try:
        res = supabase.table('crm_kontakter').select('*').eq('klient_id', klient_id).eq('email', email).maybe_single().execute()
        if not res.data:
            return jsonify({'ok': True, 'kontakt': None})
        return jsonify({'ok': True, 'kontakt': res.data})
    except Exception as e:
        print(f"portal_crm_hent fejl: {e}")
        return jsonify({'error': 'Kunne ikke hente kontakt'}), 500


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

        # Log at tilbuddet er åbnet (første gang)
        if not t.get('åbnet_dato') and t.get('status') == 'sendt':
            try:
                from datetime import datetime
                db.table('tilbud').update({'åbnet_dato': datetime.now().isoformat()}).eq('id', tilbud_id).execute()
            except:
                pass

        if t.get('status') == 'accepteret':
            godkendt_dato = t.get('godkendt_dato', '')[:19].replace('T', ' kl. ') if t.get('godkendt_dato') else '—'
            return _godkend_side('Allerede bekræftet',
                f'Dit tilbud "{html.escape(t.get("titel",""))}" er allerede godkendt den {godkendt_dato}. '
                f'Du har modtaget en bekræftelse på {html.escape(t.get("kunde_email","din email"))}.',
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
        # HTML-escapede versioner til brug i mail/side (XSS-beskyttelse)
        kunde_navn_h  = html.escape(kunde_navn)
        kunde_email_h = html.escape(kunde_email)
        titel_h       = html.escape(titel)
        total_pris  = int(t.get('total_pris', 0))
        log_aktivitet(t.get('klient_id',''), 'tilbud_godkendt', f"Tilbud godkendt — {titel}", kunde_email, beloeb=total_pris, reference_id=tilbud_id, modul='tilbud')
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
        klient_navn_h    = html.escape(klient_navn)
        klient_email_h   = html.escape(klient_email)
        klient_telefon_h = html.escape(klient_telefon)

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
      Hej <strong>{kunde_navn_h}</strong>,<br><br>
      Dette er din officielle bekræftelse på at du har accepteret følgende tilbud:
    </div>

    <table width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #d1fae5;border-radius:10px;overflow:hidden;margin-bottom:24px">
      <tr style="background:#f0fdf4">
        <td colspan="2" style="padding:12px 16px;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:1px;color:#15803d">Tilbudsdetaljer</td>
      </tr>
      <tr style="border-top:1px solid #d1fae5">
        <td style="padding:10px 16px;font-size:13px;color:#6b7280;width:40%">Tilbud</td>
        <td style="padding:10px 16px;font-size:13px;font-weight:600;color:#111">{titel_h}</td>
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
        <td style="padding:10px 16px;font-size:13px;color:#111">{kunde_email_h}</td>
      </tr>
    </table>

    <div style="background:#fefce8;border:1px solid #fde68a;border-radius:8px;padding:14px 16px;margin-bottom:24px">
      <div style="font-size:12px;color:#92400e;line-height:1.7">
        <strong>Juridisk note:</strong> Din accept er registreret elektronisk og er bindende i henhold til dansk Aftaleloven §1 og E-handelsloven. Denne email tjener som dit skriftlige bevis på accept. Gem den til fremtidig reference.
      </div>
    </div>

    <div style="font-size:14px;color:#374151;line-height:1.8">
      <strong>{klient_navn_h}</strong> vil kontakte dig hurtigst muligt for at aftale de næste skridt.
      {'<br>Email: ' + klient_email_h if klient_email else ''}
      {'<br>Telefon: ' + klient_telefon_h if klient_telefon else ''}
    </div>
  </td></tr>

  <tr><td style="background:#f9fafb;padding:18px 36px;text-align:center;border-top:1px solid #f0f0f0">
    <div style="font-size:11px;color:#9ca3af">Reference: {ref_nr} &middot; Genereret af Nordolsen</div>
  </td></tr>

</table></td></tr></table>
</body></html>"""

        try:
            send_mail(kunde_email, f'Bekræftelse: Du har godkendt "{titel}"', f'Hej {kunde_navn},\n\nDette er din bekræftelse på at du har accepteret tilbuddet "{titel}" ({total_pris:,} kr.).\n\nReference: {ref_nr}', fra_navn=klient_navn or 'Nordolsen', html_content=bekræftelse_html)
        except Exception as e:
            print(f'Bekræftelsesmail fejl: {e}')

        # Notificér klienten (virksomheden)
        try:
            if klient_email:
                notif_html = (
                    f'<div style="font-family:sans-serif;padding:20px">'
                    f'<h2 style="color:#15803d">&#9989; Tilbud accepteret!</h2>'
                    f'<p><strong>{kunde_navn_h}</strong> har accepteret tilbuddet <strong>"{titel_h}"</strong>.</p>'
                    f'<table style="border-collapse:collapse;width:100%;max-width:400px">'
                    f'<tr><td style="padding:8px;color:#666;border-bottom:1px solid #eee">Beløb</td><td style="padding:8px;font-weight:700;border-bottom:1px solid #eee">{total_pris:,} kr.</td></tr>'
                    f'<tr><td style="padding:8px;color:#666;border-bottom:1px solid #eee">Tidspunkt</td><td style="padding:8px;border-bottom:1px solid #eee">{dato_str}</td></tr>'
                    f'<tr><td style="padding:8px;color:#666">Reference</td><td style="padding:8px;font-family:monospace">{ref_nr}</td></tr>'
                    f'</table></div>'
                )
                send_mail(klient_email, f'✅ Tilbud accepteret: {titel}', f'{kunde_navn} har accepteret tilbuddet "{titel}" ({total_pris:,} kr.).', fra_navn='Nordolsen', html_content=notif_html)
        except:
            pass

        return _godkend_side(
            'Tilbud godkendt!',
            f'Tak, {html.escape(kunde_navn.split()[0]) if kunde_navn else ""}! Vi har modtaget din accept og sender dig straks en bekræftelse på {kunde_email_h}. {klient_navn_h} kontakter dig snarest for at sætte projektet i gang.',
            ref_nr=ref_nr,
            dato_str=dato_str,
            fejl=False
        ), 200
    except Exception as e:
        print(f'godkend_tilbud fejl: {e}')
        return _godkend_side('Fejl', 'Der opstod en teknisk fejl. Kontakt os direkte.', fejl=True), 500


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


@app.route('/portal/tilbud/status/<tilbud_id>', methods=['PATCH'])
@require_token
def portal_opdater_tilbud_status(tilbud_id):
    """Klientportal: opdaterer status (klient-token må kun ændre egne tilbud)"""
    if not db:
        return jsonify({'error': 'Ingen database'}), 500
    raw = request.headers.get('Authorization', '')
    token = raw.replace('Bearer ', '').strip()
    info = active_tokens.get(token, {})
    data = request.json or {}
    ny_status = data.get('status', '')
    if ny_status not in ('udkast', 'sendt', 'accepteret', 'afvist'):
        return jsonify({'error': 'Ugyldig status'}), 400
    try:
        res = db.table('tilbud').select('klient_id').eq('id', tilbud_id).single().execute()
        if info.get('role') == 'client' and info.get('klient_id') != res.data.get('klient_id'):
            return jsonify({'error': 'Ingen adgang'}), 403
        db.table('tilbud').update({'status': ny_status}).eq('id', tilbud_id).execute()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': _log_fejl(e)}), 500


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
        return jsonify({'error': _log_fejl(e)}), 500


@app.route('/portal/kunde/<klient_id>/<kunde_email_encoded>', methods=['GET'])
@require_token
def portal_kunde_historik(klient_id, kunde_email_encoded):
    """Klientportal: al historik for én kunde (leads + tilbud + bookinger)"""
    import urllib.parse
    raw = request.headers.get('Authorization', '')
    token = raw.replace('Bearer ', '').strip()
    info = active_tokens.get(token, {})
    if info.get('role') == 'client' and info.get('klient_id') != klient_id:
        return jsonify({'error': 'Ingen adgang'}), 403
    if not db:
        return jsonify({})

    kunde_email = urllib.parse.unquote(kunde_email_encoded)
    try:
        leads_res = db.table('leads').select('*').eq('klient_id', klient_id).eq('email', kunde_email).order('oprettet', desc=True).execute()
        tilbud_res = db.table('tilbud').select('id,titel,total_pris,status,oprettet,godkendt_dato,sendt_dato').eq('klient_id', klient_id).eq('kunde_email', kunde_email).order('oprettet', desc=True).execute()
        book_res = db.table('bookinger').select('*').eq('klient_id', klient_id).eq('kunde_email', kunde_email).order('oprettet', desc=True).execute()

        leads = leads_res.data or []
        tilbud = tilbud_res.data or []
        bookinger = book_res.data or []

        # Navn fra første fund
        navn = (leads[0].get('navn') if leads else None) or (tilbud[0].get('kunde_navn') if tilbud else None) or kunde_email

        return jsonify({
            'navn': navn,
            'email': kunde_email,
            'leads': leads,
            'tilbud': tilbud,
            'bookinger': bookinger,
            'total_tilbud_accepteret': sum(t.get('total_pris', 0) or 0 for t in tilbud if t.get('status') == 'accepteret')
        })
    except Exception as e:
        return jsonify({'error': _log_fejl(e)}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    print(f"🤖 Nordolsen Agent Server kører på port {port}")
    app.run(host='0.0.0.0', port=port, debug=False)

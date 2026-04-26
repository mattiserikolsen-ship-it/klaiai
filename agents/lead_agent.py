#!/usr/bin/env python3
"""
SittamTech Lead Agent
Genererer personlige opfølgningsmails til nye leads via Claude API
og sender dem via Gmail SMTP
"""

import anthropic
import smtplib
import json
import os
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

ai = anthropic.Anthropic()

# Gmail konfiguration (sættes via miljøvariabler)
GMAIL_USER = os.environ.get('GMAIL_USER', '')
GMAIL_APP_PASSWORD = os.environ.get('GMAIL_APP_PASSWORD', '')


def generer_mail(lead: dict, klient: dict, mail_nr: int = 1) -> dict:
    """Genererer en personlig opfølgningsmail til et lead via Claude."""

    prompt = f"""Du er en professionel salgsassistent for {klient['navn']}.

Skriv en kort, personlig opfølgningsmail (mail #{mail_nr}) til dette lead:

Lead navn: {lead.get('navn', 'der')}
Lead virksomhed: {lead.get('virksomhed', '')}
Lead henvendelse: {lead.get('besked', 'Generel forespørgsel')}

Om {klient['navn']}:
Ydelser: {klient.get('ydelser', '')}
Tilbud til nye leads: {klient.get('tilbud', 'Gratis uforpligtende samtale')}
Kontakt: {klient.get('kontakt', '')}

Mail #{mail_nr} instruktioner:
{'Svar straks og tak for henvendelsen. Introducer dig og tilbyd hjælp.' if mail_nr == 1 else ''}
{'Følg op venligt. Spørg om de har haft mulighed for at kigge på tilbuddet.' if mail_nr == 2 else ''}
{'Sidste opfølgning. Gør det personligt og lav et konkret tilbud.' if mail_nr == 3 else ''}

Format:
- Emne: [Skriv et fængende emne]
- Brødtekst: Kort (3-5 linjer), personlig, professionel dansk
- Afslut med navn og kontaktinfo fra klienten
- Ingen emojis
- Ingen salgsjargon — vær menneskelig

Svar KUN med dette format:
EMNE: <emnet her>
TEKST:
<brødteksten her>"""

    response = ai.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}]
    )

    tekst = response.content[0].text.strip()

    # Parse emne og tekst
    lines = tekst.split('\n')
    emne = ''
    body_lines = []
    i_body = False

    for line in lines:
        if line.startswith('EMNE:'):
            emne = line.replace('EMNE:', '').strip()
        elif line.startswith('TEKST:'):
            i_body = True
        elif i_body:
            body_lines.append(line)

    return {
        'emne': emne or f'Opfølgning fra {klient["navn"]}',
        'tekst': '\n'.join(body_lines).strip()
    }


def send_mail(til: str, emne: str, tekst: str, fra_navn: str) -> bool:
    """Sender en mail via Gmail SMTP."""
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        print(f"⚠️  Gmail ikke konfigureret — ville have sendt til {til}")
        print(f"   Emne: {emne}")
        print(f"   Tekst:\n{tekst}")
        return False

    msg = MIMEMultipart('alternative')
    msg['Subject'] = emne
    msg['From'] = f"{fra_navn} <{GMAIL_USER}>"
    msg['To'] = til

    # Plain text
    msg.attach(MIMEText(tekst, 'plain', 'utf-8'))

    # HTML version
    html = f"""
    <html><body style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:20px;color:#333">
    {'<br>'.join(tekst.split(chr(10)))}
    <hr style="margin-top:30px;border:none;border-top:1px solid #eee">
    <p style="font-size:12px;color:#999">Sendt via SittamTech</p>
    </body></html>
    """
    msg.attach(MIMEText(html, 'html', 'utf-8'))

    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_USER, til, msg.as_string())
        print(f"✅ Mail sendt til {til}: {emne}")
        return True
    except Exception as e:
        print(f"❌ Fejl ved afsendelse: {e}")
        return False


def behandl_lead(lead: dict, klient: dict, send: bool = False):
    """Genererer og evt. sender opfølgningsmails til et lead."""

    print(f"\n🎯 Behandler lead: {lead.get('navn')} ({lead.get('email')})")
    print(f"   Klient: {klient['navn']}")
    print("-" * 50)

    resultater = []
    for nr in [1, 2, 3]:
        print(f"\n📧 Genererer mail #{nr}...")
        mail = generer_mail(lead, klient, nr)
        print(f"   Emne: {mail['emne']}")
        print(f"   Tekst:\n{mail['tekst']}\n")

        if send and lead.get('email'):
            sendt = send_mail(
                til=lead['email'],
                emne=mail['emne'],
                tekst=mail['tekst'],
                fra_navn=klient['navn']
            )
            mail['sendt'] = sendt
        else:
            mail['sendt'] = False

        resultater.append(mail)

    return resultater


if __name__ == "__main__":
    # Test lead
    test_lead = {
        "navn": "Anders Hansen",
        "email": "anders@eksempel.dk",
        "virksomhed": "Hansen Holding",
        "besked": "Vi har en pool på ca. 50 kvm og ønsker ugentlig rengøring i sommersæsonen"
    }

    test_klient = {
        "navn": "Petersens Pool Service",
        "ydelser": "Poolrengøring, kemikalier, vinterlukning, sæsonåbning",
        "tilbud": "Gratis besigtigelse og tilbud uden forpligtelse",
        "kontakt": "Peter Petersen | +45 98 76 54 32 | info@petersenspool.dk"
    }

    resultater = behandl_lead(test_lead, test_klient, send=False)
    print(f"\n✅ {len(resultater)} mails genereret til {test_lead['navn']}")
    print("🤖 SittamTech Lead Agent færdig!")

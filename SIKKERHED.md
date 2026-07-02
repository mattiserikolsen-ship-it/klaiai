# Datasikkerhed hos Nordolsen

_Sidst opdateret: 2. juli 2026_

Dette dokument beskriver, hvordan Nordolsen beskytter dine og dine kunders data.
Det er skrevet så det kan deles med kunder, der spørger til datasikkerhed.

---

## Hvor ligger data

- Al data gemmes i en **PostgreSQL-database hos Supabase** i **EU (Irland, eu-west-1)**.
- Databehandlingen sker dermed inden for EU — relevant for **GDPR**.
- Systemet kører på en dedikeret server (Render.com) med krypteret forbindelse.

## Adgangskontrol

- Adgang kræver **login med email og adgangskode**. Ingen data kan tilgås uden gyldigt login.
- Hver kunde kan **kun se sine egne data** — leads, tilbud, bookinger og kontakter er adskilt pr. konto.
- Login giver en **tidsbegrænset sikkerhedsnøgle (token)**, der automatisk udløber.
- Administrator-adgang er beskyttet separat og kan begrænses til kun lokal adgang.

## Beskyttelse af adgangskoder

- Kunders adgangskoder gemmes **aldrig i klartekst** — de gemmes som **bcrypt-hash**.
- Selv hvis databasen blev læst, kan adgangskoderne ikke udledes.

## Kryptering

- Al trafik mellem browser og server sendes over **HTTPS (TLS)** — krypteret undervejs.
- Database-forbindelsen er ligeledes krypteret.

## Databasebeskyttelse

- **Row Level Security (RLS)** er slået til på alle tabeller i databasen.
- Ingen offentlig nøgle kan læse eller ændre data direkte — **al adgang går gennem serveren**, der håndhæver, hvem der må se hvad.
- Widgets på kundernes hjemmesider (chatbot, lead- og bookingformular) kan kun oprette henvendelser — de kan **ikke læse eksisterende data**.

## Beskyttelse mod almindelige angreb

- **XSS (skadelig kode i input):** Alle felter fra kunder og besøgende renses, før de vises.
- **SSRF (misbrug af scanning):** Systemet blokerer forsøg på at få serveren til at kontakte interne/private adresser.
- **Fejlmeddelelser** afslører ikke interne tekniske detaljer udadtil.
- **Rate limiting** på login begrænser gættede adgangsforsøg.

## Hemmeligheder og nøgler

- API-nøgler og adgangskoder ligger **udelukkende i sikre miljøvariabler** på serveren.
- De findes **ikke** i kildekoden og deles ikke med tredjepart udover de tjenester, der driver løsningen.

## Underleverandører (databehandlere)

Løsningen anvender følgende betroede leverandører:

| Leverandør | Funktion | Placering |
|------------|----------|-----------|
| Supabase | Database | EU (Irland) |
| Render.com | Serverdrift | EU/US |
| SendGrid | Udsendelse af e-mails | EU/US |
| Anthropic | Chatbot og tekstgenerering | US |

## Kontakt

Spørgsmål om datasikkerhed eller databehandleraftale (DPA) kan rettes til Nordolsen.

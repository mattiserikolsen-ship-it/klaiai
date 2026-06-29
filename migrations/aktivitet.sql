-- Aktivitet: fælles tidslinje på tværs af alle moduler
-- Hver hændelse (lead, tilbud sendt/godkendt, booking) bliver én linje her.
-- Driver kundekortets tidslinje og forretnings-overblikkets tal.
-- Kør i Supabase SQL Editor

CREATE TABLE IF NOT EXISTS aktivitet (
  id             SERIAL PRIMARY KEY,
  klient_id      TEXT NOT NULL,
  kontakt_email  TEXT,                 -- binder til crm_kontakter via email
  type           TEXT NOT NULL,        -- lead / tilbud_sendt / tilbud_godkendt / booking
  titel          TEXT,                 -- kort beskrivelse, fx "Nyt lead via chatbot — Mette"
  beloeb         NUMERIC,              -- valgfri (tilbud/faktura-beløb)
  reference_id   TEXT,                 -- id på lead/tilbud/booking det refererer til
  modul          TEXT,                 -- leads / tilbud / bookinger
  oprettet       TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS aktivitet_klient_idx  ON aktivitet(klient_id);
CREATE INDEX IF NOT EXISTS aktivitet_kontakt_idx ON aktivitet(klient_id, kontakt_email);
CREATE INDEX IF NOT EXISTS aktivitet_dato_idx    ON aktivitet(klient_id, oprettet DESC);

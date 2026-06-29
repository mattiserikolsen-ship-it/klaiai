-- CRM kontakter tabel
-- Kør i Supabase SQL Editor

CREATE TABLE IF NOT EXISTS crm_kontakter (
  id               SERIAL PRIMARY KEY,
  klient_id        TEXT NOT NULL,
  email            TEXT NOT NULL,
  navn             TEXT,
  telefon          TEXT,
  adresse          TEXT,
  postnummer       TEXT,
  status           TEXT DEFAULT 'ny',  -- ny / kontaktet / møde / kunde
  noter            JSONB DEFAULT '[]',
  tags             TEXT[] DEFAULT '{}',
  sidst_opdateret  TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE(klient_id, email)
);

ALTER TABLE tilbud ADD COLUMN IF NOT EXISTS followup_aktiveret BOOLEAN DEFAULT true;

CREATE INDEX IF NOT EXISTS crm_kontakter_klient_idx ON crm_kontakter(klient_id);

-- E-conomic integration migration
-- Kør i Supabase SQL Editor

CREATE TABLE IF NOT EXISTS klient_integrationer (
  id              SERIAL PRIMARY KEY,
  klient_id       TEXT NOT NULL UNIQUE,
  economic_token  TEXT,
  economic_navn   TEXT,
  opdateret       TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE tilbud ADD COLUMN IF NOT EXISTS economic_faktura_nr INTEGER;
ALTER TABLE tilbud ADD COLUMN IF NOT EXISTS economic_synced TIMESTAMPTZ;

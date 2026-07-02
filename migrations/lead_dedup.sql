-- Idempotens for lead-fangst: en klient-genereret dedup_id pr. henvendelse.
-- Widget'en gensender leads (retry + lokal ko naar backend er nede) — dedup_id
-- sikrer at samme henvendelse aldrig bliver til to leads eller to mail-flows.
-- Koer i Supabase SQL Editor.

ALTER TABLE leads ADD COLUMN IF NOT EXISTS dedup_id TEXT;

-- Hurtig opslag ved dedup-tjek (klient_id + dedup_id).
CREATE INDEX IF NOT EXISTS leads_dedup_idx ON leads(klient_id, dedup_id);

-- Email-ingestion: kundens info@-mails videresendes til Nordolsen og bliver
-- til kategoriserede indbakke-poster. Multi-tenant: kunde-noeglen ligger i
-- selve modtager-adressen ({inbound_token}@leads.klaai.dk), saa ET subdomaene
-- + EN SendGrid Inbound Parse-regel betjener alle kunder. Onboarding = generer
-- en token. Ingen ny DNS/opsaetning pr. kunde.

-- 1) Unik, ikke-gaettelig mailbox-token pr. klient.
alter table klienter
  add column if not exists inbound_token text;

create unique index if not exists klienter_inbound_token_uidx
  on klienter (inbound_token)
  where inbound_token is not null;

-- 2) Indbakke: hver indkommen mail gemmes her, kategoriseret + med signaler.
--    Lead/booking-kategorier opretter OGSAA et lead (leads-tabellen) saa det
--    autonome flow fyrer; lead_id linker tilbage hertil. Ikke-lead-kategorier
--    (faktura, erhverv, spam) lever kun her som triage.
create table if not exists indbakke_mails (
  id            uuid primary key default gen_random_uuid(),
  klient_id     text not null,
  fra_email     text,
  fra_navn      text,
  emne          text,
  besked        text,
  kategori      text default 'til_gennemsyn',  -- nyt_lead | booking | eksisterende_kunde | faktura | erhverv | spam | til_gennemsyn
  hot           boolean default false,          -- naevner pris/haster/klar til koeb
  kraever_svar  boolean default false,          -- venter paa konkret svar fra virksomheden
  lead_id       uuid,                           -- link hvis der blev oprettet et lead
  status        text default 'ny',              -- ny | laest | arkiveret
  dedup_id      text,                           -- idempotens: gensendt webhook laver ikke dubletter
  modtaget      timestamptz default now()
);

create index if not exists indbakke_klient_modtaget_idx
  on indbakke_mails (klient_id, modtaget desc);

create unique index if not exists indbakke_dedup_uidx
  on indbakke_mails (klient_id, dedup_id)
  where dedup_id is not null;

-- 3) Skala-hygiejne: dedup-opslag paa leads skal vaere hurtigt ved volumen.
create index if not exists leads_klient_dedup_idx
  on leads (klient_id, dedup_id)
  where dedup_id is not null;

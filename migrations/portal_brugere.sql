-- Bruger-lag PR. VIRKSOMHED (multi-user pr. klient).
-- I dag er der praecis EN login pr. virksomhed (email+password paa selve
-- klienter-raekken) = virksomhedens EJER. Denne migration tilfoejer under-
-- brugere, saa chefen selv kan oprette medarbejdere med begraenset adgang.
--
-- Roller:
--   ejer        = den oprindelige klienter-login. Fuld adgang, kan ikke slettes.
--                 (findes IKKE i denne tabel — den ER klienter-raekken).
--   admin       = fuld adgang + kan selv oprette/redigere brugere.
--   medarbejder = ser kun de sektioner der staar i 'adgang'.

create table if not exists portal_brugere (
  id           uuid primary key default gen_random_uuid(),
  klient_id    text not null,                    -- hvilken virksomhed (tenant)
  navn         text,
  email        text not null,                    -- login-email, global unik
  password     text,                             -- bcrypt-hash
  rolle        text default 'medarbejder',       -- admin | medarbejder
  adgang       jsonb default '[]'::jsonb,         -- liste af sektions-noegler (kun for medarbejder)
  aktiv        boolean default true,
  oprettet     timestamptz default now(),
  oprettet_af  text                              -- email paa den der oprettede brugeren
);

-- Email skal vaere unik paa tvaers af alle under-brugere (login slaar op paa email).
create unique index if not exists portal_brugere_email_uidx
  on portal_brugere (lower(email));

create index if not exists portal_brugere_klient_idx
  on portal_brugere (klient_id);

-- Sessions skal kunne baere hvilken under-bruger et token tilhoerer + dens
-- rolle/adgang, saa haandhaevelsen virker ogsaa efter en server-genstart
-- (hvor RAM-cachen er tom og token genindlaeses fra Supabase).
alter table admin_sessions
  add column if not exists bruger_id    text,
  add column if not exists bruger_rolle text,
  add column if not exists adgang       jsonb;

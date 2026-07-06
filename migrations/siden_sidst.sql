-- "Siden sidst"-resume: server-side baseline pr. portal-bruger.
-- Formaal: naar ejeren/medarbejderen logger ind (typisk paa mobil om morgenen)
-- kan vi vise praecis hvad systemet ordnede AUTONOMT mens de var vaek.
--
-- Hvorfor server-side og ikke localStorage: localStorage er bundet til EN
-- browser/enhed. Chefen der tjekker paa mobil om morgenen men bruger desktop
-- til daglig ville aldrig faa et resume. Baseline skal foelge PERSONEN, ikke
-- enheden. Derfor gemmes "sidst set" pr. identitet paa serveren.
--
-- identitet:
--   under-bruger  = portal_brugere.id (uuid som tekst)
--   ejer          = 'ejer:' + klient_id  (ejeren ER klienter-raekken, ingen bruger_id)

create table if not exists portal_sidst_set (
  identitet  text primary key,        -- bruger_id, eller 'ejer:'+klient_id
  klient_id  text not null,           -- hvilken virksomhed (til fejlsoegning)
  sidst_set  timestamptz              -- sidste gang denne person aabnede overblikket
);

create index if not exists portal_sidst_set_klient_idx
  on portal_sidst_set (klient_id);

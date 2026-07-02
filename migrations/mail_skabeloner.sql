-- Per-klient mail-skabeloner / brand-stemme.
-- Lader kunden skraeddersy deres egne mails, opdelt paa type (leads vs tilbud).
-- Alle felter er nullable/valgfri: tom = systemets smarte standard bruges,
-- saa mails virker ud af boksen selv hvis kunden aldrig roerer dem (selvkoerende).
alter table chatbot_config
  add column if not exists mail_stemme       text,  -- faelles brand-stemme/tone, bruges alle steder
  add column if not exists mail_signatur     text,  -- underskrift/afsluttende linjer
  add column if not exists lead_mail_fokus   text,  -- ekstra fokus/instruks specifikt for lead-opfoelgning
  add column if not exists tilbud_mail_tekst text,  -- foelgetekst i tilbuds-mailen ({kunde_navn}, {firma})
  add column if not exists tilbud_mail_emne  text;  -- standard-emnelinje for tilbuds-mails

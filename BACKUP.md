# Database-backup og gendannelse

Nordolsens database (Supabase, Free-plan) sikres med et **krypteret dagligt
off-site dump** via GitHub Actions: `.github/workflows/db-backup.yml`.
En backup er kun noget vaerd hvis gendannelsen er **testet** — foelg begge
tjeklister nedenfor mindst een gang, og efter stoerre aendringer.

## Engangs-opsaetning

1. **Hent connection string** i Supabase:
   Dashboard -> Project Settings -> Database -> Connection string -> **Session
   pooler** (port 5432). Brug IKKE Transaction pooler (6543) — pg_dump virker
   ikke med den.
2. **Opret to repository-secrets** (GitHub -> Settings -> Secrets and variables
   -> Actions -> New repository secret):
   - `SUPABASE_DB_URL` = connection string fra trin 1 (indeholder password).
   - `BACKUP_PASSPHRASE` = et selvvalgt, staerkt kodeord. **Gem det i en
     password manager** — uden det kan backuppen aldrig dekrypteres.
3. **Koer foerste backup manuelt:** Actions -> "Daglig database-backup" ->
   Run workflow. Tjek at den bliver groen og at der ligger en artifact.

## Test af gendannelse (goer dette mindst een gang!)

En backup du aldrig har gendannet, er ikke en backup. Test saadan:

1. Hent en artifact fra en gennemfoert workflow-koersel (under "Artifacts").
2. Dekryptér den lokalt:
   ```bash
   gpg -d --batch --passphrase 'DIT_BACKUP_PASSPHRASE' \
     -o gendan.dump nordolsen-db-YYYY-MM-DD_HHMM.dump.gpg
   ```
3. Gendan til et **test-projekt** (ikke produktion!) — opret et gratis Supabase
   test-projekt og brug dets Session pooler-URL:
   ```bash
   pg_restore --no-owner --no-privileges --clean --if-exists \
     -d 'postgresql://...TEST-projekt...' gendan.dump
   ```
4. Verificér i test-projektet at tabellerne (leads, tilbud, bookinger, CRM)
   er der med data.

## Ved reel katastrofe (produktion)

1. Hent seneste artifact og dekryptér (som ovenfor).
2. Gendan enten til det eksisterende projekt eller et nyt Supabase-projekt med
   `pg_restore`. Peg derefter appens `SUPABASE_URL`/nøgler mod det gendannede
   projekt hvis det er nyt.
3. **Bemaerk:** dumpet indeholder KUN databasen — ikke filer i Supabase Storage.
   Uploads/billeder skal sikres separat.

## Naar forretningen baerer det

Opgradér Supabase til **Pro** (~25 USD/md): daglige backups med 7 dages
retention og **ét-kliks gendannelse** i dashboardet — den aegte sikkerhedssele.
Denne workflow kan koere videre som ekstra off-site lag.

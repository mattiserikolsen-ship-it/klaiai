-- Booking portal status og tilbud kobling
-- Kør i Supabase SQL Editor

ALTER TABLE bookinger ADD COLUMN IF NOT EXISTS portal_status TEXT DEFAULT 'bekræftet';
ALTER TABLE bookinger ADD COLUMN IF NOT EXISTS tilbud_id UUID REFERENCES tilbud(id) ON DELETE SET NULL;
ALTER TABLE bookinger ADD COLUMN IF NOT EXISTS noter TEXT;

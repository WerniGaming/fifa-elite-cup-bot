-- Merkt sich, ob die Anmeldung fuer ein Turnier bereits automatisch geschlossen wurde
-- (2h vor Start), damit der automatische Task ein manuelles Wieder-Oeffnen durch einen
-- Admin nicht sofort wieder rueckgaengig macht.
ALTER TABLE tournaments ADD COLUMN IF NOT EXISTS signup_auto_closed boolean NOT NULL DEFAULT false;

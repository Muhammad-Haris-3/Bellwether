-- 026 — let the pipeline check the automation freeze (M6-FR-25, SRS FR-37).
--
-- The promotion job runs as bellwether_writer, which has no read access to
-- app.automation_freeze — the table's RLS policy requires an authenticated app
-- user, and the writer never sets one.
--
-- A SECURITY DEFINER function bridges the two: it runs as its definer (the
-- database owner, who bypasses RLS) and exposes a single boolean. The writer
-- learns whether automation is frozen and nothing else — it cannot enumerate
-- freeze rows, read who set the freeze, or alter the table.

CREATE OR REPLACE FUNCTION app.is_automation_frozen()
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, app
AS $$
    SELECT COALESCE(
        (SELECT frozen FROM app.automation_freeze ORDER BY freeze_id DESC LIMIT 1),
        false
    )
$$;

REVOKE ALL    ON FUNCTION app.is_automation_frozen() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.is_automation_frozen() TO bellwether_writer, bellwether_app;

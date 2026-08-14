-- 028 — record what human labels did to a model (M7-FR-7, M7-FR-8, SRS FR-48).
--
-- FR-48 requires the weight to be recorded in the registry entry, and the
-- reason is the same one behind every other frozen value in this schema: a
-- weight that lives only in code is a weight that can be changed after seeing
-- which value scored best, and no reader could tell afterwards.
--
-- Recorded per MODEL rather than globally, so a model trained under one weight
-- stays interpretable after the constant changes.
ALTER TABLE register.model_registry
    ADD COLUMN IF NOT EXISTS human_labels jsonb;

COMMENT ON COLUMN register.model_registry.human_labels IS
    'How human judgement entered this model: the weight, how many labels were '
    'used, and how they split across the ranked and random queue slices. Null '
    'for models trained before M7 — which is different from a model trained '
    'with zero human labels, and the two must stay distinguishable.';

-- The training job runs as bellwether_writer, which cannot read app.human_labels
-- — the table's RLS gates on an authenticated app user the writer never sets.
--
-- The same narrow door as sql/023 and sql/027: exactly the columns training
-- needs, keyed on a window, with no way to ask who judged what. The reviewer's
-- identity never reaches the model or its registry entry.
CREATE OR REPLACE FUNCTION app.labels_for_training(p_from timestamptz, p_to timestamptz)
RETURNS TABLE (revid bigint, verdict text, queue_slice text)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, app, landing
AS $$
    SELECT h.revid, h.verdict, h.queue_slice
      FROM app.human_labels h
      JOIN landing.rc_events e ON e.revid = h.revid
     WHERE e.event_ts >= p_from
       AND e.event_ts <  p_to
       -- `unsure` contributes nothing to a target. It is not a third class and
       -- it is not a soft label; it is a reviewer declining to answer, and
       -- turning that into a training signal would invent an opinion.
       AND h.verdict IN ('bad_edit', 'good_edit')
$$;

REVOKE ALL    ON FUNCTION app.labels_for_training(timestamptz, timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.labels_for_training(timestamptz, timestamptz) TO bellwether_writer;

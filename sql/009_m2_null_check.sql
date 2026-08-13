-- Bellwether — 009 M2: the null check, stored beside the verdict
--
-- Idempotent.
--
-- A model fitted to SHUFFLED labels should score at the base rate. If it beats
-- it, the evaluation harness is carrying information about the outcome — row
-- order, fold boundaries, something — and the headline margin is not measuring
-- the features at all.
--
-- The knowability guard structurally cannot catch this: it proves no feature
-- depends on the future of the event it describes, which says nothing about
-- the harness that consumes them. So the check is recorded next to the result
-- it qualifies, and a reader can judge the verdict against it rather than
-- taking the verdict alone.
ALTER TABLE outcome.evaluations
    ADD COLUMN IF NOT EXISTS null_pr_auc double precision;

ALTER TABLE outcome.evaluations
    ADD COLUMN IF NOT EXISTS base_rate double precision;

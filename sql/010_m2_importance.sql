-- Bellwether — 010 M2: what the model leaned on, and whether it needed it
--
-- Idempotent.
--
-- Permutation importance was printed to a job log and nowhere else, which made
-- it unreadable without a login and unavailable to any later comparison. "The
-- model works" is not a finding; what it leaned on is, and a feature
-- dominating for reasons nobody can explain is how a leak survives a green
-- evaluation.
--
-- The ablation column exists because of what the first importances showed.
-- log_user_id dominated, and account ids rise monotonically by construction —
-- so a model leaning on their absolute magnitude sees systematically different
-- values every month. That is covariate drift built into the top feature.
-- KC-2 asks whether signal exists, not whether one feature carries it, so the
-- margin is now also computed with that feature removed.
ALTER TABLE outcome.evaluations
    ADD COLUMN IF NOT EXISTS feature_importance jsonb;

ALTER TABLE outcome.evaluations
    ADD COLUMN IF NOT EXISTS ablated_feature text;

ALTER TABLE outcome.evaluations
    ADD COLUMN IF NOT EXISTS ablated_margin double precision;

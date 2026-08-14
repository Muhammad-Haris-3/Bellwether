"""When a prediction may be counted (M4 §2.2).

Standard library only, and deliberately so. Both the pipeline and the serving
API need this number, and the API's image contains neither numpy nor
scikit-learn — importing `bellwether.metrics` for it took the whole modelling
stack into the serving container's import graph and the deploy died on
`ModuleNotFoundError: No module named 'numpy'`.

One definition, in a module light enough for both sides to import. Two copies
is how the register and the metrics would end up disagreeing about which
predictions count, which is worse than either being wrong.
"""

from __future__ import annotations

# Seven days, not M2's 48 hours, and the difference is not a preference.
#
# Two different quantities get called "maturity". M2's 48h describes when
# reverts stop arriving — a property of the world, estimated from the survival
# curve. This one has to be a window this pipeline has actually LOOKED at, and
# for the 90% of events outside the maturity cohort there is exactly one check,
# at the final checkpoint of seven days (M1 §5).
#
# Grading needs both, and the binding constraint is observation rather than the
# world. Using 48h here produced a sample that was 100% positive: a positive
# qualifies as soon as it is found, a non-cohort negative cannot be confirmed
# until its seven-day check, and between those two points the only gradeable
# events are the reverts. At seven days both arms become available at the same
# moment, which is what makes the sample unbiased rather than merely larger.
PROVISIONAL_MATURITY_SECONDS = 7 * 24 * 3600

# The maturity cohort is the 10% that receives the FULL checkpoint grid (M1 §5),
# so a 48h check exists for every one of them and both arms of the inclusion
# rule become available at 48 hours rather than seven days.
#
# It is a deterministic 10% bucket keyed on revid, so within the events it
# covers it is a probability sample: smaller, not skewed. It does NOT cover the
# whole table — the flag is written at insert time and rows inserted before that
# code shipped can never be corrected, so the cohort begins partway through and
# is a probability sample of events from that point on.
COHORT_MATURITY_SECONDS = 48 * 3600

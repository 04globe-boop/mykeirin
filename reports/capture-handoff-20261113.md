# Capture handoff protection — 2026-11-13

The owner approved implementing the handoff repair within the existing GitHub environment. Frozen bundles/source/weights, model identifiers, T-5 cutoff, primary eligibility, trial periods and denominator are unchanged. No historical forecast is created or replaced.

Before this repair, a capture ending at20:49:31 excluded a known20:49:40 prediction trigger from its window. The next capture could start after20:50 cutoff. The regression simulates the observed102-second handoff with two independent predictor contracts; it is a scheduler simulation, not a real past forecast or proof of model accuracy.

Capture now advances its planned end through known forecast windows within120 seconds of that end, at most180 seconds beyond the original end. The end is monotonic while inference is active; absent/failed/empty inference does not request extension. Capture and raw-data timestamp rules are preserved. Preparation restores the extra3-minute date range so a midnight extension has a verified canonical baseline. New end/cap metadata stays in private capture summaries.

The publication job no longer installs requests/parser/NumPy packages. Its actual entrypoints, pack and score-free audit execute with Python site packages disabled and PYTHONPATH removed. Windows tests supply only timezone data files equivalent to Ubuntu's installed OS timezone database. Capture still installs its original dependencies. Writer locks, safe merge, private handoff and the final failure gate remain.

Validation: baseline220 research/operations tests passed. The new9-test scheduler suite initially failed5 assertions against the old behavior, then passed after the repair. Independent review found an internally removed last worker could cause one extra sleep; a regression reproduced29 seconds of delay and the post-tick availability recheck removes it. The scheduling suite now has10 tests. After that fix the full suite passed232 tests and all7 existing logger checks passed. The2 isolated-publisher tests are included in the full suite. CI and operational smoke results will be recorded after deployment in the private research health report.

Limits: GitHub scheduling delays remain outside our control. Dense schedules can reach the180-second guard cap, recorded as handoff_guard_capped. Existing collection/network work may take time beyond its planned end; the workflow's35-minute limit remains. This repair reduces known near-boundary misses and removes avoidable setup, but does not guarantee zero gaps. OP403, held rider-name disagreement and calibration deployment are separate outstanding items.

Review scope: repeated invocation in the new simulator shares its test predictor state; durable/cohort-specific restart restoration is covered by the existing ResearchGroup tests, not newly certified by that simulator. Live scheduling/network/frozen-model output and deployment require separate operational evidence.

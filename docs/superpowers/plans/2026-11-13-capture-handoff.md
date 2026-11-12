# Capture Handoff Protection Implementation Plan

> Execute inline with superpowers:executing-plans. The owner has authorized implementation and deployment in the existing GitHub environment.

**Goal:** protect known near-end forecast windows and reduce avoidable publisher setup.
**Architecture:** bounded monotonic capture end; preserve serialized state ownership and all primary issuance guards.
**Tech Stack:** existing Python standard library, unittest, GitHub Actions.
**Spec:** ../specs/2026-11-13-capture-handoff-design.md

## Global Constraints
Frozen bundles, weights, candidates, cutoffs, protocol, trial windows and denominator are unchanged. No primary backfill. Private evidence only.

## Review Focus
- Dense schedules must not extend a job indefinitely.
- Midnight cutoffs must use the race date and preserve existing day selection.
- Predictor failure must not extend collection for an absent predictor.
- Restart may see already issued forecasts; skip them rather than issue twice.
- Publisher imports must work without third-party packages; the capture still installs them.

## Task 1: bounded prediction-aware capture finish
Files: logger/run_logger.py; tests/test_research_handoff_window.py.
- [x] Add tests exercising the actual Logger with an end-aware two-cohort test predictor: nominal end20:49:31, trigger20:49:40, cutoff20:50:00; assert one pre-cutoff issue per cohort and no duplicate on restart.
- [x] Run new tests against unchanged production code and verify the missing-forecast assertion fails.
- [x] Add a bounded schedule-aware end computation: 120-second lookahead, maximum180-second extension, monotonic end; apply only with an available forecaster before tick/events.
- [x] Test no predictor, no nearby window, dense schedules/hard cap, and midnight.
- [x] Run the full research suite and existing logger checks. Expected all pass.

## Task 2: dependency-free publisher and deployment
Files: .github/workflows/odds_logger.yml; tests/test_research_publisher_stdlib.py; reports/capture-handoff-20261113.md.
- [x] Execute real publisher imports and an audit with python -S; verify clean startup without packages. Remove only publisher pip install, leave capture installation and all failure gates.
- [x] Run full suite, review the final diff independently, fix important findings with RED/GREEN tests.
- [ ] Publish PR, check CI, merge within owner authorization, run an operational smoke preserving private evidence, and record actual outcome and remaining limits.

Pre-flight: Task1 changes Logger timing only. Task2 does not change its interfaces or state ownership. Ruling: reuse the existing isolated maintenance copy and GitHub Git-data branches; do not modify the older dirty user checkout or create another worktree.

Ruling: put shared 120/180-second scheduling bounds in logger/capture_window.py and extend preparation restoration by the same 3 minutes, so guarded capture across midnight has a canonical baseline. Ruling: restart may issue new future races; the test forbids duplicate keys and changed old receipts, rather than forbidding all future issuance.

Task1: RED9 showed5 assertion failures, GREEN9 passed. Final review: fixed Important last-worker removal — test_last_worker_failure_inside_tick_ends_without_an_extra_sleep RED29-second delay to GREEN; full suite rerun follows. Final minor addressed by narrowing simulation report: same-instance repeat checks are not durable process-restart proof; existing production receipt tests retain that coverage. Final review did not assess live GitHub/network/model output; deployment smoke and CI supply the available operational proof, while next live prediction window remains unverified at night.

Task1 and code-level Task2 verification complete:232 research/operations tests and7 original logger checks passed after the final review fix. Commit/review range is the GitHub branch based on48d42c0b35fb789e1f60992a213b1c70195e20ea.

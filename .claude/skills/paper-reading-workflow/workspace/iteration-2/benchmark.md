# Skill Benchmark: paper-reading-workflow

**Model**: claude-sonnet-4-6
**Date**: 2026-05-26T08:52:30Z
**Evals**: 1, 2, 3 (1 run each per configuration)

## Summary

| Metric | With Skill | Without Skill | Delta |
|--------|------------|---------------|-------|
| Pass Rate | 100% ± 0% | 15% ± 13% | +0.85 |
| Time | 51.5s ± 6.1s | 51.7s ± 39.8s | -0.1s |
| Tokens | 21621 ± 2840 | 29054 ± 24387 | -7433 |

## Per-Eval Breakdown

### Eval 1: paper-add
| Config | Pass Rate | Time | Tokens |
|--------|-----------|------|--------|
| with_skill | 4/4 (100%) | 44.8s | 18,431 |
| without_skill | 1/4 (25%) | 23.0s | 14,078 |

### Eval 2: latex-debug
| Config | Pass Rate | Time | Tokens |
|--------|-----------|------|--------|
| with_skill | 5/5 (100%) | 55.8s | 23,637 |
| without_skill | 1/5 (20%) | 97.8s | 57,104 |

### Eval 3: batch-import
| Config | Pass Rate | Time | Tokens |
|--------|-----------|------|--------|
| with_skill | 4/4 (100%) | 54.0s | 22,795 |
| without_skill | 0/4 (0%) | 34.2s | 15,980 |

## Analyst Observations

- **Pass rate gap is decisive**: with_skill achieves 100% across all 3 evals; without_skill barely passes any agent-oriented assertions. The skill reliably guides the agent toward composable function calls.
- **Token efficiency paradox**: without_skill eval-2 (latex-debug) used 57K tokens (2.4x more) while scoring only 20%. The response was thorough but missed skill-specific artifacts (debug-table.sh, latex-failure-patterns.md, regression checklist).
- **Time variance**: without_skill has extreme variance (23s–98s) driven by eval-2's verbose LaTeX debugging output. with_skill is tightly clustered (45s–56s).
- **without_skill eval-3 scored 0%**: The baseline gives only the CLI command with no programmatic batch processing pattern — the agent-as-conductor paradigm is entirely absent without the skill.

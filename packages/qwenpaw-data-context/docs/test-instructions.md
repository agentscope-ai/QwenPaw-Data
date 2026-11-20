# Testing Methodology

Read this guide alongside the [Development Guidelines](../AGENTS.md) and [Architecture](architecture.md).

## What is a Test

**A permanent test creates an ongoing obligation to maintain its scenario, expected outcomes, fixtures, and dependencies.** Keep it only when repeated verification of that behavior justifies this cost. One-off diagnostics and exploratory checks need not become part of the test suite.

**A test is not the same as verification.** Verification establishes whether a particular change does what it should; a permanent test is a repository asset maintained to verify business behavior repeatedly as the system evolves.

**Completing a change requires verification, but does not automatically require new permanent tests.** Acceptance of the current change and admission of a test into the repository are separate decisions. Running existing tests, inspecting results, performing a manual trial, or using a temporary diagnostic can provide verification without adding permanent test code.

## Why to Write a Test

Verify real business behavior, plausible failures, and meaningful logical boundaries that existing tests do not cover. Do not invent unreachable scenarios or add tests merely because code, contracts, or bug fixes exist.

## How to Write a Test

1. **Find the gap.** Run relevant existing tests first. Add test code only when an uncovered business behavior warrants ongoing verification; prefer extending an existing scenario.
2. **Choose one boundary.** Organize by business behavior, not source structure. Verify each rule at its owning layer; add cross-layer coverage only for a distinct integration risk.
3. **Build the smallest scenario.** Use realistic inputs, minimal fixtures, and independently specified business outcomes.
   - Assert business outcomes, not internal structure, intermediate state, or counts, contracts, and formats without business meaning.
   - Avoid equivalent input matrices.
   - Test code must normally stay within 50% of production code, including fixtures and helpers but excluding blank lines, comments, and docstrings.
4. **Verify and report.** Use real backends or models when the claim depends on them. Report results, substitutes, and unverified behavior; do not turn every diagnostic into a permanent test or repeat passing checks without a new reason.

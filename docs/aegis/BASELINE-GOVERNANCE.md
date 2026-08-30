# Baseline Governance

## 1. Baseline Roles

- Product / Requirement Baseline: confirmed requirements, scenarios, acceptance criteria, constraints, and approved change intent.
- Architecture / Runtime Boundary Baseline: canonical owners, persistent contracts, dependency direction, compatibility, and retirement state.

## 2. Design Defect

A confirmed error or gap in the requirement or design must be corrected before implementation is aligned to it. Implementation must not conceal a defective baseline.

## 3. Implementation Drift

When implementation differs from a confirmed, unchanged baseline, return the implementation to that baseline rather than silently changing the requirement.

## 4. Compatibility Aliases

- Architecture Defect means an architecture-scoped Design Defect.
- Architecture Drift means architecture-scoped Implementation Drift.

## 5. Baseline Check Protocol

Before a non-trivial change:

1. Read the latest product/requirement baseline candidate.
2. Read the latest architecture/runtime-boundary candidate.
3. Compare the proposed work with acceptance and ownership boundaries.
4. Report aligned, Design Defect, Implementation Drift, missing-authority, or needs-clarification.

## 6. Architecture Review Dimensions

Review ownership integrity, module boundaries, contract changes, dependency direction, compatibility paths, retirement completeness, and net complexity.

## 7. Hard Boundaries

- This file governs only this repository's Aegis workspace.
- Baseline snapshots are evidence, not a replacement for README, code, tests, or user authority.
- ADRs record accepted decisions; they do not invent requirements.
- Changes to this file require explicit review.

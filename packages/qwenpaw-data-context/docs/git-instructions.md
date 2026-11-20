# Git Commit Guidelines

## Commit Scope

Each commit should serve one complete purpose and be independently reviewable, verifiable, and revertible. Split changes by behavior and responsibility boundaries, not mechanically by file or line counts.

Commit independently deliverable changes separately. Keep the implementation, caller adaptations, tests, and documentation needed for the same behavior together. Remove directly superseded code with its replacement; handle unrelated refactoring separately. Larger changes may be split into working stages without leaving intermediate states that break existing functionality.

## Subject

```text
type(scope): English summary
```

Write the subject in English. Briefly describe the result, start with a clear action verb, and omit the trailing period.

Use an established name for the primarily responsible module as the `scope`. For changes spanning modules, choose their shared responsibility rather than listing every directory or using a broad scope to conceal several independent purposes.

| Type | Purpose |
| --- | --- |
| `feat` | Add or extend a capability. |
| `fix` | Correct faulty behavior or restore an existing contract. |
| `refactor` | Change implementation structure or responsibilities without aiming to change external behavior. |
| `perf` | Improve performance or resource consumption while preserving business semantics. |
| `docs` | Change documentation only. |
| `test` | Change tests and their supporting code only. |
| `build` / `ci` | Change build and dependency mechanisms, or CI pipelines, respectively. |
| `chore` | Perform routine maintenance that does not fit the types above. |
| `merge` | Create an actual merge commit integrating branch histories. |
| `revert` | Revert an existing commit. |

Choose the type by the primary purpose. Tests and documentation accompanying a feature need not be split out. Classify configuration changes by their actual behavioral effect rather than always using `chore`.

## Body

Leave a blank line after the subject and write the body in Chinese Markdown. When needed, explain the reason in one sentence, then use a few bullets to describe the final changes, key boundaries, and significant compatibility or migration implications.

Describe behavior and responsibilities rather than repeating the diff file by file or recounting conversations and attempts. Verification claims must have supporting evidence and state their scope and limitations. Routine commands and results may be reported during review or handoff.

Merge descriptions should identify the source, scope, important conflict resolutions, and remaining adaptations. Revert descriptions should identify the reverted commit and the reason.

## PR Description

Write the description in Chinese Markdown, organized around **Motivation / Background** and **Detail**: establish the context and motivation, then describe the final behavior and key changes. Include relevant verification results; expand on design tradeoffs, compatibility, or migration only when material.

Keep the title and description aligned with the final PR scope. Do not repeat commit lists or enumerate files. Scale detail to the change: one or two paragraphs may suffice for a small PR, without mandatory section headings.

## Review and Execution

- Before every commit or amend, organize and stage the relevant changes. Preserve changes belonging to other tasks and ensure the staged commit contents match the intended scope.
- Inspect the final staged diff and relevant verification. Ensure the commit does not depend on omitted unstaged changes. Existing verification that remains valid need not be repeated.
- After staging, present the complete commit description (subject and body), staged scope, verification results, and limitations for user review. Commit only after explicit user approval. Material changes to the commit contents or description require renewed review and approval.
- After committing, verify the actual contents and report the hash, commit status, and push status. Commit, push, merge, and history rewriting each follow their own authorization; approval for one does not imply approval for another.

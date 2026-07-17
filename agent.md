# Agent Instructions

## Superpowers Skill Usage

For this project, do not use `superpowers` skills for every user question by default.

Only use a `superpowers` skill when the user explicitly asks for it, for example:

- "使用 superpowers"
- "调用 superpowers"
- "用某个 superpowers 技能"
- "use superpowers"

For ordinary project questions, documentation edits, folder creation, file inspection, planning, and implementation requests, proceed directly using normal project context unless the user explicitly requests a `superpowers` workflow.

## Project Collaboration Preferences

- Keep responses concise and practical.
- Do not write code when the user only asks for planning, architecture, or documentation.
- When asked to create folders, create folders only unless files are explicitly requested.
- Preserve the project's copyright and privacy boundary: do not add protected character assets, voice data, images, or model files to the repository.

## W Task Pull Request Delivery

- Treat publishing as part of the definition of done for every `Wxx` task.
- After a `Wxx` task passes its required checks, stage only that task's intended changes, create a focused commit, push the branch, and create or update its Draft PR before reporting the task as delivered.
- Use the `Wxx` identifier in the PR title and make the current Windows development plan the authority when older plans or PR descriptions conflict with it.
- Include the PR URL, validation evidence, remaining manual gates, and any publication blocker in the completion report.
- Never include unrelated user changes in a `Wxx` PR without explicit authorization.

## Guarded Merge Without Platform Enforcement

- When an owner explicitly accepts a missing branch-protection or ruleset Gate, do not treat that acceptance alone as sufficient evidence that the intended PR is safe to merge.
- First record the accepted residual risks, push the closing record, wait for every required check on that exact final head, and re-read the PR base, head, diff scope, reviews, conversations, mergeability, and draft state.
- Merge only with an expected-head guard such as `--match-head-commit`; abort if the head changed, a check is not successful, the base changed unexpectedly, or the diff contains unreviewed scope.
- After the merge, verify the remote PR state and merge commit instead of inferring success from the merge command's exit code.

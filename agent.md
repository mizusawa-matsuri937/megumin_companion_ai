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

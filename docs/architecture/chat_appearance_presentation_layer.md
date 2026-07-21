# Chat appearance presentation-layer contract

> Status: implemented on the `codex/chat-appearance-interface` branch; this is
> a standalone owner-authorized UI extension, not the start of W17 or another
> unapproved W task.

## Confirmed scope

The desktop chat window exposes a presentation-only extension point for future
visual work. It deliberately changes neither the Qt/BackendThread topology nor
the typed command/event contracts:

- `ChatAppearance` creates a transcript surface and receives only
  `ChatUiParts`, which contains Widgets and layouts.
- `ChatTranscriptSurface` receives an immutable, in-memory sequence of
  `ChatMessageView` values and must clear any rendered message text through
  `clear_sensitive()`.
- `MainWindow` keeps the current plain-text surface as the default. Existing
  send, cancel, focus, accessibility, bridge, and backend behavior are outside
  the appearance contract.
- Object names such as `chatRoot`, `chatTranscript`, and `chatSendButton`
  provide stable QSS targets without coupling a theme to business logic.

The interface intentionally does not expose the bridge, backend host,
configuration, repository, persistence service, or provider clients. Themes
must not persist, log, export, or upload the message snapshots they render.

## Extension path

A future theme implements `ChatAppearance` and can:

1. apply QSS, fonts, spacing, and button/status styling through `ChatUiParts`;
2. return a custom `ChatTranscriptSurface` for a bubble or card layout; and
3. replace all rendered text when given an empty snapshot after a reset, and
   clear any retained text through `clear_sensitive()` when the window closes.

The default `PlainTextTranscriptSurface` preserves the existing
`QPlainTextEdit` behavior, including scroll-to-end after changed content.

## Boundaries and non-goals

- This adds no theme configuration, filesystem loading, asset packaging, or
  runtime style download.
- It does not add protected character images, voices, models, screenshots, or
  user conversation fixtures.
- It is not evidence of a finished visual design or of real Windows visual UX
  validation; future subjective appearance work still needs its own review.
- The existing architecture, data-flow inventory, ADRs, threat model, and W14
  backend semantics remain unchanged because no process, protocol, storage, or
  network boundary changes.

## Automated evidence

`uv run pytest --no-cov tests\\unit\\ui\\test_appearance.py tests\\unit\\ui\\test_window.py -q`
passes with the custom-surface test and the existing chat-window regressions.
The test verifies that a custom appearance receives Widget-only handles, sees
only an immutable message snapshot, and participates in sensitive-state wipe.

## Rollback

Revert the appearance module and the `MainWindow` wiring. No configuration,
database, protocol, or backend migration is involved.

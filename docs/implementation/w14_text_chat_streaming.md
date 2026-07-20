# W14: Text chat, streaming, cancellation, and recovery

## Scope

W14 replaces the W13 desktop shell's rejecting default runtime with an in-process
`TurnService` adapter. It does not start Uvicorn, bind a TCP port, create a
WebSocket client, or reuse the development API's token, Origin, or client
identity.

The adapter owns one fixed desktop `client_id` and session. It forwards only
the existing approved `user.message` and `turn.cancel` contracts to
`TurnService`; all cancellation and new-message preemption therefore use the
same W06 linearization path as other text input.

## Cursor and recovery contract

- A `DesktopSessionCursor` retains only the latest sequence number across
  backend generations. It never retains message, subtitle, audio, or provider
  bodies.
- The runtime subscribes with that cursor and forwards ordered replay events to
  the bounded `ApplicationBridge`.
- Bridge overflow, oversized deltas, slow subscriptions, and a UI-detected
  sequence gap request an authoritative snapshot. The next subscription uses
  the existing W06 reset-and-chunk protocol.
- `SessionReset` and `SessionSnapshotChunk` are now typed bridge events.
  Snapshots contain only bounded `TurnState` metadata. The presenter clears
  visible user/assistant text before accepting a reset and never appends a
  post-gap delta to pre-gap subtitles.
- The presenter ignores duplicate or older sequence numbers, accepts coalesced
  delta ranges only when they start at the expected cursor, and reports the
  stable `event_snapshot_required` code otherwise.

## User-visible semantics

- The send control stays usable during a turn: a new non-empty message enters
  `TurnService.accept()` and preempts the prior turn according to W06.
- Clicking send again with an empty editor retries the in-memory pending
  message using its original `message_id`; duplicate submission therefore
  produces a `turn.snapshot`, not a second provider/TTS/VTS side effect.
- The stop control sends the existing `TurnCancelCommand`.
- Accepted, delta, segment, degradation, failure, cancellation, completion,
  reset, and snapshot events update the in-memory presenter. Raw exception,
  provider body, path, or secret is not copied into the UI status surface;
  only validated stable codes are displayed.
- Drafts, pending retry payloads, transcripts, and bridge queues remain
  in-memory and are wiped when the window closes. W14 adds no new persistence
  or sensitive-storage schema.

## Composition and degradation

`DesktopChatRuntime` enters the ordinary application lifespan inside the
existing backend thread, then obtains its `TurnService`; it does not serve the
FastAPI application. The default offline Mock LLM, Mock TTS, silent playback,
disabled VTS, disabled vision, and disabled proactive behavior remain in
effect unless the user has explicitly configured otherwise.

If composition or idempotency is unavailable, the backend reports a stable
body-free unavailable/degraded state instead of silently falling back to a
mock or exposing the exception. Provider, TTS, and VTS failures already
emitted by the pipeline continue to surface as their existing stable codes.

## Automated evidence

The W14 UI suite covers:

- same-`message_id` duplicate submission producing one pipeline run and one
  accepted turn;
- new-message preemption ordering the old cancellation before the new accept;
- sequence-gap handling that erases partial text before reset/snapshot
  reconstruction;
- configured application-lifespan restart using a body-free snapshot rather
  than replaying transcript content;
- legacy W13 lifecycle, bounded bridge, input, focus, accessibility, and
  close-wipe regression cases.

These checks are evidence for their mocked/headless conditions only. Remaining
human verification is limited to continuous real-user text conversation,
Chinese/Japanese IME composition while pressing Enter/Ctrl+Enter, rapid send
retry perception, transcript scrolling/selection, and subjective error-message
clarity on the target Windows environment.

## Rollback

Set the desktop host's runtime factory to `SkeletonBackendRuntime`, or remove
the W14 adapter and restore the W13 shell. This returns the desktop to an
honest non-chat state without weakening the backend's W06 idempotency,
cancellation, replay, or snapshot guarantees.

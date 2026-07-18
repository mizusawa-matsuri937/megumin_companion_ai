# W08 provider completion, deadlines, and TLS contract

This document freezes the W08 behavior before implementation. It is limited to
LLM/TTS completion semantics, TTS deadlines, and the network trust boundary for
LLM, TTS, and VTS. It does not add VTS reconnect behavior, worker isolation, or
any W12/W13 feature.

## Confirmed baseline defects

- `TTSJob.timeout_ms` has an implicit 8-second default while GPT-SoVITS also
  owns a separate 30-second `httpx.Timeout`; jobs built by the dialogue
  pipeline do not receive the configured value.
- an OpenAI-compatible stream currently treats every natural EOF as success,
  even when no completion marker was received;
- the HTTP and WebSocket endpoints accept cleartext non-loopback addresses,
  and the default clients inherit proxy configuration from the process;
- a provider exception raised inside the dialogue `TaskGroup` can be reduced to
  the generic `pipeline_failed` terminal code.

## TTS deadline ownership

`tts` settings are the only configuration source. The existing
`timeout_seconds` field is retained as the total synthesis deadline for schema
compatibility. W08 adds connect, first-audio-byte, and cancellation-settlement
deadlines beside it. No environment aliases are added for these values.

Every `TTSJob` must explicitly contain all four deadline values in milliseconds:

| Deadline | Default | Owner and terminal code |
| --- | ---: | --- |
| connect | 8 s | transport connect/TLS handshake; `tts_connect_timeout` |
| first audio byte | 8 s | GPT-SoVITS response body; `tts_first_byte_timeout` |
| total | 30 s | provider synthesis call including response validation; `tts_total_timeout` |
| cancellation settlement | 1 s | provider-owned request/response close and task handoff; `tts_cancel_timeout` if a non-cancellation result must be reported |

The schema has no deadline defaults. The dialogue pipeline injects settings into
every job. GPT-SoVITS does not own a constructor timeout and its `httpx` client
has no implicit timeout. Mock TTS consumes the same job deadlines. A first-byte
deadline applies only until the first non-empty audio chunk; a later stalled
chunk is bounded by the total deadline. Cancellation closes the response and
owned operation before local settlement; remote compute or billing may continue.
Mock TTS applies the same absolute total deadline to delay, temp registration,
WAV generation, and validation. Its local writer runs outside the event loop but
is always drained before the call returns, so timeout or repeated cancellation
cannot leave a background writer racing registry/path cleanup.

GPT-SoVITS bounds all provider-owned synthesis workers by the existing W07
`limits.tts_queue_capacity`. A worker that misses cancellation settlement keeps
its slot and opens a circuit: new calls return `tts_cancel_timeout` without
starting another worker until every unresolved cancellation really finishes.
The completion callback releases the slot; `close()` retains ownership and waits
for response, `.part`, temp-registry, lease, and final-path cleanup.

The non-synthesis API-v2 probe preserves W09 classification semantics but must
receive an explicit positive deadline from its caller; it never inherits an
environment, client, or constructor timeout.

## LLM completion capability

`llm.stream_completion_mode` is explicit and has four supported values:

- `done_and_finish_reason` (default): both an accepted finish reason and
  `[DONE]` are required;
- `done`: `[DONE]` is sufficient;
- `finish_reason`: an accepted finish reason followed by natural EOF is legal;
  `[DONE]` is a capability mismatch even if a finish reason was already seen;
- `eof`: one or more validated events followed by natural EOF are legal;
  `[DONE]` is a capability mismatch.

For text chat, `stop` is the only successful finish reason. `length` is
`llm_truncated`; `content_filter` is `llm_request_rejected`; an unknown or
malformed finish reason is `llm_protocol_error`. Under a marker-requiring mode,
natural EOF before the required marker is `llm_truncated`; `[DONE]` that violates
the configured capability is a protocol error and never substitutes for the
configured completion condition. Malformed JSON, invalid choice
shapes, and provider error envelopes remain protocol/rejection failures. The
same finish-reason classification applies to non-stream completions.
Stream events and non-stream response bodies are read incrementally and bounded
by the existing W07 `limits.llm_output_bytes`; W08 does not introduce a second
provider body budget.

The provider never returns partial text as a successful completion. The
dialogue pipeline emits one content-free `assistant.output_incomplete` event on
a provider failure, preserves the stable provider error code through the task
group, and never retries the turn. If subtitles were emitted or playback
started, the event records booleans for those facts; it contains no text, URL,
path, or provider body. Failed/truncated turns do not enter the completed-turn
history or memory finalizer.

## Endpoint and trust boundary

All three remote adapters use one shared validator:

- `http`/`ws` is accepted only when the parsed host is exactly `localhost` or a
  numeric IPv4/IPv6 loopback address;
- non-loopback LLM/TTS endpoints require `https`; non-loopback VTS endpoints
  require `wss`;
- DNS names are never resolved to infer loopback, and unspecified, wildcard,
  malformed, or userinfo-bearing endpoints are rejected;
- redirects are not followed. HTTP-to-HTTPS, HTTPS-to-HTTP, and cross-origin
  redirects all fail closed with a stable protocol/configuration code;
- certificate verification and hostname verification are always enabled for
  TLS endpoints. There is no `verify=false`, retry-over-HTTP, or TLS downgrade
  option.

Each provider may explicitly configure a credential-free HTTP/HTTPS proxy URL
and an optional CA bundle path. Process proxy variables are ignored. A custom CA
is loaded in addition to platform trust roots, must parse as a CA bundle, and
does not disable hostname or expiry validation. Proxy and CA configuration
errors are content-free; URL credentials, CA contents, and filesystem paths are
never copied into errors, ordinary logs, health, history, or diagnostics.

Injected test transports may replace socket I/O but do not bypass endpoint
classification or the production client construction path.

## Stable W08 error surface

- LLM: `llm_timeout`, `llm_connection_error`, `llm_unavailable`,
  `llm_protocol_error`, `llm_truncated`, existing authentication/rate-limit/
  rejection codes.
- TTS: `tts_connect_timeout`, `tts_first_byte_timeout`, `tts_total_timeout`,
  `tts_cancel_timeout`, `tts_tls_error`, `tts_connection_error`, plus existing
  authentication/rate-limit/request/audio validation codes.
- VTS TLS/configuration failures use content-free `vts_config` or
  `vts_disconnected` at the existing bridge boundary; W08 does not alter W09
  reconnect/generation behavior.

## Compatibility, rollback, and scope guard

The settings schema version remains unchanged. Existing `tts.timeout_seconds`
keeps its value and now unambiguously means total deadline; new stage values use
safe defaults. Existing loopback mock/GPT-SoVITS/VTS defaults remain valid.
Remote cleartext configurations fail at startup and must migrate to TLS.

Rollback may disable the remote provider, return to mock/silent playback, or
lower TTS concurrency. It must not restore duplicate/implicit timeouts,
cleartext remote endpoints, disabled certificate verification, natural-EOF
success without capability, or transparent whole-turn retry.

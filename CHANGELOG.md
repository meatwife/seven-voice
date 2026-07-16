# Changelog

## V2 — streaming TTS (2026-07-16)

### What changed

Agent replies now start playing on the **first synthesized audio chunk**
instead of waiting for the whole mp3 to be written to disk first.

The bridge streams edge-tts output through an OS pipe directly into ffmpeg
(`discord.FFmpegOpusAudio(pipe=True)`). Playback begins as soon as the first
audio chunk arrives while synthesis continues in the background. On long
replies this removes most of the dead air between "agent replied" and "agent
is audible" — the longer the reply, the bigger the win.

### Safety net

Streaming is guarded two ways:

1. **Bounded startup.** If no audio arrives within
   `STREAM_FIRST_AUDIO_TIMEOUT` seconds (default 6), or the stream errors
   before the first chunk, the utterance transparently falls back to the V1
   whole-file synthesis path. Nothing has been played yet, so the listener
   never hears a glitch — just V1-style latency for that utterance.
2. **Kill switch.** `SEVEN_TTS_STREAM=0` disables streaming entirely and
   restores the V1 file path.

### Known limitation

Once streaming playback has begun there is no mid-utterance fallback: if the
TTS stream dies partway through, that utterance simply ends early (logged as
a warning). The pipe carries no position information to resume from, and
splicing in a file-synthesized replacement mid-sentence would sound worse
than the cut. Subsequent queued utterances are unaffected — each one attempts
streaming (with its own fallback) independently.

### New configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `SEVEN_TTS_STREAM` | `1` (on) | Set to `0`/`false`/`no` to force V1 whole-file synthesis |
| `STREAM_FIRST_AUDIO_TIMEOUT` | `6.0` | Seconds to wait for the first audio chunk before falling back to file synthesis |

### Migration from V1

No action required.

- All V1 environment variables keep their names, defaults, and meanings.
- Streaming is on by default; behavior change is latency only, not content.
- If you run on a very constrained or flaky host and prefer the old
  fully-buffered behavior, set `SEVEN_TTS_STREAM=0` in your `.env`.
- Commands (`!join`, `!leave`, `!skip`, `!stop`, `!test`, `!hush`,
  `!listen`) are unchanged. `!skip` and `!stop` tear down an in-flight
  stream cleanly, including the ffmpeg process and its feeder thread.

### Tests

`test_streaming.py` is a new offline harness (no network, no Discord). It
runs real ffmpeg but fakes edge-tts, generating its own sine-tone mp3
fixture, and proves: first audio decodes before synthesis completes, clean
EOF teardown, clean skip-mid-playback teardown, startup timeout and startup
error both raise before playback (so the caller can fall back), the worker
actually falls back to file synthesis when streaming breaks, and the worker
uses the streaming path when it works — with a file-descriptor,
feeder-thread, and dangling-task leak check after every test.

Run both suites:

```bash
python3 -m unittest test_helpers.py
python3 test_streaming.py
```

## V1 — initial release (2026-07-03)

Discord voice bridge: local Whisper STT in, transcript posted to the agent's
text channel, agent's normal replies read aloud with edge-tts (whole-file
synthesis). DAVE/E2EE receive shim, consent-first docs.

# Adapting Seven Voice to Your Companion Stack

Seven Voice is a **Discord voice bridge**, not an agent runtime and not an LLM
client. Its companion-facing seam is deliberately harness-agnostic: the bridge
posts a human's locally transcribed speech into the Discord text channel where a
companion already talks, then speaks replies from the configured companion
account. The companion keeps its existing model, memory, tools, personality, and
conversation loop.

The current Python program is still Discord-specific. A companion that already
speaks in Discord usually needs configuration, not a code port. Replacing Discord,
the speech recognizer, or the speech synthesizer does require an adapter.

## A plain-language prompt you can use

> I want to add Seven Voice to my existing companion without replacing the
> companion or creating a second voice-mode agent. Please inspect how my companion
> receives human turns and emits replies. If it already talks in Discord, configure
> this bridge around that channel and its account ID. Otherwise, adapt only the
> message handoff: deliver final speech transcripts through my harness's supported
> input path and send final companion replies back to the bridge for TTS. Preserve
> the human and companion allowlists, explicit join/listen controls, local
> transcription, and the rule that the bridge never calls an LLM. Do not copy real
> tokens or private IDs into source control.

## The actual data flow

```text
allowed human in Discord voice
  -> Discord voice receive + DAVE decryption
  -> PCM buffer, flushed after configured silence
  -> local faster-whisper transcription
  -> Discord text message mentioning the allowed companion account(s)
  -> the existing companion handles that message in its normal harness
  -> a reply from an allowed companion account in the bound text channel
  -> text cleanup and chunking
  -> edge-tts -> ffmpeg -> Discord voice playback
```

`seven_voice.py` does not assemble prompts, call a model API, read companion
memory, or decide what the companion should say. Discord text is the integration
bus in this implementation.

## Decide which kind of adaptation you need

### Your companion already replies in Discord

This is the intended shortest path. Follow the README setup and set:

- `AGENT_USER_IDS` to the Discord account ID or IDs whose messages may be spoken;
- `HUMAN_USER_IDS` to the people whose incoming voice may be transcribed;
- `TEXT_CHANNEL_ID` if the bridge should bind at startup, or leave it blank and
  let `!join` bind the text channel used for that session;
- the TTS, Whisper, silence, chunking, and streaming options in `.env.example`.

OpenClaw, Letta, Hermes, a custom bot, or another harness needs no Seven
Voice-specific plugin if its normal Discord identity can see and answer the
transcript message. The transcript begins with `🎤`, mentions every configured
companion account, and may be split below Discord's message limit.

### Your companion has a local/API input but no Discord text presence

Keep Discord voice receive and playback, but replace the text-channel handoff
with a small adapter:

1. In `SevenVoice.stt_flusher`, deliver the finalized transcript to the harness's
   supported input API, queue, CLI, or tool instead of calling
   `self.text_channel.send(...)`.
2. When the harness produces a **final** companion reply, pass its text to
   `SevenVoice.enqueue_agent_message` or directly to `self.queue`.
3. Carry a stable conversation/session identifier in both directions so one
   voice room cannot receive another session's reply.
4. Preserve the allowlists and command authorization at the adapter boundary.

Do not create a second, stateless model call merely because an API is convenient.
That would replace the companion at exactly the seam this project exists to
protect.

For an MCP-only environment, a thin local MCP server can expose explicit
`start`, `stop`, `hush`, `listen`, and reply-enqueue operations. MCP does not by
itself provide the long-running Discord receive loop: run the bridge as a service
and use MCP as its control/message adapter.

### You want a different voice or chat frontend

The reusable pattern is bidirectional transport around an existing companion;
the Discord SDK code is not portable unchanged. Replace these boundaries:

| Current boundary | Location | Replacement responsibility |
| --- | --- | --- |
| Discord voice input | `on_voice_packet` and `handle_command` | Yield PCM plus a trusted speaker identity; provide explicit join/listen controls. |
| Silence-to-turn policy | `AudioBuffer` and `stt_flusher` | Decide when an utterance is complete without clipping ordinary pauses. |
| Companion input | `stt_flusher` | Deliver a finalized transcript to the existing conversation. |
| Companion output | `on_message` and `enqueue_agent_message` | Accept only final replies from the intended companion/session. |
| Voice output | `speak_worker`, `play_stream`, `play_file` | Synthesize, queue, interrupt, and play audio in the chosen frontend. |

Discord-specific DAVE handling in `enable_dave_decrypt` belongs only to the
Discord receive adapter. Do not transplant its private-library shim into an
unrelated transport.

## Behavior worth preserving

A faithful adaptation should keep these properties even if its code looks
different:

- The bridge wraps the existing companion; it does not call an LLM itself.
- Human audio and companion replies are accepted from explicit allowlists.
- A session is bound to one intended text/conversation destination.
- Transcription is local (`faster-whisper`) in the shipped implementation.
- Short noise bursts and a small list of common Whisper hallucinations are
  discarded; transcript cleanup remains conservative.
- Humans can stop input (`!hush`), resume it (`!listen`), skip speech, clear the
  queue, and disconnect.
- Long replies are chunked and spoken in order.
- Temporary whole-file TTS artifacts are deleted.
- A failure before streamed audio begins falls back to whole-file synthesis;
  once playback has begun, a failed stream ends rather than replaying text from
  an uncertain position.
- Receive and transmit are tested separately. Working TTS does not prove that
  encrypted inbound voice can be decoded.

The shipped TTS is not fully local: `edge-tts` uses Microsoft's online speech
service. Swap the synthesizer if the installation needs offline output, and make
that privacy difference explicit.

## Security and privacy boundaries

- Use a separate Discord bot token for each installation and keep `.env` out of
  version control.
- Treat Discord IDs as authorization inputs, not display metadata. Do not remove
  `HUMAN_USER_IDS` or `AGENT_USER_IDS` filtering when adding an adapter.
- Get consent from everyone in a voice channel before transcribing it.
- Check the surrounding harness's message retention, logs, and memory behavior;
  local STT does not make the Discord transcript private.
- Keep the bot in a small private test channel until both directions and the
  stop/hush controls have been verified.
- Preserve soft failure during DAVE key changes: a bad frame becomes silence
  instead of killing the receive thread.

## Verification checklist

Before calling an adaptation complete:

1. Run the repository's offline helper tests:

   ```sh
   python3 -m unittest -v
   ```

2. With a private `.env`, verify parsing without connecting:

   ```sh
   python3 seven_voice.py --check-config
   ```

3. If network TTS is allowed, run the synthesis smoke test:

   ```sh
   python3 seven_voice.py --self-test
   ```

4. In a private Discord channel, verify `!join`, `!test`, one inbound spoken
   sentence, one real companion reply, `!hush`/`!listen`, `!skip`, `!stop`, and
   `!leave`.
5. Confirm an unlisted human is not transcribed, an unlisted account is not
   spoken, and a reply from another channel does not leak into the voice room.
6. Restart the process and repeat a short two-way turn. A service definition,
   container, or supervisor belongs to the deployment that owns it; this repo
   does not ship one.

The portable thing is the pipe around the companion. Keep the companion on one
side, the human on the other, and make every adapter boundary boring enough to
audit.

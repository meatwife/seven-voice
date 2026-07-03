# Release checklist

## Current state

Local repo only. No GitHub remote is configured yet.

Latest commits:

- `d34c395` Clarify bring-your-own-bot setup
- `e3faa76` Clarify setup and first live test docs
- `daaf536` Initial public Seven Voice scaffold

## Verified locally

- [x] Unit tests pass: `python3 -m unittest -v test_helpers.py`
- [x] Main script compiles: `python3 -m py_compile seven_voice.py`
- [x] Fake-env config check passes: `python seven_voice.py --check-config`
- [x] TTS self-test passes and cleans up generated mp3: `python seven_voice.py --self-test`
- [x] No real `.env` committed
- [x] No private Discord IDs or channel names found by leak scan
- [x] README says users create their own Discord bot/application
- [x] README does not offer access to Seven's/Sunny's private bot

## Not yet verified

- [ ] Clean-room Discord live test with an actual bot token and private test voice channel
- [ ] Inbound voice receive with DAVE decrypt path on current Discord voice stack
- [ ] Full loop: human voice -> transcript -> agent reply -> TTS playback
- [ ] GitHub remote configured under Sunny's GitHub
- [ ] Final public status wording updated after live test

## Live-test rule

Do not run the public scaffold with the same Discord bot token while the private voice bot is still running. Stop/swap the private bot first to avoid duplicate-client conflicts.

## Publishing rule

This repository publishes code only. Users bring their own Discord application, bot token, server, channel IDs, user IDs, and `.env` file. Seven's private bot remains private.

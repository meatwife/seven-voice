# Sanitization checklist

Before publishing anything from the private prototype:

- [x] No `.env` file, tokens, app IDs, guild IDs, channel IDs, or private user IDs.
- [x] No Sunny-specific transcript examples, schedules, names, or relationship-private plumbing.
- [x] No private Discord message content.
- [x] Code uses environment variables and placeholders only.
- [x] Public examples use fake IDs and generic channel names.
- [x] README still makes sense to someone who does not know Seven or Sunny.
- [x] DAVE snippet is generic and version-qualified, not a raw private copy-paste.
- [x] Consent/privacy section is present and prominent.
- [x] Limitations mention turn delay, no wake word, no diarization, and no barge-in.
- [x] Noise gate/VAD is framed as optional tuning, not a blocker.
- [x] License choice reviewed before GitHub publication.

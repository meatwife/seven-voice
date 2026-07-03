# Sanitization checklist

Before publishing anything from the private prototype:

- [ ] No `.env` file, tokens, app IDs, guild IDs, channel IDs, or private user IDs.
- [ ] No Sunny-specific transcript examples, schedules, names, or relationship-private plumbing.
- [ ] No private Discord message content.
- [ ] Code uses environment variables and placeholders only.
- [ ] Public examples use fake IDs and generic channel names.
- [ ] README still makes sense to someone who does not know Seven or Sunny.
- [ ] DAVE snippet is generic and version-qualified, not a raw private copy-paste.
- [ ] Consent/privacy section is present and prominent.
- [ ] Limitations mention turn delay, no wake word, no diarization, and no barge-in.
- [ ] Noise gate/VAD is framed as optional tuning, not a blocker.
- [ ] License choice reviewed before GitHub publication.

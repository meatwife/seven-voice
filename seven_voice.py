#!/usr/bin/env python3
"""Seven Voice: Discord voice bridge for companion AI agents.

Public scaffold. This bridge:
- receives human speech in Discord voice
- transcribes it locally
- posts transcript messages into Discord where the agent already lives
- reads the agent's normal Discord replies aloud

The bridge intentionally does not call an LLM. The agent remains itself.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import discord
import edge_tts
import numpy as np
from discord.ext import voice_recv
from faster_whisper import WhisperModel

try:
    import davey
except ImportError:  # DAVE receive support is version/platform sensitive.
    davey = None

OPUS_SILENCE = b"\xf8\xff\xfe"


@dataclass(frozen=True)
class Config:
    discord_token: str
    agent_user_ids: set[int]
    human_user_ids: set[int]
    text_channel_id: int | None
    tts_voice: str = "en-US-AndrewMultilingualNeural"
    whisper_model: str = "base.en"
    silence_flush_sec: float = 4.5
    min_utterance_sec: float = 0.6
    chunk_chars: int = 700

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv_if_present()
        token = require_env("DISCORD_TOKEN")
        return cls(
            discord_token=token,
            agent_user_ids=parse_ids(require_env("AGENT_USER_IDS")),
            human_user_ids=parse_ids(require_env("HUMAN_USER_IDS")),
            text_channel_id=parse_optional_int(os.getenv("TEXT_CHANNEL_ID", "")),
            tts_voice=os.getenv("TTS_VOICE", "en-US-AndrewMultilingualNeural"),
            whisper_model=os.getenv("WHISPER_MODEL", "base.en"),
            silence_flush_sec=float(os.getenv("SILENCE_FLUSH_SEC", "4.5")),
            min_utterance_sec=float(os.getenv("MIN_UTTERANCE_SEC", "0.6")),
            chunk_chars=int(os.getenv("CHUNK_CHARS", "700")),
        )


def load_dotenv_if_present(path: str = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def parse_ids(value: str) -> set[int]:
    return {int(x.strip()) for x in value.split(",") if x.strip()}


def parse_optional_int(value: str) -> int | None:
    value = value.strip()
    return int(value) if value else None


def enable_dave_decrypt(vc: voice_recv.VoiceRecvClient) -> None:
    """Patch inbound receive so DAVE-encrypted audio reaches Opus as Opus.

    Library internals change. Treat this as the version-specific part of the
    bridge and keep it small. If your voice receive library now supports DAVE
    natively, delete this patch.
    """
    if davey is None:
        logging.warning("davey is not installed; inbound DAVE voice may fail")
        return

    try:
        decryptor = vc._reader.decryptor          # noqa: SLF001, library internals
        original = decryptor.decrypt_rtp
        connection = vc._connection              # noqa: SLF001
    except AttributeError:
        logging.exception("voice receive internals changed; cannot patch DAVE decrypt")
        return

    def decrypt_rtp(packet):
        payload = original(packet)
        session = getattr(connection, "dave_session", None)
        ready = bool(session and getattr(session, "ready", False))
        version = getattr(connection, "dave_protocol_version", 0)
        if not ready or version == 0:
            return payload
        user_id = vc._ssrc_to_id.get(packet.ssrc)  # noqa: SLF001
        if user_id is None:
            return OPUS_SILENCE
        try:
            return session.decrypt(int(user_id), davey.MediaType.audio, payload)
        except Exception:
            return OPUS_SILENCE

    decryptor.decrypt_rtp = decrypt_rtp


HALLUCINATIONS = re.compile(r"^(thank you\.?|thanks for watching\.?|you\.?|bye\.?|\.+)$", re.I)
COMMAND = re.compile(r"^\s*(?:!|voice\s+)\s*(join|leave|skip|stop|test|hush|listen)\b", re.I)


def cleanup_transcript(text: str) -> str:
    text = text.strip()
    replacements = [
        (r"\b(laughing emoji|laugh emoji)\b", "😂"),
        (r"\b(heart emoji)\b", "❤️"),
        (r"\s+question mark\b", "?"),
        (r"\s+exclamation (point|mark)\b", "!"),
        (r"\s+period\b", "."),
        (r"\s+comma\b", ","),
    ]
    for pattern, replacement in replacements:
        text = re.sub(pattern, replacement, text, flags=re.I)
    text = re.sub(r"\s+([?.!,])", r"\1", text)
    text = re.sub(r"([?.!,])([^\s?.!,])", r"\1 \2", text)
    return re.sub(r"\s+", " ", text).strip()


def clean_tts_text(msg: discord.Message) -> str:
    text = msg.content
    text = re.sub(r"```.*?```", " Code block skipped. ", text, flags=re.S)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    for mention in msg.mentions:
        text = re.sub(rf"<@!?{mention.id}>", mention.display_name, text)
    text = re.sub(r"<a?:(\w+):\d+>", r"\1", text)
    text = re.sub(r"https?://\S+", " link ", text)
    text = re.sub(r"^MEDIA:\S+$", " attachment ", text, flags=re.M)
    text = re.sub(r"[*_~#>|]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def split_text(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    pieces, current = [], ""
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        while len(sentence) > limit:
            pieces.append(sentence[:limit])
            sentence = sentence[limit:]
        if current and len(current) + len(sentence) + 1 > limit:
            pieces.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        pieces.append(current)
    return pieces


class AudioBuffer:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.chunks: list[bytes] = []
        self.last_packet = 0.0

    def feed(self, pcm: bytes) -> None:
        with self.lock:
            self.chunks.append(pcm)
            self.last_packet = time.time()

    def take_if_silent(self, silence_sec: float) -> bytes | None:
        with self.lock:
            if not self.chunks or time.time() - self.last_packet < silence_sec:
                return None
            data = b"".join(self.chunks)
            self.chunks.clear()
            return data


class SevenVoice(discord.Client):
    def __init__(self, config: Config) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.voice_states = True
        super().__init__(intents=intents)
        self.config = config
        self.voice: voice_recv.VoiceRecvClient | None = None
        self.text_channel: discord.abc.Messageable | None = None
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.skip = asyncio.Event()
        self.listening = True
        self.audio = AudioBuffer()
        self.whisper: WhisperModel | None = None
        self.stt_executor = ThreadPoolExecutor(max_workers=1)
        self._workers_started = False

    async def on_ready(self) -> None:
        logging.info("logged in as %s (%s)", self.user, self.user.id if self.user else "unknown")
        if self.config.text_channel_id and self.text_channel is None:
            channel = self.get_channel(self.config.text_channel_id) or await self.fetch_channel(self.config.text_channel_id)
            if isinstance(channel, discord.abc.Messageable):
                self.text_channel = channel
                logging.info("bound text channel from TEXT_CHANNEL_ID=%s", self.config.text_channel_id)
        if not self._workers_started:
            self._workers_started = True
            asyncio.create_task(self.speak_worker())
            asyncio.create_task(self.stt_flusher())
            threading.Thread(target=self.load_whisper, daemon=True).start()

    def load_whisper(self) -> None:
        self.whisper = WhisperModel(self.config.whisper_model, device="cpu", compute_type="int8", cpu_threads=2)
        self.whisper.transcribe(np.zeros(16000, dtype=np.float32), language="en")
        logging.info("loaded whisper model %s", self.config.whisper_model)

    def on_voice_packet(self, user: discord.User | None, data: voice_recv.VoiceData) -> None:
        if user and user.id in self.config.human_user_ids and self.listening:
            self.audio.feed(data.pcm)

    async def transcribe_pcm(self, buf: bytes) -> str:
        def work() -> str:
            audio = np.frombuffer(buf, dtype=np.int16).reshape(-1, 2).mean(axis=1)
            audio = (audio[::3] / 32768.0).astype(np.float32)
            assert self.whisper is not None
            segments, _ = self.whisper.transcribe(audio, language="en", beam_size=1, vad_filter=True)
            return " ".join(seg.text.strip() for seg in segments).strip()
        return await self.loop.run_in_executor(self.stt_executor, work)

    async def stt_flusher(self) -> None:
        min_bytes = int(48000 * 2 * 2 * self.config.min_utterance_sec)
        while True:
            await asyncio.sleep(0.25)
            buf = self.audio.take_if_silent(self.config.silence_flush_sec)
            if not buf or len(buf) < min_bytes or self.whisper is None or self.text_channel is None:
                continue
            text = await self.transcribe_pcm(buf)
            if not text or HALLUCINATIONS.match(text):
                continue
            text = cleanup_transcript(text)
            for part in split_text(text, 1800):
                mentions = " ".join(f"<@{uid}>" for uid in self.config.agent_user_ids)
                await self.text_channel.send(f"🎤 {mentions} {part}")

    async def synth(self, text: str) -> str:
        fd, path = tempfile.mkstemp(suffix=".mp3", prefix="seven-voice-")
        os.close(fd)
        await edge_tts.Communicate(text, self.config.tts_voice).save(path)
        return path

    async def speak_worker(self) -> None:
        while True:
            text = await self.queue.get()
            if not self.voice or not self.voice.is_connected():
                continue
            self.skip.clear()
            path = await self.synth(text)
            try:
                done = asyncio.Event()
                self.voice.play(discord.FFmpegOpusAudio(path), after=lambda _: self.loop.call_soon_threadsafe(done.set))
                skip_task = asyncio.create_task(self.skip.wait())
                done_task = asyncio.create_task(done.wait())
                await asyncio.wait({skip_task, done_task}, return_when=asyncio.FIRST_COMPLETED)
                if self.skip.is_set() and self.voice.is_playing():
                    self.voice.stop()
                skip_task.cancel()
                done_task.cancel()
            finally:
                try:
                    os.unlink(path)
                except OSError:
                    pass

    async def enqueue_agent_message(self, msg: discord.Message) -> None:
        text = clean_tts_text(msg)
        for part in split_text(text, self.config.chunk_chars):
            await self.queue.put(part)

    async def on_message(self, msg: discord.Message) -> None:
        if self.user and msg.author.id == self.user.id:
            return
        if msg.author.id in self.config.agent_user_ids and self.text_channel and msg.channel.id == self.text_channel.id:
            await self.enqueue_agent_message(msg)
            return
        if msg.author.bot:
            return
        match = COMMAND.match(msg.content)
        if not match:
            return
        await self.handle_command(msg, match.group(1).lower())

    def clear_queue(self) -> None:
        while not self.queue.empty():
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def handle_command(self, msg: discord.Message, command: str) -> None:
        if command == "join":
            if not msg.author.voice or not msg.author.voice.channel:
                await msg.channel.send("Join a voice channel first, then say `!join`.")
                return
            target = msg.author.voice.channel
            if self.voice and self.voice.is_connected():
                await self.voice.move_to(target)
            else:
                self.voice = await target.connect(cls=voice_recv.VoiceRecvClient)
                self.voice.listen(voice_recv.BasicSink(self.on_voice_packet))
                enable_dave_decrypt(self.voice)
            self.text_channel = msg.channel
            await msg.channel.send("Connected. Use push-to-talk or mute between turns if the room is noisy. `!hush`, `!listen`, `!skip`, `!stop`, `!leave` are available.")
        elif command == "leave":
            self.clear_queue()
            self.skip.set()
            if self.voice and self.voice.is_connected():
                await self.voice.disconnect()
            self.voice = None
            self.text_channel = None
            await msg.channel.send("Disconnected.")
        elif command == "skip":
            self.skip.set()
        elif command == "stop":
            self.clear_queue()
            self.skip.set()
            await msg.channel.send("Stopped and cleared the queue.")
        elif command == "hush":
            self.listening = False
            await msg.channel.send("Not transcribing. Say `!listen` to turn voice input back on.")
        elif command == "listen":
            self.listening = True
            await msg.channel.send("Listening again.")
        elif command == "test":
            await self.queue.put("Test test. Seven Voice is connected.")


async def run_tts_smoke(config: Config) -> None:
    client = SevenVoice(config)
    path = await client.synth("Seven Voice smoke test.")
    size = os.path.getsize(path)
    os.unlink(path)
    print(f"TTS smoke test OK: generated {size} bytes")


def self_test(config: Config) -> None:
    assert cleanup_transcript("hello comma world question mark") == "hello, world?"
    assert split_text("One. Two. Three.", 7) == ["One.", "Two.", "Three."]
    print("Helper self-test OK")
    asyncio.run(run_tts_smoke(config))


def main() -> None:
    parser = argparse.ArgumentParser(description="Seven Voice Discord voice bridge")
    parser.add_argument("--check-config", action="store_true", help="load config and exit without connecting to Discord")
    parser.add_argument("--self-test", action="store_true", help="run helper checks and a tiny TTS synthesis test, then exit")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = Config.from_env()

    if args.check_config:
        print("Config OK")
        print(f"agent_user_ids={sorted(config.agent_user_ids)}")
        print(f"human_user_ids={sorted(config.human_user_ids)}")
        print(f"text_channel_id={config.text_channel_id}")
        print(f"tts_voice={config.tts_voice}")
        print(f"whisper_model={config.whisper_model}")
        return

    if args.self_test:
        self_test(config)
        return

    client = SevenVoice(config)
    client.run(config.discord_token, log_handler=None)


if __name__ == "__main__":
    main()

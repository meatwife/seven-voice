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
import fcntl
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

PIPE_CAPACITY = 1 << 20  # widen the tts pipe so producer writes never wedge


def parse_bool(value: str, default: bool = True) -> bool:
    value = value.strip().lower()
    if not value:
        return default
    return value not in ("0", "false", "no", "off")


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
    stream_tts: bool = True
    stream_first_audio_timeout: float = 6.0

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
            stream_tts=parse_bool(os.getenv("SEVEN_TTS_STREAM", "")),
            stream_first_audio_timeout=float(os.getenv("STREAM_FIRST_AUDIO_TIMEOUT", "6.0")),
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


# ---------------------------------------------------------------- streaming TTS
#
# discord.FFmpegOpusAudio(source, pipe=True) spawns a thread that
# blocking-reads source.read(8192) and feeds ffmpeg's stdin, so the source
# must be a real file object with proper EOF semantics — an os.pipe() read
# end, not a BytesIO. edge-tts chunks go in the write end as they arrive;
# playback starts on the first chunk while synthesis continues.

tts_pipe_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tts-pipe")


class _PipeWriteEnd:
    """Write end of the tts pipe. write() and close() only ever run on
    tts_pipe_executor (single thread), which serializes them — no fd can be
    closed under an in-flight write or double-closed after fd-number reuse."""

    def __init__(self, fd: int):
        self.fd = fd
        self.closed = False

    def write(self, data: bytes) -> None:
        if self.closed:
            return
        view = memoryview(data)
        while view:
            try:
                n = os.write(self.fd, view)
            except OSError:  # reader gone (skip/stop killed ffmpeg) — drop the rest
                return
            view = view[n:]

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            try:
                os.close(self.fd)
            except OSError:
                pass


async def _stream_tts(text: str, voice: str, wend: _PipeWriteEnd,
                      first_audio: asyncio.Future) -> None:
    """Producer: pump edge-tts audio chunks into the pipe write end."""
    loop = asyncio.get_running_loop()
    t0 = time.time()
    nbytes = 0
    try:
        async for chunk in edge_tts.Communicate(text, voice).stream():
            if chunk["type"] != "audio":
                continue
            data = chunk["data"]
            nbytes += len(data)
            if not first_audio.done():
                first_audio.set_result(time.time())
            await loop.run_in_executor(tts_pipe_executor, wend.write, data)
        logging.info("tts stream: synth finished in %.2fs (%d bytes, %d chars)",
                     time.time() - t0, nbytes, len(text))
    except Exception as e:
        if not first_audio.done():
            first_audio.set_exception(e)  # caller falls back to file synthesis
        else:
            # Playback is already running off this pipe; there is no clean way
            # to splice in a fallback mid-utterance. Close the pipe so ffmpeg
            # gets EOF and the chunk just ends early.
            logging.warning("TTS stream died mid-playback, chunk cut short: %s", e)
    finally:
        tts_pipe_executor.submit(wend.close)


async def start_stream(text: str, voice: str, first_audio_timeout: float, t_dequeue: float):
    """Start edge-tts -> pipe -> ffmpeg. Returns once ffmpeg has a source
    (first audio chunk arrived). Raises if no audio within
    first_audio_timeout or the stream errors first — caller falls back to
    file synthesis; nothing has been played yet."""
    rfd, wfd = os.pipe()
    try:
        fcntl.fcntl(wfd, fcntl.F_SETPIPE_SZ, PIPE_CAPACITY)
    except OSError:
        pass  # default 64K pipe still works, writes just backpressure sooner
    rfile = os.fdopen(rfd, "rb", buffering=0)  # raw: read() returns what's available
    wend = _PipeWriteEnd(wfd)
    first_audio = asyncio.get_running_loop().create_future()
    writer = asyncio.ensure_future(_stream_tts(text, voice, wend, first_audio))
    try:
        t_first = await asyncio.wait_for(asyncio.shield(first_audio), first_audio_timeout)
        logging.info("tts stream: first audio %.2fs after dequeue (%d chars)",
                     t_first - t_dequeue, len(text))
        source = discord.FFmpegOpusAudio(rfile, pipe=True)
    except BaseException:
        writer.cancel()
        tts_pipe_executor.submit(wend.close)
        try:
            rfile.close()
        except OSError:
            pass
        await asyncio.wait({writer}, timeout=5)
        if first_audio.done() and not first_audio.cancelled():
            first_audio.exception()  # consume so asyncio doesn't log it
        else:
            first_audio.cancel()
        raise
    return source, writer, wend, rfile


async def finish_stream(source, writer, wend, rfile) -> None:
    """Tear down a stream in an order that provably unwedges every party:
    stop producing, EOF the pipe, let ffmpeg's feeder thread exit, close the
    read end (breaks any pathological blocked write), reap the producer."""
    writer.cancel()
    tts_pipe_executor.submit(wend.close)
    thread = getattr(source, "_pipe_writer_thread", None)  # noqa: SLF001, library internals
    if thread is not None:
        await asyncio.to_thread(thread.join, 3.0)
        if thread.is_alive():
            logging.warning("ffmpeg stdin feeder thread still alive after join timeout")
    try:
        rfile.close()
    except OSError:
        pass
    done, pending = await asyncio.wait({writer}, timeout=5)
    if pending:
        logging.warning("tts stream producer task did not exit within 5s")


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

    async def _play_and_wait(self, vc, source) -> None:
        """Play a source until it ends or !skip/!stop fires."""
        done = asyncio.Event()
        loop = asyncio.get_running_loop()
        vc.play(source, after=lambda _: loop.call_soon_threadsafe(done.set))
        skip_task = asyncio.create_task(self.skip.wait())
        done_task = asyncio.create_task(done.wait())
        await asyncio.wait({skip_task, done_task}, return_when=asyncio.FIRST_COMPLETED)
        if self.skip.is_set() and vc.is_playing():
            vc.stop()
        skip_task.cancel()
        done_task.cancel()

    async def play_stream(self, vc, text: str, t_dequeue: float) -> None:
        """Speak text by streaming edge-tts into ffmpeg through an OS pipe.
        Raises only for failures before playback starts (caller then falls
        back to file synthesis). Once playback has begun there is no
        fallback: a mid-stream synth failure ends the utterance early
        (logged), because the pipe carries no position info to resume from."""
        source, writer, wend, rfile = await start_stream(
            text, self.config.tts_voice, self.config.stream_first_audio_timeout, t_dequeue)
        try:
            t_play = time.time()
            await self._play_and_wait(vc, source)
            logging.info("tts stream: playback started %.2fs after dequeue, ran %.2fs",
                         t_play - t_dequeue, time.time() - t_play)
        except Exception:
            logging.exception("stream playback failed")
        finally:
            await finish_stream(source, writer, wend, rfile)

    async def play_file(self, vc, text: str, t_dequeue: float) -> None:
        """Original path: synthesize a whole mp3, then play it."""
        try:
            t0 = time.time()
            path = await self.synth(text)
            logging.info("tts file: synth took %.2fs (%d chars)", time.time() - t0, len(text))
        except Exception:
            logging.exception("TTS synthesis failed")
            return
        try:
            t_play = time.time()
            await self._play_and_wait(vc, discord.FFmpegOpusAudio(path))
            logging.info("tts file: playback started %.2fs after dequeue, ran %.2fs",
                         t_play - t_dequeue, time.time() - t_play)
        except Exception:
            logging.exception("playback failed")
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    async def speak_worker(self) -> None:
        logging.info("speak worker up, streaming tts %s", "ON" if self.config.stream_tts else "OFF")
        while True:
            text = await self.queue.get()
            vc = self.voice
            if not vc or not vc.is_connected():
                continue
            self.skip.clear()
            t_dequeue = time.time()
            if self.config.stream_tts:
                try:
                    await self.play_stream(vc, text, t_dequeue)
                    continue
                except Exception:
                    logging.exception(
                        "streaming TTS failed before playback, falling back to file synthesis")
            await self.play_file(vc, text, t_dequeue)

    async def enqueue_agent_message(self, msg: discord.Message) -> None:
        text = clean_tts_text(msg)
        if not text:
            return
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
        print(f"stream_tts={config.stream_tts}")
        print(f"stream_first_audio_timeout={config.stream_first_audio_timeout}")
        return

    if args.self_test:
        self_test(config)
        return

    client = SevenVoice(config)
    client.run(config.discord_token, log_handler=None)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Offline harness for the streaming TTS path in seven_voice.py.

Runs real ffmpeg (via discord.FFmpegOpusAudio) but fakes edge-tts with a
producer that replays a locally generated mp3 in timed chunks, so no network
and no Discord connection are needed. Proves:

  1. first audio reaches ffmpeg (and decodes) before synthesis completes
  2. normal EOF tears everything down cleanly
  3. skip/cancel mid-playback closes writer, pipe, and ffmpeg process
  4. init timeout raises before playback -> caller can fall back
  5. init error raises before playback -> caller can fall back
  6. speak_worker actually falls back to the file path when streaming fails
  leak check after every test: no stray tasks, feeder threads, or fds

Requires ffmpeg on PATH (it generates its own test mp3 with a sine tone).

Run:  python3 test_streaming.py
"""

import asyncio
import gc
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import seven_voice as sv  # noqa: E402

VOICE = "test-voice"
TIMEOUT = 6.0

MP3 = None
MP3_BYTES = b""


def make_test_mp3() -> str:
    """Generate ~8s of tone as mp3 so the harness ships no binary fixtures."""
    fd, path = tempfile.mkstemp(suffix=".mp3", prefix="seven-voice-test-")
    os.close(fd)
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=8",
         "-codec:a", "libmp3lame", "-b:a", "64k", path],
        check=True,
    )
    return path


REAL_EDGE_TTS = sv.edge_tts


class FakeCommunicate:
    """Replays MP3_BYTES in chunks with a delay, like a slow edge-tts."""

    chunk_size = 4096
    chunk_delay = 0.12
    instances = []

    def __init__(self, text, voice):
        self.yielded = 0
        self.total = (len(MP3_BYTES) + self.chunk_size - 1) // self.chunk_size
        self.finished = False
        FakeCommunicate.instances.append(self)

    async def stream(self):
        for i in range(0, len(MP3_BYTES), self.chunk_size):
            await asyncio.sleep(self.chunk_delay)
            self.yielded += 1
            yield {"type": "audio", "data": MP3_BYTES[i:i + self.chunk_size]}
        self.finished = True


class FastCommunicate(FakeCommunicate):
    chunk_delay = 0.005


class NeverCommunicate(FakeCommunicate):
    async def stream(self):
        await asyncio.sleep(3600)
        yield {"type": "audio", "data": b""}


class BrokenCommunicate(FakeCommunicate):
    async def stream(self):
        raise RuntimeError("synthetic edge-tts failure")
        yield  # pragma: no cover


def patch_comm(cls):
    sv.edge_tts = types.SimpleNamespace(Communicate=cls)


def unpatch():
    sv.edge_tts = REAL_EDGE_TTS


def feeder_threads():
    return [t for t in threading.enumerate()
            if t.name.startswith("popen-stdin-writer") and t.is_alive()]


def open_fd_count():
    return len(os.listdir("/proc/self/fd"))


async def drain(source, limit=None):
    """Read opus packets off ffmpeg stdout until EOF (or limit packets)."""
    n = 0
    while True:
        data = await asyncio.to_thread(source.read)
        if not data:
            return n, True
        n += 1
        if limit is not None and n >= limit:
            return n, False


async def read_first_packet(source):
    return await asyncio.to_thread(source.read)


def make_config(**overrides) -> sv.Config:
    defaults = dict(
        discord_token="test-token",
        agent_user_ids={1},
        human_user_ids={2},
        text_channel_id=None,
        tts_voice=VOICE,
        stream_tts=True,
        stream_first_audio_timeout=TIMEOUT,
    )
    defaults.update(overrides)
    return sv.Config(**defaults)


# ---------------------------------------------------------------- tests

async def test_first_audio_before_synth_done():
    patch_comm(FakeCommunicate)
    source, writer, wend, rfile = await sv.start_stream("x" * 500, VOICE, TIMEOUT, time.time())
    fake = FakeCommunicate.instances[-1]
    pkt = await read_first_packet(source)
    yielded_at_first_packet, total = fake.yielded, fake.total
    early = not fake.finished and yielded_at_first_packet < total
    # let it finish normally so teardown is clean
    await drain(source)
    source.cleanup()
    await sv.finish_stream(source, writer, wend, rfile)
    assert pkt, "no opus packet ever came out of ffmpeg"
    assert early, (
        f"first packet only after {yielded_at_first_packet}/{total} chunks, "
        f"finished={fake.finished} — streaming gave no head start")
    print(f"  first opus packet after {yielded_at_first_packet}/{total} "
          f"synth chunks (synthesis still running)")


async def test_normal_eof():
    patch_comm(FastCommunicate)
    source, writer, wend, rfile = await sv.start_stream("y" * 500, VOICE, TIMEOUT, time.time())
    fake = FakeCommunicate.instances[-1]
    npkt, eof = await drain(source)
    source.cleanup()  # what AudioPlayer.run() does when read() returns b''
    await sv.finish_stream(source, writer, wend, rfile)
    assert eof and npkt > 100, f"expected clean EOF after many packets, got {npkt}, eof={eof}"
    assert fake.finished, "producer did not run to completion"
    assert writer.done(), "producer task still pending"
    assert wend.closed, "pipe write end not closed"
    assert rfile.closed, "pipe read end not closed"
    assert source._process is sv.discord.player.MISSING or source._process.poll() is not None
    print(f"  {npkt} packets, clean EOF, everything closed")


async def test_skip_mid_playback():
    patch_comm(FakeCommunicate)  # slow: still synthesizing when we skip
    source, writer, wend, rfile = await sv.start_stream("z" * 500, VOICE, TIMEOUT, time.time())
    fake = FakeCommunicate.instances[-1]
    proc = source._process
    await drain(source, limit=10)  # play a little
    assert not fake.finished, "test invalid: synth finished before skip"
    # skip: AudioPlayer.stop() -> run() finally -> source.cleanup(); then the
    # worker's finally runs finish_stream. Emulate that order.
    source.cleanup()
    await sv.finish_stream(source, writer, wend, rfile)
    assert writer.done(), "producer task still pending after skip"
    assert wend.closed, "pipe write end not closed after skip"
    assert rfile.closed, "pipe read end not closed after skip"
    assert proc.poll() is not None, "ffmpeg still running after skip"
    # executor must still be usable (not wedged on a blocked write)
    assert sv.tts_pipe_executor.submit(lambda: 42).result(timeout=2) == 42
    print(f"  skipped after 10 packets at {fake.yielded}/{fake.total} chunks; "
          "writer, pipe, ffmpeg all down; executor responsive")


async def test_init_timeout_falls_back():
    patch_comm(NeverCommunicate)
    t0 = time.time()
    try:
        await sv.start_stream("t" * 100, VOICE, 0.5, time.time())
    except (asyncio.TimeoutError, TimeoutError):
        pass
    else:
        raise AssertionError("no timeout raised for silent stream")
    print(f"  silent stream raised timeout after {time.time() - t0:.2f}s, no playback")


async def test_init_error_falls_back():
    patch_comm(BrokenCommunicate)
    try:
        await sv.start_stream("e" * 100, VOICE, TIMEOUT, time.time())
    except RuntimeError as e:
        assert "synthetic" in str(e)
    else:
        raise AssertionError("stream error was not raised to caller")
    print("  immediate stream error raised to caller, no playback")


class FakeVC:
    """Just enough of VoiceClient for speak_worker: play() drives the source
    from a thread like AudioPlayer does, minus the 20ms pacing."""

    def __init__(self):
        self.sources = []
        self._thread = None

    def is_connected(self):
        return True

    def is_playing(self):
        return self._thread is not None and self._thread.is_alive()

    def stop(self):
        pass

    def play(self, source, after=None):
        self.sources.append(source)

        def run():
            error = None
            try:
                while source.read():
                    pass
            except Exception as e:  # pragma: no cover
                error = e
            finally:
                source.cleanup()
                if after:
                    after(error)

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()


async def test_worker_falls_back_to_file():
    patch_comm(BrokenCommunicate)
    client = sv.SevenVoice(make_config())

    async def fake_synth(text):
        fd, path = tempfile.mkstemp(suffix=".mp3", prefix="seven-voice-test-")
        os.close(fd)
        shutil.copy(MP3, path)
        return path

    client.synth = fake_synth
    vc = FakeVC()
    client.voice = vc
    worker = asyncio.ensure_future(client.speak_worker())
    try:
        await client.queue.put("fallback test sentence")
        for _ in range(200):
            await asyncio.sleep(0.05)
            if vc.sources and not vc.is_playing():
                break
        else:
            raise AssertionError("worker never played anything")
        assert len(vc.sources) == 1, f"expected exactly one source, got {len(vc.sources)}"
        assert isinstance(vc.sources[0], sv.discord.FFmpegOpusAudio)
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        await client.close()
    print("  streaming init failed -> worker fell back to file synthesis and played it")


async def test_worker_streams_when_enabled():
    patch_comm(FastCommunicate)
    client = sv.SevenVoice(make_config())
    file_synth_calls = []

    async def tracking_synth(text):
        file_synth_calls.append(text)  # pragma: no cover — must not be hit
        raise AssertionError("file synth used while streaming should serve")

    client.synth = tracking_synth
    vc = FakeVC()
    client.voice = vc
    worker = asyncio.ensure_future(client.speak_worker())
    try:
        await client.queue.put("streaming test sentence")
        for _ in range(200):
            await asyncio.sleep(0.05)
            if vc.sources and not vc.is_playing():
                break
        else:
            raise AssertionError("worker never played anything")
        assert len(vc.sources) == 1, f"expected exactly one source, got {len(vc.sources)}"
        assert not file_synth_calls, "worker used file synthesis despite streaming"
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        await client.close()
    print("  worker served the utterance from the streaming path, no file synth")


TESTS = [
    test_first_audio_before_synth_done,
    test_normal_eof,
    test_skip_mid_playback,
    test_init_timeout_falls_back,
    test_init_error_falls_back,
    test_worker_falls_back_to_file,
    test_worker_streams_when_enabled,
]


def leak_check(label, fd_before):
    gc.collect()
    time.sleep(0.2)
    gc.collect()
    stray = feeder_threads()
    assert not stray, f"{label}: leaked feeder threads {stray}"
    fd_after = open_fd_count()
    # allow small jitter from executor spin-up / logging, catch real leaks
    assert fd_after <= fd_before + 3, f"{label}: fds grew {fd_before} -> {fd_after}"
    return fd_after


async def amain(test):
    await test()
    # no dangling tasks besides this one
    cur = asyncio.current_task()
    others = [t for t in asyncio.all_tasks() if t is not cur and not t.done()]
    assert not others, f"dangling tasks: {others}"


def main():
    global MP3, MP3_BYTES
    MP3 = make_test_mp3()
    with open(MP3, "rb") as f:
        MP3_BYTES = f.read()
    print(f"generated {len(MP3_BYTES)}-byte test mp3\n")
    failures = 0
    try:
        fd_baseline = open_fd_count()
        for test in TESTS:
            name = test.__name__
            print(f"[ RUN  ] {name}")
            try:
                asyncio.run(amain(test))
                fd_baseline = leak_check(name, fd_baseline)
                print(f"[ PASS ] {name}")
            except Exception as e:
                failures += 1
                import traceback
                traceback.print_exc()
                print(f"[ FAIL ] {name}: {e}")
            finally:
                unpatch()
    finally:
        try:
            os.unlink(MP3)
        except OSError:
            pass
    print(f"\n{len(TESTS) - failures}/{len(TESTS)} passed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()

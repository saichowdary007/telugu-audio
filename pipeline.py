#!/usr/bin/env python3
import json
import math
import re
import statistics
import struct
import subprocess
import sys
import tempfile
import wave
from pathlib import Path


TIME_RE = re.compile(
    r"(\d{2}):(\d{2}):(\d{2}),(\d{3})\s+-->\s+(\d{2}):(\d{2}):(\d{2}),(\d{3})"
)
NAME_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9 _-]{1,30})\s*:\s*(.+)$", re.S)
VOICE_RE = re.compile(r"^(.*?)\s+([a-z]{2}_[A-Z]{2}|[a-z]{3}_[0-9]{3})\s+#", re.M)

MALE_WORDS = {"man", "men", "male", "boy", "father", "dad", "son", "brother", "husband"}
FEMALE_WORDS = {"woman", "women", "female", "girl", "mother", "mom", "daughter", "sister", "wife"}
CHILD_WORDS = {"child", "kid", "baby", "children"}

BASE_RATE = {"adult_male": 152, "adult_female": 176, "child": 210, "elderly": 138}


def usage():
    raise SystemExit(
        "Usage: python3 pipeline.py input.srt output_dir [--media movie.mp4] "
        "[--speaker-map line_speaker_map.json] [--speakers speakers.json] "
        "[--tts-engine macos|mms] [--single-only] [--dry-run]"
    )


def parse_args(argv):
    if len(argv) < 3:
        usage()
    media = None
    speaker_map = None
    speakers_path = None
    tts_engine = "macos"
    single_only = False
    dry_run = False
    extra = argv[3:]
    i = 0
    while i < len(extra):
        if extra[i] == "--media" and i + 1 < len(extra):
            media = Path(extra[i + 1])
            i += 2
        elif extra[i] == "--speaker-map" and i + 1 < len(extra):
            speaker_map = Path(extra[i + 1])
            i += 2
        elif extra[i] == "--speakers" and i + 1 < len(extra):
            speakers_path = Path(extra[i + 1])
            i += 2
        elif extra[i] == "--tts-engine" and i + 1 < len(extra):
            tts_engine = extra[i + 1]
            if tts_engine not in {"macos", "mms"}:
                usage()
            i += 2
        elif extra[i] == "--single-only":
            single_only = True
            i += 1
        elif extra[i] == "--dry-run":
            dry_run = True
            i += 1
        else:
            usage()
    return Path(argv[1]), Path(argv[2]), media, speaker_map, speakers_path, tts_engine, single_only, dry_run


def seconds(parts):
    h, m, s, ms = map(int, parts)
    return h * 3600 + m * 60 + s + ms / 1000


def parse_srt(path):
    raw = Path(path).read_text(encoding="utf-8-sig").replace("\r\n", "\n").strip()
    if not raw:
        return []

    items = []
    pending = None
    for block in re.split(r"\n\s*\n", raw):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if len(lines) < 2:
            if pending and lines:
                pending["text"] = f"{pending['text']} {' '.join(lines)}".strip()
            elif pending and not lines:
                continue
            continue
        time_line = lines[1] if lines[0].isdigit() and len(lines) > 1 else lines[0]
        text_lines = lines[2:] if lines[0].isdigit() else lines[1:]
        match = TIME_RE.search(time_line)
        text = re.sub(r"<[^>]+>", "", " ".join(text_lines)).strip()
        if match and text:
            if pending:
                items.append(pending)
                pending = None
            items.append(
                {
                    "start": seconds(match.groups()[:4]),
                    "end": seconds(match.groups()[4:]),
                    "text": text,
                }
            )
        elif match:
            pending = {
                "start": seconds(match.groups()[:4]),
                "end": seconds(match.groups()[4:]),
                "text": "",
            }
        elif text and pending:
            pending["text"] = f"{pending['text']} {text}".strip()
    if pending and pending["text"]:
        items.append(pending)
    return items


def normalize_name(name):
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def guess_type(text):
    words = set(re.findall(r"[a-z]+", text.lower()))
    if words & CHILD_WORDS:
        return "child"
    if words & FEMALE_WORDS:
        return "adult_female"
    if words & MALE_WORDS:
        return "adult_male"
    return None


def discover_telugu_voices():
    proc = subprocess.run(["say", "-v", "?"], capture_output=True, text=True, check=True)
    voices = [m.group(1).strip() for line in proc.stdout.splitlines() if (m := VOICE_RE.match(line)) and m.group(2) == "te_IN"]
    return voices or ["Geeta"]


def extract_audio(media_path, wav_path):
    try:
        subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-y", "-i", str(media_path), "-ac", "1", "-ar", "16000", "-vn", str(wav_path)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except subprocess.CalledProcessError:
        return False


def probe_duration(path):
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(proc.stdout.strip())


def read_segment(reader, start, end):
    sr = reader.getframerate()
    start_frame = max(0, int(start * sr))
    end_frame = min(reader.getnframes(), int(end * sr))
    if end_frame <= start_frame:
        return [], sr
    reader.setpos(start_frame)
    raw = reader.readframes(end_frame - start_frame)
    if not raw:
        return [], sr
    samples = struct.unpack("<{}h".format(len(raw) // 2), raw)
    return samples, sr


def estimate_pitch(samples, sr):
    if len(samples) < sr // 5:
        return None

    frame_size = max(320, int(sr * 0.04))
    hop = max(160, int(sr * 0.02))
    min_lag = max(1, int(sr / 400))
    max_lag = max(min_lag + 1, int(sr / 60))
    voiced = []

    for offset in range(0, len(samples) - frame_size + 1, hop):
        frame = samples[offset : offset + frame_size]
        mean = sum(frame) / len(frame)
        centered = [x - mean for x in frame]
        energy = sum(x * x for x in centered)
        if energy < 1_000_000:
            continue

        best_lag = None
        best_score = 0.0
        for lag in range(min_lag, max_lag + 1):
            score = 0.0
            for i in range(len(centered) - lag):
                score += centered[i] * centered[i + lag]
            score /= energy
            if score > best_score:
                best_score = score
                best_lag = lag

        if best_lag and best_score > 0.25:
            voiced.append(sr / best_lag)

    return round(statistics.median(voiced), 1) if voiced else None


def analyze_subtitle(reader, text, start, end):
    samples, sr = read_segment(reader, start, end)
    pitch = estimate_pitch(samples, sr)
    words = len(re.findall(r"\w+", text))
    duration = max(0.1, end - start)
    speech_rate = round(words / duration, 2)
    bucket = "unknown"
    if pitch is not None:
        if pitch < 110:
            bucket = "adult_male"
        elif pitch < 170:
            bucket = "adult_female"
        elif pitch < 225:
            bucket = "child"
        else:
            bucket = "child"
    return {"pitch_hz": pitch, "speech_rate": speech_rate, "bucket": bucket, "duration": round(duration, 3)}


def fallback_type(text):
    named = NAME_RE.match(text)
    if named:
        return guess_type(named.group(1)) or guess_type(named.group(2))
    return guess_type(text)


def choose_speaker_from_features(state, features, text):
    if features["pitch_hz"] is None:
        return choose_speaker_from_text(state, text)

    best_id = None
    best_score = 1e9
    for speaker_id, profile in state["profiles"].items():
        score = abs(profile["pitch_hz"] - features["pitch_hz"]) / 50
        if profile["bucket"] != features["bucket"]:
            score += 0.4
        if score < best_score:
            best_score = score
            best_id = speaker_id

    if best_id is None or best_score > 1.0:
        return new_speaker(state, features)

    update_profile(state, best_id, features)
    return best_id


def choose_speaker_from_text(state, text):
    named = NAME_RE.match(text)
    if named:
        key = normalize_name(named.group(1))
        clean = named.group(2).strip()
        speaker_type = guess_type(named.group(1)) or guess_type(clean)
        if key not in state["by_name"]:
            state["by_name"][key] = new_speaker(state, {"bucket": speaker_type or "unknown", "pitch_hz": None, "speech_rate": 1.0})
        return state["by_name"][key], clean

    speaker_type = guess_type(text)
    if speaker_type:
        typed = state["by_type"].setdefault(speaker_type, [])
        if not typed:
            typed.append(new_speaker(state, {"bucket": speaker_type, "pitch_hz": None, "speech_rate": 1.0}))
        return typed[0], text

    state["fallback_index"] = 1 - state["fallback_index"]
    speaker_type = "adult_male" if state["fallback_index"] == 0 else "adult_female"
    speaker_id = state["fallback"].get(speaker_type)
    if not speaker_id:
        speaker_id = new_speaker(state, {"bucket": speaker_type, "pitch_hz": None, "speech_rate": 1.0})
        state["fallback"][speaker_type] = speaker_id
    return speaker_id, text


def voice_rate(bucket, speech_rate, index):
    base = BASE_RATE.get(bucket, 160)
    adjust = max(-18, min(18, int((speech_rate - 2.0) * 6)))
    wobble = (index % 3 - 1) * 4
    return max(120, min(240, base + adjust + wobble))


def new_speaker(state, features):
    speaker_id = f"speaker_{len(state['speakers']) + 1:03d}"
    bucket = features["bucket"]
    idx = state["bucket_counts"].get(bucket, 0)
    state["bucket_counts"][bucket] = idx + 1
    voice_name = state["voices"][state["voice_index"] % len(state["voices"])]
    state["voice_index"] += 1
    profile = {
        "label": speaker_id,
        "type": bucket,
        "language_code": "te-IN",
        "voice_name": voice_name,
        "rate": voice_rate(bucket, features["speech_rate"], idx),
        "pitch_hz": features["pitch_hz"],
        "speech_rate": features["speech_rate"],
        "dialect_hint": {"adult_male": "low_pitch", "adult_female": "mid_pitch", "child": "high_pitch"}.get(bucket, "unknown"),
    }
    state["speakers"][speaker_id] = profile
    state["profiles"][speaker_id] = {"pitch_hz": features["pitch_hz"] or 0.0, "speech_rate": features["speech_rate"], "bucket": bucket}
    return speaker_id


def update_profile(state, speaker_id, features):
    profile = state["profiles"][speaker_id]
    profile["pitch_hz"] = round((profile["pitch_hz"] + (features["pitch_hz"] or profile["pitch_hz"])) / 2, 1) if features["pitch_hz"] else profile["pitch_hz"]
    profile["speech_rate"] = round((profile["speech_rate"] + features["speech_rate"]) / 2, 2)

    speaker = state["speakers"][speaker_id]
    speaker["pitch_hz"] = profile["pitch_hz"]
    speaker["speech_rate"] = profile["speech_rate"]
    speaker["rate"] = voice_rate(profile["bucket"], profile["speech_rate"], 0)


def synthesize(text, profile, out_path):
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        src = tmpdir / "input.txt"
        aiff = tmpdir / "clip.aiff"
        src.write_text(text, encoding="utf-8")
        subprocess.run(
            ["say", "-v", profile["voice_name"], "-r", str(profile["rate"]), "-o", str(aiff), "-f", str(src)],
            check=True,
        )
        subprocess.run(["afconvert", "-f", "m4af", "-d", "aac", str(aiff), str(out_path)], check=True)


class MmsTeluguSynthesizer:
    def __init__(self):
        try:
            import numpy as np
            import torch
            from transformers import AutoTokenizer, VitsModel
        except ImportError as exc:
            raise SystemExit(
                "MMS Telugu TTS needs the real-model dependencies. Install with:\n"
                "  /opt/homebrew/bin/python3.12 -m venv .venv\n"
                "  .venv/bin/pip install -r requirements-real-model.txt\n"
                "Then run with .venv/bin/python."
            ) from exc

        self.np = np
        self.torch = torch
        self.tokenizer = AutoTokenizer.from_pretrained("facebook/mms-tts-tel")
        self.model = VitsModel.from_pretrained("facebook/mms-tts-tel")
        self.model.eval()
        self.sample_rate = int(self.model.config.sampling_rate)

    def normalize_text(self, text):
        text = text.replace("...", ".")
        return re.sub(r"[A-Za-z][A-Za-z'_-]*", lambda match: self.latin_word_to_telugu(match.group(0)), text)

    def latin_word_to_telugu(self, word):
        known = {
            "andrew": "ఆండ్రూ",
            "boss": "బాస్",
            "chuck": "చక్",
            "daniels": "డేనియల్స్",
            "dolores": "డొలోరెస్",
            "george": "జార్జ్",
            "laeddis": "లేడిస్",
            "portland": "పోర్ట్ ల్యాండ్",
            "rachel": "రేచెల్",
            "teddy": "టెడ్డీ",
        }
        key = re.sub(r"[^a-z]", "", word.lower())
        if key in known:
            return known[key]

        consonants = {
            "b": "బ",
            "c": "క",
            "d": "డ",
            "f": "ఫ",
            "g": "గ",
            "h": "హ",
            "j": "జ",
            "k": "క",
            "l": "ల",
            "m": "మ",
            "n": "న",
            "p": "ప",
            "q": "క",
            "r": "ర",
            "s": "స",
            "t": "ట",
            "v": "వ",
            "w": "వ",
            "x": "క్స్",
            "y": "య",
            "z": "జ",
        }
        clusters = {
            "ch": "చ",
            "sh": "ష",
            "th": "త",
            "dh": "ద",
            "ph": "ఫ",
            "bh": "భ",
            "kh": "ఖ",
            "gh": "ఘ",
        }
        vowels = {
            "a": "",
            "e": "ె",
            "i": "ి",
            "o": "ొ",
            "u": "ు",
            "aa": "ా",
            "ee": "ీ",
            "ii": "ీ",
            "oo": "ూ",
            "uu": "ూ",
            "ai": "ై",
            "au": "ౌ",
        }
        independent_vowels = {
            "a": "అ",
            "e": "ఎ",
            "i": "ఇ",
            "o": "ఒ",
            "u": "ఉ",
            "aa": "ఆ",
            "ee": "ఈ",
            "ii": "ఈ",
            "oo": "ఊ",
            "uu": "ఊ",
            "ai": "ఐ",
            "au": "ఔ",
        }

        result = []
        i = 0
        while i < len(key):
            vowel = next((v for v in ("aa", "ee", "ii", "oo", "uu", "ai", "au") if key.startswith(v, i)), None)
            if vowel or key[i] in "aeiou":
                vowel = vowel or key[i]
                result.append(independent_vowels[vowel])
                i += len(vowel)
                continue

            cluster = next((c for c in ("ch", "sh", "th", "dh", "ph", "bh", "kh", "gh") if key.startswith(c, i)), None)
            base = clusters.get(cluster) if cluster else consonants.get(key[i])
            i += len(cluster) if cluster else 1
            if not base:
                continue
            vowel = next((v for v in ("aa", "ee", "ii", "oo", "uu", "ai", "au") if key.startswith(v, i)), None)
            if vowel or (i < len(key) and key[i] in "aeiou"):
                vowel = vowel or key[i]
                result.append(base + vowels[vowel])
                i += len(vowel)
            else:
                result.append(base)
        return "".join(result) or word

    def text_chunks(self, text, max_chars=110):
        text = self.normalize_text(text)
        text = re.sub(r"\s+", " ", text).strip()
        parts = [part.strip() for part in re.split(r"([.?!।॥,;:]+)", text) if part.strip()]
        merged = []
        pending = ""
        for part in parts:
            if re.fullmatch(r"[.?!।॥,;:]+", part) and pending:
                pending += part
                continue
            if pending and len(pending) + len(part) + 1 > max_chars:
                merged.append(pending)
                pending = part
            else:
                pending = f"{pending} {part}".strip()
        if pending:
            merged.append(pending)

        chunks = []
        for part in merged or [text]:
            if len(part) <= max_chars:
                chunks.append(part)
                continue
            words = part.split()
            pending = ""
            for word in words:
                if pending and len(pending) + len(word) + 1 > max_chars:
                    chunks.append(pending)
                    pending = word
                else:
                    pending = f"{pending} {word}".strip()
            if pending:
                chunks.append(pending)
        return chunks or [text]

    def render_waveform(self, text):
        inputs = self.tokenizer(text, return_tensors="pt")
        with self.torch.no_grad():
            return self.model(**inputs).waveform.squeeze().detach().cpu().numpy()

    def synthesize(self, text, profile, out_path):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            wav_path = tmpdir / "mms.wav"
            shaped_wav = tmpdir / "mms_shaped.wav"
            waveforms = []
            pause = self.np.zeros(int(self.sample_rate * 0.08), dtype=self.np.float32)
            for chunk in self.text_chunks(text):
                try:
                    chunk_waveform = self.render_waveform(chunk)
                except Exception as exc:
                    raise RuntimeError(f"MMS Telugu TTS failed for text chunk: {chunk!r}") from exc
                waveforms.append(self.np.asarray(chunk_waveform, dtype=self.np.float32))
                waveforms.append(pause)
            waveform = self.np.concatenate(waveforms[:-1] if len(waveforms) > 1 else waveforms)
            peak = float(self.np.max(self.np.abs(waveform))) or 1.0
            pcm = self.np.clip(waveform / peak * 0.92, -1.0, 1.0)
            pcm16 = (pcm * 32767).astype("<i2")
            with wave.open(str(wav_path), "wb") as writer:
                writer.setnchannels(1)
                writer.setsampwidth(2)
                writer.setframerate(self.sample_rate)
                writer.writeframes(pcm16.tobytes())

            speed = max(0.80, min(1.25, float(profile.get("rate", 160)) / 160.0))
            semitone = {"adult_male": -1.6, "adult_female": 0.8, "child": 2.0}.get(profile.get("type"), 0.0)
            pitch_factor = math.pow(2.0, semitone / 12.0)
            shaped_rate = max(8000, int(round(self.sample_rate * pitch_factor)))
            subprocess.run(
                [
                    "ffmpeg",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    str(wav_path),
                    "-af",
                    f"asetrate={shaped_rate},aresample={self.sample_rate},atempo={speed:.4f}",
                    "-c:a",
                    "pcm_s16le",
                    str(shaped_wav),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            subprocess.run(
                ["ffmpeg", "-loglevel", "error", "-y", "-i", str(shaped_wav), "-c:a", "aac", str(out_path)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )


def build_synthesizer(engine):
    if engine == "mms":
        return MmsTeluguSynthesizer().synthesize
    return synthesize


def render_single_track(clips, output_dir, final_path, total_duration=None):
    if not clips:
        return

    if len(clips) > 120:
        render_single_track_concat(clips, output_dir, final_path, total_duration)
        return

    clip_paths = []
    rendered_end = clips[-1]["end"]
    for clip in clips:
        clip_path = Path(clip.get("path", output_dir / clip["url"]))
        clip_paths.append(clip_path)
        try:
            rendered_end = max(rendered_end, max(0.0, clip["start"]) + probe_duration(clip_path))
        except Exception:
            pass

    duration = max(total_duration or 0.0, rendered_end, 0.1)
    inputs = [
        "ffmpeg",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-t",
        f"{duration:.3f}",
        "-i",
        "anullsrc=r=16000:cl=mono",
    ]
    for clip_path in clip_paths:
        inputs.extend(["-i", str(clip_path)])

    filters = ["[0:a]volume=0[base]"]
    mix_inputs = ["[base]"]
    for index, clip in enumerate(clips, 1):
        delay_ms = max(0, int(round(max(0.0, clip["start"]) * 1000)))
        label = f"clip{index}"
        filters.append(f"[{index}:a]adelay={delay_ms}:all=1[{label}]")
        mix_inputs.append(f"[{label}]")
    filters.append(
        f"{''.join(mix_inputs)}amix=inputs={len(mix_inputs)}:duration=first:normalize=0,"
        "alimiter=limit=0.95[a]"
    )

    subprocess.run(
        inputs
        + [
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[a]",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(final_path),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def extract_background_track(media_path, background_path):
    base_cmd = ["ffmpeg", "-loglevel", "error", "-y", "-i", str(media_path), "-map", "0:a:0", "-vn"]
    subprocess.run(
        base_cmd + ["-ac", "2", "-c:a", "aac", "-b:a", "192k", str(background_path)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return True


def render_single_track_concat(clips, output_dir, final_path, total_duration=None):
    duration = total_duration if total_duration is not None else clips[-1]["end"]
    silence = output_dir / "_silence.wav"
    subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=16000:cl=mono",
            "-t",
            f"{max(duration, 0.1):.3f}",
            "-c:a",
            "pcm_s16le",
            str(silence),
        ],
        check=True,
    )

    list_file = output_dir / "_concat.txt"
    lines = []
    cursor = 0.0
    for clip in clips:
        clip_path = Path(clip.get("path", output_dir / clip["url"]))
        start = max(0.0, clip["start"])
        if start > cursor:
            gap = start - cursor
            lines.extend([f"file '{silence.as_posix()}'", "inpoint 0", f"outpoint {gap:.3f}"])
            cursor = start
        lines.append(f"file '{clip_path.as_posix()}'")
        try:
            cursor += probe_duration(clip_path)
        except Exception:
            cursor = max(cursor, clip["end"])

    if duration > cursor:
        gap = duration - cursor
        lines.extend([f"file '{silence.as_posix()}'", "inpoint 0", f"outpoint {gap:.3f}"])

    list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_file),
            "-ac",
            "2",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(final_path),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def mix_dubbed_track(background_path, voice_path, final_path, center_removed):
    filter_graph = (
        "[0:a]volume=0.65[bg];"
        "[1:a]volume=1.0[voice];"
        "[bg][voice]amix=inputs=2:duration=longest:dropout_transition=0,"
        "alimiter=limit=0.95[a]"
    )
    subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(background_path),
            "-i",
            str(voice_path),
            "-filter_complex",
            filter_graph,
            "-map",
            "[a]",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(final_path),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def render_dubbed_track(media_path, voice_path, output_dir):
    background_path = output_dir / "_background.m4a"
    final_path = output_dir / "telugu_dub_track.m4a"
    center_removed = extract_background_track(media_path, background_path)
    mix_dubbed_track(background_path, voice_path, final_path, center_removed)
    return final_path, center_removed


def load_speaker_outputs(speaker_map_path, speakers_path):
    if not speaker_map_path:
        return None, None
    line_map = json.loads(speaker_map_path.read_text(encoding="utf-8"))
    speakers = {}
    if speakers_path:
        speakers = json.loads(speakers_path.read_text(encoding="utf-8"))
    return {item["line_id"]: item for item in line_map}, speakers


def main(argv):
    input_path, output_dir, media_path, speaker_map_path, speakers_path, tts_engine, single_only, dry_run = parse_args(argv)
    output_dir.mkdir(parents=True, exist_ok=True)

    subtitles = parse_srt(input_path)
    if not subtitles:
        raise SystemExit(f"No subtitles found in {input_path}")

    synthesize_clip = build_synthesizer(tts_engine) if not dry_run else None
    mapped_lines, mapped_speakers = load_speaker_outputs(speaker_map_path, speakers_path)
    state = {
        "speakers": {},
        "profiles": {},
        "by_name": {},
        "by_type": {},
        "fallback": {},
        "fallback_index": 1,
        "bucket_counts": {},
        "voices": discover_telugu_voices(),
        "voice_index": 0,
    }
    if mapped_speakers:
        state["speakers"].update(mapped_speakers)

    media_temp = None
    media_reader = None
    clip_temp = None
    total_duration = None
    try:
        if media_path is not None:
            if not media_path.exists():
                raise SystemExit(f"Media file not found: {media_path}")
            try:
                total_duration = probe_duration(media_path)
            except Exception:
                total_duration = None
            media_temp = tempfile.TemporaryDirectory()
            wav_path = Path(media_temp.name) / "source.wav"
            if extract_audio(media_path, wav_path):
                media_reader = wave.open(str(wav_path), "rb")
            else:
                print(f"warning: could not extract audio from {media_path}; falling back to subtitle-only speaker heuristics", file=sys.stderr)

        clips = []
        clip_output_dir = output_dir
        if single_only and not dry_run:
            clip_temp = tempfile.TemporaryDirectory()
            clip_output_dir = Path(clip_temp.name)
        for index, subtitle in enumerate(subtitles, 1):
            clean_text = subtitle["text"]
            if mapped_lines and index in mapped_lines:
                mapped = mapped_lines[index]
                speaker_id = mapped["speaker_id"]
                if speaker_id not in state["speakers"]:
                    state["speakers"][speaker_id] = {
                        "label": speaker_id,
                        "type": mapped.get("detected_type", "unknown"),
                        "language_code": "te-IN",
                        "voice_name": mapped.get("voice_name") or state["voices"][0],
                        "rate": voice_rate(mapped.get("detected_type", "unknown"), 2.0, 0),
                        "dialect_hint": mapped.get("detected_type", "unknown"),
                    }
                else:
                    state["speakers"][speaker_id].setdefault("dialect_hint", state["speakers"][speaker_id].get("type", "unknown"))
                    state["speakers"][speaker_id].setdefault("language_code", "te-IN")
                features = {
                    "pitch_hz": mapped.get("pitch_hz"),
                    "speech_rate": round(len(re.findall(r"\w+", clean_text)) / max(0.1, subtitle["end"] - subtitle["start"]), 2),
                    "bucket": mapped.get("detected_type", state["speakers"][speaker_id].get("type", "unknown")),
                }
            elif media_reader is not None:
                features = analyze_subtitle(media_reader, clean_text, subtitle["start"], subtitle["end"])
                speaker_id = choose_speaker_from_features(state, features, clean_text)
            else:
                speaker_id, clean_text = choose_speaker_from_text(state, clean_text)
                features = {"pitch_hz": None, "speech_rate": round(len(re.findall(r"\w+", clean_text)) / max(0.1, subtitle["end"] - subtitle["start"]), 2), "bucket": fallback_type(clean_text) or "unknown"}

            filename = f"clip_{index:03d}.m4a"
            if not dry_run:
                synthesize_clip(clean_text, state["speakers"][speaker_id], clip_output_dir / filename)
            clips.append(
                {
                    "start": subtitle["start"],
                    "end": subtitle["end"],
                    "url": filename,
                    "path": str(clip_output_dir / filename),
                    "speaker_id": speaker_id,
                    "text": clean_text,
                    "pitch_hz": features["pitch_hz"],
                    "speech_rate": features["speech_rate"],
                    "bucket": features["bucket"],
                    "dialect_hint": state["speakers"][speaker_id].get("dialect_hint", features["bucket"]),
                }
            )
            print(f"{filename}: {speaker_id} ({features['bucket']})")

        sync_clips = [{k: v for k, v in clip.items() if k != "path"} for clip in clips]
        (output_dir / "sync.json").write_text(
            json.dumps({"version": 1, "audio_clips": sync_clips}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (output_dir / "speakers.json").write_text(
            json.dumps(state["speakers"], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        final_path = output_dir / "single-track.m4a"
        if not dry_run:
            render_single_track(clips, output_dir, final_path, total_duration)
            if media_path is not None:
                dubbed_path, center_removed = render_dubbed_track(media_path, final_path, output_dir)
        if single_only:
            print(f"Rendered {len(clips)} temporary clips")
        else:
            print(f"Wrote {len(clips)} clips to {output_dir}")
        if not dry_run:
            print(f"Wrote single track to {final_path}")
            if media_path is not None:
                print(f"Wrote dubbed track to {dubbed_path}")
    finally:
        if media_reader is not None:
            media_reader.close()
        if media_temp is not None:
            media_temp.cleanup()
        if clip_temp is not None:
            clip_temp.cleanup()


if __name__ == "__main__":
    main(sys.argv)

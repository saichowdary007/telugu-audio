#!/usr/bin/env python3
import json
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
        "[--speaker-map line_speaker_map.json] [--speakers speakers.json] [--dry-run]"
    )


def parse_args(argv):
    if len(argv) < 3:
        usage()
    media = None
    speaker_map = None
    speakers_path = None
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
        elif extra[i] == "--dry-run":
            dry_run = True
            i += 1
        else:
            usage()
    return Path(argv[1]), Path(argv[2]), media, speaker_map, speakers_path, dry_run


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


def render_single_track(clips, output_dir, final_path, total_duration=None):
    if not clips:
        return

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
            f"anullsrc=r=16000:cl=mono",
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
        start = max(0.0, clip["start"])
        if start > cursor:
            gap = start - cursor
            lines.extend(
                [
                    f"file '{silence.as_posix()}'",
                    "inpoint 0",
                    f"outpoint {gap:.3f}",
                ]
            )
        lines.append(f"file '{(output_dir / clip['url']).as_posix()}'")
        cursor = max(cursor, clip["end"])

    if duration > cursor:
        gap = duration - cursor
        lines.extend(
            [
                f"file '{silence.as_posix()}'",
                "inpoint 0",
                f"outpoint {gap:.3f}",
            ]
        )

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
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(final_path),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def load_speaker_outputs(speaker_map_path, speakers_path):
    if not speaker_map_path:
        return None, None
    line_map = json.loads(speaker_map_path.read_text(encoding="utf-8"))
    speakers = {}
    if speakers_path:
        speakers = json.loads(speakers_path.read_text(encoding="utf-8"))
    return {item["line_id"]: item for item in line_map}, speakers


def main(argv):
    input_path, output_dir, media_path, speaker_map_path, speakers_path, dry_run = parse_args(argv)
    output_dir.mkdir(parents=True, exist_ok=True)

    subtitles = parse_srt(input_path)
    if not subtitles:
        raise SystemExit(f"No subtitles found in {input_path}")

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
        for index, subtitle in enumerate(subtitles, 1):
            clean_text = subtitle["text"]
            if mapped_lines and index in mapped_lines:
                mapped = mapped_lines[index]
                speaker_id = mapped["speaker_id"]
                clean_text = mapped.get("text", clean_text)
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
                synthesize(clean_text, state["speakers"][speaker_id], output_dir / filename)
            clips.append(
                {
                    "start": subtitle["start"],
                    "end": subtitle["end"],
                    "url": filename,
                    "speaker_id": speaker_id,
                    "text": clean_text,
                    "pitch_hz": features["pitch_hz"],
                    "speech_rate": features["speech_rate"],
                    "bucket": features["bucket"],
                    "dialect_hint": state["speakers"][speaker_id].get("dialect_hint", features["bucket"]),
                }
            )
            print(f"{filename}: {speaker_id} ({features['bucket']})")

        (output_dir / "sync.json").write_text(
            json.dumps({"version": 1, "audio_clips": clips}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (output_dir / "speakers.json").write_text(
            json.dumps(state["speakers"], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        final_path = output_dir / "single-track.m4a"
        if not dry_run:
            render_single_track(clips, output_dir, final_path, total_duration)
        print(f"Wrote {len(clips)} clips to {output_dir}")
        if not dry_run:
            print(f"Wrote single track to {final_path}")
    finally:
        if media_reader is not None:
            media_reader.close()
        if media_temp is not None:
            media_temp.cleanup()


if __name__ == "__main__":
    main(sys.argv)

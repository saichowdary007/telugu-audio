#!/usr/bin/env python3
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path


TIME_RE = re.compile(
    r"(\d{2}):(\d{2}):(\d{2}),(\d{3})\s+-->\s+(\d{2}):(\d{2}):(\d{2}),(\d{3})"
)
NAME_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9 _-]{1,30})\s*:\s*(.+)$", re.S)
VOICE_RE = re.compile(r"^(.*?)\s+([a-z]{2}_[A-Z]{2}|[a-z]{3}_[0-9]{3})\s+#", re.M)

MALE_WORDS = {"man", "men", "male", "boy", "father", "dad", "son", "brother", "husband"}
FEMALE_WORDS = {"woman", "women", "female", "girl", "mother", "mom", "daughter", "sister", "wife"}
CHILD_WORDS = {"child", "kid", "baby", "children"}

RATE_BY_TYPE = {
    "adult_male": [148, 158, 138],
    "adult_female": [174, 184, 164],
    "child": [205, 220],
    "elderly": [132, 142],
}


def seconds(parts):
    h, m, s, ms = map(int, parts)
    return h * 3600 + m * 60 + s + ms / 1000


def parse_srt(path):
    raw = Path(path).read_text(encoding="utf-8-sig").replace("\r\n", "\n").strip()
    if not raw:
        return []

    items = []
    for block in re.split(r"\n\s*\n", raw):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if len(lines) < 2:
            continue

        time_line = lines[1] if lines[0].isdigit() and len(lines) > 1 else lines[0]
        text_lines = lines[2:] if lines[0].isdigit() else lines[1:]
        match = TIME_RE.search(time_line)
        if not match:
            continue

        text = " ".join(text_lines)
        text = re.sub(r"<[^>]+>", "", text).strip()
        if text:
            items.append(
                {
                    "start": seconds(match.groups()[:4]),
                    "end": seconds(match.groups()[4:]),
                    "text": text,
                }
            )
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
    voices = []
    for line in proc.stdout.splitlines():
        match = VOICE_RE.match(line)
        if match and match.group(2) == "te_IN":
            voices.append(match.group(1).strip())
    return voices or ["Geeta"]


def speaker_for(text, state):
    named = NAME_RE.match(text)
    if named:
        key = normalize_name(named.group(1))
        clean_text = named.group(2).strip()
        speaker_type = guess_type(named.group(1)) or guess_type(clean_text)
        if key not in state["by_name"]:
            state["by_name"][key] = new_speaker(state, speaker_type)
        return state["by_name"][key], clean_text

    speaker_type = guess_type(text)
    if speaker_type:
        typed = state["by_type"].setdefault(speaker_type, [])
        if not typed:
            typed.append(new_speaker(state, speaker_type))
        return typed[0], text

    state["fallback_index"] = 1 - state["fallback_index"]
    speaker_type = "adult_male" if state["fallback_index"] == 0 else "adult_female"
    speaker_id = state["fallback"].get(speaker_type)
    if not speaker_id:
        speaker_id = new_speaker(state, speaker_type)
        state["fallback"][speaker_type] = speaker_id
    return speaker_id, text


def new_speaker(state, speaker_type):
    speaker_type = speaker_type or "adult_male"
    speaker_id = f"speaker_{len(state['speakers']) + 1:03d}"
    voice_name = state["voices"][state["voice_index"] % len(state["voices"])]
    state["voice_index"] += 1
    type_index = state["type_counts"].get(speaker_type, 0)
    state["type_counts"][speaker_type] = type_index + 1
    rate = RATE_BY_TYPE.get(speaker_type, [160])[type_index % len(RATE_BY_TYPE.get(speaker_type, [160]))]
    state["speakers"][speaker_id] = {
        "label": speaker_id,
        "type": speaker_type,
        "language_code": "te-IN",
        "voice_name": voice_name,
        "rate": rate,
    }
    return speaker_id


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


def main(argv):
    if len(argv) != 3:
        raise SystemExit("Usage: python3 pipeline.py input.srt output_dir")

    input_path = Path(argv[1])
    output_dir = Path(argv[2])
    output_dir.mkdir(parents=True, exist_ok=True)

    subtitles = parse_srt(input_path)
    if not subtitles:
        raise SystemExit(f"No subtitles found in {input_path}")

    state = {
        "speakers": {},
        "by_name": {},
        "by_type": {},
        "fallback": {},
        "fallback_index": 1,
        "type_counts": {},
        "voices": discover_telugu_voices(),
        "voice_index": 0,
    }

    clips = []
    for index, subtitle in enumerate(subtitles, 1):
        speaker_id, clean_text = speaker_for(subtitle["text"], state)
        filename = f"clip_{index:03d}.m4a"
        synthesize(clean_text, state["speakers"][speaker_id], output_dir / filename)
        clips.append(
            {
                "start": subtitle["start"],
                "end": subtitle["end"],
                "url": filename,
                "speaker_id": speaker_id,
                "text": clean_text,
            }
        )
        print(f"{filename}: {speaker_id}")

    (output_dir / "sync.json").write_text(
        json.dumps({"version": 1, "audio_clips": clips}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output_dir / "speakers.json").write_text(
        json.dumps(state["speakers"], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Wrote {len(clips)} clips to {output_dir}")


if __name__ == "__main__":
    main(sys.argv)

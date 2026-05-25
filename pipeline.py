#!/usr/bin/env python3
import json
import os
import re
import sys
import wave
from pathlib import Path

try:
    from google.cloud import texttospeech
except ImportError:
    texttospeech = None

try:
    from google import genai
    from google.genai import types as genai_types
except ImportError:
    genai = None
    genai_types = None


TIME_RE = re.compile(
    r"(\d{2}):(\d{2}):(\d{2}),(\d{3})\s+-->\s+(\d{2}):(\d{2}):(\d{2}),(\d{3})"
)
NAME_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9 _-]{1,30})\s*:\s*(.+)$", re.S)

MALE_WORDS = {"man", "men", "male", "boy", "father", "dad", "son", "brother", "husband"}
FEMALE_WORDS = {"woman", "women", "female", "girl", "mother", "mom", "daughter", "sister", "wife"}
CHILD_WORDS = {"child", "kid", "baby", "children"}

VOICE_SETTINGS = [
    ("adult_male", "te-IN-Standard-B", -3.0, 0.96),
    ("adult_female", "te-IN-Standard-A", 2.0, 1.03),
    ("adult_male", "te-IN-Standard-D", -1.0, 1.08),
    ("adult_female", "te-IN-Standard-C", 4.0, 0.94),
    ("child", "te-IN-Standard-A", 6.0, 1.12),
    ("elderly", "te-IN-Standard-B", -5.0, 0.86),
]

GEMINI_VOICES = {
    "adult_male": "Kore",
    "adult_female": "Aoede",
    "child": "Puck",
    "elderly": "Charon",
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


def speaker_for(text, state):
    named = NAME_RE.match(text)
    if named:
        key = normalize_name(named.group(1))
        clean_text = named.group(2).strip()
        speaker_type = guess_type(named.group(1)) or guess_type(clean_text)
        return state["by_name"].setdefault(key, new_speaker(state, speaker_type)), clean_text

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
    speaker_id = f"speaker_{len(state['speakers']) + 1:03d}"
    speaker_type = speaker_type or "adult_male"
    type_index = state["type_counts"].get(speaker_type, 0)
    state["type_counts"][speaker_type] = type_index + 1
    profile = voice_profile(speaker_id, speaker_type, type_index)
    state["speakers"][speaker_id] = profile
    return speaker_id


def voice_profile(speaker_id, speaker_type, index):
    default = VOICE_SETTINGS[index % len(VOICE_SETTINGS)]
    if speaker_type:
        matching = [item for item in VOICE_SETTINGS if item[0] == speaker_type]
        setting = matching[index % len(matching)] if matching else default
    else:
        setting = default

    kind, voice_name, pitch, speaking_rate = setting
    return {
        "label": speaker_id,
        "type": speaker_type or kind,
        "language_code": "te-IN",
        "voice_name": voice_name,
        "pitch": pitch,
        "speaking_rate": speaking_rate,
    }


def synthesize(client, text, profile, mp3_path):
    provider = os.environ.get("TTS_PROVIDER", "cloud").strip().lower()
    if provider == "gemini":
        synthesize_gemini(client, text, profile, mp3_path)
        return

    voice = texttospeech.VoiceSelectionParams(
        language_code=profile["language_code"],
        name=profile["voice_name"],
    )
    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.MP3,
        pitch=profile["pitch"],
        speaking_rate=profile["speaking_rate"],
    )
    response = client.synthesize_speech(
        input=texttospeech.SynthesisInput(text=text),
        voice=voice,
        audio_config=audio_config,
    )
    mp3_path.write_bytes(response.audio_content)


def synthesize_gemini(client, text, profile, wav_path):
    voice_name = GEMINI_VOICES.get(profile["type"], "Kore")
    response = client.models.generate_content(
        model=os.environ.get("GEMINI_TTS_MODEL", "gemini-3.1-flash-tts-preview"),
        contents=text,
        config=genai_types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=genai_types.SpeechConfig(
                voice_config=genai_types.VoiceConfig(
                    prebuilt_voice_config=genai_types.PrebuiltVoiceConfig(
                        voice_name=voice_name,
                    )
                )
            ),
        ),
    )
    pcm = response.candidates[0].content.parts[0].inline_data.data
    with wave.open(str(wav_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(24000)
        wf.writeframes(pcm)


def require_tts(provider):
    if provider == "gemini":
        if genai is None or genai_types is None:
            raise SystemExit("Missing dependency. Run: python3 -m pip install -r requirements.txt")
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise SystemExit("Missing GEMINI_API_KEY for Gemini TTS.")
        return genai.Client(api_key=api_key)

    if texttospeech is None:
        raise SystemExit("Missing dependency. Run: python3 -m pip install -r requirements.txt")
    if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        raise SystemExit("Missing GOOGLE_APPLICATION_CREDENTIALS pointing to your Google Cloud JSON key.")
    return texttospeech.TextToSpeechClient()


def main(argv):
    if len(argv) != 3:
        raise SystemExit("Usage: python3 pipeline.py input.srt output_dir")

    input_path = Path(argv[1])
    output_dir = Path(argv[2])
    output_dir.mkdir(parents=True, exist_ok=True)
    provider = os.environ.get("TTS_PROVIDER", "cloud").strip().lower()
    if provider not in {"cloud", "gemini"}:
        raise SystemExit("TTS_PROVIDER must be 'cloud' or 'gemini'")

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
    }
    clips = []
    client = require_tts(provider)

    for index, subtitle in enumerate(subtitles, 1):
        speaker_id, clean_text = speaker_for(subtitle["text"], state)
        filename = f"clip_{index:03d}.{'wav' if provider == 'gemini' else 'mp3'}"
        synthesize(client, clean_text, state["speakers"][speaker_id], output_dir / filename)
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

#!/usr/bin/env python3
import json
import math
import re
import struct
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np

from pipeline import discover_telugu_voices, parse_srt, read_segment, voice_rate


def usage():
    raise SystemExit(
        "Usage: python3 speaker_cluster.py input.srt source_audio_or_video output_dir "
        "[--max-speakers N] [--offset SECONDS]"
    )


def parse_args(argv):
    if len(argv) < 4:
        usage()
    srt_path = Path(argv[1])
    media_path = Path(argv[2])
    output_dir = Path(argv[3])
    max_speakers = None
    offset = 0.0
    extra = argv[4:]
    i = 0
    while i < len(extra):
        if extra[i] == "--max-speakers" and i + 1 < len(extra):
            max_speakers = int(extra[i + 1])
            i += 2
        elif extra[i] == "--offset" and i + 1 < len(extra):
            offset = float(extra[i + 1])
            i += 2
        else:
            usage()
    return srt_path, media_path, output_dir, max_speakers, offset


def run_ffmpeg(args):
    return subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def extract_dialogue_audio(media_path, wav_path):
    center_cmd = [
        "ffmpeg",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(media_path),
        "-af",
        "pan=mono|c0=FC",
        "-ar",
        "16000",
        "-vn",
        str(wav_path),
    ]
    if run_ffmpeg(center_cmd).returncode == 0:
        return "center"

    mono_cmd = [
        "ffmpeg",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(media_path),
        "-ac",
        "1",
        "-ar",
        "16000",
        "-vn",
        str(wav_path),
    ]
    if run_ffmpeg(mono_cmd).returncode == 0:
        return "mono"
    raise SystemExit(f"Could not extract audio from {media_path}")


def rms(samples):
    if not samples:
        return 0.0
    arr = np.asarray(samples, dtype=np.float32)
    return float(np.sqrt(np.mean(arr * arr)))


def spectral_embedding(samples, sr):
    if len(samples) < int(sr * 0.35) or rms(samples) < 90:
        return None

    arr = np.asarray(samples, dtype=np.float32)
    arr = arr - np.mean(arr)
    peak = np.max(np.abs(arr)) or 1.0
    arr = arr / peak

    n = min(len(arr), sr * 4)
    arr = arr[:n] * np.hanning(n)
    spectrum = np.abs(np.fft.rfft(arr))
    freqs = np.fft.rfftfreq(n, 1 / sr)
    total = np.sum(spectrum) + 1e-9

    zcr = np.mean(np.abs(np.diff(np.signbit(arr))))
    centroid = float(np.sum(freqs * spectrum) / total)
    cdf = np.cumsum(spectrum) / total
    rolloff = float(freqs[min(len(freqs) - 1, int(np.searchsorted(cdf, 0.85)))])

    bands = []
    edges = np.geomspace(80, 4000, 13)
    for lo, hi in zip(edges[:-1], edges[1:]):
        mask = (freqs >= lo) & (freqs < hi)
        bands.append(float(np.log(np.mean(spectrum[mask]) + 1e-6)) if np.any(mask) else -14.0)

    vec = np.asarray([zcr, centroid / 4000, rolloff / 4000, *bands], dtype=np.float32)
    vec = (vec - np.mean(vec)) / (np.std(vec) + 1e-6)
    norm = np.linalg.norm(vec)
    return vec / norm if norm else vec


def fast_pitch(samples, sr):
    if len(samples) < sr // 5:
        return None

    arr = np.asarray(samples, dtype=np.float32)
    arr = arr[: min(len(arr), int(sr * 1.6))]
    arr = arr - np.mean(arr)
    if np.sqrt(np.mean(arr * arr)) < 90:
        return None

    frame_size = max(320, int(sr * 0.04))
    hop = max(320, int(sr * 0.04))
    min_lag = max(1, int(sr / 400))
    max_lag = max(min_lag + 1, int(sr / 60))
    pitches = []

    for offset in range(0, len(arr) - frame_size + 1, hop):
        frame = arr[offset : offset + frame_size]
        frame = frame - np.mean(frame)
        energy = float(np.dot(frame, frame))
        if energy < 1e6:
            continue
        scores = [float(np.dot(frame[:-lag], frame[lag:]) / energy) for lag in range(min_lag, max_lag + 1)]
        best = int(np.argmax(scores))
        if scores[best] > 0.25:
            pitches.append(sr / (min_lag + best))

    return round(float(np.median(pitches)), 1) if pitches else None


def speaker_type(pitch_hz):
    if pitch_hz is None:
        return "unknown"
    if pitch_hz < 120:
        return "adult_male"
    if pitch_hz < 220:
        return "adult_female"
    return "child"


def cosine_distance(a, b):
    return float(1.0 - np.clip(np.dot(a, b), -1.0, 1.0))


def build_segments(subtitles, reader, offset):
    segments = []
    for line_id, sub in enumerate(subtitles, 1):
        start = max(0.0, sub["start"] + offset)
        end = max(start, sub["end"] + offset)
        samples, sr = read_segment(reader, start, end)
        pitch = fast_pitch(samples, sr)
        embedding = spectral_embedding(samples, sr)
        text = sub["text"]
        multi = bool(re.search(r"(^|\s)-\s*\w", text)) or text.count("- ") > 1
        duration = sub["end"] - sub["start"]
        segments.append(
            {
                "line_id": line_id,
                "start": sub["start"],
                "end": sub["end"],
                "text": text,
                "pitch_hz": pitch,
                "type": speaker_type(pitch),
                "embedding": embedding,
                "rms": rms(samples),
                "duration": duration,
                "multi_speaker_hint": multi,
            }
        )
    return segments


def cluster_segments(segments, max_speakers=None):
    clusters = []
    for seg in segments:
        emb = seg["embedding"]
        if emb is None:
            continue

        best = None
        best_dist = 999.0
        for cluster in clusters:
            dist = cosine_distance(emb, cluster["centroid"])
            if seg["type"] != "unknown" and cluster["type"] != "unknown" and seg["type"] != cluster["type"]:
                dist += 0.18
            if dist < best_dist:
                best = cluster
                best_dist = dist

        threshold = 0.34 if len(clusters) < 8 else 0.28
        if best is None or best_dist > threshold:
            clusters.append({"segments": [seg], "centroid": emb.copy(), "type": seg["type"]})
        else:
            best["segments"].append(seg)
            embeddings = [s["embedding"] for s in best["segments"] if s["embedding"] is not None]
            best["centroid"] = np.mean(embeddings, axis=0)
            best["centroid"] = best["centroid"] / (np.linalg.norm(best["centroid"]) + 1e-9)
            best["type"] = dominant_type(best["segments"])

    clusters = merge_close_clusters(clusters)
    clusters = [c for c in clusters if len(c["segments"]) >= 2 or avg_rms(c["segments"]) > 300]
    clusters.sort(key=lambda c: (-len(c["segments"]), c["segments"][0]["line_id"]))

    if max_speakers and len(clusters) > max_speakers:
        clusters = collapse_to_max(clusters, max_speakers)

    return clusters


def dominant_type(segments):
    counts = {}
    for seg in segments:
        counts[seg["type"]] = counts.get(seg["type"], 0) + 1
    return max(counts, key=counts.get) if counts else "unknown"


def avg_rms(segments):
    return sum(s["rms"] for s in segments) / max(1, len(segments))


def merge_close_clusters(clusters):
    changed = True
    while changed:
        changed = False
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                dist = cosine_distance(clusters[i]["centroid"], clusters[j]["centroid"])
                same_type = clusters[i]["type"] == clusters[j]["type"] or "unknown" in {clusters[i]["type"], clusters[j]["type"]}
                if same_type and dist < 0.22:
                    clusters[i]["segments"].extend(clusters[j]["segments"])
                    embeddings = [s["embedding"] for s in clusters[i]["segments"] if s["embedding"] is not None]
                    clusters[i]["centroid"] = np.mean(embeddings, axis=0)
                    clusters[i]["centroid"] = clusters[i]["centroid"] / (np.linalg.norm(clusters[i]["centroid"]) + 1e-9)
                    clusters[i]["type"] = dominant_type(clusters[i]["segments"])
                    del clusters[j]
                    changed = True
                    break
            if changed:
                break
    return clusters


def collapse_to_max(clusters, max_speakers):
    while len(clusters) > max_speakers:
        best_pair = None
        best_dist = 999.0
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                dist = cosine_distance(clusters[i]["centroid"], clusters[j]["centroid"])
                if dist < best_dist:
                    best_pair = (i, j)
                    best_dist = dist
        i, j = best_pair
        clusters[i]["segments"].extend(clusters[j]["segments"])
        embeddings = [s["embedding"] for s in clusters[i]["segments"] if s["embedding"] is not None]
        clusters[i]["centroid"] = np.mean(embeddings, axis=0)
        clusters[i]["centroid"] = clusters[i]["centroid"] / (np.linalg.norm(clusters[i]["centroid"]) + 1e-9)
        clusters[i]["type"] = dominant_type(clusters[i]["segments"])
        del clusters[j]
    return clusters


def assign_short_segments(segments, clusters):
    by_line = {}
    for idx, cluster in enumerate(clusters, 1):
        speaker_id = f"speaker_{idx:03d}"
        for seg in cluster["segments"]:
            by_line[seg["line_id"]] = speaker_id

    last_speaker = None
    for seg in segments:
        if seg["line_id"] in by_line:
            last_speaker = by_line[seg["line_id"]]
            continue
        nearest = nearest_embedding_speaker(seg, clusters)
        by_line[seg["line_id"]] = nearest or last_speaker or "speaker_001"
        last_speaker = by_line[seg["line_id"]]
    return by_line


def nearest_embedding_speaker(seg, clusters):
    emb = seg["embedding"]
    if emb is None:
        return None
    best_id = None
    best_dist = 999.0
    for idx, cluster in enumerate(clusters, 1):
        dist = cosine_distance(emb, cluster["centroid"])
        if dist < best_dist:
            best_id = f"speaker_{idx:03d}"
            best_dist = dist
    return best_id if best_dist < 0.45 else None


def confidence_for(cluster, clusters):
    if not cluster["segments"]:
        return 0.0
    intra = sum(cosine_distance(s["embedding"], cluster["centroid"]) for s in cluster["segments"] if s["embedding"] is not None)
    intra /= max(1, len(cluster["segments"]))
    nearest = min(
        (cosine_distance(cluster["centroid"], other["centroid"]) for other in clusters if other is not cluster),
        default=0.7,
    )
    return round(max(0.2, min(0.96, nearest / (nearest + intra + 1e-6))), 2)


def build_outputs(segments, clusters):
    voices = discover_telugu_voices()
    line_to_speaker = assign_short_segments(segments, clusters)
    speakers = {}

    for idx, cluster in enumerate(clusters, 1):
        speaker_id = f"speaker_{idx:03d}"
        pitches = [s["pitch_hz"] for s in cluster["segments"] if s["pitch_hz"] is not None]
        avg_pitch = round(sum(pitches) / len(pitches), 1) if pitches else None
        detected_type = speaker_type(avg_pitch)
        if detected_type == "unknown":
            detected_type = cluster["type"]
        speech_rate = avg((word_count(s["text"]) / max(0.1, s["end"] - s["start"]) for s in cluster["segments"]))
        speakers[speaker_id] = {
            "type": detected_type,
            "detected_type": detected_type,
            "avg_pitch_hz": avg_pitch,
            "segments": sorted(s["line_id"] for s in cluster["segments"]),
            "voice_name": voices[(idx - 1) % len(voices)],
            "rate": voice_rate(detected_type, speech_rate, idx - 1),
            "confidence": confidence_for(cluster, clusters),
        }

    line_map = []
    for seg in segments:
        speaker_id = line_to_speaker.get(seg["line_id"], "speaker_001")
        speaker = speakers.get(speaker_id, speakers.get("speaker_001", {}))
        line_map.append(
            {
                "line_id": seg["line_id"],
                "start": seg["start"],
                "end": seg["end"],
                "speaker_id": speaker_id,
                "text": seg["text"],
                "voice_name": speaker.get("voice_name"),
                "pitch_hz": seg["pitch_hz"],
                "detected_type": speaker.get("detected_type", seg["type"]),
                "confidence": speaker.get("confidence", 0.2),
                "multi_speaker_hint": seg["multi_speaker_hint"],
            }
        )
    return speakers, line_map


def avg(values):
    vals = list(values)
    return sum(vals) / max(1, len(vals))


def word_count(text):
    return len(re.findall(r"\w+", text))


def main(argv):
    srt_path, media_path, output_dir, max_speakers, offset = parse_args(argv)
    output_dir.mkdir(parents=True, exist_ok=True)
    subtitles = parse_srt(srt_path)
    if not subtitles:
        raise SystemExit(f"No subtitles found in {srt_path}")

    with tempfile.TemporaryDirectory() as tmpdir:
        wav_path = Path(tmpdir) / "dialogue.wav"
        mode = extract_dialogue_audio(media_path, wav_path)
        with wave.open(str(wav_path), "rb") as reader:
            segments = build_segments(subtitles, reader, offset)

    clusters = cluster_segments(segments, max_speakers)
    if not clusters:
        raise SystemExit("No usable speech clusters found. Check media/SRT alignment or audio quality.")

    speakers, line_map = build_outputs(segments, clusters)
    (output_dir / "speakers.json").write_text(json.dumps(speakers, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "line_speaker_map.json").write_text(json.dumps(line_map, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Audio extraction: {mode}")
    print(f"Subtitles: {len(subtitles)}")
    print(f"Speakers: {len(speakers)}")
    print(f"Wrote {output_dir / 'speakers.json'}")
    print(f"Wrote {output_dir / 'line_speaker_map.json'}")


if __name__ == "__main__":
    main(sys.argv)

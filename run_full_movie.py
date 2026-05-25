#!/usr/bin/env python3
import argparse
import json
import subprocess
import sys
from pathlib import Path

from pipeline import parse_srt, probe_duration

ROOT = Path(__file__).resolve().parent


def parse_args():
    parser = argparse.ArgumentParser(description="Run the full Telugu dub pipeline end to end.")
    parser.add_argument("--media", required=True, help="Original movie or audio file")
    parser.add_argument("--source-srt", required=True, help="SRT matching the original audio for speaker clustering")
    parser.add_argument("--telugu-srt", required=True, help="Translated Telugu SRT used for TTS")
    parser.add_argument("--out", required=True, help="Output directory")
    parser.add_argument("--max-speakers", type=int, default=24)
    parser.add_argument("--offset", type=float, default=0.0)
    parser.add_argument("--embedding-model", default="speechbrain", choices=["speechbrain"])
    parser.add_argument("--tts-engine", default="mms", choices=["mms"])
    parser.add_argument("--keep-clips", action="store_true", help="Keep per-line generated clips")
    return parser.parse_args()


def require_file(path, label):
    path = Path(path)
    if not path.exists():
        raise SystemExit(f"{label} not found: {path}")
    if path.stat().st_size == 0:
        raise SystemExit(f"{label} is empty: {path}")
    return path


def run(cmd):
    print("+ " + " ".join(str(part) for part in cmd), flush=True)
    subprocess.run([str(part) for part in cmd], check=True, cwd=ROOT)


def media_audio_info(path):
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=index,codec_type,codec_name,channels,channel_layout:format=duration",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(proc.stdout)
    audio_streams = [stream for stream in data.get("streams", []) if stream.get("codec_type") == "audio"]
    if not audio_streams:
        raise SystemExit(f"No audio stream found in {path}")
    return audio_streams[0]


def main():
    args = parse_args()
    media = require_file(args.media, "media")
    source_srt = require_file(args.source_srt, "source SRT")
    telugu_srt = require_file(args.telugu_srt, "Telugu SRT")
    out = Path(args.out)
    cluster_dir = out / "speaker-clusters"
    dub_dir = out / "dub"
    out.mkdir(parents=True, exist_ok=True)

    source_lines = parse_srt(source_srt)
    telugu_lines = parse_srt(telugu_srt)
    if not source_lines:
        raise SystemExit(f"No subtitle lines found in {source_srt}")
    if not telugu_lines:
        raise SystemExit(f"No subtitle lines found in {telugu_srt}")
    if len(source_lines) != len(telugu_lines):
        raise SystemExit(
            f"SRT line count mismatch: source={len(source_lines)} telugu={len(telugu_lines)}. "
            "Speaker maps are line-based, so align the SRTs first."
        )

    media_duration = probe_duration(media)
    audio_info = media_audio_info(media)
    print(
        "Audio stream: "
        f"{audio_info.get('codec_name')} {audio_info.get('channels')}ch {audio_info.get('channel_layout')}",
        flush=True,
    )
    subtitle_end = max(line["end"] for line in telugu_lines)
    if subtitle_end < media_duration * 0.70:
        raise SystemExit(
            f"Telugu SRT ends at {subtitle_end:.1f}s but media is {media_duration:.1f}s. "
            "This looks like a sample SRT, not the full movie."
        )

    cluster_cmd = [
        sys.executable,
        ROOT / "speaker_cluster.py",
        source_srt,
        media,
        cluster_dir,
        "--offset",
        args.offset,
        "--embedding-model",
        args.embedding_model,
    ]
    cluster_cmd.extend(["--max-speakers", args.max_speakers])
    run(cluster_cmd)

    pipeline_cmd = [
        sys.executable,
        ROOT / "pipeline.py",
        telugu_srt,
        dub_dir,
        "--media",
        media,
        "--speaker-map",
        cluster_dir / "line_speaker_map.json",
        "--speakers",
        cluster_dir / "speakers.json",
        "--tts-engine",
        args.tts_engine,
    ]
    if not args.keep_clips:
        pipeline_cmd.append("--single-only")
    run(pipeline_cmd)

    final_path = dub_dir / "telugu_dub_track.m4a"
    final_duration = probe_duration(final_path)
    if abs(final_duration - media_duration) > 2.0:
        raise SystemExit(
            f"Dub duration check failed: media={media_duration:.1f}s dub={final_duration:.1f}s. "
            "Inspect subtitle timing and ffmpeg mix logs."
        )

    print(f"Final dubbed track: {final_path}")
    print(f"Duration: {final_duration:.1f}s")


if __name__ == "__main__":
    main()

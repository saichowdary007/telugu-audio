# Telugu Audio

Tools for generating Telugu audio tracks and syncing them with browser video playback.

## macOS Local Pipeline

Generate local Telugu audio clips from an `.srt` file with built-in macOS speech:

```bash
python3 pipeline.py input.srt out
```

Output:

```text
out/
  clip_001.m4a
  clip_002.m4a
  single-track.m4a
  sync.json
  speakers.json
```

The pipeline uses macOS `say` and `afconvert`, so there is no external TTS
service, no API key, and no Python dependency install step.

Install the one analysis dependency if it is not already present:

```bash
python3 -m pip install -r requirements.txt
```

Voice handling is simple by design:
- It uses any Telugu voices installed on your Mac.
- It assigns different speakers different local voice IDs when available.
- If only one Telugu voice exists, it still separates characters using different
  speaking rates so the audio does not sound identical.
- Named subtitle lines like `Ravi: Hello` keep the same speaker profile across
  the file. Unnamed dialogue alternates automatically.
- The pipeline also stitches a single `single-track.m4a` that follows the
  subtitle timeline, so you can listen to one file instead of many clips.

### Automatic Speaker Detection

For stable speaker IDs, run the clustering step first. It extracts the center
channel when available, builds a small acoustic embedding for each subtitle
window, clusters similar voices, and writes `speakers.json` plus
`line_speaker_map.json`:

```bash
python3 speaker_cluster.py input.srt movie_or_audio.mkv cluster-out
```

Then generate Telugu audio with the stable speaker map:

```bash
python3 pipeline.py input.srt out --speaker-map cluster-out/line_speaker_map.json --speakers cluster-out/speakers.json
```

This local clustering is lightweight. It uses acoustic embeddings plus pitch,
not actor names. Pitch is only used to choose adult male, adult female, or child
voice type.

You can still pass `--media` directly to `pipeline.py`, but that path is the
older pitch-only shortcut:

```bash
python3 pipeline.py input.srt out --media movie.mp4
```

If you only want to inspect the subtitle file and speaker assignment without
generating every audio clip, add `--dry-run`:

```bash
python3 pipeline.py input.srt out --dry-run
```

## Auto Sync Extension

The extension does not need manual offset tuning. It mutes the webpage video and
plays the generated clip that matches `video.currentTime`.

1. Serve the output folder:

```bash
python3 -m http.server 8000 -d out
```

2. Open Chrome, load the unpacked extension from `extension/`, and set the
sync package URL to:

```text
http://localhost:8000/
```

3. Play a video on a page with a `<video>` element. The extension will load
`sync.json`, mute the original track, and stay aligned on play, pause, and seek.

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

If you also have the source movie or audio file, you can add `--media` and the
script will use FFmpeg to extract the soundtrack, estimate pitch per subtitle
interval, and cluster recurring speakers automatically:

```bash
python3 pipeline.py input.srt out --media movie.mp4
```

This is pitch-based voice typing:
- low pitch usually maps to a male voice bucket
- mid pitch maps to a female voice bucket
- high pitch maps to a child-like bucket

That is useful for automatic voice selection, but it is not true linguistic
dialect detection. Pitch alone cannot reliably identify a spoken dialect.

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

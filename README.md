# Telugu Audio

Tools for generating Telugu audio tracks and syncing them with browser video playback.

## macOS Local Pipeline

Generate local Telugu audio clips from an `.srt` file with built-in macOS speech:

```bash
python3 pipeline.py input.srt out
```

For the real full-movie pipeline with open-source models, use Python 3.12 and
install the model dependencies:

```bash
/opt/homebrew/bin/python3.12 -m venv .venv
.venv/bin/pip install -r requirements-real-model.txt
```

Then run the end-to-end command with the original media or audio, original
matching SRT, and Telugu translated SRT:

```bash
.venv/bin/python run_full_movie.py \
  --media movie.mkv \
  --source-srt original.srt \
  --telugu-srt telugu.srt \
  --out full-dub \
  --embedding-model speechbrain \
  --tts-engine mms
```

That produces:

```text
full-dub/
  speaker-clusters/
    speakers.json
    line_speaker_map.json
  dub/
    single-track.m4a
    telugu_dub_track.m4a
    sync.json
    speakers.json
```

`telugu_dub_track.m4a` is the file to use for the movie: it mixes the original
background track with the generated Telugu speech timeline. The command refuses
to run when the Telugu SRT is clearly only a short sample, because the final dub
must match the full media duration. Mono inputs are converted to stereo so the
final track plays through both left and right speakers.

If you only want the final Telugu audio track and do not want the individual
subtitle clips in the output folder, run:

```bash
python3 pipeline.py input.srt out --single-only
```

Output:

```text
out/
  clip_001.m4a
  clip_002.m4a
  single-track.m4a
  telugu_dub_track.m4a   # only when --media is provided
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
  subtitle timeline, so you can listen to one file instead of many clips. It
  places each Telugu line at its subtitle timestamp instead of concatenating
  clips, so generated line lengths do not shift later dialogue out of sync.
- When `--media` is provided, it also writes `telugu_dub_track.m4a`, which
  mixes the Telugu timeline voice over the original background.
- `--tts-engine mms` uses Meta MMS Telugu TTS (`facebook/mms-tts-tel`) through
  Transformers. `--tts-engine macos` keeps the older built-in macOS `say` path.

### Automatic Speaker Detection

For stable speaker IDs, run the clustering step first. It extracts mono analysis
audio from the source, builds a speaker embedding for each subtitle window,
clusters similar voices, and writes `speakers.json` plus
`line_speaker_map.json`:

```bash
python3 speaker_cluster.py input.srt movie_or_audio.mkv cluster-out --embedding-model speechbrain
```

Then generate Telugu audio with the stable speaker map:

```bash
python3 pipeline.py input.srt out --media movie_or_audio.mkv --speaker-map cluster-out/line_speaker_map.json --speakers cluster-out/speakers.json
```

That two-command flow covers the three offline phases:
- Phase 1: `speaker_cluster.py` creates stable `speaker_id` values from the
  original audio and subtitle timings.
- Phase 2: `pipeline.py` reuses the same local Telugu voice settings for each
  `speaker_id` and writes the synced Telugu speech timeline.
- Phase 3: when `--media` is passed, `pipeline.py` mixes that Telugu timeline
  with the original background and writes `telugu_dub_track.m4a`.

The final mix keeps the source audio as the background bed, converts it to
stereo when needed, and overlays the Telugu speech timeline.

This local clustering is lightweight. It uses acoustic embeddings plus pitch,
not actor names. Pitch is only used to choose adult male, adult female, or child
voice type.

With `--embedding-model speechbrain`, clustering uses the open-source
SpeechBrain ECAPA speaker-recognition model (`speechbrain/spkrec-ecapa-voxceleb`)
for real speaker embeddings. The production runner does not use fallback
embeddings or fallback TTS.

You can still pass `--media` directly to `pipeline.py`, but without the
speaker map that path is the older pitch-only shortcut:

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

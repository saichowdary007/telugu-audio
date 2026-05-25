# Telugu Audio

Tools for generating Telugu audio tracks and syncing them with browser video playback.

## Python Pipeline

Generate Telugu MP3 clips and sync metadata from an `.srt` file:

```bash
python3 -m pip install -r requirements.txt
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/google-cloud-key.json
python3 pipeline.py input.srt out
```

Output:

```text
out/
  clip_001.mp3
  clip_002.mp3
  sync.json
  speakers.json
```

The script uses simple automatic speaker detection. Named subtitle lines like
`Ravi: Hello` keep the same voice profile across the file. Unnamed dialogue
alternates between two default speakers, and words like `woman`, `girl`,
`father`, or `child` influence the voice type. Speaker differences are created
with Google Telugu TTS voice selection plus pitch and speaking-rate changes.

### TTS Provider

The pipeline supports two Google TTS paths:

```bash
# Higher free usage volume for standard voices
TTS_PROVIDER=cloud python3 pipeline.py input.srt out

# Gemini 3.1 Flash TTS preview
TTS_PROVIDER=gemini GEMINI_API_KEY=... python3 pipeline.py input.srt out
```

For free-tier usage volume, Cloud Text-to-Speech standard voices currently have
the larger free allowance. Gemini 3.1 Flash TTS is the newer controllable model,
but its free-tier request limits are much tighter.

If you pasted an API key into chat, rotate it and use an environment variable
instead.

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

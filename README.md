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

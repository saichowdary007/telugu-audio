(async () => {
  const state = {
    sourceUrl: "",
    clips: [],
    video: null,
    audio: new Audio(),
    currentClip: -1,
    ready: false,
  };

  state.audio.preload = "auto";

  function baseUrl(url) {
    return url.endsWith("/") ? url : `${url}/`;
  }

  function findClip(time) {
    for (const [index, clip] of state.clips.entries()) {
      if (time >= clip.start && time < clip.end) return [index, clip];
    }
    return [-1, null];
  }

  function stopAudio() {
    state.audio.pause();
    state.audio.removeAttribute("src");
    state.audio.load();
    state.currentClip = -1;
  }

  function playClip(src, offset, index) {
    state.audio.pause();
    state.audio.onloadedmetadata = () => {
      state.audio.currentTime = offset;
      state.audio.play().catch(() => {});
    };
    state.audio.src = src;
    state.audio.playbackRate = state.video?.playbackRate || 1;
    state.currentClip = index;
    state.audio.load();
  }

  function resync() {
    const video = state.video;
    if (!video || !state.ready) return;
    video.muted = true;

    if (video.paused || video.ended) {
      stopAudio();
      return;
    }

    const [index, clip] = findClip(video.currentTime);
    if (!clip) {
      stopAudio();
      return;
    }

    const targetSrc = `${baseUrl(state.sourceUrl)}${clip.url}`;
    const offset = Math.max(0, video.currentTime - clip.start);

    if (index !== state.currentClip || state.audio.src !== targetSrc) {
      playClip(targetSrc, offset, index);
      return;
    }

    state.audio.playbackRate = video.playbackRate;
    if (Math.abs(state.audio.currentTime - offset) > 0.25) {
      state.audio.currentTime = offset;
    }
    if (state.audio.paused && !state.audio.ended) state.audio.play().catch(() => {});
  }

  function bindVideo(video) {
    if (state.video === video) return;
    if (state.video) {
      state.video.removeEventListener("play", resync);
      state.video.removeEventListener("pause", resync);
      state.video.removeEventListener("seeking", resync);
      state.video.removeEventListener("timeupdate", resync);
      state.video.removeEventListener("ratechange", resync);
    }
    state.video = video;
    video.addEventListener("play", resync);
    video.addEventListener("pause", resync);
    video.addEventListener("seeking", resync);
    video.addEventListener("timeupdate", resync);
    video.addEventListener("ratechange", resync);
    resync();
  }

  async function loadPackage() {
    if (!state.sourceUrl) return;
    const response = await fetch(`${baseUrl(state.sourceUrl)}sync.json`, { cache: "no-store" });
    const data = await response.json();
    state.clips = data.audio_clips || [];
    state.ready = true;
    resync();
  }

  async function loadConfig() {
    const { sourceUrl } = await chrome.storage.local.get({ sourceUrl: "" });
    state.sourceUrl = sourceUrl.trim();
    state.ready = false;
    stopAudio();
    if (state.sourceUrl) await loadPackage();
  }

  const observer = new MutationObserver(() => {
    const video = document.querySelector("video");
    if (video) bindVideo(video);
  });

  observer.observe(document.documentElement, { childList: true, subtree: true });
  await loadConfig();
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area === "local" && changes.sourceUrl) loadConfig();
  });
  const video = document.querySelector("video");
  if (video) bindVideo(video);
})();

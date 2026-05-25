const source = document.getElementById("source");
const save = document.getElementById("save");

chrome.storage.local.get({ sourceUrl: "" }, ({ sourceUrl }) => {
  source.value = sourceUrl;
});

save.addEventListener("click", async () => {
  await chrome.storage.local.set({ sourceUrl: source.value.trim() });
  window.close();
});

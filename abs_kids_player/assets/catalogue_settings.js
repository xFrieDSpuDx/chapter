"use strict";
const activeLibrary = document.getElementById("active-library");
const lastRefresh = document.getElementById("last-refresh");
const statusLine = document.getElementById("refresh-status");
const optionsStatus = document.getElementById("library-options-status");
const libraryChoice = document.getElementById("library-choice");
const librarySave = document.getElementById("library-save");
const picker = document.getElementById("library-picker");
let libraryId = picker.dataset.libraryId;
const requestId = picker.dataset.requestId;

async function libraryJSON(path) {
  const response = await fetch(path, {cache: "no-store"});
  if (!response.ok) throw new Error("Library request failed");
  return response.json();
}

function showActiveLibrary(name) {
  const option = Array.from(libraryChoice.options).find(row => row.value === libraryId);
  if (option) libraryChoice.value = libraryId;
  if (name || option) activeLibrary.textContent = name || option.textContent;
}

async function loadLibraryChoices() {
  try {
    const data = await libraryJSON("/settings/catalogue/options");
    if (data.error) {
      optionsStatus.textContent = data.error;
      return;
    }
    for (const library of data.libraries) {
      libraryChoice.add(new Option(library.name, library.id));
    }
    libraryChoice.disabled = librarySave.disabled = !data.libraries.length;
    optionsStatus.textContent = data.libraries.length ? "" : "Your account has no available audiobook libraries.";
    showActiveLibrary("");
  } catch (error) {
    optionsStatus.textContent = "Could not load the library list. Reload this page to try again.";
  }
}

async function watchLibraryRefresh() {
  const deadline = Date.now() + 90000;
  try {
    while (Date.now() < deadline) {
      const data = await libraryJSON("/settings/catalogue/result?request=" + encodeURIComponent(requestId));
      if (data.outcome === "queued") {
        await new Promise(resolve => setTimeout(resolve, 750));
        continue;
      }
      libraryId = data.libraryId;
      showActiveLibrary(data.libraryName);
      lastRefresh.textContent = data.lastRefresh;
      statusLine.textContent = data.outcome === "applied" ? "Library is up to date."
        : data.outcome === "superseded" ? "A newer library request or settings change took priority."
        : "Refresh failed. " + (data.error || "Try again.");
      for (const key of document.querySelectorAll('input[name="catalogue_key"]')) {
        key.value = data.catalogueKey;
      }
      history.replaceState(null, "", "/settings/catalogue");
      return;
    }
    statusLine.textContent = "Chapter has not confirmed the refresh yet. Reload this page to check again.";
  } catch (error) {
    statusLine.textContent = "Could not check the refresh result. Reload this page to check again.";
  }
}

if (libraryChoice.form.elements.catalogue_key.value) loadLibraryChoices();
if (requestId) watchLibraryRefresh();

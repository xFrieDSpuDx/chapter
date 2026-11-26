(() => {
  const root = document.documentElement;
  const modeKey = "chapter-admin-theme";
  const paletteKey = "chapter-admin-palette";
  const modes = ["system", "light", "dark"];
  const palettes = root.dataset.paletteOptions.split(" ");

  function readChoice(key, options, fallback) {
    try {
      const saved = localStorage.getItem(key);
      if (options.includes(saved)) return saved;
    } catch {}
    return fallback;
  }

  function saveChoice(key, value) {
    try { localStorage.setItem(key, value); } catch {}
  }

  root.dataset.theme = readChoice(modeKey, modes, "system");
  root.dataset.palette = readChoice(paletteKey, palettes, root.dataset.palette);

  document.addEventListener("DOMContentLoaded", () => {
    const mode = document.querySelector("[data-admin-theme]");
    mode.value = root.dataset.theme;
    mode.parentElement.hidden = false;
    mode.addEventListener("change", () => {
      root.dataset.theme = mode.value;
      saveChoice(modeKey, mode.value);
    });

    const paletteSelect = document.querySelector("[data-admin-palette]");
    const picker = document.querySelector("[data-palette-picker]");
    const options = picker?.querySelectorAll("input[name=palette]") || [];
    function displayPalette(value) {
      root.dataset.palette = value;
      paletteSelect.value = value;
      for (const option of options) option.checked = option.value === value;
    }
    function choosePalette(value) {
      displayPalette(value);
      saveChoice(paletteKey, value);
    }
    displayPalette(root.dataset.palette);
    paletteSelect.addEventListener("change", () => choosePalette(paletteSelect.value));
    for (const option of options) {
      option.addEventListener("change", () => {
        if (option.checked) choosePalette(option.value);
      });
    }
    if (picker) picker.hidden = false;
  });
})();

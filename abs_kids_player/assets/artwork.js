(() => {
  function hideMissingCover(image) {
    if (image instanceof HTMLImageElement && image.hasAttribute("data-book-cover")) {
      image.hidden = true;
    }
  }
  document.addEventListener("error", (event) => hideMissingCover(event.target), true);
  document.querySelectorAll("img[data-book-cover][src]").forEach((image) => {
    if (image.complete && !image.naturalWidth) hideMissingCover(image);
  });
})();

(() => {
  const shell = document.querySelector(".admin-shell");
  const button = shell.querySelector(".admin-menu-toggle");
  const menu = document.getElementById(button.getAttribute("aria-controls"));
  const appearance = menu.querySelector(".menu-appearance");
  const mode = shell.querySelector(".admin-theme");
  const compact = window.matchMedia("(max-width: 740px)");

  function setOpen(open) {
    button.setAttribute("aria-expanded", String(open));
    menu.hidden = compact.matches && !open;
  }

  function layout() {
    const focus = document.activeElement;
    const movingToMenu = compact.matches && (menu.contains(focus) || mode.contains(focus));
    shell.classList.toggle("has-mobile-menu", compact.matches);
    button.hidden = !compact.matches;
    appearance.hidden = !compact.matches;
    if (compact.matches) appearance.prepend(mode);
    else button.before(mode);
    setOpen(false);
    if (movingToMenu) button.focus();
    else if (!compact.matches && (focus === button || appearance.contains(focus) || mode.contains(focus))) {
      mode.querySelector("select").focus();
    }
  }

  function outside(target) {
    return !menu.contains(target) && !button.contains(target);
  }

  button.addEventListener("click", () => setOpen(menu.hidden));
  document.addEventListener("click", event => {
    if (compact.matches && !menu.hidden && (outside(event.target) || event.target.closest(".admin-nav a"))) {
      setOpen(false);
    }
  });
  document.addEventListener("focusin", event => {
    if (compact.matches && !menu.hidden && outside(event.target)) setOpen(false);
  });
  document.addEventListener("keydown", event => {
    if (event.key === "Escape" && compact.matches && !menu.hidden) {
      setOpen(false);
      button.focus();
    }
  });
  compact.addEventListener("change", layout);
  layout();
})();

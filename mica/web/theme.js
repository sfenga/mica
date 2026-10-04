// Apply before styles paint so a saved light preference never flashes dark.
(() => {
  const key = "mica.theme";
  const choices = new Set(["light", "dark", "system"]);
  const media = window.matchMedia("(prefers-color-scheme: dark)");
  let preference = "dark";
  try {
    const saved = localStorage.getItem(key);
    if (choices.has(saved)) preference = saved;
  } catch {
    /* Theme switching still works when storage is unavailable. */
  }
  function apply() {
    const theme =
      preference === "system" ? (media.matches ? "dark" : "light") : preference;
    document.documentElement.dataset.theme = theme;
    document.documentElement.style.colorScheme = theme;
    document.querySelectorAll("[data-theme-choice]").forEach((button) => {
      button.setAttribute(
        "aria-pressed",
        String(button.dataset.themeChoice === preference),
      );
    });
  }
  apply();
  media.addEventListener("change", () => {
    if (preference === "system") apply();
  });
  window.addEventListener("storage", (event) => {
    if (event.key !== key && event.key !== null) return;
    preference = choices.has(event.newValue) ? event.newValue : "dark";
    apply();
  });
  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll("[data-theme-choice]").forEach((button) => {
      button.addEventListener("click", () => {
        preference = button.dataset.themeChoice;
        try {
          localStorage.setItem(key, preference);
        } catch {
          /* Keep the in-memory selection. */
        }
        apply();
      });
    });
    apply();
  });
})();

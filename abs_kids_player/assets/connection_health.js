(function() {
  var status = document.querySelector("[data-connection-health]");
  var overviewFields = document.querySelectorAll("[data-overview-value]");
  var visibilityTarget = status || overviewFields[0];
  if (!visibilityTarget) return;
  var lastContent = status?.innerHTML.trim();
  var bluetooth = document.querySelector("[data-bluetooth-status]");
  var bluetoothCheckAt = 0;
  var readingOverview = false;
  var readingWifi = false;

  function refreshWifi() {
    const label = document.querySelector("[data-wifi-status]");
    const card = document.querySelector("[data-wifi-card]");
    const visibleCard = card && !card.closest("[hidden]");
    if (readingWifi || (!visibleCard && (!label || label.closest("[hidden]")))) return;
    readingWifi = true;
    const query = visibleCard ? "?card=1" : "";
    fetch("/status/wifi" + query, {cache: "no-store", signal: AbortSignal.timeout?.(5000)})
      .then(response => response.ok ? response.json() : null)
      .then(result => {
        if (!result) return;
        if (label) {
          if (label.innerHTML !== result.label) label.innerHTML = result.label;
          label.classList.toggle("ok", result.connected === true);
          label.classList.toggle("bad", result.connected === false);
        }
        if (card && result.card && card.dataset.wifiCard !== result.cardKey) card.outerHTML = result.card;
      })
      .catch(() => {})
      .finally(() => { readingWifi = false; });
  }

  function refreshOverviewStatus() {
    if (!overviewFields.length || readingOverview) return;
    readingOverview = true;
    fetch("/status/overview", {cache: "no-store", signal: AbortSignal.timeout?.(5000)})
      .then(response => response.ok ? response.json() : null)
      .then(result => {
        if (!result) return;
        for (const field of overviewFields) {
          const value = result[field.dataset.overviewValue];
          if (typeof value !== "string") continue;
          if (field.textContent !== value) field.textContent = value;
          if (field.hidden !== !value) field.hidden = !value;
        }
      })
      .catch(() => {})
      .finally(() => { readingOverview = false; });
  }

  function refreshBluetooth() {
    if (!bluetooth || bluetooth.closest("[hidden]") || Date.now() < bluetoothCheckAt) return;
    bluetoothCheckAt = Date.now() + 15000;
    fetch("/status/bluetooth", {cache: "no-store"})
      .then(response => response.ok ? response.json() : null)
      .then(result => {
        if (!result) return;
        if (result.label === "Checking Bluetooth…") bluetoothCheckAt = 0;
        if (bluetooth.innerHTML !== result.label) bluetooth.innerHTML = result.label;
        bluetooth.parentElement.classList.toggle("ok", result.connected);
      })
      .catch(() => {});
  }

  function refreshConnectionHealth() {
    if (!document.hidden) refreshWifi();
    if (document.hidden || visibilityTarget.closest("[hidden]")) {
      window.setTimeout(refreshConnectionHealth, 3000);
      return;
    }
    refreshOverviewStatus();
    if (!status) {
      window.setTimeout(refreshConnectionHealth, 3000);
      return;
    }
    refreshBluetooth();
    fetch("/status/audiobookshelf", { cache: "no-store" })
      .then(function(response) {
        if (!response.ok) throw new Error("Connection status unavailable");
        return response.text();
      })
      .then(function(content) {
        if (lastContent !== content) {
          status.innerHTML = content;
          lastContent = content;
        }
      })
      .catch(function() {
        // Keep the last result and its timestamp when the admin page loses its connection.
      })
      .finally(function() {
        window.setTimeout(refreshConnectionHealth, 3000);
      });
  }

  window.setTimeout(refreshConnectionHealth, 1000);
  bluetoothCheckAt = Date.now() + 1000;
})();

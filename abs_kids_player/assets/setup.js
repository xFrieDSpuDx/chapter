    var volumeSlider = document.querySelector("[data-volume-slider]");
    var volumePercent = document.querySelector("[data-volume-percent]");
    var progressBar = document.querySelector("[data-listening-progress]");
    var playbackForm = document.querySelector("[data-playback-form]");
    var playbackAction = playbackForm?.querySelector("[data-playback-action]");
    var playbackButton = playbackForm?.querySelector("[data-playback-button]");
    var playbackPending = document.querySelector("[data-playback-pending]");
    var podcastRows = document.querySelector("[data-podcast-rows]");
    var addPodcastRowButton = document.querySelector("[data-add-podcast-row]");
    var confirmationTriggers = document.querySelectorAll("[data-confirm-trigger]");
    var confirmationPanels = document.querySelectorAll("[data-confirm-panel]");
    var volumeTimer = null;
    var latestPlayerStatus = null;
    var pendingPlaybackAction = null;
    var commandSequence = {playback: 0, volume: 0};
    var sseStatusReceived = false;
    var sseFallbackTimer = null;
    var fallbackPollTimer = null;
    var systemUpdatePollTimer = null;
    var softwareUpdateWasInstalling = false;
    var csrfToken = document.querySelector('meta[name="chapter-csrf-token"]').getAttribute("content");
    function applySystemUpdateStatus(status) {
      var statusText = document.querySelector("[data-system-update-status]");
      var description = document.querySelector("[data-system-update-description]");
      var trigger = document.querySelector("[data-system-update-trigger]");
      var versionText = document.querySelector("[data-system-update-version]");
      var targetField = document.querySelector("[data-system-update-target]");
      var latestVersion = status.latestVersion ? "v" + status.latestVersion : "";
      if (status.phase === "installing") {
        softwareUpdateWasInstalling = true;
      }
      if (
        softwareUpdateWasInstalling &&
        (status.phase === "completed" || (status.phase === "current" && !status.updateAvailable))
      ) {
        softwareUpdateWasInstalling = false;
        window.location.replace("/");
        return;
      }
      var updateServiceIsRestarting = (
        softwareUpdateWasInstalling && status.phase === "available" && status.updateAvailable
      );
      if (status.phase === "installing") {
        if (status.installationStage === "rolling_back") {
          statusText.textContent = "Restoring previous version…";
        } else if (status.installationStage === "verifying") {
          statusText.textContent = "Checking display and setup service…";
        } else {
          statusText.textContent = latestVersion ? "Installing " + latestVersion + "…" : "Installing update…";
        }
        description.textContent = status.message || "Installing the latest stable release. Chapter will restart its services when ready.";
      } else if (updateServiceIsRestarting) {
        statusText.textContent = "Finishing update...";
        description.textContent = "Chapter is restarting with the newly installed version.";
      } else if (status.phase === "failed") {
        softwareUpdateWasInstalling = false;
        statusText.textContent = "Update failed";
        description.textContent = status.message || "The software update did not finish.";
      } else if (status.phase === "local") {
        statusText.textContent = "Local package installed";
        description.textContent = status.message || "";
      } else if (status.phase === "trial") {
        statusText.textContent = "Local trial build";
        description.textContent = status.message || "";
      } else if (status.phase === "error") {
        statusText.textContent = softwareUpdateWasInstalling ? "Finishing update..." : "Update check needs attention";
        description.textContent = softwareUpdateWasInstalling
          ? "Waiting for the Chapter web service to restart."
          : (status.message || "Chapter could not check for software updates.");
      } else if (status.updateAvailable) {
        statusText.textContent = latestVersion + " available";
        description.textContent = "A newer stable version of Chapter is ready to install.";
      } else if (status.phase === "completed") {
        statusText.textContent = "Up to date";
        description.textContent = status.message || "";
      } else {
        statusText.textContent = "Up to date";
        description.textContent = "";
      }
      description.hidden = !description.textContent.trim();
      var updateSummary = document.querySelector("[data-update-summary]");
      if (updateSummary) updateSummary.textContent = statusText.textContent;
      if (versionText) {
        versionText.textContent = latestVersion || "the latest release";
      }
      if (targetField) {
        targetField.value = status.updateAvailable ? status.latestVersion : "";
      }
      if (trigger) {
        trigger.hidden = softwareUpdateWasInstalling || !status.updateAvailable;
        trigger.textContent = latestVersion ? "Update to " + latestVersion : "Update";
        if (trigger.hidden) {
          closeConfirmation("update", false);
        }
      }
      if (systemUpdatePollTimer) {
        window.clearTimeout(systemUpdatePollTimer);
      }
      systemUpdatePollTimer = window.setTimeout(
        refreshSystemUpdateStatus,
        softwareUpdateWasInstalling ? 2000 : (status.phase === "error" ? 60000 : 900000)
      );
    }
    function refreshSystemUpdateStatus() {
      var request = new XMLHttpRequest();
      request.open("GET", "/system/update/status", true);
      request.onreadystatechange = function() {
        if (request.readyState !== 4) {
          return;
        }
        if (request.status === 200) {
          try {
            applySystemUpdateStatus(JSON.parse(request.responseText));
            return;
          } catch (error) {}
        }
        applySystemUpdateStatus({
          phase: "error",
          latestVersion: "",
          updateAvailable: false,
          message: "Chapter could not check for software updates. It will try again automatically."
        });
      };
      request.send();
    }
    refreshSystemUpdateStatus();
    function closeConfirmation(panelName, restoreFocus) {
      var panel = document.querySelector('[data-confirm-panel="' + panelName + '"]');
      var trigger = document.querySelector('[data-confirm-trigger="' + panelName + '"]');
      if (panel) {
        panel.hidden = true;
      }
      if (trigger) {
        trigger.setAttribute("aria-expanded", "false");
        if (restoreFocus) {
          trigger.focus();
        }
      }
    }
    confirmationTriggers.forEach(function(trigger) {
      trigger.addEventListener("click", function() {
        var panelName = trigger.getAttribute("data-confirm-trigger");
        confirmationPanels.forEach(function(panel) {
          closeConfirmation(panel.getAttribute("data-confirm-panel"), false);
        });
        var panel = document.querySelector('[data-confirm-panel="' + panelName + '"]');
        if (!panel) {
          return;
        }
        panel.hidden = false;
        trigger.setAttribute("aria-expanded", "true");
        var cancelButton = panel.querySelector("[data-confirm-cancel]");
        if (cancelButton) {
          cancelButton.focus();
        }
      });
    });
    document.querySelectorAll("[data-confirm-cancel]").forEach(function(button) {
      button.addEventListener("click", function() {
        var panel = button.closest("[data-confirm-panel]");
        if (panel) {
          closeConfirmation(panel.getAttribute("data-confirm-panel"), true);
        }
      });
    });
    function encodeForm(fields) {
      var parts = [];
      for (var key in fields) {
        if (Object.prototype.hasOwnProperty.call(fields, key)) {
          parts.push(encodeURIComponent(key) + "=" + encodeURIComponent(fields[key]));
        }
      }
      return parts.join("&");
    }
    function postPlayer(fields, callback) {
      var request = new XMLHttpRequest();
      fields["$csrf_field_name"] = csrfToken;
      request.open("POST", "/player", true);
      request.timeout = 8000;
      request.setRequestHeader("Content-Type", "application/x-www-form-urlencoded");
      request.setRequestHeader("X-Requested-With", "fetch");
      request.onreadystatechange = function() {
        if (request.readyState === 4 && callback) {
          var result = null;
          if (request.status === 202) {
            try { result = JSON.parse(request.responseText); } catch (error) {}
          }
          callback(result && result.id);
        }
      };
      request.send(encodeForm(fields));
    }
    function sendVolume(value) {
      sendConfirmedCommand("volume", {action: "volume", volume: value});
    }
    function sendConfirmedCommand(lane, fields) {
      var sequence = ++commandSequence[lane];
      var feedback = document.querySelector('[data-command-feedback="' + lane + '"]');
      if (feedback) feedback.textContent = lane === "volume" ? "Sending volume…" : "";
      function finish(message, applied) {
        if (sequence !== commandSequence[lane]) return;
        if (feedback) feedback.textContent = message;
        if (lane === "playback") {
          setPendingPlayback(null);
          if (applied) renderPlaybackControl(fields.action === "play");
        }
        refreshPlayerStatus();
      }
      postPlayer(fields, function(id) {
        if (sequence !== commandSequence[lane]) return;
        if (!id) {
          finish("Command could not be queued.");
          return;
        }
        if (feedback && lane === "volume") feedback.textContent = "Waiting for Chapter…";
        var deadline = Date.now() + 12000;
        function poll() {
          if (sequence !== commandSequence[lane]) return;
          fetch("/player/command?id=" + encodeURIComponent(id), {cache: "no-store", signal: AbortSignal.timeout?.(5000)})
            .then(function(response) { return response.json(); })
            .then(function(result) {
              if (result.outcome === "applied") finish(lane === "volume" ? "Applied." : "", true);
              else if (result.outcome === "rejected") finish("Chapter could not apply this command.");
              else if (result.outcome === "superseded") finish("Replaced by a newer command.");
              else if (Date.now() < deadline) window.setTimeout(poll, 300);
              else finish("Chapter has yet to confirm this command.");
            })
            .catch(function() { finish("Command confirmation unavailable."); });
        }
        poll();
      });
    }
    function applyPlayerStatus(status) {
      latestPlayerStatus = status;
      document.dispatchEvent(new CustomEvent("chapter:session", {detail: !!(status.hasSession || status.openingKey)}));
      var playerContainer = document.querySelector("[data-now-playing]");
      var title = document.querySelector("[data-now-playing-title]");
      var cover = playerContainer && playerContainer.querySelector("[data-book-cover]");
      var coverUrl = status.hasSession ? (status.coverUrl || "") : "";
      if (cover && coverUrl !== (cover.getAttribute("src") || "")) {
        if (coverUrl) cover.setAttribute("src", coverUrl);
        else cover.removeAttribute("src");
        cover.hidden = !coverUrl;
      }
      var meta = document.querySelector("[data-now-playing-meta]");
      var volumeForm = document.querySelector("[data-volume-form]");
      if (!status.hasSession) {
        if (progressBar) progressBar.hidden = true;
        if (playerContainer) {
          playerContainer.setAttribute("data-has-session", "0");
        }
        if (title) {
          title.textContent = "Nothing playing";
        }
        if (meta) {
          meta.textContent = "";
        }
        if (volumeForm) {
          volumeForm.classList.add("hidden");
        }
        return;
      }
      if (playerContainer) {
        playerContainer.setAttribute("data-has-session", "1");
      }
      if (volumeForm) {
        volumeForm.classList.remove("hidden");
      }
      if (title) {
        title.textContent = status.title;
      }
      renderLivePlayerTime();
      if (!pendingPlaybackAction) renderPlaybackControl(status.isPlaying);
      if (volumeSlider && volumePercent && document.activeElement !== volumeSlider) {
        volumeSlider.value = status.volumePercent;
        volumePercent.textContent = status.volumePercent + "%";
      }
    }
    function twoDigit(value) {
      value = String(value);
      return value.length < 2 ? "0" + value : value;
    }
    function formatDuration(seconds) {
      seconds = Math.max(0, Math.floor(seconds || 0));
      var hours = Math.floor(seconds / 3600);
      var minutes = Math.floor((seconds % 3600) / 60);
      var secs = seconds % 60;
      if (hours) {
        return hours + ":" + twoDigit(minutes) + ":" + twoDigit(secs);
      }
      return minutes + ":" + twoDigit(secs);
    }
    function livePlaybackPosition(status) {
      var currentTime = Number(status.currentTime || 0);
      var duration = Number(status.duration || 0);
      if (status.isPlaying) {
        var updatedAt = Number(status.updatedAt || 0);
        if (updatedAt > 0) {
          currentTime += Math.max(0, Date.now() / 1000 - updatedAt) * (status.playbackRate || 1);
        }
      }
      return duration > 0 ? Math.min(currentTime, duration) : currentTime;
    }
    function renderLivePlayerTime() {
      var meta = document.querySelector("[data-now-playing-meta]");
      if (!meta || !latestPlayerStatus || !latestPlayerStatus.hasSession) {
        return;
      }
      var duration = Number(latestPlayerStatus.duration || 0);
      var position = livePlaybackPosition(latestPlayerStatus);
      var time = formatDuration(position);
      meta.textContent = latestPlayerStatus.state + " · " + (duration > 0 ? time + " / " + formatDuration(duration) : time);
      if (progressBar) {
        progressBar.hidden = duration <= 0;
        progressBar.value = duration > 0 ? position / duration : 0;
      }
    }
    function renderPlaybackControl(playing) {
      if (!playbackButton) return;
      var icon = playbackButton.firstElementChild;
      var label = playing ? "Pause on Chapter" : "Play on Chapter";
      icon.classList.toggle("icon-pause", playing);
      icon.classList.toggle("icon-play", !playing);
      playbackAction.value = playing ? "pause" : "play";
      playbackButton.setAttribute("aria-label", label);
      playbackButton.title = label;
    }
    function setPendingPlayback(action) {
      pendingPlaybackAction = action;
      playbackButton.disabled = !!action;
      if (action) playbackButton.setAttribute("aria-busy", "true");
      else playbackButton.removeAttribute("aria-busy");
      playbackPending.textContent = action ? (action === "play" ? "Resuming…" : "Pausing…") : "";
    }
    function refreshPlayerStatus() {
      var request = new XMLHttpRequest();
      request.open("GET", "/player/status?_=" + Date.now(), true);
      request.onreadystatechange = function() {
        if (request.readyState !== 4) {
          return;
        }
        if (request.status >= 200 && request.status < 300) {
          try {
            var status = JSON.parse(request.responseText);
            applyPlayerStatus(status);
          } catch (error) {}
        }
      };
      request.send();
    }
    function startFallbackPolling() {
      if (fallbackPollTimer) {
        return;
      }
      fallbackPollTimer = window.setInterval(refreshPlayerStatus, 1000);
      refreshPlayerStatus();
    }
    function startPlayerEventStream() {
      if (!window.EventSource) {
        startFallbackPolling();
        return false;
      }
      try {
        var source = new EventSource("/player/events");
        source.onmessage = function(event) {
          try {
            applyPlayerStatus(JSON.parse(event.data));
            sseStatusReceived = true;
            if (sseFallbackTimer) {
              window.clearTimeout(sseFallbackTimer);
              sseFallbackTimer = null;
            }
          } catch (error) {
            // Keep the fallback polling running.
          }
        };
        source.onerror = function() {
          if (sseFallbackTimer) {
            window.clearTimeout(sseFallbackTimer);
          }
          sseFallbackTimer = window.setTimeout(function() {
            if (!sseStatusReceived) {
              startFallbackPolling();
            }
          }, 4000);
        };
        return true;
      } catch (error) {
        startFallbackPolling();
        return false;
      }
    }
    function sendPlayerAction(action) {
      sendConfirmedCommand("playback", {action: action});
    }
    function addPodcastRow(value) {
      if (!podcastRows) {
        return;
      }
      var row = document.createElement("div");
      row.className = "podcast-row";
      var label = document.createElement("label");
      label.className = "field";
      label.textContent = "Podcast URL";
      var input = document.createElement("input");
      input.name = "podcast_url";
      input.type = "url";
      input.placeholder = "https://podcasts.apple.com/…";
      input.autocapitalize = "none";
      input.spellcheck = false;
      input.value = value || "";
      var remove = document.createElement("button");
      remove.className = "secondary";
      remove.type = "button";
      remove.textContent = "Remove";
      remove.setAttribute("aria-label", "Remove podcast URL");
      remove.setAttribute("data-remove-podcast-row", "1");
      label.appendChild(input);
      row.appendChild(label);
      row.appendChild(remove);
      podcastRows.appendChild(row);
      attachPodcastRemoveButtons();
      input.focus();
    }
    function attachPodcastRemoveButtons() {
      var buttons = document.querySelectorAll("[data-remove-podcast-row]");
      buttons.forEach(function(button) {
        if (button.getAttribute("data-bound") === "1") {
          return;
        }
        button.setAttribute("data-bound", "1");
        button.addEventListener("click", function() {
          var row = button.closest(".podcast-row");
          if (row) {
            row.remove();
          }
          if (podcastRows && !podcastRows.querySelector(".podcast-row")) {
            addPodcastRow("");
          }
        });
      });
    }
    if (playbackForm) {
      playbackForm.addEventListener("submit", function(event) {
        event.preventDefault();
        if (pendingPlaybackAction) return;
        setPendingPlayback(playbackAction.value);
        sendPlayerAction(playbackAction.value);
      });
    }
    if (volumeSlider && volumePercent) {
      volumeSlider.addEventListener("input", function() {
        volumePercent.textContent = volumeSlider.value + "%";
        window.clearTimeout(volumeTimer);
        volumeTimer = window.setTimeout(function() {
          sendVolume(volumeSlider.value);
        }, 120);
      });
      volumeSlider.addEventListener("change", function() {
        window.clearTimeout(volumeTimer);
        sendVolume(volumeSlider.value);
      });
    }
    if (addPodcastRowButton) {
      addPodcastRowButton.addEventListener("click", function() {
        addPodcastRow("");
      });
    }
    attachPodcastRemoveButtons();
    startPlayerEventStream();
    refreshPlayerStatus();
    window.setInterval(renderLivePlayerTime, 1000);

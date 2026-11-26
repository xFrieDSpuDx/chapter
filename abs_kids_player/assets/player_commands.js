// Post gestures in order. A slow device receipt must not hold up the next gesture.
function createPlayerCommands(post) {
  let posted = Promise.resolve();
  let lastSubmission = null;
  const pending = new Map();

  function start(action, value, key) {
    const group = {play: "transport", pause: "transport", mute: "mute", unmute: "mute"}[action] || action;
    const command = {action, value, key, group, confirmedAt: Infinity};
    // Only adjacent, unsent volume targets are interchangeable.
    if (action === "volume" && lastSubmission?.action === "volume" && lastSubmission.key === key) {
      lastSubmission.replaced = true;
    }
    lastSubmission = command;
    pending.delete(group);
    pending.set(group, command);
    const submitted = posted.then(() => {
      if (lastSubmission === command) lastSubmission = null;
      if (!command.replaced) return post(action, key, value);
    });
    posted = submitted.catch(() => {});
    return {command, submitted};
  }

  function discard(command) {
    if (pending.get(command.group) === command) pending.delete(command.group);
  }

  function preview(device) {
    const view = {...device};
    for (const command of pending.values()) {
      const key = command.action === "cancel_open" ? device.openingKey : device.sessionKey;
      if (command.key !== key || (device.updatedAt || 0) >= command.confirmedAt) {
        discard(command);
        continue;
      }
      const value = Number(command.value);
      switch (command.action) {
        case "volume":
          view.volumePercent = value;
          view.muted = value === 0;
          if (!value) view.isPlaying = false;
          break;
        case "speed": view.speed = value; break;
        case "play": view.isPlaying = true; break;
        case "pause": view.isPlaying = false; break;
        case "mute": view.muted = true; break;
        case "unmute": view.muted = false; break;
        case "sleep_timer": view.sleepPreset = value; break;
        case "seek":
          view.currentTime = value / 1000;
          view.updatedAt = Date.now() / 1000;
          break;
      }
    }
    return view;
  }

  return {start, discard, preview, current: command => pending.get(command.group) === command};
}

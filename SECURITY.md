# Security Policy

## Supported Versions

Security fixes are made for the latest tagged release. Update to the release shown at the top of the [README](README.md) before reporting a problem.

## Trusted Network Design

Chapter Player is for a trusted private home network. Set an owner username and password in **Settings → Security** to require login. You can allow read-only visitor access to Overview, Library and Downloads from the same page. Settings and commands always require login once an owner account exists. See the [owner access and recovery instructions](docs/raspberry-pi-zero-2w-install.md#owner-access-and-password-recovery).

Before you set an owner password, anyone who can reach the administration page can control playback, change player settings, replace the Audiobookshelf login, change or forget Wi-Fi, reboot the player, or restore it to default settings. The setup service also performs privileged system operations so it can manage networking and reboot the appliance.

- Do not expose ports `80` or `47831` to the internet.
- Do not configure router port forwarding for the player.
- Keep the player off public, guest, or otherwise untrusted networks.
- Complete first-time setup in a controlled location. The temporary `Chapter-Setup` network is intentionally open.
- Use a dedicated Audiobookshelf account limited to the libraries and content intended for the player.

The administration page uses CSRF tokens to stop unrelated websites from submitting commands through a parent's browser. CSRF protection is not authentication and does not prevent another person already on the same network from opening the page directly.

## Reporting A Vulnerability

Please do not disclose a suspected vulnerability in a public issue. Use GitHub's private vulnerability reporting option on the repository's Security tab when available. Include the affected release, reproduction steps, expected behavior, and observed behavior.

## Installed code ownership

The local package keeps both executable releases under root-owned directories in `/opt/chapter-player`. The root setup and boot services use the system Python installation. The service account writes preferences, listening state and downloads in its home directory. The fork's published-release installer uses a root-owned Git checkout and checks its parent directories before installation. Local packages use their own rollback path.

# Listening offline

## Before leaving Wi-Fi

1. Open **Library** in the web interface. Press the download arrow on the cover of each book you want to take.
2. Open **Downloads** to see the queue, transfer progress and free space. These update automatically while the page is open. When a book is ready, its cover shows **Remove download** (the bin icon). On that book, the **ⓘ** button opens its details.
3. Open **Settings → Downloads**. **Prefer downloaded books** is enabled on a fresh installation. With it enabled, choosing a completed book in the ordinary library plays the copy on the Pi. Open **Player** while listening and look for **Downloaded copy**.
4. On the OLED, choose **Browse library → Downloaded**, select a book, then choose **Start** or **Continue**. This shelf always opens the local copy, including when Wi-Fi is available. The OLED can browse the saved books even when it cannot reach Audiobookshelf.

**Storage and artwork.** Under **Settings → Downloads**, set **Download limit (MiB)** and choose **Save settings**. The configured allowance and the Pi’s actual free space can differ. Chapter also saves available cover art and book details beside the audio so you can open **ⓘ** with the server offline. A missing image or synopsis can be filled in later while the server is reachable.

## If a download stops

After a Wi-Fi drop or a reboot, reopen **Downloads** to see the saved queue. Leave a book marked **Waiting to retry** in the queue. Chapter retries when the server becomes reachable. If the server confirms the file is still the same, Chapter requests the remaining bytes. If the server supplies a replacement file, Chapter can restart that track. If Chapter reports **The audio files changed. Remove and download the book again**, use **Remove download** for that incomplete copy and add the book again. Completed books remain available while another transfer is pending.

If Chapter reports **Download storage limit reached**, check **Settings → Downloads → Download limit (MiB)**. For **insufficient free space**, free space on the Pi itself.

**Cancel download** stops a queued or active transfer. **Retry download** resumes the job. **Remove download** deletes the local copy. If Chapter says the book is open, stop playback to release it. Its original remains in your Audiobookshelf library.

## Stop a downloaded book and pick up where you left off

1. Choose a completed download in **Library**. If you have the browser open, **Player** shows **Downloaded copy** while Chapter plays it.
2. At a passage you recognise, choose **... → Stop playback** on Chapter or **Player → Session → Stop playback** in the browser.
3. Select the same book and choose **Continue**. Playback resumes at the saved passage. Choose **Start over** when you want the beginning.

Chapter saves your place locally and retries the sync in the background. While a book is open, **Player** shows **Saved on Chapter · Waiting to sync**, **Saved on Chapter · Sync will retry** or **Synced to Audiobookshelf** when a saved position is available. After closing it, open **System → System status and diagnostics** for **Pending progress updates**, the latest result and the time of the last successful sync.

To move from the Audiobookshelf browser to Chapter, pause the browser, open the book from **Library** and choose **Continue**. Chapter checks the saved position on your account and plays the downloaded audio. A browser rewind to an earlier chapter counts too. A newer unsynced edit on the Pi takes precedence. If the server is unavailable, Chapter uses the position on the Pi and retries progress sync in the background.

If Chapter detects that another player moved the book during an online session, it pauses at the other player’s position. You can continue there or reopen the book from **Library** to check the latest position again.

## If downloaded books appear to be missing

First clear the Library filters and open **Downloads**. In **Connections**, check the Audiobookshelf server address and account. Chapter keeps each account’s downloads separate. If the shelf changed after an upgrade, check the active build in **System → System status and diagnostics**. For a local package, follow the [reboot verification steps](local-pi-release.md#verify-a-full-reboot).

On a standard installation, Chapter keeps audio in the service user’s home under `~/.local/state/abs-kids-player/downloads/`, outside the application checkout. Each completed book has a `book.json` record and its `track-*` files. A leftover `.lock` file alone cannot establish whether a download completed or why it is missing. If you are investigating a loss, record the build, download count and free space before downloading replacements. The settings backup exports preferences only.

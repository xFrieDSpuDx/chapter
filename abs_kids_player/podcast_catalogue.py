"""Build the podcast shelves when the listener opens Podcasts."""

from __future__ import annotations

from concurrent.futures import Future
from threading import Thread
from urllib.parse import quote

from .api import AudiobookshelfClient, AudiobookshelfError, client_from_config
from .config import AppConfig
from .models import Book
from .podcast import PodcastError, rss_episode_books
from .progress_sync import progress_account


def request_podcast_catalogue(config: AppConfig) -> Future[list[Book]]:
    result: Future[list[Book]] = Future()

    def load() -> None:
        try:
            result.set_result(load_podcast_catalogue(config))
        except Exception as error:
            result.set_exception(error)

    try:
        Thread(target=load, name="chapter-podcast-catalogue", daemon=True).start()
    except RuntimeError as error:
        result.set_exception(error)
    return result


def load_podcast_catalogue(config: AppConfig) -> list[Book]:
    episodes: list[Book] = []
    errors = False
    if config.is_ready:
        try:
            episodes.extend(server_podcast_episodes(client_from_config(config)))
        except AudiobookshelfError:
            errors = True
    for feed in config.podcasts:
        try:
            episodes.extend(rss_episode_books(feed, progress_account(config) or "local-rss"))
        except PodcastError:
            errors = True
    if errors and not episodes:
        raise PodcastError("Podcast library unavailable.")
    return episodes


def server_podcast_episodes(client: AudiobookshelfClient) -> list[Book]:
    libraries = [library for library in client.get_libraries() if library.get("mediaType") == "podcast"]
    if not libraries:
        return []
    user = client.get_current_user()
    if isinstance(user.get("user"), dict):
        user = user["user"]
    progress = {
        (row.get("libraryItemId"), row.get("episodeId")): row
        for row in user.get("mediaProgress") or []
        if isinstance(row, dict) and row.get("episodeId")
    }
    result = []
    for library in libraries:
        path = f"/api/libraries/{quote(str(library['id']), safe='')}/items"
        listing = client.request("GET", path, query={"minified": 0})
        for item in listing.get("results", []):
            item_id = str(item["id"])
            media = item.get("media") or {}
            if not isinstance(media.get("episodes"), list):
                expanded = client.request("GET", f"/api/items/{quote(item_id, safe='')}", query={"expanded": 1})
                media = expanded.get("media") or {}
            metadata = media.get("metadata") or {}
            title = str(metadata.get("title") or "Podcast")
            episodes = sorted(media.get("episodes") or [], key=lambda row: -(row.get("publishedAt") or 0))
            for episode in episodes:
                episode_id = str(episode["id"])
                row = progress.get((item_id, episode_id), {})
                audio = episode.get("audioFile") or {}
                result.append(Book(
                    f"{item_id}/{episode_id}", str(episode.get("title") or "Untitled episode"),
                    str(metadata.get("author") or ""), float(episode.get("duration") or audio.get("duration") or 0),
                    "", current_time=float(row.get("currentTime") or 0), progress=float(row.get("progress") or 0),
                    is_finished=bool(row.get("isFinished")), progress_updated_at=int(row.get("lastUpdate") or 0),
                    episode_id=episode_id, source_item_id=item_id, podcast_title=title,
                ))
    return result

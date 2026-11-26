"""Fetch the session information needed to play a book or podcast."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .api import AudiobookshelfClient, AudiobookshelfError, client_from_config, server_position
from .catalogue_settings import catalogue_request_key
from .config import AppConfig, load_config
from .display_frames import DisplayFrame, TwoLineFrame
from .download_store import book_directory, local_book_position, read_download
from .models import PlaybackSession
from .network import setup_address_frame
from .podcast import PodcastError, PodcastProgressClient, latest_podcast_session, podcast_by_book_id
from .progress_sync import progress_account, read_pending_progress


if TYPE_CHECKING:
    from .playback_timing import PlaybackTiming
    from .rotary_ui import MenuCommand


BOOK_PLAYBACK_ERROR_FRAME = TwoLineFrame("Could not play", "Check setup")
DOWNLOAD_PROGRESS_TIMEOUT_SECONDS = 1.0


@dataclass(frozen=True)
class PreparedPlayback:
    session: PlaybackSession
    client: AudiobookshelfClient | PodcastProgressClient


@dataclass(frozen=True)
class PlaybackPreparation:
    prepared: PreparedPlayback | None = None
    error_frame: DisplayFrame | None = None


def prepare_book_playback(
    book_id: str,
    config: AppConfig,
    *,
    resume_time: float = 0,
    start_over: bool = False,
    episode_id: str = "",
    local_only: bool = False,
    timing: PlaybackTiming | None = None,
) -> PreparedPlayback | None:
    podcast = podcast_by_book_id(book_id, config.podcasts)
    if podcast is not None and not local_only:
        account = progress_account(config) or "local-rss"
        with timing.measure_source("podcast") if timing is not None else nullcontext():
            session = latest_podcast_session(
                podcast,
                resume_time=resume_time,
                start_over=start_over,
                episode_id=episode_id,
                account=account,
            )
        return PreparedPlayback(session, PodcastProgressClient(account, session.episode_id))

    if not config.is_ready:
        return None

    client = client_from_config(config, AudiobookshelfClient)
    directory = book_directory(config, book_id)
    download = read_download(directory) if not episode_id else None

    def prepare_local() -> PreparedPlayback:
        with timing.measure_source("download") if timing is not None else nullcontext():
            remote = None
            if not start_over:
                try:
                    remote = client.get_progress(book_id, timeout=DOWNLOAD_PROGRESS_TIMEOUT_SECONDS)
                except (AudiobookshelfError, OSError):
                    pass  # Offline playback uses the latest position already on the Pi.
            live = server_position(remote)
            resume_point = (0.0, live[1]) if live and remote.get("isFinished") is True else live
            position, updated_at = local_book_position(
                config, download.book, resume_time, start_over=start_over, live=resume_point,
            )
            session = download.local_session(directory, progress_account(config), position, updated_at)
            session.library_id = config.library_id
            session.server_checkpoint = live
        return PreparedPlayback(session, client)

    if download and download.complete and (local_only or config.prefer_downloaded):
        return prepare_local()
    if local_only:
        raise DownloadUnavailable("This title is not downloaded.")

    options = {}
    if episode_id:
        options["episode_id"] = episode_id
    if not start_over:
        pending = read_pending_progress().get((progress_account(config), book_id, episode_id))
        if pending is not None:
            options["pending_position"] = (pending.current_time, pending.updated_at)
    try:
        with timing.measure_source("audiobookshelf") if timing is not None else nullcontext():
            session = client.start_playback(
                book_id,
                start_over=start_over,
                resume_time=resume_time,
                **options,
            )
    except AudiobookshelfError:
        if download and download.complete:
            return prepare_local()
        raise
    session.library_id = config.library_id
    return PreparedPlayback(session, client)


def prepare_book_request(
    command: MenuCommand, *, timing: PlaybackTiming | None = None
) -> PlaybackPreparation:
    try:
        config = load_config()
        if command.web_command and command.web_command.catalogue_key != catalogue_request_key(config):
            return PlaybackPreparation(error_frame=TwoLineFrame("Library changed", "Choose the book again"))
        prepared = prepare_book_playback(
            command.book_id,
            config,
            resume_time=command.resume_time,
            start_over=command.start_over,
            episode_id=command.episode_id,
            local_only=command.local_only,
            timing=timing,
        )
        if prepared is None:
            return PlaybackPreparation(error_frame=setup_address_frame())
        return PlaybackPreparation(prepared=prepared)
    except DownloadUnavailable:
        return PlaybackPreparation(error_frame=TwoLineFrame("Book unavailable", "Download it in setup"))
    except (AudiobookshelfError, PodcastError) as error:
        print(f"Playback failed: {error}")
        return PlaybackPreparation(error_frame=BOOK_PLAYBACK_ERROR_FRAME)


class DownloadUnavailable(RuntimeError):
    pass

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SeriesMembership:
    id: str
    name: str
    sequence: str = ""


@dataclass
class Book:
    id: str
    title: str
    author: str
    duration: float
    cover_url: str
    current_time: float = 0
    progress: float = 0
    series_name: str = ""
    series_sequence: str = ""
    series_total: int = 0
    series_memberships: tuple[SeriesMembership, ...] = ()
    progress_updated_at: int = 0
    is_finished: bool = False
    pinned_position: int | None = None
    episode_id: str = ""
    podcast_title: str = ""
    source_item_id: str = ""
    narrator: str = ""
    downloaded: bool = False
    download_only: bool = False

    @property
    def has_play_history(self) -> bool:
        return self.current_time > 0 and not self.finished

    @property
    def finished(self) -> bool:
        return self.is_finished or self.progress >= 0.995

    @property
    def series(self) -> tuple[SeriesMembership, ...]:
        if self.series_memberships:
            return self.series_memberships
        if self.series_name:
            return (SeriesMembership("", self.series_name, self.series_sequence),)
        return ()

    @property
    def display_title(self) -> str:
        title = self.title.strip()
        series = self.series_name.strip()
        if not series:
            return title
        prefix = f"{series}:"
        if title.casefold().startswith(prefix.casefold()):
            return title
        return f"{series}: {title}"

    @property
    def series_position(self) -> str:
        if not self.series_sequence or self.series_total < 2:
            return ""
        return f"{self.series_sequence}/{self.series_total}"


@dataclass
class AudioTrack:
    index: int
    start_offset: float
    duration: float
    url: str
    title: str
    file_id: int | None = None
    size_bytes: int | None = None


@dataclass
class Chapter:
    index: int
    title: str
    start: float
    end: float


@dataclass
class PlaybackSession:
    id: str
    library_item_id: str
    title: str
    author: str
    duration: float
    current_time: float
    cover_url: str
    tracks: list[AudioTrack]
    chapters: list[Chapter]
    play_method: int | None = None
    episode_id: str = ""
    progress_account: str = ""
    downloaded: bool = False
    download_directory: str = ""
    progress_updated_at: int = 0
    library_id: str = ""
    server_checkpoint: tuple[float, int] | None = None

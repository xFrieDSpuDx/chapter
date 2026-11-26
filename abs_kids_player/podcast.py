from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.error import URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from .api import DEFAULT_REQUEST_TIMEOUT_SECONDS
from .config import PodcastConfig, default_podcasts
from .models import AudioTrack, Book, Chapter, PlaybackSession
from .player_state import state_path
from .storage import write_json_atomically


YOTO_DAILY_BOOK_ID = "podcast:yoto-daily"
YOTO_DAILY_FALLBACK_FEED_URL = "https://feeds.acast.com/public/shows/62cebd180ce17d0012d3347b"
YOTO_DAILY_APPLE_ID = "1635154611"
YOTO_DAILY_APPLE_LOOKUP_URL = f"https://itunes.apple.com/lookup?id={YOTO_DAILY_APPLE_ID}&entity=podcast"
TRIVIA_FOR_KIDS_BOOK_ID = "podcast:trivia-for-kids"
TRIVIA_FOR_KIDS_APPLE_ID = "1603986433"
APPLE_BOOK_IDS = {
    YOTO_DAILY_APPLE_ID: YOTO_DAILY_BOOK_ID,
    TRIVIA_FOR_KIDS_APPLE_ID: TRIVIA_FOR_KIDS_BOOK_ID,
}
ITUNES_NS = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"
PODCAST_REQUEST_HEADERS = {
    "User-Agent": "ChapterAudiobookPlayer/1.0",
    "Accept": "application/rss+xml, application/xml, text/xml, application/json, */*",
}
_PROGRESS_LOCK = threading.Lock()


def known_podcast_fallbacks() -> list[PodcastConfig]:
    """Metadata used only when resolving explicitly added podcast URLs."""
    return [
        PodcastConfig(
            url="https://podcasts.apple.com/us/podcast/yoto-daily/id1635154611",
            title="Yoto Daily",
            author="Yoto",
            feed_url=YOTO_DAILY_FALLBACK_FEED_URL,
            book_id=YOTO_DAILY_BOOK_ID,
        ),
        PodcastConfig(
            url="https://podcasts.apple.com/us/podcast/trivia-for-kids/id1603986433",
            title="Trivia for Kids",
            author="KRCreative",
            feed_url="https://rss.pdrl.fm/920abb/feeds.libsyn.com/529502/rss/?redirect=false",
            book_id=TRIVIA_FOR_KIDS_BOOK_ID,
        ),
    ]


@dataclass(frozen=True)
class PodcastEpisode:
    title: str
    audio_url: str
    duration: float = 0.0
    guid: str = ""
    cover_url: str = ""


class PodcastError(RuntimeError):
    pass


class PodcastProgressClient:
    def __init__(self, account: str = "", episode_id: str = "") -> None:
        self.account = account
        self.episode_id = episode_id

    def update_progress(
        self,
        item_id: str,
        current_time: float,
        duration: float,
        session_id: str = "",
        time_listened: float = 0,
    ) -> None:
        if not self.account or not self.episode_id:
            return
        with _PROGRESS_LOCK:
            progress = podcast_progress()
            progress[podcast_progress_key(self.account, item_id, self.episode_id)] = {
                "currentTime": current_time,
                "lastUpdate": time.time_ns() // 1_000_000,
                "isFinished": bool(duration and current_time / duration >= 0.995),
            }
            write_json_atomically(state_path().with_name("podcast-progress.json"), progress)


def podcast_progress() -> dict:
    try:
        data = json.loads(state_path().with_name("podcast-progress.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def podcast_progress_key(account: str, book_id: str, episode_id: str) -> str:
    return f"{account}/{book_id}/{episode_id}"


def podcast_episode_id(episode: PodcastEpisode) -> str:
    return hashlib.sha256((episode.guid or episode.audio_url).encode()).hexdigest()


def rss_episode_books(podcast: PodcastConfig, account: str) -> list[Book]:
    show = podcast_book(podcast)
    saved = podcast_progress()
    result = []
    for episode in read_podcast_episodes(resolve_podcast_feed_url(podcast)):
        key = podcast_episode_id(episode)
        progress = saved.get(podcast_progress_key(account, show.id, key), {})
        result.append(Book(
            f"{show.id}/{key}", episode.title, show.author, episode.duration, episode.cover_url,
            current_time=float(progress.get("currentTime") or 0),
            progress_updated_at=int(progress.get("lastUpdate") or 0),
            is_finished=bool(progress.get("isFinished")),
            episode_id=key, podcast_title=show.title, source_item_id=show.id,
        ))
    return result


def yoto_daily_book() -> Book:
    return podcast_book(known_podcast_fallbacks()[0])


def podcast_books(podcasts: list[PodcastConfig] | tuple[PodcastConfig, ...] | None = None) -> list[Book]:
    return [podcast_book(podcast) for podcast in (podcasts if podcasts is not None else default_podcasts())]


def podcast_book(podcast: PodcastConfig) -> Book:
    return Book(
        id=configured_podcast_book_id(podcast),
        title=podcast.title.strip() or podcast_title_from_url(podcast.url or podcast.feed_url),
        author=podcast.author.strip(),
        duration=0,
        cover_url="",
    )


def is_podcast_book_id(book_id: str, podcasts: list[PodcastConfig] | None = None) -> bool:
    if podcasts is None:
        return book_id.startswith("podcast:")
    return podcast_by_book_id(book_id, podcasts) is not None


def podcast_by_book_id(book_id: str, podcasts: list[PodcastConfig]) -> PodcastConfig | None:
    for podcast in podcasts:
        if configured_podcast_book_id(podcast) == book_id:
            return podcast
    return None


def latest_yoto_daily_session(resume_time: float = 0.0, start_over: bool = False) -> PlaybackSession:
    return latest_podcast_session(known_podcast_fallbacks()[0], resume_time=resume_time, start_over=start_over)


def latest_podcast_session(
    podcast_or_book_id: PodcastConfig | str,
    resume_time: float = 0.0,
    start_over: bool = False,
    *,
    episode_id: str = "",
    account: str = "",
) -> PlaybackSession:
    podcast = podcast_or_book_id
    if isinstance(podcast, str):
        resolved = podcast_by_book_id(podcast, known_podcast_fallbacks())
        if resolved is None:
            raise PodcastError("Podcast is not configured.")
        podcast = resolved

    feed_url = resolve_podcast_feed_url(podcast)
    if episode_id:
        episode = next((episode for episode in read_podcast_episodes(feed_url)
                        if podcast_episode_id(episode) == episode_id), None)
        if episode is None:
            raise PodcastError("Episode is no longer in the feed.")
    else:
        episode = latest_episode(feed_url)
    key = podcast_episode_id(episode)
    show_id = configured_podcast_book_id(podcast)
    saved = podcast_progress().get(podcast_progress_key(account, show_id, key), {}) if account else {}
    fallback = resume_time if episode_id or not account else 0
    position = saved.get("currentTime", fallback)
    current_time = 0.0 if start_over else max(0.0, float(position))
    duration = max(episode.duration, current_time)
    return PlaybackSession(
        id=episode.guid or configured_podcast_book_id(podcast),
        library_item_id=configured_podcast_book_id(podcast),
        title=episode.title if episode_id else podcast.title.strip() or podcast_title_from_url(podcast.url or podcast.feed_url),
        author=podcast.author.strip(),
        duration=duration,
        current_time=current_time,
        cover_url=episode.cover_url,
        tracks=[
            AudioTrack(
                index=1,
                start_offset=0,
                duration=duration,
                url=episode.audio_url,
                title=episode.title or podcast.title or "Latest episode",
            )
        ],
        chapters=[
            Chapter(
                index=1,
                title=episode.title or "Latest episode",
                start=0,
                end=duration,
            )
        ],
        episode_id=key,
        progress_account=account,
    )


def resolve_podcast_config(source_url: str) -> PodcastConfig:
    url = source_url.strip()
    if not valid_http_url(url):
        raise PodcastError("Podcast URL must start with http:// or https://.")

    apple_id = apple_podcast_id(url)
    if apple_id:
        return resolve_apple_podcast_config(url, apple_id)

    title, author = podcast_feed_metadata(url)
    return PodcastConfig(
        url=url,
        title=title or podcast_title_from_url(url),
        author=author,
        feed_url=url,
        book_id=podcast_book_id(url),
    )


def configured_podcast_book_id(podcast: PodcastConfig) -> str:
    return podcast.book_id.strip() or podcast_book_id(podcast.url or podcast.feed_url)


def podcast_book_id(url: str) -> str:
    apple_id = apple_podcast_id(url)
    if apple_id:
        return APPLE_BOOK_IDS.get(apple_id, f"podcast:apple:{apple_id}")
    digest = hashlib.sha1(url.strip().lower().encode("utf-8")).hexdigest()[:12]
    return f"podcast:rss:{digest}"


def apple_podcast_id(url: str) -> str:
    parsed = urlparse(url.strip())
    match = re.search(r"/id(\d+)(?:[/?#]|$)", parsed.path)
    if match:
        return match.group(1)
    query_id = parse_qs(parsed.query).get("id", [""])[0]
    return query_id.strip() if query_id.isdigit() else ""


def valid_http_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def resolve_apple_podcast_config(source_url: str, apple_id: str) -> PodcastConfig:
    try:
        result = apple_podcast_lookup(apple_id)
    except PodcastError:
        fallback = default_podcast_for_apple_id(apple_id)
        if fallback is None:
            raise
        return PodcastConfig(
            url=source_url,
            title=fallback.title,
            author=fallback.author,
            feed_url=fallback.feed_url,
            book_id=fallback.book_id,
        )

    feed_url = str(result.get("feedUrl") or "").strip()
    if not feed_url:
        fallback = default_podcast_for_apple_id(apple_id)
        if fallback is None:
            raise PodcastError("Apple Podcasts did not provide an RSS feed for that show.")
        feed_url = fallback.feed_url

    return PodcastConfig(
        url=source_url,
        title=str(result.get("collectionName") or result.get("trackName") or "").strip()
        or podcast_title_from_url(source_url),
        author=str(result.get("artistName") or "").strip(),
        feed_url=feed_url,
        book_id=podcast_book_id(source_url),
    )


def apple_podcast_lookup(apple_id: str) -> dict:
    lookup_url = f"https://itunes.apple.com/lookup?id={apple_id}&entity=podcast"
    try:
        with open_podcast_url(lookup_url) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, json.JSONDecodeError) as error:
        raise PodcastError("Apple Podcasts lookup failed.") from error

    for result in data.get("results", []):
        if str(result.get("feedUrl") or "").strip():
            return result
    raise PodcastError("Apple Podcasts did not provide an RSS feed for that show.")


def default_podcast_for_apple_id(apple_id: str) -> PodcastConfig | None:
    for podcast in known_podcast_fallbacks():
        if apple_podcast_id(podcast.url) == apple_id:
            return podcast
    return None


def resolve_yoto_daily_feed_url() -> str:
    try:
        return str(apple_podcast_lookup(YOTO_DAILY_APPLE_ID).get("feedUrl") or "").strip()
    except PodcastError:
        return YOTO_DAILY_FALLBACK_FEED_URL


def resolve_podcast_feed_url(podcast: PodcastConfig) -> str:
    if podcast.feed_url.strip():
        return podcast.feed_url.strip()
    apple_id = apple_podcast_id(podcast.url)
    if apple_id:
        try:
            return str(apple_podcast_lookup(apple_id).get("feedUrl") or "").strip()
        except PodcastError:
            fallback = default_podcast_for_apple_id(apple_id)
            if fallback is not None and fallback.feed_url:
                return fallback.feed_url
            raise
    return podcast.url.strip()


def podcast_feed_metadata(feed_url: str) -> tuple[str, str]:
    try:
        with open_podcast_url(feed_url) as response:
            xml = response.read()
    except (OSError, URLError) as error:
        raise PodcastError("Podcast feed unavailable.") from error

    try:
        root = ET.fromstring(xml)
    except ET.ParseError as error:
        raise PodcastError("Podcast feed could not be read.") from error

    channel = root.find("./channel")
    if channel is None:
        raise PodcastError("Podcast feed could not be read.")
    title = text_from_child(channel, "title")
    author = (
        text_from_child(channel, f"{ITUNES_NS}author")
        or text_from_child(channel, "managingEditor")
        or text_from_child(channel, "webMaster")
    )
    return title, author


def read_podcast_episodes(feed_url: str) -> list[PodcastEpisode]:
    try:
        with open_podcast_url(feed_url) as response:
            xml = response.read()
    except (OSError, URLError) as error:
        raise PodcastError("Podcast feed unavailable.") from error

    try:
        root = ET.fromstring(xml)
    except ET.ParseError as error:
        raise PodcastError("Podcast feed could not be read.") from error

    episodes: list[PodcastEpisode] = []
    for item in root.findall("./channel/item"):
        enclosure = item.find("enclosure")
        audio_url = enclosure.get("url", "").strip() if enclosure is not None else ""
        if not valid_http_url(audio_url):
            continue
        episodes.append(PodcastEpisode(
            title=text_from_child(item, "title") or "Untitled episode",
            audio_url=audio_url,
            duration=parse_duration(text_from_child(item, f"{ITUNES_NS}duration")),
            guid=text_from_child(item, "guid"),
            cover_url=podcast_image_url(item),
        ))
    if not episodes:
        raise PodcastError("Podcast feed has no playable episodes.")
    return episodes


def latest_episode(feed_url: str) -> PodcastEpisode:
    return read_podcast_episodes(feed_url)[0]


def text_from_child(parent: ET.Element, name: str) -> str:
    child = parent.find(name)
    if child is None or child.text is None:
        return ""
    return child.text.strip()


def podcast_image_url(item: ET.Element) -> str:
    image = item.find(f"{ITUNES_NS}image")
    if image is None:
        return ""
    return image.get("href", "").strip()


def podcast_title_from_url(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.rstrip("/").split("/")[-1]
    label = path or parsed.netloc or "Podcast"
    label = re.sub(r"[-_]+", " ", label)
    return label.strip().title() or "Podcast"


def open_podcast_url(url: str):
    request = Request(url, headers=PODCAST_REQUEST_HEADERS)
    return urlopen(request, timeout=DEFAULT_REQUEST_TIMEOUT_SECONDS)


def parse_duration(value: str) -> float:
    value = value.strip()
    if not value:
        return 0.0
    if value.isdigit():
        return float(value)
    parts = value.split(":")
    if len(parts) == 2 and all(part.isdigit() for part in parts):
        return float(int(parts[0]) * 60 + int(parts[1]))
    if len(parts) == 3 and all(part.isdigit() for part in parts):
        return float(int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2]))
    match = re.match(r"(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:(\d+)s)?$", value.lower())
    if match and any(match.groups()):
        hours, minutes, seconds = (int(part or 0) for part in match.groups())
        return float(hours * 3600 + minutes * 60 + seconds)
    return 0.0

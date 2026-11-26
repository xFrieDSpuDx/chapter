"""Apply a bookmark lookup only to the OLED picker that requested it."""

from concurrent.futures import Future

from .bookmarks import Bookmark, request_bookmarks
from .config import AppConfig
from .models import PlaybackSession
from .progress_sync import progress_account
from .rotary_ui import ApplianceMenu, Screen


class OledBookmarkPicker:
    def __init__(self) -> None:
        self._pending: tuple[PlaybackSession, Future[list[Bookmark] | None]] | None = None

    def open(self, config: AppConfig, menu: ApplianceMenu) -> bool:
        session = menu.session
        if session is None:
            self._pending = None
            return False
        self._pending = session, request_bookmarks(config, session)
        return True

    def apply_ready(self, config: AppConfig, menu: ApplianceMenu) -> bool:
        if self._pending is None:
            return False
        session, result = self._pending
        if menu.screen != Screen.BOOKMARK_PICKER or menu.session is not session:
            self._pending = None
            return False
        if progress_account(config) != session.progress_account:
            self._pending = None
            menu.set_bookmarks(None)
            return True
        if not result.done():
            return False
        self._pending = None
        menu.set_bookmarks(result.result())
        return True

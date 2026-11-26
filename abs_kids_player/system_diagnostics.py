"""Collect the local measurements that can be shared in a diagnostic report."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass

from .build_info import BuildInfo, installed_build
from .catalogue_settings import load_catalogue_status
from .config import AppConfig
from .download_transfer import MIB, storage_usage
from .playback_startup_history import StartupSample, load_startup_history
from .progress_sync import (
    ProgressSyncStatus,
    load_progress_sync_status,
    progress_account,
    read_pending_progress,
)


@dataclass(frozen=True)
class StorageStatus:
    free_bytes: int | None
    download_bytes: int | None
    download_limit_bytes: int


@dataclass(frozen=True)
class SystemDiagnostics:
    collected_at: float
    build: BuildInfo
    audiobookshelf_configured: bool
    last_library_refresh_at: float | None
    library_refresh_result: str
    progress_sync: ProgressSyncStatus
    pending_progress_count: int
    storage: StorageStatus
    playback_startups: list[StartupSample]


def collect_system_diagnostics(config: AppConfig) -> SystemDiagnostics:
    catalogue = load_catalogue_status(config)
    if catalogue.error:
        refresh_result = "failed"
    else:
        refresh_result = "succeeded" if catalogue.last_success_at else "not_recorded"
    account = progress_account(config)
    try:
        used, free = storage_usage()
    except (OSError, StopIteration):
        used, free = None, None
    return SystemDiagnostics(
        collected_at=time.time(),
        build=installed_build(),
        audiobookshelf_configured=config.is_ready,
        last_library_refresh_at=catalogue.last_success_at or None,
        library_refresh_result=refresh_result,
        progress_sync=load_progress_sync_status(account),
        pending_progress_count=sum(key[0] == account for key in read_pending_progress()) if account else 0,
        storage=StorageStatus(free, used, config.download_limit_mib * MIB),
        playback_startups=load_startup_history(),
    )


def diagnostic_payload(report: SystemDiagnostics) -> dict:
    # Only fields from this deliberately limited report are serialized.
    return {"schema_version": 1, **asdict(report)}

"""Preload plugins for an MP4 audiobook without constructing a playback pipeline."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def preferred_aac_decoder(gst: Any):
    # qtdemux supplies raw AAC frames for an AAC track in an MP4 container.
    caps = gst.Caps.from_string("audio/mpeg, mpegversion=(int)4, stream-format=(string)raw")
    audio_decoders = gst.ELEMENT_FACTORY_TYPE_DECODER | gst.ELEMENT_FACTORY_TYPE_MEDIA_AUDIO
    factories = gst.ElementFactory.list_get_elements(audio_decoders, gst.Rank.MARGINAL)
    return next((factory for factory in factories if factory.can_sink_any_caps(caps)), None)


def preload_audiobook_plugins(gst: Any, playback_requested: Callable[[], bool]) -> None:
    for role, find_factory in (
        ("mp4_reader", lambda: gst.ElementFactory.find("qtdemux")),
        ("aac_decoder", lambda: preferred_aac_decoder(gst)),
    ):
        if playback_requested():
            print(f"GStreamer preload role={role} status=yielded")
            return
        try:
            factory = find_factory()
            if factory is None:
                print(f"GStreamer preload role={role} status=unavailable")
                continue
            if playback_requested():
                print(f"GStreamer preload role={role} status=yielded")
                return
            name = factory.get_name()
            status = "loaded" if factory.load() is not None else "unavailable"
            print(f"GStreamer preload role={role} factory={name} status={status}")
        except Exception as error:
            # Normal GStreamer plugin selection still runs when a book opens.
            print(f"GStreamer preload role={role} status=failed error={type(error).__name__}")

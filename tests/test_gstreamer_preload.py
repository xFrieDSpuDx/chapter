import unittest
from unittest.mock import Mock, patch

from abs_kids_player.gstreamer_preload import preload_audiobook_plugins


class AudiobookPluginPreloadTest(unittest.TestCase):
    def setUp(self):
        self.gst = Mock(ELEMENT_FACTORY_TYPE_DECODER=1, ELEMENT_FACTORY_TYPE_MEDIA_AUDIO=8)
        self.reader = self.gst.ElementFactory.find.return_value
        self.reader.get_name.return_value = "qtdemux"
        self.decoder = Mock()
        self.decoder.get_name.return_value = "platform_aac"
        self.decoder.can_sink_any_caps.return_value = True
        self.gst.ElementFactory.list_get_elements.return_value = [self.decoder]

    def test_preloads_reader_and_highest_ranked_matching_decoder_without_creating_elements(self):
        wrong_format = Mock()
        wrong_format.can_sink_any_caps.return_value = False
        lower_ranked = Mock()
        self.gst.ElementFactory.list_get_elements.return_value = [wrong_format, self.decoder, lower_ranked]
        with patch("builtins.print") as log:
            preload_audiobook_plugins(self.gst, lambda: False)
        self.gst.ElementFactory.find.assert_called_once_with("qtdemux")
        self.gst.ElementFactory.list_get_elements.assert_called_once_with(9, self.gst.Rank.MARGINAL)
        self.decoder.can_sink_any_caps.assert_called_once_with(self.gst.Caps.from_string.return_value)
        self.reader.load.assert_called_once_with()
        self.decoder.load.assert_called_once_with()
        wrong_format.load.assert_not_called()
        lower_ranked.load.assert_not_called()
        self.gst.ElementFactory.make.assert_not_called()
        log.assert_any_call("GStreamer preload role=aac_decoder factory=platform_aac status=loaded")

    def test_missing_reader_and_no_matching_decoder_are_optional(self):
        self.gst.ElementFactory.find.return_value = None
        self.decoder.can_sink_any_caps.return_value = False
        with patch("builtins.print") as log:
            preload_audiobook_plugins(self.gst, lambda: False)
        self.decoder.load.assert_not_called()
        log.assert_any_call("GStreamer preload role=mp4_reader status=unavailable")
        log.assert_any_call("GStreamer preload role=aac_decoder status=unavailable")

    def test_failed_reader_does_not_prevent_decoder_attempt(self):
        self.reader.load.side_effect = RuntimeError("plugin unavailable")
        self.decoder.load.return_value = None
        with patch("builtins.print") as log:
            preload_audiobook_plugins(self.gst, lambda: False)
        self.decoder.load.assert_called_once_with()
        log.assert_any_call("GStreamer preload role=mp4_reader status=failed error=RuntimeError")
        log.assert_any_call("GStreamer preload role=aac_decoder factory=platform_aac status=unavailable")

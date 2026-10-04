"""Desktop UI regression checks without starting the assistant or audio devices."""

import os
import threading
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QLabel, QPushButton

import ui
from desktop_chat import ChatSurface, ChatTranscript


class DesktopChatTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.preferences = {}
        self.settings = Mock()
        self.settings.value.side_effect = lambda key, default=None: self.preferences.get(key, default)
        self.settings.setValue.side_effect = self.preferences.__setitem__
        with patch.object(ui, "_read_full_config", return_value={}), \
             patch.object(ui.MainWindow, "_check_config", return_value=True), \
             patch.object(ui.MainWindow, "_check_autostart", return_value=False), \
             patch.object(ui, "QSettings", return_value=self.settings):
            self.window = ui.MainWindow("")
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def test_main_window_uses_chat_and_renders_at_minimum_size(self):
        self.assertIsInstance(self.window.hud, ChatSurface)
        self.assertIsInstance(self.window._log, ChatTranscript)
        self.assertFalse(self.window._right_panel.isVisible())
        self.window.resize(820, 580)
        self.app.processEvents()
        self.assertTrue(self.window._input.isVisible())
        self.assertTrue(self.window._mute_btn.isVisible())
        self.assertFalse(self.window.grab().isNull())

    def test_messages_are_plain_text_and_system_logs_do_not_hide_welcome(self):
        self.window._log.append_log("SYS: Startup complete")
        self.assertTrue(self.window._log._welcome.isVisible())
        self.window._log_sig.emit("You: <b>Keep this literal</b>")
        self.window._log_sig.emit("JARVIS: Hello there")
        self.assertFalse(self.window._log._welcome.isVisible())
        texts = [label.text() for label in self.window._log.findChildren(QLabel)]
        self.assertIn("<b>Keep this literal</b>", texts)
        self.assertIn("Hello there", texts)

    def test_send_still_calls_engine(self):
        received = []
        done = threading.Event()

        def command(text):
            received.append(text)
            done.set()

        self.window.on_text_command = command
        self.window._input.setText("Help me plan today")
        self.window._send()
        self.assertTrue(done.wait(2))
        self.assertEqual(received, ["Help me plan today"])
        self.assertEqual(self.window._input.text(), "")

    def test_state_mute_and_interrupt_remain_connected(self):
        self.window._state_sig.emit("SPEAKING")
        self.assertEqual(self.window._chat_status.text(), "Speaking…")
        self.assertTrue(self.window.hud.speaking)
        self.window._toggle_mute()
        self.assertTrue(self.window._muted)
        self.assertEqual(self.window._mute_btn.text(), "Unmute mic")
        called = []
        self.window.on_interrupt = lambda: called.append(True)
        self.window._interrupt_btn.click()
        self.assertEqual(called, [True])

    def test_settings_content_and_confirmation_still_open(self):
        self.window._drawer_btn.click()
        self.assertTrue(self.window._quick_drawer.isVisible())
        self.window._show_content("Result", "Example result")
        self.assertTrue(self.window._content_panel.isVisible())
        self.window._show_confirm_banner("Confirm action", "Example action")
        self.assertTrue(self.window._confirm_overlay.isVisible())
        self.window._hide_confirm_banner()

    def test_theme_switch_preserves_voice_state_and_existing_conversation(self):
        self.window._log.append_log("You: Keep my conversation")
        self.window._input.setText("Unsent message")
        self.window._toggle_mute()
        callback = Mock()
        self.window.on_interrupt = callback
        self.window._toggle_chat_theme()
        self.assertEqual(self.preferences["appearance"], "dark")
        self.assertIn("#212121", self.window.centralWidget().styleSheet())
        self.assertTrue(self.window._muted)
        self.assertEqual(self.window.hud.state, "MUTED")
        self.assertEqual(self.window._input.text(), "Unsent message")
        callback.assert_not_called()
        self.window._log.append_log("JARVIS: A new response")
        labels = self.window._log.findChildren(QLabel)
        response = next(label for label in labels if label.text() == "A new response")
        self.assertIn("#ececec", response.styleSheet())
        self.window._toggle_chat_theme()
        self.assertEqual(self.preferences["appearance"], "light")
        self.assertIn("#252525", response.styleSheet())
        self.assertTrue(self.window._muted)

    def test_audio_feedback_is_worker_safe_and_does_not_report_playback_as_input(self):
        facade = ui.JarvisUI.__new__(ui.JarvisUI)
        facade._win = self.window
        self.window._apply_state("LISTENING")
        worker = threading.Thread(target=lambda: facade.set_audio_level(0.65))
        worker.start()
        worker.join(2)
        self.window._refresh_mic_indicator()
        self.assertEqual(self.window._mic_meter.value(), 65)
        self.assertFalse(facade.muted)
        self.window._apply_state("SPEAKING")
        facade.set_audio_level(0.9)
        self.window._refresh_mic_indicator()
        self.assertEqual(self.window._mic_meter.value(), 0)
        self.assertEqual(self.window._mic_indicator.text(), "Mic paused")
        self.window._toggle_mute()
        self.window._refresh_mic_indicator()
        self.assertEqual(self.window._mic_indicator.text(), "Mic muted")

    def test_saved_dark_mode_is_restored_on_startup(self):
        self.preferences["appearance"] = "dark"
        with patch.object(ui, "_read_full_config", return_value={}), \
             patch.object(ui.MainWindow, "_check_config", return_value=True), \
             patch.object(ui.MainWindow, "_check_autostart", return_value=False), \
             patch.object(ui, "QSettings", return_value=self.settings):
            restored = ui.MainWindow("")
        try:
            self.assertTrue(restored._dark_mode)
            self.assertEqual(restored._theme_btn.text(), "Light mode")
            self.assertIn("#212121", restored.centralWidget().styleSheet())
        finally:
            restored.close()
            restored.deleteLater()

    def test_audio_dialog_follows_theme_and_does_not_change_devices_on_open(self):
        callback = Mock()
        self.window.on_audio_device_change = callback
        with patch('core.audio_devices.list_devices', return_value=['Test microphone']), \
             patch('memory.config_manager.get_input_device', return_value='Test microphone'), \
             patch('memory.config_manager.get_output_device', return_value=''):
            self.window._drawer_btn.click()
            self.window._open_audio_devices()
        panel = self.window._audio_overlay
        self.assertFalse(self.window._quick_drawer.isVisible())
        self.assertTrue(self.window._desktop_style.shade.isVisible())
        self.assertEqual(panel._in_box.currentData(), 'Test microphone')
        self.assertIn('#ffffff', panel.styleSheet())
        self.window._toggle_chat_theme()
        self.assertIn('#262626', panel.styleSheet())
        self.assertIn('#303030', panel._in_box.styleSheet())
        self.assertEqual(panel._in_box.currentData(), 'Test microphone')
        callback.assert_not_called()
        panel.hide()
        self.assertFalse(self.window._desktop_style.shade.isVisible())

    def test_settings_and_dynamic_controls_keep_modern_styles(self):
        self.window._drawer_btn.click()
        self.assertGreaterEqual(self.window._quick_drawer.width(), 280)
        self.window._update_brief_btn(False)
        self.assertNotIn('Courier', self.window._brief_btn.styleSheet())
        self.assertNotIn('#001a08', self.window._brief_btn.styleSheet())
        self.assertIn('off', self.window._brief_btn.text())
        for button in (self.window._theme_btn, self.window._drawer_btn):
            self.assertFalse(button.icon().isNull())

    def test_plugin_customization_and_confirmation_panels_are_themed(self):
        self.window._toggle_chat_theme()
        self.window._open_plugin_manager()
        plugin = self.window._plugin_manager_overlay
        self.assertIn('#262626', plugin.styleSheet())
        plugin.hide()
        with patch.object(ui, '_read_full_config', return_value={}):
            self.window._open_customize()
        custom = self.window._customize_overlay
        self.assertIn('#262626', custom.styleSheet())
        custom.hide()
        self.window._show_confirm_banner('Test action', 'Must still require an answer')
        confirm = self.window._confirm_overlay
        self.assertIn('#262626', confirm.styleSheet())
        cancel = next(b for b in confirm.findChildren(QPushButton) if b.text() == 'Cancel')
        self.assertTrue(cancel.isDefault())
        self.window._hide_confirm_banner()


if __name__ == "__main__":
    unittest.main()

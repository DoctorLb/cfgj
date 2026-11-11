"""Offline checks using real Qt widgets and mocked desktop input/listeners.

Run: python -m unittest discover -s tests -v
"""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
PROJECT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("cf_release", PROJECT / "release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)
QT_APP = release.QApplication.instance() or release.QApplication([])
CLIENT_RECT = (100, 50, 1024, 768)


class ValidationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="cf-test-")
        self.addCleanup(temp.cleanup)
        previous_cwd = os.getcwd()
        os.chdir(temp.name)
        self.addCleanup(os.chdir, previous_cwd)
        # 程序把 config.json / 按钮模板放在脚本目录，测试时指向临时目录
        patcher = patch.object(release, "APP_DIR", Path(temp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.windows, self.workers, self.inputs = [], [], {}
        for name in ("press", "moveTo", "click", "screenshot", "position"):
            patcher = patch.object(release.pyautogui, name)
            self.inputs[name] = patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(release.keyboard, "Listener")
        self.listener_class = patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("find_game_window", "focus_game_window", "game_is_foreground"):
            patcher = patch.object(release, name)
            value = True if name != "find_game_window" else 1234
            patcher.start().return_value = value
            self.addCleanup(patcher.stop)
        patcher = patch.object(release, "game_client_rect", return_value=CLIENT_RECT)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.close_windows)
        self.window = self.new_window()

    def click_point(self, ratio):
        return (round(100 + ratio[0] * 1024), round(50 + ratio[1] * 768))

    def new_window(self):
        window = release.MacroApp()
        window.custom_button_image = Path.cwd() / "summon_button_custom.png"
        window.monster_template_image = Path.cwd() / release.MONSTER_TEMPLATE_NAME
        self.windows.append(window)
        return window

    def make_monster_template(self):
        """一张有明显纹理的假怪物图，供真实 OpenCV 匹配使用。"""
        from PIL import Image, ImageDraw
        template = Image.new("RGB", release.MONSTER_TEMPLATE_SIZE, (20, 120, 60))
        draw = ImageDraw.Draw(template)
        for col in range(0, release.MONSTER_TEMPLATE_SIZE[0], 6):
            draw.rectangle((col, 0, col + 3, release.MONSTER_TEMPLATE_SIZE[1] - 1),
                           fill=(230, 235, 240))
        draw.ellipse((20, 20, 100, 100), fill=(200, 40, 40))
        template.save(self.window.monster_template_image)
        return template

    def close_windows(self):
        for window in self.windows:
            window.run_event.clear()
            window.aim_event.clear()
        for worker in self.workers:
            worker.join(timeout=1)
        for window in self.windows:
            if window._marker is not None:
                window._marker.close()
            window.close()
            window.deleteLater()
        QT_APP.processEvents()

    def test_config_path_lives_next_to_program(self):
        self.assertEqual(Path(self.window.config_path),
                         Path(release.APP_DIR) / "config.json")

    def test_source_syntax(self):
        for path in PROJECT.glob("*.py"):
            with self.subTest(file=path.name):
                compile(path.read_bytes(), str(path), "exec")

    def test_ui_defaults_and_bounds(self):
        self.assertEqual(self.window.post_summon_seconds, 5.0)
        self.assertEqual(self.window.post_summon_delay.value(), 5.0)
        self.window.post_summon_delay.setValue(-10)
        self.assertEqual(self.window.post_summon_delay.value(), 0)
        self.window.post_summon_delay.setValue(100)
        self.assertEqual(self.window.post_summon_delay.value(), 60)
        self.assertEqual(self.window.Statistics.text(), "1000")

    def test_ui_has_no_removed_features(self):
        for attr in ("shoot_event", "loop_event", "_exec_lock",
                     "handl_f9", "handle_no_dps", "execute_sequence", "check_image",
                     "start_detection_loop", "save_loop_state", "card_delay_range",
                     "card_delay_min", "card_delay_max"):
            with self.subTest(attr=attr):
                self.assertFalse(hasattr(self.window, attr))

    def test_unsaved_ui_does_not_change_active_wait(self):
        self.window.post_summon_delay.setValue(12)
        self.assertEqual(self.window.post_summon_seconds, 5.0)

    def test_save_and_reload(self):
        self.window.post_summon_delay.setValue(4.5)
        self.window.card_ratio = (0.3, 0.4)
        self.window.summon_ratio = (0.7, 0.8)
        self.window.save_config()
        self.assertEqual(self.window.post_summon_seconds, 4.5)
        reloaded = self.new_window()
        self.assertEqual(reloaded.post_summon_seconds, 4.5)
        self.assertEqual(reloaded.post_summon_delay.value(), 4.5)
        self.assertEqual(reloaded.card_ratio, (0.3, 0.4))
        self.assertEqual(reloaded.summon_ratio, (0.7, 0.8))

    def test_save_writes_only_current_keys(self):
        self.window.save_config()
        config = json.loads(Path("config.json").read_text(encoding="utf-8"))
        self.assertEqual(set(config), {
            "post_summon_seconds", "card_point_ratio",
            "summon_point_ratio", "button_template_client_width",
            "aim_gain", "aim_template_client_width",
        })

    def test_invalid_stored_waits_fall_back(self):
        for value in (-1, 61, None, "bad", float("nan"), float("inf")):
            with self.subTest(value=value):
                Path("config.json").write_text(json.dumps({
                    "post_summon_seconds": value,
                }), encoding="utf-8")
                self.assertEqual(self.new_window().post_summon_seconds, 5.0)

    def test_invalid_stored_ratios_fall_back(self):
        for card, summon in (([2, 0.5], [-1, 0.5]), ("bad", [0.5]),
                             ([0.1], "bad"), ([0.2, 0.3, 0.4], [True, 0.5])):
            with self.subTest(card=card, summon=summon):
                Path("config.json").write_text(json.dumps({
                    "card_point_ratio": card, "summon_point_ratio": summon,
                }), encoding="utf-8")
                window = self.new_window()
                self.assertEqual(window.card_ratio, release.DEFAULT_CARD_RATIO)
                self.assertEqual(window.summon_ratio, release.DEFAULT_SUMMON_RATIO)

    def test_invalid_template_width_falls_back(self):
        for value in (0, -5, "1024", 1.5, True, None):
            with self.subTest(value=value):
                Path("config.json").write_text(json.dumps({
                    "button_template_client_width": value,
                }), encoding="utf-8")
                self.assertEqual(self.new_window().template_client_width, 1024)

    def test_wait_elapsed_time(self):
        self.window.run_event.set()
        started = time.monotonic()
        self.assertTrue(self.window.interruptible_sleep(0.08))
        self.assertGreaterEqual(time.monotonic() - started, 0.08)

    def test_wait_is_interrupted(self):
        self.window.run_event.set()
        threading.Timer(0.05, self.window.run_event.clear).start()
        started = time.monotonic()
        self.assertFalse(self.window.interruptible_sleep(2))
        self.assertLess(time.monotonic() - started, 1)

    def test_ratio_maps_to_current_client_rect(self):
        self.window.card_ratio = (0.25, 0.5)
        self.window.summon_ratio = (0.75, 0.25)
        self.assertEqual(self.window.card_point(), (356, 434))
        self.assertEqual(self.window.summon_point(), (868, 242))

    def test_f6_records_card_ratio_and_saves(self):
        self.inputs["position"].return_value = (600, 500)
        self.window.capture_custom_position("card")
        self.assertEqual(self.window.card_ratio, (round(500 / 1024, 4), round(450 / 768, 4)))
        config = json.loads(Path("config.json").read_text(encoding="utf-8"))
        self.assertEqual(config["card_point_ratio"], list(self.window.card_ratio))
        self.inputs["screenshot"].assert_not_called()

    def test_f7_records_summon_ratio_and_button_crop(self):
        from PIL import Image
        self.inputs["position"].return_value = (600, 500)
        self.inputs["screenshot"].return_value = Image.new("RGB", (80, 20))
        self.window.capture_custom_position("summon")
        self.inputs["screenshot"].assert_called_once_with(region=(560, 490, 80, 20))
        self.assertTrue(self.window.custom_button_image.is_file())
        self.assertEqual(self.window.summon_ratio, (round(500 / 1024, 4), round(450 / 768, 4)))
        self.assertEqual(self.window.template_client_width, 1024)
        config = json.loads(Path("config.json").read_text(encoding="utf-8"))
        self.assertEqual(config["button_template_client_width"], 1024)

    def test_capture_outside_game_window_is_rejected(self):
        self.inputs["position"].return_value = (10, 10)
        self.window.capture_custom_position("card")
        self.assertEqual(self.window.card_ratio, release.DEFAULT_CARD_RATIO)
        self.assertIn("游戏画面内", self.window.log.toPlainText())

    def test_capture_near_edge_is_rejected_for_button(self):
        self.inputs["position"].return_value = (110, 60)
        self.window.capture_custom_position("summon")
        self.assertEqual(self.window.summon_ratio, release.DEFAULT_SUMMON_RATIO)
        self.assertIn("边缘太近", self.window.log.toPlainText())

    def test_capture_is_blocked_during_automation(self):
        self.window.run_event.set()
        self.window.capture_custom_position("card")
        self.inputs["position"].assert_not_called()
        self.assertEqual(self.window.card_ratio, release.DEFAULT_CARD_RATIO)
        self.assertIn("先按 F11 停止", self.window.log.toPlainText())

    def test_capture_hotkey_ignores_key_repeat(self):
        self.window.capture_point_signal.disconnect()
        requests = []
        self.window.capture_point_signal.connect(requests.append)
        self.window.on_key_press(release.Key.f6)
        self.window.on_key_press(release.Key.f6)
        self.window.on_key_release(release.Key.f6)
        self.window.on_key_press(release.Key.f6)
        self.window.on_key_press(release.Key.f7)
        self.assertEqual(requests, ["card", "card", "summon"])

    def test_reset_positions_restores_defaults(self):
        self.window.card_ratio = (0.1, 0.2)
        self.window.summon_ratio = (0.3, 0.4)
        self.window.template_client_width = 800
        self.window.aim_gain_spin.setValue(2.5)
        self.make_monster_template()
        self.window.reset_custom_positions()
        self.assertEqual(self.window.card_ratio, release.DEFAULT_CARD_RATIO)
        self.assertEqual(self.window.summon_ratio, release.DEFAULT_SUMMON_RATIO)
        self.assertEqual(self.window.template_client_width, 1024)
        self.assertEqual(self.window.aim_gain, release.DEFAULT_AIM_GAIN)
        config = json.loads(Path("config.json").read_text(encoding="utf-8"))
        self.assertEqual(config["card_point_ratio"], list(release.DEFAULT_CARD_RATIO))
        self.assertEqual(config["aim_gain"], release.DEFAULT_AIM_GAIN)
        # 清除位置只恢复参数，不删除已生成的模板
        self.assertTrue(self.window.monster_template_image.is_file())

    def test_open_panel_is_not_toggled_closed(self):
        with patch.object(self.window, "summon_panel_visible", return_value=True):
            self.assertTrue(self.window.open_summon_panel())
        self.inputs["press"].assert_not_called()

    def test_closed_panel_retries_e_and_opens(self):
        self.window.run_event.set()
        with patch.object(self.window, "summon_panel_visible", return_value=False), \
                patch.object(self.window, "wait_for_panel", side_effect=[False, True]) as wait, \
                patch.object(self.window, "interruptible_sleep", return_value=True) as sleep:
            self.assertTrue(self.window.open_summon_panel())
        self.assertEqual(self.inputs["press"].call_count, 2)
        self.inputs["press"].assert_called_with("e")
        self.assertEqual(wait.call_count, 2)
        sleep.assert_not_called()

    def test_panel_failure_returns_false_after_all_attempts(self):
        self.window.run_event.set()
        with patch.object(self.window, "summon_panel_visible", return_value=False), \
                patch.object(self.window, "wait_for_panel", return_value=False) as wait, \
                patch.object(self.window, "interruptible_sleep", return_value=True) as sleep:
            self.assertFalse(self.window.open_summon_panel())
        self.assertEqual(self.inputs["press"].call_count, release.E_RETRY_ATTEMPTS)
        self.assertEqual([c.args[0] for c in wait.call_args_list], [1.0] * release.E_RETRY_ATTEMPTS)
        self.assertTrue(self.window._e_attempts_exhausted)
        sleep.assert_not_called()
        self.assertEqual(release.E_RETRY_WAIT, 1.0)

    def test_thirty_failed_e_presses_stop_loop(self):
        self.window.run_event.set()
        self.window.Card_statistic = 1000

        def exhaust():
            self.window._e_attempts_exhausted = True
            return False

        with patch.object(self.window, "open_summon_panel", side_effect=exhaust):
            self.assertFalse(self.window.execute_f11_cycle())
        self.assertFalse(self.window.run_event.is_set())
        self.inputs["moveTo"].assert_not_called()
        self.assertIn("自动退出循环", self.window.log.toPlainText())


    def test_stopped_loop_does_not_press_e(self):
        with patch.object(self.window, "summon_panel_visible", return_value=False):
            self.assertFalse(self.window.open_summon_panel())
        self.inputs["press"].assert_not_called()

    def test_full_cycle_clicks_card_then_summon_and_counts(self):
        self.window.run_event.set()
        self.window.Card_statistic = 5
        with patch.object(self.window, "open_summon_panel", return_value=True), \
                patch.object(self.window, "summon_panel_visible", return_value=True), \
                patch.object(self.window, "wait_for_panel_closed", return_value=True), \
                patch.object(self.window, "interruptible_sleep", return_value=True) as sleep:
            self.assertTrue(self.window.execute_f11_cycle())
        card = self.click_point(release.DEFAULT_CARD_RATIO)
        summon = self.click_point(release.DEFAULT_SUMMON_RATIO)
        self.assertEqual(
            [c.args for c in self.inputs["moveTo"].call_args_list],
            [(card[0], card[1]), (summon[0], summon[1])])
        self.assertEqual(self.inputs["click"].call_count, 2)
        self.assertEqual(self.window.Card_statistic_ed, 1)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [0.3, 5.0])
        self.assertIn("5 秒后立即开始下一轮", self.window.log.toPlainText())
        self.assertIn("已发送第 1/5 轮", self.window.log.toPlainText())

    def test_cycle_uses_configured_wait(self):
        self.window.run_event.set()
        self.window.post_summon_seconds = 4.2
        with patch.object(self.window, "open_summon_panel", return_value=True), \
                patch.object(self.window, "summon_panel_visible", return_value=True), \
                patch.object(self.window, "wait_for_panel_closed", return_value=True), \
                patch.object(self.window, "interruptible_sleep", return_value=True) as sleep:
            self.window.execute_f11_cycle()
        self.assertEqual(sleep.call_args_list[-1].args[0], 4.2)
        self.assertIn("4.2 秒后立即开始下一轮", self.window.log.toPlainText())

    def test_full_cycle_uses_captured_ratios(self):
        self.window.run_event.set()
        self.window.card_ratio = (0.2, 0.3)
        self.window.summon_ratio = (0.8, 0.9)
        with patch.object(self.window, "open_summon_panel", return_value=True), \
                patch.object(self.window, "summon_panel_visible", return_value=True), \
                patch.object(self.window, "wait_for_panel_closed", return_value=True), \
                patch.object(self.window, "interruptible_sleep", return_value=True):
            self.window.execute_f11_cycle()
        self.assertEqual(self.inputs["moveTo"].call_args_list[0].args, self.click_point((0.2, 0.3)))
        self.assertEqual(self.inputs["moveTo"].call_args_list[1].args, self.click_point((0.8, 0.9)))

    def test_panel_never_opens_does_not_stop_loop(self):
        self.window.run_event.set()
        with patch.object(self.window, "open_summon_panel", return_value=False), \
                patch.object(self.window, "interruptible_sleep", return_value=True) as sleep:
            self.assertTrue(self.window.execute_f11_cycle())
        self.assertTrue(self.window.run_event.is_set())
        self.inputs["moveTo"].assert_not_called()
        sleep.assert_called_once_with(1.0)
        self.assertIn("本轮跳过", self.window.log.toPlainText())
        self.assertIn("继续重试", self.window.log.toPlainText())

    def test_button_missing_after_card_prevents_summon(self):
        self.window.run_event.set()
        with patch.object(self.window, "open_summon_panel", return_value=True), \
                patch.object(self.window, "summon_panel_visible", return_value=False), \
                patch.object(self.window, "interruptible_sleep", return_value=True):
            self.assertTrue(self.window.execute_f11_cycle())
        self.assertEqual(self.inputs["moveTo"].call_count, 1)
        self.assertEqual(self.inputs["moveTo"].call_args.args,
                         self.click_point(release.DEFAULT_CARD_RATIO))
        self.assertTrue(self.window.run_event.is_set())

    def test_panel_not_closed_is_not_counted_and_continues(self):
        self.window.run_event.set()
        with patch.object(self.window, "open_summon_panel", return_value=True), \
                patch.object(self.window, "summon_panel_visible", return_value=True), \
                patch.object(self.window, "wait_for_panel_closed", return_value=False), \
                patch.object(self.window, "interruptible_sleep", return_value=True):
            self.assertTrue(self.window.execute_f11_cycle())
        self.assertEqual(self.window.Card_statistic_ed, 0)
        self.assertTrue(self.window.run_event.is_set())
        self.assertIn("本轮不计入", self.window.log.toPlainText())

    def test_stopped_before_cycle_does_not_click(self):
        with patch.object(self.window, "open_summon_panel", return_value=True) as panel:
            self.assertFalse(self.window.execute_f11_cycle())
        panel.assert_not_called()
        self.inputs["moveTo"].assert_not_called()
        self.assertEqual(self.window.Card_statistic_ed, 0)

    def test_f11_stop_after_card_prevents_summon(self):
        self.window.run_event.set()
        with patch.object(self.window, "open_summon_panel", return_value=True), \
                patch.object(self.window, "interruptible_sleep", return_value=False):
            self.assertFalse(self.window.execute_f11_cycle())
        self.assertEqual(self.inputs["moveTo"].call_count, 1)
        self.inputs["press"].assert_not_called()

    def test_click_failure_is_logged_and_raised(self):
        self.window.run_event.set()
        self.inputs["click"].side_effect = RuntimeError("input failed")
        with patch.object(self.window, "open_summon_panel", return_value=True), \
                patch.object(self.window, "summon_panel_visible", return_value=True), \
                patch.object(self.window, "interruptible_sleep", return_value=True):
            with self.assertRaises(RuntimeError):
                self.window.execute_f11_cycle()
        self.assertIn("点击失败", self.window.log.toPlainText())

    def test_failure_logging_backs_off_after_five(self):
        self.window.run_event.set()
        with patch.object(self.window, "interruptible_sleep", return_value=True) as sleep:
            for _ in range(5):
                self.window.note_failure("[识别] 测试失败")
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [1.0] * 4 + [3.0])
        self.assertTrue(self.window.run_event.is_set())
        self.assertIn("连续失败 5 次", self.window.log.toPlainText())

    def test_worker_repeats_until_count(self):
        self.window.Card_statistic = 2
        self.window.run_event.set()
        executed = []

        def cycle():
            executed.append(True)
            self.window.Card_statistic_ed += 1
            return True

        with patch.object(self.window, "execute_f11_cycle", side_effect=cycle), \
                patch.object(self.window, "interruptible_sleep") as sleep:
            self.window.auto_cycle_worker()
        self.assertEqual(len(executed), 2)
        self.assertEqual(self.window.Card_statistic_ed, 2)
        self.assertFalse(self.window.run_event.is_set())
        sleep.assert_not_called()
        self.assertIn("已完成设定的循环次数", self.window.log.toPlainText())

    def test_worker_stops_when_cancelled(self):
        self.window.Card_statistic = 2
        self.window.run_event.set()

        def cycle():
            self.window.Card_statistic_ed += 1
            self.window.run_event.clear()
            return True

        with patch.object(self.window, "execute_f11_cycle", side_effect=cycle):
            self.window.auto_cycle_worker()
        self.assertEqual(self.window.Card_statistic_ed, 1)
        self.assertFalse(self.window.run_event.is_set())

    def test_worker_logs_unexpected_exception_and_stops(self):
        self.window.Card_statistic = 5
        self.window.run_event.set()
        with patch.object(self.window, "execute_f11_cycle", side_effect=RuntimeError("boom")):
            self.window.auto_cycle_worker()
        self.assertFalse(self.window.run_event.is_set())
        self.assertIn("循环异常: boom", self.window.log.toPlainText())

    def test_start_requires_count_and_game_window(self):
        with patch.object(release.threading, "Thread") as worker:
            self.window.Card_statistic = 0
            self.window.start_auto_cycle()
            self.window.Card_statistic = 10
            release.find_game_window.return_value = None
            self.window.start_auto_cycle()
        worker.assert_not_called()
        self.assertFalse(self.window.run_event.is_set())
        self.assertIn("未找到游戏窗口", self.window.log.toPlainText())

    def test_invalid_count_text_starts_nothing(self):
        self.window.Statistics.setText("abc")
        with patch.object(release.threading, "Thread") as worker:
            self.window.handle_f11()
        worker.assert_not_called()
        self.assertFalse(self.window.run_event.is_set())
        self.assertIn("必须大于 0", self.window.log.toPlainText())

    def test_start_stores_count_and_launches_worker(self):
        self.window.Card_statistic = 7
        with patch.object(release.threading, "Thread") as worker:
            self.window.start_auto_cycle()
        self.assertTrue(self.window.run_event.is_set())
        self.assertEqual(self.window.Card_statistic, 7)
        self.assertEqual(self.window.Card_statistic_ed, 0)
        worker.assert_called_once()
        self.assertEqual(worker.call_args.kwargs["target"], self.window.auto_cycle_worker)

    def test_start_refuses_duplicate_worker(self):
        self.window._auto_thread = Mock()
        self.window._auto_thread.is_alive.return_value = True
        with patch.object(release.threading, "Thread") as worker:
            self.window.start_auto_cycle()
        worker.assert_not_called()
        self.assertFalse(self.window.run_event.is_set())
        self.assertIn("请稍后再按 F11", self.window.log.toPlainText())

    def test_handle_f11_toggles(self):
        self.window.Statistics.setText("3")
        with patch.object(release.threading, "Thread") as worker:
            self.window.handle_f11()
            self.assertTrue(self.window.run_event.is_set())
            self.window.handle_f11()
        self.assertFalse(self.window.run_event.is_set())
        self.assertIn("自动循环停止", self.window.log.toPlainText())
        self.assertEqual(worker.call_count, 1)

    def test_save_keeps_function_key_listener(self):
        self.window.save_config()
        self.window.key_listener.stop.assert_not_called()

    def test_close_stops_loop_and_listener(self):
        self.window.run_event.set()
        self.window.close()
        self.assertFalse(self.window.run_event.is_set())
        self.window.key_listener.stop.assert_called()

    def test_missing_template_is_an_error(self):
        self.assertEqual(self.window.custom_button_image,
                         Path.cwd() / "summon_button_custom.png")
        with self.assertRaisesRegex(RuntimeError, "按钮模板不存在"):
            self.window.summon_panel_visible()

    def test_bundled_template_is_used_when_local_copy_missing(self):
        bundled = Path.cwd() / "bundle"
        bundled.mkdir()
        (bundled / "summon_button_custom.png").write_bytes(
            (PROJECT / "summon_button_custom.png").read_bytes())
        with patch.object(release.sys, "_MEIPASS", str(bundled), create=True):
            self.assertEqual(self.window.button_template_path(),
                             bundled / "summon_button_custom.png")

    def test_real_template_matching_against_rendered_screen(self):
        from PIL import Image, ImageDraw
        template = Image.new("RGB", (80, 20), (30, 60, 90))
        draw = ImageDraw.Draw(template)
        for col in range(0, 80, 4):
            draw.rectangle((col, 0, col + 1, 19), fill=(220, 230, 240))
        for row in range(0, 20, 6):
            draw.rectangle((0, row, 79, row + 1), fill=(150, 90, 40))
        template.save(self.window.custom_button_image)
        canvas = Image.new("RGB", (1024, 768), (18, 18, 18))
        x, y = self.click_point(release.DEFAULT_SUMMON_RATIO)
        canvas.paste(template, (x - 100 - 40, y - 50 - 10))
        self.inputs["screenshot"].return_value = canvas
        self.assertTrue(self.window.summon_panel_visible())

        blank = Image.new("RGB", (1024, 768), (18, 18, 18))
        self.inputs["screenshot"].return_value = blank
        self.assertFalse(self.window.summon_panel_visible())

    def test_repository_config_can_be_read(self):
        self.window.config_path = str(PROJECT / "config.json")
        config = self.window.load_config()
        self.assertEqual(config.get("post_summon_seconds"), 5.0)
        self.assertTrue(config.get("card_point_ratio"))

    # ---------------- 自动瞄准 ----------------

    def test_aim_ui_defaults_and_bounds(self):
        self.assertEqual(self.window.aim_gain, release.DEFAULT_AIM_GAIN)
        self.assertEqual(self.window.aim_gain_spin.value(), release.DEFAULT_AIM_GAIN)
        self.assertEqual(self.window.aim_template_client_width, 1024)
        self.window.aim_gain_spin.setValue(0.01)
        self.assertEqual(self.window.aim_gain_spin.value(), release.AIM_GAIN_RANGE[0])
        self.window.aim_gain_spin.setValue(99)
        self.assertEqual(self.window.aim_gain_spin.value(), release.AIM_GAIN_RANGE[1])

    def test_invalid_stored_gain_falls_back(self):
        for value in ("bad", None, True, False, 0, -1, 99, float("nan"), float("inf")):
            with self.subTest(value=value):
                Path("config.json").write_text(json.dumps({"aim_gain": value}), encoding="utf-8")
                self.assertEqual(self.new_window().aim_gain, release.DEFAULT_AIM_GAIN)

    def test_stored_gain_and_aim_width_reload(self):
        self.window.aim_gain_spin.setValue(1.6)
        self.window.save_config()
        reloaded = self.new_window()
        self.assertEqual(reloaded.aim_gain, 1.6)
        self.assertEqual(reloaded.aim_gain_spin.value(), 1.6)

    def test_f8_records_monster_crop_and_template_width(self):
        from PIL import Image
        self.inputs["position"].return_value = (600, 500)
        self.inputs["screenshot"].return_value = Image.new("RGB", release.MONSTER_TEMPLATE_SIZE)
        self.window.capture_custom_position("monster")
        half = release.MONSTER_TEMPLATE_SIZE[0] // 2
        self.inputs["screenshot"].assert_called_once_with(
            region=(600 - half, 500 - half, *release.MONSTER_TEMPLATE_SIZE))
        self.assertTrue(self.window.monster_template_image.is_file())
        self.assertEqual(self.window.aim_template_client_width, 1024)
        config = json.loads(Path("config.json").read_text(encoding="utf-8"))
        self.assertEqual(config["aim_template_client_width"], 1024)
        self.assertIn("已记录怪物模板", self.window.log.toPlainText())

    def test_monster_capture_near_edge_is_rejected(self):
        self.inputs["position"].return_value = (150, 500)
        self.window.capture_custom_position("monster")
        self.assertFalse(self.window.monster_template_image.is_file())
        self.assertIn("边缘太近", self.window.log.toPlainText())

    def test_monster_capture_blocked_while_aiming(self):
        self.window.aim_event.set()
        self.window.capture_custom_position("monster")
        self.inputs["position"].assert_not_called()
        self.assertIn("先按 F12 停止", self.window.log.toPlainText())

    def test_hotkeys_cover_monster_capture_and_aim_toggle(self):
        self.window.capture_point_signal.disconnect()
        self.window.f12_signal.disconnect()
        requests, toggles = [], []
        self.window.capture_point_signal.connect(requests.append)
        self.window.f12_signal.connect(lambda: toggles.append(True))
        self.window.on_key_press(release.Key.f8)
        self.window.on_key_press(release.Key.f8)
        self.window.on_key_release(release.Key.f8)
        self.window.on_key_press(release.Key.f8)
        self.window.on_key_press(release.Key.f12)
        self.window.on_key_press(release.Key.f12)
        self.assertEqual(requests, ["monster", "monster"])
        self.assertEqual(toggles, [True])

    def test_aim_once_converts_error_through_gain(self):
        self.window.aim_event.set()
        self.window.aim_gain = 2.0
        center = (612, 434)
        with patch.object(self.window, "detect_monster",
                          return_value=(0.9, (center[0] + 40, center[1] - 20))), \
                patch.object(release, "mouse_relative_move", return_value=True) as move:
            self.window.aim_once()
        self.assertEqual(move.call_args.args, (20.0, -10.0))

    def test_aim_once_deadzone_and_step_clamp(self):
        self.window.aim_event.set()
        center = (612, 434)
        with patch.object(release, "mouse_relative_move", return_value=True) as move:
            with patch.object(self.window, "detect_monster",
                              return_value=(0.9, (center[0] + 3, center[1] + 2))):
                self.window.aim_once()
            move.assert_not_called()
            with patch.object(self.window, "detect_monster",
                              return_value=(0.9, (center[0] + 900, center[1] - 500))):
                self.window.aim_once()
            cap = release.AIM_MAX_STEP_PX
            self.assertEqual(move.call_args.args, (float(cap), -float(cap)))
        self.assertIn(f"已修正 ({cap},{-cap})px", self.window.log.toPlainText())

    def test_aim_once_without_detection_does_not_move(self):
        self.window.aim_event.set()
        with patch.object(self.window, "detect_monster", return_value=None), \
                patch.object(release, "mouse_relative_move", return_value=True) as move:
            self.window.aim_once()
        move.assert_not_called()

    def test_aim_input_failure_is_logged_once(self):
        self.window.aim_event.set()
        with patch.object(self.window, "detect_monster", return_value=(0.9, (712, 434))), \
                patch.object(release, "mouse_relative_move", return_value=False) as move:
            self.window.aim_once()
        move.assert_called_once()
        self.assertIn("相对鼠标移动发送失败", self.window.log.toPlainText())
        self.assertEqual(self.window._aim_input_failures, 1)

    def test_detect_monster_returns_matched_center(self):
        from PIL import Image
        template = self.make_monster_template()
        canvas = Image.new("RGB", (1024, 768), (15, 40, 25))
        paste = (300, 200)
        canvas.paste(template, paste)
        self.inputs["screenshot"].return_value = canvas
        score, point = self.window.detect_monster()
        self.assertGreaterEqual(score, release.AIM_SCORE_MIN)
        half = release.MONSTER_TEMPLATE_SIZE[0] // 2
        self.assertEqual((round(point[0]), round(point[1])),
                         (100 + paste[0] + half, 50 + paste[1] + half))

    def test_detect_monster_misses_blank_screen(self):
        from PIL import Image
        self.make_monster_template()
        self.inputs["screenshot"].return_value = Image.new("RGB", (1024, 768), (15, 40, 25))
        self.assertIsNone(self.window.detect_monster())

    def test_detect_monster_missing_template_raises(self):
        with self.assertRaisesRegex(RuntimeError, "怪物模板不存在"):
            self.window.detect_monster()

    def test_end_to_end_aim_moves_crosshair_to_monster(self):
        from PIL import Image
        template = self.make_monster_template()
        canvas = Image.new("RGB", (1024, 768), (15, 40, 25))
        half = release.MONSTER_TEMPLATE_SIZE[0] // 2
        paste = (512 + 60 - half, 384 - half)  # 怪物中心在客户区内比准星偏右 60px
        canvas.paste(template, paste)
        self.inputs["screenshot"].return_value = canvas
        self.window.aim_event.set()
        with patch.object(release, "mouse_relative_move", return_value=True) as move:
            self.window.aim_once()
        self.assertEqual(move.call_args.args, (60.0, 0.0))

    def test_start_aim_requires_monster_template(self):
        with patch.object(release.threading, "Thread") as worker:
            self.window.start_aim_cycle()
        worker.assert_not_called()
        self.assertFalse(self.window.aim_event.is_set())
        self.assertIn("怪物模板不存在", self.window.log.toPlainText())

    def test_start_aim_refuses_while_card_loop_runs(self):
        self.make_monster_template()
        self.window.run_event.set()
        with patch.object(release.threading, "Thread") as worker:
            self.window.start_aim_cycle()
        worker.assert_not_called()
        self.assertFalse(self.window.aim_event.is_set())
        self.assertIn("放卡循环正在运行", self.window.log.toPlainText())

    def test_card_loop_refuses_while_aiming(self):
        self.window.aim_event.set()
        self.window.Card_statistic = 5
        with patch.object(release.threading, "Thread") as worker:
            self.window.start_auto_cycle()
        worker.assert_not_called()
        self.assertFalse(self.window.run_event.is_set())
        self.assertIn("自动瞄准正在运行", self.window.log.toPlainText())

    def test_f12_toggles_aim_and_restores_acceleration(self):
        self.make_monster_template()
        with patch.object(release.threading, "Thread") as worker, \
                patch.object(release, "set_mouse_acceleration") as accel:
            self.window.handle_f12()
            self.assertTrue(self.window.aim_event.is_set())
            self.window.handle_f12()
        self.assertFalse(self.window.aim_event.is_set())
        self.assertEqual(worker.call_count, 1)
        self.assertEqual(worker.call_args.kwargs["target"], self.window.aim_cycle_worker)
        self.assertEqual([c.args[0] for c in accel.call_args_list], [False, True])

    def test_aim_worker_loops_until_stopped(self):
        self.window.aim_event.set()
        calls = []

        def aim():
            calls.append(1)
            if len(calls) >= 3:
                self.window.aim_event.clear()

        with patch.object(self.window, "aim_once", side_effect=aim), \
                patch.object(self.window, "interruptible_sleep", return_value=True):
            self.window.aim_cycle_worker()
        self.assertEqual(len(calls), 3)

    def test_aim_worker_stops_and_restores_on_exception(self):
        self.window.aim_event.set()
        with patch.object(self.window, "aim_once", side_effect=RuntimeError("cv boom")), \
                patch.object(release, "set_mouse_acceleration") as accel:
            self.window.aim_cycle_worker()
        self.assertFalse(self.window.aim_event.is_set())
        self.assertIn("瞄准异常: cv boom", self.window.log.toPlainText())
        self.assertEqual(accel.call_args_list[-1].args[0], True)

    def test_preview_marker_follows_and_hides(self):
        self.window.update_target_marker((300, 200), 0.9)
        self.assertIsNotNone(self.window._marker)
        self.assertEqual((self.window._marker.x(), self.window._marker.y()),
                         (300 - release.AIM_MARKER_SIZE[0] // 2,
                          200 - release.AIM_MARKER_SIZE[1] // 2))
        self.window.on_preview_toggled(False)
        self.assertFalse(self.window._marker.isVisible())

    def test_preview_marker_created_only_when_checked(self):
        from PIL import Image
        template = self.make_monster_template()
        canvas = Image.new("RGB", (1024, 768), (15, 40, 25))
        canvas.paste(template, (300, 200))
        self.inputs["screenshot"].return_value = canvas
        self.window.detect_monster()
        self.assertIsNone(self.window._marker)
        self.window.aim_preview_check.setChecked(True)
        self.window.detect_monster()
        self.assertIsNotNone(self.window._marker)

    def test_aiming_never_clicks_or_moves_cursor_absolutely(self):
        """瞄准只发送相对移动：任何点击或 SetCursorPos 都会破坏“开枪由玩家控制”。"""
        from PIL import Image
        template = self.make_monster_template()
        canvas = Image.new("RGB", (1024, 768), (15, 40, 25))
        canvas.paste(template, (500, 300))
        self.inputs["screenshot"].return_value = canvas
        self.window.aim_event.set()
        with patch.object(release, "mouse_relative_move", return_value=True) as move:
            self.window.aim_once()
        move.assert_called_once()
        self.inputs["click"].assert_not_called()
        self.inputs["moveTo"].assert_not_called()
        self.inputs["press"].assert_not_called()

    def test_windows_input_struct_layout(self):
        import ctypes
        packet = release._WinInput()
        packet.type = 0
        packet.ii.mi = release._MouseInput(-120, 60, 0, release.MOUSEEVENTF_MOVE, 0, 0)
        self.assertEqual(packet.ii.mi.dx, -120)
        self.assertEqual(packet.ii.mi.dy, 60)
        self.assertEqual(packet.ii.mi.dwFlags, release.MOUSEEVENTF_MOVE)
        if sys.maxsize > 2**32:  # 64 位下必须和 Windows INPUT 布局一致，否则 SendInput 会失败
            self.assertEqual(ctypes.sizeof(release._MouseInput), 32)
            self.assertEqual(ctypes.sizeof(packet), 40)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    unittest.main(verbosity=2)

import unittest
from unittest.mock import Mock, patch

import psutil

from core import resolution


class RestartTests(unittest.TestCase):
    def setUp(self):
        self.config = dict(enabled=True, require_1080p=True, engine="ue4",
                           launch_args={"ue4": ["-windowed", "-ResX=1920", "-ResY=1080"]})
        self.watcher = resolution.Watcher(self.config)
        self.process = Mock(pid=123)
        self.process.exe.return_value = "C:/Games/Game.exe"
        self.process.cwd.return_value = "C:/Games"
        self.process.cmdline.return_value = ["Game.exe", "-token=keep", "-ResX=800"]
        self.process.create_time.return_value = 100
        self.state = resolution.Window(1, (800, 600), True, "ue")

    def test_denied_cwd_produces_plan_without_killing(self):
        self.process.cwd.side_effect = psutil.AccessDenied(123)
        with self.assertRaises(resolution.RestartRequested) as caught:
            self.watcher._restart(self.process, self.state, {})
        self.assertEqual(caught.exception.launch, {
            "exe": "C:/Games/Game.exe", "cwd": "C:/Games", "role": "game",
            "args": ["-token=keep", "-windowed", "-ResX=1920", "-ResY=1080"]})
        self.process.kill.assert_not_called()

    def test_unreadable_command_line_never_kills_game(self):
        self.process.cmdline.side_effect = psutil.AccessDenied(123)
        with self.assertRaises(psutil.AccessDenied):
            self.watcher._restart(self.process, self.state, {})
        self.process.kill.assert_not_called()

    def test_temporary_access_failure_retries(self):
        with patch.object(resolution.psutil, "Process", return_value=self.process), \
                patch.object(resolution, "window_state", return_value=self.state), \
                patch.object(resolution.time, "time", return_value=101) as now, \
                patch.object(self.watcher, "_restart", side_effect=[psutil.AccessDenied(123), None]) as restart:
            self.watcher.poll({123})
            self.watcher.poll({123})
            self.assertEqual(restart.call_count, 1)
            now.return_value = 117
            self.watcher.poll({123})
            self.assertEqual(restart.call_count, 2)
            self.assertTrue(self.watcher._runs[(123, 100)]["restarted"])

    def test_disabled_restart_only_resizes(self):
        watcher = resolution.Watcher(dict(self.config, engine="ue5", restart_with_args=False, launch_args={}))
        with patch.object(resolution.psutil, "Process", return_value=self.process), \
                patch.object(resolution, "window_state", return_value=self.state), \
                patch.object(resolution.time, "time", return_value=101), \
                patch.object(watcher, "_restart") as restart, \
                patch.object(watcher, "_force") as resize:
            watcher.poll({123})
        restart.assert_not_called()
        resize.assert_called_once_with(123, self.state)


if __name__ == "__main__":
    unittest.main()

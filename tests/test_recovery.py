import unittest
from contextlib import nullcontext
from unittest.mock import Mock, patch

from core import tasks, proc, scheduler, control
from core.configuration import validate_task


class Clock:
    def __init__(self):
        self.now = 0

    def sleep(self, seconds):
        self.now += seconds


def monitor_config():
    return dict(name="game", type="monitor", timeout=600, poll_interval=5,
                game=dict(matchers=[dict(names=["Game.exe"])], max_exits=2),
                script=dict(matchers=[dict(names=["Script.exe"])], max_exits=3),
                recovery=dict(script=dict(exe="C:/Script/App/Script.exe", cwd="C:/Script/App", args=["--run"])))


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.monitor = tasks.MonitorTask(monitor_config(), {})
        self.recovery = tasks.RecoveryTask.from_monitor(self.monitor, dict(exe="Game.exe", args=["-windowed"]))
        self.recovery._check_interrupt = Mock()

    def test_inherits_limits_and_scoped_python(self):
        self.assertEqual(self.recovery.timeout, 600)
        self.assertEqual(self.recovery.attempts, 2)
        self.assertEqual(self.recovery.categories["game"]["max_exits"], 2)
        self.assertIn(dict(names=["python.exe", "pythonw.exe"], python_root="C:/Script/App"), self.recovery.cleanup)
        self.assertEqual(self.recovery.scripts[0]["args"], ["--run"])
        self.assertIsNone(self.recovery.parallel)

    def test_reappearance_resets_full_three_minutes(self):
        clock = Clock()
        self.recovery._interruptible_sleep = clock.sleep
        with patch.object(tasks.time, "monotonic", side_effect=lambda: clock.now), \
                patch.object(tasks.proc, "any_alive", side_effect=lambda _: clock.now in (0, 100)), \
                patch.object(self.recovery, "_cleanup") as cleanup:
            self.assertTrue(self.recovery.quiesce())
        self.assertEqual(clock.now, 285)
        self.assertEqual(cleanup.call_count, 2)

    def test_never_launches_when_process_will_not_exit(self):
        clock = Clock()
        self.recovery._interruptible_sleep = clock.sleep
        with patch.object(tasks.time, "monotonic", side_effect=lambda: clock.now), \
                patch.object(tasks.proc, "any_alive", return_value=True), \
                patch.object(self.recovery, "_cleanup"), patch.object(self.recovery, "_attempt") as attempt:
            self.assertFalse(self.recovery.run())
        attempt.assert_not_called()
        self.assertEqual(self.recovery.status, "kill_giveup")

    def test_script_budget_then_game_script(self):
        with patch.object(self.recovery, "quiesce", return_value=True), \
                patch.object(self.recovery, "_attempt", side_effect=["timeout", "success"]) as attempt:
            self.assertTrue(self.recovery.run())
        self.assertEqual([c.args[0] for c in attempt.call_args_list], [False, True])

    def test_script_success_does_not_start_game_phase(self):
        with patch.object(self.recovery, "quiesce", return_value=True), \
                patch.object(self.recovery, "_attempt", return_value="success") as attempt:
            self.assertTrue(self.recovery.run())
        attempt.assert_called_once_with(False)

    def test_two_failures_notify_once_and_never_retry_log_errors(self):
        callback = self.recovery.failure_callback = Mock()
        with patch.object(self.recovery, "quiesce", return_value=True), \
                patch.object(self.recovery, "_attempt", return_value="error_limit") as attempt:
            self.assertFalse(self.recovery.run())
        self.assertEqual([c.args[0] for c in attempt.call_args_list], [False, True])
        callback.assert_called_once_with(self.recovery.name, "error_limit")

    def test_success_after_first_failure_is_silent(self):
        callback = self.recovery.failure_callback = Mock()
        with patch.object(self.recovery, "quiesce", return_value=True), \
                patch.object(self.recovery, "_attempt", side_effect=["timeout", "success"]):
            self.assertTrue(self.recovery.run())
        callback.assert_not_called()

    def test_monitor_failure_notifies_once(self):
        callback = self.monitor.failure_callback = Mock()
        self.monitor.status = "timeout"
        self.monitor.report_failure("fail")
        callback.assert_called_once_with(self.monitor.name, "timeout")

    def test_attempt_duration_is_capped_at_twenty_minutes(self):
        cfg = monitor_config()
        cfg["timeout"] = 15000
        recovery = tasks.RecoveryTask.from_monitor(tasks.MonitorTask(cfg, {}), self.recovery.game)
        self.assertEqual(recovery.timeout, 1200)
        clock = Clock()
        recovery._interruptible_sleep = clock.sleep
        recovery._check_interrupt = Mock()
        with patch.object(tasks.time, "monotonic", side_effect=lambda: clock.now), \
                patch.object(tasks.proc, "start_process", return_value=Mock()), \
                patch.object(tasks, "_pids_batch", return_value=[{9}, {10}, set()]):
            self.assertEqual(recovery._attempt(False), "timeout")
        self.assertEqual(clock.now, 1200)

    def test_total_deadline_stops_waiting_and_prevents_launch(self):
        clock = Clock()
        self.recovery._recovery_deadline = 3000
        clock.now = 2990
        self.recovery._interruptible_sleep = clock.sleep
        with patch.object(tasks.time, "monotonic", side_effect=lambda: clock.now), \
                patch.object(tasks.proc, "any_alive", return_value=False), \
                patch.object(self.recovery, "_cleanup") as cleanup, \
                patch.object(self.recovery, "_attempt") as attempt:
            self.assertFalse(self.recovery.run())
        self.assertEqual(clock.now, 3000)
        self.assertEqual(self.recovery.status, "timeout")
        attempt.assert_not_called()
        cleanup.assert_called_once()

    def test_resume_does_not_repeat_used_attempt(self):
        with patch.object(self.recovery, "quiesce", return_value=True), \
                patch.object(self.recovery, "_attempt", side_effect=[control.TaskInterrupted("pause"), "success"]) as attempt:
            with self.assertRaises(control.TaskInterrupted):
                self.recovery.run()
            self.assertTrue(self.recovery.run())
        self.assertEqual([c.args[0] for c in attempt.call_args_list], [False, True])

    def test_interrupt_is_propagated(self):
        self.recovery._check_interrupt.side_effect = control.TaskInterrupted("pause")
        with self.assertRaises(control.TaskInterrupted):
            self.recovery.quiesce()

    def test_launch_order_and_waits_for_python(self):
        clock = Clock()
        self.recovery._interruptible_sleep = clock.sleep
        self.recovery.stable_dead = 10
        child = Mock()
        child.poll.return_value = 0
        def groups(matchers):
            # Only the related Python remains after the launcher has exited.
            return [{9}, set(), {10} if clock.now < 30 else set()]
        with patch.object(tasks.time, "monotonic", side_effect=lambda: clock.now), \
                patch.object(tasks.proc, "start_process", return_value=child) as start, \
                patch.object(tasks, "_pids_batch", side_effect=groups):
            self.assertEqual(self.recovery._attempt(True), "success")
        self.assertEqual([c.args[0] for c in start.call_args_list],
                         ["Game.exe", "C:/Script/App/Script.exe"])
        self.assertEqual(clock.now, 40)

    def test_unreadable_python_cannot_count_as_quiet(self):
        clock = Clock()
        self.recovery._interruptible_sleep = clock.sleep
        with patch.object(tasks.time, "monotonic", side_effect=lambda: clock.now), \
                patch.object(tasks.proc, "any_alive", side_effect=proc.psutil.AccessDenied()), \
                patch.object(tasks.log, "warning"), patch.object(self.recovery, "_cleanup") as cleanup:
            self.assertFalse(self.recovery.quiesce())
        cleanup.assert_not_called()

    def test_python_cwd_and_sibling_directory(self):
        process = Mock()
        process.cwd.return_value = "C:/Script/App"
        matcher = dict(names=["python.exe"], python_root="C:/Script/App")
        self.assertTrue(proc._match_process(process, "python.exe", "C:/Python/python.exe", matcher))
        process.cwd.return_value = "C:/Script/App2"
        process.cmdline.return_value = ["python.exe", "main.py"]
        self.assertFalse(proc._match_process(process, "python.exe", "C:/Python/python.exe", matcher))
        process.cmdline.return_value = ["python.exe", "C:/Script/App/main.py"]
        self.assertTrue(proc._match_process(process, "python.exe", "C:/Python/python.exe", matcher))

    def test_bad_recovery_arguments_rejected(self):
        cfg = monitor_config()
        cfg["recovery"]["script"]["args"] = "--run"
        with self.assertRaises(ValueError):
            validate_task(cfg, {})

    def test_recovery_runs_after_master_close(self):
        queue = dict(name="test", tasks=[
            dict(name="master", type="launch", exe="Master.exe", master_close_task="close"),
            monitor_config(), dict(name="close", type="kill"),
            dict(name="next", type="launch", exe="Next.exe")])
        order = []

        def run(task, results):
            order.append(task.name)
            if task.TYPE == "monitor":
                task.deferred = [self.recovery]
            if isinstance(task, tasks.RecoveryBatch):
                self.assertEqual(task.pending, [self.recovery])
                task.pending.clear()
            return None

        with patch.object(scheduler, "_run_one", side_effect=run), \
                patch.object(scheduler, "maybe_cleanup_logs_monthly"), \
                patch.object(scheduler.notify, "send_queue_report"), \
                patch.object(scheduler.control, "consume_action", return_value=None):
            scheduler.run_queue(queue, {})
        self.assertEqual(order, ["master", "game", "close", "监控参数补跑组", "next"])

    def test_monitor_defers_instead_of_launching(self):
        self.monitor._check_interrupt = Mock()
        self.monitor.resolution_check = dict(enabled=True, require_1080p=True,
                                            engine="unity", launch_args={"unity": ["-screen-width", "1920"]})
        with patch.object(tasks.proc, "any_alive", return_value=True), \
                patch.object(tasks, "_pids_batch", return_value=[{1}, {2}]), \
                patch.object(tasks.resolution.Watcher, "poll", side_effect=tasks.resolution.RestartRequested(self.recovery.game)), \
                patch.object(tasks.RecoveryTask, "quiesce", return_value=True), \
                patch.object(tasks.proc, "start_process") as start:
            self.assertTrue(self.monitor.run())
        start.assert_not_called()
        self.assertEqual(self.monitor.status, "deferred")
        self.assertEqual(len(self.monitor.take_deferred()), 1)
        self.assertEqual(self.monitor.take_deferred(), [])

    def test_queue_report_keeps_monitor_and_recovery_failures(self):
        queue = dict(name="test", tasks=[monitor_config()])
        def run(task, results):
            if task.TYPE == "monitor":
                results[task.name] = (False, "timeout")
                task.deferred = [self.recovery]
            else:
                task._sub_results[self.recovery.name] = (False, "timeout")
                task.pending.clear()
        with patch.object(scheduler, "_run_one", side_effect=run), \
                patch.object(scheduler, "maybe_cleanup_logs_monthly"), \
                patch.object(scheduler.notify, "send_queue_report") as report, \
                patch.object(scheduler.control, "consume_action", return_value=None):
            results = scheduler.run_queue(queue, {})
        self.assertIn(self.monitor.name, results)  # Still available locally.
        self.assertEqual(report.call_args.args[1], {self.monitor.name: (False, "timeout"),
                                                    self.recovery.name: (False, "timeout")})


if __name__ == "__main__":
    unittest.main()

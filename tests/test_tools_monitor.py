"""SliceMonitor и wait_for_slice: обработка состояний бэкенда без Qt.

Запуск из корня репозитория: python -m unittest tests.test_tools_monitor
"""

import os
import sys
import threading
import unittest

PLUGIN_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "plugin", "AIAssistant")
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)

from agent.slicer.monitor import (DONE, ERROR, NOT_STARTED, PROCESSING, SliceMonitor, WaitOutcome,  # noqa: E402
                                  stage_text, wait_for_slice)


class Clock(object):
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class MonitorTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.monitor = SliceMonitor(clock=self.clock)
        self.monitor.set_current("CuraEngineBackend")

    def start(self, backend="CuraEngineBackend", mode="classic"):
        generation = self.monitor.mark_started(backend, mode)
        # forceSlice(): stopSlicing() шлёт NotStarted, потом slice() синхронно шлёт Processing
        self.monitor.on_state(backend, NOT_STARTED)
        self.monitor.on_state(backend, PROCESSING)
        self.assertTrue(self.monitor.finish_start())
        return generation

    def test_normal_flow(self):
        generation = self.start()
        snap = self.monitor.snapshot()
        self.assertEqual(snap["state"], "processing")
        self.assertEqual(snap["generation"], generation)
        self.monitor.on_progress("CuraEngineBackend", 0.5)
        self.clock.sleep(3)
        self.monitor.on_state("CuraEngineBackend", DONE)
        snap = self.monitor.snapshot()
        self.assertEqual(snap["state"], "done")
        self.assertEqual(snap["progress"], 1.0)
        self.assertEqual(snap["elapsed_s"], 3.0)

    def test_events_of_other_backends_are_ignored(self):
        self.start(backend="CylindricalBackend", mode="cylindrical_full")
        # CylindricalBackend сам управляет CuraEngineBackend — его сигналы не должны завершать нашу нарезку
        self.monitor.on_state("CuraEngineBackend", DONE)
        self.monitor.on_progress("CuraEngineBackend", 1.0)
        self.monitor.on_error("CuraEngineBackend", "boom")
        snap = self.monitor.snapshot()
        self.assertEqual(snap["state"], "processing")
        self.assertNotIn("error", snap)

    def test_not_started_backend(self):
        self.monitor.mark_started("CuraEngineBackend", "classic")
        self.assertFalse(self.monitor.finish_start())   # «обработка слоёв ещё занята, попробуем позже»

    def test_ui_started_slice_opens_generation(self):
        self.monitor.on_state("CuraEngineBackend", NOT_STARTED)
        before = self.monitor.snapshot()["generation"]
        self.monitor.on_state("CuraEngineBackend", PROCESSING)
        self.assertEqual(self.monitor.snapshot()["generation"], before + 1)

    def test_error_message(self):
        self.start()
        self.monitor.on_state("CuraEngineBackend", ERROR)
        self.monitor.on_error("CuraEngineBackend", "Нечего слайсить", "nothing_to_slice")
        snap = self.monitor.snapshot()
        self.assertEqual(snap["state"], "error")
        self.assertEqual(snap["error_kind"], "nothing_to_slice")

    def test_stage_text(self):
        self.assertIn("ATSS", stage_text("cylindrical_full", 0.55))
        self.assertIn("CliParser", stage_text("cylindrical", 0.7))
        self.assertIn("CuraEngine", stage_text("classic", 0.3))
        self.assertEqual(stage_text("classic", 1.0, "done"), "Слайсинг завершён")


class WaitTest(unittest.TestCase):
    def run_wait(self, statuses, timeout=100.0, cancel=None, stall_limit=300.0, hint_after=60.0):
        clock = Clock()
        sequence = list(statuses)
        updates = []

        def get_status():
            status = sequence[0] if len(sequence) == 1 else sequence.pop(0)
            return status(clock) if callable(status) else status

        outcome, status, hint = wait_for_slice(get_status, 1, timeout, cancel_event=cancel,
                                               on_update=lambda s, h: updates.append((s.get("state"), h)),
                                               poll=1.0, stall_limit=stall_limit, hint_after=hint_after,
                                               sleep=clock.sleep, clock=clock)
        return outcome, status, hint, updates

    def test_done(self):
        outcome, status, _hint, updates = self.run_wait([
            {"state": "processing", "progress": 0.1, "generation": 1},
            {"state": "processing", "progress": 0.6, "generation": 1},
            {"state": "done", "progress": 1.0, "generation": 1}])
        self.assertEqual(outcome, WaitOutcome.DONE)
        self.assertEqual([u[0] for u in updates], ["processing", "processing", "done"])

    def test_error_waits_for_message(self):
        outcome, status, _hint, _ = self.run_wait([
            {"state": "error", "progress": 0.0},
            {"state": "error", "progress": 0.0, "error": "Ошибки в настройках"}])
        self.assertEqual(outcome, WaitOutcome.ERROR)
        self.assertEqual(status["error"], "Ошибки в настройках")

    def test_error_without_message(self):
        outcome, _status, _hint, _ = self.run_wait([{"state": "error", "progress": 0.0}])
        self.assertEqual(outcome, WaitOutcome.ERROR)

    def test_interrupted_and_disabled(self):
        self.assertEqual(self.run_wait([{"state": "not_started"}])[0], WaitOutcome.INTERRUPTED)
        self.assertEqual(self.run_wait([{"state": "disabled"}])[0], WaitOutcome.DISABLED)

    def test_timeout(self):
        outcome, _status, _hint, _ = self.run_wait([{"state": "processing", "progress": 0.2, "mode": "classic",
                                                     "since_progress_change_s": 0.0}], timeout=5)
        self.assertEqual(outcome, WaitOutcome.TIMEOUT)

    def test_cancel(self):
        cancel = threading.Event()
        cancel.set()
        outcome, _status, _hint, _ = self.run_wait([{"state": "processing", "progress": 0.2}], cancel=cancel)
        self.assertEqual(outcome, WaitOutcome.CANCELLED)

    def test_5d_glicer_stall_gives_hint_then_stalls(self):
        def stuck(clock, start=[None]):
            if start[0] is None:
                start[0] = clock()
            return {"state": "processing", "progress": 0.55, "mode": "cylindrical_full",
                    "since_progress_change_s": clock() - start[0]}
        outcome, _status, hint, updates = self.run_wait([stuck], timeout=1000, stall_limit=30, hint_after=10)
        self.assertEqual(outcome, WaitOutcome.STALLED)
        self.assertIn("ATSS", hint)
        self.assertTrue(any(h for _s, h in updates))

    def test_classic_is_never_stalled(self):
        def slow(clock):
            return {"state": "processing", "progress": 0.55, "mode": "classic", "since_progress_change_s": 999}
        outcome, _status, _hint, _ = self.run_wait([slow], timeout=50, stall_limit=10)
        self.assertEqual(outcome, WaitOutcome.TIMEOUT)


if __name__ == "__main__":
    unittest.main()

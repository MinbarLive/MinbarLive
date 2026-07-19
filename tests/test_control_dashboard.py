"""Headless motion contracts for the V3 operator dashboard."""

from types import SimpleNamespace

from gui.control_dashboard import DashboardChrome


class _Host:
    def __init__(self):
        self._running = True
        self.scheduled = []
        self.cancelled = []

    def after(self, delay, callback):
        job = f"after#{len(self.scheduled)}"
        self.scheduled.append((job, delay, callback))
        return job

    def after_cancel(self, job):
        self.cancelled.append(job)


def _chrome_with_two_stages():
    host = _Host()
    chrome = DashboardChrome(host)
    chrome._stage_widgets = [{}, {}]
    chrome._states = lambda: [
        SimpleNamespace(label="one"),
        SimpleNamespace(label="two"),
    ]
    chrome._refresh_summary = lambda _states: None
    return host, chrome


def test_reduced_motion_paints_signal_path_without_scheduling_jobs():
    host, chrome = _chrome_with_two_stages()
    painted = []
    chrome._reduced_motion = True
    chrome._paint_stage = lambda index, stage: painted.append((index, stage.label))

    chrome.refresh(animate=True)

    assert host.scheduled == []
    assert painted == [(0, "one"), (1, "two")]
    assert chrome._motion_jobs == set()


def test_live_signal_path_jobs_are_staggered_and_cancelled_deterministically():
    host, chrome = _chrome_with_two_stages()
    chrome._reduced_motion = False
    chrome._paint_stage = lambda _index, _stage: None

    chrome.refresh(animate=True)

    assert [delay for _job, delay, _callback in host.scheduled] == [0, 45]
    assert chrome._motion_jobs == {"after#0", "after#1"}

    chrome.cancel_motion()
    chrome.cancel_motion()

    assert set(host.cancelled) == {"after#0", "after#1"}
    assert len(host.cancelled) == 2
    assert chrome._motion_jobs == set()


def test_layout_transition_uses_160ms_and_finishes_at_exact_final_state():
    host = _Host()
    chrome = DashboardChrome(host)
    chrome._reduced_motion = False
    progress = []
    completed = []

    chrome.animate_transition(progress.append, on_complete=lambda: completed.append(1))

    assert progress == [0.0]
    assert [delay for _job, delay, _callback in host.scheduled] == [40, 80, 120, 160]
    for _job, _delay, callback in host.scheduled:
        callback()
    assert progress == [0.0, 0.25, 0.5, 0.75, 1.0]
    assert completed == [1]
    assert chrome._motion_jobs == set()


def test_reduced_motion_layout_transition_is_immediate_and_job_free():
    host = _Host()
    chrome = DashboardChrome(host)
    chrome._reduced_motion = True
    progress = []
    completed = []

    chrome.animate_transition(progress.append, on_complete=lambda: completed.append(1))

    assert progress == [1.0]
    assert completed == [1]
    assert host.scheduled == []
    assert chrome._motion_jobs == set()

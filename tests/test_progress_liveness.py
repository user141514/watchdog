from __future__ import annotations

from pathlib import Path

from chat_watchdog.progress_liveness import MymemLiteStore


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_progress_file_keeps_uncertain_stall_claim_pending_until_settled(tmp_path: Path) -> None:
    clock = Clock()
    store = MymemLiteStore(tmp_path, clock=clock)
    cid = "00000000-0000-0000-0000-000000000111"
    target = f"https://chatgpt.com/c/{cid}"

    store.ensure(cid, target)
    path = store.path_for(cid)
    initial_size = path.stat().st_size

    first = store.observe(cid, target, "fingerprint-a", observable=True)
    size_after_first = path.stat().st_size
    same = store.observe(cid, target, "fingerprint-a", observable=True)

    assert first.progress_seq == 1
    assert same.progress_seq == 1
    assert size_after_first > initial_size
    assert path.stat().st_size == size_after_first

    clock.advance(299)
    assert store.claim_stall(cid, timeout_seconds=300, state_version=9, writer_epoch=3) is False
    clock.advance(1)
    assert store.claim_stall(cid, timeout_seconds=300, state_version=9, writer_epoch=3) is True
    size_after_claim = path.stat().st_size
    # Re-reading the same stalled epoch is reconciliation, not a second claim/effect.
    assert store.claim_stall(cid, timeout_seconds=300, state_version=9, writer_epoch=3) is True
    assert path.stat().st_size == size_after_claim

    store.settle_stall(cid)
    size_after_settle = path.stat().st_size
    assert size_after_settle > size_after_claim
    assert store.claim_stall(cid, timeout_seconds=300, state_version=9, writer_epoch=3) is False
    assert path.stat().st_size == size_after_settle

    clock.advance(1)
    second = store.observe(cid, target, "fingerprint-b", observable=True)
    assert second.progress_seq == 2
    assert path.stat().st_size > size_after_claim


def test_unobservable_window_never_becomes_a_stall_and_resume_restarts_timer(tmp_path: Path) -> None:
    clock = Clock()
    store = MymemLiteStore(tmp_path, clock=clock)
    cid = "00000000-0000-0000-0000-000000000112"
    target = f"https://chatgpt.com/c/{cid}"

    store.ensure(cid, target)
    store.observe(cid, target, "fingerprint-a", observable=True)
    clock.advance(200)
    store.observe(cid, target, None, observable=False)
    clock.advance(1000)

    assert store.claim_stall(cid, timeout_seconds=300, state_version=9, writer_epoch=3) is False

    resumed = store.observe(cid, target, "fingerprint-a", observable=True)
    assert resumed.progress_seq == 2
    clock.advance(299)
    assert store.claim_stall(cid, timeout_seconds=300, state_version=9, writer_epoch=3) is False
    clock.advance(1)
    assert store.claim_stall(cid, timeout_seconds=300, state_version=9, writer_epoch=3) is True


def test_remove_deletes_ephemeral_progress_file(tmp_path: Path) -> None:
    store = MymemLiteStore(tmp_path)
    cid = "00000000-0000-0000-0000-000000000113"
    target = f"https://chatgpt.com/c/{cid}"

    store.ensure(cid, target)
    path = store.path_for(cid)
    assert path.exists()

    store.remove(cid)
    assert not path.exists()

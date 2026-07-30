"""EXPIRED accounts must never be selected for upstream requests."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.control.account.enums import AccountStatus
from app.control.account.models import AccountRecord
from app.dataplane.account.feedback import apply_status_change
from app.dataplane.account.selector import select, select_any, set_strategy
from app.dataplane.account.sync import apply_changes, bootstrap
from app.dataplane.account.table import make_empty_table
from app.dataplane.shared.enums import ModeId, StatusId
from app.platform.runtime.clock import now_s


def _slot_kwargs(
    *,
    status_id: int = int(StatusId.ACTIVE),
    quota_console: int = 20,
    window_console: int = 7200,
) -> dict:
    return dict(
        pool_id=0,
        status_id=status_id,
        quota_auto=10,
        quota_fast=10,
        quota_expert=10,
        quota_heavy=-1,
        quota_grok_4_3=-1,
        quota_console=quota_console,
        total_auto=10,
        total_fast=10,
        total_expert=10,
        total_heavy=0,
        total_grok_4_3=0,
        total_console=20,
        window_auto=7200,
        window_fast=7200,
        window_expert=7200,
        window_heavy=0,
        window_grok_4_3=0,
        window_console=window_console,
        reset_auto=0,
        reset_fast=0,
        reset_expert=0,
        reset_heavy=0,
        reset_grok_4_3=0,
        reset_console=0,
        health=1.0,
        last_use_s=0,
        last_fail_s=0,
        fail_count=0,
        tags=[],
    )


def _record(token: str, status: AccountStatus) -> AccountRecord:
    return AccountRecord(
        token=token,
        pool="basic",
        status=status,
        created_at=1,
        updated_at=1,
    )


class TestExpiredNotInRequestPool(unittest.TestCase):
    def tearDown(self) -> None:
        set_strategy("random")

    def test_expired_slot_not_selected_quota_or_random(self) -> None:
        table = make_empty_table()
        live = table._append_slot("live", **_slot_kwargs())
        dead = table._append_slot(
            "dead", **_slot_kwargs(status_id=int(StatusId.EXPIRED))
        )
        # Simulate a stale index entry that should never be selectable.
        table.mode_available.setdefault((0, int(ModeId.CONSOLE)), set()).add(dead)

        set_strategy("quota")
        ts = now_s()
        for _ in range(20):
            idx = select(table, 0, int(ModeId.CONSOLE), now_s=ts)
            self.assertEqual(idx, live)
            self.assertNotEqual(idx, dead)

        set_strategy("random")
        for _ in range(20):
            idx = select(table, 0, int(ModeId.CONSOLE), now_s=ts)
            self.assertEqual(idx, live)

        any_idx = select_any(table, 0, now_s=ts)
        self.assertEqual(any_idx, live)

    def test_unauthorized_feedback_removes_from_mode_available(self) -> None:
        table = make_empty_table()
        idx = table._append_slot("tok", **_slot_kwargs())
        self.assertIn(idx, table.mode_available[(0, int(ModeId.CONSOLE))])

        apply_status_change(table, idx, int(StatusId.EXPIRED))
        for mode_id in (
            ModeId.AUTO,
            ModeId.FAST,
            ModeId.EXPERT,
            ModeId.CONSOLE,
        ):
            bucket = table.mode_available.get((0, int(mode_id)), set())
            self.assertNotIn(idx, bucket)

        # Idempotent: already EXPIRED still forces index cleanup.
        table.mode_available.setdefault((0, int(ModeId.CONSOLE)), set()).add(idx)
        apply_status_change(table, idx, int(StatusId.EXPIRED))
        self.assertNotIn(idx, table.mode_available[(0, int(ModeId.CONSOLE))])

    def test_bootstrap_skips_expired_and_disabled(self) -> None:
        snapshot = SimpleNamespace(
            revision=42,
            items=[
                _record("live", AccountStatus.ACTIVE),
                _record("dead", AccountStatus.EXPIRED),
                _record("banned", AccountStatus.DISABLED),
            ],
        )
        repo = SimpleNamespace(runtime_snapshot=AsyncMock(return_value=snapshot))

        import asyncio

        table = asyncio.run(bootstrap(repo))  # type: ignore[arg-type]
        self.assertEqual(table.size, 1)
        self.assertIn("live", table.idx_by_token)
        self.assertNotIn("dead", table.idx_by_token)
        self.assertNotIn("banned", table.idx_by_token)

    def test_apply_changes_drops_newly_expired(self) -> None:
        table = make_empty_table()
        idx = table._append_slot("tok", **_slot_kwargs())
        self.assertIn(idx, table.mode_available[(0, int(ModeId.CONSOLE))])

        changeset = SimpleNamespace(
            revision=99,
            batch_max_revision=99,
            has_more=False,
            deleted_tokens=[],
            items=[_record("tok", AccountStatus.EXPIRED)],
        )
        repo = SimpleNamespace(scan_changes=AsyncMock(return_value=changeset))

        import asyncio

        changed = asyncio.run(apply_changes(table, repo))  # type: ignore[arg-type]
        self.assertTrue(changed)
        self.assertEqual(int(table.status_by_idx[idx]), int(StatusId.EXPIRED))
        self.assertNotIn(idx, table.mode_available.get((0, int(ModeId.CONSOLE)), set()))
        # Still present in token map for release/feedback of in-flight leases,
        # but not selectable.
        self.assertIn("tok", table.idx_by_token)
        set_strategy("quota")
        self.assertIsNone(select(table, 0, int(ModeId.CONSOLE), now_s=now_s()))


if __name__ == "__main__":
    unittest.main()

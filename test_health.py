"""Tests for proactive Vinted Agent health monitoring."""

from __future__ import annotations

import signal
import unittest
from unittest.mock import patch

import health_monitor


class HealthMonitorTests(unittest.TestCase):
    """Exercise issue detection and alert-state decisions without I/O."""

    @staticmethod
    def _healthy_snapshot() -> dict:
        return {
            "monitoring_paused": False,
            "agent_service": "active",
            "commands_service": "active",
            "agent_uptime_seconds": 3600,
            "agent_memory_bytes": 700 * 1024 * 1024,
            "browser_processes": 8,
            "database": {
                "ok": True,
                "listings": 1000,
                "pending_notifications": 0,
                "failed_notifications": 0,
                "latest_activity_age_minutes": 2.0,
                "oldest_queue_age_minutes": None,
            },
            "disk": {
                "ok": True,
                "free_bytes": 20 * 1024 * 1024 * 1024,
                "free_percent": 50.0,
            },
            "telegram": {
                "api_ok": True,
                "webhook_url": "",
            },
        }

    def test_healthy_snapshot_has_no_issues(self) -> None:
        issues = health_monitor.evaluate_issues(
            self._healthy_snapshot()
        )
        self.assertEqual(issues, [])

    def test_detects_service_queue_browser_and_webhook_failures(self) -> None:
        snapshot = self._healthy_snapshot()
        snapshot["commands_service"] = "inactive"
        snapshot["browser_processes"] = 0
        snapshot["database"]["failed_notifications"] = 2
        snapshot["telegram"]["webhook_url"] = (
            "https://example.com/webhook"
        )

        codes = {
            issue["code"]
            for issue in health_monitor.evaluate_issues(
                snapshot
            )
        }

        self.assertTrue(
            {
                "commands_service",
                "chromium",
                "telegram_queue_failed",
                "telegram_webhook",
            }.issubset(codes)
        )

    def test_stale_database_is_ignored_while_monitoring_is_paused(self) -> None:
        snapshot = self._healthy_snapshot()
        snapshot["monitoring_paused"] = True
        snapshot["database"]["latest_activity_age_minutes"] = 120.0

        codes = {
            issue["code"]
            for issue in health_monitor.evaluate_issues(
                snapshot
            )
        }

        self.assertNotIn("database_stale", codes)

    def test_notification_action_alerts_once_and_recovers(self) -> None:
        issue = [
            {
                "code": "database",
                "message": "database failed",
            }
        ]

        self.assertEqual(
            health_monitor.notification_action([], issue),
            "alert",
        )
        self.assertIsNone(
            health_monitor.notification_action(
                ["database"],
                issue,
            )
        )
        self.assertEqual(
            health_monitor.notification_action(
                ["database"],
                [],
            ),
            "recovery",
        )

    def test_self_heal_restarts_once_for_stalled_database(self) -> None:
        issues = [
            {
                "code": "database_stale",
                "message": "database activity is 23 minutes old",
            }
        ]

        self.assertTrue(
            health_monitor.self_heal_action(
                [],
                issues,
                "active",
            )
        )
        self.assertFalse(
            health_monitor.self_heal_action(
                ["database_stale"],
                issues,
                "active",
            )
        )

    def test_self_heal_ignores_queue_only_problem(self) -> None:
        issues = [
            {
                "code": "telegram_queue_stale",
                "message": "oldest queued Telegram alert is 23 minutes old",
            }
        ]

        self.assertFalse(
            health_monitor.self_heal_action(
                [],
                issues,
                "active",
            )
        )

    def test_self_heal_does_not_signal_inactive_agent(self) -> None:
        issues = [
            {
                "code": "chromium",
                "message": "Chromium is not detected in the agent service",
            }
        ]

        self.assertFalse(
            health_monitor.self_heal_action(
                [],
                issues,
                "inactive",
            )
        )

    def test_restart_agent_signals_main_pid_and_observes_replacement(self) -> None:
        with (
            patch.object(
                health_monitor,
                "_service_main_pid",
                side_effect=[1234, 5678],
            ),
            patch.object(
                health_monitor.os,
                "kill",
            ) as kill,
        ):
            restarted = health_monitor._restart_agent_process(
                grace_seconds=1
            )

        self.assertTrue(restarted)
        kill.assert_called_once_with(
            1234,
            signal.SIGTERM,
        )

    def test_health_message_contains_operational_metrics(self) -> None:
        snapshot = self._healthy_snapshot()
        snapshot["issues"] = []

        message = health_monitor.build_health_message(
            snapshot
        )

        for text in (
            "VINTED AGENT HEALTH",
            "Agent: active",
            "Commands: active",
            "Memory:",
            "Chromium processes:",
            "Queue:",
            "Disk free:",
            "Telegram API: OK",
            "Webhook: none",
        ):
            self.assertIn(text, message)

    def test_alert_message_mentions_automatic_recovery(self) -> None:
        snapshot = self._healthy_snapshot()
        snapshot["issues"] = [
            {
                "code": "database_stale",
                "message": "database activity is 23 minutes old",
            }
        ]
        snapshot["auto_restart_planned"] = True

        message = health_monitor.build_health_message(
            snapshot,
            alert=True,
        )

        self.assertIn(
            "Automatic recovery: restarting vinted-agent",
            message,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)

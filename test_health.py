"""Tests for proactive Vinted Agent health monitoring."""

from __future__ import annotations

import unittest

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
        issues = health_monitor.evaluate_issues(self._healthy_snapshot())
        self.assertEqual(issues, [])

    def test_detects_service_queue_browser_and_webhook_failures(self) -> None:
        snapshot = self._healthy_snapshot()
        snapshot["commands_service"] = "inactive"
        snapshot["browser_processes"] = 0
        snapshot["database"]["failed_notifications"] = 2
        snapshot["telegram"]["webhook_url"] = "https://example.com/webhook"

        codes = {
            issue["code"]
            for issue in health_monitor.evaluate_issues(snapshot)
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
            for issue in health_monitor.evaluate_issues(snapshot)
        }

        self.assertNotIn("database_stale", codes)

    def test_notification_action_alerts_once_and_recovers(self) -> None:
        issue = [{"code": "database", "message": "database failed"}]

        self.assertEqual(
            health_monitor.notification_action([], issue),
            "alert",
        )
        self.assertIsNone(
            health_monitor.notification_action(["database"], issue)
        )
        self.assertEqual(
            health_monitor.notification_action(["database"], []),
            "recovery",
        )

    def test_health_message_contains_operational_metrics(self) -> None:
        snapshot = self._healthy_snapshot()
        snapshot["issues"] = []

        message = health_monitor.build_health_message(snapshot)

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


if __name__ == "__main__":
    unittest.main(verbosity=2)

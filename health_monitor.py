"""Proactive health monitoring and self-healing for Vinted Agent."""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

import requests

from config import BOT_TOKEN, CHAT_ID, DATABASE_PATH, DATA_DIR
from runtime_state import is_paused, write_health


LOGGER = logging.getLogger(__name__)

AGENT_SERVICE: Final[str] = "vinted-agent.service"
COMMAND_SERVICE: Final[str] = "vinted-commands.service"
DATABASE_STALE_MINUTES: Final[float] = 15.0
QUEUE_STALE_MINUTES: Final[float] = 15.0
MEMORY_WARNING_BYTES: Final[int] = 2560 * 1024 * 1024
DISK_WARNING_BYTES: Final[int] = 2 * 1024 * 1024 * 1024
DISK_WARNING_PERCENT: Final[float] = 10.0
TELEGRAM_TIMEOUT: Final[tuple[int, int]] = (5, 10)
SELF_HEAL_GRACE_SECONDS: Final[int] = 15
SELF_HEAL_ISSUE_CODES: Final[frozenset[str]] = frozenset(
    {
        "database_stale",
        "chromium",
    }
)
ALERT_STATE_FILE: Final[Path] = DATA_DIR / "health-alert-state.json"
SELF_HEAL_STATE_FILE: Final[Path] = DATA_DIR / "health-self-heal-state.json"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timestamp(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None

    for candidate in (text, text.removesuffix(" UTC")):
        try:
            parsed = datetime.strptime(candidate, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        return parsed.replace(tzinfo=timezone.utc)

    return None


def _age_minutes(value: object, now: datetime) -> float | None:
    parsed = _parse_timestamp(value)
    if parsed is None:
        return None
    return max(0.0, (now - parsed).total_seconds() / 60.0)


def _run_systemctl(*args: str) -> str:
    try:
        result = subprocess.run(
            ["systemctl", *args],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""

    return (result.stdout or result.stderr or "").strip()


def _service_state(unit: str) -> str:
    return _run_systemctl("is-active", unit) or "unknown"


def _service_property(unit: str, name: str) -> str:
    return _run_systemctl(
        "show",
        unit,
        f"--property={name}",
        "--value",
    )


def _service_main_pid(unit: str) -> int | None:
    value = _service_property(unit, "MainPID")
    try:
        pid = int(value)
    except (TypeError, ValueError):
        return None
    return pid if pid > 1 else None


def _service_memory_bytes(unit: str) -> int | None:
    value = _service_property(unit, "MemoryCurrent")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _service_uptime_seconds(unit: str) -> float | None:
    active_value = _service_property(
        unit,
        "ActiveEnterTimestampMonotonic",
    )

    try:
        active_seconds = int(active_value) / 1_000_000.0
        system_uptime = float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None

    if active_seconds <= 0:
        return None

    return max(0.0, system_uptime - active_seconds)


def _browser_process_count(unit: str) -> int | None:
    control_group = _service_property(unit, "ControlGroup")
    if not control_group:
        return None

    process_file = (
        Path("/sys/fs/cgroup")
        / control_group.lstrip("/")
        / "cgroup.procs"
    )

    try:
        process_ids = process_file.read_text(encoding="utf-8").split()
    except OSError:
        return None

    count = 0
    for process_id in process_ids:
        try:
            command = (
                Path("/proc") / process_id / "cmdline"
            ).read_bytes().replace(b"\x00", b" ").decode(
                "utf-8",
                errors="ignore",
            ).casefold()
        except OSError:
            continue

        if (
            "chromedriver" in command
            or "google-chrome" in command
            or "chromium" in command
        ):
            count += 1

    return count


def _database_snapshot(now: datetime) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ok": False,
        "listings": 0,
        "pending_notifications": 0,
        "failed_notifications": 0,
        "latest_activity": None,
        "latest_activity_age_minutes": None,
        "oldest_queue_age_minutes": None,
    }

    try:
        connection = sqlite3.connect(
            str(DATABASE_PATH),
            timeout=10,
        )
        connection.row_factory = sqlite3.Row

        try:
            total = connection.execute(
                "SELECT COUNT(*) AS count FROM listings"
            ).fetchone()

            latest = connection.execute(
                """
                SELECT MAX(
                    MAX(
                        COALESCE(last_seen, ''),
                        COALESCE(status_checked_at, ''),
                        COALESCE(first_seen, '')
                    )
                ) AS value
                FROM listings
                """
            ).fetchone()

            queue = connection.execute(
                """
                SELECT
                    SUM(CASE WHEN state = 'pending' THEN 1 ELSE 0 END) AS pending,
                    SUM(CASE WHEN state = 'failed' THEN 1 ELSE 0 END) AS failed,
                    MIN(
                        CASE
                            WHEN state IN ('pending', 'failed')
                            THEN created_at
                        END
                    ) AS oldest
                FROM notification_queue
                """
            ).fetchone()
        finally:
            connection.close()

    except sqlite3.Error:
        LOGGER.exception("Health monitor could not read the database")
        return result

    latest_value = latest["value"] if latest else None
    oldest_value = queue["oldest"] if queue else None

    result.update(
        {
            "ok": True,
            "listings": int(total["count"] or 0) if total else 0,
            "pending_notifications": (
                int(queue["pending"] or 0) if queue else 0
            ),
            "failed_notifications": (
                int(queue["failed"] or 0) if queue else 0
            ),
            "latest_activity": latest_value,
            "latest_activity_age_minutes": _age_minutes(
                latest_value,
                now,
            ),
            "oldest_queue_age_minutes": _age_minutes(
                oldest_value,
                now,
            ),
        }
    )
    return result


def _disk_snapshot() -> dict[str, Any]:
    try:
        usage = shutil.disk_usage(DATA_DIR)
    except OSError:
        return {"ok": False}

    free_percent = (
        usage.free / usage.total * 100.0
        if usage.total
        else 0.0
    )

    return {
        "ok": True,
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
        "free_percent": free_percent,
    }


def _telegram_snapshot() -> dict[str, Any]:
    result: dict[str, Any] = {
        "api_ok": False,
        "webhook_url": None,
    }

    if not BOT_TOKEN:
        return result

    api = f"https://api.telegram.org/bot{BOT_TOKEN}"

    try:
        identity = requests.get(
            f"{api}/getMe",
            timeout=TELEGRAM_TIMEOUT,
        )
        identity_payload = identity.json()
        result["api_ok"] = bool(
            identity.ok
            and identity_payload.get("ok") is True
        )

        webhook = requests.get(
            f"{api}/getWebhookInfo",
            timeout=TELEGRAM_TIMEOUT,
        )
        webhook_payload = webhook.json()
        if webhook.ok and webhook_payload.get("ok") is True:
            webhook_result = webhook_payload.get("result") or {}
            result["webhook_url"] = str(
                webhook_result.get("url") or ""
            )
    except (requests.RequestException, ValueError):
        LOGGER.warning("Telegram health request failed")

    return result


def collect_health(now: datetime | None = None) -> dict[str, Any]:
    """Collect one complete health snapshot without changing agent state."""

    checked_at = now or _utc_now()
    agent_state = _service_state(AGENT_SERVICE)
    commands_state = _service_state(COMMAND_SERVICE)

    snapshot: dict[str, Any] = {
        "checked_at": checked_at.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "monitoring_paused": is_paused(),
        "agent_service": agent_state,
        "commands_service": commands_state,
        "agent_uptime_seconds": _service_uptime_seconds(AGENT_SERVICE),
        "agent_memory_bytes": _service_memory_bytes(AGENT_SERVICE),
        "browser_processes": (
            _browser_process_count(AGENT_SERVICE)
            if agent_state == "active"
            else 0
        ),
        "database": _database_snapshot(checked_at),
        "disk": _disk_snapshot(),
        "telegram": _telegram_snapshot(),
    }

    issues = evaluate_issues(snapshot)
    snapshot["issues"] = issues
    snapshot["status"] = "healthy" if not issues else "unhealthy"
    return snapshot


def evaluate_issues(snapshot: dict[str, Any]) -> list[dict[str, str]]:
    """Return stable issue codes and human-readable descriptions."""

    issues: list[dict[str, str]] = []

    if snapshot.get("agent_service") != "active":
        issues.append(
            {
                "code": "agent_service",
                "message": "vinted-agent service is not active",
            }
        )

    if snapshot.get("commands_service") != "active":
        issues.append(
            {
                "code": "commands_service",
                "message": "vinted-commands service is not active",
            }
        )

    database = snapshot.get("database") or {}
    if not database.get("ok"):
        issues.append(
            {
                "code": "database",
                "message": "database health check failed",
            }
        )
    else:
        age = database.get("latest_activity_age_minutes")
        if (
            not snapshot.get("monitoring_paused")
            and age is not None
            and float(age) > DATABASE_STALE_MINUTES
        ):
            issues.append(
                {
                    "code": "database_stale",
                    "message": (
                        f"database activity is {float(age):.0f} minutes old"
                    ),
                }
            )

        failed = int(database.get("failed_notifications") or 0)
        if failed:
            issues.append(
                {
                    "code": "telegram_queue_failed",
                    "message": (
                        f"{failed} Telegram notification(s) are failed"
                    ),
                }
            )

        queue_age = database.get("oldest_queue_age_minutes")
        if (
            queue_age is not None
            and float(queue_age) > QUEUE_STALE_MINUTES
        ):
            issues.append(
                {
                    "code": "telegram_queue_stale",
                    "message": (
                        "oldest queued Telegram alert is "
                        f"{float(queue_age):.0f} minutes old"
                    ),
                }
            )

    if snapshot.get("agent_service") == "active":
        browser_processes = snapshot.get("browser_processes")
        if browser_processes == 0:
            issues.append(
                {
                    "code": "chromium",
                    "message": (
                        "Chromium is not detected in the agent service"
                    ),
                }
            )

    memory_bytes = snapshot.get("agent_memory_bytes")
    if (
        memory_bytes is not None
        and int(memory_bytes) > MEMORY_WARNING_BYTES
    ):
        issues.append(
            {
                "code": "memory",
                "message": (
                    f"agent memory is {int(memory_bytes) / 1024**3:.1f} GiB"
                ),
            }
        )

    disk = snapshot.get("disk") or {}
    if not disk.get("ok"):
        issues.append(
            {
                "code": "disk",
                "message": "disk usage check failed",
            }
        )
    else:
        free_bytes = int(disk.get("free_bytes") or 0)
        free_percent = float(disk.get("free_percent") or 0.0)
        if (
            free_bytes < DISK_WARNING_BYTES
            or free_percent < DISK_WARNING_PERCENT
        ):
            issues.append(
                {
                    "code": "disk_low",
                    "message": (
                        "disk free space is "
                        f"{free_bytes / 1024**3:.1f} GiB "
                        f"({free_percent:.1f}%)"
                    ),
                }
            )

    telegram = snapshot.get("telegram") or {}
    if not telegram.get("api_ok"):
        issues.append(
            {
                "code": "telegram_api",
                "message": "Telegram Bot API check failed",
            }
        )

    webhook_url = str(telegram.get("webhook_url") or "").strip()
    if webhook_url:
        issues.append(
            {
                "code": "telegram_webhook",
                "message": (
                    "Telegram webhook is configured; "
                    "polling commands will conflict"
                ),
            }
        )

    return issues


def _issue_codes(issues: list[dict[str, str]]) -> list[str]:
    return sorted(
        str(issue.get("code") or "")
        for issue in issues
        if issue.get("code")
    )


def _self_heal_issue_codes(
    issues: list[dict[str, str]],
) -> list[str]:
    return sorted(
        code
        for code in _issue_codes(issues)
        if code in SELF_HEAL_ISSUE_CODES
    )


def _load_json_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_json_state(path: Path, payload: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(path)


def _load_alert_state() -> dict[str, Any]:
    return _load_json_state(ALERT_STATE_FILE)


def _save_alert_state(codes: list[str]) -> None:
    _save_json_state(
        ALERT_STATE_FILE,
        {
            "notified_issue_codes": sorted(codes),
            "updated_at": _utc_now().strftime("%Y-%m-%d %H:%M:%S UTC"),
        },
    )


def _load_self_heal_state() -> dict[str, Any]:
    return _load_json_state(SELF_HEAL_STATE_FILE)


def _save_self_heal_state(codes: list[str]) -> None:
    _save_json_state(
        SELF_HEAL_STATE_FILE,
        {
            "handled_issue_codes": sorted(codes),
            "updated_at": _utc_now().strftime("%Y-%m-%d %H:%M:%S UTC"),
        },
    )


def notification_action(
    previous_codes: list[str],
    current_issues: list[dict[str, str]],
) -> str | None:
    """Return alert/recovery when the notified health state changed."""

    old = sorted(previous_codes)
    new = _issue_codes(current_issues)

    if old == new:
        return None
    if new:
        return "alert"
    if old:
        return "recovery"
    return None


def self_heal_action(
    previous_codes: list[str],
    current_issues: list[dict[str, str]],
    agent_state: str,
) -> bool:
    """Return True when this health episode warrants one agent restart."""

    if agent_state != "active":
        return False

    current = set(_self_heal_issue_codes(current_issues))
    previous = set(previous_codes)

    if not current:
        return False

    return not current.issubset(previous)


def _restart_agent_process(
    grace_seconds: int = SELF_HEAL_GRACE_SECONDS,
) -> bool:
    """Signal the agent process so systemd Restart=always can recover it."""

    pid = _service_main_pid(AGENT_SERVICE)
    if pid is None:
        LOGGER.error("Automatic recovery could not find agent MainPID")
        return False

    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError) as exc:
        LOGGER.error("Automatic recovery could not signal agent: %s", exc)
        return False

    LOGGER.warning(
        "Automatic recovery sent SIGTERM to vinted-agent MainPID %d",
        pid,
    )

    deadline = time.monotonic() + max(0, grace_seconds)
    while time.monotonic() < deadline:
        current_pid = _service_main_pid(AGENT_SERVICE)
        if current_pid != pid:
            return True
        time.sleep(1)

    current_pid = _service_main_pid(AGENT_SERVICE)
    if current_pid != pid:
        return True

    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except (PermissionError, OSError) as exc:
        LOGGER.error(
            "Automatic recovery could not force-stop agent: %s",
            exc,
        )
        return False

    LOGGER.error(
        "Automatic recovery escalated to SIGKILL for vinted-agent MainPID %d",
        pid,
    )
    return True


def _format_duration(seconds: object) -> str:
    if seconds is None:
        return "n/a"

    total = max(0, int(float(seconds)))
    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60

    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def build_health_message(
    snapshot: dict[str, Any],
    *,
    alert: bool = False,
) -> str:
    """Build a compact Telegram health summary."""

    issues = snapshot.get("issues") or []
    database = snapshot.get("database") or {}
    disk = snapshot.get("disk") or {}
    telegram = snapshot.get("telegram") or {}

    if alert and issues:
        lines = ["🚨 VINTED AGENT HEALTH ALERT", ""]
        lines.extend(
            f"• {issue['message']}"
            for issue in issues
        )

        if snapshot.get("auto_restart_planned"):
            lines.extend(
                [
                    "",
                    "♻️ Automatic recovery: restarting vinted-agent",
                ]
            )
    elif alert:
        lines = ["✅ VINTED AGENT RECOVERED"]
    else:
        icon = "✅" if not issues else "⚠️"
        lines = [f"{icon} VINTED AGENT HEALTH"]

    lines.extend(
        [
            "",
            f"Agent: {snapshot.get('agent_service', 'unknown')}",
            f"Commands: {snapshot.get('commands_service', 'unknown')}",
            (
                "Monitoring: paused"
                if snapshot.get("monitoring_paused")
                else "Monitoring: running"
            ),
            f"Uptime: {_format_duration(snapshot.get('agent_uptime_seconds'))}",
            (
                "Memory: n/a"
                if snapshot.get("agent_memory_bytes") is None
                else (
                    "Memory: "
                    f"{int(snapshot['agent_memory_bytes']) / 1024**2:.0f} MiB"
                )
            ),
            (
                "Chromium processes: "
                f"{snapshot.get('browser_processes', 'n/a')}"
            ),
            (
                "Database: "
                f"{'OK' if database.get('ok') else 'ERROR'} "
                f"({int(database.get('listings') or 0)} listings)"
            ),
            (
                "Queue: "
                f"{int(database.get('pending_notifications') or 0)} pending / "
                f"{int(database.get('failed_notifications') or 0)} failed"
            ),
            (
                "Disk free: n/a"
                if not disk.get("ok")
                else (
                    "Disk free: "
                    f"{int(disk.get('free_bytes') or 0) / 1024**3:.1f} GiB "
                    f"({float(disk.get('free_percent') or 0):.1f}%)"
                )
            ),
            (
                "Telegram API: OK"
                if telegram.get("api_ok")
                else "Telegram API: ERROR"
            ),
            (
                "Webhook: none"
                if not telegram.get("webhook_url")
                else "Webhook: CONFIGURED"
            ),
        ]
    )

    return "\n".join(lines)


def _send_telegram(message: str) -> bool:
    if not BOT_TOKEN or not CHAT_ID:
        LOGGER.error("Telegram BOT_TOKEN or CHAT_ID is missing")
        return False

    try:
        response = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={
                "chat_id": CHAT_ID,
                "text": message,
                "disable_web_page_preview": True,
            },
            timeout=TELEGRAM_TIMEOUT,
        )
        payload = response.json()
    except (requests.RequestException, ValueError):
        LOGGER.warning("Unable to send Telegram health alert")
        return False

    if not response.ok or payload.get("ok") is not True:
        LOGGER.warning("Telegram rejected health alert")
        return False

    return True


def run_health_check() -> dict[str, Any]:
    """Collect health, alert on changes, and self-heal a stalled agent once."""

    snapshot = collect_health()

    alert_state = _load_alert_state()
    previous_alert_codes = (
        alert_state.get("notified_issue_codes") or []
    )
    if not isinstance(previous_alert_codes, list):
        previous_alert_codes = []

    self_heal_state = _load_self_heal_state()
    previous_heal_codes = (
        self_heal_state.get("handled_issue_codes") or []
    )
    if not isinstance(previous_heal_codes, list):
        previous_heal_codes = []

    current_heal_codes = _self_heal_issue_codes(
        snapshot["issues"]
    )
    should_restart = self_heal_action(
        previous_heal_codes,
        snapshot["issues"],
        str(snapshot.get("agent_service") or ""),
    )
    snapshot["auto_restart_planned"] = should_restart

    action = notification_action(
        previous_alert_codes,
        snapshot["issues"],
    )

    if action == "alert":
        sent = _send_telegram(
            build_health_message(snapshot, alert=True)
        )
        if sent:
            _save_alert_state(_issue_codes(snapshot["issues"]))
    elif action == "recovery":
        sent = _send_telegram(
            build_health_message(snapshot, alert=True)
        )
        if sent:
            _save_alert_state([])
    elif (
        not ALERT_STATE_FILE.exists()
        and not snapshot["issues"]
    ):
        _save_alert_state([])

    if should_restart:
        restarted = _restart_agent_process()
        snapshot["auto_restart_requested"] = restarted
        if restarted:
            _save_self_heal_state(current_heal_codes)
    elif not current_heal_codes and previous_heal_codes:
        _save_self_heal_state([])

    write_health(snapshot)

    LOGGER.info(
        "Health check: %s (%d issue%s)%s",
        snapshot["status"],
        len(snapshot["issues"]),
        "" if len(snapshot["issues"]) == 1 else "s",
        " | automatic restart requested"
        if snapshot.get("auto_restart_requested")
        else "",
    )

    return snapshot


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    run_health_check()


if __name__ == "__main__":
    main()

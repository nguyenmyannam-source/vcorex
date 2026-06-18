"""
Scheduled maintenance tasks for bot health and consistency.
Handles orphan cleanup, reconciliation, and other periodic maintenance.
"""

from loguru import logger
from core.scheduler import task_scheduler
from typing import Optional
import asyncio


class MaintenanceScheduler:
    """Manages periodic maintenance tasks."""

    _instance: Optional["MaintenanceScheduler"] = None
    _reconciliation_service = None
    _task_ids = {}

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    @classmethod
    def set_reconciliation_service(cls, reconciliation_service) -> None:
        """Set the reconciliation service instance."""
        cls._reconciliation_service = reconciliation_service
        logger.info("MaintenanceScheduler: Reconciliation service configured")

    @classmethod
    async def register_periodic_tasks(cls) -> None:
        """Register all periodic maintenance tasks with the scheduler."""
        if not cls._reconciliation_service:
            logger.warning("MaintenanceScheduler: Reconciliation service not set, skipping task registration")
            return

        try:
            # Task 1: Orphan algo order detection and cleanup (every 5 minutes)
            orphan_cleanup_task_id = task_scheduler.add_recurring_task(
                name="orphan_algo_cleanup",
                callback=cls._cleanup_orphan_algos_periodic,
                interval_seconds=300,  # 5 minutes
                max_retries=2,
                retry_delay=5.0,
                enabled=True,
            )
            cls._task_ids["orphan_cleanup"] = orphan_cleanup_task_id
            logger.info(f"Registered orphan cleanup task: {orphan_cleanup_task_id}")

            # Task 2: Full reconciliation (every 10 minutes)
            reconcile_task_id = task_scheduler.add_recurring_task(
                name="full_reconciliation",
                callback=cls._full_reconciliation_periodic,
                interval_seconds=600,  # 10 minutes
                max_retries=2,
                retry_delay=5.0,
                enabled=True,
            )
            cls._task_ids["reconcile"] = reconcile_task_id
            logger.info(f"Registered full reconciliation task: {reconcile_task_id}")

        except Exception as e:
            logger.error(f"Failed to register maintenance tasks: {e}")

    @classmethod
    async def _cleanup_orphan_algos_periodic(cls) -> None:
        """Periodic task to detect and cleanup orphan algo orders."""
        if not cls._reconciliation_service:
            logger.warning("MaintenanceScheduler: Reconciliation service not available")
            return

        try:
            logger.info("[MAINTENANCE] Running orphan algo cleanup check...")

            # Get all local positions
            local_positions = []
            if hasattr(cls._reconciliation_service, "order_handler"):
                local_positions = list(cls._reconciliation_service.order_handler._positions.values())

            # Detect orphan algo orders
            orphan_algos = await cls._reconciliation_service._detect_orphan_algo_orders(local_positions)

            if orphan_algos:
                logger.warning(f"[MAINTENANCE] Detected {len(orphan_algos)} orphan algo orders")

                # Auto-cleanup
                cleanup_results = await cls._reconciliation_service.cleanup_orphan_algo_orders(orphan_algos)

                logger.info(
                    f"[MAINTENANCE-CLEANUP] Orphan cleanup complete: "
                    f"{cleanup_results.get('success', 0)} success, "
                    f"{cleanup_results.get('failed', 0)} failed"
                )
            else:
                logger.debug("[MAINTENANCE] No orphan algo orders detected")

        except Exception as e:
            # Log error but don't crash - orphan detection is secondary maintenance
            logger.warning(f"[MAINTENANCE-ERROR] Orphan cleanup skipped: {e}")
            # Do NOT raise - allow bot to continue even if orphan detection fails

    @classmethod
    async def _full_reconciliation_periodic(cls) -> None:
        """Periodic task to run full reconciliation check."""
        if not cls._reconciliation_service:
            logger.warning("MaintenanceScheduler: Reconciliation service not available")
            return

        try:
            logger.info("[MAINTENANCE] Running full reconciliation check...")

            # Call reconcile_all
            anomalies = await cls._reconciliation_service.reconcile_all()

            # Log summary
            total_anomalies = sum(len(v) if isinstance(v, list) else 1 for v in anomalies.values())
            if total_anomalies > 0:
                logger.warning(f"[MAINTENANCE] Full reconciliation found {total_anomalies} anomalies:")
                for anomaly_type, details in anomalies.items():
                    if details:
                        logger.warning(f"  - {anomaly_type}: {len(details) if isinstance(details, list) else 1}")
            else:
                logger.info("[MAINTENANCE] Full reconciliation: No anomalies detected")

        except Exception as e:
            logger.error(f"[MAINTENANCE-ERROR] Error during full reconciliation: {e}", exc_info=True)

    @classmethod
    def disable_task(cls, task_name: str) -> bool:
        """Disable a specific maintenance task."""
        try:
            task_id = cls._task_ids.get(task_name)
            if not task_id:
                logger.warning(f"MaintenanceScheduler: Task '{task_name}' not found")
                return False

            if task_id in task_scheduler._tasks:
                task_scheduler._tasks[task_id].enabled = False
                logger.info(f"[MAINTENANCE] Disabled task: {task_name}")
                return True
            return False
        except Exception as e:
            logger.error(f"Error disabling task '{task_name}': {e}")
            return False

    @classmethod
    def enable_task(cls, task_name: str) -> bool:
        """Enable a specific maintenance task."""
        try:
            task_id = cls._task_ids.get(task_name)
            if not task_id:
                logger.warning(f"MaintenanceScheduler: Task '{task_name}' not found")
                return False

            if task_id in task_scheduler._tasks:
                task_scheduler._tasks[task_id].enabled = True
                logger.info(f"[MAINTENANCE] Enabled task: {task_name}")
                return True
            return False
        except Exception as e:
            logger.error(f"Error enabling task '{task_name}': {e}")
            return False

    @classmethod
    def get_task_status(cls, task_name: str) -> dict:
        """Get status of a specific maintenance task."""
        try:
            task_id = cls._task_ids.get(task_name)
            if not task_id or task_id not in task_scheduler._tasks:
                return {"status": "not_found", "task_name": task_name}

            task = task_scheduler._tasks[task_id]
            return {
                "task_name": task_name,
                "task_id": task_id,
                "enabled": task.enabled,
                "interval_seconds": task.interval,
                "last_run": task.last_run,
                "next_run": task.next_run,
                "total_runs": task.total_runs,
                "total_failures": task.total_failures,
            }
        except Exception as e:
            logger.error(f"Error getting task status for '{task_name}': {e}")
            return {"status": "error", "task_name": task_name, "error": str(e)}

    @classmethod
    def get_all_task_status(cls) -> dict:
        """Get status of all maintenance tasks."""
        return {
            "orphan_cleanup": cls.get_task_status("orphan_cleanup"),
            "reconcile": cls.get_task_status("reconcile"),
        }


# Singleton instance
maintenance_scheduler = MaintenanceScheduler()

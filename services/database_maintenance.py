import asyncio

from loguru import logger

class DatabaseMaintenanceService:
    def __init__(self, db_session_factory=None, *args, **kwargs):
        self.db_session_factory = db_session_factory or kwargs.get("session_factory")
        self.engine = kwargs.get("engine")
        self.retention_days = kwargs.get("retention_days", 30)
        self.args = args
        self.kwargs = kwargs
        self._is_running = False
        self._task = None

    async def start(self) -> None:
        """Kích hoạt dịch vụ bảo trì database chạy ngầm."""
        if self._is_running:
            return
        self._is_running = True
        logger.info("DatabaseMaintenanceService started.")
        self._task = asyncio.create_task(self._maintenance_loop())

    async def stop(self) -> None:
        """Dừng dịch vụ bảo trì an toàn."""
        self._is_running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("DatabaseMaintenanceService stopped.")

    async def run_maintenance(self) -> None:
        """Thực hiện các thao tác dọn dẹp database một cách an toàn.

        - Dùng async with cho AsyncSession (đúng chuẩn SQLAlchemy async).
        - Dùng run_in_executor cho VACUUM vì engine là synchronous.
        """
        logger.info("[DB-MAINTENANCE] Starting database maintenance...")
        try:
            if self.db_session_factory is None:
                logger.debug("[DB-MAINTENANCE] db_session_factory is None (Chế độ In-Memory/Phòng thủ). Bỏ qua dọn dẹp DB.")
                return

            if not callable(self.db_session_factory):
                logger.warning("[DB-MAINTENANCE] db_session_factory không phải là hàm có thể gọi (not callable).")
                return

            from datetime import datetime, timedelta, timezone
            from sqlalchemy import delete, text
            from infrastructure.storage.database import AuditLog, DeadLetterEvent, StateSnapshot

            cutoff = datetime.now(timezone.utc) - timedelta(days=self.retention_days)

            # Dọn dẹp bảng log cũ — dùng async with vì db_session_factory trả về AsyncSession
            async with self.db_session_factory() as session:
                async with session.begin():
                    await session.execute(delete(AuditLog).where(AuditLog.timestamp < cutoff))
                    await session.execute(delete(DeadLetterEvent).where(DeadLetterEvent.created_at < cutoff))
                    await session.execute(delete(StateSnapshot).where(StateSnapshot.timestamp < cutoff))

            # VACUUM SQLite — engine là synchronous, phải chạy trong thread pool
            if self.engine and getattr(self.engine.url, "drivername", "") == "sqlite":
                engine_ref = self.engine

                def _vacuum():
                    with engine_ref.connect() as conn:
                        conn.execution_options(isolation_level="AUTOCOMMIT").execute(text("VACUUM"))

                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, _vacuum)

            logger.info("[DB-MAINTENANCE] ✅ Database maintenance completed successfully.")

        except Exception as e:
            logger.error(f"[DB-MAINTENANCE] Lỗi xử lý trong lệnh run_maintenance: {str(e)}")

    async def _maintenance_loop(self) -> None:
        """Vòng lặp chạy ngầm định kỳ gọi lệnh bảo trì."""
        MAINTENANCE_INTERVAL = 3600  # Chạy dọn dẹp mỗi giờ một lần (hoặc tùy chỉnh)

        # Chờ một lúc sau khi khởi động bot mới chạy lượt đầu tiên
        await asyncio.sleep(10)

        while self._is_running:
            try:
                await self.run_maintenance()
            except Exception as e:
                logger.error(f"[DB-MAINTENANCE] Error running database maintenance: {str(e)}")

            try:
                await asyncio.sleep(MAINTENANCE_INTERVAL)
            except asyncio.CancelledError:
                break

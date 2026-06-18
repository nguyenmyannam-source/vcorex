
import asyncio
import time
from typing import Any, Optional
from sqlalchemy import event
from loguru import logger

sqlite_metrics = {"slow_query_count": 0, "total_queries": 0}

class DatabaseAdapter:
    def __init__(self, session_factory):
        self.session_factory = session_factory
        self.engine = session_factory.kw['bind']

    def initialize(self):
        logger.info("DatabaseAdapter initialized.")
        if hasattr(self.engine, "sync_engine"):
            sync_engine = self.engine.sync_engine
            
            import sqlalchemy
            if isinstance(sync_engine, sqlalchemy.engine.Engine):
                @event.listens_for(sync_engine, "before_cursor_execute")
                def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
                    conn.info.setdefault("query_start_time", []).append(time.perf_counter())

                @event.listens_for(sync_engine, "after_cursor_execute")
                def after_cursor_execute(conn, cursor, statement, parameters, context, executemany):
                    start_time = conn.info["query_start_time"].pop(-1)
                    total_time = time.perf_counter() - start_time
                    sqlite_metrics["total_queries"] += 1
                    if total_time > 1.0:  # slow query threshold
                        sqlite_metrics["slow_query_count"] += 1
                        logger.warning(f"[DB-SLOW-QUERY] Query took {total_time:.2f}s: {statement}")

    def close(self):
        if self.engine:
            self.engine.dispose()
            logger.info("DatabaseAdapter connections closed.")

    async def save_position(self, position_obj: Any) -> bool:
        session = self.session_factory()
        try:
            session.add(position_obj)
            await asyncio.to_thread(session.commit)
            logger.info(f"[DB-SUCCESS] Position {getattr(position_obj, 'position_id', 'UNKNOWN')} saved securely via OS Thread.")
            return True
        except Exception as e:
            logger.error(f"[DB-ERROR] Thất bại khi ghi đè vị thế xuống SQLite: {str(e)}", exc_info=True)
            await asyncio.to_thread(session.rollback)
            return False
        finally:
            await asyncio.to_thread(session.close)

def create_database_adapter(session_factory) -> DatabaseAdapter:
    """Factory function bắt buộc của kiến trúc VCOREX"""
    return DatabaseAdapter(session_factory)

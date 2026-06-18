"""
Tamper-proof audit journal for VCOREX.
Implements append-only storage, SHA-256 hash chains, and asynchronous queue-based batch writing.
"""

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from loguru import logger


def calculate_hash(
    sequence_id: int,
    previous_hash: str,
    event_type: str,
    payload_str: str,
    timestamp: datetime
) -> str:
    """Compute SHA-256 hash representing a block in the audit trail."""
    # Convert to timezone-naive UTC for identical database representation
    if timestamp.tzinfo is not None:
        ts_naive = timestamp.astimezone(timezone.utc).replace(tzinfo=None)
    else:
        ts_naive = timestamp
    timestamp_str = ts_naive.strftime("%Y-%m-%d %H:%M:%S.%f")
    
    message = f"{sequence_id}:{previous_hash}:{event_type}:{payload_str}:{timestamp_str}"
    return hashlib.sha256(message.encode("utf-8")).hexdigest()


class AuditJournal:
    """
    Append-only, buffered audit log persistence engine.
    Ensures cryptographic tamper detection and asynchronous write safety.
    """

    def __init__(self, batch_size: int = 50, flush_interval: float = 1.0):
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=5000)
        self._batch_size = batch_size
        self._flush_interval = flush_interval
        self._running = False
        self._worker_task: Optional[asyncio.Task] = None
        self._write_lock = asyncio.Lock()

    async def start(self) -> None:
        """Start the background batch writer task."""
        if self._running:
            return
        self._running = True
        self._worker_task = asyncio.create_task(self._worker_loop())
        logger.info("AuditJournal background worker started successfully")

    async def stop(self) -> None:
        """Gracefully stop the worker and flush remaining items."""
        if not self._running:
            return
        self._running = False
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        logger.info("AuditJournal background worker stopped")

    def log_event(
        self,
        event_id: str,
        request_id: Optional[str],
        correlation_id: Optional[str],
        causation_id: Optional[str],
        parent_request_id: Optional[str],
        event_type: str,
        payload: Any,
        actor: str,
        event_version: str = "1.0",
        timestamp: Optional[datetime] = None
    ) -> None:
        """
        Enqueues an event for logging. Non-blocking call.
        """
        item = {
            "event_id": event_id,
            "request_id": request_id,
            "correlation_id": correlation_id,
            "causation_id": causation_id,
            "parent_request_id": parent_request_id,
            "event_type": event_type,
            "payload": payload,
            "actor": actor,
            "event_version": event_version,
            "timestamp": timestamp or datetime.now(timezone.utc)
        }
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            logger.error("AuditJournal queue full! Dropping audit event to prevent blockages.")

    async def _flush_batch(self, batch: List[Dict[str, Any]]) -> None:
        """Flushes a batch of audit logs to SQLite in a single transaction."""
        if not batch:
            return

        from infrastructure.storage.database import AsyncSessionLocal, AuditLog
        from sqlalchemy import select

        if not AsyncSessionLocal:
            logger.warning("AuditJournal: AsyncSessionLocal database not initialized yet.")
            return

        async with self._write_lock:
            async with AsyncSessionLocal() as session:
                try:
                    # Retrieve the latest seq_id and event_hash to preserve the chain
                    result = await session.execute(
                        select(AuditLog).order_by(AuditLog.sequence_id.desc()).limit(1)
                    )
                    last_log = result.scalar_one_or_none()

                    if last_log:
                        current_seq = last_log.sequence_id
                        current_hash = last_log.event_hash
                    else:
                        current_seq = 0
                        current_hash = "0" * 64

                    log_entities = []
                    for item in batch:
                        current_seq += 1
                        prev_hash = current_hash

                        # Extract payload str & timestamp format
                        payload_data = item["payload"]
                        if hasattr(payload_data, "__dict__"):
                            payload_dict = {
                                k: str(v) if isinstance(v, datetime) else v
                                for k, v in payload_data.__dict__.items()
                            }
                        elif isinstance(payload_data, dict):
                            payload_dict = {
                                k: str(v) if isinstance(v, datetime) else v
                                for k, v in payload_data.items()
                            }
                        else:
                            payload_dict = {"data": str(payload_data)}

                        payload_str = json.dumps(payload_dict, default=str)

                        # Cryptographic event hash computation
                        event_hash = calculate_hash(
                            sequence_id=current_seq,
                            previous_hash=prev_hash,
                            event_type=item["event_type"],
                            payload_str=payload_str,
                            timestamp=item["timestamp"]
                        )

                        entity = AuditLog(
                            sequence_id=current_seq,
                            event_id=item["event_id"],
                            request_id=item["request_id"] or "N/A",
                            correlation_id=item["correlation_id"] or "N/A",
                            causation_id=item["causation_id"] or "N/A",
                            parent_request_id=item["parent_request_id"],
                            event_type=item["event_type"],
                            payload=payload_str,
                            actor=item["actor"],
                            event_version=item["event_version"],
                            timestamp=item["timestamp"],
                            event_hash=event_hash,
                            previous_hash=prev_hash
                        )
                        log_entities.append(entity)
                        current_hash = event_hash

                    session.add_all(log_entities)
                    await session.commit()
                except Exception as e:
                    await session.rollback()
                    logger.error(f"AuditJournal: Failed to flush batch: {e}", exc_info=True)

    async def _worker_loop(self) -> None:
        """Background loop consuming logs and flushing them in batches or time interval."""
        batch = []
        last_flush_time = asyncio.get_running_loop().time()

        while self._running:
            try:
                now = asyncio.get_running_loop().time()
                time_since_last_flush = now - last_flush_time
                timeout = max(0.05, self._flush_interval - time_since_last_flush)

                try:
                    item = await asyncio.wait_for(self._queue.get(), timeout=timeout)
                    batch.append(item)
                    self._queue.task_done()
                except asyncio.TimeoutError:
                    pass

                current_time = asyncio.get_running_loop().time()
                if batch and (len(batch) >= self._batch_size or (current_time - last_flush_time) >= self._flush_interval):
                    await self._flush_batch(batch)
                    batch.clear()
                    last_flush_time = current_time
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"AuditJournal background worker error: {e}")
                await asyncio.sleep(1.0)

        # Flush any remaining logs before shutdown
        if batch:
            await self._flush_batch(batch)

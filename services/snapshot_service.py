"""
State Snapshot Service for periodic checkpointing and recovery.
"""

import hashlib
import json
import zlib
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from loguru import logger
from sqlalchemy import select, desc

from infrastructure.storage.database import StateSnapshot, get_session


class SnapshotService:
    """Takes periodic zlib-compressed, SHA-256 hashed state snapshots."""

    def __init__(self, version: str = "1.0"):
        self.version = version

    async def save_snapshot(self, last_sequence_id: int, state_data: Dict[str, Any]) -> str:
        """Serialize, compress, sign, and write system state snapshot to DB."""
        snapshot_id = f"snap_{int(datetime.now(timezone.utc).timestamp())}_{last_sequence_id}"
        logger.info(f"Saving system state snapshot {snapshot_id} (seq: {last_sequence_id})")

        # 1. Deterministic JSON serialization
        serialized = json.dumps(state_data, sort_keys=True)
        # 2. Compute integrity hash of the serialized string
        integrity_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        # 3. Compress using zlib
        compressed_blob = zlib.compress(serialized.encode("utf-8"))

        async with get_session() as session:
            async with session.begin():
                snap = StateSnapshot(
                    snapshot_id=snapshot_id,
                    state_blob=compressed_blob,
                    integrity_hash=integrity_hash,
                    last_sequence_id=last_sequence_id,
                    version=self.version
                )
                session.add(snap)
        logger.info(f"State snapshot {snapshot_id} saved successfully.")
        return snapshot_id

    async def load_latest_snapshot(self) -> Optional[Dict[str, Any]]:
        """Retrieve, verify integrity, decompress, and return the latest state snapshot."""
        logger.info("Loading latest system state snapshot...")
        async with get_session() as session:
            async with session.begin():
                stmt = select(StateSnapshot).order_by(desc(StateSnapshot.last_sequence_id)).limit(1)
                result = await session.execute(stmt)
                snap = result.scalars().first()
                if not snap:
                    logger.info("No state snapshots found.")
                    return None

                # 1. Decompress
                decompressed = zlib.decompress(snap.state_blob).decode("utf-8")
                # 2. Verify integrity signature
                current_hash = hashlib.sha256(decompressed.encode("utf-8")).hexdigest()
                if current_hash != snap.integrity_hash:
                    logger.error(
                        f"CRITICAL: Snapshot integrity mismatch for {snap.snapshot_id}! "
                        f"Expected: {snap.integrity_hash}, Got: {current_hash}"
                    )
                    raise ValueError(f"Snapshot corruption detected for {snap.snapshot_id}")

                # 3. Deserialize
                state_data = json.loads(decompressed)
                state_data["_last_sequence_id"] = snap.last_sequence_id
                state_data["_snapshot_id"] = snap.snapshot_id
                logger.info(f"Loaded snapshot {snap.snapshot_id} at sequence {snap.last_sequence_id}.")
                return state_data
        return None
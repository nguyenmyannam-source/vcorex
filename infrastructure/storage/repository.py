"""
Repository pattern implementation for database access abstraction.
Provides a clean API for database operations while hiding SQLAlchemy specifics.
"""

from datetime import datetime, timezone
from typing import Any, Dict, Generic, List, Optional, Type, TypeVar

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .database import BaseModel, Position, Signal, SystemState, Trade, AuditLog

T = TypeVar("T", bound=BaseModel)


class Repository(Generic[T]):
    """Generic repository base class for database operations."""

    def __init__(self, model: Type[T], session: AsyncSession):
        self.model = model
        self.session = session

    async def get_by_id(self, id: str) -> Optional[T]:
        """Get record by ID."""
        result = await self.session.execute(select(self.model).where(self.model.id == id))
        return result.scalar_one_or_none()

    async def list_all(self, limit: int = 100, offset: int = 0) -> List[T]:
        """List all records with pagination."""
        result = await self.session.execute(
            select(self.model).order_by(self.model.created_at.desc()).limit(limit).offset(offset)
        )
        return list(result.scalars().all())

    async def add(self, entity: T) -> T:
        """Add a new record."""
        self.session.add(entity)
        return entity

    async def add_all(self, entities: List[T]) -> None:
        """Add multiple records in one transaction."""
        self.session.add_all(entities)

    async def update(self, id: str, data: Dict[str, Any]) -> Optional[T]:
        """Update a record by ID."""
        await self.session.execute(update(self.model).where(self.model.id == id).values(**data))
        return await self.get_by_id(id)

    async def delete(self, id: str) -> bool:
        """Delete a record by ID."""
        await self.session.execute(delete(self.model).where(self.model.id == id))
        return True

    async def count(self) -> int:
        """Get total record count."""
        result = await self.session.execute(select(self.model.id))
        return len(list(result.scalars().all()))


class SignalRepository(Repository[Signal]):
    """Repository for signal operations."""

    def __init__(self, session: AsyncSession):
        super().__init__(Signal, session)


class PositionRepository(Repository[Position]):
    """Repository for position operations."""

    def __init__(self, session: AsyncSession):
        super().__init__(Position, session)

    async def get_by_position_id(self, position_id: str) -> Optional[Position]:
        """Get position by exchange position ID."""
        result = await self.session.execute(
            select(Position).where(Position.position_id == position_id)
        )
        return result.scalar_one_or_none()

    async def close_position(self, position_id: str, realized_pnl: float) -> Optional[Position]:
        """Mark a position as closed with realized P&L."""
        await self.session.execute(
            update(Position)
            .where(Position.position_id == position_id)
            .values(
                status="CLOSED", realized_pnl=realized_pnl, closed_at=datetime.now(timezone.utc)
            )
        )
        return await self.get_by_position_id(position_id)

    async def update_pnl_and_fee(self, position_id: str, realized_pnl: float, fee_paid: float) -> Optional[Position]:
        """Update realized P&L and accumulated fee paid for a position."""
        await self.session.execute(
            update(Position)
            .where(Position.position_id == position_id)
            .values(realized_pnl=realized_pnl, fee_paid=fee_paid)
        )
        return await self.get_by_position_id(position_id)

    async def upsert(self, entity_id: str, data: Dict[str, Any]) -> Optional[Position]:
        """Upsert (update or insert) a position by its position_id or internal id."""
        from sqlalchemy import select

        position_id = data.get("position_id")
        existing = None

        if position_id:
            result = await self.session.execute(
                select(Position).where(Position.position_id == position_id)
            )
            existing = result.scalar_one_or_none()

        if not existing:
            result = await self.session.execute(select(Position).where(Position.id == entity_id))
            existing = result.scalar_one_or_none()

        if existing:
            await self.session.execute(
                update(Position).where(Position.id == existing.id).values(**data)
            )
            return await self.get_by_id(existing.id)

        position = Position(id=entity_id, **data)
        await self.add(position)
        return position

    async def find(self, filters: Dict[str, Any]) -> List[Position]:
        """Find positions matching the given filters."""
        query = select(Position)

        # Xử lý các filter cơ bản, ví dụ: {"status": {"$nin": ["closed", ...]}}
        if "status" in filters:
            status_filter = filters["status"]
            if "$nin" in status_filter:
                query = query.where(Position.status.notin_(status_filter["$nin"]))
            else:
                query = query.where(Position.status == status_filter)

        result = await self.session.execute(query)
        return list(result.scalars().all())


class TradeRepository(Repository[Trade]):
    """Repository for trade operations."""

    def __init__(self, session: AsyncSession):
        super().__init__(Trade, session)


class SystemStateRepository(Repository[SystemState]):
    """Repository for system state persistence."""

    def __init__(self, session: AsyncSession):
        super().__init__(SystemState, session)

    async def get_by_key(self, key: str) -> Optional[SystemState]:
        """Get state record by key."""
        result = await self.session.execute(select(SystemState).where(SystemState.key == key))
        return result.scalar_one_or_none()


class AuditLogRepository(Repository[AuditLog]):
    """Repository for audit log operations."""

    def __init__(self, session: AsyncSession):
        super().__init__(AuditLog, session)

    async def get_latest(self) -> Optional[AuditLog]:
        """Get the latest audit log entry by sequence_id."""
        result = await self.session.execute(
            select(AuditLog).order_by(AuditLog.sequence_id.desc()).limit(1)
        )
        return result.scalar_one_or_none()

    async def list_ordered(self, limit: int = 100, offset: int = 0) -> List[AuditLog]:
        """List audit logs ordered by sequence_id."""
        result = await self.session.execute(
            select(AuditLog).order_by(AuditLog.sequence_id.asc()).limit(limit).offset(offset)
        )
        return list(result.scalars().all())


class UnitOfWork:
    """
    Unit of Work pattern implementation to manage database transactions.
    Provides access to all repositories and ensures atomic operations.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self.session_factory = session_factory
        self.session: Optional[AsyncSession] = None
        self.signals: Optional[SignalRepository] = None
        self.positions: Optional[PositionRepository] = None
        self.trades: Optional[TradeRepository] = None
        self.system_state: Optional[SystemStateRepository] = None
        self.audit_logs: Optional[AuditLogRepository] = None
        self._committed = False

    async def __aenter__(self) -> "UnitOfWork":
        self.session = self.session_factory()
        self.signals = SignalRepository(self.session)
        self.positions = PositionRepository(self.session)
        self.trades = TradeRepository(self.session)
        self.system_state = SystemStateRepository(self.session)
        self.audit_logs = AuditLogRepository(self.session)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        if exc_type is not None:
            await self.rollback()
        elif not self._committed:
            await self.commit()
        if self.session:
            await self.session.close()

    async def commit(self) -> None:
        """Commit the transaction."""
        if self.session:
            await self.session.commit()
            self._committed = True

    async def rollback(self) -> None:
        """Rollback the transaction."""
        if self.session:
            await self.session.rollback()

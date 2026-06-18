"""
Database configuration and base models using SQLAlchemy 2.0.
Implements repository pattern and unit of work for data access.
"""

import asyncio
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from typing import AsyncGenerator, Generator, Optional

from loguru import logger
from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, String, Text, text, LargeBinary, create_engine
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker, Session
from sqlalchemy.pool import StaticPool

from core.config.settings import Settings
from infrastructure.storage.database_adapter import create_database_adapter, DatabaseAdapter

# Global engine and base, initialized in init_database
engine = None
active_adapter: Optional[DatabaseAdapter] = None



class Base(DeclarativeBase):
    """Base class for all SQLAlchemy models."""

    pass


SessionLocal: Optional[sessionmaker[Session]] = None
async_engine: Optional[AsyncEngine] = None
AsyncSessionLocal: Optional[async_sessionmaker[AsyncSession]] = None


# Database Models
from uuid import uuid4


class BaseModel:
    """Base model with common fields."""

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )


class Trade(Base, BaseModel):
    """Trade history model."""

    __tablename__ = "trades"

    order_id: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    client_order_id: Mapped[Optional[str]] = mapped_column(String(100))
    symbol: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    side: Mapped[str] = mapped_column(String(10), nullable=False)  # buy/sell
    order_type: Mapped[str] = mapped_column(String(20), nullable=False)  # market/limit
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    filled_amount: Mapped[float] = mapped_column(Float, default=0.0)
    price: Mapped[Optional[float]] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    strategy_name: Mapped[Optional[str]] = mapped_column(String(100))
    signal_id: Mapped[Optional[str]] = mapped_column(String(100), ForeignKey("signals.id"))

    # Relationships
    signal = relationship("Signal", back_populates="trades")


class Position(Base, BaseModel):
    """Open/closed position model."""

    __tablename__ = "positions"

    position_id: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    side: Mapped[str] = mapped_column(String(10), nullable=False)  # long/short
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    amount_remaining: Mapped[float] = mapped_column(Float, nullable=False)
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    current_price: Mapped[float] = mapped_column(Float, nullable=False)
    unrealized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    realized_pnl: Mapped[Optional[float]] = mapped_column(Float)
    fee_paid: Mapped[float] = mapped_column(Float, default=0.0)
    leverage: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, index=True
    )  # OPENED/PARTIAL_TP/CLOSED/LIQUIDATED
    stop_loss_price: Mapped[Optional[float]] = mapped_column(Float)
    take_profit_prices: Mapped[Optional[str]] = mapped_column(Text)  # JSON serialized list
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    strategy_name: Mapped[Optional[str]] = mapped_column(String(100))
    algo_order_ids: Mapped[Optional[str]] = mapped_column(Text)
    exchange_id: Mapped[Optional[str]] = mapped_column(String(100))
    ct_val: Mapped[Optional[float]] = mapped_column(Float, default=1.0)
    signal_id: Mapped[Optional[str]] = mapped_column(String(100))
    timeframe: Mapped[Optional[str]] = mapped_column(String(20))


class Signal(Base, BaseModel):
    """Generated signals model."""

    __tablename__ = "signals"

    signal_id: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    strategy_name: Mapped[str] = mapped_column(String(100), nullable=False)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    timeframe: Mapped[str] = mapped_column(String(10), nullable=False)
    signal_type: Mapped[str] = mapped_column(String(10), nullable=False)
    signal_strength: Mapped[str] = mapped_column(String(20), nullable=False)
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    stop_loss_price: Mapped[Optional[float]] = mapped_column(Float)
    take_profit_prices: Mapped[Optional[str]] = mapped_column(Text)  # JSON serialized
    position_size_usdt: Mapped[float] = mapped_column(Float, nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(Text)
    indicators: Mapped[Optional[str]] = mapped_column(Text)  # JSON serialized
    validated: Mapped[bool] = mapped_column(Boolean, default=False)
    risk_approved: Mapped[bool] = mapped_column(Boolean, default=False)
    executed: Mapped[bool] = mapped_column(Boolean, default=False)
    rejected_reason: Mapped[Optional[str]] = mapped_column(Text)

    # Relationships
    trades = relationship("Trade", back_populates="signal")


class SystemState(Base, BaseModel):
    """System state persistence model."""

    __tablename__ = "system_state"

    key: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)  # JSON serialized
    component: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    last_updated: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )


class AuditLog(Base, BaseModel):
    """Immutable audit trail of all system events with tamper detection chain."""

    __tablename__ = "audit_logs"

    sequence_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False, index=True)
    event_id: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    request_id: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    causation_id: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    parent_request_id: Mapped[Optional[str]] = mapped_column(String(100), index=True)
    event_type: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)  # JSON-serialized
    actor: Mapped[str] = mapped_column(String(100), nullable=False)
    event_version: Mapped[str] = mapped_column(String(20), default="1.0", nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    event_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    previous_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class DeadLetterEvent(Base, BaseModel):
    """Failed events stored for quarantine, analysis, and replay."""

    __tablename__ = "dead_letter_events"

    event_id: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    error: Mapped[str] = mapped_column(Text, nullable=False)
    retry_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    quarantined: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class StateSnapshot(Base, BaseModel):
    """System state snapshot for quick recovery and replay optimization."""

    __tablename__ = "state_snapshots"

    snapshot_id: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    state_blob: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    integrity_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    last_sequence_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    version: Mapped[str] = mapped_column(String(20), default="1.0", nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)




# Session management
@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Get a new database session for async operations."""
    if AsyncSessionLocal is not None:
        async with AsyncSessionLocal() as session:
            yield session
    elif active_adapter:
        session = await asyncio.to_thread(active_adapter.session_factory)
        try:
            yield session
        finally:
            await asyncio.to_thread(session.close)
    else:
        raise RuntimeError("Database session factory not initialized")


def init_database(settings: Settings) -> None:
    """Initialize database and create all tables."""
    global engine, SessionLocal, active_adapter, async_engine, AsyncSessionLocal
    logger.info("Initializing database...")
    try:
        # Create database engines for sync and async access.
        # For SQLite in-memory URLs, use a shared cache URI so both sync and async engines see the same database.
        sync_db_url = settings.database_url
        async_db_url = settings.database_url
        db_connect_args = {}
        db_poolclass = None
        uri_flag = False

        if "sqlite" in settings.database_url:
            db_connect_args = {"check_same_thread": False}
            db_poolclass = StaticPool

            if settings.database_url.startswith("sqlite:///:memory:") or settings.database_url.startswith("sqlite+aiosqlite:///:memory:"):
                sync_db_url = "sqlite:///:memory:?cache=shared"
                async_db_url = "sqlite+aiosqlite:///:memory:?cache=shared"
                db_connect_args["uri"] = True
            elif async_db_url.startswith("sqlite://") and not async_db_url.startswith("sqlite+aiosqlite://"):
                async_db_url = async_db_url.replace("sqlite://", "sqlite+aiosqlite://", 1)

        elif async_db_url.startswith("postgresql://") and not async_db_url.startswith("postgresql+asyncpg://"):
            async_db_url = async_db_url.replace("postgresql://", "postgresql+asyncpg://", 1)

        engine = create_engine(
            sync_db_url,
            connect_args=db_connect_args,
            poolclass=db_poolclass,
            future=True,
        )
        SessionLocal = sessionmaker(
            bind=engine, expire_on_commit=False
        )

        async_engine = create_async_engine(
            async_db_url,
            connect_args=db_connect_args,
            poolclass=db_poolclass,
            future=True,
        )
        AsyncSessionLocal = async_sessionmaker(async_engine, expire_on_commit=False, class_=AsyncSession)

        # Apply SQLite safety pragmas to both sync and async connections
        if "sqlite" in settings.database_url:
            from sqlalchemy import event
            
            def set_sqlite_pragma(dbapi_connection, connection_record):
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=NORMAL")
                cursor.execute("PRAGMA busy_timeout=5000")
                cursor.close()
                
            event.listen(engine, "connect", set_sqlite_pragma)
            event.listen(async_engine.sync_engine, "connect", set_sqlite_pragma)
            logger.info("SQLite safety pragmas applied (WAL mode, synchronous=NORMAL, busy_timeout=5000ms)")

        # Now create the adapter with the correct session factory
        active_adapter = create_database_adapter(SessionLocal)
        active_adapter.initialize()

        # Create all tables on the sync engine; async sessions share the SQLite in-memory database via shared-cache URI.
        Base.metadata.create_all(bind=engine)

        # HEAL DATABASE SCHEMAS FOR SQLITE
        if "sqlite" in settings.database_url:
            def add_column_if_missing(table_name: str, col_name: str, col_type: str, default_val: str = None):
                with engine.connect() as conn:
                    result = conn.execute(text(f"PRAGMA table_info({table_name});"))
                    columns = [row[1] for row in result.fetchall()]
                    if col_name not in columns:
                        alter_query = f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_type}"
                        if default_val is not None:
                            alter_query += f" DEFAULT {default_val}"
                        conn.execute(text(alter_query))
                        conn.commit()
                        logger.info(f"Self-healed schema: Added column {col_name} ({col_type}) to table {table_name}")

            add_column_if_missing("positions", "updated_at", "DATETIME", "CURRENT_TIMESTAMP")
            add_column_if_missing("positions", "fee_paid", "FLOAT", "0.0")
            add_column_if_missing("positions", "strategy_name", "TEXT", "NULL")
            add_column_if_missing("positions", "algo_order_ids", "TEXT", "NULL")
            add_column_if_missing("positions", "exchange_id", "TEXT", "NULL")
            add_column_if_missing("positions", "ct_val", "FLOAT", "1.0")
            add_column_if_missing("positions", "signal_id", "TEXT", "NULL")
            add_column_if_missing("positions", "timeframe", "TEXT", "NULL")
            add_column_if_missing("trades", "updated_at", "DATETIME", "CURRENT_TIMESTAMP")
            add_column_if_missing("trades", "filled_amount", "FLOAT", "0.0")
            add_column_if_missing("signals", "updated_at", "DATETIME", "CURRENT_TIMESTAMP")
            add_column_if_missing("signals", "executed", "BOOLEAN", "0")
            add_column_if_missing("signals", "rejected_reason", "TEXT", "NULL")
            add_column_if_missing("system_state", "updated_at", "DATETIME", "CURRENT_TIMESTAMP")
            add_column_if_missing("system_state", "last_updated", "DATETIME", "CURRENT_TIMESTAMP")

        logger.info("Database initialization complete, all tables created")
    except Exception as e:
        logger.error("Failed to initialize database: {}", e, exc_info=True)
        raise


async def async_init_database(settings: Settings) -> None:
    """Async wrapper for database initialization."""
    return init_database(settings)


async def async_close_database() -> None:
    """Async wrapper for closing database connections."""
    global active_adapter, engine, async_engine
    if active_adapter:
        active_adapter.close()
    if engine:
        engine.dispose()
    if async_engine:
        await async_engine.dispose()
    logger.info("Async database connections closed")


def close_database() -> None:
    """Close database connections."""
    global active_adapter, engine, async_engine
    if active_adapter:
        active_adapter.close()
    if engine:
        engine.dispose()
    if async_engine:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(async_engine.dispose())
        else:
            if loop.is_running():
                loop.create_task(async_engine.dispose())
            else:
                loop.run_until_complete(async_engine.dispose())
    logger.info("Database connections closed")

"""
Dependency injection container for managing application dependencies.
Provides centralized access to all core services with proper lifecycle management.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import inspect
from typing import Any, Coroutine, Dict, Optional, Set, Type, TypeVar

from loguru import logger

from core.exceptions import DependencyCreationError, DependencyResolutionError

T = TypeVar("T")


@dataclass
class ServiceMetadata:
    """Metadata for registered services."""

    instance: Any
    singleton: bool
    dependencies: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    initialized: bool = False


class DependencyContainer:
    """
    Central dependency injection container that manages service lifecycle.
    Supports singleton and transient services with dependency resolution.
    """

    def __init__(self):
        self._services: Dict[str, ServiceMetadata] = {}
        self._instances: Dict[str, Any] = {}
        logger.info("DependencyContainer initialized")

    def register(
        self,
        name: str,
        service_type: Type[T],
        singleton: bool = True,
        dependencies: Optional[list[str]] = None,
    ) -> None:
        """
        Register a service type with the container.
        """
        if name in self._services:
            logger.warning(f"Overwriting existing service registration: {name}")

        self._services[name] = ServiceMetadata(
            instance=service_type, singleton=singleton, dependencies=dependencies or []
        )
        logger.debug(f"Registered service: {name} (singleton={singleton})")

    def register_instance(self, name: str, instance: Any, singleton: bool = True) -> None:
        """
        Register an already created instance with the container.
        """
        # Store the actual instance in ServiceMetadata to avoid re-instantiation
        self._services[name] = ServiceMetadata(
            instance=instance, singleton=singleton, dependencies=[], created_at=datetime.now(timezone.utc), initialized=True
        )
        self._instances[name] = instance
        logger.debug(f"Registered instance: {name}")

    async def resolve(self, name: str) -> Any:
        """
        Resolve and return a service instance, creating it if necessary.
        Handles dependency injection for constructor parameters.
        """
        if name not in self._services:
            logger.error(f"Service not registered: {name}")
            raise ValueError(f"Service not registered: {name}")

        service_meta = self._services[name]

        # Return existing instance if it's a singleton
        if service_meta.singleton and name in self._instances:
            return self._instances[name]

        # Resolve dependencies first
        resolved_deps = {}
        for dep_name in service_meta.dependencies:
            if dep_name not in self._services:
                logger.error(f"Missing dependency registration: {dep_name}")
                raise DependencyResolutionError(
                    f"Missing dependency registration: {dep_name}",
                    context={"service_name": name, "missing_dependency": dep_name},
                )
            resolved_deps[dep_name] = await self.resolve(dep_name)

        # Create new instance
        try:
            if isinstance(service_meta.instance, type):
                instance = service_meta.instance(**resolved_deps)
            else:
                instance = service_meta.instance
        except Exception as error:
            logger.error(
                f"Failed to instantiate service {name}: {error}",
                exc_info=True,
            )
            raise DependencyCreationError(
                f"Failed to instantiate service {name}: {error}",
                context={"service_name": name, "dependencies": list(resolved_deps.keys())},
            ) from error

        try:
            if hasattr(instance, "initialize") and callable(instance.initialize):
                if inspect.iscoroutinefunction(instance.initialize):
                    await instance.initialize()
                else:
                    instance.initialize()
        except Exception as error:
            logger.error(f"Initialization failed for service {name}: {error}", exc_info=True)
            raise DependencyCreationError(
                f"Initialization failed for service {name}: {error}",
                context={"service_name": name},
            ) from error

        # Cache singleton instance
        if service_meta.singleton:
            self._instances[name] = instance
            service_meta.initialized = True

        logger.debug(f"Resolved service: {name}")
        return instance

    async def initialize_all(self) -> None:
        """
        Initialize all singleton services that have an initialize method.
        """
        initialization_tasks = []

        for name, service_meta in self._services.items():
            if service_meta.singleton and not service_meta.initialized:
                if name not in self._instances:
                    task = asyncio.create_task(self.resolve(name))
                    initialization_tasks.append(task)

        if initialization_tasks:
            await asyncio.gather(*initialization_tasks, return_exceptions=True)

        logger.info("All services initialized")

    async def shutdown_all(self) -> None:
        """
        Gracefully shutdown all services that have a shutdown method.
        """
        shutdown_tasks = []

        for name, instance in self._instances.items():
            if hasattr(instance, "shutdown") and callable(instance.shutdown):
                shutdown_func = instance.shutdown
                if inspect.iscoroutinefunction(shutdown_func):
                    task = asyncio.create_task(shutdown_func())
                    shutdown_tasks.append(task)
                else:
                    try:
                        shutdown_func()
                    except Exception as e:
                        logger.error(f"Error shutting down {name}: {str(e)}")

        if shutdown_tasks:
            await asyncio.gather(*shutdown_tasks, return_exceptions=True)

        logger.info("All services shutdown")
        self._instances.clear()

    def get(self, name: str) -> Optional[Any]:
        """
        Get an existing singleton instance without creating it.
        """
        return self._instances.get(name)

    def has(self, name: str) -> bool:
        """
        Check if a service is registered.
        """
        return name in self._services

    def get_instance_names(self) -> list[str]:
        """
        Get list of all active singleton instances.
        """
        return list(self._instances.keys())

    def clear(self) -> None:
        """
        Clear all registered services and instances.
        """
        self._services.clear()
        self._instances.clear()
        logger.info("Dependency container cleared")


def _on_task_done(task: asyncio.Task, tracker: Optional[Set[asyncio.Task]] = None) -> None:
    """Callback to log exceptions from fire-and-forget tasks and clean up."""
    try:
        exc = task.exception()
        if exc:
            logger.error(f"Safe task failed with exception: {exc}", exc_info=exc)
    except asyncio.CancelledError:
        pass
    finally:
        if tracker is not None:
            tracker.discard(task)


def run_safe_task(
    coro: Coroutine[Any, Any, Any], tracker: Optional[Set[asyncio.Task]] = None
) -> asyncio.Task:
    """
    Utility to run an async coroutine as a background task safely.
    Catches and logs any unhandled exceptions to prevent silent failures.
    If tracker set is provided, task is added to it and automatically removed when done.
    """
    task = asyncio.create_task(coro)
    if tracker is not None:
        tracker.add(task)
    task.add_done_callback(lambda t: _on_task_done(t, tracker))
    return task


# Global container instance
container = DependencyContainer()
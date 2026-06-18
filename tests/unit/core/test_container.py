import pytest

from core.container import DependencyContainer


class ServiceA:
    def __init__(self) -> None:
        self.initialized = False

    async def initialize(self) -> None:
        self.initialized = True


class ServiceB:
    def __init__(self, service_a: ServiceA) -> None:
        self.service_a = service_a


class ServiceC:
    def __init__(self) -> None:
        self.shutdown_called = False

    async def initialize(self) -> None:
        pass

    async def shutdown(self) -> None:
        self.shutdown_called = True


@pytest.mark.asyncio
async def test_register_and_resolve_singleton_instance() -> None:
    container = DependencyContainer()
    service_a = ServiceA()
    container.register_instance("service_a", service_a)

    resolved = await container.resolve("service_a")
    assert resolved is service_a
    assert resolved.initialized is False


@pytest.mark.asyncio
async def test_resolve_class_with_dependencies() -> None:
    container = DependencyContainer()
    container.register("service_a", ServiceA)
    container.register("service_b", ServiceB, dependencies=["service_a"])

    resolved_b = await container.resolve("service_b")
    assert isinstance(resolved_b, ServiceB)
    assert isinstance(resolved_b.service_a, ServiceA)
    assert resolved_b.service_a.initialized is True


@pytest.mark.asyncio
async def test_initialize_all_and_shutdown_all() -> None:
    container = DependencyContainer()
    container.register("service_c", ServiceC)

    await container.initialize_all()
    assert "service_c" in container.get_instance_names()

    service_c = container.get("service_c")
    assert service_c is not None
    assert isinstance(service_c, ServiceC)

    await container.shutdown_all()
    assert service_c.shutdown_called is True
    assert container.get("service_c") is None


@pytest.mark.asyncio
async def test_resolve_missing_service_raises() -> None:
    container = DependencyContainer()
    with pytest.raises(ValueError):
        await container.resolve("unknown_service")

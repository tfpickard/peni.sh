import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "integration: runs the real worker entrypoint as a subprocess"
    )

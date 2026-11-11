"""Integration tests that spin up the Docker stack and hit real endpoints.

These tests require Docker to be available and are skipped automatically
when Docker is not present.  They use the ``docker`` Python SDK to bring
up ``docker-compose.yml``, wait for healthchecks to pass, then exercise
the HTTP endpoints against the running containers.
"""

from __future__ import annotations

import os
import time

import pytest

try:
    import docker
    from docker.errors import DockerException
except ImportError:  # pragma: no cover - docker SDK not installed
    docker = None  # type: ignore[assignment]
    DockerException = Exception  # type: ignore[assignment, misc]

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMPOSE_FILE = os.path.join(REPO_ROOT, "docker-compose.yml")


def _docker_available() -> bool:
    if docker is None:
        return False
    try:
        client = docker.from_env()
        client.ping()
        return True
    except (DockerException, Exception):
        return False


docker_available = pytest.mark.skipif(
    not _docker_available(),
    reason="Docker is not available — integration tests require a running Docker daemon",
)


@docker_available
class TestDockerStack:
    """Spin up docker-compose and verify real HTTP endpoints."""

    @pytest.fixture(scope="class")
    def stack(self):
        """Bring up docker-compose, wait for health, yield client, then tear down."""
        import docker

        client = docker.from_env()

        # Pull/build and start services
        os.system(f"docker compose -f {COMPOSE_FILE} up -d --build ghost-shell alchemist")

        # Wait for ghost-shell to become healthy
        ghost_shell = None
        deadline = time.time() + 120
        while time.time() < deadline:
            containers = client.containers.list(all=True, filters={"name": "ghost-shell"})
            if containers:
                c = containers[0]
                c.reload()
                if c.attrs["State"]["Health"]["Status"] == "healthy":
                    ghost_shell = c
                    break
            time.sleep(2)

        if ghost_shell is None:
            pytest.fail("ghost-shell did not become healthy within 120s")

        # Get the mapped host port
        ghost_shell.reload()
        ports = ghost_shell.attrs["NetworkSettings"]["Ports"]
        host_port = None
        for container_port, bindings in ports.items():
            if container_port == "5000/tcp" and bindings:
                host_port = bindings[0]["HostPort"]
                break

        base_url = f"http://localhost:{host_port}"

        yield base_url

        # Teardown
        os.system(f"docker compose -f {COMPOSE_FILE} down")
        client.close()

    def test_health_endpoint_returns_ok(self, stack):
        import urllib.request

        with urllib.request.urlopen(f"{stack}/health", timeout=10) as resp:
            assert resp.status == 200
            import json

            data = json.loads(resp.read())
            assert data["status"] == "ok"

    def test_status_endpoint_requires_auth(self, stack):
        import urllib.request
        import urllib.error

        # No token → 401
        req = urllib.request.Request(f"{stack}/status")
        try:
            urllib.request.urlopen(req, timeout=10)
            pytest.fail("Expected 401 for unauthenticated request")
        except urllib.error.HTTPError as exc:
            assert exc.code == 401

    def test_status_endpoint_accepts_header_token(self, stack):
        import urllib.request

        token = os.getenv("GHOST_AUTH_TOKEN", "test-token")
        req = urllib.request.Request(
            f"{stack}/status",
            headers={"X-Ghost-Token": token},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            assert resp.status == 200
            import json

            data = json.loads(resp.read())
            assert data["service"] == "ghost-server"

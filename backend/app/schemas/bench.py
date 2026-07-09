"""Request/response models for the bench inventory API (session 1.6)."""

from datetime import datetime

from pydantic import BaseModel, Field

from app.models.bench import Bench


class BenchPorts(BaseModel):
    """The port map parsed from sites/common_site_config.json."""

    webserver_port: int | None = None
    socketio_port: int | None = None
    redis_cache_port: int | None = None
    redis_queue_port: int | None = None
    redis_socketio_port: int | None = None
    file_watcher_port: int | None = None


class BenchOut(BaseModel):
    id: int
    server_id: int
    name: str
    path: str
    frappe_version: str | None
    python_version: str | None
    node_version: str | None
    ports: BenchPorts
    is_production: bool
    status: str
    discovered_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, bench: Bench) -> "BenchOut":
        return cls(
            id=bench.id,
            server_id=bench.server_id,
            name=bench.name,
            path=bench.path,
            frappe_version=bench.frappe_version,
            python_version=bench.python_version,
            node_version=bench.node_version,
            ports=BenchPorts(
                webserver_port=bench.webserver_port,
                socketio_port=bench.socketio_port,
                redis_cache_port=bench.redis_cache_port,
                redis_queue_port=bench.redis_queue_port,
                redis_socketio_port=bench.redis_socketio_port,
                file_watcher_port=bench.file_watcher_port,
            ),
            is_production=bench.is_production,
            status=bench.status,
            discovered_at=bench.discovered_at,
            created_at=bench.created_at,
            updated_at=bench.updated_at,
        )


class DiscoverRequest(BaseModel):
    """Optional overrides for a discover-benches run. Empty base_paths falls back
    to the server defaults plus the SSH user's home."""

    base_paths: list[str] = Field(default_factory=list)
    priority: str = "default"

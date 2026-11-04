from __future__ import annotations

import time
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def new_id() -> str:
    return uuid.uuid4().hex


class Entity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(default_factory=new_id)
    revision: int = 0
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)


class Budget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_requests: int | None = Field(default=None, ge=0)
    max_tokens: int | None = Field(default=None, ge=0)
    max_seconds: float | None = Field(default=None, ge=0)
    max_retries: int = Field(default=2, ge=0, le=20)
    max_sessions: int = Field(default=1, ge=1, le=100)
    max_agents: int = Field(default=0, ge=0, le=32)


class Task(Entity):
    title: str = Field(min_length=1, max_length=500)
    objective: str = ""
    parent_id: str | None = None
    dependencies: list[str] = Field(default_factory=list)
    priority: int = Field(default=0, ge=-100, le=100)
    status: Literal["planned", "ready", "running", "paused", "waiting_resource", "blocked", "complete", "failed"] = "planned"
    host_id: str | None = None
    cwd: str | None = None
    allowed_roots: list[str] = Field(default_factory=list)
    allowed_models: list[str] = Field(default_factory=list)
    acceptance: str = ""
    budget: Budget = Field(default_factory=Budget)
    requests_used: int = 0
    tokens_used: int = 0
    tokens_reserved: int = 0
    started_at: float | None = None
    retries_used: int = 0
    verification: dict[str, Any] | None = None
    verification_after_seq: int = 0
    blocked_reason: str | None = None
    next_step: str = ""


class Host(Entity):
    name: str
    address: str
    username: str | None = None
    port: int = Field(default=22, ge=1, le=65535)
    transport: Literal["auto", "stdio", "tunnel"] = "auto"
    jump: str | None = None
    identity_file: str | None = None
    known_hosts_file: str | None = None
    codex_command: list[str] = Field(default_factory=lambda: ["codex"])
    codex_home: str | None = None
    control_socket: str = ""
    machine_id: str | None = None
    discovered: bool = False
    auto_discovered_socket: bool = False
    inference_url: str | None = None
    gateway_remote_port: int | None = Field(default=None, ge=1024, le=65535)
    gateway_local_port: int | None = Field(default=None, ge=1, le=65535, exclude=True)
    allowed_roots: list[str] = Field(default_factory=list)
    architecture: str | None = None
    enabled: bool = True
    connection: Literal["disconnected", "connecting", "connected", "error"] = "disconnected"
    last_error: str | None = None
    capabilities: dict[str, Any] = Field(default_factory=dict)

    @field_validator("address", "username", "jump")
    @classmethod
    def safe_ssh_value(cls, value: str | None) -> str | None:
        if value is not None and (not value or value.startswith("-") or any(c.isspace() for c in value)):
            raise ValueError("SSH 目标参数不能以 - 开头或包含空白")
        return value

    @field_validator("codex_command")
    @classmethod
    def nonempty_command(cls, value: list[str]) -> list[str]:
        if not value or not value[0]:
            raise ValueError("Codex 命令不能为空")
        return value


class Backend(Entity):
    name: str
    kind: Literal["mixed", "pd", "unknown"] = "unknown"
    opaque: bool = False
    health: Literal["unknown", "available", "unavailable"] = "unknown"
    max_sessions: int = Field(default=1, ge=0)
    max_requests: int = Field(default=1, ge=0)
    metrics_url: str | None = None
    metrics_token_env: str | None = None
    metrics: dict[str, float] = Field(default_factory=dict)
    observed_at: float | None = None
    last_error: str | None = None


class ModelRoute(Entity):
    name: str
    backend_ids: list[str] = Field(default_factory=list)
    purpose: Literal["execution", "recovery"] = "execution"
    opaque: bool = False
    enabled: bool = True
    can_pin_backend: bool = False
    upstream_model: str | None = None


class StallPolicy(BaseModel):
    enabled: bool = False
    suspect_after: float = Field(default=900, gt=0)
    confirm_after: float = Field(default=1800, gt=0)
    cooldown: float = Field(default=1800, ge=0)


class Session(Entity):
    host_id: str
    thread_id: str
    task_id: str | None = None
    parent_thread_id: str | None = None
    name: str = ""
    role: Literal["execution", "supervisor"] = "execution"
    agent_nickname: str | None = None
    agent_role: str | None = None
    binding_source: Literal["discovered", "manual", "inherited", "managed"] = "discovered"
    cwd: str = ""
    model: str = ""
    model_provider: str | None = None
    backend_id: str | None = None
    status: str = "unknown"
    connected: bool = False
    control_enabled: bool = False
    active_turn_id: str | None = None
    compaction_turn_id: str | None = None
    last_compaction_at: float | None = None
    goal: dict[str, Any] | None = None
    last_activity_at: float = Field(default_factory=time.time)
    last_output_at: float | None = None
    last_progress_at: float = Field(default_factory=time.time)
    progress_revision: int = 0
    control_revision: int = 0
    waiting_reason: str | None = None
    tool_running: bool = False
    expected_tool_until: float | None = None
    diagnosis: dict[str, Any] | None = None
    stall_policy: StallPolicy = Field(default_factory=StallPolicy)
    last_recovery_at: float | None = None
    last_error: str | None = None
    budget_accounting: Literal["native_estimate", "gateway"] = "native_estimate"
    budget_ready: bool = False
    last_token_total: int | None = None
    last_external_model: str | None = None
    last_external_model_at: float | None = None
    settings_observed_at: float = 0
    status_observed_at: float = 0
    goal_observed_at: float = 0


class Recovery(Entity):
    session_id: str
    task_id: str
    original_model: str
    target_model: str
    compression_model: str = "ZL-DeepSeek-v4.1"
    state: str = "planned"
    expected_progress_revision: int
    expected_control_revision: int
    attempts: int = 0
    compaction_turn_id: str | None = None
    allocation_id: str | None = None
    baseline_turn_ids: list[str] | None = None
    baseline_completed_item_ids: list[str] | None = None
    goal_objective: str | None = None
    scope_fingerprint: str = ""
    continuation_turn_id: str | None = None
    error: str | None = None
    steps: list[dict[str, Any]] = Field(default_factory=list)


def public_recovery(record: dict[str, Any]) -> dict[str, Any]:
    """原始回合基线留在协调者数据库，不占用看板和管理 agent 的上下文。"""
    view = {key: value for key, value in record.items() if key not in {"baseline_turn_ids", "baseline_completed_item_ids", "scope_fingerprint"}}
    if record.get("baseline_turn_ids") is not None:
        view["baseline_turn_count"] = len(record["baseline_turn_ids"])
    if record.get("baseline_completed_item_ids") is not None:
        view["baseline_completed_item_count"] = len(record["baseline_completed_item_ids"])
    return view


class InferenceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    upstream_url: str = ""
    api_key_env: str = "SUPER_BRAIN_UPSTREAM_KEY"
    public_url: str | None = None
    ledger_path: str = ".runtime/inference-usage.db"
    total_token_limit: int = Field(default=90_000_000, gt=0)
    max_output_tokens: int = Field(default=16384, gt=0)
    max_concurrent_requests: int = Field(default=2, ge=1)
    queue_timeout: float = Field(default=600, gt=0)


class ServiceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    database: str = ".runtime/super-brain.db"
    listen: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)
    token_env: str = "SUPER_BRAIN_TOKEN"
    allowed_hosts: list[str] = Field(default_factory=lambda: ["127.0.0.1:*", "localhost:*", "[::1]:*"])
    poll_interval: float = Field(default=15, ge=1)
    reconnect_interval: float = Field(default=5, ge=1)
    hosts: list[Host] = Field(default_factory=list)
    backends: list[Backend] = Field(default_factory=list)
    routes: list[ModelRoute] = Field(default_factory=list)
    supervisor: dict[str, Any] | None = None
    auto_dispatch: bool = False
    inference: InferenceConfig = Field(default_factory=InferenceConfig)
    default_task_budget: Budget = Field(default_factory=lambda: Budget(max_requests=20, max_tokens=500000))

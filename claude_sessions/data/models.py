from datetime import datetime
from typing import Optional, List, Any
from pydantic import BaseModel, Field


class ToolCallDetail(BaseModel):
    """Detailed tool call information."""

    name: str
    id: Optional[str] = None
    input_summary: Optional[str] = None  # Short summary of inputs
    file_path: Optional[str] = None  # For file operations
    command: Optional[str] = None  # For bash operations
    query: Optional[str] = None  # For search operations


class ConversationMessage(BaseModel):
    """A single message in a conversation."""

    uuid: str
    parent_uuid: Optional[str] = None
    type: str  # "user" | "assistant" | "summary"
    timestamp: datetime
    content: str  # Extracted text content
    tool_calls: List[dict] = Field(default_factory=list)
    tool_details: List[ToolCallDetail] = Field(default_factory=list)
    thinking: Optional[str] = None
    model: Optional[str] = None
    token_usage: Optional[dict] = None
    is_sidechain: bool = False


class SessionMetadata(BaseModel):
    """Metadata for a Claude Code session."""

    model_config = {"protected_namespaces": ()}

    session_id: str
    project_path: str
    start_time: datetime
    last_activity: datetime
    message_count: int = 0
    user_message_count: int = 0
    assistant_message_count: int = 0
    model_used: Optional[str] = None
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    summaries: List[str] = Field(default_factory=list)
    is_active: bool = False
    file_path: Optional[str] = None
    title: Optional[str] = None
    first_user_message: Optional[str] = None
    has_pasted_content: bool = False
    pasted_content_types: List[str] = Field(default_factory=list)


class SessionStats(BaseModel):
    """Aggregated statistics across all sessions."""

    model_config = {"protected_namespaces": ()}

    total_sessions: int = 0
    total_messages: int = 0
    active_sessions: int = 0
    daily_activity: List[dict] = Field(default_factory=list)
    model_usage: dict = Field(default_factory=dict)
    longest_session: Optional[dict] = None
    first_session_date: Optional[str] = None


class TodoItem(BaseModel):
    """A todo item from a session."""

    content: str
    status: str  # "pending" | "in_progress" | "completed"
    active_form: Optional[str] = None


class Artifact(BaseModel):
    """A file artifact created or modified by Claude."""

    file_path: str
    file_name: str
    file_type: str  # code, document, config, image, etc.
    operation: str  # create, edit
    session_id: str
    timestamp: datetime
    size_bytes: int = 0
    mime_type: str = "application/octet-stream"
    exists: bool = True  # Whether file still exists on disk


class ConversationThread(BaseModel):
    """A user question + Claude response grouped as one collapsible thread."""

    thread_id: str
    user_message: Optional[ConversationMessage] = None
    assistant_messages: List[ConversationMessage] = Field(default_factory=list)
    depth: int = 0
    is_sidechain: bool = False
    branch_children: List["ConversationThread"] = Field(default_factory=list)
    parent_thread_id: Optional[str] = None


class ConversationTree(BaseModel):
    """Full conversation as a tree of threads."""

    threads: List[ConversationThread] = Field(default_factory=list)
    branch_points: int = 0
    total_messages: int = 0


class JournalEntry(BaseModel):
    """A daily journal entry written by Claude reflecting on conversations."""

    id: str
    date: str  # YYYY-MM-DD
    content: str
    emotional_tone: str
    secondary_tones: List[dict] = Field(default_factory=list)
    session_ids: List[str] = Field(default_factory=list)
    model_used: Optional[str] = None
    prompt_version: str = "1"
    token_cost: Optional[dict] = None
    created_at: Optional[str] = None


class MemoryOrb(BaseModel):
    """A specific moment worth preserving, extracted from conversations."""

    id: str
    date: str
    content: str
    emotion: str  # clarity, frustration, momentum, doubt, playfulness, avoidance, breakthrough, tension, pride, vulnerability, focus, scattered
    intensity: float = 0.5  # 0.0-1.0
    source_session_id: Optional[str] = None
    source_context: Optional[str] = None
    is_core: bool = False
    promoted_at: Optional[str] = None
    island_id: Optional[str] = None
    created_at: Optional[str] = None


class Island(BaseModel):
    """A persistent theme that emerges from clusters of core memories."""

    id: str
    name: str  # e.g. "The Craft Island"
    description: str
    formed_at: str
    last_updated: str
    strength: float = 0.5  # 0.0-1.0
    core_memory_ids: List[str] = Field(default_factory=list)
    status: str = "active"  # active, fading, merged


class SessionContext(BaseModel):
    """Context export for session continuation."""

    session_id: str
    project_path: str
    start_time: datetime
    last_activity: datetime
    duration_minutes: int
    summary: str
    key_files: List[str] = Field(default_factory=list)
    pending_todos: List[TodoItem] = Field(default_factory=list)
    recent_messages: List[ConversationMessage] = Field(default_factory=list)
    continuation_prompt: str
    resume_command: Optional[str] = None

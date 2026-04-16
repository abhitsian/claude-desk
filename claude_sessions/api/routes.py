from fastapi import APIRouter, Query, HTTPException
from typing import Optional, List

from ..data import SessionParser, ActiveSessionDetector, SessionMetadata, ArtifactParser
from ..services.context_generator import ContextGenerator

router = APIRouter(prefix="/api")

# Initialize services
parser = SessionParser()
detector = ActiveSessionDetector()
artifact_parser = ArtifactParser()
context_gen = ContextGenerator(parser, detector)


@router.get("/sessions")
async def list_sessions(
    limit: int = Query(50, le=200),
    offset: int = 0,
    active_only: bool = False,
) -> dict:
    """List all sessions with pagination."""
    sessions = parser.get_all_sessions()
    active_ids = set(detector.get_active_sessions())

    # Mark active sessions
    for session in sessions:
        session.is_active = session.session_id in active_ids

    # Filter if needed
    if active_only:
        sessions = [s for s in sessions if s.is_active]

    total = len(sessions)
    sessions = sessions[offset : offset + limit]

    return {
        "sessions": [s.model_dump() for s in sessions],
        "total": total,
        "active_count": len(active_ids),
    }


@router.get("/sessions/active")
async def list_active_sessions() -> dict:
    """List currently active sessions."""
    active_ids = detector.get_active_sessions()
    latest_id = detector.get_latest_session_id()

    sessions = []
    for session_id in active_ids:
        session = parser.get_session(session_id)
        if session:
            session.is_active = True
            sessions.append(session)

    # Sort by last activity
    sessions.sort(key=lambda s: s.last_activity, reverse=True)

    return {
        "sessions": [s.model_dump() for s in sessions],
        "latest_session_id": latest_id,
        "count": len(sessions),
    }


@router.get("/sessions/{session_id}")
async def get_session(session_id: str) -> dict:
    """Get detailed session metadata."""
    session = parser.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    session.is_active = detector.is_session_active(session_id)
    todos = parser.get_session_todos(session_id)

    return {
        "session": session.model_dump(),
        "todos": [t.model_dump() for t in todos],
    }


@router.get("/sessions/{session_id}/messages")
async def get_session_messages(
    session_id: str,
    limit: int = Query(100, le=500),
    offset: int = 0,
) -> dict:
    """Get conversation messages for a session."""
    session = parser.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    messages = parser.get_session_messages(session_id, limit=limit, offset=offset)
    total = session.user_message_count + session.assistant_message_count

    return {
        "messages": [m.model_dump() for m in messages],
        "total": total,
        "has_more": offset + len(messages) < total,
    }


@router.post("/sessions/{session_id}/context")
async def generate_context(
    session_id: str,
    include_files: bool = True,
    include_todos: bool = True,
    max_messages: int = 10,
) -> dict:
    """Generate a context prompt for continuing a session."""
    session = parser.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    context = context_gen.generate_context(
        session_id,
        include_files=include_files,
        include_todos=include_todos,
        max_recent_messages=max_messages,
    )

    return context.model_dump()


@router.get("/stats")
async def get_stats() -> dict:
    """Get aggregated usage statistics."""
    stats = parser.get_stats()
    active_count = len(detector.get_active_sessions())
    stats.active_sessions = active_count
    return stats.model_dump()


@router.get("/search")
async def search_sessions(
    q: str = Query(..., min_length=2),
    search_content: bool = False,
) -> dict:
    """Search sessions by content or metadata."""
    results = parser.search_sessions(q, search_content=search_content)
    active_ids = set(detector.get_active_sessions())

    for session in results:
        session.is_active = session.session_id in active_ids

    return {
        "results": [s.model_dump() for s in results],
        "total": len(results),
        "query": q,
    }


# ===== Artifacts =====


@router.get("/viz/sessions")
async def viz_sessions_data(
    limit: int = Query(200, le=500),
) -> dict:
    """Session data shaped for visualizations (terrain + scatter)."""
    from ..services.insights import calculate_session_cost
    sessions = parser.get_all_sessions()
    active_ids = set(detector.get_active_sessions())

    data = []
    for s in sessions[:limit]:
        cost = calculate_session_cost(s)
        duration_min = (s.last_activity - s.start_time).total_seconds() / 60
        data.append({
            "id": s.session_id,
            "title": s.title or s.session_id[:8],
            "start": s.start_time.isoformat(),
            "last": s.last_activity.isoformat(),
            "messages": s.message_count,
            "user_messages": s.user_message_count,
            "assistant_messages": s.assistant_message_count,
            "input_tokens": s.total_input_tokens,
            "output_tokens": s.total_output_tokens,
            "total_tokens": s.total_input_tokens + s.total_output_tokens,
            "cost": cost["total_cost"],
            "model": s.model_used or "unknown",
            "duration_min": round(duration_min, 1),
            "active": s.session_id in active_ids,
            "has_pasted": s.has_pasted_content,
            "hour": s.start_time.hour,
            "day": s.start_time.strftime("%a"),
            "date": s.start_time.strftime("%Y-%m-%d"),
        })

    return {"sessions": data, "total": len(data)}


@router.get("/artifacts")
async def list_artifacts(
    limit: int = Query(100, le=500),
    file_type: Optional[str] = None,
    session_id: Optional[str] = None,
) -> dict:
    """List all artifacts (files created/modified by Claude)."""
    if session_id:
        artifacts = artifact_parser.get_session_artifacts(session_id)
    else:
        artifacts = artifact_parser.get_all_artifacts(limit=limit)

    if file_type:
        artifacts = [a for a in artifacts if a.file_type == file_type]

    return {
        "artifacts": [a.model_dump() for a in artifacts],
        "total": len(artifacts),
    }


@router.get("/artifacts/stats")
async def get_artifact_stats() -> dict:
    """Get artifact statistics."""
    return artifact_parser.get_artifact_stats()


@router.get("/sessions/{session_id}/topics")
async def get_session_topics(session_id: str) -> dict:
    """Extract topic blocks from a session."""
    from ..services.topic_extractor import extract_topic_blocks
    session = parser.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    blocks = extract_topic_blocks(session_id, parser)
    return {
        "session_id": session_id,
        "topics": [b.to_dict() for b in blocks],
        "count": len(blocks),
    }


@router.get("/search/messages")
async def search_messages(
    q: str = Query(..., min_length=2),
    limit: int = Query(50, le=200),
) -> dict:
    """Message-level search — returns individual messages matching the query,
    with session context and deep-link UUIDs."""
    from ..data.search_index import SearchIndex
    from ..data.archive import SessionArchive
    idx = SearchIndex()
    archive = SessionArchive()
    results = idx.search_messages(q, limit=limit)
    # Also search archive
    archive_results = archive.search_messages(q, limit=limit)
    seen = {r["message_uuid"] for r in results}
    for ar in archive_results:
        if ar["message_uuid"] not in seen:
            results.append(ar)
            seen.add(ar["message_uuid"])
    return {"results": results[:limit], "total": len(results), "query": q}


@router.get("/sessions/{session_id}/message/{message_uuid}/context")
async def get_message_context(session_id: str, message_uuid: str, surrounding: int = Query(2, le=5)) -> dict:
    """Get a message and its surrounding messages for expanded search result context."""
    from ..data.archive import SessionArchive
    archive = SessionArchive()

    # Try live session first
    session_file = parser._find_session_file(session_id)

    msg_list = []
    if session_file:
        try:
            msg_list = [
                {"uuid": m.uuid, "type": m.type, "content": m.content or "",
                 "timestamp": m.timestamp.isoformat()}
                for m in parser._stream_messages(session_file)
                if m.type in ("user", "assistant") and m.content
            ]
        except Exception:
            pass

    if not msg_list:
        # Try archive
        archived_msgs = archive.get_archived_messages(session_id)
        msg_list = [
            {"uuid": m["uuid"] or "", "type": m["type"], "content": m["content"] or "",
             "timestamp": m["timestamp"]}
            for m in archived_msgs
            if m["type"] in ("user", "assistant") and m.get("content")
        ]

    # Find the target message and return surrounding context
    target_idx = None
    for i, m in enumerate(msg_list):
        if m["uuid"] == message_uuid:
            target_idx = i
            break

    if target_idx is None:
        raise HTTPException(status_code=404, detail="Message not found")

    start = max(0, target_idx - surrounding)
    end = min(len(msg_list), target_idx + surrounding + 1)
    context_msgs = []
    for i in range(start, end):
        m = msg_list[i]
        # Truncate very long messages but keep more than the snippet
        content = m["content"]
        if len(content) > 2000:
            content = content[:2000] + "..."
        context_msgs.append({
            "uuid": m["uuid"],
            "type": m["type"],
            "content": content,
            "timestamp": m["timestamp"],
            "is_target": i == target_idx,
        })

    return {"messages": context_msgs, "session_id": session_id, "message_uuid": message_uuid}


@router.get("/sessions/{session_id}/artifacts")
async def get_session_artifacts(session_id: str) -> dict:
    """Get artifacts for a specific session."""
    session = parser.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    artifacts = artifact_parser.get_session_artifacts(session_id)

    return {
        "artifacts": [a.model_dump() for a in artifacts],
        "total": len(artifacts),
        "session_id": session_id,
    }

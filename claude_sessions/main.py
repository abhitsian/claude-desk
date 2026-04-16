import json
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .api import router as api_router
from .data import SessionParser, ActiveSessionDetector, ArtifactParser, SearchIndex, SessionMetadata
from .data import favorites
from .data.archive import SessionArchive
from .data.history_reader import get_all_session_history
from .data.semantic_index import SemanticIndex
from .config import settings

app = FastAPI(
    title="Claude Desk",
    description="View and manage UI for Claude Code",
    version="0.2.0",
)

# Mount static files
static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")

# Templates
templates_dir = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=templates_dir)

# Include API routes
app.include_router(api_router)

# Initialize services
parser = SessionParser()
detector = ActiveSessionDetector()
artifact_parser = ArtifactParser()
search_index = SearchIndex()
archive = SessionArchive()
semantic_index = SemanticIndex()


@app.on_event("startup")
async def startup_event():
    """Build search index on startup."""
    if search_index.is_stale():
        search_index.build_index()
    # Update which archived sessions are still live
    archive._mark_gone_sessions()

    # Auto-generate descriptions for sessions missing them (runs in background)
    import threading
    def _auto_describe():
        from .services.session_describer import describe_session, get_cached_description
        try:
            sessions = parser.get_all_sessions()
            for s in sessions[:30]:  # Cap at 30 per startup to avoid long blocks
                if not get_cached_description(s.session_id):
                    try:
                        describe_session(s.session_id, parser)
                    except Exception:
                        continue
        except Exception:
            pass
    threading.Thread(target=_auto_describe, daemon=True).start()


def format_duration(minutes: int) -> str:
    """Format duration in human-readable form."""
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    mins = minutes % 60
    if mins == 0:
        return f"{hours}h"
    return f"{hours}h {mins}m"


def format_timestamp(dt) -> str:
    """Format timestamp for display."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc) if dt.tzinfo else datetime.now()
    diff = now - dt

    if diff.days == 0:
        hours = diff.seconds // 3600
        if hours == 0:
            minutes = diff.seconds // 60
            return f"{minutes}m ago"
        return f"{hours}h ago"
    elif diff.days == 1:
        return "Yesterday"
    elif diff.days < 7:
        return f"{diff.days}d ago"
    else:
        return dt.strftime("%b %d")


# Add template filters
templates.env.filters["format_duration"] = format_duration
templates.env.filters["format_timestamp"] = format_timestamp


def _archived_to_metadata(a: dict) -> SessionMetadata:
    """Convert an archive row dict to SessionMetadata."""
    return SessionMetadata(
        session_id=a["session_id"],
        project_path=a.get("project_path") or "",
        start_time=datetime.fromisoformat(a["start_time"]),
        last_activity=datetime.fromisoformat(a["last_activity"]),
        message_count=a["message_count"],
        user_message_count=a.get("user_message_count", 0),
        assistant_message_count=a.get("assistant_message_count", 0),
        model_used=a.get("model_used"),
        total_input_tokens=a.get("total_input_tokens", 0),
        total_output_tokens=a.get("total_output_tokens", 0),
        title=a.get("title"),
        has_pasted_content=bool(a.get("has_pasted_content", 0)),
        pasted_content_types=json.loads(a.get("pasted_content_types") or "[]"),
    )


import time as _time

# Simple in-memory cache with TTL
_cache: dict = {}
_CACHE_TTL = 30  # seconds — fresh enough for dashboard, avoids re-parsing on every click


def _cached(key: str, ttl: int = _CACHE_TTL):
    """Decorator for caching function results with TTL."""
    def decorator(fn):
        def wrapper(*args, **kwargs):
            cache_key = f"{key}:{args}:{kwargs}"
            entry = _cache.get(cache_key)
            if entry and (_time.time() - entry[0]) < ttl:
                return entry[1]
            result = fn(*args, **kwargs)
            _cache[cache_key] = (_time.time(), result)
            return result
        return wrapper
    return decorator


@_cached("unified_sessions", ttl=30)
def _get_all_sessions_unified(limit: int = 200) -> list:
    """Get ALL sessions ever — live JSONL + archived DB + history.jsonl metadata."""
    # 1. Live sessions (have JSONL files)
    live = parser.get_all_sessions()
    seen_ids = {s.session_id for s in live}

    # 2. Archived sessions (JSONL deleted but we saved content)
    archived = archive.get_archived_only(limit=500)
    for a in archived:
        if a["session_id"] not in seen_ids:
            seen_ids.add(a["session_id"])
            live.append(_archived_to_metadata(a))

    # 3. History-only sessions (JSONL deleted, not archived, but history.jsonl has prompts)
    all_history = get_all_session_history()
    for sid, entry in all_history.items():
        if sid in seen_ids:
            continue
        seen_ids.add(sid)
        if entry["start_time"] and entry["last_activity"]:
            live.append(SessionMetadata(
                session_id=sid,
                project_path=entry.get("project_path") or "",
                start_time=entry["start_time"],
                last_activity=entry["last_activity"],
                message_count=entry.get("message_count", 0),
                title=entry.get("title"),
            ))

    live.sort(key=lambda s: s.last_activity, reverse=True)
    return live[:limit]


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Main dashboard page — shows live + archived sessions unified."""
    sessions = _get_all_sessions_unified(limit=20)
    active_ids = set(detector.get_active_sessions())
    latest_id = detector.get_latest_session_id()

    for session in sessions:
        session.is_active = session.session_id in active_ids

    active_sessions = [s for s in sessions if s.is_active]
    recent_sessions = [s for s in sessions if not s.is_active][:10]

    stats = parser.get_stats()
    stats.active_sessions = len(active_ids)
    # Show total from unified view, not just live JSONL
    all_unified = _get_all_sessions_unified()
    stats.total_sessions = len(all_unified)
    stats.total_messages = sum(s.message_count for s in all_unified)

    # Archive status
    archive_stats = archive.get_stats()
    last_archive_ts = archive.get_last_archive_time()
    if last_archive_ts:
        last_archive_dt = datetime.fromtimestamp(last_archive_ts)
        last_archive_time = format_timestamp(last_archive_dt)
        last_archive_full = last_archive_dt.strftime("%Y-%m-%d %H:%M")
    else:
        last_archive_time = None
        last_archive_full = None

    # Get favorites
    fav_ids = {f["session_id"] for f in favorites.get_favorites()}

    # Get favorited sessions that might not be in recent
    fav_sessions = []
    if fav_ids:
        for fav_id in fav_ids:
            session = parser.get_session(fav_id)
            if session:
                session.is_active = session.session_id in active_ids
                fav_sessions.append(session)
        fav_sessions.sort(key=lambda s: s.last_activity, reverse=True)

    # Load favorite labels and descriptions
    fav_labels = {f["session_id"]: f.get("label", "") for f in favorites.get_favorites()}
    from .services.session_describer import get_cached_description
    descriptions = {}
    for s in fav_sessions + recent_sessions[:5]:
        desc = get_cached_description(s.session_id)
        if desc:
            descriptions[s.session_id] = desc

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "active_sessions": active_sessions,
            "recent_sessions": recent_sessions,
            "fav_sessions": fav_sessions,
            "fav_ids": fav_ids,
            "fav_labels": fav_labels,
            "descriptions": descriptions,
            "stats": stats,
            "latest_id": latest_id,
            "archive_stats": archive_stats,
            "last_archive_time": last_archive_time,
            "last_archive_full": last_archive_full,
        },
    )


@app.get("/timeline", response_class=HTMLResponse)
async def timeline(request: Request, view: str = "topics"):
    """Timeline view — all sessions grouped by day, with topic extraction.

    view=topics (default): Groups by topic clusters within each day
    view=sessions: Classic flat session list per day
    """
    from .services.topic_extractor import extract_session_topics_summary, cluster_topics_across_sessions

    sessions = _get_all_sessions_unified(limit=200)
    active_ids = set(detector.get_active_sessions())
    fav_ids = {f["session_id"] for f in favorites.get_favorites()}

    for session in sessions:
        session.is_active = session.session_id in active_ids

    days: OrderedDict = OrderedDict()
    for s in sessions:
        day_key = s.start_time.strftime("%Y-%m-%d")
        day_label = s.start_time.strftime("%A, %B %d, %Y")
        if day_key not in days:
            days[day_key] = {"label": day_label, "sessions": [], "date": day_key, "topic_clusters": [], "session_topics": {}}
        days[day_key]["sessions"].append(s)

    # Extract topics for each session and cluster within each day
    if view == "topics":
        for day_key, day_data in days.items():
            day_session_topics = []
            for s in day_data["sessions"]:
                try:
                    topics_summary = extract_session_topics_summary(s.session_id, parser)
                    day_session_topics.append(topics_summary)
                    day_data["session_topics"][s.session_id] = topics_summary
                except Exception:
                    day_data["session_topics"][s.session_id] = {"topics": [], "topic_count": 0, "primary_domain": None}
            day_data["topic_clusters"] = cluster_topics_across_sessions(day_session_topics)

    # Load cached descriptions (no LLM calls on page load)
    from .services.session_describer import get_cached_description
    descriptions = {}
    for s in sessions:
        desc = get_cached_description(s.session_id)
        if desc:
            descriptions[s.session_id] = desc

    # Load favorite labels
    fav_labels = {f["session_id"]: f.get("label", "") for f in favorites.get_favorites()}

    return templates.TemplateResponse(
        "timeline.html",
        {
            "request": request,
            "days": list(days.values()),
            "fav_ids": fav_ids,
            "fav_labels": fav_labels,
            "descriptions": descriptions,
            "view": view,
        },
    )


@app.get("/sessions", response_class=HTMLResponse)
async def sessions_list(request: Request, page: int = 1, q: str = "", mode: str = "sessions", sort: str = "relevance"):
    """Session list page with FTS search. Includes archived sessions.

    mode=sessions (default): session-level results
    mode=messages: message-level results with deep links
    """
    page_size = 20
    offset = (page - 1) * page_size

    message_results = []

    if q:
        if mode == "messages":
            # Hybrid search: semantic + keyword FTS
            # 1. Get FTS keyword results
            fts_results = search_index.search_messages(q, limit=100)
            archive_msg_results = archive.search_messages(q, limit=100)
            seen_uuids = {r["message_uuid"] for r in fts_results}
            for ar in archive_msg_results:
                if ar["message_uuid"] not in seen_uuids:
                    fts_results.append(ar)
                    seen_uuids.add(ar["message_uuid"])

            # 2. Hybrid: merge semantic + FTS with weighted scoring
            try:
                message_results = semantic_index.hybrid_search(
                    q, fts_results, top_k=50,
                    semantic_weight=0.6, fts_weight=0.4,
                )
            except Exception:
                # Fallback to FTS-only if semantic index not built yet
                message_results = fts_results
                message_results.sort(key=lambda r: -r.get("match_score", 0))

            # Apply sort order
            if sort == "recent":
                message_results.sort(key=lambda r: r.get("timestamp", ""), reverse=True)

        # Always get session-level results too
        # Use NL preprocessing for better results
        processed_q = search_index._preprocess_nl_query(q)
        archive_results = archive.search(processed_q, limit=100)
        # Also search live index
        live_results = search_index.search(processed_q, limit=100)

        sessions = []
        seen_ids = set()

        # Merge live and archive results, deduping
        for result in live_results:
            sid = result["session_id"]
            if sid in seen_ids:
                continue
            seen_ids.add(sid)
            session = parser.get_session(sid)
            if session:
                sessions.append(session)

        for result in archive_results:
            sid = result["session_id"]
            if sid in seen_ids:
                continue
            seen_ids.add(sid)
            session = parser.get_session(sid)
            if not session:
                archived_data = archive.get_archived_session(sid)
                if archived_data:
                    session = SessionMetadata(
                        session_id=sid,
                        project_path=archived_data["project_path"] or "",
                        start_time=datetime.fromisoformat(archived_data["start_time"]),
                        last_activity=datetime.fromisoformat(archived_data["last_activity"]),
                        message_count=archived_data["message_count"],
                        user_message_count=archived_data.get("user_message_count", 0),
                        assistant_message_count=archived_data.get("assistant_message_count", 0),
                        model_used=archived_data.get("model_used"),
                        title=archived_data.get("title"),
                        has_pasted_content=bool(archived_data.get("has_pasted_content", 0)),
                        pasted_content_types=json.loads(archived_data.get("pasted_content_types") or "[]"),
                    )
            if session:
                sessions.append(session)
    else:
        sessions = _get_all_sessions_unified(limit=200)

    active_ids = set(detector.get_active_sessions())
    fav_ids = {f["session_id"] for f in favorites.get_favorites()}
    for session in sessions:
        session.is_active = session.session_id in active_ids

    total = len(sessions)
    paginated_sessions = sessions[offset : offset + page_size]
    total_pages = (total + page_size - 1) // page_size

    # Semantic index stats for UI
    sem_stats = semantic_index.get_stats()
    sem_stats["is_stale"] = semantic_index.is_stale()

    # Check if hybrid search was used
    has_semantic = (
        mode == "messages" and q and
        sem_stats.get("index_exists") and
        any(r.get("semantic_score", 0) > 0 for r in message_results[:5])
    )

    # Load cached descriptions for paginated sessions
    from .services.session_describer import get_cached_description
    descriptions = {}
    for s in paginated_sessions:
        desc = get_cached_description(s.session_id)
        if desc:
            descriptions[s.session_id] = desc

    fav_labels = {f["session_id"]: f.get("label", "") for f in favorites.get_favorites()}

    return templates.TemplateResponse(
        "sessions.html",
        {
            "request": request,
            "sessions": paginated_sessions,
            "page": page,
            "total_pages": total_pages,
            "total": total,
            "query": q,
            "fav_ids": fav_ids,
            "fav_labels": fav_labels,
            "descriptions": descriptions,
            "mode": mode,
            "message_results": message_results[:50] if mode == "messages" else [],
            "message_total": len(message_results) if mode == "messages" else 0,
            "semantic_stats": sem_stats,
            "has_semantic": has_semantic,
            "sort": sort,
        },
    )


@app.get("/sessions/{session_id}", response_class=HTMLResponse)
async def session_detail(request: Request, session_id: str):
    """Session detail page. Falls back to archive if JSONL is gone."""
    session = parser.get_session(session_id)
    from_archive = False

    if session:
        session.is_active = detector.is_session_active(session_id)
        conv_tree = parser.get_conversation_tree(session_id)
        todos = parser.get_session_todos(session_id)
    else:
        # Try archive
        archived = archive.get_archived_session(session_id)
        if not archived:
            return templates.TemplateResponse(
                "error.html",
                {"request": request, "message": "Session not found"},
                status_code=404,
            )
        from_archive = True
        # Build a SessionMetadata-like object from archive
        from .data.models import SessionMetadata, ConversationThread, ConversationMessage, ToolCallDetail
        session = SessionMetadata(
            session_id=session_id,
            project_path=archived["project_path"] or "",
            start_time=datetime.fromisoformat(archived["start_time"]),
            last_activity=datetime.fromisoformat(archived["last_activity"]),
            message_count=archived["message_count"],
            user_message_count=archived["user_message_count"],
            assistant_message_count=archived["assistant_message_count"],
            model_used=archived["model_used"],
            total_input_tokens=archived["total_input_tokens"],
            total_output_tokens=archived["total_output_tokens"],
            title=archived["title"],
            has_pasted_content=bool(archived["has_pasted_content"]),
            pasted_content_types=json.loads(archived["pasted_content_types"] or "[]"),
        )

        # Build threads from archived messages
        archived_msgs = archive.get_archived_messages(session_id)
        threads = []
        current_thread = None
        for msg_data in archived_msgs:
            tool_details = []
            try:
                for td in json.loads(msg_data.get("tool_details_json") or "[]"):
                    tool_details.append(ToolCallDetail(
                        name=td.get("name", ""),
                        input_summary=td.get("input_summary"),
                        file_path=td.get("file_path"),
                        command=td.get("command"),
                    ))
            except Exception:
                pass

            msg = ConversationMessage(
                uuid=msg_data["uuid"] or "",
                parent_uuid=msg_data.get("parent_uuid"),
                type=msg_data["type"],
                timestamp=datetime.fromisoformat(msg_data["timestamp"]),
                content=msg_data["content"] or "",
                tool_details=tool_details,
                thinking=msg_data.get("thinking"),
                model=msg_data.get("model"),
                is_sidechain=bool(msg_data.get("is_sidechain", 0)),
            )

            if msg.type == "user":
                if current_thread:
                    threads.append(current_thread)
                current_thread = ConversationThread(
                    thread_id=msg.uuid,
                    user_message=msg,
                )
            elif msg.type == "assistant":
                if current_thread is None:
                    current_thread = ConversationThread(thread_id=msg.uuid)
                current_thread.assistant_messages.append(msg)

        if current_thread:
            threads.append(current_thread)

        from .data.models import ConversationTree
        conv_tree = ConversationTree(threads=threads, total_messages=len(archived_msgs))
        todos = []

    is_fav = favorites.is_favorite(session_id)

    # Get or generate summary
    summary = search_index.get_summary(session_id)
    if not summary:
        from .services.summarizer import generate_summary
        summary = generate_summary(session_id, parser)
        search_index.save_summary(session_id, summary)

    # LLM-generated description
    from .services.session_describer import get_cached_description
    description = get_cached_description(session_id)

    # Favorite label
    fav_label = ""
    if is_fav:
        for f in favorites.get_favorites():
            if f["session_id"] == session_id:
                fav_label = f.get("label", "")
                break

    # Cost + efficiency insights for this session
    from .services.insights import calculate_session_cost, analyze_prompt_efficiency, find_related_sessions
    cost = calculate_session_cost(session)
    efficiency = analyze_prompt_efficiency(session_id, parser) if not from_archive else {}

    # Related sessions
    all_sessions = parser.get_all_sessions()
    related = find_related_sessions(session_id, all_sessions, parser, top_n=4) if not from_archive else []

    return templates.TemplateResponse(
        "session_detail.html",
        {
            "request": request,
            "session": session,
            "threads": conv_tree.threads,
            "branch_points": conv_tree.branch_points if hasattr(conv_tree, 'branch_points') else 0,
            "todos": todos,
            "is_favorite": is_fav,
            "fav_label": fav_label,
            "description": description,
            "summary": summary,
            "from_archive": from_archive,
            "cost": cost,
            "efficiency": efficiency,
            "related": related,
        },
    )


@app.get("/sessions/{session_id}/markdown", response_class=PlainTextResponse)
async def session_markdown(request: Request, session_id: str):
    """Export session as markdown."""
    session = parser.get_session(session_id)
    if not session:
        return PlainTextResponse("Session not found", status_code=404)

    messages = parser.get_session_messages(session_id, limit=2000)
    title = session.title or session_id[:8]

    lines = [
        f"# {title}",
        "",
        f"**Session ID:** `{session_id}`",
        f"**Project:** `{session.project_path}`",
        f"**Started:** {session.start_time.strftime('%Y-%m-%d %H:%M')}",
        f"**Last Activity:** {session.last_activity.strftime('%Y-%m-%d %H:%M')}",
        f"**Model:** {session.model_used or 'Unknown'}",
        f"**Messages:** {session.message_count}",
        "",
        "---",
        "",
    ]

    for msg in messages:
        role = "**You**" if msg.type == "user" else "**Claude**"
        time_str = msg.timestamp.strftime("%H:%M")
        lines.append(f"### {role} ({time_str})")
        lines.append("")
        lines.append(msg.content)
        lines.append("")

        if msg.tool_details:
            for td in msg.tool_details:
                lines.append(f"> Tool: `{td.name}` — {td.input_summary or ''}")
            lines.append("")

        lines.append("---")
        lines.append("")

    md = "\n".join(lines)

    return PlainTextResponse(
        md,
        media_type="text/markdown",
        headers={
            "Content-Disposition": f'attachment; filename="session-{session_id[:8]}.md"'
        },
    )


@app.get("/sessions/{session_id}/context", response_class=HTMLResponse)
async def session_context(request: Request, session_id: str):
    """Context export page."""
    from .services.context_generator import ContextGenerator

    session = parser.get_session(session_id)
    if not session:
        return templates.TemplateResponse(
            "error.html",
            {"request": request, "message": "Session not found"},
            status_code=404,
        )

    context_gen = ContextGenerator(parser, detector)
    context = context_gen.generate_context(session_id)

    return templates.TemplateResponse(
        "context.html",
        {
            "request": request,
            "session": session,
            "context": context,
        },
    )


# ===== Favorites API =====


@app.post("/api/resume/{session_id}")
async def resume_session(session_id: str):
    """Open Terminal.app and run claude --resume for this session."""
    import subprocess
    cmd = f'claude --resume {session_id}'
    apple_script = f'''
    tell application "Terminal"
        activate
        do script "{cmd}"
    end tell
    '''
    try:
        subprocess.Popen(["osascript", "-e", apple_script])
        return JSONResponse({"ok": True, "session_id": session_id})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/fork")
async def fork_session(request: Request):
    """Fork a conversation — writes context file and opens claude in Terminal."""
    import subprocess
    import tempfile
    body = await request.json()
    prompt = body.get("fork_prompt", "")
    source = body.get("source_session", "")
    context = body.get("context", "")

    # Build the initial prompt for the forked session
    fork_text = f"I'm forking from a previous conversation (session {source[:8]}).\n\n"
    fork_text += "Here's the context from that conversation:\n\n"
    fork_text += context
    if prompt:
        fork_text += f"\n\n---\n\nNow I want to explore a different direction: {prompt}"
    else:
        fork_text += "\n\n---\n\nPlease continue from this context."

    # Write to a temp file
    fork_file = tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", prefix="claude-fork-",
        dir="/tmp", delete=False
    )
    fork_file.write(fork_text)
    fork_file.close()

    # Open Terminal and run claude with the fork file piped as first message
    cmd = f'cat {fork_file.name} | claude'
    apple_script = f'''
    tell application "Terminal"
        activate
        do script "{cmd}"
    end tell
    '''
    try:
        subprocess.Popen(["osascript", "-e", apple_script])
        return JSONResponse({"ok": True, "fork_file": fork_file.name})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/apply-playbook")
async def apply_playbook(request: Request):
    """Append prompting playbook to the user's CLAUDE.md."""
    body = await request.json()
    content = body.get("content", "")
    if not content:
        return JSONResponse({"ok": False, "error": "No content"})

    claude_md = Path.home() / ".claude" / "CLAUDE.md"
    marker = "# Prompting Guidelines (auto-generated from usage patterns)"

    try:
        existing = claude_md.read_text() if claude_md.exists() else ""

        # Remove old playbook block if present
        if marker in existing:
            before = existing[:existing.index(marker)].rstrip()
            # Find the end of the block (next # heading or end of file)
            after_start = existing.index(marker) + len(marker)
            remaining = existing[after_start:]
            # Find next top-level heading
            next_heading = remaining.find("\n# ")
            if next_heading >= 0:
                after = remaining[next_heading:]
            else:
                after = ""
            existing = (before + "\n\n" + after).strip()

        # Append new playbook
        new_content = existing + "\n\n" + content + "\n"
        claude_md.write_text(new_content)
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.post("/api/favorites/{session_id}/toggle")
async def toggle_favorite(session_id: str):
    """Toggle favorite status for a session."""
    new_state = favorites.toggle_favorite(session_id)
    return JSONResponse({"favorited": new_state, "session_id": session_id})


@app.post("/api/favorites/{session_id}/pin")
async def pin_session(session_id: str, request: Request):
    """Pin a session with a custom name."""
    body = await request.json()
    label = body.get("label", "").strip()

    # Ensure it's favorited first
    if not favorites.is_favorite(session_id):
        favorites.toggle_favorite(session_id, label=label)
    else:
        favorites.set_label(session_id, label)

    return JSONResponse({"ok": True, "session_id": session_id, "label": label})


@app.post("/api/describe/{session_id}")
async def describe_session_api(session_id: str):
    """Generate an LLM description for a session."""
    from .services.session_describer import describe_session
    try:
        description = describe_session(session_id, parser)
        return JSONResponse({"ok": True, "session_id": session_id, "description": description})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/describe/batch")
async def describe_batch_api(request: Request):
    """Generate descriptions for multiple sessions."""
    from .services.session_describer import describe_session
    body = await request.json()
    session_ids = body.get("session_ids", [])[:20]  # Cap at 20
    results = {}
    for sid in session_ids:
        try:
            results[sid] = describe_session(sid, parser)
        except Exception:
            results[sid] = "Description unavailable."
    return JSONResponse({"ok": True, "descriptions": results})


# ===== Semantic Search API =====


@app.post("/api/semantic/build")
async def build_semantic_index(request: Request):
    """Build or rebuild the semantic search index."""
    body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    force = body.get("force", False)
    try:
        stats = semantic_index.build_index(force=force)
        return JSONResponse({"ok": True, **stats})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/api/semantic/stats")
async def semantic_stats():
    """Get semantic index statistics."""
    return JSONResponse(semantic_index.get_stats())


@app.post("/api/semantic/search")
async def semantic_search_api(request: Request):
    """Direct semantic search API."""
    body = await request.json()
    query = body.get("query", "")
    top_k = body.get("top_k", 20)
    if not query:
        return JSONResponse({"error": "No query"}, status_code=400)
    try:
        results = semantic_index.search(query, top_k=top_k)
        return JSONResponse({"results": results, "total": len(results), "query": query})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/intelligence", response_class=HTMLResponse)
async def intelligence_page(request: Request, tab: str = "cost", days: int = 7):
    """Unified intelligence page — cost, prompting, skills, tools."""
    from .services.insights import (
        calculate_period_costs, analyze_cross_session_patterns,
        calculate_session_cost, generate_prompting_playbook,
    )
    from .services.skill_analytics import get_skill_stats
    from .services.tool_analytics import get_tool_stats
    from .services.skill_scanner import scan_skills, group_by_source, get_stats as get_skill_browser_stats

    # Only compute data for the active tab — saves seconds per page load
    costs = patterns = playbook = session_costs = None
    skill_stats = tool_stats = None
    skill_groups = skill_browser_stats = None

    if tab == "cost":
        all_sessions = _get_all_sessions_unified(limit=500)
        costs = calculate_period_costs(all_sessions, days=days)
        session_costs = []
        for s in all_sessions[:50]:
            c = calculate_session_cost(s)
            c["title"] = s.title
            c["session_id"] = s.session_id
            c["date"] = s.start_time.strftime("%b %d")
            session_costs.append(c)

    elif tab == "prompting":
        all_sessions = _get_all_sessions_unified(limit=500)
        patterns = analyze_cross_session_patterns(all_sessions, parser)
        live_sessions = parser.get_all_sessions()
        playbook = generate_prompting_playbook(live_sessions, parser, days=days)

    elif tab == "skills":
        live_sessions = parser.get_all_sessions()
        skill_stats = get_skill_stats(live_sessions, parser, days=days)

    elif tab == "tools":
        live_sessions = parser.get_all_sessions()
        tool_stats = get_tool_stats(live_sessions, parser, days=days)

    elif tab == "browse":
        all_skills = scan_skills()
        skill_groups = group_by_source(all_skills)
        skill_browser_stats = get_skill_browser_stats(all_skills)

    return templates.TemplateResponse(
        "intelligence.html",
        {
            "request": request,
            "tab": tab,
            "days": days,
            "costs": costs or {},
            "patterns": patterns or {},
            "playbook": playbook or {},
            "session_costs": session_costs or [],
            "skill_stats": skill_stats or {},
            "tool_stats": tool_stats or {},
            "skill_groups": skill_groups or {},
            "skill_browser_stats": skill_browser_stats or {},
        },
    )


@app.get("/insights", response_class=HTMLResponse)
async def insights_page(request: Request, days: int = 7):
    """Cross-conversation insights and cost analysis."""
    from .services.insights import (
        calculate_period_costs, analyze_cross_session_patterns,
        calculate_session_cost, generate_prompting_playbook,
    )

    all_sessions = _get_all_sessions_unified(limit=500)

    # Period costs
    costs = calculate_period_costs(all_sessions, days=days)

    # Cross-session patterns
    patterns = analyze_cross_session_patterns(all_sessions, parser)

    # Personalized prompting playbook — reactive to selected time window
    live_sessions = parser.get_all_sessions()
    playbook = generate_prompting_playbook(live_sessions, parser, days=days)

    # Per-session costs for the table
    session_costs = []
    for s in all_sessions[:50]:
        c = calculate_session_cost(s)
        c["title"] = s.title
        c["session_id"] = s.session_id
        c["date"] = s.start_time.strftime("%b %d")
        session_costs.append(c)

    return templates.TemplateResponse(
        "insights.html",
        {
            "request": request,
            "costs": costs,
            "patterns": patterns,
            "playbook": playbook,
            "session_costs": session_costs,
            "days": days,
        },
    )


@app.get("/skills", response_class=HTMLResponse)
async def skills_page(request: Request):
    """Skills browser — all commands and skills from user + plugins."""
    from .services.skill_scanner import scan_skills, group_by_source, get_stats

    all_skills = scan_skills()
    groups = group_by_source(all_skills)
    stats = get_stats(all_skills)

    return templates.TemplateResponse(
        "skills.html",
        {
            "request": request,
            "groups": groups,
            "stats": stats,
        },
    )


@app.get("/visualize", response_class=HTMLResponse)
async def visualize_page(request: Request):
    """Rich visualizations — 3D terrain + constellation scatter."""
    return templates.TemplateResponse(
        "visualize.html",
        {"request": request},
    )


@app.get("/artifacts", response_class=HTMLResponse)
async def artifacts_list(request: Request, type: str = ""):
    """Artifacts gallery — versioned creations with app launcher."""
    from .data.artifact_store import get_all_artifacts, get_artifact_stats

    all_artifacts = get_all_artifacts(artifact_type=type if type else None, limit=200)
    stats = get_artifact_stats()

    # Split into launchable apps vs everything else
    apps = [a for a in all_artifacts if a["artifact_type"] == "app" and a["exists_on_disk"]]
    other = [a for a in all_artifacts if a not in apps]

    return templates.TemplateResponse(
        "artifacts_v2.html",
        {
            "request": request,
            "apps": apps,
            "other_artifacts": other,
            "stats": stats,
        },
    )


@app.get("/artifacts/{artifact_id}", response_class=HTMLResponse)
async def artifact_detail_page(request: Request, artifact_id: int):
    """Artifact detail with version history and preview."""
    from .data.artifact_store import get_artifact_detail

    artifact = get_artifact_detail(artifact_id)
    if not artifact:
        return templates.TemplateResponse(
            "error.html",
            {"request": request, "message": "Artifact not found"},
            status_code=404,
        )

    return templates.TemplateResponse(
        "artifact_detail.html",
        {"request": request, "artifact": artifact},
    )


@app.get("/artifacts/{artifact_id}/raw")
async def artifact_raw(artifact_id: int):
    """Serve the raw artifact file (for iframe preview / image display)."""
    from .data.artifact_store import get_artifact_detail
    from fastapi.responses import FileResponse

    artifact = get_artifact_detail(artifact_id)
    if not artifact or not artifact["exists_on_disk"]:
        return PlainTextResponse("File not found", status_code=404)

    file_path = artifact["file_path"]
    return FileResponse(file_path)


@app.get("/artifacts/{artifact_id}/preview")
async def artifact_preview(artifact_id: int):
    """Full-page preview of an artifact (opens in new tab)."""
    from .data.artifact_store import get_artifact_detail
    from fastapi.responses import FileResponse

    artifact = get_artifact_detail(artifact_id)
    if not artifact or not artifact["exists_on_disk"]:
        return PlainTextResponse("File not found", status_code=404)

    return FileResponse(artifact["file_path"])


@app.post("/api/artifacts/build")
async def build_artifact_index():
    """Scan all sessions and build the artifact index."""
    from .data.artifact_store import ingest_all_sessions
    result = ingest_all_sessions()
    return JSONResponse({"ok": True, **result})


@app.post("/api/artifacts/open")
async def open_artifact_in_finder(request: Request):
    """Reveal an artifact in Finder (macOS)."""
    import subprocess
    body = await request.json()
    path = body.get("path", "")
    if path and Path(path).exists():
        subprocess.Popen(["open", "-R", path])
        return JSONResponse({"ok": True})
    return JSONResponse({"ok": False, "error": "File not found"}, status_code=404)


# ===== Analytics Page =====


@app.get("/analytics", response_class=HTMLResponse)
async def analytics_page(request: Request, days: int = 30):
    """Skill and tool performance analytics."""
    from .services.skill_analytics import get_skill_stats
    from .services.tool_analytics import get_tool_stats

    all_sessions = _get_all_sessions_unified(limit=500)
    live_sessions = parser.get_all_sessions()

    skill_stats = get_skill_stats(live_sessions, parser, days=days)
    tool_stats = get_tool_stats(live_sessions, parser, days=days)

    return templates.TemplateResponse(
        "analytics.html",
        {
            "request": request,
            "skill_stats": skill_stats,
            "tool_stats": tool_stats,
            "days": days,
        },
    )


# ===== Continuity Page =====


@app.get("/continuity", response_class=HTMLResponse)
async def continuity_page(request: Request):
    """Where Was I? — work threads, blocked items, resumption."""
    from .services.continuity import get_continuity_summary

    sessions = _get_all_sessions_unified(limit=100)
    live_sessions = parser.get_all_sessions()
    summary = get_continuity_summary(live_sessions, parser)

    return templates.TemplateResponse(
        "continuity.html",
        {"request": request, "summary": summary},
    )


# ===== Decisions Page =====


@app.get("/decisions", response_class=HTMLResponse)
async def decisions_page(request: Request):
    """Decision log — extracted decisions across all sessions."""
    from .data.decisions import get_all_decisions, get_decision_stats, extract_and_store

    # Extract decisions from recent sessions that haven't been processed
    live_sessions = parser.get_all_sessions()
    for s in live_sessions[:30]:
        try:
            extract_and_store(s.session_id, parser)
        except Exception:
            continue

    decisions = get_all_decisions(limit=200)
    stats = get_decision_stats()

    return templates.TemplateResponse(
        "decisions.html",
        {"request": request, "decisions": decisions, "stats": stats},
    )


@app.post("/api/decisions/{decision_id}/status")
async def update_decision_status(decision_id: int, request: Request):
    """Update a decision's status."""
    from .data.decisions import update_status
    body = await request.json()
    new_status = body.get("status", "")
    ok = update_status(decision_id, new_status)
    return JSONResponse({"ok": ok})


# ===== Memory Page =====


@app.get("/memory", response_class=HTMLResponse)
async def memory_page(request: Request):
    """Memory explorer — browse all Claude Code memories."""
    from .data.memory_reader import scan_all_memories, get_memory_stats
    from collections import defaultdict

    memories = scan_all_memories()
    stats = get_memory_stats()

    memories_by_project = defaultdict(list)
    for m in memories:
        memories_by_project[m["project"]].append(m)

    return templates.TemplateResponse(
        "memory.html",
        {
            "request": request,
            "memories_by_project": dict(memories_by_project),
            "stats": stats,
        },
    )


# ===== Prompts Page =====


@app.get("/prompts", response_class=HTMLResponse)
async def prompts_page(request: Request):
    """Prompt library — saved reusable prompts."""
    from .data.prompt_library import get_all_prompts, get_categories

    prompts = get_all_prompts()
    categories = get_categories()

    return templates.TemplateResponse(
        "prompts.html",
        {"request": request, "prompts": prompts, "categories": categories},
    )


@app.post("/api/prompts")
async def save_prompt_api(request: Request):
    """Save a prompt to the library."""
    from .data.prompt_library import save_prompt
    body = await request.json()
    result = save_prompt(
        content=body.get("content", ""),
        source_session=body.get("source_session", ""),
        source_uuid=body.get("source_uuid", ""),
        title=body.get("title", ""),
        tags=body.get("tags", []),
        category=body.get("category", ""),
    )
    return JSONResponse({"ok": True, **result})


@app.delete("/api/prompts/{prompt_id}")
async def delete_prompt_api(prompt_id: str):
    """Delete a prompt from the library."""
    from .data.prompt_library import delete_prompt
    ok = delete_prompt(prompt_id)
    return JSONResponse({"ok": ok})


@app.post("/api/prompts/{prompt_id}/use")
async def use_prompt_api(prompt_id: str):
    """Increment usage counter for a prompt."""
    from .data.prompt_library import increment_usage
    increment_usage(prompt_id)
    return JSONResponse({"ok": True})


# ===== Outcome Rating API =====


@app.post("/api/sessions/{session_id}/rate")
async def rate_session_api(session_id: str, request: Request):
    """Rate a session outcome."""
    from .data.outcomes import rate_session
    body = await request.json()
    result = rate_session(
        session_id=session_id,
        rating=body.get("rating", 3),
        tags=body.get("tags", []),
        note=body.get("note", ""),
    )
    return JSONResponse({"ok": True, **result})


@app.get("/api/sessions/{session_id}/outcome")
async def get_session_outcome(session_id: str):
    """Get outcome rating for a session."""
    from .data.outcomes import get_outcome
    outcome = get_outcome(session_id)
    if not outcome:
        return JSONResponse({"rated": False})
    return JSONResponse({"rated": True, **outcome})


# ===== Marginalia (Journal + Reflection) =====


@app.get("/journal", response_class=HTMLResponse)
async def journal_page(request: Request, date: str = "", view: str = "journal"):
    """Inner Life — Claude's journal, memory orbs, islands, and autoDream consolidation."""
    from .data.journal import (
        get_journal_entries, get_journal_entry, get_memory_orbs,
        get_islands, get_journal_stats, get_consolidations,
    )

    stats = get_journal_stats()
    entries = get_journal_entries(limit=30)
    islands = get_islands(include_fading=True)
    all_orbs = get_memory_orbs(limit=100)
    core_orbs = get_memory_orbs(limit=50, core_only=True)

    # Selected entry
    selected_entry = None
    selected_orbs = []
    if date:
        selected_entry = get_journal_entry(date)
        selected_orbs = get_memory_orbs(date=date)
    elif entries:
        selected_entry = entries[0]
        selected_orbs = get_memory_orbs(date=entries[0]["date"])

    # Consolidation data for autoDream view
    consolidation = None
    if view == "autodream":
        consolidations = get_consolidations(limit=1)
        if consolidations:
            consolidation = consolidations[0]

    template = "journal.html" if view == "journal" else "journal_autodream.html"

    return templates.TemplateResponse(
        template,
        {
            "request": request,
            "view": view,
            "stats": stats,
            "entries": entries,
            "islands": islands,
            "all_orbs": all_orbs,
            "core_orbs": core_orbs,
            "selected_entry": selected_entry,
            "selected_orbs": selected_orbs,
            "selected_date": date or (entries[0]["date"] if entries else ""),
            "consolidation": consolidation,
        },
    )


@app.get("/api/journal/entries")
async def api_journal_entries(limit: int = 30, offset: int = 0):
    """Get journal entries."""
    from .data.journal import get_journal_entries
    entries = get_journal_entries(limit=limit, offset=offset)
    return JSONResponse({"entries": entries, "total": len(entries)})


@app.get("/api/journal/entry/{date}")
async def api_journal_entry(date: str):
    """Get a single journal entry by date."""
    from .data.journal import get_journal_entry, get_memory_orbs
    entry = get_journal_entry(date)
    if not entry:
        return JSONResponse({"error": "No entry for this date"}, status_code=404)
    orbs = get_memory_orbs(date=date)
    return JSONResponse({"entry": entry, "orbs": orbs})


@app.get("/api/journal/orbs")
async def api_journal_orbs(core_only: bool = False, limit: int = 50):
    """Get memory orbs."""
    from .data.journal import get_memory_orbs
    orbs = get_memory_orbs(limit=limit, core_only=core_only)
    return JSONResponse({"orbs": orbs, "total": len(orbs)})


@app.get("/api/journal/islands")
async def api_journal_islands():
    """Get all islands."""
    from .data.journal import get_islands
    islands = get_islands(include_fading=True)
    return JSONResponse({"islands": islands})


@app.get("/api/journal/stats")
async def api_journal_stats():
    """Get journal statistics."""
    from .data.journal import get_journal_stats
    return JSONResponse(get_journal_stats())


@app.post("/api/journal/generate")
async def api_journal_generate(request: Request):
    """Manually trigger journal generation for a date (non-streaming fallback)."""
    from .services.journal_writer import generate_journal_entry
    body = await request.json()
    date = body.get("date", "")
    if not date:
        from datetime import datetime, timezone
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        result = generate_journal_entry(date, parser)
        if result:
            return JSONResponse({"ok": True, "date": date})
        return JSONResponse({"ok": False, "error": "No sessions found for this date"})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/api/journal/generate-stream")
async def api_journal_generate_stream(date: str = ""):
    """SSE endpoint — streams progress events during journal generation."""
    from .services.journal_writer import generate_journal_entry
    from starlette.responses import StreamingResponse
    import asyncio
    import queue
    import threading

    if not date:
        date = datetime.now().strftime("%Y-%m-%d")

    progress_queue = queue.Queue()

    def on_progress(step, detail):
        progress_queue.put({"step": step, "detail": detail})

    def _run():
        try:
            generate_journal_entry(date, parser, on_progress=on_progress)
        except Exception as e:
            progress_queue.put({"step": "error", "detail": str(e)})

    async def event_stream():
        thread = threading.Thread(target=_run, daemon=True)
        thread.start()

        finished = False
        while not finished:
            # Drain all available events
            events_sent = False
            while True:
                try:
                    event = progress_queue.get_nowait()
                    yield f"data: {json.dumps(event)}\n\n"
                    events_sent = True
                    if event["step"] in ("done", "error"):
                        finished = True
                        break
                except queue.Empty:
                    break

            if not finished:
                if not thread.is_alive():
                    # Thread died without sending done/error
                    yield f"data: {json.dumps({'step': 'error', 'detail': 'Generation process ended unexpectedly'})}\n\n"
                    break
                # Keepalive every 2s during the long reflection step
                yield f": keepalive\n\n"
                await asyncio.sleep(2)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.post("/api/journal/consolidate")
async def api_journal_consolidate(request: Request):
    """Manually trigger autoDream consolidation."""
    from .services.journal_writer import run_consolidation
    body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    days = body.get("days", 30)
    try:
        result = run_consolidation(days=days)
        if result:
            return JSONResponse({"ok": True})
        return JSONResponse({"ok": False, "error": "Not enough entries to consolidate (need at least 3)"})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/journal/weekly-review")
async def api_weekly_review():
    """Manually trigger the weekly review."""
    from .services.journal_writer import run_weekly_review
    try:
        result = run_weekly_review(parser)
        return JSONResponse({"ok": True, "result": result})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "claude_sessions.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
    )

"""MCP server exposing Otter.ai recordings, summaries and transcripts.

Wraps the otterai package so MCP clients (Claude Code, Claude Desktop, ...)
can browse recordings, read their summaries, pull full transcripts, search
within a recording and export recordings to files.

Authentication is resolved once, lazily, on the first tool call:

1. A saved session file (OTTERAI_SESSION_FILE, default ~/.otterai/session.json)
   is loaded and validated first. Reusing a session avoids the repeated
   logins that can trip Otter's rate limiting / captcha.
2. Otherwise OTTERAI_USERNAME / OTTERAI_PASSWORD are used to log in, and the
   fresh session is saved back to the session file for later runs.

Both variables can also come from a .env file (python-dotenv).
"""

import functools
import json
import os
import threading
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from dotenv import load_dotenv

try:  # mcp SDK >= 2.0
    from mcp.server import MCPServer as _ServerClass
except ImportError:
    try:  # mcp SDK 1.x
        from mcp.server.fastmcp import FastMCP as _ServerClass
    except ImportError as exc:
        raise ImportError(
            "The 'mcp' package is required for the MCP connector. "
            "Install it with: pip install 'otterai-api[mcp]' "
            "(or: uv pip install '.[mcp]')"
        ) from exc
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from otterai.otterai import OtterAI, OtterAIException

load_dotenv()

mcp = _ServerClass("otterai_mcp")

DEFAULT_SESSION_FILE = os.path.join("~", ".otterai", "session.json")
SUMMARY_SNIPPET_CHARS = 200

_client: Optional[OtterAI] = None
_client_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Client / auth management
# ---------------------------------------------------------------------------


def _session_file() -> str:
    return os.path.expanduser(os.getenv("OTTERAI_SESSION_FILE", DEFAULT_SESSION_FILE))


def get_client() -> OtterAI:
    """Return an authenticated OtterAI client, creating it on first use."""
    global _client
    with _client_lock:
        if _client is not None:
            return _client

        otter = OtterAI()
        session_file = _session_file()
        if otter.load_session(session_file) and otter.is_session_valid():
            _client = otter
            return _client

        username = os.getenv("OTTERAI_USERNAME")
        password = os.getenv("OTTERAI_PASSWORD")
        if not username or not password:
            raise OtterAIException(
                "Not authenticated: no valid saved session was found at "
                f"'{session_file}' and OTTERAI_USERNAME / OTTERAI_PASSWORD are "
                "not set. Set both environment variables (a .env file works "
                "too), or point OTTERAI_SESSION_FILE at a session saved with "
                "OtterAI.save_session()."
            )

        response = otter.login(username, password)
        if response.get("status") != 200:
            raise OtterAIException(
                f"Login to Otter.ai failed with status {response.get('status')}. "
                "Check OTTERAI_USERNAME / OTTERAI_PASSWORD. Repeated failures "
                "may mean Otter is rate limiting logins; wait before retrying."
            )

        _persist_session(otter, session_file)

        _client = otter
        return _client


def _persist_session(otter: OtterAI, session_file: str) -> None:
    """Best-effort save of the login session for reuse by later runs."""
    try:
        session_dir = os.path.dirname(session_file)
        if session_dir:
            os.makedirs(session_dir, exist_ok=True)
        otter.save_session(session_file)
    except OSError:
        pass


def _reset_client() -> None:
    """Drop the cached client (used by tests and after auth failures)."""
    global _client
    with _client_lock:
        _client = None


def _unwrap(response: dict, context: str) -> dict:
    """Return response['data'] or raise with an actionable message."""
    status = response.get("status")
    if status != 200:
        if status in (401, 403):
            _reset_client()
            raise OtterAIException(
                f"Otter.ai returned status {status} while {context}. The "
                "session has likely expired; it was discarded and the next "
                "call will re-authenticate."
            )
        if status == 404:
            raise OtterAIException(
                f"Otter.ai returned 404 while {context}. Check that the "
                "speech_id (otid) is correct — it comes from "
                "otterai_list_recordings."
            )
        if status == 429:
            raise OtterAIException(
                f"Otter.ai rate limited the request while {context}. Wait "
                "before retrying."
            )
        raise OtterAIException(f"Otter.ai returned status {status} while {context}.")
    return response.get("data") or {}


def _tool(fn):
    """Convert exceptions into 'Error: ...' strings for the model."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs) -> str:
        try:
            return fn(*args, **kwargs)
        except OtterAIException as e:
            return f"Error: {e}"
        except Exception as e:  # noqa: BLE001 - never leak a traceback to the client
            return f"Error: unexpected {type(e).__name__}: {e}"

    return wrapper


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _format_timestamp(epoch: Any) -> str:
    try:
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M UTC"
        )
    except (TypeError, ValueError, OSError, OverflowError):
        return "unknown"


def _format_duration(seconds: Any) -> str:
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return "unknown"
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def _format_offset(milliseconds: Any) -> str:
    try:
        total_seconds = int(milliseconds) // 1000
    except (TypeError, ValueError):
        return "?"
    minutes, secs = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _speech_title(speech: dict) -> str:
    return speech.get("title") or "Untitled recording"


def _speech_summary(speech: dict) -> str:
    """Best-available summary text from a speech object."""
    abstract = speech.get("abstract_summary")
    if isinstance(abstract, dict):
        parts = []
        short = abstract.get("short_summary") or abstract.get("summary")
        if short:
            parts.append(str(short))
        items = abstract.get("items") or abstract.get("key_points") or []
        if isinstance(items, list) and items:
            parts.extend(f"- {item}" for item in items if item)
        if parts:
            return "\n".join(parts)
    elif isinstance(abstract, str) and abstract.strip():
        return abstract.strip()
    summary = speech.get("summary")
    if isinstance(summary, str) and summary.strip():
        return summary.strip()
    return ""


def _speech_brief(speech: dict) -> dict:
    """Condensed view of a speech object for list responses."""
    owner = speech.get("owner") or {}
    summary = _speech_summary(speech)
    return {
        "speech_id": speech.get("otid") or speech.get("speech_id"),
        "title": _speech_title(speech),
        "created_at": _format_timestamp(speech.get("created_at")),
        "duration": _format_duration(speech.get("duration")),
        "owner": owner.get("name") or owner.get("email") or "unknown",
        "summary": summary,
    }


def _speaker_names(speech: dict) -> dict:
    """Map speaker id -> display name from a speech object."""
    names = {}
    for entry in speech.get("speakers") or []:
        if not isinstance(entry, dict):
            continue
        nested = entry.get("speaker") if isinstance(entry.get("speaker"), dict) else {}
        speaker_id = entry.get("id", nested.get("id"))
        name = entry.get("speaker_name") or nested.get("speaker_name")
        if speaker_id is not None and name:
            names[speaker_id] = name
    return names


def _fetch_speech(client: OtterAI, speech_id: str) -> dict:
    data = _unwrap(client.get_speech(speech_id), f"fetching recording {speech_id}")
    return data.get("speech") or {}


def _json_dumps(payload: Any) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# Input models
# ---------------------------------------------------------------------------


class ResponseFormat(str, Enum):
    """Output format for tool responses."""

    MARKDOWN = "markdown"
    JSON = "json"


class GetUserInput(BaseModel):
    """Input model for otterai_get_current_user."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="'markdown' for human-readable output, 'json' for the raw user object",
    )


class ListRecordingsInput(BaseModel):
    """Input model for otterai_list_recordings."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    folder: int = Field(
        default=0,
        description="Folder id to list from (0 = all recordings; ids come from otterai_list_folders)",
        ge=0,
    )
    page_size: int = Field(
        default=20, description="Maximum recordings to return", ge=1, le=100
    )
    source: str = Field(
        default="owned",
        description="Which recordings to list: 'owned' (default) or 'shared'",
        pattern="^(owned|shared)$",
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="'markdown' for a readable list, 'json' for structured data",
    )


class GetRecordingInput(BaseModel):
    """Input model for otterai_get_recording and otterai_get_summary."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    speech_id: str = Field(
        ...,
        description="Recording id (the 'otid' / speech_id from otterai_list_recordings)",
        min_length=1,
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="'markdown' for readable output, 'json' for the raw speech object",
    )


class GetTranscriptInput(BaseModel):
    """Input model for otterai_get_transcript."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    speech_id: str = Field(
        ...,
        description="Recording id (the 'otid' / speech_id from otterai_list_recordings)",
        min_length=1,
    )
    include_timestamps: bool = Field(
        default=True,
        description="Prefix each segment with its approximate [h:mm:ss] offset",
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="'markdown' for a readable transcript, 'json' for raw segments",
    )


class SearchRecordingInput(BaseModel):
    """Input model for otterai_search_recording."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    query: str = Field(..., description="Text to search for", min_length=1)
    speech_id: str = Field(
        ...,
        description="Recording id (otid) to search within",
        min_length=1,
    )
    size: int = Field(default=50, description="Maximum hits to return", ge=1, le=500)


class ListFoldersInput(BaseModel):
    """Input model for otterai_list_folders."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="'markdown' for a readable list, 'json' for raw folder objects",
    )


class ExportFormat(str, Enum):
    """File formats Otter's bulk export supports."""

    TXT = "txt"
    PDF = "pdf"
    MP3 = "mp3"
    DOCX = "docx"
    SRT = "srt"
    ALL = "all"  # every format, delivered as a zip


class ExportRecordingInput(BaseModel):
    """Input model for otterai_export_recording."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    speech_id: str = Field(
        ...,
        description="Recording id (otid) to export",
        min_length=1,
    )
    file_format: ExportFormat = Field(
        default=ExportFormat.TXT,
        description="Export format; 'all' downloads txt+pdf+mp3+docx+srt as a zip",
    )
    output_dir: str = Field(
        default=".",
        description="Directory to save the exported file into (created if missing)",
    )


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def _annotations(title: str, read_only: bool = True) -> ToolAnnotations:
    # The only non-read-only tool (export) writes to a predictable local
    # path and overwrites an existing file there, so it is flagged
    # destructive to let clients apply their confirmation flow.
    return ToolAnnotations(
        title=title,
        readOnlyHint=read_only,
        destructiveHint=not read_only,
        idempotentHint=True,
        openWorldHint=True,
    )


@mcp.tool(
    name="otterai_get_current_user",
    annotations=_annotations("Get Otter.ai Account Info"),
)
@_tool
def otterai_get_current_user(params: GetUserInput) -> str:
    """Show the authenticated Otter.ai account (name, email, plan).

    Useful as a connectivity/auth check before other calls.

    Args:
        params (GetUserInput): response_format ('markdown' or 'json').

    Returns:
        str: Account details, or "Error: ..." when authentication fails.
    """
    client = get_client()
    data = _unwrap(client.get_user(), "fetching account info")
    user = data.get("user") or data
    if params.response_format == ResponseFormat.JSON:
        return _json_dumps(user)
    lines = ["# Otter.ai account", ""]
    for label, key in (
        ("Name", "name"),
        ("Email", "email"),
        ("User id", "id"),
        ("Plan", "plan_type"),
    ):
        value = user.get(key)
        if value:
            lines.append(f"- **{label}**: {value}")
    return "\n".join(lines) if len(lines) > 2 else _json_dumps(user)


@mcp.tool(
    name="otterai_list_recordings",
    annotations=_annotations("List Otter.ai Recordings"),
)
@_tool
def otterai_list_recordings(params: ListRecordingsInput) -> str:
    """List recordings (speeches) in the Otter.ai account, newest first.

    Each entry includes the speech_id needed by otterai_get_recording,
    otterai_get_summary, otterai_get_transcript, otterai_search_recording
    and otterai_export_recording, plus a snippet of the recording's summary.

    Args:
        params (ListRecordingsInput): folder (0 = all), page_size (1-100),
            source ('owned' or 'shared'), response_format.

    Returns:
        str: Markdown list or JSON with:
        {
            "count": int,
            "end_of_list": bool | null,   # false means more can be fetched
            "recordings": [
                {"speech_id", "title", "created_at", "duration", "owner", "summary"}
            ]
        }
    """
    client = get_client()
    data = _unwrap(
        client.get_speeches(
            folder=params.folder, page_size=params.page_size, source=params.source
        ),
        "listing recordings",
    )
    speeches = data.get("speeches") or []
    briefs = [_speech_brief(s) for s in speeches if isinstance(s, dict)]

    if params.response_format == ResponseFormat.JSON:
        return _json_dumps(
            {
                "count": len(briefs),
                "end_of_list": data.get("end_of_list"),
                "recordings": briefs,
            }
        )

    if not briefs:
        return "No recordings found."
    lines = [f"# Otter.ai recordings ({len(briefs)})", ""]
    for brief in briefs:
        lines.append(f"## {brief['title']}")
        lines.append(f"- **Id**: `{brief['speech_id']}`")
        lines.append(f"- **Recorded**: {brief['created_at']}")
        lines.append(f"- **Duration**: {brief['duration']}")
        lines.append(f"- **Owner**: {brief['owner']}")
        if brief["summary"]:
            snippet = brief["summary"].replace("\n", " ")
            if len(snippet) > SUMMARY_SNIPPET_CHARS:
                snippet = snippet[:SUMMARY_SNIPPET_CHARS] + "…"
            lines.append(f"- **Summary**: {snippet}")
        lines.append("")
    if data.get("end_of_list") is False:
        lines.append("_More recordings available — raise page_size to fetch more._")
    return "\n".join(lines).rstrip()


@mcp.tool(
    name="otterai_get_recording",
    annotations=_annotations("Get Otter.ai Recording Details"),
)
@_tool
def otterai_get_recording(params: GetRecordingInput) -> str:
    """Get full details of one recording: metadata, speakers and summary.

    Does NOT include the transcript — use otterai_get_transcript for that.

    Args:
        params (GetRecordingInput): speech_id (otid), response_format.

    Returns:
        str: Markdown details, or the raw speech JSON object when
        response_format='json'. "Error: ..." on failure.
    """
    client = get_client()
    speech = _fetch_speech(client, params.speech_id)
    if params.response_format == ResponseFormat.JSON:
        return _json_dumps(speech)

    brief = _speech_brief(speech)
    lines = [f"# {brief['title']}", ""]
    lines.append(f"- **Id**: `{brief['speech_id']}`")
    lines.append(f"- **Recorded**: {brief['created_at']}")
    lines.append(f"- **Duration**: {brief['duration']}")
    lines.append(f"- **Owner**: {brief['owner']}")
    speakers = sorted(_speaker_names(speech).values())
    if speakers:
        lines.append(f"- **Speakers**: {', '.join(speakers)}")
    if speech.get("process_finished") is False:
        lines.append("- **Note**: Otter is still processing this recording")
    lines.append("")
    if brief["summary"]:
        lines.append("## Summary")
        lines.append(brief["summary"])
    else:
        lines.append("_No summary available for this recording._")
    return "\n".join(lines)


@mcp.tool(
    name="otterai_get_summary",
    annotations=_annotations("Get Otter.ai Recording Summary"),
)
@_tool
def otterai_get_summary(params: GetRecordingInput) -> str:
    """Get just the summary of a recording.

    Lighter than otterai_get_recording when only the summary matters.

    Args:
        params (GetRecordingInput): speech_id (otid), response_format.

    Returns:
        str: The summary as markdown, or JSON:
        {"speech_id": str, "title": str, "summary": str}
    """
    client = get_client()
    speech = _fetch_speech(client, params.speech_id)
    summary = _speech_summary(speech)
    if params.response_format == ResponseFormat.JSON:
        return _json_dumps(
            {
                "speech_id": params.speech_id,
                "title": _speech_title(speech),
                "summary": summary,
            }
        )
    if not summary:
        return (
            f"No summary available for '{_speech_title(speech)}'. Otter may "
            "still be processing it, or summaries may not be enabled for "
            "this recording — otterai_get_transcript returns the full text."
        )
    return f"# Summary: {_speech_title(speech)}\n\n{summary}"


@mcp.tool(
    name="otterai_get_transcript",
    annotations=_annotations("Get Otter.ai Transcript"),
)
@_tool
def otterai_get_transcript(params: GetTranscriptInput) -> str:
    """Get the full transcript of a recording, segment by segment.

    Speaker ids are resolved to display names where the recording has
    labeled speakers. Offsets are approximate and derived from segment
    start offsets.

    Args:
        params (GetTranscriptInput): speech_id (otid), include_timestamps,
            response_format.

    Returns:
        str: Markdown transcript ("**Speaker** [m:ss]: text" per segment),
        or JSON {"speech_id", "title", "segments": [{"speaker", "start_offset",
        "end_offset", "text"}]}.
    """
    client = get_client()
    data = _unwrap(
        client.get_speech_transcript(params.speech_id),
        f"fetching transcript for {params.speech_id}",
    )
    segments = data.get("transcripts") or []

    title = params.speech_id
    names = {}
    try:
        speech = _fetch_speech(client, params.speech_id)
        title = _speech_title(speech)
        names = _speaker_names(speech)
    except OtterAIException:
        pass  # transcript is still useful without speaker names

    def speaker_label(segment: dict) -> str:
        speaker_id = segment.get("speaker_id")
        return (
            names.get(speaker_id)
            or segment.get("speaker_model_label")
            or (f"Speaker {speaker_id}" if speaker_id is not None else "Speaker")
        )

    if params.response_format == ResponseFormat.JSON:
        return _json_dumps(
            {
                "speech_id": params.speech_id,
                "title": title,
                "segments": [
                    {
                        "speaker": speaker_label(s),
                        "start_offset": s.get("start_offset"),
                        "end_offset": s.get("end_offset"),
                        "text": s.get("transcript", ""),
                    }
                    for s in segments
                    if isinstance(s, dict)
                ],
            }
        )

    if not segments:
        return (
            f"No transcript segments found for '{title}'. Otter may still be "
            "processing this recording."
        )
    lines = [f"# Transcript: {title}", ""]
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        text = (segment.get("transcript") or "").strip()
        if not text:
            continue
        prefix = f"**{speaker_label(segment)}**"
        if params.include_timestamps:
            prefix += f" [{_format_offset(segment.get('start_offset'))}]"
        lines.append(f"{prefix}: {text}")
        lines.append("")
    return "\n".join(lines).rstrip()


@mcp.tool(
    name="otterai_search_recording",
    annotations=_annotations("Search Within an Otter.ai Recording"),
)
@_tool
def otterai_search_recording(params: SearchRecordingInput) -> str:
    """Search for text within one recording's transcript.

    Faster than pulling the whole transcript when looking for specific
    words or topics in a long recording.

    Args:
        params (SearchRecordingInput): query, speech_id (otid), size.

    Returns:
        str: JSON with the raw search hits from Otter:
        {"query": str, "speech_id": str, "hit_count": int, "hits": [...]}
        or "No matches..." when nothing was found.
    """
    client = get_client()
    data = _unwrap(
        client.query_speech(params.query, params.speech_id, size=params.size),
        f"searching in {params.speech_id}",
    )
    hits = data.get("hits") or []
    if not hits:
        return f"No matches for '{params.query}' in recording {params.speech_id}."
    return _json_dumps(
        {
            "query": params.query,
            "speech_id": params.speech_id,
            "hit_count": len(hits),
            "hits": hits,
        }
    )


@mcp.tool(
    name="otterai_list_folders",
    annotations=_annotations("List Otter.ai Folders"),
)
@_tool
def otterai_list_folders(params: ListFoldersInput) -> str:
    """List folders in the Otter.ai account.

    Folder ids can be passed to otterai_list_recordings to list only the
    recordings filed in that folder.

    Args:
        params (ListFoldersInput): response_format.

    Returns:
        str: Markdown list "name (id ...)" or the raw folders JSON.
    """
    client = get_client()
    data = _unwrap(client.get_folders(), "listing folders")
    folders = data.get("folders") or []
    if params.response_format == ResponseFormat.JSON:
        return _json_dumps(folders)
    if not folders:
        return "No folders found."
    lines = [f"# Otter.ai folders ({len(folders)})", ""]
    for folder in folders:
        if not isinstance(folder, dict):
            continue
        name = folder.get("folder_name") or folder.get("name") or "Unnamed"
        entry = f"- **{name}** (id {folder.get('id')})"
        if folder.get("speech_count") is not None:
            entry += f" — {folder['speech_count']} recordings"
        lines.append(entry)
    return "\n".join(lines)


@mcp.tool(
    name="otterai_export_recording",
    annotations=_annotations("Export an Otter.ai Recording to a File", read_only=False),
)
@_tool
def otterai_export_recording(params: ExportRecordingInput) -> str:
    """Download a recording export (transcript and/or audio) to a local file.

    Writes '<output_dir>/<speech_id>.<format>' ('.zip' for format 'all').

    Args:
        params (ExportRecordingInput): speech_id (otid), file_format
            (txt/pdf/mp3/docx/srt/all), output_dir.

    Returns:
        str: "Saved export to <path>" or "Error: ...".
    """
    client = get_client()
    output_dir = os.path.expanduser(params.output_dir)
    os.makedirs(output_dir, exist_ok=True)
    if params.file_format == ExportFormat.ALL:
        fileformat = "txt,pdf,mp3,docx,srt"
    else:
        fileformat = params.file_format.value
    # download_speech appends ".<format>" (or ".zip") to `name`
    name = os.path.join(output_dir, str(params.speech_id))
    response = client.download_speech(
        params.speech_id, name=name, fileformat=fileformat
    )
    filename = (response.get("data") or {}).get("filename")
    return f"Saved export to {filename}"


def main() -> None:
    """Run the MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()

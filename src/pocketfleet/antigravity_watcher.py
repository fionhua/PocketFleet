"""Antigravity Continuous Session & Transcript Watcher.

Extracted, hardened, and adapted from ccgram's antigravity provider architecture.
Provides zero-dependency, production-grade:
1. Workspace-to-conversation discovery (via official CLI cache & transcript prefix matching).
2. Incremental transcript tracking & offset-based change polling.
3. Accurate dialogue content cleaning (XML scaffolding / metadata stripping).
4. Tool invocation & tool result event streaming.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import time
from typing import Any, Iterator, Optional
from urllib.parse import unquote, urlparse

# Standard UUID regular expression
UUID_REGEX = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

# Known XML metadata wrapper blocks in Antigravity transcripts
METADATA_BLOCKS_REGEX = re.compile(
    r"<(ADDITIONAL_METADATA|USER_SETTINGS_CHANGE|USER_INFORMATION|USER_RULES|SKILLS|PLUGINS|SUBAGENTS|MESSAGING|CONVERSATION_TRANSCRIPT|ARTIFACTS|SLASH_COMMANDS|GUIDELINES|COMMUNICATION_STYLE)>[\s\S]*?</\1>",
    re.IGNORECASE,
)
TAG_WRAPPERS_REGEX = re.compile(r"</?USER_REQUEST>", re.IGNORECASE)

WORKSPACE_KEYS = frozenset({"cwd", "directorypath", "workspace", "directory"})
MAX_CWD_SCAN_LINES = 30


@dataclass(frozen=True)
class ResumableSession:
    """Represents a discovered resumable Antigravity session."""
    session_id: str
    cwd: str
    mtime: float
    transcript_path: Path
    summary: str = ""
    source: str = "cli"  # "cli" or "ide"


@dataclass
class AgentMessage:
    """Represents a parsed turn or tool event from transcript.jsonl."""
    role: str  # "user" or "assistant"
    text: str
    content_type: str = "text"  # "text", "tool_use", "tool_result"
    tool_name: Optional[str] = None
    tool_use_id: Optional[str] = None
    timestamp: Optional[str] = None
    step_index: Optional[int] = None


def clean_antigravity_content(text: str) -> str:
    """Clean XML metadata wrappers and system tags from Antigravity user input."""
    if not text:
        return ""
    # Extract explicit user request tag if present
    match = re.search(r"<USER_REQUEST>(.*?)</USER_REQUEST>", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    # Strip metadata scaffolding
    cleaned = METADATA_BLOCKS_REGEX.sub("", text)
    cleaned = TAG_WRAPPERS_REGEX.sub("", cleaned)
    # Collapse multiple whitespaces
    return " ".join(cleaned.split()).strip()


def resolve_antigravity_executable() -> str:
    """Resolve Antigravity CLI executable using deterministic precedence."""
    override = os.environ.get("ANTIGRAVITY_CLI_COMMAND", "").strip()
    if override:
        return override

    for name in ("agy", "agy.cmd", "antigravity", "antigravity.cmd"):
        which_path = shutil.which(name)
        if which_path:
            return which_path

    home = Path.home()
    candidates = (
        home / "AppData" / "Local" / "agy" / "bin" / "agy.exe",
        home / ".local" / "bin" / "agy",
        home / ".gemini" / "antigravity-cli" / "bin" / "agy",
    )
    for cand in candidates:
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)

    return "agy"


def get_default_brain_dirs() -> list[Path]:
    """Return ordered list of existing brain directories (both CLI and IDE)."""
    home = Path.home()
    dirs: list[Path] = []
    candidates = [
        home / ".gemini" / "antigravity-cli" / "brain",
        home / ".gemini" / "antigravity-ide" / "brain",
        home / ".antigravity" / "brain",
    ]
    for cand in candidates:
        try:
            resolved = cand.resolve()
            if resolved.is_dir() and resolved not in dirs:
                dirs.append(resolved)
        except (OSError, PermissionError):
            continue
    return dirs


def decode_workspace_path(value: str) -> Optional[str]:
    """Decode an absolute workspace path or file:// URI."""
    candidate = value.strip().strip("[]")
    if candidate.startswith("file://"):
        parsed = urlparse(candidate)
        if parsed.scheme == "file":
            candidate = unquote(parsed.path)
            # Fix leading slash on Windows (e.g. /d:/path -> d:/path)
            if re.match(r"^/[a-zA-Z]:", candidate):
                candidate = candidate[1:]
    if not candidate:
        return None
    try:
        return str(Path(candidate).resolve())
    except (OSError, ValueError):
        return None


def normalize_workspace_path(path: Path | str) -> str:
    """Normalize path with Windows case-insensitivity and symlink resolution."""
    try:
        resolved = Path(path).expanduser().resolve()
        return os.path.normcase(str(resolved))
    except Exception:
        return os.path.normcase(str(path).strip())


def extract_line_cwd_matches(line: str) -> list[str]:
    """Extract high-confidence workspace paths from JSONL entry (deep extraction)."""
    results: list[str] = []

    # 1. Regex search for <user_information> [...path...] -> Corpus
    m = re.search(r"\[([a-zA-Z]:\\[^\]]+|/[^\]]+)\]\s*->", line)
    if m:
        p = m.group(1).strip()
        if Path(p).is_dir():
            results.append(normalize_workspace_path(p))

    try:
        payload = json.loads(line)
    except Exception:
        return results

    if not isinstance(payload, dict):
        return results

    # 2. Check top-level keys
    for k, v in payload.items():
        if str(k).lower() in WORKSPACE_KEYS and isinstance(v, str):
            decoded = decode_workspace_path(v)
            if decoded:
                results.append(normalize_workspace_path(decoded))

    # 3. Check tool_calls args (e.g. Cwd, DirectoryPath, SearchPath)
    tool_calls = payload.get("tool_calls")
    if isinstance(tool_calls, list):
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            args = tc.get("args")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    pass
            if isinstance(args, dict):
                for ak, av in args.items():
                    if str(ak).lower() in ("cwd", "directorypath", "searchpath", "targetfile") and isinstance(av, str):
                        clean_av = av.strip(' "\'')
                        if Path(clean_av).is_dir():
                            results.append(normalize_workspace_path(clean_av))
                        elif Path(clean_av).is_file():
                            results.append(normalize_workspace_path(Path(clean_av).parent))

    return list(dict.fromkeys(results))


def read_transcript_cwd(log_file: Path, target_cwd: Optional[str] = None) -> Optional[str]:
    """Inspect first MAX_CWD_SCAN_LINES lines of transcript.jsonl to discover project CWD."""
    resolved_target = str(Path(target_cwd).resolve()) if target_cwd else None
    if not log_file.is_file():
        return None

    try:
        with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
            for idx, line in enumerate(f):
                if idx >= MAX_CWD_SCAN_LINES:
                    break
                for candidate_cwd in extract_line_cwd_matches(line):
                    if resolved_target:
                        if candidate_cwd.lower() == resolved_target.lower():
                            return candidate_cwd
                    elif Path(candidate_cwd).is_dir():
                        return candidate_cwd
    except (OSError, PermissionError):
        pass
    return None


def read_official_last_conversations_cache() -> dict[str, str]:
    """Read Google Antigravity official last_conversations.json cache with ambiguity detection."""
    cache_path = Path.home() / ".gemini" / "antigravity-cli" / "cache" / "last_conversations.json"
    if not cache_path.is_file():
        return {}

    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict):
            return {}
    except Exception:
        return {}

    mapping: dict[str, str] = {}
    ambiguous: set[str] = set()

    for raw_cwd, session_id in payload.items():
        if not isinstance(raw_cwd, str) or not isinstance(session_id, str):
            continue
        if not UUID_REGEX.match(session_id.strip()):
            continue

        norm_cwd = normalize_workspace_path(raw_cwd)

        if norm_cwd in ambiguous:
            continue

        prev = mapping.get(norm_cwd)
        if prev is not None and prev.lower() != session_id.strip().lower():
            mapping.pop(norm_cwd, None)
            ambiguous.add(norm_cwd)
            continue

        mapping[norm_cwd] = session_id.strip().lower()

    return mapping


class AntigravitySessionResolver:
    """Discovers and resolves sessions for workspaces and project paths."""

    def __init__(self, brain_dirs: Optional[list[Path]] = None):
        self.brain_dirs = brain_dirs or get_default_brain_dirs()

    def resolve_session_for_cwd(self, cwd: Path | str) -> Optional[ResumableSession]:
        """High-precision lookup: resolves latest conversation ID bound to a specific workspace CWD."""
        norm_target = normalize_workspace_path(cwd)

        # Step 1: Check official last_conversations.json cache first
        official_cache = read_official_last_conversations_cache()
        cached_id = official_cache.get(norm_target)
        if cached_id:
            for b_dir in self.brain_dirs:
                t_file = b_dir / cached_id / ".system_generated" / "logs" / "transcript.jsonl"
                if t_file.is_file():
                    return ResumableSession(
                        session_id=cached_id,
                        cwd=norm_target,
                        mtime=t_file.stat().st_mtime,
                        transcript_path=t_file,
                        summary=f"Cached: {cached_id[:8]}",
                        source="cli" if "antigravity-cli" in str(b_dir) else "ide",
                    )

        # Step 2: Scan brain directories for matching transcripts
        candidates: list[ResumableSession] = []
        for b_dir in self.brain_dirs:
            if not b_dir.is_dir():
                continue
            for conv_dir in b_dir.iterdir():
                if not conv_dir.is_dir() or not UUID_REGEX.match(conv_dir.name):
                    continue
                t_file = conv_dir / ".system_generated" / "logs" / "transcript.jsonl"
                if not t_file.is_file():
                    continue

                matched_cwd = read_transcript_cwd(t_file, target_cwd=norm_target)
                if matched_cwd:
                    candidates.append(
                        ResumableSession(
                            session_id=conv_dir.name.lower(),
                            cwd=matched_cwd,
                            mtime=t_file.stat().st_mtime,
                            transcript_path=t_file,
                            source="cli" if "antigravity-cli" in str(b_dir) else "ide",
                        )
                    )

        if not candidates:
            return None

        candidates.sort(key=lambda s: s.mtime, reverse=True)
        return candidates[0]

    def list_recent_sessions(self, limit: int = 15) -> list[ResumableSession]:
        """List most recent sessions across all brain directories."""
        seen: set[str] = set()
        results: list[ResumableSession] = []

        for b_dir in self.brain_dirs:
            if not b_dir.is_dir():
                continue
            for conv_dir in b_dir.iterdir():
                if not conv_dir.is_dir() or not UUID_REGEX.match(conv_dir.name):
                    continue
                cid = conv_dir.name.lower()
                if cid in seen:
                    continue
                t_file = conv_dir / ".system_generated" / "logs" / "transcript.jsonl"
                if not t_file.is_file():
                    continue

                seen.add(cid)
                mtime = t_file.stat().st_mtime
                cwd = read_transcript_cwd(t_file) or "Unknown Workspace"
                results.append(
                    ResumableSession(
                        session_id=cid,
                        cwd=cwd,
                        mtime=mtime,
                        transcript_path=t_file,
                        source="cli" if "antigravity-cli" in str(b_dir) else "ide",
                    )
                )

        results.sort(key=lambda s: s.mtime, reverse=True)
        return results[:limit]


class TranscriptTracker:
    """Incremental transcript reader and event stream emitter."""

    def __init__(self, transcript_path: Path | str):
        self.transcript_path = Path(transcript_path)
        self.last_offset: int = 0
        self.pending_tools: dict[str, str] = {}  # tool_id -> tool_name

        if self.transcript_path.is_file():
            # Initial offset points to current EOF if we want only new messages,
            # or 0 if reading history.
            self.last_offset = 0

    def read_all_history(self) -> list[AgentMessage]:
        """Read all historical turns from beginning of file."""
        self.last_offset = 0
        self.pending_tools.clear()
        return self.poll_new_messages()

    def poll_new_messages(self) -> list[AgentMessage]:
        """Poll for new JSONL lines appended since last offset."""
        if not self.transcript_path.is_file():
            return []

        try:
            curr_size = self.transcript_path.stat().st_size
            if curr_size <= self.last_offset:
                return []

            messages: list[AgentMessage] = []
            with open(self.transcript_path, "r", encoding="utf-8", errors="ignore") as f:
                f.seek(self.last_offset)
                for line in f:
                    l_str = line.strip()
                    if not l_str:
                        continue
                    try:
                        entry = json.loads(l_str)
                    except Exception:
                        continue

                    parsed_list = self._parse_entry(entry)
                    messages.extend(parsed_list)

                self.last_offset = f.tell()

            return messages
        except (OSError, PermissionError):
            return []

    def _parse_entry(self, entry: dict[str, Any]) -> list[AgentMessage]:
        """Parse raw JSONL entry into structured AgentMessage list."""
        results: list[AgentMessage] = []
        entry_type = str(entry.get("type", "")).upper()
        source = str(entry.get("source", "")).upper()
        timestamp = str(entry.get("created_at") or entry.get("timestamp") or "")
        step_index = entry.get("step_index")

        # 1. Tool Invocations
        tool_calls = entry.get("tool_calls")
        if isinstance(tool_calls, list) and (source == "MODEL" or entry_type == "PLANNER_RESPONSE"):
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                tid = str(tc.get("id") or tc.get("tool_call_id") or tc.get("name") or "unknown")
                tname = str(tc.get("name") or "unknown")
                self.pending_tools[tid] = tname
                results.append(
                    AgentMessage(
                        role="assistant",
                        text=f"🔧 [Tool Call] {tname}",
                        content_type="tool_use",
                        tool_name=tname,
                        tool_use_id=tid,
                        timestamp=timestamp,
                        step_index=step_index,
                    )
                )

        # 2. Tool Execution Results
        if entry_type in ("RUN_COMMAND", "TOOL_RESULT", "EXECUTE_RESULT", "RESULT"):
            tid = str(entry.get("tool_call_id") or entry.get("id") or "")
            tname = self.pending_tools.pop(tid, None) if tid else None
            if not tname:
                tname = str(entry.get("tool_name") or "tool")
            content = entry.get("content", "")
            res_str = str(content) if content else ""
            if res_str:
                results.append(
                    AgentMessage(
                        role="assistant",
                        text=res_str[:500] + ("..." if len(res_str) > 500 else ""),
                        content_type="tool_result",
                        tool_name=tname,
                        tool_use_id=tid or None,
                        timestamp=timestamp,
                        step_index=step_index,
                    )
                )
            return results

        # 3. User Input
        if entry_type in ("USER_INPUT", "USER_EXPLICIT", "USER_IMPLICIT") or source.startswith("USER"):
            raw_content = entry.get("content", "")
            cleaned = clean_antigravity_content(str(raw_content))
            if cleaned:
                results.append(
                    AgentMessage(
                        role="user",
                        text=cleaned,
                        content_type="text",
                        timestamp=timestamp,
                        step_index=step_index,
                    )
                )
            return results

        # 4. Planner / Assistant Response
        if entry_type == "PLANNER_RESPONSE" or source == "MODEL":
            raw_content = entry.get("content", "")
            cleaned = str(raw_content).strip()
            if cleaned:
                results.append(
                    AgentMessage(
                        role="assistant",
                        text=cleaned,
                        content_type="text",
                        timestamp=timestamp,
                        step_index=step_index,
                    )
                )
            return results

        return results


def build_antigravity_launch_command(
    session_id: Optional[str] = None,
    use_continue: bool = False,
    cli_bin: Optional[str] = None,
) -> list[str]:
    """Construct deterministic execution command array."""
    bin_path = cli_bin or resolve_antigravity_executable()
    cmd = [bin_path]
    if session_id:
        cmd.extend(["--conversation", session_id.strip().lower()])
    elif use_continue:
        cmd.append("--continue")
    return cmd


def safe_print(text: str) -> None:
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode("gbk", errors="replace").decode("gbk"))


if __name__ == "__main__":
    safe_print("=== Antigravity Watcher Diagnostic Test ===")
    resolver = AntigravitySessionResolver()

    safe_print("\n1. Resolving current workspace CWD...")
    cwd = Path.cwd()
    session = resolver.resolve_session_for_cwd(cwd)
    if session:
        safe_print(f"[OK] Found bound session for {cwd}:")
        safe_print(f"  Session ID : {session.session_id}")
        safe_print(f"  Source     : {session.source}")
        safe_print(f"  Transcript : {session.transcript_path}")
        safe_print(f"  Last active: {datetime.fromtimestamp(session.mtime)}")
    else:
        safe_print(f"[WARN] No session directly mapped for {cwd}")

    safe_print("\n2. Scanning recent sessions across all brain directories...")
    recent = resolver.list_recent_sessions(limit=5)
    for s in recent:
        safe_print(f"  - [{s.source.upper()}] {s.session_id[:12]}... (mtime: {datetime.fromtimestamp(s.mtime)}) in {s.cwd}")

    if session:
        safe_print("\n3. Testing incremental transcript tracker on bound session...")
        tracker = TranscriptTracker(session.transcript_path)
        history = tracker.read_all_history()
        safe_print(f"[OK] Total historical events parsed: {len(history)}")
        user_msgs = [m for m in history if m.role == "user"]
        if user_msgs:
            safe_print(f"  Latest user turn: \"{user_msgs[-1].text[:120]}...\"")
        asst_msgs = [m for m in history if m.role == "assistant" and m.content_type == "text"]
        if asst_msgs:
            safe_print(f"  Latest assistant response: \"{asst_msgs[-1].text[:120]}...\"")

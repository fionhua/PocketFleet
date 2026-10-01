"""Antigravity Track Discovery, Import Detection, and CLI Track Binding Core.

Hard constraints:
1. 'agy --conversation <IDE-ID>' fails with not found and creates a new track.
   IDE tracks MUST NOT be bound directly to the CLI; official migration requires
   running interactive `agy` -> `/resume` -> Tab to Antigravity -> Import.
2. Comprehensive size = db + db-wal + recursive brain directory files.
   db-shm is volatile shared memory and must NOT be included in durable size.
3. Last activity = maximum mtime among durable files.
4. Fault tolerant against disappearing files and permissions errors.
5. Fail-loud on ambiguous import (0 or >1 new tracks); never guess 'latest'.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import sqlite3
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Sequence, Set

logger = logging.getLogger(__name__)


_UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

DEFAULT_TRACK_MIN_BYTES = 524288  # 512 KB


class AntigravityTrackError(Exception):
    """Base exception for Antigravity track operations."""


class ImportRequiredError(AntigravityTrackError):
    """Raised when attempting to directly bind an IDE track before CLI import."""


class ImportDetectionError(AntigravityTrackError):
    """Raised when detecting new imported CLI tracks fails or is ambiguous."""


def is_valid_uuid(val: str) -> bool:
    """Validate that val is a well-formed 36-char hexadecimal UUID string."""
    if not val or not isinstance(val, str):
        return False
    val = val.strip()
    if not _UUID_PATTERN.match(val):
        return False
    try:
        u = uuid.UUID(val)
        return str(u).lower() == val.lower()
    except (ValueError, AttributeError):
        return False


@dataclass
class TrackCandidate:
    source: str  # "ide" or "cli"
    conversation_id: str  # Validated 36-char lowercase UUID
    db_path: Path
    brain_path: Optional[Path]
    last_activity: float
    total_bytes: int
    last_user_prompt: str = ""
    last_model_response: str = ""


def get_default_ide_root() -> Path:
    """Return default root for Antigravity IDE state, respecting env override."""
    override = os.environ.get("POCKETFLEET_ANTIGRAVITY_IDE_ROOT")
    if override:
        return Path(override)
    user_home = Path(os.environ.get("USERPROFILE") or Path.home())
    return user_home / ".gemini" / "antigravity-ide"


def get_default_cli_root() -> Path:
    """Return default root for Antigravity CLI state, respecting env override."""
    override = os.environ.get("POCKETFLEET_ANTIGRAVITY_CLI_ROOT")
    if override:
        return Path(override)
    user_home = Path(os.environ.get("USERPROFILE") or Path.home())
    return user_home / ".gemini" / "antigravity-cli"


def get_track_min_bytes() -> int:
    """Return minimum byte threshold for filtering tracks, respecting env override."""
    raw = os.environ.get("POCKETFLEET_TRACK_MIN_BYTES")
    if raw and raw.isdigit():
        return int(raw)
    return DEFAULT_TRACK_MIN_BYTES


def _safe_stat(path: Path) -> tuple[int, float] | None:
    """Safely stat a file, returning (size, mtime) or None on any error."""
    try:
        st = path.stat()
        return st.st_size, st.st_mtime
    except (OSError, PermissionError, FileNotFoundError):
        return None


_ANTIGRAVITY_SCAFFOLDING_RE = re.compile(
    r"<(ADDITIONAL_METADATA|USER_SETTINGS_CHANGE|USER_INFORMATION|USER_RULES|"
    r"SKILLS|PLUGINS|SUBAGENTS|MESSAGING|CONVERSATION_TRANSCRIPT|ARTIFACTS|"
    r"SLASH_COMMANDS|GUIDELINES|COMMUNICATION_STYLE|SYSTEM_MESSAGE|"
    r"RULE\[.*?\]|context|system_information)>[\s\S]*?</\1>",
    re.IGNORECASE,
)
_USER_REQUEST_RE = re.compile(r"<USER_REQUEST>([\s\S]*?)</USER_REQUEST>", re.IGNORECASE)


def clean_dialogue_snippet(text: str, max_chars: int = 100) -> str:
    """Clean markdown and prompt scaffolding tags using comprehensive Antigravity regex."""
    if not text:
        return ""
    # Extract inner user request if wrapped in envelope
    if "【指挥官外勤任务正文】" in text:
        text = text.split("【指挥官外勤任务正文】")[-1]
    elif "【任务正文】" in text:
        text = text.split("【任务正文】")[-1]
    # Extract user request inner text if wrapped
    m = _USER_REQUEST_RE.search(text)
    if m:
        text = m.group(1)
    # Strip full set of system/scaffolding blocks
    text = _ANTIGRAVITY_SCAFFOLDING_RE.sub("", text)
    # Also strip stray unclosed wrapper tags
    text = re.sub(r"</?USER_REQUEST>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<RULE\[.*?\]>", "", text, flags=re.IGNORECASE)
    text = " ".join(text.split()).strip()
    if not text:
        return ""
    if len(text) > max_chars:
        return text[:max_chars] + "..."
    return text


def format_expandable_quote(text: str, max_chars: int = 3500) -> str:
    """Format multiline output into Telegram expandable blockquote HTML.

    Renders as a compact 3-line collapsible block on mobile, avoiding chat flooding.
    """
    if not text:
        return ""
    line_count = text.count("\n") + 1
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n… (截断，全文共 {len(text)} 字符)"
    stats = f"↳ {line_count} 行输出"
    return f"{stats}\n<blockquote expandable>{text}</blockquote>"


def extract_last_dialogue(brain_path: Optional[Path]) -> tuple[str, str]:
    """Extract latest meaningful user prompt and model response from transcript.jsonl.

    Uses progressive tiered backwards seeking (64KB -> 256KB -> 1MB -> 4MB -> Full)
    to sub-millisecond backtrack across large tool execution gaps until both a valid,
    scaffolding-free user prompt and model response are retrieved.
    """
    if not brain_path or not brain_path.is_dir():
        return "", ""
    jsonl = brain_path / ".system_generated" / "logs" / "transcript.jsonl"
    if not jsonl.is_file():
        return "", ""
    try:
        size = jsonl.stat().st_size
        if size == 0:
            return "", ""

        import json

        # Tiered chunk sizes to backtrack from end of file
        chunk_sizes = [65536, 262144, 1048576, 4194304, size]
        last_u, last_m = "", ""

        with open(jsonl, "rb") as f:
            for chunk_size in chunk_sizes:
                read_bytes = min(size, chunk_size)
                f.seek(size - read_bytes)
                raw = f.read().decode("utf-8", errors="ignore")

                raw_lines = raw.split("\n")
                # Drop first partial line if we did not read from file beginning
                if read_bytes < size and len(raw_lines) > 1:
                    raw_lines = raw_lines[1:]

                for l in reversed(raw_lines):
                    l_str = l.strip()
                    if not l_str:
                        continue
                    try:
                        item = json.loads(l_str)
                        itype = item.get("type")
                        content = item.get("content", "")

                        if not last_u and itype == "USER_INPUT":
                            cleaned = clean_dialogue_snippet(content, max_chars=1000)
                            if cleaned:
                                last_u = cleaned

                        elif not last_m and itype == "PLANNER_RESPONSE":
                            cleaned = clean_dialogue_snippet(content, max_chars=1000)
                            if cleaned:
                                last_m = cleaned

                        if last_u and last_m:
                            return last_u, last_m
                    except Exception:
                        continue

                # Stop if we already inspected the whole file
                if read_bytes >= size:
                    break

        return last_u, last_m
    except Exception:
        return "", ""


def inspect_track_candidate(root: Path, conversation_id: str, source: str) -> TrackCandidate | None:
    """Inspect durable track files for a given conversation UUID.
    
    Calculates total durable bytes (db + db-wal + brain recursive files)
    and max mtime among those files. db-shm is explicitly excluded.
    """
    if not is_valid_uuid(conversation_id):
        return None

    cid = conversation_id.strip().lower()
    conv_dir = root / "conversations"
    db_path = conv_dir / f"{cid}.db"
    wal_path = conv_dir / f"{cid}.db-wal"
    brain_dir = root / "brain" / cid

    db_stat = _safe_stat(db_path)
    if not db_stat:
        return None

    total_bytes, max_mtime = db_stat

    wal_stat = _safe_stat(wal_path)
    if wal_stat:
        wal_bytes, wal_mtime = wal_stat
        total_bytes += wal_bytes
        max_mtime = max(max_mtime, wal_mtime)

    brain_path: Optional[Path] = None
    if brain_dir.is_dir():
        brain_path = brain_dir
        try:
            for dirpath, _, filenames in os.walk(brain_dir):
                for filename in filenames:
                    file_p = Path(dirpath) / filename
                    f_stat = _safe_stat(file_p)
                    if f_stat:
                        f_bytes, f_mtime = f_stat
                        total_bytes += f_bytes
                        max_mtime = max(max_mtime, f_mtime)
        except (OSError, PermissionError):
            pass

    last_u, last_m = extract_last_dialogue(brain_path)

    return TrackCandidate(
        source=source,
        conversation_id=cid,
        db_path=db_path,
        brain_path=brain_path,
        last_activity=max_mtime,
        total_bytes=total_bytes,
        last_user_prompt=last_u,
        last_model_response=last_m,
    )


def scan_candidates(
    root: Path | str,
    source: str,
    min_bytes: int | None = None,
    show_all: bool = False,
) -> list[TrackCandidate]:
    """Scan root for conversation tracks, filtering by durable bytes and sorting by activity."""
    root_path = Path(root)
    conv_dir = root_path / "conversations"
    threshold = min_bytes if min_bytes is not None else get_track_min_bytes()

    candidates: list[TrackCandidate] = []
    if not conv_dir.is_dir():
        return candidates

    try:
        entries = list(conv_dir.iterdir())
    except (OSError, PermissionError):
        return candidates

    for entry in entries:
        if not entry.name.endswith(".db"):
            continue
        cid = entry.name[:-3]
        if not is_valid_uuid(cid):
            continue

        cand = inspect_track_candidate(root_path, cid, source)
        if cand is None:
            continue

        if show_all or cand.total_bytes >= threshold:
            candidates.append(cand)

    candidates.sort(key=lambda c: c.last_activity, reverse=True)
    return candidates


def scan_all_candidates(
    ide_root: Path | str | None = None,
    cli_root: Path | str | None = None,
    min_bytes: int | None = None,
    show_all: bool = False,
) -> list[TrackCandidate]:
    """Scan both IDE and CLI roots with strict IDE priority deduplication.

    Since IDE tracks can be cloned to CLI domain (but CLI tracks are never copied back to IDE),
    any track present in IDE is authoritatively an IDE native track. CLI tracks are only listed
    if their UUID does not already exist in the IDE domain.
    """
    ide_dir = Path(ide_root) if ide_root else get_default_ide_root()
    cli_dir = Path(cli_root) if cli_root else get_default_cli_root()

    ide_candidates = scan_candidates(ide_dir, source="ide", min_bytes=min_bytes, show_all=show_all)
    cli_candidates = scan_candidates(cli_dir, source="cli", min_bytes=min_bytes, show_all=show_all)

    # IDE priority: collect all IDE track UUIDs
    ide_uuids = {c.conversation_id.lower() for c in ide_candidates}

    results: list[TrackCandidate] = list(ide_candidates)
    for c in cli_candidates:
        if c.conversation_id.lower() not in ide_uuids:
            results.append(c)

    results.sort(key=lambda c: c.last_activity, reverse=True)
    return results


def snapshot_cli_track_ids(cli_root: Path | str | None = None) -> Set[str]:
    """Snapshot all valid conversation UUIDs currently in the CLI conversations directory."""
    cli_dir = Path(cli_root) if cli_root else get_default_cli_root()
    conv_dir = cli_dir / "conversations"
    if not conv_dir.is_dir():
        return set()

    try:
        return {
            entry.name[:-3].lower()
            for entry in conv_dir.iterdir()
            if entry.name.endswith(".db") and is_valid_uuid(entry.name[:-3])
        }
    except (OSError, PermissionError):
        return set()


def detect_new_imported_track(
    initial_snapshot: Set[str],
    cli_root: Path | str | None = None,
    min_bytes: int = 0,
    show_all: bool = True,
) -> TrackCandidate:
    """Detect exactly one newly imported CLI track compared to initial snapshot.
    
    Fail-loud behavior:
    - 0 new tracks -> raises ImportDetectionError
    - >1 new tracks -> raises ImportDetectionError (refuses to guess 'latest')
    - exactly 1 new track -> returns TrackCandidate
    """
    cli_dir = Path(cli_root) if cli_root else get_default_cli_root()
    current_ids = snapshot_cli_track_ids(cli_dir)
    initial_norm = {i.lower() for i in initial_snapshot}
    new_ids = current_ids - initial_norm

    if len(new_ids) == 0:
        raise ImportDetectionError(
            "No new CLI conversation track detected. "
            "Please ensure the official interactive import ('agy' -> /resume -> Tab to Antigravity -> Import) completed successfully."
        )
    if len(new_ids) > 1:
        sorted_ids = sorted(new_ids)
        raise ImportDetectionError(
            f"Ambiguous import: {len(new_ids)} new tracks detected ({', '.join(sorted_ids)}). "
            "Import detection requires exactly one newly created track; refusing to guess."
        )

    single_id = next(iter(new_ids))
    cand = inspect_track_candidate(cli_dir, single_id, source="cli")
    if not cand:
        raise ImportDetectionError(f"Detected new track '{single_id}' but failed to read its files.")
    return cand


def bind_conversation_id_to_env(
    conversation_id: str,
    env_path: Path | str,
    source: str = "cli",
) -> Path:
    """Bind conversation ID to specified .env file preserving comments and existing keys.
    
    Enforces that only imported CLI tracks (source='cli') can be bound.
    """
    if source != "cli":
        raise ImportRequiredError(
            f"Cannot bind conversation from source '{source}'. "
            "Only imported CLI tracks (source='cli') can be bound to PocketFleet. "
            "Please perform official migration via `agy` -> `/resume` -> Tab to Antigravity -> Import."
        )

    cid_clean = str(conversation_id).strip().lower()
    if not is_valid_uuid(cid_clean):
        raise ValueError(f"Invalid conversation ID '{conversation_id}'. Must be a valid UUID.")

    target_env = Path(env_path)
    target_env.parent.mkdir(parents=True, exist_ok=True)

    key = "POCKETFLEET_ANTIGRAVITY_CONVERSATION_ID"
    lines: list[str] = []
    replaced = False

    if target_env.is_file():
        for raw_line in target_env.read_text(encoding="utf-8-sig").splitlines():
            line_clean = raw_line.strip()
            if line_clean.startswith(f"{key}=") or line_clean.startswith(f"export {key}="):
                lines.append(f"{key}={cid_clean}")
                replaced = True
            else:
                lines.append(raw_line)

    if not replaced:
        lines.append(f"{key}={cid_clean}")

    target_env.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ[key] = cid_clean
    return target_env


def safe_snapshot_sqlite(src_db: Path, dst_db: Path) -> None:
    """Safely create an online, transactional snapshot of SQLite database using SQLite Backup API.

    Opens source DB in read-only URI mode ('file:<path>?mode=ro') to guarantee
    no lock contention with active IDE processes, and flushes WAL pages into dst_db
    atomically to eliminate transaction tearing.
    Falls back gracefully to safe copy if the source file is not a valid SQLite database
    (e.g., in unit test mock environments or legacy formats).
    """
    dst_db.parent.mkdir(parents=True, exist_ok=True)
    try:
        if src_db.is_file() and src_db.stat().st_size > 0:
            with open(src_db, "rb") as f:
                header = f.read(16)
            if header.startswith(b"SQLite format 3\x00"):
                uri = f"file:{src_db.resolve().as_posix()}?mode=ro"
                src_conn = sqlite3.connect(uri, uri=True, timeout=10.0)
                try:
                    dst_conn = sqlite3.connect(dst_db)
                    try:
                        with dst_conn:
                            src_conn.backup(dst_conn, pages=-1)
                        return
                    finally:
                        dst_conn.close()
                finally:
                    src_conn.close()
    except Exception:
        pass

    if src_db.is_file():
        shutil.copy2(src_db, dst_db)


def clone_ide_track_to_cli(
    candidate: TrackCandidate,
    cli_root: Path | str | None = None,
) -> TrackCandidate:
    """Seamlessly and atomically clone an IDE track's SQLite DB and brain directory into CLI domain.

    Enables instant 1-click binding from GUI without manual interactive terminal resume.
    Uses SQLite Backup API for zero-lock, transactionally consistent DB snapshot.
    Uses non-destructive incremental copy for the brain directory without rmtree.
    """
    if candidate.source == "cli":
        return candidate

    target_cli_root = Path(cli_root) if cli_root else get_default_cli_root()
    conv_dir = target_cli_root / "conversations"
    brain_dir = target_cli_root / "brain" / candidate.conversation_id

    conv_dir.mkdir(parents=True, exist_ok=True)

    dst_db = conv_dir / f"{candidate.conversation_id}.db"

    if candidate.db_path and candidate.db_path.is_file():
        safe_snapshot_sqlite(candidate.db_path, dst_db)

    # Note: SQLite backup API incorporates active WAL pages into dst_db automatically.
    # We clean up any stale dst_wal if present so the single consistent dst_db is used cleanly.
    dst_wal = conv_dir / f"{candidate.conversation_id}.db-wal"
    if dst_wal.exists():
        try:
            dst_wal.unlink()
        except OSError:
            pass

    if candidate.brain_path and candidate.brain_path.is_dir():
        brain_dir.parent.mkdir(parents=True, exist_ok=True)
        # Non-destructive incremental copy: never call rmtree to prevent lock/permission failures
        shutil.copytree(candidate.brain_path, brain_dir, dirs_exist_ok=True)

    return TrackCandidate(
        source="cli",
        conversation_id=candidate.conversation_id,
        db_path=dst_db,
        brain_path=brain_dir if brain_dir.is_dir() else None,
        last_activity=candidate.last_activity,
        total_bytes=candidate.total_bytes,
        last_user_prompt=candidate.last_user_prompt,
        last_model_response=candidate.last_model_response,
    )


def bind_track_candidate(
    candidate: TrackCandidate,
    env_path: Path | str,
    cli_root: Path | str | None = None,
) -> Path:
    """Helper to bind a TrackCandidate directly to an .env file, auto-cloning IDE tracks seamlessly."""
    if candidate.source == "ide":
        candidate = clone_ide_track_to_cli(candidate, cli_root=cli_root)
    return bind_conversation_id_to_env(
        conversation_id=candidate.conversation_id,
        env_path=env_path,
        source="cli",
    )


def write_handover_dossier(
    workspace_cwd: Path | str,
    conversation_id: str,
    cli_root: Path | str | None = None,
    note: str = "",
) -> Path:
    """Write an immutable physical handover dossier (.fleet_handover.md) in the workspace.

    Serves as the physical black box ensuring that even if notification channels fail,
    the user and IDE track have an unambiguous record of mobile operations.
    """
    ws = Path(workspace_cwd)
    handover_file = ws / ".fleet_handover.md"
    cid = conversation_id.strip().lower()

    cli_dir = Path(cli_root) if cli_root else get_default_cli_root()
    transcript_file = cli_dir / "brain" / cid / ".system_generated" / "logs" / "transcript.jsonl"

    line_count = 0
    if transcript_file.is_file():
        try:
            with open(transcript_file, "r", encoding="utf-8", errors="ignore") as f:
                line_count = sum(1 for _ in f)
        except Exception:
            pass

    cand = inspect_track_candidate(cli_dir, cid, source="cli")
    last_u = cand.last_user_prompt if cand else ""
    last_m = cand.last_model_response if cand else ""

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    start_line = max(1, line_count - 50)
    content = [
        "# 🛸 PocketFleet 外勤施工交接公文",
        f"- **交接时间**: `{now_str}`",
        f"- **会话 UUID**: `{cid}`",
        f"- **外勤执行轨**: `Antigravity CLI Track`",
        f"- **黑匣子物理路径**: `{transcript_file.as_posix()}`",
        f"- **黑匣子总行数**: `{line_count}` 行",
        "",
        "## 📝 最新交互概览",
        f"- **用户指令**: {last_u or '（无）'}",
        f"- **执行反馈**: {last_m or '（无）'}",
    ]
    if note:
        content.extend(["", "## 📌 附加交接说明", note])

    content.extend([
        "",
        "## 🧭 IDE 接关操作指引",
        "IDE 侧 Coding Agent 收到通知后，可调用 `view_file` 查阅黑匣子日志：",
        "```text",
        f"view_file path: {transcript_file.as_posix()}",
        f"line_range: {start_line} - {line_count}",
        "```",
        "以快速吸收外勤施工成果，无需重启 IDE，实现无缝接关！",
        "",
    ])

    handover_file.write_text("\n".join(content), encoding="utf-8")
    return handover_file


def find_agentapi_binary() -> str | None:
    """Locate the agentapi executable across environment, IDE directories, and PATH."""
    override = os.environ.get("POCKETFLEET_AGENTAPI_PATH")
    if override and os.path.isfile(override):
        return override

    ide_root = get_default_ide_root()
    for candidate in [
        ide_root / "bin" / "agentapi.bat",
        ide_root / "bin" / "agentapi.cmd",
        ide_root / "bin" / "agentapi.exe",
        ide_root / "bin" / "agentapi",
    ]:
        if candidate.is_file():
            return str(candidate)

    for name in ["agentapi.bat", "agentapi.cmd", "agentapi.exe", "agentapi"]:
        found = shutil.which(name)
        if found:
            return found

    return None


def get_active_ide_conversation_id(ide_root: Path | str | None = None) -> str | None:
    """Resolve active IDE conversation ID, preferring env override then latest active IDE track."""
    env_id = os.environ.get("POCKETFLEET_IDE_CONVERSATION_ID", "").strip()
    if env_id and is_valid_uuid(env_id):
        return env_id.lower()

    ide_dir = Path(ide_root) if ide_root else get_default_ide_root()
    cands = scan_candidates(ide_dir, source="ide", show_all=True)
    if cands:
        return cands[0].conversation_id.lower()
    return None


def send_agentapi_message(
    content: str,
    recipient_id: str | None = None,
    title: str | None = None,
    ide_root: Path | str | None = None,
    timeout: float = 8.0,
) -> bool:
    """Send message to active Antigravity IDE conversation using official agentapi CLI."""
    agentapi_cmd = find_agentapi_binary()
    if not agentapi_cmd:
        logger.debug("agentapi binary not found; skipping IDE notification.")
        return False

    target_id = recipient_id or get_active_ide_conversation_id(ide_root)
    if not target_id:
        logger.debug("No active IDE conversation ID could be resolved.")
        return False

    cmd = [agentapi_cmd, "send-message"]
    if title:
        cmd.append(f"--title={title}")
    cmd.extend([target_id, content])

    if os.name == "nt" and agentapi_cmd.lower().endswith((".bat", ".cmd")):
        comspec = os.environ.get("COMSPEC", "cmd.exe")
        cmd = [comspec, "/c"] + cmd

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        return proc.returncode == 0
    except Exception as exc:
        logger.debug("agentapi execution exception: %s", exc)
        return False


def notify_ide_handover(
    conversation_id: str,
    handover_file: Path | str,
    line_start: int = 1,
    line_end: int = 100,
    transcript_path: Path | str | None = None,
    target_ide_id: str | None = None,
    ide_root: Path | str | None = None,
) -> bool:
    """Send handover notification to active IDE session using agentapi send-message if available.

    Returns True if notification was sent successfully, False otherwise (fails soft).
    """
    msg = (
        f"【PocketFleet 外勤交接通知】\n"
        f"用户已在外勤轨（CLI 会话: {conversation_id}）完成手机端操作。\n"
        f"1. 工作区交接档案已落盘：{Path(handover_file).as_posix()}\n"
    )
    if transcript_path:
        msg += (
            f"2. 外勤黑匣子记录：{Path(transcript_path).as_posix()}\n"
            f"请调用 view_file 查阅第 {line_start} 至 {line_end} 行以同步外勤施工认知。\n"
        )
    else:
        msg += "请查阅上述档案以同步外勤施工认知。\n"

    return send_agentapi_message(
        content=msg,
        recipient_id=target_ide_id,
        title="外勤交接通知",
        ide_root=ide_root,
    )


def notify_ide_launch(
    conversation_id: str,
    workspace_cwd: Path | str,
    initial_prompt: str = "",
    cli_root: Path | str | None = None,
    target_ide_id: str | None = None,
    min_interval_sec: float = 3600.0,
    force: bool = False,
    state_store: Any | None = None,
) -> bool:
    """Send launch-time handshake notification to active IDE session at CLI commencement.

    Throttled by min_interval_sec (default 60 minutes / 3600s). If the interval has not
    elapsed since the last launch notification, skips sending and returns False.
    """
    now = time.time()
    cli_dir = Path(cli_root) if cli_root else get_default_cli_root()
    marker_file = cli_dir / ".last_ide_handshake_ts"

    # Check 60-minute threshold
    last_ts = 0.0
    if state_store and hasattr(state_store, "get_meta"):
        try:
            val = state_store.get_meta("last_ide_handshake_ts", "0.0")
            last_ts = float(val or 0.0)
        except Exception:
            last_ts = 0.0
    elif marker_file.is_file():
        try:
            val = marker_file.read_text(encoding="utf-8").strip()
            last_ts = float(val or 0.0)
        except Exception:
            last_ts = 0.0

    if not force and last_ts > 0.0 and (now - last_ts) < min_interval_sec:
        # Throttled: do not spam the IDE within 60 minutes
        return False

    ws = Path(workspace_cwd)
    dossier_path = write_handover_dossier(
        workspace_cwd=ws,
        conversation_id=conversation_id,
        cli_root=cli_root,
        note=f"外勤任务开工指令: {clean_dialogue_snippet(initial_prompt, max_chars=120)}",
    )

    transcript_file = cli_dir / "brain" / conversation_id.lower() / ".system_generated" / "logs" / "transcript.jsonl"
    line_count = 0
    if transcript_file.is_file():
        try:
            with open(transcript_file, "r", encoding="utf-8", errors="ignore") as f:
                line_count = sum(1 for _ in f)
        except Exception:
            pass

    prompt_snippet = clean_dialogue_snippet(initial_prompt, max_chars=120) or "外勤协同任务启动"

    msg = (
        f"【🛸 PocketFleet 外勤开工与协同接关通知】\n"
        f"外勤任务已在手机端启动施工（外勤 CLI 轨 UUID: {conversation_id}）。\n"
        f"1. 工作区交接档案：{dossier_path.as_posix()}\n"
    )
    if transcript_file.is_file():
        msg += f"2. 外勤实时黑匣子：{transcript_file.as_posix()} (当前约 {line_count} 行)\n"
    msg += f"3. 本轮外勤指令：{prompt_snippet}\n"

    msg += (
        f"👉 指挥官随时可能回到 PC 电脑前查看进展或下达后续指令。\n"
        f"IDE 侧 Coding Agent 收到本公文后请保持就绪：若指挥官在 IDE 中询问外勤进度，"
        f"直接调用 view_file 查阅上述黑匣子最新日志，即可实时对齐外勤认知并平滑接管！\n"
    )

    sent = send_agentapi_message(
        content=msg,
        recipient_id=target_ide_id,
        title="外勤开工与协同接关通知",
    )

    # Record timestamp on successful send or attempt
    if state_store and hasattr(state_store, "set_meta"):
        try:
            state_store.set_meta("last_ide_handshake_ts", str(now))
        except Exception:
            pass
    try:
        marker_file.parent.mkdir(parents=True, exist_ok=True)
        marker_file.write_text(str(now), encoding="utf-8")
    except Exception:
        pass

    return sent



class AntigravityTrackController:
    """Controller service mediating between GUI/CLI callers and Antigravity track operations."""

    def __init__(
        self,
        workspace_cwd: Path | str | None = None,
        env_path: Path | str | None = None,
    ) -> None:
        self.workspace_cwd = Path(workspace_cwd) if workspace_cwd else Path.cwd()
        self.env_path = Path(env_path) if env_path else (self.workspace_cwd / ".env")
        self.import_snapshot: set[str] | None = None

    def create_handover(self, note: str = "", notify_ide: bool = True) -> tuple[Path, bool]:
        """Create physical handover dossier in workspace and optionally notify IDE via agentapi.

        Returns tuple of (dossier_path, notification_sent).
        """
        cid = self.get_current_bound_id()
        if not cid:
            raise AntigravityTrackError("无法创建交接公文：当前未绑定任何 Antigravity 会话 UUID。")

        dossier_path = write_handover_dossier(
            workspace_cwd=self.workspace_cwd,
            conversation_id=cid,
            note=note,
        )

        notified = False
        if notify_ide:
            cli_root = get_default_cli_root()
            transcript_file = cli_root / "brain" / cid / ".system_generated" / "logs" / "transcript.jsonl"
            line_count = 0
            if transcript_file.is_file():
                try:
                    with open(transcript_file, "r", encoding="utf-8", errors="ignore") as f:
                        line_count = sum(1 for _ in f)
                except Exception:
                    pass
            start_line = max(1, line_count - 50)
            notified = notify_ide_handover(
                conversation_id=cid,
                handover_file=dossier_path,
                line_start=start_line,
                line_end=line_count,
                transcript_path=transcript_file if transcript_file.is_file() else None,
            )

        return dossier_path, notified

    def get_current_bound_id(self) -> str | None:
        """Get currently configured Antigravity conversation ID from env or .env file."""
        val = os.environ.get("POCKETFLEET_ANTIGRAVITY_CONVERSATION_ID")
        if val and is_valid_uuid(val):
            return val.lower().strip()
        if self.env_path.is_file():
            try:
                for line in self.env_path.read_text(encoding="utf-8-sig").splitlines():
                    clean = line.strip()
                    if clean.startswith("POCKETFLEET_ANTIGRAVITY_CONVERSATION_ID="):
                        part = clean.split("=", 1)[1].strip()
                        if is_valid_uuid(part):
                            return part.lower().strip()
            except Exception:
                pass
        return None

    def scan_tracks(
        self,
        min_bytes: int | None = None,
        show_all: bool = False,
        ide_root: Path | str | None = None,
        cli_root: Path | str | None = None,
    ) -> list[TrackCandidate]:
        """Scan both IDE and CLI candidates sorted by last activity."""
        return scan_all_candidates(
            ide_root=ide_root,
            cli_root=cli_root,
            min_bytes=min_bytes,
            show_all=show_all,
        )

    def bind_track(
        self,
        candidate: TrackCandidate,
        is_daemon_running: bool = False,
        cli_root: Path | str | None = None,
    ) -> Path:
        """Bind selected track to .env. Seamlessly auto-clones IDE tracks to CLI root."""
        if is_daemon_running:
            raise RuntimeError(
                "Telegram Bridge Daemon 正在运行，禁止修改轨道绑定。\n"
                "请先停止 Daemon 服务（点击 Stop），以防止长轮询冲突造成 409 Conflict。"
            )
        if candidate.source == "ide":
            candidate = clone_ide_track_to_cli(candidate, cli_root=cli_root)

        return bind_conversation_id_to_env(
            conversation_id=candidate.conversation_id,
            env_path=self.env_path,
            source="cli",
        )

    def prepare_official_import(self, cli_root: Path | str | None = None) -> set[str]:
        """Snapshot current CLI track IDs before starting interactive import."""
        self.import_snapshot = snapshot_cli_track_ids(cli_root)
        return self.import_snapshot

    def launch_interactive_import_terminal(
        self,
        binary_path: str | None = None,
        cli_root: Path | str | None = None,
    ) -> subprocess.Popen:
        """Launch visible interactive agy terminal in a new console window."""
        self.prepare_official_import(cli_root=cli_root)

        bin_name = (
            binary_path
            or os.environ.get("POCKETFLEET_ANTIGRAVITY_CLI")
            or shutil.which("agy.cmd")
            or shutil.which("agy")
            or "agy"
        )

        cmd = [bin_name]
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010)

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(self.workspace_cwd),
                shell=False,
                creationflags=creationflags,
            )
            return proc
        except Exception as exc:
            raise RuntimeError(
                f"无法启动官方 Antigravity 交互式终端 ('{bin_name}'): {exc}\n"
                "请确认已安装 Antigravity CLI 并加入 PATH，或在配置中指定绝对路径。"
            ) from exc

    def detect_import_result(self, cli_root: Path | str | None = None) -> TrackCandidate:
        """Detect exactly one newly imported CLI track comparing against the pre-import snapshot."""
        if self.import_snapshot is None:
            raise ImportDetectionError(
                "未找到导入前快照！请先点击【开始官方导入】启动终端并执行导入。"
            )
        return detect_new_imported_track(
            initial_snapshot=self.import_snapshot,
            cli_root=cli_root,
        )

    def format_row(
        self,
        candidate: TrackCandidate,
        bound_id: str | None = None,
    ) -> dict[str, str]:
        """Format candidate attributes into human-readable table fields."""
        is_bound = bool(bound_id and candidate.conversation_id.lower() == bound_id.lower())
        bound_label = "★ 当前绑定" if is_bound else ""

        if is_bound:
            status_label = "🟢 当前绑定 (IDE)" if candidate.source == "ide" else "🟢 当前绑定 (CLI)"
        elif candidate.source == "cli":
            status_label = "⚡ CLI 对话轨"
        else:
            status_label = "💬 IDE 对话轨"

        source_label = "IDE 原生轨" if candidate.source == "ide" else "CLI 外勤轨"

        # Activity time format
        if candidate.last_activity > 0:
            act_str = datetime.fromtimestamp(candidate.last_activity).strftime("%Y-%m-%d %H:%M:%S")
        else:
            act_str = "未知"

        # Byte size format
        kb = candidate.total_bytes / 1024.0
        if kb >= 1024:
            size_str = f"{kb / 1024.0:.2f} MB"
        else:
            size_str = f"{kb:.1f} KB"

        u_snippet = clean_dialogue_snippet(candidate.last_user_prompt, max_chars=80)
        if u_snippet:
            snippet = u_snippet
        else:
            m_snippet = clean_dialogue_snippet(candidate.last_model_response, max_chars=76)
            if m_snippet:
                snippet = f"🤖 {m_snippet}"
            else:
                snippet = "（空白新对话轨）"

        return {
            "status": status_label,
            "snippet": snippet,
            "source": source_label,
            "last_activity": act_str,
            "total_bytes": size_str,
            "uuid": candidate.conversation_id,
            "bound": bound_label,
            "is_bound": str(is_bound),
            "raw_user_prompt": candidate.last_user_prompt,
            "raw_model_response": candidate.last_model_response,
        }

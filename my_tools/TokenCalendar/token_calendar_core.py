import hashlib
import json
import sqlite3
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


DEFAULT_AUTH_FILE = Path.home() / ".codex" / "auth.json"
DEFAULT_CONFIG_FILE = Path.home() / ".codex" / "config.toml"
DEFAULT_DB_PATH = Path("data/token_logs.sqlite3")
DEFAULT_PAGE_SIZE = 1000
QUOTA_PER_USD = 500_000


def load_api_key(auth_file: Path = DEFAULT_AUTH_FILE) -> str:
    payload = json.loads(auth_file.read_text())
    key = payload.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError(f"OPENAI_API_KEY was not found in {auth_file}")
    return key


def load_base_url(config_file: Path = DEFAULT_CONFIG_FILE) -> str:
    config = tomllib.loads(config_file.read_text())
    provider_name = config.get("model_provider")
    providers = config.get("model_providers", {})

    provider = providers.get(provider_name) if provider_name else None
    if not provider:
        for candidate in providers.values():
            if isinstance(candidate, dict) and candidate.get("base_url"):
                provider = candidate
                break

    if not provider or not provider.get("base_url"):
        raise RuntimeError(f"No model provider base_url was found in {config_file}")

    return provider["base_url"].rstrip("/")


def management_root(base_url: str) -> str:
    parsed = urllib.parse.urlparse(base_url)
    path = parsed.path.rstrip("/")
    if path == "/v1":
        path = ""
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, path, "", "", "")).rstrip("/")


def source_host(source: str) -> str:
    parsed = urllib.parse.urlparse(source)
    return parsed.netloc or parsed.path or source


def request_json(url: str, api_key: str, timeout: float = 6.0) -> dict:
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": "TokenCalendar/0.2",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} from {url}: {body[:500]}") from exc
    return json.loads(body)


def fetch_usage(root: str, api_key: str, timeout: float = 6.0) -> dict:
    payload = request_json(f"{root}/api/usage/token/", api_key, timeout=timeout)
    if payload.get("data"):
        return payload["data"]
    return payload


def fetch_logs(root: str, api_key: str, page_size: int = DEFAULT_PAGE_SIZE, timeout: float = 6.0) -> list[dict]:
    query = urllib.parse.urlencode({"p": 1, "page_size": page_size})
    payload = request_json(f"{root}/api/log/token/?{query}", api_key, timeout=timeout)
    data = payload.get("data", [])
    if not isinstance(data, list):
        raise RuntimeError("The log endpoint did not return a data list")

    seen = set()
    rows = []
    for row in data:
        identity = log_identity(row)
        if identity in seen:
            continue
        seen.add(identity)
        rows.append(row)
    return rows


def as_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def json_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def log_identity(row: dict) -> str:
    request_id = row.get("request_id")
    if request_id:
        return str(request_id)
    fingerprint = json.dumps(
        {
            "created_at": row.get("created_at"),
            "model_name": row.get("model_name"),
            "prompt_tokens": row.get("prompt_tokens"),
            "completion_tokens": row.get("completion_tokens"),
            "quota": row.get("quota"),
            "token_name": row.get("token_name"),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return "hash:" + hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()


@contextmanager
def file_lock(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as handle:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass


def connect_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS token_logs (
            request_id TEXT PRIMARY KEY,
            created_at INTEGER NOT NULL,
            token_name TEXT,
            model_name TEXT,
            quota INTEGER NOT NULL DEFAULT 0,
            prompt_tokens INTEGER NOT NULL DEFAULT 0,
            completion_tokens INTEGER NOT NULL DEFAULT 0,
            ip TEXT,
            other TEXT,
            raw_json TEXT NOT NULL,
            first_seen_at INTEGER NOT NULL,
            last_seen_at INTEGER NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_token_logs_created_at
            ON token_logs(created_at);

        CREATE TABLE IF NOT EXISTS usage_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scanned_at INTEGER NOT NULL,
            source_host TEXT NOT NULL,
            usage_json TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_usage_snapshots_scanned_at
            ON usage_snapshots(scanned_at);

        CREATE TABLE IF NOT EXISTS scan_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scanned_at INTEGER NOT NULL,
            source_host TEXT NOT NULL,
            fetched_count INTEGER NOT NULL,
            inserted_count INTEGER NOT NULL,
            duplicate_count INTEGER NOT NULL,
            oldest_created_at INTEGER,
            newest_created_at INTEGER,
            previous_newest_created_at INTEGER,
            gap_risk INTEGER NOT NULL DEFAULT 0
        );
        """
    )


def insert_usage_snapshot(conn: sqlite3.Connection, scanned_at: int, host: str, usage: dict) -> None:
    conn.execute(
        """
        INSERT INTO usage_snapshots (scanned_at, source_host, usage_json)
        VALUES (?, ?, ?)
        """,
        (scanned_at, host, json.dumps(usage, ensure_ascii=False, sort_keys=True)),
    )


def previous_newest_created_at(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT MAX(created_at) AS newest FROM token_logs").fetchone()
    if not row or row["newest"] is None:
        return None
    return int(row["newest"])


def insert_logs(conn: sqlite3.Connection, rows: list[dict], scanned_at: int) -> tuple[int, int]:
    inserted = 0
    duplicates = 0
    for row in rows:
        request_id = log_identity(row)
        values = (
            request_id,
            as_int(row.get("created_at")),
            str(row.get("token_name") or ""),
            str(row.get("model_name") or ""),
            as_int(row.get("quota")),
            as_int(row.get("prompt_tokens")),
            as_int(row.get("completion_tokens")),
            str(row.get("ip") or ""),
            json_text(row.get("other")),
            json.dumps(row, ensure_ascii=False, sort_keys=True),
            scanned_at,
            scanned_at,
        )
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO token_logs (
                request_id, created_at, token_name, model_name, quota,
                prompt_tokens, completion_tokens, ip, other, raw_json,
                first_seen_at, last_seen_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )
        if cursor.rowcount == 1:
            inserted += 1
        else:
            duplicates += 1
            conn.execute(
                """
                UPDATE token_logs
                SET last_seen_at = ?, raw_json = ?
                WHERE request_id = ?
                """,
                (scanned_at, values[9], request_id),
            )
    return inserted, duplicates


def record_scan(
    conn: sqlite3.Connection,
    scanned_at: int,
    host: str,
    fetched_count: int,
    inserted_count: int,
    duplicate_count: int,
    oldest_created_at: int | None,
    newest_created_at: int | None,
    previous_newest: int | None,
    gap_risk: bool,
) -> None:
    conn.execute(
        """
        INSERT INTO scan_runs (
            scanned_at, source_host, fetched_count, inserted_count,
            duplicate_count, oldest_created_at, newest_created_at,
            previous_newest_created_at, gap_risk
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            scanned_at,
            host,
            fetched_count,
            inserted_count,
            duplicate_count,
            oldest_created_at,
            newest_created_at,
            previous_newest,
            1 if gap_risk else 0,
        ),
    )


def collect_once(
    db_path: Path = DEFAULT_DB_PATH,
    auth_file: Path = DEFAULT_AUTH_FILE,
    config_file: Path = DEFAULT_CONFIG_FILE,
    page_size: int = DEFAULT_PAGE_SIZE,
    timeout: float = 6.0,
    usage_timeout: float = 2.0,
) -> dict:
    db_path = Path(db_path)
    with file_lock(db_path.with_suffix(db_path.suffix + ".lock")):
        api_key = load_api_key(Path(auth_file))
        base_url = load_base_url(Path(config_file))
        root = management_root(base_url)
        host = source_host(root)
        scanned_at = int(time.time())

        with ThreadPoolExecutor(max_workers=2) as executor:
            usage_future = executor.submit(fetch_usage, root, api_key, min(timeout, usage_timeout))
            rows_future = executor.submit(fetch_logs, root, api_key, page_size, timeout)
            rows = rows_future.result()
            try:
                usage = usage_future.result()
            except Exception:
                usage = {}
        created_times = [as_int(row.get("created_at")) for row in rows if as_int(row.get("created_at"))]
        oldest = min(created_times) if created_times else None
        newest = max(created_times) if created_times else None

        with connect_db(db_path) as conn:
            init_db(conn)
            previous_newest = previous_newest_created_at(conn)
            inserted, duplicates = insert_logs(conn, rows, scanned_at)
            if usage:
                insert_usage_snapshot(conn, scanned_at, host, usage)
            gap_risk = bool(previous_newest and oldest and oldest > previous_newest)
            record_scan(
                conn,
                scanned_at,
                host,
                len(rows),
                inserted,
                duplicates,
                oldest,
                newest,
                previous_newest,
                gap_risk,
            )
            conn.commit()

        return {
            "scanned_at": scanned_at,
            "source_host": host,
            "fetched_count": len(rows),
            "inserted_count": inserted,
            "duplicate_count": duplicates,
            "oldest_created_at": oldest,
            "newest_created_at": newest,
            "previous_newest_created_at": previous_newest,
            "gap_risk": gap_risk,
            "usage": usage,
        }


def load_logs_from_db(db_path: Path = DEFAULT_DB_PATH, start_timestamp: int | None = None) -> list[dict]:
    db_path = Path(db_path)
    if not db_path.exists():
        return []
    with connect_db(db_path) as conn:
        init_db(conn)
        where = ""
        params = ()
        if start_timestamp is not None:
            where = "WHERE created_at >= ?"
            params = (start_timestamp,)
        rows = conn.execute(
            f"""
            SELECT
                created_at, token_name, model_name, quota, prompt_tokens,
                completion_tokens, ip, other
            FROM token_logs
            {where}
            ORDER BY created_at DESC
            """,
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def load_latest_usage(db_path: Path = DEFAULT_DB_PATH) -> tuple[dict, str]:
    db_path = Path(db_path)
    if not db_path.exists():
        return {}, ""
    with connect_db(db_path) as conn:
        init_db(conn)
        row = conn.execute(
            """
            SELECT usage_json, source_host
            FROM usage_snapshots
            ORDER BY scanned_at DESC, id DESC
            LIMIT 1
            """
        ).fetchone()
    if not row:
        return {}, ""
    return json.loads(row["usage_json"]), row["source_host"]


def db_stats(db_path: Path = DEFAULT_DB_PATH) -> dict:
    db_path = Path(db_path)
    if not db_path.exists():
        return {}
    with connect_db(db_path) as conn:
        init_db(conn)
        log_row = conn.execute(
            """
            SELECT COUNT(*) AS stored_log_count,
                   MIN(created_at) AS first_created_at,
                   MAX(created_at) AS last_created_at
            FROM token_logs
            """
        ).fetchone()
        scan_row = conn.execute(
            """
            SELECT *
            FROM scan_runs
            ORDER BY scanned_at DESC, id DESC
            LIMIT 1
            """
        ).fetchone()
    stats = dict(log_row) if log_row else {}
    if scan_row:
        stats["last_scan"] = dict(scan_row)
    return stats

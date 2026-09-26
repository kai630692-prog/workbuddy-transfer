#!/usr/bin/env python3
"""Back up and reassign local WorkBuddy conversations between accounts.

This tool never handles login credentials. Run `prepare` while the old account
is still present, then `migrate` only after signing in to the new account and
closing WorkBuddy completely.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import uuid
from contextlib import closing
from datetime import datetime, timezone
from urllib.parse import quote


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_ROOT = Path.home() / ".workbuddy"
SNAPSHOT_DIRS = (
    "projects", "artifact-index", "changes-index", "changes-detail",
    "file-history", "file-tree-manifests", "blobs", "memory", "sessions",
    "storage", "tasks", "plans", "managed-prompts", "local_storage",
    "connectors", "inspiration", "audit-log", "clipboard-images",
    "shell-snapshots",
)
SNAPSHOT_FILES = (
    "settings.json", "user-state.json", "models.json", "workspace-state.json",
    "last-launch.json", "device-id", "mcp-approvals.json", "ioa-im-override.json",
    "qimei-cache.json", "SOUL.md", "IDENTITY.md", "USER.md",
    "BOOTSTRAP.md", "edge-sync-mapping-v4.db",
)
SESSION_RECORD_DIRS = (
    "projects", "artifact-index", "changes-index", "changes-detail",
    "file-history", "file-tree-manifests", "blobs",
)


def fail(message):
    raise RuntimeError(message)


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def app_running():
    if sys.platform != "win32":
        return False
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "(Get-Process -Name WorkBuddy -ErrorAction SilentlyContinue | Measure-Object).Count"],
        capture_output=True, text=True, errors="replace", check=False,
    )
    if result.returncode != 0:
        fail("无法确认 WorkBuddy 是否退出；进程检查失败，拒绝写入数据库")
    try:
        return int(result.stdout.strip()) > 0
    except ValueError:
        fail("无法解析 WorkBuddy 进程检查结果，拒绝写入数据库")


def ensure_closed(root):
    if root.resolve() == DEFAULT_ROOT.resolve():
        if sys.platform != "win32":
            fail("目前仅支持 Windows 上的 WorkBuddy 数据目录")
        if app_running():
            fail("WorkBuddy 仍在运行。请从托盘完全退出后重试；不会强制结束进程。")


def normalize_uid(value):
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError):
        fail(f"账号 UID 不是有效 UUID：{value}")


def readonly_uri(path):
    # A closed WAL-mode database needs a writable -shm file for ordinary
    # read-only connections. `immutable=1` avoids this when no WAL exists.
    # When a WAL is present, keep normal mode so uncheckpointed rows are seen.
    suffix = "?mode=ro" if Path(str(path) + "-wal").exists() else "?mode=ro&immutable=1"
    return "file:" + quote(path.resolve().as_posix(), safe="/:") + suffix


def connect_readonly(path):
    if not path.is_file():
        fail(f"找不到数据库：{path}")
    return sqlite3.connect(readonly_uri(path), uri=True)


def session_rows(db):
    with closing(connect_readonly(db)) as con:
        if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            fail(f"数据库完整性检查失败：{db}")
        return con.execute("SELECT id, user_id FROM sessions").fetchall()


def jsonl_ids(root):
    return {path.stem for path in (root / "projects").rglob("*.jsonl")}


def copy_tree_verified(source, target):
    count = 0
    bytes_copied = 0
    for parent, dirs, files in os.walk(source):
        dirs[:] = [name for name in dirs if not (Path(parent) / name).is_symlink()]
        relative = Path(parent).relative_to(source)
        (target / relative).mkdir(parents=True, exist_ok=True)
        for name in files:
            old = Path(parent) / name
            if old.is_symlink():
                fail(f"备份遇到符号链接，请人工检查：{old}")
            new = target / relative / name
            shutil.copy2(old, new)
            if old.stat().st_size != new.stat().st_size or sha256(old) != sha256(new):
                fail(f"备份文件校验失败：{old}")
            count += 1
            bytes_copied += new.stat().st_size
    return count, bytes_copied


def snapshot(root, target, require_old_uid=None):
    if target.resolve().is_relative_to(root.resolve()):
        fail("备份目录不能位于 WorkBuddy 数据目录内")
    if target.exists():
        fail(f"备份目录已存在，拒绝覆盖：{target}")
    target.mkdir(parents=True)
    try:
        source_db = root / "workbuddy.db"
        with closing(sqlite3.connect(readonly_uri(source_db), uri=True)) as source:
            with closing(sqlite3.connect(target / "workbuddy.db")) as dest:
                source.backup(dest)
        rows = session_rows(target / "workbuddy.db")
        counts = {}
        for _, uid in rows:
            counts[uid] = counts.get(uid, 0) + 1
        if require_old_uid and counts.get(require_old_uid, 0) == 0:
            fail(f"备份里没有旧账号 {require_old_uid} 的会话")
        copied = total_bytes = 0
        for dirname in SNAPSHOT_DIRS:
            source = root / dirname
            if source.is_dir():
                n, size = copy_tree_verified(source, target / dirname)
                copied += n
                total_bytes += size
        for filename in SNAPSHOT_FILES:
            source = root / filename
            if source.is_file():
                dest = target / filename
                shutil.copy2(source, dest)
                if sha256(source) != sha256(dest):
                    fail(f"备份文件校验失败：{source}")
                copied += 1
                total_bytes += dest.stat().st_size
        ids = {sid for sid, _ in rows}
        texts = jsonl_ids(target)
        if ids - texts:
            fail(f"备份缺少 {len(ids - texts)} 个会话正文文件")
        manifest = {
            "format": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source": str(root),
            "session_counts": counts,
            "session_ids": sorted(ids),
            "selected_old_uid": require_old_uid,
            "selected_old_session_ids": sorted(sid for sid, uid in rows if uid == require_old_uid),
            "transcript_count": len(texts),
            "files_copied": copied,
            "bytes_copied": total_bytes,
            "database_sha256": sha256(target / "workbuddy.db"),
        }
        (target / "snapshot.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return manifest
    except BaseException:
        # Preserve a failed snapshot for investigation; never present it as valid.
        (target / "INCOMPLETE.txt").write_text("备份未完成，不可用于迁移。", encoding="utf-8")
        raise


def load_snapshot(path):
    manifest_path = path / "snapshot.json"
    if (path / "INCOMPLETE.txt").exists() or not manifest_path.is_file():
        fail(f"备份不完整：{path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if sha256(path / "workbuddy.db") != manifest["database_sha256"]:
        fail("旧账号备份数据库的校验值不匹配")
    rows = session_rows(path / "workbuddy.db")
    if {sid for sid, _ in rows} != set(manifest["session_ids"]):
        fail("旧账号备份的会话清单与数据库不一致")
    selected = manifest.get("selected_old_uid")
    if selected and {sid for sid, uid in rows if uid == selected} != set(manifest["selected_old_session_ids"]):
        fail("旧账号备份的目标会话清单与数据库不一致")
    if set(manifest["session_ids"]) - jsonl_ids(path):
        fail("旧账号备份缺少会话正文")
    return manifest


def prepare(args):
    ensure_closed(args.root)
    rows = session_rows(args.root / "workbuddy.db")
    by_uid = {}
    for sid, uid in rows:
        by_uid.setdefault(uid, []).append(sid)
    if args.old_uid:
        old_uid = args.old_uid
        if old_uid not in by_uid:
            fail(f"数据库里没有指定的旧账号 UID：{old_uid}")
    elif len(by_uid) == 1:
        old_uid = next(iter(by_uid))
    else:
        fail(f"发现 {len(by_uid)} 个账号，请用 --old-uid 明确指定旧账号")
    ids = by_uid[old_uid]
    if {sid for sid, _ in rows} - jsonl_ids(args.root):
        fail("当前会话索引与原始正文文件不一致，先停止换号")
    name = "old-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    target = args.backups / name
    manifest = snapshot(args.root, target, require_old_uid=old_uid)
    print(json.dumps({"status": "prepared", "snapshot": str(target),
                      "old_uid": old_uid, "sessions": len(ids),
                      "files": manifest["files_copied"]}, ensure_ascii=False, indent=2))


def inspect(args):
    old = load_snapshot(args.snapshot)
    current = session_rows(args.root / "workbuddy.db")
    by_uid = {}
    for sid, uid in current:
        by_uid.setdefault(uid, []).append(sid)
    candidates = {}
    old_ids = set(old["session_ids"])
    for sid, uid in current:
        if sid not in old_ids:
            candidates[uid] = candidates.get(uid, 0) + 1
    print(json.dumps({"snapshot": str(args.snapshot),
                      "old_accounts": old["session_counts"],
                      "current_accounts": {uid: len(ids) for uid, ids in by_uid.items()},
                      "new_uid_candidates": candidates,
                      "app_running": app_running()}, ensure_ascii=False, indent=2))


def merge_record_files(old_root, root):
    copied = conflicts = 0
    for dirname in SESSION_RECORD_DIRS:
        base = old_root / dirname
        if not base.is_dir():
            continue
        for source in base.rglob("*"):
            if not source.is_file():
                continue
            target = root / dirname / source.relative_to(base)
            if target.exists():
                if sha256(source) != sha256(target):
                    conflicts += 1  # Keep the current version, which may be newer.
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            if sha256(source) != sha256(target):
                fail(f"恢复文件校验失败：{target}")
            copied += 1
    return copied, conflicts


def merge_database(old_db, current_db, old_uid, new_uid, old_ids):
    con = sqlite3.connect(current_db, timeout=60, uri=True)
    try:
        con.execute("ATTACH DATABASE ? AS olddb", (readonly_uri(old_db),))
        con.execute("BEGIN IMMEDIATE")
        con.execute("CREATE TEMP TABLE transfer_ids (id TEXT PRIMARY KEY)")
        con.executemany("INSERT INTO transfer_ids VALUES (?)", [(sid,) for sid in old_ids])
        for sid, uid in con.execute(
            "SELECT s.id, s.user_id FROM sessions s JOIN transfer_ids t ON t.id=s.id"
        ):
            if uid not in (old_uid, new_uid):
                fail(f"会话 {sid} 已属于第三个账号，拒绝覆盖")
        current_columns = [row[1] for row in con.execute("PRAGMA table_info(sessions)")]
        old_columns = {row[1] for row in con.execute("PRAGMA olddb.table_info(sessions)")}
        if not set(current_columns).issubset(old_columns):
            fail("新旧数据库 sessions 表结构不同，拒绝迁移")
        columns = ", ".join('"' + c + '"' for c in current_columns)
        con.execute(f"INSERT OR IGNORE INTO sessions ({columns}) "
                    f"SELECT {columns} FROM olddb.sessions WHERE user_id=?", (old_uid,))
        con.execute("UPDATE sessions SET user_id=? WHERE user_id=?", (new_uid, old_uid))
        # Usage records are keyed by session ID; retain any records the new DB already has.
        has_usage = con.execute("SELECT 1 FROM olddb.sqlite_master WHERE name='session_usage'").fetchone()
        if has_usage and con.execute("SELECT 1 FROM sqlite_master WHERE name='session_usage'").fetchone():
            cols = [row[1] for row in con.execute("PRAGMA table_info(session_usage)")]
            old_cols = {row[1] for row in con.execute("PRAGMA olddb.table_info(session_usage)")}
            if set(cols).issubset(old_cols):
                qcols = ", ".join('"' + c + '"' for c in cols)
                con.execute(f"INSERT OR IGNORE INTO session_usage ({qcols}) "
                            f"SELECT {qcols} FROM olddb.session_usage WHERE session_id IN "
                            f"(SELECT id FROM olddb.sessions WHERE user_id=?)", (old_uid,))
        result = con.execute(
            "SELECT s.id, s.user_id FROM sessions s JOIN transfer_ids t ON t.id=s.id"
        ).fetchall()
        if len(result) != len(old_ids) or any(uid != new_uid for _, uid in result):
            fail("事务内校验失败：旧会话没有全部指向新账号")
        if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            fail("事务内数据库完整性检查失败")
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


def migrate(args):
    ensure_closed(args.root)
    manifest = load_snapshot(args.snapshot)
    old_uid = manifest.get("selected_old_uid")
    old_ids = manifest.get("selected_old_session_ids", [])
    if not old_uid or not old_ids:
        fail("备份没有指定旧账号或目标会话，请重新执行 prepare")
    if args.new_uid == old_uid:
        fail("新旧账号 UID 相同")
    current = session_rows(args.root / "workbuddy.db")
    unexpected_old = {sid for sid, uid in current if uid == old_uid} - set(old_ids)
    if unexpected_old:
        fail(f"旧账号在备份后又增加了 {len(unexpected_old)} 条会话；需要先补备份")
    snapshot_ids = set(manifest["session_ids"])
    candidates = {uid for sid, uid in current if sid not in snapshot_ids}
    if candidates != {args.new_uid}:
        fail(f"新账号候选 UID 不唯一或与输入不符：{sorted(candidates)}")
    new_ids = [sid for sid, uid in current if uid == args.new_uid and sid not in snapshot_ids]
    if not new_ids:
        fail("当前数据库没有新账号独有的会话；请先在新号创建一条会话，再运行迁移")
    after_backup_ms = int(datetime.fromisoformat(manifest["created_at"]).timestamp() * 1000)
    with closing(connect_readonly(args.root / "workbuddy.db")) as con:
        newest_new = con.execute(
            "SELECT MAX(created_at) FROM sessions WHERE user_id=?", (args.new_uid,)
        ).fetchone()[0]
    if newest_new is None or newest_new < after_backup_ms:
        fail("未找到备份之后在新账号创建的会话；请先在新号创建一条会话")
    if all(uid == args.new_uid for sid, uid in current if sid in set(old_ids)) and \
            set(old_ids).issubset({sid for sid, _ in current}):
        print("旧会话已经全部归属目标账号，无需再次迁移。")
        return
    before = args.backups / ("before-migration-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    snapshot(args.root, before)
    copied, conflicts = merge_record_files(args.snapshot, args.root)
    merge_database(args.snapshot / "workbuddy.db", args.root / "workbuddy.db",
                   old_uid, args.new_uid, old_ids)
    memory_dir = args.root / "memory"
    old_memory = args.snapshot / "memory" / f"{old_uid}_memory.md"
    new_memory = memory_dir / f"{args.new_uid}_memory.md"
    if old_memory.is_file():
        memory_dir.mkdir(parents=True, exist_ok=True)
        dest = new_memory if not new_memory.exists() else memory_dir / f"{args.new_uid}_memory.from-old.md"
        shutil.copy2(old_memory, dest)
    actual = dict(session_rows(args.root / "workbuddy.db"))
    if any(actual.get(sid) != args.new_uid for sid in old_ids):
        fail("迁移后校验失败；迁移前备份已保留")
    missing_texts = set(old_ids) - jsonl_ids(args.root)
    if missing_texts:
        fail(f"迁移后缺少 {len(missing_texts)} 个正文文件；迁移前备份已保留")
    report = {"status": "migrated", "old_uid": old_uid, "new_uid": args.new_uid,
              "old_sessions": len(old_ids), "new_account_sessions_before": len(new_ids),
              "record_files_restored": copied, "record_file_conflicts_kept_current": conflicts,
              "before_migration_snapshot": str(before),
              "note": "本机数据校验通过；仍需打开 WorkBuddy 实际查看会话和附件。"}
    report_path = SCRIPT_DIR / ("migration-report-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".json")
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--backups", type=Path, default=SCRIPT_DIR / "backups")
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_parser = commands.add_parser("prepare", help="备份旧账号会话和关联本地记录")
    prepare_parser.add_argument("--old-uid", help="本地有多个账号时指定旧账号 UID")
    inspect_parser = commands.add_parser("inspect", help="只读查看换号状态")
    inspect_parser.add_argument("--snapshot", type=Path, required=True)
    migrate_parser = commands.add_parser("migrate", help="登录新号并退出 WorkBuddy 后迁移")
    migrate_parser.add_argument("--snapshot", type=Path, required=True)
    migrate_parser.add_argument("--new-uid", required=True)
    args = parser.parse_args()
    args.root = args.root.resolve()
    args.backups = args.backups.resolve()
    if getattr(args, "old_uid", None):
        args.old_uid = normalize_uid(args.old_uid)
    if getattr(args, "new_uid", None):
        args.new_uid = normalize_uid(args.new_uid)
    if hasattr(args, "snapshot"):
        args.snapshot = args.snapshot.resolve()
    if args.command == "prepare":
        prepare(args)
    elif args.command == "inspect":
        inspect(args)
    else:
        migrate(args)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)


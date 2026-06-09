"""Database setup with async SQLite support."""

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

engine = create_async_engine(
    settings.database_url,
    echo=settings.debug,
    connect_args={"timeout": 30} if settings.database_url.startswith("sqlite") else {},
)


if settings.database_url.startswith("sqlite"):
    @event.listens_for(engine.sync_engine, "connect")
    def _configure_sqlite(dbapi_connection, connection_record):
        """Improve SQLite concurrency for chat + background Worker writes."""

        del connection_record
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy models."""
    pass


async def init_db():
    """Create all tables and apply tiny SQLite-safe compatibility migrations."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        def _sqlite_compat(sync_conn):
            # Existing local DBs may predate the persisted Director chat table and
            # the full Board schema. `create_all` handles new tables, but not
            # additive columns/constraints, so keep this idempotent and local.
            chat_rows = sync_conn.exec_driver_sql("PRAGMA table_info(chat_turns)").fetchall()
            if chat_rows:
                chat_columns = {row[1] for row in chat_rows}
                legacy_chat_columns = {"assigned_agent_id", "result", "logs", "updated_at"}

                # A bad development schema once put Board task columns on chat_turns.
                # If those columns are NOT NULL without safe defaults, SQLAlchemy
                # inserts into the current ChatTurn model fail. SQLite cannot relax
                # existing NOT NULL/default constraints in place, so rebuild the
                # table with the intended chat-only schema and copy preserved fields.
                if legacy_chat_columns & chat_columns:
                    sync_conn.exec_driver_sql("DROP TABLE IF EXISTS chat_turns_migrated")
                    sync_conn.exec_driver_sql(
                        """
                        CREATE TABLE chat_turns_migrated (
                            id VARCHAR(36) NOT NULL PRIMARY KEY,
                            role VARCHAR(20) NOT NULL,
                            content TEXT NOT NULL,
                            agent_id VARCHAR(36),
                            runtime VARCHAR(20) DEFAULT '',
                            board_task_ids JSON DEFAULT '[]',
                            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                        )
                        """
                    )

                    def expr(column: str, fallback: str) -> str:
                        return column if column in chat_columns else fallback

                    sync_conn.exec_driver_sql(
                        f"""
                        INSERT INTO chat_turns_migrated
                            (id, role, content, agent_id, runtime, board_task_ids, created_at)
                        SELECT
                            {expr('id', "lower(hex(randomblob(4))) || '-' || lower(hex(randomblob(2))) || '-4' || substr(lower(hex(randomblob(2))),2) || '-' || substr('89ab', abs(random()) % 4 + 1, 1) || substr(lower(hex(randomblob(2))),2) || '-' || lower(hex(randomblob(6)))")},
                            COALESCE({expr('role', "'user'")}, 'user'),
                            COALESCE({expr('content', "''")}, ''),
                            {expr('agent_id', 'NULL')},
                            COALESCE({expr('runtime', "''")}, ''),
                            COALESCE({expr('board_task_ids', "'[]'")}, '[]'),
                            COALESCE({expr('created_at', 'CURRENT_TIMESTAMP')}, CURRENT_TIMESTAMP)
                        FROM chat_turns
                        """
                    )
                    sync_conn.exec_driver_sql("DROP TABLE chat_turns")
                    sync_conn.exec_driver_sql("ALTER TABLE chat_turns_migrated RENAME TO chat_turns")
                else:
                    if "runtime" not in chat_columns:
                        sync_conn.exec_driver_sql("ALTER TABLE chat_turns ADD COLUMN runtime VARCHAR(20) DEFAULT ''")
                    if "board_task_ids" not in chat_columns:
                        sync_conn.exec_driver_sql("ALTER TABLE chat_turns ADD COLUMN board_task_ids JSON DEFAULT '[]'")

            task_rows = sync_conn.exec_driver_sql("PRAGMA table_info(tasks)").fetchall()
            if task_rows:
                task_columns = {row[1] for row in task_rows}
                if "assigned_agent_id" not in task_columns:
                    sync_conn.exec_driver_sql("ALTER TABLE tasks ADD COLUMN assigned_agent_id VARCHAR(36)")
                if "result" not in task_columns:
                    sync_conn.exec_driver_sql("ALTER TABLE tasks ADD COLUMN result TEXT DEFAULT ''")
                if "logs" not in task_columns:
                    sync_conn.exec_driver_sql("ALTER TABLE tasks ADD COLUMN logs JSON DEFAULT '[]'")
                if "created_at" not in task_columns:
                    sync_conn.exec_driver_sql("ALTER TABLE tasks ADD COLUMN created_at DATETIME DEFAULT CURRENT_TIMESTAMP")
                if "updated_at" not in task_columns:
                    sync_conn.exec_driver_sql("ALTER TABLE tasks ADD COLUMN updated_at DATETIME DEFAULT CURRENT_TIMESTAMP")
                # Subtask hierarchy + priority for Director-driven decomposition.
                if "parent_task_id" not in task_columns:
                    sync_conn.exec_driver_sql("ALTER TABLE tasks ADD COLUMN parent_task_id VARCHAR(36)")
                    sync_conn.exec_driver_sql(
                        "CREATE INDEX IF NOT EXISTS ix_tasks_parent_task_id ON tasks(parent_task_id)"
                    )
                if "priority" not in task_columns:
                    sync_conn.exec_driver_sql("ALTER TABLE tasks ADD COLUMN priority INTEGER DEFAULT 100")

            # Agent permission flags. Conservative defaults: read+question only.
            agent_rows = sync_conn.exec_driver_sql("PRAGMA table_info(agents)").fetchall()
            if agent_rows:
                agent_columns = {row[1] for row in agent_rows}
                perm_defaults = {
                    "perm_read": "1",
                    "perm_write": "0",
                    "perm_execute": "0",
                    "perm_mcp": "0",
                    "perm_mode": "0",
                    "perm_subtasks": "0",
                    "perm_question": "1",
                }
                for col, default in perm_defaults.items():
                    if col not in agent_columns:
                        sync_conn.exec_driver_sql(
                            f"ALTER TABLE agents ADD COLUMN {col} BOOLEAN DEFAULT {default}"
                        )

            log_rows = sync_conn.exec_driver_sql("PRAGMA table_info(backend_logs)").fetchall()
            if log_rows:
                log_columns = {row[1] for row in log_rows}
                log_defaults = {
                    "timestamp": "DATETIME DEFAULT CURRENT_TIMESTAMP",
                    "source": "VARCHAR(80) DEFAULT 'Бэкенд'",
                    "level": "VARCHAR(20) DEFAULT 'INFO'",
                    "message": "TEXT DEFAULT ''",
                    "error_details": "TEXT DEFAULT ''",
                    "action_type": "VARCHAR(80) DEFAULT ''",
                    "status": "VARCHAR(20) DEFAULT ''",
                    "correlation_id": "VARCHAR(36)",
                    "agent_id": "VARCHAR(36)",
                    "task_id": "VARCHAR(36)",
                    "payload": "JSON DEFAULT '{}'",
                }
                for col, ddl in log_defaults.items():
                    if col not in log_columns:
                        sync_conn.exec_driver_sql(f"ALTER TABLE backend_logs ADD COLUMN {col} {ddl}")
            sync_conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_backend_logs_timestamp ON backend_logs(timestamp)"
            )
            sync_conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_backend_logs_level ON backend_logs(level)"
            )
            sync_conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_backend_logs_source ON backend_logs(source)"
            )
            sync_conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_backend_logs_action_type ON backend_logs(action_type)"
            )

        await conn.run_sync(_sqlite_compat)


async def get_db() -> AsyncSession:
    """Dependency for getting async database sessions."""
    async with async_session() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

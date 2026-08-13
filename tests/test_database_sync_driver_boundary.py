from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATABASE = ROOT / "app/core/database.py"
REQUIREMENTS = ROOT / "requirements.txt"


def test_sync_database_path_uses_declared_psycopg3_driver() -> None:
    source = DATABASE.read_text(encoding="utf-8")
    requirements = REQUIREMENTS.read_text(encoding="utf-8")

    assert 'replace("+asyncpg", "+psycopg")' in source
    assert 'replace("+asyncpg", "")' not in source
    assert "psycopg[binary]>=3.3,<4" in requirements


def test_sync_engine_remains_separate_from_async_engine() -> None:
    source = DATABASE.read_text(encoding="utf-8")

    assert "create_async_engine(" in source
    assert "create_engine(SYNC_DATABASE_URL" in source
    assert "SyncSessionLocal = sessionmaker(" in source
    assert "SessionLocal = SyncSessionLocal" in source

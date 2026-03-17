"""
Database initialization: create pgvector extension and all tables.
"""

from sqlalchemy import create_engine, text

from backend.config import get_config
from backend.storage.models import Base


def main():
    config = get_config()
    engine = create_engine(config.database.sync_url)

    # Enable pgvector extension
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))

    # Create all tables
    Base.metadata.create_all(engine)
    print("Database tables created successfully.")

    engine.dispose()


if __name__ == "__main__":
    main()

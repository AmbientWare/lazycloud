from __future__ import annotations

from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

from database import alembic_config, bootstrap_database


def test_existing_cli_tokens_are_classified_without_changing_authority(
    postgres_database_url: URL,
) -> None:
    database_url = postgres_database_url.render_as_string(hide_password=False)
    command.upgrade(alembic_config(database_url), "0034_remove_reload_limit")
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            user_id = connection.scalar(
                text("""
                INSERT INTO users (display_name, email, avatar_url, role, status)
                VALUES ('operator', '', '', 'member', 'active') RETURNING id
            """)
            )
            for name, kind in [
                ("cli", "user"),
                ("cli@laptop", "user"),
                ("ci-deploy", "user"),
                ("cli@browser", "session"),
            ]:
                connection.execute(
                    text("""
                        INSERT INTO tokens (
                            name, token_hash, prefix, kind, user_id, worker_id,
                            status, scopes, reusable, disabled_by_admin
                        ) VALUES (
                            :name, :name, 'redacted', :kind, :user_id, '',
                            'active', '["*"]', true, false
                        )
                    """),
                    {"name": name, "kind": kind, "user_id": user_id},
                )
            before = list(
                connection.execute(
                    text("""
                SELECT id, name, token_hash, kind, user_id, status, scopes, reusable
                FROM tokens ORDER BY name
            """)
                )
            )

        bootstrap_database(database_url)

        with engine.connect() as connection:
            assert dict(
                connection.execute(text("SELECT name, device_login FROM tokens")).tuples().all()
            ) == {
                "cli": True,
                "cli@laptop": True,
                "ci-deploy": False,
                "cli@browser": False,
            }
            assert (
                list(
                    connection.execute(
                        text("""
                SELECT id, name, token_hash, kind, user_id, status, scopes, reusable
                FROM tokens ORDER BY name
            """)
                    )
                )
                == before
            )
    finally:
        engine.dispose()

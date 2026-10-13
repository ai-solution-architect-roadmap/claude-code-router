from alembic import op


def upgrade():
    op.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")


def downgrade():
    op.execute("DROP TABLE users")

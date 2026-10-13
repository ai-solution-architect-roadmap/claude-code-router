from alembic import op


def upgrade():
    op.execute("ALTER TABLE users ADD COLUMN email TEXT")


def downgrade():
    op.execute("ALTER TABLE users DROP COLUMN email")

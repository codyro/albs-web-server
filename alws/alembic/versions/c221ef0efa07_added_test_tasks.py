"""Added test tasks

Revision ID: c221ef0efa07
Revises: 09647b29300e
Create Date: 2021-10-01 09:16:23.878042

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "c221ef0efa07"
down_revision = "09647b29300e"
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table(
        "test_tasks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("package_name", sa.TEXT(), nullable=False),
        sa.Column("package_version", sa.TEXT(), nullable=False),
        sa.Column("package_release", sa.TEXT(), nullable=True),
        sa.Column("env_arch", sa.TEXT(), nullable=False),
        sa.Column("build_task_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.Integer(), nullable=False),
        sa.Column(
            "alts_response", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["build_task_id"], ["build_tasks.id"],),
        sa.PrimaryKeyConstraint("id"),
    )
    op.add_column("platforms", sa.Column("test_dist_name", sa.Text(), nullable=False, server_default=''))
    op.alter_column("platforms", "test_dist_name", server_default=None)
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_column("platforms", "test_dist_name")
    op.drop_table("test_tasks")
    # ### end Alembic commands ###

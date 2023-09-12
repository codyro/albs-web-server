"""Add test configuration field for build ref

Revision ID: 9b1d146ab424
Revises: b924266af778
Create Date: 2023-09-07 08:59:27.393476

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '9b1d146ab424'
down_revision = 'b924266af778'
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column('build_task_refs', sa.Column('test_configuration', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_column('build_task_refs', 'test_configuration')
    # ### end Alembic commands ###

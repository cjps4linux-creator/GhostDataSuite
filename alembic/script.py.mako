"""${message}"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "${rev_id}"
down_revision = ${down_revision}
branch_labels = ${branch_labels}
depends_on = ${depends_on}


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

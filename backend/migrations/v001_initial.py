"""Create the first version of the Cachet schema on a new SQLite database."""

from backend.app.models import Base


def upgrade(connection):
    Base.metadata.create_all(connection)

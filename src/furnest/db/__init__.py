"""Database layer: SQLite in WAL mode, shared by the web and worker processes.

* :mod:`furnest.db.engine`: engine factory that applies the SQLite pragmas.
* :mod:`furnest.db.session`: session factory and the ``session_scope`` unit of work.
* :mod:`furnest.db.base`: declarative ``Base`` with a constraint naming convention.
* :mod:`furnest.db.models`: ORM models; importing it registers every table.
* :mod:`furnest.db.migrate`: run Alembic migrations and report revision vs head.
"""

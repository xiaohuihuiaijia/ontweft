"""Small atomic login budgets shared by all API processes through the existing DB."""

import hashlib
import json
import time

from sqlalchemy import case, delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from ontology.core.errors import DomainError
from ontology.identity.models import LoginThrottle


def bucket(*parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()


def reserve_login(db, address, tenant, identifier):
    account = bucket("account", address, tenant, identifier)
    budgets = [(account, 10, 300), (bucket("address", address), 100, 60)]
    current, denied = time.time(), False
    with db.transaction(write=True) as session:
        insert = pg_insert if session.bind.dialect.name == "postgresql" else sqlite_insert
        expired = select(LoginThrottle.key_hash).where(LoginThrottle.expires_at <= current).limit(256)
        session.execute(delete(LoginThrottle).where(LoginThrottle.key_hash.in_(expired)))
        # A stable order avoids lock inversions when concurrent attempts share buckets.
        for key, limit, seconds in sorted(budgets):
            stale = LoginThrottle.expires_at <= current
            statement = insert(LoginThrottle).values(key_hash=key, attempts=1, expires_at=current + seconds)
            attempts = session.scalar(
                statement.on_conflict_do_update(
                    index_elements=[LoginThrottle.key_hash],
                    set_={
                        "attempts": case(
                            (stale, 1),
                            (LoginThrottle.attempts <= limit, LoginThrottle.attempts + 1),
                            else_=limit + 1,
                        ),
                        "expires_at": case((stale, current + seconds), else_=LoginThrottle.expires_at),
                    },
                ).returning(LoginThrottle.attempts)
            )
            denied = denied or attempts > limit
    if denied:
        raise DomainError(429, "rate_limited", "登录尝试过多，请稍后重试")
    return account


def clear_login(db, key):
    with db.transaction(write=True) as session:
        session.execute(delete(LoginThrottle).where(LoginThrottle.key_hash == key))

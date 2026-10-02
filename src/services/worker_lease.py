"""Short database leases for periodic work shared by dashboard processes."""
from datetime import datetime, timedelta
from src.database.db import db_session
from src.database.delivery_models import WorkerLease


async def acquire(name: str, owner: str, seconds: int) -> bool:
    async with db_session(write=True) as session:
        lease = await session.get(WorkerLease, name)
        now = datetime.utcnow()
        if lease is not None and lease.expires_at > now:
            return False
        if lease is None:
            lease = WorkerLease(name=name, owner=owner, expires_at=now)
            session.add(lease)
        lease.owner = owner
        lease.expires_at = now + timedelta(seconds=seconds)
    return True


async def release(name: str, owner: str) -> None:
    async with db_session(write=True) as session:
        lease = await session.get(WorkerLease, name)
        if lease is not None and lease.owner == owner:
            lease.expires_at = datetime.utcnow()


async def renew(name: str, owner: str, seconds: int) -> bool:
    async with db_session(write=True) as session:
        lease = await session.get(WorkerLease, name)
        now = datetime.utcnow()
        if lease is None or lease.owner != owner or lease.expires_at <= now:
            return False
        lease.expires_at = now + timedelta(seconds=seconds)
        return True

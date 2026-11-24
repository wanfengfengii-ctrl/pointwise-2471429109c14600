from dataclasses import dataclass
import sqlite3

from packing.addresses import PackAddress
from snapshot.evidence import RoomEvidenceSide
from snapshot.receipts import PublicationReceipt


@dataclass(frozen=True, slots=True)
class RoomSnapshotRecord:
    snapshot_id: str
    manifest: bytes
    evidence: bytes
    receipt: PublicationReceipt


def create_room_schema(db: sqlite3.Connection) -> None:
    db.execute('''
        CREATE TABLE IF NOT EXISTS room_snapshots (
            snapshot_id TEXT NOT NULL PRIMARY KEY,
            manifest BLOB NOT NULL,
            evidence BLOB NOT NULL,
            record_count INTEGER NOT NULL CHECK (record_count >= 0),
            total_bytes INTEGER NOT NULL CHECK (total_bytes >= 0),
            manifest_sha256 TEXT NOT NULL
        ) WITHOUT ROWID
    ''')
    db.execute('''
        CREATE TABLE IF NOT EXISTS room_snapshot_candidates (
            snapshot_id TEXT NOT NULL REFERENCES room_snapshots(snapshot_id),
            room TEXT NOT NULL CHECK (room IN ('east', 'west')),
            pack_id TEXT NOT NULL,
            slot INTEGER NOT NULL CHECK (slot >= 0),
            certified_value BLOB NOT NULL,
            PRIMARY KEY (snapshot_id, room, pack_id, slot)
        ) WITHOUT ROWID
    ''')
    db.execute('''
        CREATE TABLE IF NOT EXISTS room_snapshot_packs (
            snapshot_id TEXT NOT NULL REFERENCES room_snapshots(snapshot_id),
            room TEXT NOT NULL CHECK (room IN ('east', 'west')),
            pack_id TEXT NOT NULL,
            pack_sha256 TEXT NOT NULL,
            PRIMARY KEY (snapshot_id, room, pack_id)
        ) WITHOUT ROWID
    ''')


def save_room_snapshot(
    db: sqlite3.Connection, record: RoomSnapshotRecord,
    sides: dict[str, RoomEvidenceSide],
) -> None:
    receipt = record.receipt
    to_be = (
        record.snapshot_id, record.manifest, record.evidence,
        receipt.record_count, receipt.total_bytes, receipt.manifest_sha256,
    )
    db.execute('''
        INSERT INTO room_snapshots(snapshot_id, manifest, evidence, record_count, total_bytes, manifest_sha256)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', to_be)
    for room, side in sides.items():
        for candidate in side.candidates:
            db.execute('''
                INSERT INTO room_snapshot_candidates(snapshot_id, room, pack_id, slot, certified_value)
                VALUES (?, ?, ?, ?, ?)
            ''', (
                record.snapshot_id, room, candidate.address.pack_id,
                candidate.address.slot, candidate.value,
            ))
        for pack_id, digest in side.packs:
            db.execute('''
                INSERT INTO room_snapshot_packs(snapshot_id, room, pack_id, pack_sha256)
                VALUES (?, ?, ?, ?)
            ''', (record.snapshot_id, room, pack_id, digest))


def load_room_snapshot(db: sqlite3.Connection, snapshot_id: str) -> RoomSnapshotRecord | None:
    row = db.execute('''
        SELECT manifest, evidence, record_count, total_bytes, manifest_sha256
        FROM room_snapshots WHERE snapshot_id=?
    ''', (snapshot_id,)).fetchone()
    if row is None:
        return None
    manifest, evidence, record_count, total_bytes, digest = row
    receipt = PublicationReceipt(snapshot_id, record_count, total_bytes, digest)
    return RoomSnapshotRecord(snapshot_id, manifest, evidence, receipt)


def load_room_candidate_sets(
    db: sqlite3.Connection, snapshot_id: str,
) -> dict[str, frozenset[PackAddress]]:
    rooms: dict[str, set[PackAddress]] = {'east': set(), 'west': set()}
    rows = db.execute('''
        SELECT room, pack_id, slot FROM room_snapshot_candidates WHERE snapshot_id=?
    ''', (snapshot_id,))
    for room, pack_id, slot in rows:
        rooms[room].add(PackAddress(pack_id, slot))
    return {room: frozenset(addresses) for room, addresses in rooms.items()}

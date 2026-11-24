from dataclasses import dataclass
import hashlib
import sqlite3

from ingest.records import InvalidObservation, RevisionKey, validate_identifier
from maintenance.transactions import read_transaction
from packing.addresses import PackAddress
from packing.chains import IncompleteEvidence
from packing.format import PackFormatError, decode_pack
from packing.room_chains import RoomChainError, RoomSlot, build_room_plan
from reading.audit import AuditResult, assemble_audit
from reading.reconstruct import reconstruct_entry
from snapshot.catalog import get_manifest_bytes
from snapshot.evidence import EvidenceError, RoomEvidence, parse_room_evidence
from snapshot.manifest import ManifestError, SnapshotManifest, parse_manifest
from snapshot.room_catalog import RoomSnapshotRecord, load_room_snapshot


class SnapshotReadError(ValueError):
    pass


def _open_in_view(db: sqlite3.Connection, selector: str | bytes) -> SnapshotManifest:
    if isinstance(selector, bytes):
        manifest = parse_manifest(selector)
        if get_manifest_bytes(db, manifest.snapshot_id) != selector:
            raise SnapshotReadError(f'no exact published manifest for {manifest.snapshot_id}')
        return manifest
    validate_identifier(selector, 'snapshot id')
    payload = get_manifest_bytes(db, selector)
    if payload is None:
        raise SnapshotReadError(f'unknown snapshot {selector}')
    manifest = parse_manifest(payload)
    if manifest.snapshot_id != selector:
        raise SnapshotReadError(f'manifest belongs to another snapshot: {selector}')
    return manifest


def open_snapshot(db: sqlite3.Connection, selector: str | bytes) -> SnapshotManifest:
    try:
        with read_transaction(db):
            return _open_in_view(db, selector)
    except (InvalidObservation, ManifestError) as exc:
        raise SnapshotReadError(str(exc)) from exc


def read_snapshot(db: sqlite3.Connection, selector: str | bytes) -> AuditResult:
    try:
        with read_transaction(db):
            manifest = _open_in_view(db, selector)
            values = (reconstruct_entry(db, item.address, item.revision) for item in manifest.entries)
            return assemble_audit(manifest, values)
    except (InvalidObservation, ManifestError, IncompleteEvidence) as exc:
        raise SnapshotReadError(str(exc)) from exc


@dataclass(frozen=True, slots=True)
class RoomAuditRow:
    position: int
    address: PackAddress
    revision: RevisionKey
    value: bytes
    chain: tuple[PackAddress, ...]

    @property
    def byte_length(self) -> int:
        return len(self.value)


@dataclass(frozen=True, slots=True)
class RoomReadResult:
    snapshot_id: str
    rows: tuple[RoomAuditRow, ...]

    @property
    def total_bytes(self) -> int:
        return sum(row.byte_length for row in self.rows)


def _check_room_record(
    snapshot_id: str, record: RoomSnapshotRecord,
    manifest: SnapshotManifest, evidence: RoomEvidence,
) -> None:
    receipt = record.receipt
    consistent = (
        manifest.snapshot_id == snapshot_id
        and evidence.snapshot_id == snapshot_id
        and receipt.snapshot_id == snapshot_id
        and receipt.manifest_sha256 == hashlib.sha256(record.manifest).hexdigest()
        and receipt.record_count == len(manifest.entries)
        and len(evidence.positions) == len(manifest.entries)
        and receipt.total_bytes == sum(len(value) for value in evidence.positions)
    )
    if not consistent:
        raise SnapshotReadError(f'inconsistent room authentication for {snapshot_id}')


def _read_room_from_evidence(
    snapshot_id: str, room: str, fetch_pack,
    manifest: SnapshotManifest, evidence: RoomEvidence,
) -> RoomReadResult:
    side = evidence.rooms[room]
    pack_identities = dict(side.packs)
    slots = []
    for candidate in side.candidates:
        if candidate.address.pack_id not in pack_identities:
            raise SnapshotReadError(
                f'room evidence misses pack identity for {candidate.address.pack_id}'
            )
        slots.append(RoomSlot(candidate.address, candidate.revision, candidate.value))
    delivered: dict[str, tuple | None] = {}

    def pack_entries(pack_id: str):
        if pack_id not in delivered:
            payload = fetch_pack(pack_id)
            entries = None
            if payload is not None:
                if not isinstance(payload, bytes):
                    raise SnapshotReadError(f'room port returned invalid pack {pack_id}')
                if hashlib.sha256(payload).hexdigest() == pack_identities[pack_id]:
                    entries = decode_pack(payload)
                # A pack whose bytes differ from the fixed identity is damaged
                # material: it is never used as evidence.
            delivered[pack_id] = entries
        return delivered[pack_id]

    def entry_of(address: PackAddress):
        entries = pack_entries(address.pack_id)
        if entries is None:
            return None
        if address.slot >= len(entries):
            raise SnapshotReadError(f'delivered pack lacks slot {address!r}')
        return entries[address.slot]

    plan = build_room_plan(slots, entry_of)
    rows = []
    for position, pinned in enumerate(manifest.entries):
        root = plan.find_root(pinned.revision, evidence.positions[position])
        if root is None:
            raise SnapshotReadError(f'{room} room cannot verify position {position} of {snapshot_id}')
        chain = plan.derive_chain(root.address)
        rows.append(RoomAuditRow(position, pinned.address, pinned.revision, chain.value, chain.addresses))
    return RoomReadResult(snapshot_id, tuple(rows))


def read_room_snapshot(
    db: sqlite3.Connection, snapshot_id: str, room: str, fetch_pack,
) -> RoomReadResult:
    try:
        validate_identifier(snapshot_id, 'snapshot id')
        if room not in ('east', 'west'):
            raise SnapshotReadError(f'unknown room {room!r}')
        if not callable(fetch_pack):
            raise SnapshotReadError('expected a pack fetch port')
        with read_transaction(db):
            record = load_room_snapshot(db, snapshot_id)
            if record is None:
                raise SnapshotReadError(f'unknown room snapshot {snapshot_id}')
            manifest = parse_manifest(record.manifest)
            evidence = parse_room_evidence(record.evidence)
            _check_room_record(snapshot_id, record, manifest, evidence)
        return _read_room_from_evidence(snapshot_id, room, fetch_pack, manifest, evidence)
    except (InvalidObservation, ManifestError, EvidenceError, RoomChainError,
            PackFormatError) as exc:
        raise SnapshotReadError(str(exc)) from exc

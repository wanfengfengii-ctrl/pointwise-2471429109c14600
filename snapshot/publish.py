from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import sqlite3

from ingest.records import InvalidObservation, validate_identifier
from maintenance.transactions import write_transaction
from packing.addresses import InvalidPackAddress, PackAddress, validate_pack_address
from packing.chains import IncompleteEvidence
from packing.format import DeltaEntry, PackFormatError, decode_pack
from packing.room_chains import RoomChainError, RoomSlot, build_room_plan
from packing.store import read_pack_bytes
from reading.reconstruct import reconstruct_entry
from snapshot.catalog import get_manifest_bytes, save_manifest
from snapshot.evidence import (
    EvidenceError, RoomCandidateEvidence, RoomEvidence, RoomEvidenceSide,
    parse_room_evidence, serialize_room_evidence,
)
from snapshot.manifest import (
    ManifestEntry, ManifestError, SnapshotManifest, parse_manifest, serialize_manifest,
)
from snapshot.receipts import PublicationReceipt, load_publication_receipt, save_publication_receipt
from snapshot.room_catalog import (
    RoomSnapshotRecord, load_room_candidate_sets, load_room_snapshot, save_room_snapshot,
)


class PublicationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PublicationResult:
    manifest: bytes
    receipt: PublicationReceipt


def _reissue(
    db: sqlite3.Connection, snapshot_id: str, addresses: tuple[PackAddress, ...], payload: bytes,
) -> PublicationResult:
    manifest = parse_manifest(payload)
    if manifest.snapshot_id != snapshot_id:
        raise PublicationError('stored manifest belongs to another snapshot')
    if tuple(item.address for item in manifest.entries) != addresses:
        raise PublicationError(f'different selection for snapshot {snapshot_id}')
    receipt = load_publication_receipt(db, snapshot_id)
    digest = hashlib.sha256(payload).hexdigest()
    if receipt is None or receipt.manifest_sha256 != digest or receipt.record_count != len(addresses):
        raise PublicationError(f'incomplete publication evidence for {snapshot_id}')
    return PublicationResult(payload, receipt)


def publish_snapshot(
    db: sqlite3.Connection, snapshot_id: str, addresses: Sequence[PackAddress],
) -> PublicationResult:
    try:
        validate_identifier(snapshot_id, 'snapshot id')
        try:
            ordered = tuple(addresses)
        except TypeError as exc:
            raise PublicationError('expected ordered snapshot addresses') from exc
        for address in ordered:
            validate_pack_address(address)
        with write_transaction(db):
            previous = get_manifest_bytes(db, snapshot_id)
            if previous is not None:
                return _reissue(db, snapshot_id, ordered, previous)
            entries = []
            total_bytes = 0
            for address in ordered:
                restored = reconstruct_entry(db, address)
                entries.append(ManifestEntry(address, restored.revision))
                total_bytes += len(restored.value)
            payload = serialize_manifest(SnapshotManifest(snapshot_id, tuple(entries)))
            receipt = PublicationReceipt(
                snapshot_id, len(entries), total_bytes, hashlib.sha256(payload).hexdigest(),
            )
            save_manifest(db, snapshot_id, payload)
            save_publication_receipt(db, receipt)
            return PublicationResult(payload, receipt)
    except (InvalidObservation, InvalidPackAddress, IncompleteEvidence, ManifestError) as exc:
        raise PublicationError(f'cannot publish {snapshot_id!r}: {exc}') from exc


@dataclass(frozen=True, slots=True)
class RoomPublicationResult:
    manifest: bytes
    evidence: bytes
    receipt: PublicationReceipt


_ROOMS = ('east', 'west')


def _normalize_room_candidates(room_candidates) -> dict[str, frozenset[PackAddress]]:
    if not isinstance(room_candidates, Mapping) or set(room_candidates) != set(_ROOMS):
        raise PublicationError('room candidates must be a mapping keyed by east and west')
    rooms = {}
    for room in _ROOMS:
        try:
            raw = tuple(room_candidates[room])
        except TypeError as exc:
            raise PublicationError(f'expected ordered {room} room candidates') from exc
        for address in raw:
            validate_pack_address(address)
        rooms[room] = frozenset(raw)
    return rooms


def _authenticate_room(
    db: sqlite3.Connection, room: str, candidates: frozenset[PackAddress],
    entries: list[ManifestEntry], originals: list[bytes],
) -> RoomEvidenceSide:
    slots = []
    decoded = {}
    pack_identities = {}
    for address in sorted(candidates):
        payload = read_pack_bytes(db, address.pack_id)
        if payload is None:
            raise PublicationError(f'{room} room candidate pack is not a sealed pack: {address.pack_id}')
        pack_identities[address.pack_id] = hashlib.sha256(payload).hexdigest()
        try:
            pack_entries = decode_pack(payload)
        except PackFormatError as exc:
            raise PublicationError(f'{room} room candidate pack is unreadable: {exc}') from exc
        if address.slot >= len(pack_entries):
            raise PublicationError(f'{room} room candidate slot outside its pack: {address!r}')
        entry = pack_entries[address.slot]
        decoded[address] = entry
        restored = reconstruct_entry(db, address)
        slots.append(RoomSlot(address, entry.revision, restored.value))
    certified = {slot.address: slot.value for slot in slots}
    for address, entry in decoded.items():
        if isinstance(entry, DeltaEntry):
            original_base = reconstruct_entry(db, entry.base).value
            if original_base[:entry.prefix] != certified[address][:entry.prefix]:
                raise PublicationError(
                    f'{room} room candidate base prefix is inconsistent at {address!r}'
                )
    plan = build_room_plan(slots, decoded.get)
    for position, (pinned, original) in enumerate(zip(entries, originals, strict=True)):
        if plan.find_root(pinned.revision, original) is None:
            raise PublicationError(f'{room} room candidates cannot restore manifest position {position}')
    side = tuple(
        RoomCandidateEvidence(slot.address, slot.revision, slot.value) for slot in slots
    )
    return RoomEvidenceSide(side, tuple(sorted(pack_identities.items())))


def _reissue_room_snapshot(
    db: sqlite3.Connection, snapshot_id: str, addresses: tuple[PackAddress, ...],
    rooms: dict[str, frozenset[PackAddress]], record: RoomSnapshotRecord,
) -> RoomPublicationResult:
    manifest = parse_manifest(record.manifest)
    if manifest.snapshot_id != snapshot_id:
        raise PublicationError('stored manifest belongs to another snapshot')
    if tuple(item.address for item in manifest.entries) != addresses:
        raise PublicationError(f'different selection for room snapshot {snapshot_id}')
    persisted = load_room_candidate_sets(db, snapshot_id)
    for room in _ROOMS:
        if persisted[room] != rooms[room]:
            raise PublicationError(f'different {room} room candidates for room snapshot {snapshot_id}')
    evidence = parse_room_evidence(record.evidence)
    receipt = record.receipt
    digest = hashlib.sha256(record.manifest).hexdigest()
    if (evidence.snapshot_id != snapshot_id or receipt.snapshot_id != snapshot_id
            or receipt.manifest_sha256 != digest or receipt.record_count != len(addresses)
            or len(evidence.positions) != len(manifest.entries)
            or receipt.total_bytes != sum(len(value) for value in evidence.positions)):
        raise PublicationError(f'incomplete publication evidence for {snapshot_id}')
    for room in _ROOMS:
        if {item.address for item in evidence.rooms[room].candidates} != rooms[room]:
            raise PublicationError(f'room evidence mismatch for {snapshot_id}')
    return RoomPublicationResult(record.manifest, record.evidence, receipt)


def publish_room_snapshot(
    db: sqlite3.Connection, snapshot_id: str, addresses: Sequence[PackAddress],
    room_candidates,
) -> RoomPublicationResult:
    try:
        validate_identifier(snapshot_id, 'snapshot id')
        try:
            ordered = tuple(addresses)
        except TypeError as exc:
            raise PublicationError('expected ordered snapshot addresses') from exc
        for address in ordered:
            validate_pack_address(address)
        rooms = _normalize_room_candidates(room_candidates)
        shared = {item.pack_id for item in rooms['east']} & {item.pack_id for item in rooms['west']}
        if shared:
            raise PublicationError(f'pack registered in both rooms: {sorted(shared)}')
        with write_transaction(db):
            record = load_room_snapshot(db, snapshot_id)
            if record is not None:
                return _reissue_room_snapshot(db, snapshot_id, ordered, rooms, record)
            entries = []
            originals = []
            total_bytes = 0
            for address in ordered:
                restored = reconstruct_entry(db, address)
                entries.append(ManifestEntry(address, restored.revision))
                originals.append(restored.value)
                total_bytes += len(restored.value)
            sides = {
                room: _authenticate_room(db, room, rooms[room], entries, originals)
                for room in _ROOMS
            }
            evidence = serialize_room_evidence(RoomEvidence(snapshot_id, tuple(originals), sides))
            payload = serialize_manifest(SnapshotManifest(snapshot_id, tuple(entries)))
            receipt = PublicationReceipt(
                snapshot_id, len(entries), total_bytes, hashlib.sha256(payload).hexdigest(),
            )
            save_room_snapshot(
                db, RoomSnapshotRecord(snapshot_id, payload, evidence, receipt), sides,
            )
            return RoomPublicationResult(payload, evidence, receipt)
    except (InvalidObservation, InvalidPackAddress, IncompleteEvidence, ManifestError,
            EvidenceError, RoomChainError) as exc:
        raise PublicationError(f'cannot publish room snapshot {snapshot_id!r}: {exc}') from exc

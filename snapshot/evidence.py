from collections.abc import Mapping
from dataclasses import dataclass
import json

from ingest.records import InvalidObservation, RevisionKey, validate_identifier, validate_revision_key
from packing.addresses import InvalidPackAddress, PackAddress, validate_pack_address


class EvidenceError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RoomCandidateEvidence:
    address: PackAddress
    revision: RevisionKey
    value: bytes


@dataclass(frozen=True, slots=True)
class RoomEvidenceSide:
    candidates: tuple[RoomCandidateEvidence, ...]
    packs: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class RoomEvidence:
    snapshot_id: str
    positions: tuple[bytes, ...]
    rooms: Mapping[str, RoomEvidenceSide]


_ROOMS = ('east', 'west')
_HEX_DIGEST = frozenset('0123456789abcdef')


def _check_hex_digest(value: str) -> None:
    if not isinstance(value, str) or len(value) != 64 or not set(value) <= _HEX_DIGEST:
        raise EvidenceError(f'invalid pack byte identity: {value!r}')


def _serialize_side(side: RoomEvidenceSide) -> dict:
    candidates = []
    for candidate in sorted(side.candidates, key=lambda item: item.address):
        validate_pack_address(candidate.address)
        validate_revision_key(candidate.revision)
        if not isinstance(candidate.value, bytes):
            raise EvidenceError('certified candidate value must be bytes')
        candidates.append([
            candidate.address.pack_id, candidate.address.slot, candidate.revision.source,
            candidate.revision.record, candidate.revision.revision, candidate.value.hex(),
        ])
    packs = []
    for pack_id, digest in sorted(side.packs):
        validate_identifier(pack_id, 'pack id')
        _check_hex_digest(digest)
        packs.append([pack_id, digest])
    return {'candidates': candidates, 'packs': packs}


def serialize_room_evidence(evidence: RoomEvidence) -> bytes:
    try:
        validate_identifier(evidence.snapshot_id, 'snapshot id')
        positions = []
        for value in evidence.positions:
            if not isinstance(value, bytes):
                raise EvidenceError('certified position value must be bytes')
            positions.append(value.hex())
        rooms = {room: _serialize_side(evidence.rooms[room]) for room in _ROOMS}
        document = [2, evidence.snapshot_id, positions, rooms]
        return json.dumps(document, separators=(',', ':'), sort_keys=True).encode('ascii')
    except (InvalidObservation, InvalidPackAddress, KeyError) as exc:
        raise EvidenceError(str(exc)) from exc


def _parse_hex(value) -> bytes:
    if not isinstance(value, str):
        raise EvidenceError(f'invalid hex field: {value!r}')
    try:
        return bytes.fromhex(value)
    except ValueError as exc:
        raise EvidenceError(f'invalid hex field: {value!r}') from exc


def _parse_side(document) -> RoomEvidenceSide:
    if not isinstance(document, dict) or set(document) != {'candidates', 'packs'}:
        raise EvidenceError('invalid room evidence side')
    rows, packs = document['candidates'], document['packs']
    if not isinstance(rows, list) or not isinstance(packs, list):
        raise EvidenceError('invalid room evidence side')
    candidates = []
    for row in rows:
        if not isinstance(row, list) or len(row) != 6:
            raise EvidenceError('invalid room candidate evidence')
        pack_id, slot, source, record, revision, value_hex = row
        try:
            candidate = RoomCandidateEvidence(
                PackAddress(pack_id, slot), RevisionKey(source, record, revision),
                _parse_hex(value_hex),
            )
        except (InvalidObservation, InvalidPackAddress) as exc:
            raise EvidenceError(str(exc)) from exc
        candidates.append(candidate)
    if len({item.address for item in candidates}) != len(candidates):
        raise EvidenceError('duplicate room candidate evidence')
    pack_identities = []
    for row in packs:
        if not isinstance(row, list) or len(row) != 2:
            raise EvidenceError('invalid room pack identity')
        pack_id, digest = row
        try:
            validate_identifier(pack_id, 'pack id')
        except InvalidObservation as exc:
            raise EvidenceError(str(exc)) from exc
        _check_hex_digest(digest)
        pack_identities.append((pack_id, digest))
    if len({pack_id for pack_id, _ in pack_identities}) != len(pack_identities):
        raise EvidenceError('duplicate room pack identity')
    return RoomEvidenceSide(tuple(candidates), tuple(pack_identities))


def parse_room_evidence(payload: bytes) -> RoomEvidence:
    if not isinstance(payload, bytes):
        raise EvidenceError('evidence must be bytes')
    try:
        document = json.loads(payload.decode('ascii'))
    except (ValueError, RecursionError) as exc:
        raise EvidenceError('invalid evidence encoding') from exc
    if not isinstance(document, list) or len(document) != 4:
        raise EvidenceError('invalid evidence structure')
    version, snapshot_id, positions, rooms = document
    if type(version) is not int or version != 2:
        raise EvidenceError('unsupported evidence structure')
    if not isinstance(positions, list) or not isinstance(rooms, dict):
        raise EvidenceError('invalid evidence structure')
    if set(rooms) != set(_ROOMS):
        raise EvidenceError('evidence rooms must be east and west')
    evidence = RoomEvidence(
        snapshot_id, tuple(_parse_hex(value) for value in positions),
        {room: _parse_side(rooms[room]) for room in _ROOMS},
    )
    if serialize_room_evidence(evidence) != payload:
        raise EvidenceError('evidence bytes are not canonical')
    return evidence

from collections.abc import Iterable, Mapping
from contextlib import closing
import sqlite3

from ingest.batches import BatchSubmission, accept_batch
from ingest.records import Observation, RevisionKey
from maintenance.transactions import initialize_database, open_database
from packing.addresses import PackAddress
from packing.chains import IncompleteEvidence, materialize_value
from packing.delta import apply_prefix_delta
from packing.format import DeltaEntry, decode_pack
from packing.seal import SealSelection, seal_pack
from packing.store import read_pack_bytes
from reading.open import read_snapshot
from snapshot.publish import publish_snapshot


class RoomShelf:
    def __init__(self, packs: Mapping[str, bytes | None]):
        self.packs = dict(packs)
        self.calls: list[str] = []

    def __call__(self, pack_id: str) -> bytes | None:
        self.calls.append(pack_id)
        if pack_id not in self.packs:
            raise AssertionError(f"attempted to access unavailable room pack {pack_id}")
        return self.packs[pack_id]


def capture_packs(db: sqlite3.Connection, pack_ids: Iterable[str]) -> dict[str, bytes]:
    result = {}
    for pack_id in pack_ids:
        payload = read_pack_bytes(db, pack_id)
        if payload is None:
            raise AssertionError(f"fixture pack missing: {pack_id}")
        result[pack_id] = payload
    return result


def stage_rooms(db: sqlite3.Connection) -> tuple[
    tuple[PackAddress, ...], dict[str, tuple[PackAddress, ...]],
]:
    source = "outpost"
    observations = tuple(
        Observation(RevisionKey(source, record, revision), value)
        for record, revision, value in (
            ("signal", "base", b"AB"), ("signal", "shift", b"Ax"),
            ("missing", "v1", b""), ("signal", "tail", b"Ax!"),
            ("reference", "west", b"AC"), ("noise", "v1", b"ZZ"),
        )
    )
    base, signal, empty, tail, reference, noise = accept_batch(
        db, BatchSubmission(source, "shift", observations),
    ).revisions
    east_base, = seal_pack(db, "P", (SealSelection(base),)).addresses
    east = seal_pack(db, "E", tuple(SealSelection(key) for key in (signal, empty, tail))).addresses
    west = seal_pack(db, "Q", (
        SealSelection(signal, east_base, 1), SealSelection(empty),
        SealSelection(tail, east[0], 2), SealSelection(empty, east_base, 0),
    )).addresses
    donors = seal_pack(db, "W", (SealSelection(reference), SealSelection(noise))).addresses
    addresses = east[0], east[1], east[0], east[2]
    return addresses, {"east": (east_base, *east), "west": (*west[:3], *donors)}


def stage_late_donor(db: sqlite3.Connection) -> PackAddress:
    source = "outpost"
    key = RevisionKey(source, "reference", "late")
    accept_batch(db, BatchSubmission(source, "late", (Observation(key, b"AD"),)))
    address, = seal_pack(db, "L", (SealSelection(key),)).addresses
    return address


def main() -> None:
    with closing(open_database(":memory:")) as db:
        initialize_database(db)
        addresses, _ = stage_rooms(db)
        ordinary = publish_snapshot(db, "v1-shift", addresses)
        values = tuple(row.value for row in read_snapshot(db, ordinary.manifest).rows)
        if values != (b"Ax", b"", b"Ax", b"Ax!"):
            raise AssertionError(f"unexpected V1 values {values!r}")
        shelf = RoomShelf(capture_packs(db, ("Q", "W")))

        def west_entry(address: PackAddress):
            if address.pack_id not in shelf.packs:
                return None
            return decode_pack(shelf(address.pack_id))[address.slot]

        delta = west_entry(PackAddress("Q", 0))
        if not isinstance(delta, DeltaEntry) or delta.base != PackAddress("P", 0):
            raise AssertionError(f"unexpected west delta {delta!r}")
        try:
            materialize_value(PackAddress("Q", 0), west_entry)
        except IncompleteEvidence as exc:
            if "P" not in str(exc):
                raise
            print(f"西房原址读法遇到缺件：{exc}")
        else:
            raise AssertionError("west physical chain unexpectedly reached the east pack")
        donor, wrong = decode_pack(shelf("W"))
        if apply_prefix_delta(donor.value, delta.prefix, delta.suffix) != b"Ax":
            raise AssertionError("fixture donor did not supply the correct prefix")
        if apply_prefix_delta(wrong.value, delta.prefix, delta.suffix) != b"Zx":
            raise AssertionError("fixture wrong donor did not expose the mismatch")
        stage_late_donor(db)
        print("V1 全仓原序：", [value.hex() for value in values])
        print("同长度候选前缀代入：AC→Ax，ZZ→Zx")


if __name__ == "__main__":
    main()

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from ingest.records import RevisionKey
from packing.addresses import PackAddress
from packing.delta import InvalidPrefix, apply_prefix_delta
from packing.format import DeltaEntry, FullEntry, PackEntry


class RoomChainError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RoomSlot:
    address: PackAddress
    revision: RevisionKey
    value: bytes


@dataclass(frozen=True, slots=True)
class RoomChain:
    root: PackAddress
    addresses: tuple[PackAddress, ...]
    value: bytes


class RoomPlan:
    """Restorable evidence chains within one room's certified candidates.

    A candidate is restorable when its entry is a full value consistent with
    its certified bytes, or when it is a delta whose required prefix (the
    first ``prefix`` bytes of its certified value, fixed by the center from
    the original base item) is matched by the certified value of another
    restorable candidate in the same room. Ranks measure the shortest chain
    to a full entry, so derived chains are finite and acyclic.
    """

    def __init__(
        self, slots: tuple[RoomSlot, ...], entries: dict[PackAddress, PackEntry],
    ) -> None:
        self._slots = slots
        self._by_address = {slot.address: slot for slot in slots}
        self._entries = entries
        self._ranks: dict[PackAddress, int] = {}
        self._compute_ranks()

    def _compute_ranks(self) -> None:
        ranks = self._ranks
        progressed = True
        while progressed:
            progressed = False
            for slot in self._slots:
                if slot.address in ranks:
                    continue
                entry = self._entries.get(slot.address)
                if entry is None:
                    continue
                if isinstance(entry, FullEntry):
                    if entry.revision == slot.revision and entry.value == slot.value:
                        ranks[slot.address] = 0
                        progressed = True
                    continue
                if not isinstance(entry, DeltaEntry):
                    raise RoomChainError(f'unknown entry type at {slot.address!r}')
                if entry.revision != slot.revision or entry.prefix > len(slot.value):
                    continue
                required = slot.value[:entry.prefix]
                best = None
                for other in self._slots:
                    rank = ranks.get(other.address)
                    if rank is None or other.value[:entry.prefix] != required:
                        continue
                    if best is None or rank < best:
                        best = rank
                if best is not None:
                    ranks[slot.address] = best + 1
                    progressed = True

    def restorable(self, address: PackAddress) -> bool:
        return address in self._ranks

    def find_root(self, revision: RevisionKey, value: bytes) -> RoomSlot | None:
        for slot in self._slots:
            if slot.address in self._ranks and slot.revision == revision and slot.value == value:
                return slot
        return None

    def derive_chain(self, root: PackAddress) -> RoomChain:
        if root not in self._ranks:
            raise RoomChainError(f'no restorable room chain from {root!r}')
        addresses = [root]
        current = root
        while True:
            entry = self._entries[current]
            if isinstance(entry, FullEntry):
                break
            if not isinstance(entry, DeltaEntry):
                raise RoomChainError(f'unknown entry type at {current!r}')
            required = self._by_address[current].value[:entry.prefix]
            target = self._ranks[current] - 1
            follow = None
            for slot in self._slots:
                if self._ranks.get(slot.address) != target:
                    continue
                if slot.value[:entry.prefix] != required:
                    continue
                follow = slot.address
                break
            if follow is None:
                raise RoomChainError(f'room chain interrupted at {current!r}')
            addresses.append(follow)
            current = follow
        terminal = self._entries[current]
        value = terminal.value
        if terminal.revision != self._by_address[current].revision or value != self._by_address[current].value:
            raise RoomChainError(f'certified value mismatch at {current!r}')
        for address in reversed(addresses[:-1]):
            entry = self._entries[address]
            if not isinstance(entry, DeltaEntry):
                raise RoomChainError(f'unknown entry type at {address!r}')
            try:
                value = apply_prefix_delta(value, entry.prefix, entry.suffix)
            except InvalidPrefix as exc:
                raise RoomChainError(f'invalid delta at {address!r}') from exc
            if entry.revision != self._by_address[address].revision or value != self._by_address[address].value:
                raise RoomChainError(f'certified value mismatch at {address!r}')
        return RoomChain(root, tuple(addresses), value)


def build_room_plan(
    slots: Iterable[RoomSlot],
    entry_of: Callable[[PackAddress], PackEntry | None],
) -> RoomPlan:
    ordered = []
    seen = set()
    for slot in slots:
        if slot.address in seen:
            raise RoomChainError(f'duplicate room candidate {slot.address!r}')
        seen.add(slot.address)
        ordered.append(slot)
    ordered.sort(key=lambda slot: slot.address)
    entries = {}
    for slot in ordered:
        entry = entry_of(slot.address)
        if entry is not None:
            if not isinstance(entry, (FullEntry, DeltaEntry)):
                raise RoomChainError(f'unknown entry type at {slot.address!r}')
            entries[slot.address] = entry
    return RoomPlan(tuple(ordered), entries)

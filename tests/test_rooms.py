from packing.addresses import PackAddress
from reading.open import SnapshotReadError, read_room_snapshot
from room_fixture import RoomShelf, capture_packs, stage_late_donor, stage_rooms
from snapshot.publish import PublicationError, publish_room_snapshot
from snapshot.room_catalog import load_room_snapshot
from tests.support import LakeCase


class RoomPublishTests(LakeCase):
    def test_dual_room_certification_and_cold_read(self):
        addresses, candidates = stage_rooms(self.db)
        result = publish_room_snapshot(self.db, 'room-shift', addresses, candidates)
        self.assertIsInstance(result.manifest, bytes)
        self.assertIsInstance(result.evidence, bytes)
        self.assertEqual(result.receipt.snapshot_id, 'room-shift')
        self.assertEqual(result.receipt.record_count, 4)
        self.assertEqual(result.receipt.total_bytes, 7)
        e0, e1, e2 = (PackAddress('E', slot) for slot in (0, 1, 2))
        east = read_room_snapshot(self.db, 'room-shift', 'east', RoomShelf(capture_packs(self.db, ('P', 'E'))))
        self.assertEqual(
            [(row.position, row.address, row.value, row.chain) for row in east.rows],
            [(0, e0, b'Ax', (e0,)), (1, e1, b'', (e1,)), (2, e0, b'Ax', (e0,)), (3, e2, b'Ax!', (e2,))],
        )
        q0, q1, q2, w0 = (PackAddress(*ref) for ref in (('Q', 0), ('Q', 1), ('Q', 2), ('W', 0)))
        west = read_room_snapshot(self.db, 'room-shift', 'west', RoomShelf(capture_packs(self.db, ('Q', 'W'))))
        self.assertEqual(
            [(row.position, row.address, row.value, row.chain) for row in west.rows],
            [(0, e0, b'Ax', (q0, w0)), (1, e1, b'', (q1,)),
             (2, e0, b'Ax', (q0, w0)), (3, e2, b'Ax!', (q2, q0, w0))],
        )
        self.assertEqual(
            [(row.revision.record, row.revision.revision) for row in west.rows],
            [('signal', 'shift'), ('missing', 'v1'), ('signal', 'shift'), ('signal', 'tail')],
        )
        self.assertEqual(west.total_bytes, 7)

    def test_reclaim_same_params_and_rebind_rejected(self):
        addresses, candidates = stage_rooms(self.db)
        result = publish_room_snapshot(self.db, 'room-shift', addresses, candidates)
        reordered = {
            'east': tuple(reversed(candidates['east'])),
            'west': tuple(reversed(candidates['west'])),
        }
        self.reopen()
        self.assertEqual(publish_room_snapshot(self.db, 'room-shift', list(addresses), reordered), result)
        with self.assertRaises(PublicationError):
            publish_room_snapshot(self.db, 'room-shift', addresses[::-1], candidates)
        narrowed = {'east': candidates['east'], 'west': candidates['west'][:-1]}
        with self.assertRaises(PublicationError):
            publish_room_snapshot(self.db, 'room-shift', addresses, narrowed)
        with self.assertRaises(PublicationError):
            publish_room_snapshot(self.db, 'room-shift', addresses, {'east': candidates['east']})
        with self.assertRaises(PublicationError):
            publish_room_snapshot(self.db, 'room-shift', addresses, dict(candidates, north=()))
        self.assertEqual(publish_room_snapshot(self.db, 'room-shift', addresses, candidates), result)

    def test_missing_donor_and_zero_prefix_delta_rejected(self):
        addresses, candidates = stage_rooms(self.db)
        west = tuple(item for item in candidates['west'] if item != PackAddress('W', 0))
        with self.assertRaises(PublicationError):
            publish_room_snapshot(self.db, 'no-donor', addresses, {'east': candidates['east'], 'west': west})
        self.assertIsNone(load_room_snapshot(self.db, 'no-donor'))
        e1 = PackAddress('E', 1)
        with self.assertRaises(PublicationError):
            publish_room_snapshot(self.db, 'zero-prefix', (e1,), {'east': (e1,), 'west': (PackAddress('Q', 3),)})
        self.assertIsNone(load_room_snapshot(self.db, 'zero-prefix'))
        control = publish_room_snapshot(self.db, 'empty-position', (e1,), {'east': (e1,), 'west': (PackAddress('Q', 1),)})
        self.assertEqual(control.receipt.record_count, 1)
        self.assertEqual(control.receipt.total_bytes, 0)

    def test_pack_registered_in_one_room_only(self):
        addresses, candidates = stage_rooms(self.db)
        moved = {'east': candidates['east'] + (PackAddress('Q', 1),), 'west': candidates['west']}
        with self.assertRaises(PublicationError):
            publish_room_snapshot(self.db, 'shared-pack', addresses, moved)
        self.assertIsNone(load_room_snapshot(self.db, 'shared-pack'))

    def test_candidates_must_be_real_sealed_slots(self):
        addresses, candidates = stage_rooms(self.db)
        for ghost in (PackAddress('Q', 9), PackAddress('Z', 0)):
            with self.subTest(ghost=ghost):
                rooms = {'east': candidates['east'], 'west': candidates['west'] + (ghost,)}
                with self.assertRaises(PublicationError):
                    publish_room_snapshot(self.db, 'ghost', addresses, rooms)
                self.assertIsNone(load_room_snapshot(self.db, 'ghost'))

    def test_empty_manifest_and_empty_candidates(self):
        result = publish_room_snapshot(self.db, 'empty-shift', (), {'east': (), 'west': ()})
        self.assertEqual(result.receipt.record_count, 0)
        self.assertEqual(result.receipt.total_bytes, 0)
        self.reopen()
        for room in ('east', 'west'):
            self.assertEqual(read_room_snapshot(self.db, 'empty-shift', room, RoomShelf({})).rows, ())
        self.assertEqual(
            publish_room_snapshot(self.db, 'empty-shift', (), {'east': [], 'west': []}), result,
        )


class RoomReadTests(LakeCase):
    def _publish(self):
        addresses, candidates = stage_rooms(self.db)
        result = publish_room_snapshot(self.db, 'room-shift', addresses, candidates)
        return result

    def test_late_donor_cannot_join_and_forged_pack_rejected(self):
        self._publish()
        stage_late_donor(self.db)
        packs = capture_packs(self.db, ('Q', 'W', 'L'))
        missing = RoomShelf({'Q': packs['Q'], 'W': None, 'L': packs['L']})
        with self.assertRaises(SnapshotReadError):
            read_room_snapshot(self.db, 'room-shift', 'west', missing)
        forged = RoomShelf({'Q': packs['Q'], 'W': packs['L']})
        with self.assertRaises(SnapshotReadError):
            read_room_snapshot(self.db, 'room-shift', 'west', forged)
        intact = read_room_snapshot(self.db, 'room-shift', 'west', RoomShelf({'Q': packs['Q'], 'W': packs['W']}))
        self.assertEqual([row.value for row in intact.rows], [b'Ax', b'', b'Ax', b'Ax!'])

    def test_partial_materials_within_certified_scope(self):
        self._publish()
        packs = capture_packs(self.db, ('P', 'E'))
        shelf = RoomShelf({'P': None, 'E': packs['E']})
        east = read_room_snapshot(self.db, 'room-shift', 'east', shelf)
        self.assertEqual([row.value for row in east.rows], [b'Ax', b'', b'Ax', b'Ax!'])
        forged = RoomShelf({'P': b'forged-pack', 'E': packs['E']})
        self.assertEqual(
            [row.value for row in read_room_snapshot(self.db, 'room-shift', 'east', forged).rows],
            [b'Ax', b'', b'Ax', b'Ax!'],
        )

    def test_cold_read_needs_no_main_store_and_survives_reopen(self):
        result = self._publish()
        east_packs = capture_packs(self.db, ('P', 'E'))
        west_packs = capture_packs(self.db, ('Q', 'W'))
        self.db.execute('DELETE FROM pack_payloads')
        west = read_room_snapshot(self.db, 'room-shift', 'west', RoomShelf(west_packs))
        self.assertEqual([row.value for row in west.rows], [b'Ax', b'', b'Ax', b'Ax!'])
        self.reopen()
        self.assertEqual(
            read_room_snapshot(self.db, 'room-shift', 'west', RoomShelf(west_packs)), west,
        )
        east = read_room_snapshot(self.db, 'room-shift', 'east', RoomShelf(east_packs))
        self.assertEqual([row.value for row in east.rows], [b'Ax', b'', b'Ax', b'Ax!'])
        self.assertEqual(
            publish_room_snapshot(self.db, 'room-shift', tuple(row.address for row in east.rows),
                                  {'east': (PackAddress('P', 0), PackAddress('E', 0), PackAddress('E', 1), PackAddress('E', 2)),
                                   'west': (PackAddress('Q', 0), PackAddress('Q', 1), PackAddress('Q', 2),
                                            PackAddress('W', 0), PackAddress('W', 1))}),
            result,
        )

    def test_unknown_snapshot_and_wrong_room_rejected(self):
        self._publish()
        with self.assertRaises(SnapshotReadError):
            read_room_snapshot(self.db, 'nope', 'west', RoomShelf({}))
        with self.assertRaises(SnapshotReadError):
            read_room_snapshot(self.db, 'room-shift', 'north', RoomShelf({}))

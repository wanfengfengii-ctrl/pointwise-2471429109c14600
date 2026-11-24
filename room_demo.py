"""双机房演示：签发双机房认证后关掉东房，西房仍凭端口实到包件读回 Ax。"""
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from maintenance.transactions import initialize_database, open_database
from packing.addresses import PackAddress
from reading.open import read_room_snapshot
from room_fixture import RoomShelf, capture_packs, stage_rooms
from snapshot.publish import publish_room_snapshot


def _show_rows(title, result):
    print(title)
    for row in result.rows:
        chain = ' → '.join(f'{address.pack_id}:{address.slot}' for address in row.chain)
        revision = f'{row.revision.record}/{row.revision.revision}'
        value = row.value.hex() or '(空)'
        print(f'  位置 {row.position} 原修订 {revision} 值 {value} 取证链 {chain}')


def main() -> int:
    with TemporaryDirectory(prefix='chengchuan-room-') as directory:
        path = Path(directory) / 'rooms.sqlite3'
        with closing(open_database(path)) as db:
            initialize_database(db)
            addresses, candidates = stage_rooms(db)
            published = publish_room_snapshot(db, 'room-shift', addresses, candidates)
            receipt = published.receipt
            print('双机房签发', receipt.snapshot_id, '记录数', receipt.record_count, '总字节', receipt.total_bytes)
            east = read_room_snapshot(db, 'room-shift', 'east', RoomShelf(capture_packs(db, ('P', 'E'))))
            _show_rows('东房领取（东房端口包 P、E）：', east)
        # 关掉东房并重开库：只经西房端口用已固定的实到包件领取。
        with closing(open_database(path)) as db:
            shelf = RoomShelf(capture_packs(db, ('Q', 'W')))
            west = read_room_snapshot(db, 'room-shift', 'west', shelf)
            _show_rows('东房停用后西房领取（西房端口包 Q、W）：', west)
            print('西房端口实际请求：', ', '.join(shelf.calls))
            values = tuple(row.value for row in west.rows)
            if values != (b'Ax', b'', b'Ax', b'Ax!'):
                raise AssertionError(f'unexpected west values {values!r}')
            expected_tail = (PackAddress('Q', 2), PackAddress('Q', 0), PackAddress('W', 0))
            if west.rows[-1].chain != expected_tail:
                raise AssertionError(f'unexpected west chain {west.rows[-1].chain!r}')
            print('西房读回 Ax：', [row.value.hex() for row in west.rows])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

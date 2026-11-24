"""核验入口：依次执行原回归、工作日样例和双机房交班样例，任一失败即非零退出。"""
import sys
import unittest

import room_fixture
import workday


def _regression_passed() -> bool:
    suite = unittest.defaultTestLoader.discover('tests')
    return unittest.TextTestRunner(verbosity=1).run(suite).wasSuccessful()


def main() -> int:
    if not _regression_passed():
        print('VERIFY_FAILED: 原回归未通过', file=sys.stderr)
        return 1
    if workday.main() != 0:
        print('VERIFY_FAILED: 工作日样例未通过', file=sys.stderr)
        return 1
    try:
        room_fixture.main()
    except Exception as exc:
        print(f'VERIFY_FAILED: 双机房交班样例未通过 {exc!r}', file=sys.stderr)
        return 1
    print('VERIFY_OK: 原回归、工作日样例与双机房核验全部通过')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

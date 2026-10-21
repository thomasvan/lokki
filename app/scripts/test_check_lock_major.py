import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import check_lock_major as clm  # noqa: E402

READER_8 = 'MANAGEMENT_MAJOR = "8"          # the `/v8/` route prefix this module uses\n'
READER_9 = 'MANAGEMENT_MAJOR = "9"\n'


class LockMajorTests(unittest.TestCase):
    def test_the_lock_and_the_reader_speak_the_same_major(self):
        # (label, lock, reader, refused)
        rows = [
            ("the same major", {"version": "8.0.23"}, READER_8, False),
            ("a minor bump", {"version": "8.1.0"}, READER_8, False),
            ("a major bump the reader does not speak", {"version": "9.0.0"}, READER_8, True),
            ("a major bump with the reader moved", {"version": "9.0.0"}, READER_9, False),
            ("a reader ahead of the lock", {"version": "8.0.23"}, READER_9, True),
            ("a reader without the constant", {"version": "8.0.23"}, "# nothing here\n", True),
        ]
        for label, lock, reader, refused in rows:
            with self.subTest(label):
                problem = clm.refuse(lock, reader)
                self.assertEqual(problem is not None, refused, problem)

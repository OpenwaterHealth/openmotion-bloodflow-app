"""Write or read an encrypted database the way omotion.db_open does (#682).

    python roundtrip.py write <db>   # create it with this interpreter's sqlcipher3
    python roundtrip.py read <db>    # open it, check every page and the rows

The sqlcipher3 wheel workflow writes with the PyPI wheel and reads with the
rebuilt one, then the reverse, so a database crosses the upgrade (and a
rollback) in both directions. Same open sequence as the SDK: a raw 32-byte
key, set before any page is touched, then WAL. Standard library only, so it
runs in a bare venv. The key is a fixed test value, never a real one.
"""

import sys

from sqlcipher3 import dbapi2

KEY = "0f1e2d3c4b5a69788796a5b4c3d2e1f00112233445566778899aabbccddeeff"
ROWS = [(i, f"session-{i}", i * 0.25) for i in range(1, 501)]


def connect(path):
    conn = dbapi2.connect(path)
    conn.execute(f"PRAGMA key = \"x'{KEY}'\"")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def write(path):
    conn = connect(path)
    conn.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY, name TEXT, bfi REAL)")
    conn.executemany("INSERT INTO sessions VALUES (?, ?, ?)", ROWS)
    conn.commit()
    conn.close()


def read(path):
    conn = connect(path)
    # Every page's HMAC, then SQLite's own structural check.
    assert conn.execute("PRAGMA cipher_integrity_check").fetchall() == [], "HMAC check failed"
    assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)], "integrity check failed"
    assert conn.execute("SELECT * FROM sessions ORDER BY id").fetchall() == ROWS, "rows differ"
    print(f"{path}: read {len(ROWS)} rows with SQLCipher "
          f"{conn.execute('PRAGMA cipher_version').fetchone()[0]}")
    conn.close()


if __name__ == "__main__":
    {"write": write, "read": read}[sys.argv[1]](sys.argv[2])

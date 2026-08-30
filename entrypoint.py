#!/usr/bin/env python3
"""Container entrypoint: drop privileges to PUID:PGID, then run main.py.

Reads PUID/PGID from the environment (default 1000). When started as root it
makes the data directory (and the SQLite file) writable for that uid:gid,
drops privileges and execs main.py. Extra CLI arguments are forwarded, so
`docker run ... python entrypoint.py --force` works.
"""

import os
import sys

PUID = int(os.getenv("PUID", "1000"))
PGID = int(os.getenv("PGID", "1000"))
MAIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")


def main():
    if os.geteuid() == 0:
        for path in ("/app/data", os.getenv("DB_PATH", "/app/data/betterposters.db")):
            try:
                if os.path.exists(path):
                    os.chown(path, PUID, PGID)
            except OSError:
                pass
        os.setgroups([])
        os.setgid(PGID)
        os.setuid(PUID)
    os.execv(sys.executable, [sys.executable, MAIN] + sys.argv[1:])


if __name__ == "__main__":
    main()

from __future__ import annotations

import os

from desktop.server.launcher import main


if __name__ == "__main__":
    os.environ.pop("OPENEVO_NATIVE_EXECUTABLE_FD", None)
    raise SystemExit(main())

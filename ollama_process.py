"""Linux launcher: terminate the owned Ollama server if the worker dies, even via SIGKILL."""

import ctypes
import os
import signal
import sys


def main() -> None:
    parent_pid = int(sys.argv[1])
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
        raise OSError(ctypes.get_errno(), "Cannot set Ollama parent death signal")
    if os.getppid() != parent_pid:  # Close the race between Popen and prctl.
        raise SystemExit(1)
    os.execvp(sys.argv[2], sys.argv[2:])


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# Record a driver script inside a fixed 100x40 pseudo-terminal, so the asciicast
# has a stable size regardless of the controlling terminal (asciinema otherwise
# falls back to 80x24 when there is no tty). Driver + cast come from the env.
import os, pty, fcntl, termios, struct, sys
cols, rows = 100, 40
drv = os.environ["DRIVER"]; cast = os.environ["CAST"]
argv = [sys.executable, "-m", "asciinema", "rec", "--overwrite", "-c", f"bash {drv}", cast]
pid, fd = pty.fork()
if pid == 0:
    os.environ["TERM"] = "xterm-256color"
    os.execvp(argv[0], argv)
else:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    while True:
        try: data = os.read(fd, 4096)
        except OSError: break
        if not data: break
    os.waitpid(pid, 0)

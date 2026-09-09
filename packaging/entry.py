"""Entry point for the packaged executable.

The .exe is both halves of the program at once: the CLI it has always been,
and the local interface that the CLI's ``gui`` command starts. Which one a
person wants is answered by how they launched it. A double-click from
Explorer arrives with no arguments and no intent to type any, so that opens
the interface. A shell invocation with arguments means the CLI.

Running the interface directly, rather than through the CLI group, keeps
``lemon-zest.exe`` usable as a shortcut target without a stray ``gui`` in
the command line.
"""
import multiprocessing
import sys


def main():
    # A frozen build re-executes itself for every child process; without
    # this, anything that spawns one would relaunch the whole interface.
    multiprocessing.freeze_support()

    from lemonzest.cli import main as cli_main

    if len(sys.argv) > 1:
        return cli_main()

    sys.argv = [sys.argv[0], "gui"]
    return cli_main()


if __name__ == "__main__":
    main()

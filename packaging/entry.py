"""Entry point for the packaged executable.

The .exe is both halves of the program at once: the CLI it has always been,
and the local interface that the CLI's ``gui`` command starts. Which one a
person wants is answered by how they launched it. A double-click from
Explorer arrives with no arguments and no intent to type any, so that opens
the interface. A shell invocation with arguments means the CLI.

Running the interface directly, rather than through the CLI group, keeps
``lemon-zest.exe`` usable as a shortcut target without a stray ``gui`` in
the command line.

It is also yt-dlp. A frozen build has no ``python -m yt_dlp`` to call - the
interpreter inside it takes no ``-m`` - so the binary re-executes itself
with ``--yt-dlp`` in front and hands the rest of the command line to the
yt-dlp that is bundled with it. That keeps downloads running in their own
process, which is what lets the interface stream the log and survive a
yt-dlp that dies, without needing a yt-dlp installed on the machine.
"""
import multiprocessing
import sys

YTDLP_FLAG = "--yt-dlp"


def run_ytdlp():
    """Be yt-dlp for this process. Never returns."""
    import yt_dlp

    sys.argv = ["yt-dlp"] + sys.argv[2:]
    sys.exit(yt_dlp.main(sys.argv[1:]))


def main():
    # A frozen build re-executes itself for every child process; without
    # this, anything that spawns one would relaunch the whole interface.
    multiprocessing.freeze_support()

    # Checked before anything else is imported: this process is not the
    # music app at all, and click must never see these arguments.
    if len(sys.argv) > 1 and sys.argv[1] == YTDLP_FLAG:
        return run_ytdlp()

    from lemonzest import bundled

    bundled.install()

    from lemonzest.cli import main as cli_main

    if len(sys.argv) > 1:
        return cli_main()

    sys.argv = [sys.argv[0], "gui"]
    return cli_main()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Synthesize MEMORY_ACTIVE.md from system + personal memory files. Checks mtimes."""
import argparse
import os


def needs_regen(system_file, user_file, output_file):
    """Return True if the output file needs regeneration based on source mtimes."""
    if not os.path.exists(output_file):
        return True
    out_mtime = os.path.getmtime(output_file)
    return any(
        os.path.exists(f) and os.path.getmtime(f) > out_mtime
        for f in [system_file, user_file]
    )


def synthesize(system_file, user_file, output_file):
    """Combine system and user memory files into output. Returns True if regenerated."""
    if not needs_regen(system_file, user_file, output_file):
        return False
    parts = []
    for f in [system_file, user_file]:
        if os.path.exists(f):
            parts.append(open(f).read().strip())
    os.makedirs(os.path.dirname(output_file) or ".", exist_ok=True)
    with open(output_file, "w") as fh:
        fh.write("\n\n---\n\n".join(parts) + "\n")
    return True


if __name__ == "__main__":
    p = argparse.ArgumentParser(
        description="Synthesize MEMORY_ACTIVE.md from system + personal memory files"
    )
    p.add_argument("--system-file", required=True, help="Path to system memory file")
    p.add_argument("--user-file", required=True, help="Path to user/agent memory file")
    p.add_argument("--output-file", required=True, help="Path to output combined file")
    args = p.parse_args()
    result = synthesize(args.system_file, args.user_file, args.output_file)
    print("Regenerated." if result else "Up to date.")

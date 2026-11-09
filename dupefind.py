#!/usr/bin/env python3
"""dupefind - find duplicate files and see how much space they waste.

Compares files by content (size first, then a quick look at the start, then a full SHA-256), so two files are
only called duplicates when every byte matches. Hard links to the same data are not counted as duplicates
(they take no extra space) and symlinks are never followed. dupefind only REPORTS: it deletes nothing. With
--rm-script it writes a review script in which every removal is commented out, for you to read and enable.
Standard library only. Exit codes: 0 = no duplicates, 1 = duplicates found, 2 = usage error.
"""
import argparse
import fnmatch
import hashlib
import json
import os
import re
import shlex
import sys

__version__ = "1.0.0"

CHUNK = 1 << 20
HEAD = 64 * 1024
SIZE_UNITS = {"b": 1, "kb": 1000, "mb": 1000 ** 2, "gb": 1000 ** 3, "kib": 1024, "mib": 1024 ** 2, "gib": 1024 ** 3,
              "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}


def parse_size(text):
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([a-z]*)\s*", str(text).lower())
    if not m or (m.group(2) and m.group(2) not in SIZE_UNITS):
        raise ValueError("not a size: %r (use e.g. 1, 10KB, 5MiB)" % (text,))
    return int(float(m.group(1)) * SIZE_UNITS.get(m.group(2) or "b"))


def human(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return "%d B" % n if unit == "B" else "%.1f %s" % (n, unit)
        n /= 1000.0


def walk(paths, min_size=1, exclude=(), same_device=False):
    """Regular files under ``paths`` as (path, size, (dev, inode), mtime). Symlinks and unreadable entries are skipped."""
    seen_roots = set()
    for root in paths:
        root_dev = os.stat(root).st_dev
        if os.path.isfile(root):
            candidates = [(os.path.dirname(root) or ".", [], [os.path.basename(root)])]
        else:
            candidates = os.walk(root, followlinks=False)
        for dirpath, dirs, names in candidates:
            dirs[:] = sorted(d for d in dirs if not _excluded(os.path.join(dirpath, d), d, exclude)
                             and not (same_device and _device(os.path.join(dirpath, d)) != root_dev))
            for name in sorted(names):
                full = os.path.join(dirpath, name)
                if _excluded(full, name, exclude):
                    continue
                try:
                    st = os.lstat(full)
                except OSError:
                    continue
                if not os.path.isfile(full) or os.path.islink(full) or st.st_size < min_size:
                    continue
                real = os.path.realpath(full)
                if real in seen_roots:
                    continue
                seen_roots.add(real)
                yield full, st.st_size, (st.st_dev, st.st_ino), st.st_mtime


def _device(path):
    try:
        return os.lstat(path).st_dev
    except OSError:
        return None


def _excluded(full, name, patterns):
    return any(fnmatch.fnmatch(name, p) or fnmatch.fnmatch(full, p) for p in patterns)


def _hash(path, limit=None):
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            if limit is not None:
                h.update(fh.read(limit))
            else:
                for chunk in iter(lambda: fh.read(CHUNK), b""):
                    h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def find_duplicates(files):
    """Group identical files: -> list of groups, each a list of (path, size, mtime), biggest waste first."""
    by_size = {}
    for path, size, inode, mtime in files:
        by_size.setdefault(size, {}).setdefault(inode, (path, size, mtime))  # hard links count once
    groups = []
    for size, inodes in by_size.items():
        members = list(inodes.values())
        if len(members) < 2:
            continue
        by_head = {}
        for m in members:
            digest = _hash(m[0], HEAD) if size > HEAD else "-"
            if digest is not None:
                by_head.setdefault(digest, []).append(m)
        for same_head in by_head.values():
            if len(same_head) < 2:
                continue
            by_full = {}
            for m in same_head:
                digest = _hash(m[0])
                if digest is not None:
                    by_full.setdefault(digest, []).append(m)
            for digest, same in by_full.items():
                if len(same) >= 2:
                    groups.append(sorted(same, key=lambda m: (m[2], m[0])))
    groups.sort(key=lambda g: (-g[0][1] * (len(g) - 1), g[0][0]))
    return groups


def choose_keeper(group, keep):
    """Index of the file to keep: oldest/newest by modification time, or the shortest path."""
    if keep == "newest":
        return max(range(len(group)), key=lambda i: (group[i][2], -len(group[i][0])))
    if keep == "shortest-path":
        return min(range(len(group)), key=lambda i: (len(group[i][0]), group[i][0]))
    return min(range(len(group)), key=lambda i: (group[i][2], group[i][0]))


def wasted(groups):
    return sum(g[0][1] * (len(g) - 1) for g in groups)


def has_control_chars(path):
    return any(ord(c) < 32 or ord(c) == 127 for c in path)


def printable(path):
    """The path with control characters shown as escapes, safe to put in a comment."""
    return "".join(c if ord(c) >= 32 and ord(c) != 127 else "\\x%02x" % ord(c) for c in path)


def rm_script(groups, keep):
    """A shell script that only *contains* the removals, every one commented out. Paths are shell-quoted."""
    lines = ["#!/bin/sh",
             "# Generated by dupefind. Nothing below runs until you remove the leading '# ' from a line.",
             "# For each group one file is kept (%s); the other copies are listed as removals." % keep, ""]
    for n, group in enumerate(groups, 1):
        k = choose_keeper(group, keep)
        lines.append("# group %d: %s each, %d copies" % (n, human(group[0][1]), len(group)))
        lines.append("# keep: %s" % printable(group[k][0]))
        for i, (path, _s, _m) in enumerate(group):
            if i == k:
                continue
            if has_control_chars(path):  # a newline in a name could end the comment and start a live command
                lines.append("# SKIPPED (file name contains control characters, deal with it by hand): %s" % printable(path))
            else:
                lines.append("# rm -- %s" % shlex.quote(path))
        lines.append("")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Find duplicate files by content. Reports only; deletes nothing.")
    ap.add_argument("paths", nargs="*", default=["."], help="folders or files to search (default: .)")
    ap.add_argument("--min-size", default="1", help="ignore files smaller than this (default 1 byte; e.g. 100KB)")
    ap.add_argument("--exclude", action="append", default=[], metavar="GLOB", help="skip names or paths matching this glob (repeatable)")
    ap.add_argument("--one-file-system", action="store_true", help="do not cross into other filesystems")
    ap.add_argument("--keep", choices=("oldest", "newest", "shortest-path"), default="oldest",
                    help="which copy --rm-script and the report mark as the one to keep (default oldest)")
    ap.add_argument("--rm-script", metavar="FILE", help="write a review script with every removal commented out")
    ap.add_argument("--top", type=int, default=0, help="show only the N groups that waste the most space")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--version", action="version", version=__version__)
    args = ap.parse_args(argv)
    try:
        min_size = max(1, parse_size(args.min_size))
        for p in args.paths:
            os.stat(p)
        files = list(walk(args.paths, min_size, args.exclude, args.one_file_system))
    except (ValueError, OSError) as exc:
        print("dupefind: %s" % exc, file=sys.stderr)
        return 2
    groups = find_duplicates(files)
    total = wasted(groups)
    shown = groups[:args.top] if args.top else groups
    if args.rm_script:
        if os.path.exists(args.rm_script):
            print("dupefind: %s already exists; not overwriting" % args.rm_script, file=sys.stderr)
            return 2
        fd = os.open(args.rm_script, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", errors="replace") as fh:
            fh.write(rm_script(shown, args.keep))
    if args.json:
        print(json.dumps({
            "files_scanned": len(files), "groups": len(groups), "duplicate_files": sum(len(g) - 1 for g in groups),
            "wasted_bytes": total,
            "result": [{"size": g[0][1], "wasted": g[0][1] * (len(g) - 1), "keep": g[choose_keeper(g, args.keep)][0],
                        "copies": [m[0] for m in g]} for g in shown]}, indent=2))
    else:
        for g in shown:
            k = choose_keeper(g, args.keep)
            print("%s each, %d copies (%s wasted)" % (human(g[0][1]), len(g), human(g[0][1] * (len(g) - 1))))
            for i, (path, _s, _m) in enumerate(g):
                print("  %s %s" % ("keep" if i == k else "    ", path))
        print("dupefind: %d file(s) scanned, %d duplicate group(s), %s could be reclaimed%s" % (
            len(files), len(groups), human(total), "; review script written to %s" % args.rm_script if args.rm_script else ""),
            file=sys.stderr)
    return 1 if groups else 0


if __name__ == "__main__":
    sys.exit(main())

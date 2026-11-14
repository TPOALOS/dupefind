# dupefind

**Find duplicate files and see how much space they waste.** Two files are called duplicates only when every byte matches
(compared by size, then a quick look at the start, then a full SHA-256). dupefind **reports only**: it deletes nothing.

```
$ dupefind photos backup-trip.mov
3.0 MB each, 3 copies (6.0 MB wasted)
  keep photos/trip.mov
       photos/trip-copy.mov
       ./backup-trip.mov
40.0 KB each, 2 copies (40.0 KB wasted)
  keep photos/a.jpg
       photos/a-edited-copy.jpg
dupefind: 5 file(s) scanned, 2 duplicate group(s), 6.0 MB could be reclaimed
```

One file, standard library only.

## Install and use

```bash
pip install git+https://github.com/TPOALOS/dupefind          # or copy dupefind.py
dupefind ~/Downloads ~/Documents
dupefind . --min-size 1MB --top 20                   # the 20 groups that waste the most space
dupefind /data --exclude node_modules --exclude '*.log' --one-file-system
dupefind . --json
dupefind . --keep newest --rm-script review.sh       # writes a script to READ, with every removal commented out
```

## Safe by design

- **Nothing is ever deleted.** `--rm-script FILE` writes a shell script in which **every removal line starts with `# `**: you
  read it, un-comment what you want, then run it. The file is created with mode `600` and never overwrites an existing one.
- Paths in the script are shell-quoted. A file name containing a newline or other control character could end a comment and
  start a live command, so such files are listed as `SKIPPED` for you to handle by hand instead of getting a removal line.
- **Hard links** to the same data are not duplicates (they take no extra space) and are counted once. **Symlinks are never
  followed**, so a link pointing outside your folder cannot pull other files into the report or loop forever.
- Unreadable files are skipped quietly. Empty files are ignored (use `--min-size` to raise the bar further).
- `--keep oldest|newest|shortest-path` only chooses which copy the report marks `keep` and which the script leaves alone.

Exit codes: `0` no duplicates, `1` duplicates found, `2` usage error.

## Limits

It compares whole files, so two photos that merely *look* alike (a resized or re-saved copy) are different files and are not
reported. Very large trees take time: every candidate with a matching size and start is read in full.

## Tests

```bash
python3 -m unittest -v
```

## License

MIT (c) TPOALOS

---

**More tools by [TPOALOS](https://github.com/TPOALOS):** [backupcheck](https://github.com/TPOALOS/backupcheck) · [jobwatch](https://github.com/TPOALOS/jobwatch) · [diskwatch](https://github.com/TPOALOS/diskwatch) · [all 51 tools](https://tpoalos.github.io)

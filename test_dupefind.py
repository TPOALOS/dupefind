import contextlib
import io
import json
import os
import stat
import tempfile
import unittest

import dupefind as df


class Base(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.d = self._t.name

    def tearDown(self):
        self._t.cleanup()

    def put(self, rel, data, mtime=None):
        p = os.path.join(self.d, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(data)
        if mtime is not None:
            os.utime(p, (mtime, mtime))
        return p

    def groups(self, **kw):
        files = list(df.walk([self.d], kw.pop("min_size", 1), kw.pop("exclude", ()), kw.pop("same_device", False)))
        return df.find_duplicates(files)

    def names(self, group):
        return sorted(os.path.relpath(m[0], self.d) for m in group)


class HelperTests(unittest.TestCase):
    def test_parse_size(self):
        self.assertEqual(df.parse_size("1"), 1)
        self.assertEqual(df.parse_size("100KB"), 100000)
        self.assertEqual(df.parse_size("2MiB"), 2 * 1024 ** 2)
        for bad in ("", "x", "5 parsecs", "-1"):
            with self.assertRaises(ValueError):
                df.parse_size(bad)

    def test_human(self):
        self.assertEqual(df.human(0), "0 B")
        self.assertEqual(df.human(1500), "1.5 KB")
        self.assertEqual(df.human(3 * 10 ** 9), "3.0 GB")


class DetectionTests(Base):
    def test_identical_files_are_grouped(self):
        self.put("a.txt", b"same content")
        self.put("sub/b.txt", b"same content")
        self.put("sub/c.txt", b"same content")
        self.put("other.txt", b"different!!!")
        g = self.groups()
        self.assertEqual(len(g), 1)
        self.assertEqual(self.names(g[0]), ["a.txt", "sub/b.txt", "sub/c.txt"])

    def test_same_size_different_content_is_not_a_duplicate(self):
        self.put("a.bin", b"A" * 100)
        self.put("b.bin", b"B" * 100)
        self.assertEqual(self.groups(), [])

    def test_same_start_different_end_is_not_a_duplicate(self):
        head = b"x" * (df.HEAD + 1000)
        self.put("a.bin", head + b"ending-one")
        self.put("b.bin", head + b"ending-two")
        self.assertEqual(self.groups(), [])

    def test_same_start_and_end_but_different_middle(self):
        big = df.HEAD * 3
        a = b"s" * df.HEAD + b"1" + b"m" * (big - df.HEAD - 1)
        b = b"s" * df.HEAD + b"2" + b"m" * (big - df.HEAD - 1)
        self.put("a.bin", a)
        self.put("b.bin", b)
        self.put("c.bin", a)
        g = self.groups()
        self.assertEqual([self.names(x) for x in g], [["a.bin", "c.bin"]])

    def test_large_identical_files(self):
        data = os.urandom(df.HEAD * 2 + 123)
        self.put("one.bin", data)
        self.put("two.bin", data)
        self.assertEqual(len(self.groups()), 1)

    def test_separate_groups_sorted_by_wasted_space(self):
        self.put("small1", b"s" * 10)
        self.put("small2", b"s" * 10)
        self.put("big1", b"B" * 5000)
        self.put("big2", b"B" * 5000)
        self.put("big3", b"B" * 5000)
        g = self.groups()
        self.assertEqual([x[0][1] for x in g], [5000, 10])
        self.assertEqual(df.wasted(g), 5000 * 2 + 10)

    def test_hard_links_are_not_duplicates(self):
        a = self.put("a.txt", b"shared data")
        os.link(a, os.path.join(self.d, "hardlink.txt"))
        self.assertEqual(self.groups(), [])

    def test_hard_link_plus_real_copy(self):
        a = self.put("a.txt", b"shared data")
        os.link(a, os.path.join(self.d, "hardlink.txt"))
        self.put("copy.txt", b"shared data")
        g = self.groups()
        self.assertEqual(len(g), 1)
        self.assertEqual(len(g[0]), 2)  # the hard-linked pair counts once
        self.assertEqual(df.wasted(g), len(b"shared data"))

    def test_symlinks_are_never_followed_or_counted(self):
        a = self.put("a.txt", b"payload")
        os.symlink(a, os.path.join(self.d, "link.txt"))
        os.symlink(self.d, os.path.join(self.d, "loop"))
        self.assertEqual(self.groups(), [])
        with tempfile.TemporaryDirectory() as outside:
            with open(os.path.join(outside, "secret.txt"), "wb") as fh:
                fh.write(b"payload")
            os.symlink(outside, os.path.join(self.d, "to-outside"))
            self.assertEqual(self.groups(), [])

    def test_empty_files_ignored_by_default(self):
        self.put("e1", b"")
        self.put("e2", b"")
        self.assertEqual(self.groups(), [])

    def test_min_size(self):
        self.put("a", b"x" * 50)
        self.put("b", b"x" * 50)
        self.assertEqual(self.groups(min_size=51), [])
        self.assertEqual(len(self.groups(min_size=50)), 1)

    def test_exclude_by_name_and_path(self):
        self.put("keep/a.log", b"log data")
        self.put("keep/b.log", b"log data")
        self.put("node_modules/x/c.log", b"log data")
        g = self.groups(exclude=["node_modules"])
        self.assertEqual(self.names(g[0]), ["keep/a.log", "keep/b.log"])
        self.assertEqual(self.groups(exclude=["*.log"]), [])

    def test_unreadable_files_are_skipped(self):
        a = self.put("a.txt", b"content")
        self.put("b.txt", b"content")
        locked = self.put("c.txt", b"content")
        os.chmod(locked, 0)
        try:
            g = self.groups()
            self.assertEqual(self.names(g[0]), ["a.txt", "b.txt"])
        finally:
            os.chmod(locked, 0o600)

    def test_same_file_given_twice_is_scanned_once(self):
        a = self.put("a.txt", b"content")
        files = list(df.walk([self.d, a]))
        self.assertEqual(len(files), 1)

    def test_nothing_is_ever_modified_or_removed(self):
        self.put("a.txt", b"same")
        self.put("b.txt", b"same")
        before = sorted(os.listdir(self.d))
        self.groups()
        self.assertEqual(sorted(os.listdir(self.d)), before)


class KeeperTests(unittest.TestCase):
    GROUP = [("/x/long/path/new.txt", 5, 300.0), ("/x/a.txt", 5, 100.0), ("/x/bb/old-copy.txt", 5, 200.0)]

    def test_oldest(self):
        self.assertEqual(self.GROUP[df.choose_keeper(self.GROUP, "oldest")][0], "/x/a.txt")

    def test_newest(self):
        self.assertEqual(self.GROUP[df.choose_keeper(self.GROUP, "newest")][0], "/x/long/path/new.txt")

    def test_shortest_path(self):
        self.assertEqual(self.GROUP[df.choose_keeper(self.GROUP, "shortest-path")][0], "/x/a.txt")

    def test_ties_are_deterministic(self):
        g = [("/b", 1, 1.0), ("/a", 1, 1.0)]
        self.assertEqual(g[df.choose_keeper(g, "oldest")][0], "/a")


class ScriptTests(Base):
    def test_every_removal_is_commented_out_and_quoted(self):
        g = [[("/data/a b/it's.txt", 10, 1.0), ("/data/$(touch pwned)/copy.txt", 10, 2.0), ("/data/x;rm -rf ~/y.txt", 10, 3.0)]]
        script = df.rm_script(g, "oldest")
        for line in script.splitlines():
            self.assertTrue(line.startswith("#") or not line.strip(), line)
        self.assertIn("# rm -- '/data/$(touch pwned)/copy.txt'", script)
        self.assertIn("# rm -- '/data/x;rm -rf ~/y.txt'", script)
        self.assertNotIn("# rm -- '/data/a b/it's.txt'", script)  # the keeper is never listed as a removal
        self.assertIn("# keep: /data/a b/it's.txt", script)

    def test_newlines_in_names_cannot_inject_lines(self):
        g = [[("/a/ok.txt", 1, 1.0), ("/a/evil\nrm -rf ~\n.txt", 1, 2.0), ("/a/cr\rname.txt", 1, 3.0)]]
        script = df.rm_script(g, "oldest")
        for line in script.splitlines():
            self.assertTrue(line.startswith("#") or not line.strip(), repr(line))
        self.assertEqual(script.count("SKIPPED"), 2)
        self.assertNotIn("# rm -- '/a/evil", script)
        self.assertIn("\\x0a", script)

    def test_a_keeper_with_control_characters_cannot_inject_either(self):
        g = [[("/a/bad\nrm -rf ~\n.txt", 1, 1.0), ("/a/ok.txt", 1, 2.0)]]
        for line in df.rm_script(g, "oldest").splitlines():
            self.assertTrue(line.startswith("#") or not line.strip(), repr(line))

    def test_printable_escapes_only_control_characters(self):
        self.assertEqual(df.printable("/a/b c/ü.txt"), "/a/b c/ü.txt")
        self.assertEqual(df.printable("x\ny\x7f"), "x\\x0ay\\x7f")


def run_main(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = df.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class CliTests(Base):
    def setUp(self):
        super().setUp()
        self.put("a/one.txt", b"duplicate payload", mtime=1000)
        self.put("b/two.txt", b"duplicate payload", mtime=2000)
        self.put("c/unique.txt", b"i am unique here")

    def test_text_report_and_exit_code(self):
        code, out, err = run_main(self.d)
        self.assertEqual(code, 1)
        self.assertIn("2 copies", out)
        self.assertIn("keep ", out)
        self.assertEqual(out.count("keep "), 1)
        self.assertIn("1 duplicate group(s)", err)
        self.assertNotIn("unique.txt", out)

    def test_no_duplicates_exit_zero(self):
        os.remove(os.path.join(self.d, "b", "two.txt"))
        code, out, _ = run_main(self.d)
        self.assertEqual((code, out), (0, ""))

    def test_json(self):
        code, out, _ = run_main(self.d, "--json", "--keep", "newest")
        data = json.loads(out)
        self.assertEqual((code, data["groups"], data["duplicate_files"], data["wasted_bytes"], data["files_scanned"]),
                         (1, 1, 1, len(b"duplicate payload"), 3))
        self.assertTrue(data["result"][0]["keep"].endswith("two.txt"))
        self.assertEqual(len(data["result"][0]["copies"]), 2)

    def test_rm_script_is_private_and_not_overwritten(self):
        script = os.path.join(self.d, "..", "review-%d.sh" % os.getpid())
        try:
            code, _, err = run_main(self.d, "--rm-script", script)
            self.assertEqual(code, 1)
            self.assertEqual(stat.S_IMODE(os.stat(script).st_mode), 0o600)
            with open(script) as fh:
                text = fh.read()
            self.assertIn("# rm -- ", text)
            self.assertIn("two.txt", text)  # oldest (one.txt) is kept
            code, _, err = run_main(self.d, "--rm-script", script)
            self.assertEqual(code, 2)
            self.assertIn("not overwriting", err)
        finally:
            if os.path.exists(script):
                os.remove(script)

    def test_top(self):
        self.put("d/big1", b"B" * 9000)
        self.put("d/big2", b"B" * 9000)
        _, out, _ = run_main(self.d, "--top", "1", "--json")
        data = json.loads(out)
        self.assertEqual((data["groups"], len(data["result"]), data["result"][0]["size"]), (2, 1, 9000))

    def test_min_size_and_exclude(self):
        self.assertEqual(run_main(self.d, "--min-size", "1MB")[0], 0)
        self.assertEqual(run_main(self.d, "--exclude", "two.txt")[0], 0)

    def test_usage_errors(self):
        self.assertEqual(run_main("/nonexistent/path")[0], 2)
        self.assertEqual(run_main(self.d, "--min-size", "lots")[0], 2)

    def test_default_path_is_the_current_directory(self):
        cwd = os.getcwd()
        try:
            os.chdir(self.d)
            self.assertEqual(run_main()[0], 1)
        finally:
            os.chdir(cwd)

    def test_single_files_as_arguments(self):
        a = os.path.join(self.d, "a", "one.txt")
        b = os.path.join(self.d, "b", "two.txt")
        code, out, _ = run_main(a, b)
        self.assertEqual(code, 1)
        self.assertIn("2 copies", out)

    def test_one_file_system_flag_runs(self):
        self.assertEqual(run_main(self.d, "--one-file-system")[0], 1)


if __name__ == "__main__":
    unittest.main()

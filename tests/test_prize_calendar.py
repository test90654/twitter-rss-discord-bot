import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import prize_calendar as calendar


class TwitterResponseTests(unittest.TestCase):
    def invoke(self, stdout, stderr="", returncode=0):
        result = subprocess.CompletedProcess([], returncode, stdout, stderr)
        with patch.object(calendar, "check_twitter_secrets"), patch.object(calendar.subprocess, "run", return_value=result):
            return calendar.run_twitter_cli(["user-posts", "amagi_amuse"])

    def test_warning_on_stderr_does_not_invalidate_success(self):
        posts = [{"id": "123", "text": "hello"}]
        self.assertEqual(self.invoke(json.dumps({"ok": True, "data": posts}), "WARNING ClientTransaction"), posts)

    def test_error_envelope_is_failure_even_with_exit_zero(self):
        with self.assertRaisesRegex(RuntimeError, "not_found"):
            self.invoke('{"ok": false, "error": {"code": "not_found"}}')

    def test_error_json_takes_priority_over_warning(self):
        with self.assertRaisesRegex(RuntimeError, "HTTP 404"):
            self.invoke('{"ok": false, "error": {"message": "HTTP 404"}}', "WARNING ClientTransaction", 1)

    def test_missing_or_invalid_response_is_failure(self):
        for value in ("", "not json", "{}", "null", '{"data": null}', '{"data": [42]}', '{"data": [{}]}'):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                self.invoke(value)

    def test_explicit_empty_list_is_valid(self):
        self.assertEqual(self.invoke('{"ok": true, "data": []}'), [])

    def test_nonzero_exit_is_failure_even_with_valid_data(self):
        with self.assertRaises(RuntimeError):
            self.invoke('{"data": []}', "failed", 1)


class PostSelectionTests(unittest.TestCase):
    def setUp(self):
        self.posts = [
            {"id": "old", "text": calendar.ARRIVAL_HEADING, "created_at": "2026-11-01T00:00:00Z"},
            {"id": "new", "text": calendar.ARRIVAL_HEADING, "created_at": "2026-11-03T00:00:00Z"},
        ]

    def test_timeline_matches_skip_broken_search_and_sort_by_time(self):
        with patch.object(calendar, "recent_posts", return_value=self.posts), patch.object(calendar, "run_twitter_cli") as cli:
            result = calendar.search_posts(calendar.ARRIVAL_HEADING, 1)
        self.assertEqual([p["id"] for p in result], ["new"])
        cli.assert_not_called()

    def test_search_can_recover_when_timeline_fails(self):
        with patch.object(calendar, "recent_posts", side_effect=RuntimeError("timeline failed")), patch.object(calendar, "run_twitter_cli", return_value=self.posts):
            self.assertEqual(calendar.search_posts(calendar.ARRIVAL_HEADING, 40), self.posts)

    def test_both_sources_fail_instead_of_reporting_zero(self):
        for timeline in (None, [], [{"id": "other", "text": "unrelated"}]):
            with self.subTest(timeline=timeline):
                kwargs = {"side_effect": RuntimeError("timeline failed")} if timeline is None else {"return_value": timeline}
                with patch.object(calendar, "recent_posts", **kwargs), patch.object(calendar, "run_twitter_cli", side_effect=RuntimeError("search failed")):
                    with self.assertRaises(RuntimeError):
                        calendar.search_posts(calendar.ARRIVAL_HEADING, 40)

    def test_successful_empty_search_is_valid(self):
        with patch.object(calendar, "recent_posts", return_value=[]), patch.object(calendar, "run_twitter_cli", return_value=[]):
            self.assertEqual(calendar.search_posts(calendar.ARRIVAL_HEADING, 40), [])

    def test_recent_posts_are_cached_for_both_queries(self):
        with patch.object(calendar, "_recent_posts", None), patch.object(calendar, "run_twitter_cli", return_value=self.posts) as cli:
            self.assertEqual(calendar.recent_posts(), self.posts)
            self.assertEqual(calendar.recent_posts(), self.posts)
        cli.assert_called_once()


class ArrivalDiagnosticsTests(unittest.TestCase):
    def test_records_valid_rows_and_explains_skips(self):
        valid = {"id": "1", "text": "【新プライズ情報】\nテスト商品\n入荷いたしました！", "created_at": "2026-11-03T00:00:00Z"}
        posts = [valid, {**valid, "id": "2"}, {**valid, "id": "3", "text": "【新プライズ情報】商品名のみ"}, {**valid, "id": "4", "created_at": "invalid"}]
        ws = Mock()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            added = calendar.record_arrivals(ws, posts, {calendar.post_url(posts[1])})
        self.assertEqual(added, 1)
        ws.append_rows.assert_called_once_with([["2026-11-03", "テスト商品", calendar.post_url(valid)]], value_input_option="RAW")
        for detail in ("対象 4件", "記録済み 1件", "本文解析不可 1件", "日時解析不可 1件", "追加 1件"):
            self.assertIn(detail, output.getvalue())


if __name__ == "__main__":
    unittest.main()

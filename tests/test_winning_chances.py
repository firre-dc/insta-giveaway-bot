"""Tests for the giveaway chance calculation (stdlib unittest, no extras)."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from insta_bot.winning_chances import (
    CachedComment,
    CachedUser,
    analyse_comments,
    build_parser,
    default_comments_file,
    download_new_comments,
    format_report,
    get_post_url,
    get_read_credentials,
    load_cached_comments,
    load_tracked_usernames,
    merge_comments,
    parse_words,
    run,
    save_comments,
)

WORDS = ["Koffiemachine", "Pascual"]


def comment(username: str, text: str) -> SimpleNamespace:
    return SimpleNamespace(text=text, user=SimpleNamespace(username=username))


def raw_comment(pk: str, text: str, username: str) -> dict:
    return {
        "pk": pk,
        "text": text,
        "user": {"pk": str(100 + int(pk)), "username": username},
        "created_at_utc": 1700000000 + int(pk),
        "content_type": "comment",
        "status": "ok",
    }


class FakeClient:
    """Minimal stand-in for the parts of instagrapi's Client we use."""

    def __init__(self, pages: list[dict]) -> None:
        self.pages = iter(pages)
        self.calls: list[dict | None] = []
        self.last_json: dict = {}

    def media_id(self, media_pk: str) -> str:
        return f"{media_pk}_123"

    def private_request(self, endpoint: str, params: dict | None = None) -> dict:
        self.calls.append(params)
        self.last_json = next(self.pages)
        return self.last_json


class AnalyseCommentsTests(unittest.TestCase):
    def test_substring_matching_and_case(self) -> None:
        stats = analyse_comments(
            [
                comment("alice", "@bob Koffiemachine!!"),
                comment("bob", "@alice koffiemachine"),
                comment("alice", "no prize here"),
            ],
            WORDS,
            ["alice", "bob"],
        )
        prize = stats.per_word[0]
        self.assertEqual(stats.total_comments, 3)
        self.assertEqual(stats.matching_comments, 2)
        self.assertEqual(prize.comments, 2)
        self.assertEqual(prize.entries, 2)
        self.assertEqual(prize.counts["alice"], 1)
        self.assertEqual(prize.counts["bob"], 1)
        self.assertAlmostEqual(prize.chance("alice"), 0.5)

    def test_mentions_of_tracked_users_do_not_count(self) -> None:
        stats = analyse_comments(
            [comment("stranger", "@alice Koffiemachine")], WORDS, ["alice"]
        )
        prize = stats.per_word[0]
        self.assertEqual(prize.entries, 1)
        self.assertEqual(prize.counts["alice"], 0)
        self.assertAlmostEqual(prize.chance("alice"), 0.0)

    def test_each_matching_comment_counts_once(self) -> None:
        stats = analyse_comments(
            [comment("alice", "@alice @alice Koffiemachine")], WORDS, ["alice"]
        )
        self.assertEqual(stats.per_word[0].entries, 1)
        self.assertEqual(stats.per_word[0].counts["alice"], 1)

    def test_repeated_comments_each_count(self) -> None:
        stats = analyse_comments(
            [
                comment("alice", "Koffiemachine"),
                comment("alice", "Koffiemachine"),
            ],
            WORDS,
            ["alice"],
        )
        self.assertEqual(stats.per_word[0].entries, 2)
        self.assertEqual(stats.per_word[0].counts["alice"], 2)

    def test_case_insensitive_usernames(self) -> None:
        stats = analyse_comments(
            [comment("ALICE", "@bob Koffiemachine")], WORDS, ["alice"]
        )
        self.assertEqual(stats.per_word[0].counts["alice"], 1)

    def test_comments_with_multiple_words_counted_once_overall(self) -> None:
        stats = analyse_comments(
            [comment("alice", "@bob Koffiemachine Pascual")],
            WORDS,
            ["alice", "bob"],
        )
        self.assertEqual(stats.matching_comments, 1)
        self.assertEqual(stats.overall.comments, 1)
        self.assertEqual(stats.overall.entries, 1)
        self.assertEqual([p.comments for p in stats.per_word], [1, 1])

    def test_no_entries(self) -> None:
        stats = analyse_comments(
            [comment("alice", "no prize here")], WORDS, ["alice"]
        )
        self.assertEqual(stats.per_word[0].chance("alice"), 0.0)
        self.assertIn("No matching comments", format_report(stats, ["alice"]))

    def test_report_contains_percentages(self) -> None:
        stats = analyse_comments(
            [
                comment("alice", "@bob Koffiemachine"),
                comment("bob", "@alice Koffiemachine"),
            ],
            WORDS,
            ["alice", "bob"],
        )
        report = format_report(stats, ["alice", "bob"])
        self.assertIn("50.00% chance", report)
        self.assertIn("Overall (all prize words)", report)
        self.assertIn("Per prize word", report)


class CacheTests(unittest.TestCase):
    def test_default_cache_path_uses_media_pk_and_xdg(self) -> None:
        with patch.dict("os.environ", {"XDG_CONFIG_HOME": "/tmp/xdg"}):
            path = default_comments_file("123_456")
        self.assertEqual(
            path, Path("/tmp/xdg/insta-bot/comments/123_456.json")
        )

    def test_load_cache_missing_and_corrupt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.json"
            self.assertEqual(load_cached_comments(path), [])
            path.write_text("not json")
            self.assertEqual(load_cached_comments(path), [])

    def test_save_and_reload_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "comments.json"
            save_comments(
                [
                    SimpleNamespace(
                        pk="1",
                        user=SimpleNamespace(username="alice"),
                        text="@alice Koffiemachine",
                        created_at_utc=1700000000,
                    )
                ],
                path,
            )
            loaded = load_cached_comments(path)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].pk, "1")
        self.assertEqual(loaded[0].user.username, "alice")
        self.assertEqual(loaded[0].text, "@alice Koffiemachine")

    def test_merge_deduplicates_by_pk(self) -> None:
        cached = [CachedComment("1", CachedUser("alice"), "old")]
        new = [SimpleNamespace(pk="1", text="updated"), SimpleNamespace(pk="2")]
        self.assertEqual(len(merge_comments(cached, new)), 2)
        self.assertEqual(merge_comments(cached, new)[0].text, "updated")

    def test_stops_at_cached_comment_when_paging_older(self) -> None:
        client = FakeClient(
            [
                {
                    "comments": [raw_comment("3", "new", "alice")],
                    "has_more_comments": True,
                    "next_max_id": "10",
                },
                {
                    "comments": [raw_comment("1", "old", "bob")],
                    "has_more_comments": True,
                    "next_max_id": "5",
                },
            ]
        )
        result = download_new_comments(client, "123", {"1", "2"})
        self.assertEqual([comment.pk for comment in result], ["3"])
        self.assertEqual(client.calls, [None, {"max_id": "10"}])

    def test_fetches_headload_comments(self) -> None:
        client = FakeClient(
            [
                {
                    "comments": [raw_comment("1", "old", "alice")],
                    "has_more_headload_comments": True,
                    "next_min_id": "min-1",
                },
                {
                    "comments": [raw_comment("2", "newer", "bob")],
                    "has_more_headload_comments": False,
                },
            ]
        )
        result = download_new_comments(client, "123", {"1"})
        self.assertEqual([comment.pk for comment in result], ["2"])
        self.assertEqual(client.calls, [None, {"min_id": "min-1"}])


class RunTests(unittest.TestCase):
    def test_first_run_saves_cache_then_second_run_only_fetches_new(self) -> None:
        args = build_parser().parse_args(
            [
                "--username",
                "alice",
                "--password",
                "pw",
                "--word",
                "Koffiemachine",
                "--json",
            ]
        )
        first_pages = [
            {
                "comments": [
                    raw_comment("1", "@alice @bob Koffiemachine!!", "alice"),
                    raw_comment("2", "@bob @carol Another post", "bob"),
                ]
            }
        ]
        second_pages = [
            {
                "comments": [
                    raw_comment("3", "@alice @bob Koffiemachine", "bob"),
                    raw_comment("1", "@alice @bob Koffiemachine!!", "alice"),
                ]
            }
        ]

        with (
            tempfile.TemporaryDirectory() as directory,
            patch(
                "insta_bot.winning_chances.create_authenticated_client"
            ) as authenticated,
            patch(
                "insta_bot.winning_chances.get_post_url",
                return_value="https://instagram.com/p/abc/",
            ) as reference,
            patch("insta_bot.winning_chances.resolve_media_pk", return_value="123"),
            patch(
                "insta_bot.winning_chances.load_tracked_usernames",
                return_value=["alice", "bob"],
            ),
            patch(
                "insta_bot.winning_chances.default_session_file",
                return_value=Path(directory) / "session.json",
            ),
        ):
            cache_file = Path(directory) / "comments.json"
            args.comments_file = cache_file

            authenticated.return_value = (FakeClient(first_pages), None)
            first_output = io.StringIO()
            with redirect_stdout(first_output):
                self.assertEqual(run(args), 0)
            self.assertEqual(len(load_cached_comments(cache_file)), 2)

            authenticated.return_value = (FakeClient(second_pages), None)
            second_output = io.StringIO()
            with redirect_stdout(second_output):
                self.assertEqual(run(args), 0)
            cached = load_cached_comments(cache_file)

        reference.assert_called_with()
        self.assertEqual(authenticated.call_count, 2)
        self.assertEqual(authenticated.call_args.args[0], "alice")
        self.assertEqual(authenticated.call_args.args[1], "pw")

        self.assertEqual([record.pk for record in cached], ["3", "1", "2"])
        payload = json.loads(second_output.getvalue())
        self.assertEqual(payload["total_comments"], 3)
        self.assertEqual(payload["matching_comments"], 2)
        self.assertEqual(payload["overall"]["entries"], 2)
        self.assertAlmostEqual(payload["overall"]["chances"]["alice"], 0.5)
        self.assertAlmostEqual(payload["overall"]["chances"]["bob"], 0.5)
        self.assertEqual(payload["per_word"][0]["word"], "Koffiemachine")


class ConfigTests(unittest.TestCase):
    def test_parse_words_deduplicates_and_splits(self) -> None:
        with patch("insta_bot.winning_chances.GIVEAWAY_ITEMS", WORDS):
            self.assertEqual(
                parse_words(["foo, bar", "FOO", "baz"]),
                ["foo", "bar", "baz"],
            )
            self.assertEqual(parse_words(None), WORDS)

    def test_tracked_usernames_are_deduplicated(self) -> None:
        env = {
            "INSTAGRAM_USERNAMES": "alice, @bob",
            "INSTAGRAM_TAGGABLE_USERNAMES": "BOB,carol",
        }
        with patch.dict("os.environ", env, clear=True):
            self.assertEqual(
                load_tracked_usernames(), ["alice", "bob", "carol"]
            )

    def test_post_url_from_env(self) -> None:
        with patch.dict(
            "os.environ", {"INSTAGRAM_URL": " https://instagram.com/p/abc/ "}
        ):
            self.assertEqual(get_post_url(), "https://instagram.com/p/abc/")
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(Exception, "INSTAGRAM_URL"):
                get_post_url()

    def test_read_credentials_from_args(self) -> None:
        self.assertEqual(
            get_read_credentials("@reader", "secret"), ("reader", "secret")
        )

    def test_read_credentials_prompt_for_missing_password(self) -> None:
        with patch("insta_bot.winning_chances.getpass", return_value="typed"):
            self.assertEqual(
                get_read_credentials("reader", None), ("reader", "typed")
            )


if __name__ == "__main__":
    unittest.main()

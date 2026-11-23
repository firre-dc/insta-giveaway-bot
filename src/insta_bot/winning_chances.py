"""Estimate giveaway winning chances from a cached copy of a post's comments.

Comments are downloaded incrementally and saved locally so repeat runs only
fetch new comments.  Giveaway comments have the shape ``@friend @friend prize``.
Instagram users often add punctuation or other characters to the prize word, so
words are matched with a case-insensitive substring check instead of an exact
match.  Every matching comment is one entry for the account that posted it,
regardless of who it mentions; an account's chance of winning is its entries
divided by all entries.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from getpass import getpass
from pathlib import Path
from typing import Any

from instagrapi.exceptions import (
    BadCredentials,
    BadPassword,
    ChallengeRequired,
    ClientError,
    ClientNotFoundError,
    MediaNotFound,
    RateLimitError,
    TwoFactorRequired,
)
from instagrapi.extractors import extract_comment
from instagrapi.types import Comment

from insta_bot.comment import (
    GIVEAWAY_ITEMS,
    InputError,
    create_authenticated_client,
    default_session_file,
    is_needs_upgrade_error,
    resolve_media_pk,
)


def parse_words(values: list[str] | None) -> list[str]:
    """Return the prize words to look for, from flags or the bot's list."""
    raw = GIVEAWAY_ITEMS if not values else [
        word for value in values for word in value.split(",")
    ]
    words: list[str] = []
    seen: set[str] = set()
    for word in raw:
        word = word.strip()
        if word and word.casefold() not in seen:
            seen.add(word.casefold())
            words.append(word)
    if not words:
        raise InputError("At least one prize word is required.")
    return words


def load_tracked_usernames() -> list[str]:
    """Return the configured login and taggable usernames, de-duplicated."""
    usernames: list[str] = []
    seen: set[str] = set()
    for name in ("INSTAGRAM_USERNAMES", "INSTAGRAM_TAGGABLE_USERNAMES"):
        raw = os.environ.get(name, "")
        for entry in raw.split(","):
            username = entry.strip().removeprefix("@")
            if username and username.casefold() not in seen:
                seen.add(username.casefold())
                usernames.append(username)
    if not usernames:
        raise InputError(
            "Set INSTAGRAM_USERNAMES and/or INSTAGRAM_TAGGABLE_USERNAMES "
            "in .env to choose whose chances to report."
        )
    return usernames


def get_post_url() -> str:
    """Return the post URL configured as INSTAGRAM_URL in .env."""
    url = os.environ.get("INSTAGRAM_URL", "").strip()
    if not url:
        raise InputError("INSTAGRAM_URL is not set in .env.")
    return url


def get_read_credentials(username: str, password: str | None) -> tuple[str, str]:
    """Use the supplied login credentials, prompting for a missing password.

    These are deliberately separate from the bot accounts in .env so a
    different account can be used to read comments.
    """
    username = username.strip().removeprefix("@")
    if not username:
        raise InputError("--username must not be empty.")

    if password is None:
        password = getpass("Instagram password: ")
    if not password:
        raise InputError("A password is required via --password or the prompt.")
    return username, password


@dataclass
class PrizeStats:
    """Entry counts for one prize word (or for all words combined)."""

    word: str
    comments: int = 0
    entries: int = 0
    counts: Counter[str] = field(default_factory=Counter)

    def chance(self, username: str) -> float:
        """Fraction of all entries that belong to ``username``."""
        if not self.entries:
            return 0.0
        return self.counts[username.casefold()] / self.entries

    def as_dict(self, usernames: list[str]) -> dict:
        return {
            "word": self.word,
            "matching_comments": self.comments,
            "entries": self.entries,
            "chances": {
                username: self.chance(username) for username in usernames
            },
        }


@dataclass
class GiveawayStats:
    """Result of scanning the comments of one post."""

    total_comments: int
    matching_comments: int
    per_word: list[PrizeStats]
    overall: PrizeStats

    def as_dict(self, usernames: list[str]) -> dict:
        return {
            "total_comments": self.total_comments,
            "matching_comments": self.matching_comments,
            "tracked_usernames": usernames,
            "overall": self.overall.as_dict(usernames),
            "per_word": [stats.as_dict(usernames) for stats in self.per_word],
        }


def analyse_comments(
    comments: list[Comment], words: list[str], tracked_usernames: list[str]
) -> GiveawayStats:
    """Count entries per prize word for the tracked accounts.

    Every matching comment is one entry for the account that posted it.
    Mentions of other accounts in the text do not affect the result.  A comment
    that contains several prize words is counted for each of them, but only
    once in the combined ``overall`` stats.
    """
    per_word = [PrizeStats(word=word) for word in words]
    overall = PrizeStats(word="All prize words")
    stats_by_word = {stats.word.casefold(): stats for stats in per_word}
    tracked = {username.casefold() for username in tracked_usernames}
    matching_comments = 0

    for comment in comments:
        text = comment.text
        folded = text.casefold()
        matched_words = [
            stats_by_word[word.casefold()]
            for word in words
            if word.casefold() in folded
        ]
        if not matched_words:
            continue

        user = getattr(comment, "user", None)
        author = (getattr(user, "username", "") or "").casefold()
        matching_comments += 1
        overall.comments += 1
        overall.entries += 1
        if author in tracked:
            overall.counts[author] += 1
        for stats in matched_words:
            stats.comments += 1
            stats.entries += 1
            if author in tracked:
                stats.counts[author] += 1

    return GiveawayStats(
        total_comments=len(comments),
        matching_comments=matching_comments,
        per_word=per_word,
        overall=overall,
    )


def _format_section(
    stats: PrizeStats, tracked_usernames: list[str], width: int
) -> list[str]:
    lines = [
        f"  Matching comments: {stats.comments:,} | "
        f"entries (one per comment): {stats.entries:,}"
    ]
    if not stats.entries:
        lines.append(
            "  No matching comments found, so no chances can be calculated."
        )
        return lines

    ranked = sorted(tracked_usernames, key=stats.chance, reverse=True)
    for username in ranked:
        lines.append(
            f"  @{username:<{width}} "
            f"{stats.counts[username.casefold()]:>7,} entries  "
            f"{stats.chance(username):>7.2%} chance"
        )
    return lines


def format_report(
    stats: GiveawayStats, tracked_usernames: list[str]
) -> str:
    """Build the human-readable report."""
    width = max((len(name) for name in tracked_usernames), default=0)
    lines = [
        f"Analysed {stats.total_comments:,} comments; "
        f"{stats.matching_comments:,} contain a prize word.",
        "",
        "Chance = your comments / all matching comments "
        "(one entry per comment).",
        "",
        "Overall (all prize words)",
        *_format_section(stats.overall, tracked_usernames, width),
        "",
        "Per prize word",
    ]
    for index, prize in enumerate(stats.per_word):
        if index:
            lines.append("")
        lines.append(f"{prize.word}:")
        lines.extend(_format_section(prize, tracked_usernames, width))
    return "\n".join(lines)


@dataclass(frozen=True)
class CachedUser:
    username: str


@dataclass(frozen=True)
class CachedComment:
    """A comment restored from the local cache."""

    pk: str
    user: CachedUser
    text: str
    created_at_utc: int | None = None


def default_comments_file(media_pk: str) -> Path:
    """Return the per-post cache path."""
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    safe_pk = re.sub(r"[^0-9A-Za-z_-]", "_", media_pk)
    return base / "insta-bot" / "comments" / f"{safe_pk}.json"


def load_cached_comments(path: Path) -> list[CachedComment]:
    """Load cached comments, treating a missing or broken file as no cache."""
    if not path.is_file():
        return []
    try:
        records: Any = json.loads(path.read_text())
        return [
            CachedComment(
                pk=str(record["pk"]),
                user=CachedUser(username=str(record.get("username") or "")),
                text=str(record["text"] or ""),
                created_at_utc=record.get("created_at_utc"),
            )
            for record in records
        ]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(
            f"warning: ignoring unreadable comment cache {path}: {exc}",
            file=sys.stderr,
        )
        return []


def download_new_comments(
    client: Any, media_pk: str, known_pks: set[str]
) -> list[Comment]:
    """Fetch comments that are not already in ``known_pks``.

    Older comments are returned newest first, so pagination stops once a page
    contains only already-cached comments.  Headload comments (newer than the
    first page)
    are always fully fetched to catch comments added since the last run.
    """
    media_id = client.media_id(media_pk)
    new_comments: list[Comment] = []
    seen = set(known_pks)
    headload_min_id: str | None = None

    def request(params: dict[str, str] | None) -> dict[str, Any]:
        try:
            return client.private_request(f"media/{media_id}/comments/", params)
        except ClientNotFoundError as exc:
            raise MediaNotFound(exc, media_id=media_id, **client.last_json) from exc
        except ClientError as exc:
            if "Media not found" in str(exc):
                raise MediaNotFound(exc, media_id=media_id, **client.last_json) from exc
            raise

    def collect(result: dict[str, Any]) -> bool:
        """Store fresh comments; report if the whole page was already cached."""
        page_comments = result.get("comments") or []
        fresh_count = 0
        for raw in page_comments:
            comment = extract_comment(dict(raw))
            pk = str(comment.pk)
            if pk in seen:
                continue
            seen.add(pk)
            new_comments.append(comment)
            fresh_count += 1
        return bool(page_comments) and fresh_count == 0

    result = request(None)
    page_fully_cached = collect(result)
    if result.get("has_more_headload_comments") and result.get("next_min_id"):
        headload_min_id = result["next_min_id"]

    seen_max_ids: set[str] = set()
    while (
        not page_fully_cached
        and result.get("has_more_comments")
        and result.get("next_max_id")
    ):
        max_id = result["next_max_id"]
        if max_id in seen_max_ids:
            break
        seen_max_ids.add(max_id)
        result = request({"max_id": max_id})
        page_fully_cached = collect(result)
        if result.get("has_more_headload_comments") and result.get("next_min_id"):
            headload_min_id = result["next_min_id"]

    seen_min_ids: set[str] = set()
    while headload_min_id and headload_min_id not in seen_min_ids:
        seen_min_ids.add(headload_min_id)
        result = request({"min_id": headload_min_id})
        collect(result)
        if result.get("has_more_headload_comments") and result.get("next_min_id"):
            headload_min_id = result["next_min_id"]
        else:
            headload_min_id = None

    return new_comments


def merge_comments(
    cached: list[CachedComment], new: list[Comment]
) -> list[Comment | CachedComment]:
    """Combine cached and freshly downloaded comments without duplicates."""
    by_pk: dict[str, Comment | CachedComment] = {}
    for comment in [*new, *cached]:
        by_pk.setdefault(str(comment.pk), comment)
    return list(by_pk.values())


def save_comments(comments: list[Comment | CachedComment], path: Path) -> None:
    """Write the downloaded comments to the cache JSON file."""
    records = [
        {
            "pk": comment.pk,
            "username": comment.user.username,
            "text": comment.text,
            "created_at_utc": comment.created_at_utc,
        }
        for comment in comments
    ]
    path = path.expanduser()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(records, indent=2, default=str) + "\n")
    except OSError as exc:
        raise InputError(f"Could not write comments to {path}: {exc}") from exc
    print(f"Saved {len(records)} comments to {path}.", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Download all comments on an Instagram post and estimate "
            "giveaway winning chances for the configured accounts."
        ),
    )
    parser.add_argument(
        "--username",
        required=True,
        metavar="USERNAME",
        help="Instagram account used to read comments (not the bot accounts)",
    )
    parser.add_argument(
        "--password",
        metavar="PASSWORD",
        help="Password for --username (prompted for when omitted)",
    )
    parser.add_argument(
        "--word",
        action="append",
        metavar="WORD",
        help=(
            "Prize word to look for; repeatable or comma-separated "
            "(default: the bot's GIVEAWAY_ITEMS)"
        ),
    )
    parser.add_argument(
        "--session-file",
        type=Path,
        default=None,
        help="Where to read/cache the login session (default: per username)",
    )
    parser.add_argument(
        "--comments-file",
        "--save-comments",
        dest="comments_file",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "JSON cache of downloaded comments "
            "(default: $XDG_CONFIG_HOME/insta-bot/comments/<media-pk>.json)"
        ),
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Ignore the cache and download every comment again",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON instead of a table",
    )
    return parser


def run(args: argparse.Namespace) -> int:
    words = parse_words(args.word)
    tracked_usernames = load_tracked_usernames()
    username, password = get_read_credentials(args.username, args.password)
    post_reference = get_post_url()
    session_file = (
        args.session_file.expanduser()
        if args.session_file is not None
        else default_session_file(username)
    )

    client, _ = create_authenticated_client(username, password, session_file)
    media_pk = resolve_media_pk(client, post_reference)

    comments_file = (
        args.comments_file.expanduser()
        if args.comments_file is not None
        else default_comments_file(media_pk)
    )
    cached = [] if args.refresh else load_cached_comments(comments_file)
    if cached:
        print(
            f"Loaded {len(cached):,} cached comments from {comments_file}.",
            file=sys.stderr,
        )

    new_comments = download_new_comments(
        client, media_pk, {str(comment.pk) for comment in cached}
    )
    comments = merge_comments(cached, new_comments)
    print(
        f"Downloaded {len(new_comments):,} new comments "
        f"({len(comments):,} total).",
        file=sys.stderr,
    )
    save_comments(comments, comments_file)

    stats = analyse_comments(comments, words, tracked_usernames)
    if args.json:
        print(json.dumps(stats.as_dict(tracked_usernames), indent=2))
    else:
        print(format_report(stats, tracked_usernames))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        return run(args)
    except InputError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except EOFError:
        print(
            "error: no input is available for the password prompt. "
            "Pass --password to provide it as an argument.",
            file=sys.stderr,
        )
        return 2
    except (BadCredentials, BadPassword):
        print(
            "error: Instagram rejected the username or password. "
            "Check your credentials or log in once in the app.",
            file=sys.stderr,
        )
        return 1
    except TwoFactorRequired:
        print(
            "error: Instagram requires two-factor authentication. Run the "
            "command again and enter a current code when prompted.",
            file=sys.stderr,
        )
        return 1
    except ChallengeRequired as exc:
        print(
            "error: Instagram requires a manual security challenge. "
            f"Open Instagram, complete the challenge, then retry. ({exc})",
            file=sys.stderr,
        )
        return 1
    except MediaNotFound:
        print(
            "error: the post was not found, or your account cannot view it.",
            file=sys.stderr,
        )
        return 1
    except RateLimitError:
        print(
            "error: Instagram rate-limited this account. Wait before trying again.",
            file=sys.stderr,
        )
        return 1
    except ClientError as exc:
        if is_needs_upgrade_error(exc):
            print(
                "error: Instagram rejected the client as out of date. Wait "
                "before retrying; repeated login attempts can trigger a "
                "throttle.",
                file=sys.stderr,
            )
            return 1
        print(f"error: Instagram request failed: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

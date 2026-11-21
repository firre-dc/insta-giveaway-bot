"""Post a comment on an Instagram post with instagrapi."""

from __future__ import annotations

import argparse
import getpass
import os
import random
import sys
from pathlib import Path

from dotenv import load_dotenv
from instagrapi import Client
from instagrapi.exceptions import (
    BadCredentials,
    BadPassword,
    ChallengeRequired,
    ClientError,
    ClientLoginRequired,
    CommentsDisabled,
    LoginRequired,
    MediaNotFound,
    RateLimitError,
    TwoFactorRequired,
)
from instagrapi.types import Account

load_dotenv()

COMMENT_AMOUNT = 20
GIVEAWAY_ITEMS = ["Koffiemachine", "Pascual", "Corner", "Amis", "Poliakov", "Peterman"]


def load_usernames(current_user: str) -> list[str]:
    """Read the comma-separated username list from the environment."""
    usernames = []
    for entry in os.environ.get("INSTAGRAM_USERNAMES", "").split(","):
        username = entry.strip().removeprefix("@")
        if username and username != current_user: # prevent users from tagging themselves
            usernames.append(username)
    return usernames


DEFAULT_SESSION_FILE = (
    Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    / "insta-bot"
    / "session.json"
)


class InputError(ValueError):
    """Raised when the user supplies invalid input."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Comment on an Instagram post.",
    )
    parser.add_argument(
        "post",
        help="Instagram post/reel URL, or a numeric media PK",
    )
    parser.add_argument(
        "--username",
        help="Instagram username (default: INSTAGRAM_USERNAME)",
    )
    parser.add_argument(
        "--session-file",
        type=Path,
        default=DEFAULT_SESSION_FILE,
        help="Where to cache the login session (default: %(default)s)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log in and resolve the post, but do not publish the comment",
    )
    return parser

def resolve_media_pk(client: Client, reference: str) -> str:
    reference = reference.strip()
    if reference.isdigit():
        return reference
    try:
        return client.media_pk_from_url(reference)
    except Exception as exc:
        raise InputError(f"Could not resolve the post URL: {exc}") from exc


def get_credentials(username: str | None) -> tuple[str, str]:
    username = (username or os.environ.get("INSTAGRAM_USERNAME", "")).strip()
    if not username:
        username = input("Instagram username: ").strip()
    if not username:
        raise InputError("An Instagram username is required.")

    password = os.environ.get("INSTAGRAM_PASSWORD")
    if password is None:
        password = getpass.getpass(f"Instagram password for @{username}: ")
    if not password:
        raise InputError("An Instagram password is required.")

    return username, password


def restore_session(client: Client, session_file: Path) -> Account | None:
    """Load a saved session and return the account if it is still valid."""
    if not session_file.is_file():
        return None

    try:
        client.load_settings(session_file)
        account = client.account_info()
    except (OSError, ValueError) as exc:
        raise InputError(
            f"Could not read the session file {session_file}: {exc}"
        ) from exc
    except (LoginRequired, ClientLoginRequired):
        # A stale cookie/session is normal; the caller will log in again.
        print("The saved session has expired; logging in again.", file=sys.stderr)
        return None

    print(f"Using the saved session for @{account.username}.")
    return account


def login(
    client: Client,
    username: str,
    password: str,
    *,
    fresh_session: bool,
) -> None:
    """Authenticate, prompting for a 2FA code if Instagram asks for one."""
    try:
        client.login(username, password, relogin=fresh_session)
    except TwoFactorRequired:
        code = input("Two-factor authentication code: ").strip()
        if not code:
            raise InputError("A two-factor authentication code is required.")
        client.login(username, password, verification_code=code)


def save_session(client: Client, session_file: Path) -> bool:
    """Save the session, but do not block commenting if the cache fails."""
    session_file = session_file.expanduser()
    try:
        session_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        client.dump_settings(session_file)
        session_file.chmod(0o600)
    except (OSError, ClientError) as exc:
        print(
            f"warning: could not cache the session in {session_file}: {exc}",
            file=sys.stderr,
        )
        return False
    return True


def run(args: argparse.Namespace) -> int:
    client = Client()
    session_file = args.session_file.expanduser()
    account = restore_session(client, session_file)

    requested_username = (
        args.username or os.environ.get("INSTAGRAM_USERNAME") or ""
    ).strip()
    if (
        account is not None
        and requested_username
        and account.username.casefold() != requested_username.casefold()
    ):
        print(
            f"The saved session belongs to @{account.username}, not "
            f"@{requested_username}; logging in as @{requested_username}.",
            file=sys.stderr,
        )
        account = None

    if account is None:
        username, password = get_credentials(args.username)
        login(
            client,
            username,
            password,
            fresh_session=session_file.is_file(),
        )
        account = client.account_info()
        if save_session(client, session_file):
            print(
                f"Logged in as @{account.username}; "
                f"session saved to {session_file}."
            )
        else:
            print(f"Logged in as @{account.username}.")

    usernames = load_usernames(account.username)
    tagged_users = " ".join("@" + str(u) for u in random.sample(usernames, 2))
    comment = tagged_users + " " + str(random.choice(GIVEAWAY_ITEMS))

    media_pk = resolve_media_pk(client, args.post)
    if args.dry_run:
        print(f"Dry run: would comment on media {media_pk}:")
        print(comment)
        return 0

    while True:
        try:
            result = client.media_comment(media_pk, comment)
            print(f"Commented on media {media_pk} as @{account.username}.")
            print(f"Comment ID: {result.pk}")
        except RateLimitError:
            return 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        return run(args)
    except InputError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except EOFError:
        print(
            "error: no input is available for a required prompt. "
            "Set INSTAGRAM_USERNAME and INSTAGRAM_PASSWORD in the environment.",
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
            "error: Instagram requires two-factor authentication. "
            "Run the command again and enter a current code when prompted.",
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
    except CommentsDisabled:
        print("error: comments are disabled on that post.", file=sys.stderr)
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
        print(f"error: Instagram request failed: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

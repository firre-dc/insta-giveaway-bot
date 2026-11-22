"""Post a comment on an Instagram post with instagrapi."""

from __future__ import annotations

import argparse
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

COMMENT_AMOUNT = 50 + random.randint(0,20)
GIVEAWAY_ITEMS = ["Koffiemachine", "Pascual", "Corner", "Amis", "Poliakov", "Peterman"]


class InputError(ValueError):
    """Raised when the user supplies invalid input."""


def load_usernames(current_user: str) -> list[str]:
    """Read the comma-separated username list from the environment."""
    usernames = []
    for entry in os.environ.get("INSTAGRAM_USERNAMES", "").split(","):
        username = entry.strip().removeprefix("@")
        if username and username != current_user: # prevent users from tagging themselves
            usernames.append(username)
    return usernames


def _env_list(name: str) -> list[str]:
    """Read a comma-separated list from the environment."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return []
    return [entry.strip() for entry in raw.split(",")]


def load_accounts() -> list[tuple[str, str]]:
    """Read the index-aligned username/password arrays from the environment."""
    usernames = [
        username.removeprefix("@")
        for username in _env_list("INSTAGRAM_USERNAMES")
    ]
    passwords = _env_list("INSTAGRAM_PASSWORDS")

    if not usernames:
        raise InputError(
            "No accounts configured. Set INSTAGRAM_USERNAMES and "
            "INSTAGRAM_PASSWORDS in .env."
        )
    if len(usernames) != len(passwords):
        raise InputError(
            f"INSTAGRAM_USERNAMES has {len(usernames)} entries, but "
            f"INSTAGRAM_PASSWORDS has {len(passwords)}; they must match."
        )
    if any(not username for username in usernames):
        raise InputError("INSTAGRAM_USERNAMES contains an empty entry.")
    if any(not password for password in passwords):
        raise InputError("INSTAGRAM_PASSWORDS contains an empty entry.")

    return list(zip(usernames, passwords, strict=True))


def default_session_file(username: str) -> Path:
    """Return the per-account session cache path."""
    safe_username = "".join(
        character if character.isalnum() or character in "._-" else "_"
        for character in username
    )
    return (
        Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        / "insta-bot"
        / "sessions"
        / f"{safe_username}.json"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Comment on an Instagram post.",
    )
    parser.add_argument(
        "post",
        nargs="?",
        help=(
            "Instagram post/reel URL, or a numeric media PK "
            "(default: INSTAGRAM_URL)"
        ),
    )
    parser.add_argument(
        "--user",
        type=int,
        default=0,
        metavar="INDEX",
        help=(
            "Zero-based index of the account in INSTAGRAM_USERNAMES/"
            "INSTAGRAM_PASSWORDS (default: %(default)s)"
        ),
    )
    parser.add_argument(
        "--session-file",
        type=Path,
        default=None,
        help=(
            "Where to cache the login session (default: "
            "$XDG_CONFIG_HOME/insta-bot/sessions/<username>.json)"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log in and resolve the post, but do not publish the comment",
    )
    return parser


def get_post_reference(post: str | None) -> str:
    """Return the post reference from the argument or INSTAGRAM_URL."""
    reference = (post or os.environ.get("INSTAGRAM_URL", "")).strip()
    if not reference:
        raise InputError(
            "A post URL or media PK is required. Pass it as an argument or "
            "set INSTAGRAM_URL."
        )
    return reference


def resolve_media_pk(client: Client, reference: str) -> str:
    reference = reference.strip()
    if reference.isdigit():
        return reference
    try:
        return client.media_pk_from_url(reference)
    except Exception as exc:
        raise InputError(f"Could not resolve the post URL: {exc}") from exc


def get_credentials(user_index: int) -> tuple[str, str]:
    """Select the username/password pair at the requested index."""
    accounts = load_accounts()
    if not 0 <= user_index < len(accounts):
        choices = ", ".join(
            f"{index}=@{username}"
            for index, (username, _) in enumerate(accounts)
        )
        raise InputError(
            f"--user {user_index} is out of range; choose one of: {choices}."
        )
    return accounts[user_index]


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
    username, password = get_credentials(args.user)
    post_reference = get_post_reference(args.post)
    session_file = (
        args.session_file.expanduser()
        if args.session_file is not None
        else default_session_file(username)
    )

    client = Client()
    account = restore_session(client, session_file)

    if (
        account is not None
        and account.username.casefold() != username.casefold()
    ):
        print(
            f"The saved session belongs to @{account.username}, not "
            f"@{username}; logging in as @{username}.",
            file=sys.stderr,
        )
        account = None

    if account is None:
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

    media_pk = resolve_media_pk(client, post_reference)
    if args.dry_run:
        print(f"Dry run: would comment on media {media_pk}:")
        print(comment)
        return 0

    for _ in range(COMMENT_AMOUNT):
        result = client.media_comment(media_pk, comment)
        print(f"Commented on media {media_pk} as @{account.username}.")
        print(f"Comment ID: {result.pk}")
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
            "error: no input is available for a required prompt. "
            "Set INSTAGRAM_USERNAMES and INSTAGRAM_PASSWORDS in .env.",
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

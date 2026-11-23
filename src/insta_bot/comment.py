"""Post a comment on an Instagram post with instagrapi."""

from __future__ import annotations

import argparse
import itertools
import os
import random
import sys
from pathlib import Path
from time import sleep

from dotenv import load_dotenv
from instagrapi import Client
from instagrapi.exceptions import (
    BadCredentials,
    BadPassword,
    ChallengeRequired,
    ClientError,
    ClientLoginRequired,
    ClientNeedsUpgradeError,
    CommentsDisabled,
    FeedbackRequired,
    LoginRequired,
    MediaNotFound,
    RateLimitError,
    TwoFactorRequired,
    UnknownError,
)
from instagrapi.types import Account

load_dotenv()

COMMENT_AMOUNT = 70 + random.randint(0, 30)
GIVEAWAY_ITEMS = ["Koffiemachine", "Pascual", "Corner", "Amis", "Poliakov", "Peterman", "Okay"]
COMMENT_BLOCK_KEYS = {
    "comment_si_blocked",
    "comment_blocked",
    "commenting_blocked",
    "feedback_required",
}


class InputError(ValueError):
    """Raised when the user supplies invalid input."""


class CommentBlockedError(RuntimeError):
    """Raised when Instagram temporarily refuses new comments."""


def _env_list(name: str) -> list[str]:
    """Read a comma-separated list from the environment."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return []
    return [entry.strip() for entry in raw.split(",")]


def load_taggable_usernames(current_user: str) -> list[str]:
    """Read the separate taggable list, excluding the account running the bot."""
    current = current_user.casefold()
    usernames: list[str] = []
    for entry in _env_list("INSTAGRAM_TAGGABLE_USERNAMES"):
        username = entry.removeprefix("@")
        if (
            username
            and username.casefold() != current
            and username not in usernames
        ):
            usernames.append(username)

    if len(usernames) < 2:
        raise InputError(
            "INSTAGRAM_TAGGABLE_USERNAMES must contain at least two unique "
            "usernames other than the account running the bot."
        )
    return usernames


def build_comments(taggable_usernames: list[str]) -> list[str]:
    """Build distinct comments so Instagram does not reject repeated text."""
    pairs = list(itertools.combinations(taggable_usernames, 2))
    variants = [
        f"@{first} @{second} {item}"
        for first, second in pairs
        for item in GIVEAWAY_ITEMS
    ]
    random.shuffle(variants)
    return variants


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


def is_needs_upgrade_error(exc: Exception) -> bool:
    """Whether Instagram rejected the client/session as out of date."""
    if isinstance(exc, ClientNeedsUpgradeError):
        return True
    message = str(exc).casefold()
    return any(
        marker in message
        for marker in (
            "version of instagram is out of date",
            "needs_upgrade",
            "app_out_of_date",
        )
    )


def is_comment_blocked_error(exc: Exception) -> bool:
    """Whether Instagram refused a comment because of a temporary block."""
    if isinstance(exc, (RateLimitError, FeedbackRequired)):
        return True

    error_key = str(getattr(exc, "comment_error_key", "")).casefold()
    if error_key in COMMENT_BLOCK_KEYS:
        return True

    message = str(exc).casefold()
    if any(key in message for key in COMMENT_BLOCK_KEYS):
        return True
    if "unable to comment" in message:
        return True

    # Instagram returns this generic 400 body for a comment action block,
    # without the comment_error_key field that older responses included.
    return (
        isinstance(exc, UnknownError)
        and str(getattr(exc, "status_code", "")) == "400"
        and "something went wrong" in message
    )


def clear_session_file(session_file: Path) -> None:
    """Remove a session so the next login uses the current client profile."""
    try:
        session_file.unlink(missing_ok=True)
    except OSError as exc:
        print(
            f"warning: could not remove the stale session file "
            f"{session_file}: {exc}",
            file=sys.stderr,
        )


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
    except ClientError as exc:
        # Sessions saved by an older instagrapi release pin an older Android
        # app profile, which Instagram rejects with "out of date".
        if is_needs_upgrade_error(exc):
            print(
                "The saved session uses an outdated app profile; logging in "
                "again with a fresh client.",
                file=sys.stderr,
            )
            return None
        raise

    print(f"Using the saved session for @{account.username}.")
    return account

def challenge_code_handler(username: str, choice) -> str:
    """Prompt for the code Instagram sends (email/SMS) during a challenge."""
    code = input(
        f"Challenge code sent to you via {choice} for @{username}: "
    ).strip()
    if not code:
        raise InputError("A challenge code is required.")
    return code

def login(client: Client, username: str, password: str) -> None:
    """Authenticate, prompting for a 2FA code if Instagram asks for one."""
    client.challenge_code_handler = challenge_code_handler
    try:
        client.login(username, password)
    except TwoFactorRequired:
        code = input("Two-factor authentication code: ").strip()
        if not code:
            raise InputError("A two-factor authentication code is required.")
        client.login(username, password, verification_code=code)
    except ChallengeRequired:
        client.challenge_resolve(client.last_json)
        client.login(username, password)

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


def create_authenticated_client(
    username: str, password: str, session_file: Path
) -> tuple[Client, Account]:
    """Return a logged-in client and its account, reusing a cached session."""
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
        # load_settings() pins an older device/app profile in the client, so
        # start over with a new client that uses instagrapi's current profile.
        clear_session_file(session_file)
        client = Client()
        login(client, username, password)
        account = client.account_info()
        if save_session(client, session_file):
            print(
                f"Logged in as @{account.username}; "
                f"session saved to {session_file}."
            )
        else:
            print(f"Logged in as @{account.username}.")

    return client, account


def run(args: argparse.Namespace) -> int:
    username, password = get_credentials(args.user)
    post_reference = get_post_reference(args.post)
    session_file = (
        args.session_file.expanduser()
        if args.session_file is not None
        else default_session_file(username)
    )

    client, account = create_authenticated_client(username, password, session_file)

    taggable_usernames = load_taggable_usernames(account.username)
    comments = build_comments(taggable_usernames)
    media_pk = resolve_media_pk(client, post_reference)
    if args.dry_run:
        print(f"Dry run: would comment on media {media_pk} (this is only the first item in the list):")
        print(comments[0])
        return 0

    print(f"Commenting {COMMENT_AMOUNT} times...")

    comment = comments[0]
    for posted in range(COMMENT_AMOUNT):
        if posted % random.randint(20, 30) == 0:
            comment = comments[random.randint(0, len(comments) - 1)]
        sleep(2 * random.random())

        try:
            client.media_comment(media_pk, comment)
        except ClientError as exc:
            print(exc)
            if not is_comment_blocked_error(exc):
                raise
            if posted:
                outcome = f"after {posted} comment{'s' if posted != 1 else ''}"
            else:
                outcome = "before the first comment"
            raise CommentBlockedError(
                f"Instagram is blocking new comments from @{account.username} "
                f"{outcome}. This is a temporary comment action block, not a "
                "login problem. Stop the bot and wait at least 24-48 hours; "
                "retrying now can extend the block. Check Account Status in "
                "the Instagram app."
            ) from exc
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        return run(args)
    except CommentBlockedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
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
        if is_needs_upgrade_error(exc):
            print(
                "error: Instagram still rejected the client as out of date "
                "after a fresh login. Wait before retrying; repeated login "
                "attempts can trigger an account-level throttle.",
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

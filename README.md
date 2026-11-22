# insta-bot

Instagram comment tooling with [instagrapi](https://github.com/subzeroid/instagrapi),


**NOTE: Instagram can rate-limit, challenge, or lock accounts that automate actions. Use a dedicated account at your own risk, and do not spam comments.**
## Live comment script

Post comments on an Instagram post with instagrapi, using one of the accounts
configured in `.env`.

### Configuration

Create a `.env` file in the project root with the account lists and post URL:

```dotenv
INSTAGRAM_USERNAMES=account_one,account_two
INSTAGRAM_PASSWORDS=secret_one,secret_two
INSTAGRAM_TAGGABLE_USERNAMES=friend_one,friend_two,friend_three
INSTAGRAM_URL=https://www.instagram.com/p/SHORTCODE/
```

The password at each position belongs to the username at the same position.
Select an account by its zero-based index with `--user`:

```bash
uv run insta-bot --user 0 "https://www.instagram.com/p/SHORTCODE/"
```

`--user` defaults to `0`. Commas separate entries, so passwords cannot contain
commas. The accounts used for login come from `INSTAGRAM_USERNAMES`. The
comment tags two random accounts from the separate
`INSTAGRAM_TAGGABLE_USERNAMES` list, skips the account running the bot, and
adds a random giveaway item. That list needs at least two usable usernames.

You can also set `INSTAGRAM_URL` in `.env` and omit the post argument:

```bash
uv run insta-bot --user 0
```

Each account caches its own session in
`~/.config/insta-bot/sessions/<username>.json` (or
`$XDG_CONFIG_HOME/insta-bot/sessions/<username>.json`) so switching accounts
does not overwrite another account's session. Override the location with
`--session-file`.

Use `--dry-run` to log in and resolve the post without publishing anything:

```bash
uv run insta-bot --user 1 --dry-run "https://www.instagram.com/p/SHORTCODE/"
```

Show all options with:

```bash
uv run insta-bot --help
```

## GitHub Actions

The workflow in `.github/workflows/insta-bot.yml` runs the bot every 4 hours
(at minute 0 UTC), after pushes to `main`, and can also be started manually
from the Actions tab.

Configure the repository before enabling it:

1. Add repository secrets `INSTAGRAM_USERNAMES` and `INSTAGRAM_PASSWORDS`
   with the same comma-separated values you use in `.env`.
2. Add a repository variable (or secret) `INSTAGRAM_URL` with the post/reel
   URL or media PK to comment on.
3. Add a repository variable (or secret) `INSTAGRAM_TAGGABLE_USERNAMES` with
   the comma-separated usernames to tag. It needs at least two usable names.
4. Optionally add a repository variable `INSTAGRAM_USER_INDEX` to choose the
   account index (defaults to `0`).

Manual runs can override the account index with the `user` input, and set
`dry_run` to log in and resolve the post without publishing comments.

The workflow caches `~/.config/insta-bot/sessions` between runs to avoid
logging in from scratch every time. The cache key includes `uv.lock`, so a
dependency upgrade starts with fresh sessions. That cache contains
authentication cookies, so keep the repository private or remove the cache
steps.

## Notes

- The session file contains authentication cookies. Treat it like a password
  and do not commit or share it.
- Instagram can rate-limit, challenge, or lock accounts that automate actions.
  Use a dedicated account at your own risk, and do not spam comments.
- If Instagram reports that the app version is out of date, the bot discards
  the cached session and logs in with a fresh client. If it still happens,
  wait before retrying; repeated login attempts can trigger a throttle.
- If Instagram asks for two-factor authentication, the script prompts for the
  code. GitHub Actions runs are non-interactive, so a 2FA prompt or security
  challenge fails the run. Complete the challenge in the Instagram app and
  re-run the workflow.

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
commas. The accounts used for login come from `INSTAGRAM_USERNAMES`. Each
comment tags two random accounts from the separate
`INSTAGRAM_TAGGABLE_USERNAMES` list, skips the account running the bot, and
adds a random giveaway item. That list needs at least two usable usernames.
Comments posted in one run are distinct so Instagram does not reject repeated
text.

By default the bot posts one comment. Use `--count` (or
`INSTAGRAM_COMMENT_COUNT`) to post more, but keep it low: Instagram blocks
accounts that post many comments in a short window. The bot stops with a clear
error as soon as Instagram refuses a comment. A comment action block can last
from hours to days, and retrying while blocked can extend it.

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

## Winning chances script

Estimate the giveaway odds for the accounts listed in `.env`, using a local
cache of the comments on the post configured there:

```bash
uv run insta-winning-chances --username reader_account --password secret
```

The post URL is always read from `INSTAGRAM_URL` in `.env`; there is no URL
argument. `--username`/`--password` are for a separate reading account, not
the bot accounts in `.env`. Omit `--password` to be prompted for it securely:

```bash
uv run insta-winning-chances --username reader_account
```

The session is cached per reading account in
`$XDG_CONFIG_HOME/insta-bot/sessions/<username>.json`; override it with
`--session-file`. The accounts whose odds are reported still come from
`INSTAGRAM_USERNAMES` and `INSTAGRAM_TAGGABLE_USERNAMES` in `.env`.

Giveaway comments look like `@friend @friend prize`. A comment counts when it
contains one of the prize words from `GIVEAWAY_ITEMS` as a case-insensitive
substring, so `Koffiemachine!!` still matches. Use `--word` to supply a
different list (repeat the flag or use comma-separated values):

```bash
uv run insta-winning-chances --word Koffiemachine --word Pascual
```

Every matching comment counts as one entry for the account that posted it,
regardless of which accounts it mentions. An account's chance is its entries
divided by all entries in matching comments, which is also the chance of
winning when one winner is drawn per prize from the commenters. Chances are
reported overall and per prize word. Words come from
`src/insta_bot/comment.py` by default; updates to that list apply to both
scripts.

Comments are cached locally, so repeat runs only download comments added since
the previous run. The cache lives at
`$XDG_CONFIG_HOME/insta-bot/comments/<media-pk>.json` (normally
`~/.config/insta-bot/comments/`). Use `--comments-file comments.json` to store
it elsewhere (`--save-comments` is an alias), or `--refresh` to download every
comment again. A missing or corrupt cache is treated as an empty one.

Other options:

- `--json` prints the counts and probabilities as JSON.

```bash
uv run insta-winning-chances --username reader_account --refresh --json
```

Run the calculation tests with:

```bash
uv run python -m unittest discover -s tests -v
```

## GitHub Actions

The workflow in `.github/workflows/insta-bot.yml` is started manually from the
Actions tab. It no longer runs on a schedule.

Configure the repository before enabling it:

1. Add repository secrets `INSTAGRAM_USERNAMES` and `INSTAGRAM_PASSWORDS`
   with the same comma-separated values you use in `.env`.
2. Add a repository variable (or secret) `INSTAGRAM_URL` with the post/reel
   URL or media PK to comment on.
3. Add a repository variable (or secret) `INSTAGRAM_TAGGABLE_USERNAMES` with
   the comma-separated usernames to tag. It needs at least two usable names.
4. Optionally add a repository variable `INSTAGRAM_USER_INDEX` to choose the
   account index (defaults to `0`).
5. Optionally add a repository variable `INSTAGRAM_COMMENT_COUNT` to post more
   than one comment per run (defaults to `1`). Keep this low to avoid a
   temporary comment block.

Manual runs can override the account index with the `user` input, set
`dry_run` to log in and resolve the post without publishing comments, and set
`count` to override the comment count.

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

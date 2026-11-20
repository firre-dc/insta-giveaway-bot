# insta-bot

Post a comment on an Instagram post with
[instagrapi](https://github.com/subzeroid/instagrapi).

## Usage

```bash
export INSTAGRAM_USERNAME="your_username"
export INSTAGRAM_PASSWORD="your_password"

uv run insta-bot "https://www.instagram.com/p/SHORTCODE/" "Nice shot!"
```

The post argument can be a post, reel, or IGTV URL, or a numeric media PK.
Pass `-` as the comment to read it from standard input:

```bash
printf 'Hello from the CLI!' | uv run insta-bot "https://www.instagram.com/reel/SHORTCODE/" -
```

If `INSTAGRAM_USERNAME` or `INSTAGRAM_PASSWORD` is not set, the script prompts
for the missing value. The login session is cached in
`~/.config/insta-bot/session.json` (or `$XDG_CONFIG_HOME/insta-bot/session.json`)
so later runs can reuse it. Override the location with `--session-file`.

Use `--dry-run` to log in and resolve the post without publishing anything:

```bash
uv run insta-bot --dry-run "https://www.instagram.com/p/SHORTCODE/" "Test"
```

Show all options with:

```bash
uv run insta-bot --help
```

## Notes

- The session file contains authentication cookies. Treat it like a password
  and do not commit or share it.
- Instagram can rate-limit, challenge, or lock accounts that automate actions.
  Use a dedicated account at your own risk, and do not spam comments.
- If Instagram asks for two-factor authentication, the script prompts for the
  code.

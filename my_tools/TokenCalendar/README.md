# TokenCalendar

CLI calendar heatmap for the current New API token configured in Codex.

TokenCalendar always stores logs in a local SQLite archive. The remote New API
endpoint currently returns only the latest 1000 rows per scan, but SQLite is
cumulative: every unique row seen by any scan is kept.

## Usage

Show the interactive terminal heatmap:

```bash
./token-calendar
```

Every run does this first:

```text
fetch latest remote token logs -> insert new rows into SQLite -> open the TUI from the updated local archive
```

The refresh waits before opening so the first screen reflects the latest scan.
The two remote requests are made in parallel to keep startup time down.

In an interactive terminal, this opens a TUI. Move the mouse over a day, click a
day, or use arrow keys to inspect the selected day. Like `top`/`htop`, the TUI
keeps running and refreshes the archive in the background every 5 minutes by
default. Press `q` to quit.

Keyboard controls:

```text
arrows / hjkl  move selected day
1              total tokens
2              input tokens
3              output tokens
4              cache read tokens
5              cache create tokens
6              requests
7              quota
8              NewAPI CNY estimate
9              cache hit rate
0              official CNY estimate
r / f          refresh now
space          toggle live refresh
g / G          first / latest day
q              quit
```

Plain non-interactive output:

```bash
./token-calendar --plain
```

Plain mode waits for the refresh before printing, because there is no live TUI
to update after the command exits.

Offline/local-only view:

```bash
./token-calendar --no-refresh
```

Other views:

```bash
./token-calendar --metric input
./token-calendar --metric output
./token-calendar --metric cache-read
./token-calendar --metric cache-create
./token-calendar --metric cache-hit
./token-calendar --metric quota
./token-calendar --metric requests
./token-calendar --metric usd
./token-calendar --metric official-usd
./token-calendar --ips 10
./token-calendar --mask-ip
./token-calendar --refresh-pricing
./token-calendar --offline-pricing
./token-calendar --offline-fx
./token-calendar --watch-interval 300
./token-calendar --no-watch
./token-calendar --ascii-heatmap
./token-calendar --days 365
./token-calendar --color never
./token-calendar --timeout 5
./token-calendar --usage-timeout 1
```

## Background Collection

A user-level systemd timer collects logs every hour even when you are not
running the command:

```bash
mkdir -p ~/.config/systemd/user
cp systemd/token-calendar-collect.service.example ~/.config/systemd/user/token-calendar-collect.service
cp systemd/token-calendar-collect.timer ~/.config/systemd/user/token-calendar-collect.timer
python_bin="$(command -v python3)"
project_dir="$(pwd)"
sed -i "s#__PYTHON_BIN__#${python_bin}#g; s#__TOKENCALENDAR_DIR__#${project_dir}#g" ~/.config/systemd/user/token-calendar-collect.service
systemctl --user daemon-reload
systemctl --user enable --now token-calendar-collect.timer
systemctl --user status token-calendar-collect.timer
systemctl --user list-timers token-calendar-collect.timer
```

View collection logs:

```bash
journalctl --user -u token-calendar-collect.service -n 50 --no-pager
```

Disable the timer:

```bash
systemctl --user disable --now token-calendar-collect.timer
```

## Data Files

- `data/token_logs.sqlite3`: cumulative local archive
- `data/usd_cny_rate.json`: cached historical USD/CNY rate for yesterday
- `token_calendar_core.py`: New API collection and SQLite storage
- `token_calendar_cli.py`: terminal heatmap renderer
- `collect_logs.py`: one-shot background collector

The API key is read from `~/.codex/auth.json` only during collection. It is not
stored in SQLite.

The SQLite archive is private operational data. It stores the token log fields
returned by New API, including token names, model names, request counts, quota,
IP addresses, the source host recorded in scan metadata, usage snapshots, and
the raw JSON returned by the log endpoint. If the server returns usernames,
user IDs, group names, or token IDs, those values can also be present in the
local archive. Do not commit `data/`, terminal screenshots, or copied command
output unless you have reviewed and redacted it.

## Publishing Safely

Before publishing this repository, verify that only source files are staged:

```bash
git add -n .
git status --short --ignored .
git check-ignore -v data/token_logs.sqlite3 data/token_logs.sqlite3-wal data/token_logs.sqlite3-shm
```

Never commit:

- `data/`
- `~/.codex/auth.json`
- `~/.codex/config.toml`
- generated `systemd/token-calendar-collect.service`
- screenshots or logs that show full IPs, token names, balances, or the New API host
- any exported SQLite/CSV/JSON data derived from your real token logs

## Token Columns

The CLI separates total usage into input, output, cache read, and cache create
when the New API log row contains those fields. In the current observed logs,
cache read comes from `other.cache_tokens`. Cache create remains `0` unless the
server starts returning a cache creation field such as
`cache_creation_input_tokens`.

`Cache Read` is the real cache hit amount: input tokens served from prompt
cache. `Cache Create` is the amount of input written into cache for possible
future reuse. The cache hit rate shown by TokenCalendar is:

```text
cache_read / (input + cache_create + cache_read)
```

Output tokens are intentionally excluded from this denominator because prompt
cache only applies to the input side.

## IP Distribution

When New API returns an `ip` field in token logs, TokenCalendar stores it in
SQLite and shows the selected day's IP distribution in the TUI. Plain output
prints the latest visible day's IP distribution.

Each IP row includes request count, New API quota-based cost, and official
OpenAI-estimated CNY cost. Full IPs are shown by default for local inspection; use
`--mask-ip` when producing shareable output.

## Official Price Estimate

TokenCalendar reports two cost views:

- `newapi`: the New API quota-based estimate, using `quota / 500000`, displayed with the RMB symbol
- `official`: an OpenAI API price estimate converted from USD to RMB

For this demo, only `gpt-5.5` pricing is applied. `gpt-5.5-openai-compact`
is intentionally normalized to `gpt-5.5`; other model names are ignored by the
official estimate:

```text
input $5.00 / 1M tokens
cached input $0.50 / 1M tokens
output $30.00 / 1M tokens
```

When the cached LiteLLM pricing includes the long-context tier, requests with
more than 272K prompt-side tokens use the higher `gpt-5.5` long-context rates.

The formula is:

```text
input * input_price
+ cache_create * input_price
+ cache_read * cached_input_price
+ output * output_price
```

Pricing is loaded like `ccusage`: TokenCalendar uses cached LiteLLM pricing
from `data/model_prices_and_context_window.json` when available, refreshes it
when requested or older than 24 hours, and falls back to the embedded `gpt-5.5`
price if the network fetch fails.

Official USD costs are converted to CNY using yesterday's historical USD/CNY
rate for the configured timezone. The returned rate date must match yesterday's
date before it is cached. The primary source is Frankfurter's historical API,
with a dated jsDelivr currency-api file as fallback. Use `--offline-fx` to rely
only on the cached `data/usd_cny_rate.json` value.

## Important Limit

Older history that had already fallen out of the remote 1000-row window before
collection started cannot be recovered with the current API key. From now on,
hourly collection plus command-time collection should keep future logs
accumulating locally, as long as fewer than 1000 requests happen between two
collection runs.

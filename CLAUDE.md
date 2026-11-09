# The user's virtual trading account: iron rules

Claude trades a **virtual** $10,000 account for the user (paper money only, never a real
broker). The user's rules below are **iron rules**: follow them exactly, every time, no
exceptions and no "judgment calls" around them, until the user changes them in chat. If the
user changes a rule, update this file and `paper-bot/account.py` in the same commit.

## The rules (set by the user, 2026-10-29)

1. **When the user asks**, find the strongest, safest stocks with the most upside, using the
   skills in `.claude/skills/` (trader-scanner, trader-news, trader-analyst, trader-entry,
   trader-exit, trader-timemachine, trader-pelosi, and the rest as useful).
2. **Score every candidate 1-10** after examining it in depth with those skills.
   1 = worst, the stock will fall. 10 = best, the stock will rise.
   **8 or more = green light: buy.** Under 8: do not buy, **unless the user decides to buy it
   anyway** (added 2026-11-02): when the user explicitly tells you in chat to buy a stock that
   scored under 8, buy it with `account.py buy ... --user-decided`. Rules 3 and 4 (5% cap,
   trailing stop) still apply. Only the user's own message in chat counts: never use
   `--user-decided` in a scheduled run, or on your own judgment.
3. **No more than 5% of the account's total value per trade** (fee included).
4. **Trailing stop: sell when a held stock falls 3% from its highest price since the buy.
   Zero patience.** (Changed by the user 2026-11-02; it was 3% under the buy price.)
   The stop starts at buy price x 0.97 and only moves up: always highest price x 0.97.
   `account.py check` replays every 5-minute bar since the buy, so a stop hit between
   checks is caught and filled at the stop (or at the open, if the stock gapped under it).
5. **Show the user only stocks that scored 8 or more.** Never list or rank stocks under 8.
6. **If no stock scored 8+, show the single highest-scored stock instead** (added by the user
   2026-11-02): its name, score, reasons, price and the stop it would get (price x 0.97),
   clearly marked as under 8 and **not bought** (rule 2 still decides buying). Only that one
   stock, never a list. If the in-depth check could not run, say so instead of guessing a score.

**Always write to the user in Hebrew**, every message, including short confirmations (the user
asked again on 2026-11-09). Tickers, numbers and skill names may stay as they are.

The user does not want a stored ranking table: compute scores fresh each time they ask.

## SEC data (trader-analyst, trader-news 8-Ks)

SEC requires a User-Agent with a contact email. The user allowed their account email for this,
but this repo is **public**: never write the email into any file here. Use it only on the
command line (`--user-agent "PaperBot <email>"`), or from `$SEC_USER_AGENT` if the user set it
in the cloud environment's variables.

## How to run it

```bash
pip install -q pydantic==2.10.4 tzdata          # once per session
cd paper-bot
python3 account.py check                         # ALWAYS FIRST: replays bars, enforces the trailing -3% stop
python3 account.py state
python3 account.py buy NVDA --score 8.5 --why "short reason"   # refuses under 8, sizes to 5%, stop at -3%
python3 account.py sell NVDA --why "short reason"
```

`account.py` enforces rules 2-4 in code. The account lives in `paper-bot/account/paperbot.db`
with a trade journal in `paper-bot/account/journal.jsonl`; **commit and push both after every
change**, or the trades are lost when the session ends.

## Each time the user asks for picks: use ALL the skills

The user wants every skill they installed used to find the best stocks (2026-11-04). Work
from a scratch folder (the skills read and write `trader/` in the current folder):
`S=<repo>/.claude/skills; mkdir -p /tmp/scan && cd /tmp/scan`. Report any step that could
not run (and why) in the message, and lower confidence for it.

1. **trader-alerts** `python3 $S/trader-alerts/alerts.py clock`: is the NYSE open today
   (it knows holidays and early closes)? Closed all day: only `account.py check` + a short message.
2. `python3 <repo>/paper-bot/account.py check` (sells anything that fell 3% from its peak).
3. **trader-scanner** `scan.py universe` then `scan.py daily --show 0` (all ~6,000 stocks).
4. `python3 <repo>/paper-bot/research.py --json research.json`: trend, strength vs SPY, calm
   for a 3% stop, one-year stop test, earnings hint (top 50 bullish setups, ~7 min).
5. **trader-pelosi** `ptr.py new --since <30 days ago> --read --dry-run --tickers <top ~10>`
   (~2 min; lines ending "<- SYM is on your list" are the hits): recent congressional trades; a recent purchase of a candidate is a small plus, a sale a small minus
   (filings come up to 45 days late: never decisive). Needs disclosures-clerk.house.gov.
6. Top ~10 by pre-score: **trader-news** `news.py --tickers ... --hours 168` (headlines and
   8-Ks; SEC user agent from `$SEC_USER_AGENT`) and **trader-analyst** `facts.py SYM ...`
   (revenue, margins, EPS vs a year ago; latest earnings 8-K date). A report due within ~5
   trading days can gap past the stop: lower the score.
7. Top ~5 finalists, each: **trader-exit** `exit.py plan --symbol SYM --entry <price>
   --date <today> --time "9:45 AM" --stop pct:3 --target 2R --write`, then
   **trader-timemachine** `timemachine.py run` (odds of hitting the -3% stop within one session,
   chance of being green at the close). If the session is open (after 9:45 ET),
   **trader-entry** `entry.py check SYM`: an opening-range breakout is a plus.
8. Score each 1-10 from all of the above. Show **only the 8+ stocks** (rule 5; if none, the one
   highest-scored stock marked "under 8, not bought", rule 6) with reasons in a few plain
   lines (Hebrew) and the stop: buy price and starting stop = buy price x 0.97
   (e.g. "קנייה 100$ ← סטופ 97$"), which then trails 3% under the peak.
9. Each 8+ stock, a second opinion before buying: **trader-budget** `size.py --write` and
   **trader-risk** `check.py` with `trader/config.json` set to the user's rules (budget = account
   value, max_position_pct 5, max_order_usd 5% of value, fractional true, no time windows) and
   `trader/portfolio.json` = the account's cash and positions as
   `{"cash": C, "positions": [{"ticker": T, "lots": [{"shares": Q, "cost": Q*last_price}]}]}`
   (without lots/cost, trader-risk values positions at 0 and wrongly blocks on the 5% cap); **trader-broker**
   `ticket.py show` renders the ticket for the record. There is no broker connected: nothing is
   sent anywhere, the trade happens only in the virtual account.
10. Buy every 8+ stock with `account.py buy` (one position per stock), unless the cash is gone.
    Never buy under 8 (unless the user decides, rule 2), never above 5%.
11. Commit and push `paper-bot/account/`.

## Daily automatic run (scheduled task, US trading days, 17:00 Israel time = after the open)

The user asked (2026-11-05) for the scan and the update on their stocks together, after the
market opens, at 17:00 Israel time (~10:00 ET, after trader-entry's 15-minute opening range).
Scheduled runs can start late in that hour: if it fires late, just run it. While the session is open
the day's volume is partial: run `scan.py daily --show 0 --min-volume 400000`. Each run:

1. If the NYSE is closed today (weekend or holiday), skip the scan: just run
   `account.py check`, and send a one-line message saying the market is closed plus the account.
2. Otherwise do the full "Each time the user asks for picks" procedure above, buy every 8+
   stock, commit and push.
3. End with ONE short message in Hebrew for the user's phone:
   - first, the holdings update: each open stock's price now, today's change in $ and % since
     yesterday's close (`marketdata.daily(SYM)[-2]` is yesterday's bar), P&L since the buy,
     peak and trailing stop, and any stop sale;
   - each 8+ stock: name, ticker, score, 2-3 reasons (naming the skills behind them), buy
     price, starting stop = price x 0.97 (show the calculation), and that it was bought;
   - which skills ran and which could not (one line);
   - or, if nothing reached 8: say so, then the one highest-scored stock with its score,
     reasons, price and would-be stop (price x 0.97), marked "under 8, not bought";
   - any stop-loss sales since the last run;
   - the account: total value, cash, P&L, open positions with their peak and current
     trailing stop (peak x 0.97).

## Daily close summary (scheduled task, US trading days, after the 16:00 ET close)

No scanning and no buying. Run `account.py check` (applies the trailing stop to the whole
day's bars), then `account.py report`, commit and push `paper-bot/account/`, and send ONE short
message in Hebrew: for every open stock, the buy price, the current trailing stop (peak x 0.97,
show the peak), and its profit/loss in $ and %; every stock sold (today or before) with its
buy price, sale price and profit/loss; and the account total (value, cash, total P&L).
On a day the NYSE was closed, one line saying so plus the account total.

Prices come from `paper-bot/marketdata.py`: Twelve Data first (the user's free personal key is
a network secret on api.twelvedata.com: the session never sees it; 8 requests a minute, 800 a
day, so research.py checks the top 50 by default, about 7 minutes), Yahoo as a fallback.

Yahoo rate-limits cloud IPs (HTTP 429). In your own scan scripts, pause ~0.5 s between
requests and retry 429s with a growing pause (5 s, 10 s, 15 s...) instead of dropping the stock.

Be honest in the scores: a score is a judgment from the data, not a promise, and the user
should hear when data was missing (a blocked source lowers confidence and the score).

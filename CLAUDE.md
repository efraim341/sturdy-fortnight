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
   **8 or more = green light: buy.** Under 8: do not buy.
3. **No more than 5% of the account's total value per trade** (fee included).
4. **Trailing stop: sell when a held stock falls 3% from its highest price since the buy.
   Zero patience.** (Changed by the user 2026-11-02; it was 3% under the buy price.)
   The stop starts at buy price x 0.97 and only moves up: always highest price x 0.97.
   `account.py check` replays every 5-minute bar since the buy, so a stop hit between
   checks is caught and filled at the stop (or at the open, if the stock gapped under it).
5. **Show the user only stocks that scored 8 or more.** Never list, mention or rank stocks
   under 8. If nothing scored 8+, say so in one line (and what was checked), with no names.

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

## Each time the user asks for picks

1. `python3 account.py check` first (sells anything that fell 3% from its peak).
2. Scan, from a scratch folder (the tools write `trader/` in the current folder):
   ```bash
   mkdir -p /tmp/scan && cd /tmp/scan
   python3 <repo>/.claude/skills/trader-scanner/scan.py universe
   python3 <repo>/.claude/skills/trader-scanner/scan.py daily --show 0
   python3 <repo>/paper-bot/research.py --json research.json
   ```
   `research.py` ranks the bullish setups by trend, strength vs SPY, calm (for a 3% stop), a
   one-year test of -3% stop vs +6% target, and flags headlines about upcoming earnings
   (a hint only: verify). Both tools wait and retry when Yahoo answers 429; let them finish.
3. Examine the top ~10 in depth: news and 8-Ks (`trader-news --tickers ...`), earnings and
   margins (`trader-analyst`, SEC), and the earnings date. A report due within ~5 trading days
   can gap the stock past the -3% stop: that lowers the score. A pre-score is not the score.
4. Score each 1-10. Show the user **only the 8+ stocks**, each with its score and reasons in a
   few plain lines (Hebrew), and always spell out the stop: buy price and starting stop
   = buy price x 0.97 (e.g. "קנייה 100$ ← סטופ 97$"), which then trails 3% under the peak.
5. Buy every stock that scored 8+ with `account.py buy` (one position per stock), unless the
   cash is gone. Never buy under 8, never above 5%.
6. Commit and push `paper-bot/account/`.

## Daily automatic run (scheduled task, US trading days, message ready by 16:30 Israel time)

The user asked for a daily message after a full scan. Each run:

1. If the NYSE is closed today (weekend or holiday), skip the scan: just run
   `account.py check`, and send a one-line message saying the market is closed plus the account.
2. Otherwise do the full "Each time the user asks for picks" procedure above, buy every 8+
   stock, commit and push.
3. End with ONE short message in Hebrew for the user's phone:
   - each 8+ stock: name, ticker, score, 2-3 reasons, buy price, starting stop = price x 0.97
     (show the calculation), and that it was bought;
   - or, if nothing reached 8: one line saying no stock reached 8 today (no names);
   - any stop-loss sales since the last run;
   - the account: total value, cash, P&L, open positions with their peak and current
     trailing stop (peak x 0.97).

Yahoo rate-limits cloud IPs (HTTP 429). In your own scan scripts, pause ~0.5 s between
requests and retry 429s with a growing pause (5 s, 10 s, 15 s...) instead of dropping the stock.

Be honest in the scores: a score is a judgment from the data, not a promise, and the user
should hear when data was missing (a blocked source lowers confidence and the score).

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
4. **When a held stock falls 3% (from its buy price), sell. Zero patience.** Every buy
   gets its stop at -3% from the buy price.
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
python3 account.py check                         # ALWAYS FIRST: live prices, enforces the -3% stop
python3 account.py state
python3 account.py buy NVDA --score 8.5 --why "short reason"   # refuses under 8, sizes to 5%, sets -3% stop
python3 account.py sell NVDA --why "short reason"
```

`account.py` enforces rules 2-4 in code. The account lives in `paper-bot/account/paperbot.db`
with a trade journal in `paper-bot/account/journal.jsonl`; **commit and push both after every
change**, or the trades are lost when the session ends.

## Each time the user asks for picks

1. `python3 account.py check` first (sell anything at -3%).
2. Scan for candidates (`trader-scanner` daily scan; a liquid list with `--symbols` if the
   universe download is blocked).
3. Examine the best candidates in depth: news and 8-Ks (`trader-news`), earnings and margins
   (`trader-analyst`), chart/setup (`trader-entry`), and odds with the stop at -3%
   (`trader-exit` + `trader-timemachine`).
4. Score each 1-10. Show the user **only the 8+ stocks**, each with its score and reasons in a
   few plain lines (Hebrew).
5. Buy every stock that scored 8+ with `account.py buy` (one position per stock), unless the
   cash is gone. Never buy under 8, never above 5%.
6. Commit and push `paper-bot/account/`.

Be honest in the scores: a score is a judgment from the data, not a promise, and the user
should hear when data was missing (a blocked source lowers confidence and the score).

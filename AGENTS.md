# AGENTS.md — Task-to-File Router
# READ THIS before exploring the repo. Open ONLY the listed files. Skip everything else.

## Morning Brief (9 AM IST email)
OPEN: src/reports/pre_market_brief.py
OPEN: src/runner.py → search "pre_market" block only
IF LLM broken: src/llm/ + src/juror/
IF DB broken: src/db/models.py → DailySignal, ConvictionScore only
SKIP: strategies/, market_chronicle.py, feedback_loop.py

## Mid-Session Report (12:30 PM IST)
OPEN: src/reports/market_chronicle.py → search "mid_session" block
OPEN: src/runner.py → search "mid_session" block only
SKIP: pre_market_brief.py, strategies/, feedback_loop.py

## Post-Market / EOD Report
OPEN: src/reports/post_market_report.py (full))
OPEN: src/reports/feedback_loop.py
IF LLM broken: src/llm/ + src/juror/
SKIP: pre_market_brief.py, strategies/

## DAWN Strategy (8:45 AM email + 9:15 market order)
OPEN: src/strategies/viper.py → search "DAWN" block
OPEN: src/reports/pre_market_brief.py → search "dawn" function
OPEN: src/runner.py → search "dawn" or "0845" block
SKIP: market_chronicle.py, feedback_loop.py, sniper/

## Conviction Score / Threshold
OPEN: src/strategies/viper.py → search "conviction_score" or "threshold"
OPEN: src/db/models.py → ConvictionScore table only
OPEN: src/juror/ → search "score" block
SKIP: reports/, feedback_loop.py

## VIPER Strategy
OPEN: src/strategies/viper.py (full)
OPEN: src/db_writer.py → search "viper" block only
SKIP: sniper/, reports/, llm/ (unless LLM scoring broken)

## PatternDB / Layer E
OPEN: data/pattern_db.json → inspect first
OPEN: src/db_writer.py → search "pattern" or "layer_e"
SKIP: everything else unless root cause points elsewhere

## Scheduler / Service Timing
OPEN: src/runner.py (full)
OPEN: /etc/systemd/system/voltedge.service
SKIP: all src/ except runner.py

## Token Refresh / Auth / Kite
OPEN: search repo for "access_token" or "kite_connect" → open that file only
SKIP: everything else

## DB / SQLAlchemy
OPEN: src/db/models.py
OPEN: src/db_writer.py
SKIP: reports/, strategies/, llm/, runner.py

## Email / SMTP
OPEN: search repo for "smtplib" or "send_email" → open that utility file only
OPEN: whichever report module is broken (see above)
SKIP: strategies/, db/, runner.py

## Juror / LLM Scoring
OPEN: src/juror/ (full)
OPEN: src/llm/ → only the vendor file relevant to the bug (gemini.py / grok.py / claude.py)
SKIP: strategies/, reports/, db/

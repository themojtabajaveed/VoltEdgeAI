# BUGS.md — Active Bugs (wipe entries when fixed)
# Last updated: 2026-04-14

## BUG-001 — SlotManager missing `.used` attribute
Symptom: Grok optimizer crashes every cycle
File: src/juror/ or src/llm/grok.py → search "SlotManager"
Fix needed: Add `.used` attribute initialization
Status: FIXED 2026-04-14
Fix applied: Added `self.used: int = 0` to `SlotManager.__init__()`, reset in `reset_daily()`, incremented in `allocate()` — src/strategies/slot_manager.py

## BUG-002 — Pre-market brief fires at wrong time
Symptom: Fires 06:00 UTC (11:30 IST) instead of 03:30 UTC (09:00 IST)
File: src/runner.py → search "pre_market" scheduler block
Fix needed: Correct UTC trigger time
Status: FIXED 2026-04-14
Fix applied: Changed `dt_time(8, 45)` → `dt_time(9, 0)` (09:00 IST = 03:30 UTC) and retry `dt_time(8, 50)` → `dt_time(9, 5)` — src/runner.py line 1748

## BUG-003 — Pre-market brief email not received
Symptom: Job runs but no email arrives
File: SMTP utility file (search "smtplib") + src/reports/pre_market_brief.py
Fix needed: Verify SMTP config, check for silent exception swallowing
Status: FIXED 2026-04-14
Fix applied: Replaced bare `except Exception: pass` in `_generate_emergency_fallback()` with `except Exception as fallback_e: logger.error(...)` — src/reports/pre_market_brief.py line 98

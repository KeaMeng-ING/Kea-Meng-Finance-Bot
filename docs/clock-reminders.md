# Clock In / Clock Out Reminders

Weekday (Mon–Fri) push-notification reminders for timesheet clock in/out.

These run as **Claude Code cloud routines**, not as code in this repo. The schedule
lives on Anthropic's servers and is managed at <https://claude.ai/code/routines>.
This file is a record of the configuration so it is versioned somewhere.

## Schedule

Local timezone is `Asia/Phnom_Penh` (UTC+7, no DST), so cron expressions are
stored in UTC with a −7h offset. Local 07:29–16:25 maps to UTC 00:29–09:25 on the
same calendar day, so `1-5` (Mon–Fri) is correct in both zones.

| Fires (local) | Intended time | Cron (UTC) | Notification |
| --- | --- | --- | --- |
| 07:29 | 7:30 AM clock in | `29 0 * * 1-5` | `Clock in now — 7:30 AM morning shift.` |
| 11:50 | 11:50 AM clock out | `50 4 * * 1-5` | `Clock out now — 11:50 AM lunch break.` |
| 12:59 | 1:00 PM clock in | `59 5 * * 1-5` | `Clock in now — 1:00 PM afternoon shift.` |
| 16:25 | 4:25 PM clock out | `25 9 * * 1-5` | `Clock out now — 4:25 PM end of day.` |

## Trigger IDs

| Reminder | Trigger ID |
| --- | --- |
| Clock In — 7:30 AM | `trig_014c3u8Rwd4axTdsYmxWZBqQ` |
| Clock Out — 11:50 AM | `trig_014so7t4mTcz4KYxkHyRdAdC` |
| Clock In — 1:00 PM | `trig_01VyrycyP6a9TE35tbh8Go5m` |
| Clock Out — 4:25 PM | `trig_013warX42Ji172wgge8RkHxL` |

Each routine runs `claude-sonnet-5` with `allowed_tools: ["PushNotification"]` and a
prompt that fires the notification and stops — no file reads, no shell commands.

## Why two reminders fire one minute early

The scheduler adds several minutes of random jitter to jobs landing on the `:00`
and `:30` minute marks, because those are the most heavily scheduled times across
the platform. Observed on this account:

- `30 0 * * 1-5` (7:30) resolved to **00:35:13Z** — 5m13s late
- `0 6 * * 1-5` (13:00) resolved to **06:03:39Z** — 3m39s late
- `50 4` and `25 9` resolved exactly, with `:00` seconds

Off-mark minutes are not jittered. Moving those two to `:29` and `:59` makes them
fire exactly, one minute early, instead of arriving three to five minutes late and
at an unpredictable time. The notification text still names the real clock time.

## Notes

- Delivery is via the Claude mobile app; Remote Control must be connected.
- Routines can be created and updated through the API, but **deletion** has to be
  done from the routines page in the browser.

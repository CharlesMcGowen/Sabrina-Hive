# Paper portfolio config: design

Status: implemented locally in `tools/kraken_paper_1000_forward.py` with seed
`data/control/paper_portfolios.json`, 14 tests passing. Not deployed to Ego, and the
running Ego service has not been restarted.

Implemented: add, edit, retire-when-flat. Not implemented: pause (no entry gate exists
in the decision code). Retire is refused while a portfolio has open positions, so its
exits keep running. A portfolio missing from the file is kept, not deleted.

## Problem

`tools/kraken_paper_1000_forward.py` defines its portfolios in the literal dict
`PORTFOLIO_SPECS` (line 708, six entries). Every decision loop iterates that dict
directly (lines 897, 1295, 1687, 2130, 3821, 4233), so the dict is the single source
of truth for which portfolios trade and with what parameters.

Two things stop an edit from ever taking effect:

1. Python reads the module once, at process start. A running service keeps its
   in-memory dict until it restarts.
2. The running service on Ego runs its own copy of the file. That copy was modified
   after the service started, so the live process has never seen the latest edits.

## Goal

Portfolio definitions can be added, changed, paused, or retired by editing one data
file. The running lab picks the change up on its next cycle, with no restart. The
existing decision code stays as it is.

## Design

### 1. One data file

`data/control/paper_portfolios.json`, a JSON object keyed by portfolio name. Each value
has exactly the fields `PORTFOLIO_SPECS` has today. The current six entries are the
seed for the first version.

Every value in `PORTFOLIO_SPECS` is a literal string, number, bool, or nested dict
(checked: no lambdas, no references to other constants), so the conversion is a
direct copy.

Top-level shape:

```json
{
  "version": 1,
  "portfolios": {
    "live_mirror_40bps": { "...same fields as PORTFOLIO_SPECS...": "" }
  }
}
```

An optional `"status"` field per portfolio: `"active"` (default), `"paused"`
(keeps its state, stops opening new positions), or `"retired"` (keeps its state and
reports, stops trading and drops out of the loops).

### 2. Reload in place

Keep `PORTFOLIO_SPECS` as a module-level dict. The loader updates it **in place**
(`clear()` + `update()`), never rebinds it. Every existing call site keeps reading the
same name.

A function `reload_portfolio_specs(path)` runs at the top of each cycle:

- Stat the file. If the mtime and size are unchanged, return.
- Parse and validate. If it fails, keep the previous specs, log the error, and carry on.
  A bad edit must never stop the lab.
- Validate required keys: `description`, `maker_fee_bps`, `taker_fee_bps`, `tp_bps`,
  `sl_bps`, `cat_bps`, `be_trig_bps`, `be_floor_bps`, `max_hold_sec`, `cooldown_sec`,
  `entry_params`, `majors_only`, and `max_spread_bps` inside `entry_params`.
- Apply the diff: new names are added to the dict, which lets the existing code at
  line 1687 create their state. Paused portfolios keep their keys with a flag the
  entry path checks. Retired portfolios are removed from the dict; their state stays
  in `paper_state.json` for reporting.

### 3. What changes on a reload

| Change | Effect |
| --- | --- |
| Edit a numeric parameter | Applies on the next cycle, to new decisions. Open positions keep their entry data. |
| Add a portfolio | New state is created with $1,000 at the next cycle (existing code, line 1687). |
| Pause | No new entries. Existing positions run their exit logic as usual. |
| Retire | Stops trading. Removed from loops. State and history remain. |

### 4. Limits to handle explicitly

- **Reserve policy.** `build_cash_reserve_policy` (line 888) freezes the reserve per
  run and lists the portfolios it knows. A portfolio added mid-run must be given the
  reserve policy explicitly, or it trades without the reserve. The loader should write
  the new name into that policy when it is added.
- **Second copy of the list.** The reserve policy and the cycle loops are not the only
  consumers. Any monitor card or plugin that hard-codes portfolio names needs to read
  the same file. I have not yet confirmed which ones do, so this needs a search on
  Ego before the change.
- **Local and Ego must be the same file.** Until the local folder is copied to Ego
  and kept in sync, edits won't reach the service.

### 5. Tests (local, before any deploy)

- Load the seed file and check it reproduces the current six specs exactly.
- Bad JSON keeps the previous specs and logs an error.
- Add, pause, and retire each produce the expected dict and state changes.
- Parameter edit takes effect on the next call to `step`.
- A portfolio added mid-run is created with start cash $1,000 and is included in
  the reserve policy.

### 6. Rollout

1. Implement the loader and seed file locally. Run the tests.
2. Copy the local folder to Ego next to the running copy, and diff them.
3. Restart the paper service once, to load the loader. This is the only restart.
   It needs your OK.
4. After that, portfolio changes are edits to the JSON file, with no restart.

### Open questions

- Should changes to `paper_portfolios.json` require review before they take effect, or
  should the lab apply them as soon as they're written? The lab is paper-only, so the
  risk is low, but the answer decides whether the loader needs an approval step.
- Should a removed portfolio be retired (kept in reports) or deleted from state?
  I'd suggest retiring.
- Which monitor cards, if any, hard-code portfolio names? Not yet searched on Ego.

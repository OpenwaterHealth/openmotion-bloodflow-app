# HIL Test Style Guide

Conventions for writing pytest-based UI tests against the Open-Motion
app. Distilled from the patterns we've converged on after
real-world failures in this repo and from public best-practice
references (links at the bottom).

The guide is opinionated about *what to do*, but every recommendation
ends with the symptom you'll see if you don't follow it — so the
"why" stays with the rule.

---

## 1. Scope and audience

These are **hardware-in-the-loop end-to-end UI tests**. They:

- launch the real BloodFlow app (frozen `Open-Motion.exe` or
  `python main.py` with `OPENWATER_FROM_SOURCE=1`),
- drive the QML UI with `pyautogui` mouse and keyboard input,
- inspect window state via `pywinauto` UI Automation (UIA),
- talk to physical hardware (cameras, console MCU) over USB / UART,
- and cycle hardware power via a Shelly WiFi outlet.

If you're writing **unit tests** for the SDK or pure logic, this
guide doesn't apply — use plain pytest with no fixture / no UI
overhead.

---

## 2. File organization

### Naming

```
tests/
├── conftest.py                   # fixtures + hooks shared by all tests
├── utils.py                      # plain helper functions
├── shelly.py                     # Shelly outlet driver
├── pytest.ini                    # markers + collection config
├── test_<feature>.py             # one feature per file
├── test_<feature>_abbreviated.py # short variant of a long test
└── STYLE_GUIDE.md                # this file
```

- One test file per **feature or modal**, not per class. `test_notes.py`
  exhausts the Notes textarea; `test_history.py` exhausts the History
  modal; etc.
- Test methods are numbered sequentially inside an incremental class
  (`test_01_open`, `test_02_…`) so pytest's default lexicographic
  collection order matches intended run order.

### File header

Every test file starts with a docstring that names what it covers,
why it exists, and any preconditions a runner needs. Mirror the
hardware-test-automation guidance from the FixturFab and Golioth
guides: a fresh contributor should be able to read the docstring
and know whether to run the test on their bench.

---

## 3. Markers: `dev` vs `release`

Two tiers, set per-file via `pytestmark = pytest.mark.<tier>`:

| Marker | Runtime | When it fires | Examples |
|---|---|---|---|
| `dev` | < ~5 min total | Every push to `next` | `test_history`, `test_notes`, `test_scan_settings` |
| `release` | up to ~70 min | Tag pushes only (release-pattern tags) | `test_scan_flow`, `test_clinicalmode`, `test_scan_auto_stop_bug` |

Rules of thumb:

- **dev**: fast modal-interaction smoke tests that don't run actual
  scans (or run < 60 s scans).
- **release**: real scans, multi-iteration repros, anything that
  power-cycles the console, or anything that takes > 5 min.

Don't mark a test both `dev` and `release` — pick one. If a test is
borderline, default to `release` and let it run nightly.

---

## 4. Fixture conventions

### Scope

- `session`: launched once per pytest run. Use for the app process
  itself (`app` fixture). **Symptom of getting this wrong**: the app
  relaunches between every test, your suite runs 50× longer, and
  any state held in the QML model resets between tests.
- `class`: setup that should run once per test class. Use for
  pre-seeding test data, dismissing leftover modals, calibrating
  panel button positions. **Symptom**: setup runs before every test
  method (function-scope) or never re-runs after a teardown
  (session-scope).
- `function` (default): per-test setup. Reserve for invariants
  that must hold at the start of *every* test (e.g.
  `_check_app_alive`, `_dismiss_leftover_modals_per_class`).

### `autouse=True`

Use sparingly. Every autouse fixture runs for every test in scope —
the cost compounds. Reach for it when the work has to happen for
correctness regardless of whether the test author remembered:

- `_check_app_alive(app)`: fail fast at the test boundary if the
  app crashed in the prior test, instead of cascading "App window
  not found" through 50 tests.
- `_dismiss_leftover_modals_per_class(app)`: send Escape at class
  boundaries so a stale modal from a prior class can't mask the
  current class's modal in the UIA tree.
- `_calibrate_panel_buttons_once(app)`: discover sidebar panel
  button screen positions once after the app launches, so every
  test that calls `click_panel(...)` gets correct coordinates for
  *this* machine's DPI / window size.

### Naming

- Fixtures follow `_helper_name` convention (leading underscore) when
  they're autouse and not requested by name in tests.
- Fixtures requested by name get plain names: `outlet`, `app`,
  `panel_buttons`.

### Cleanup

Always use `yield` + post-yield teardown for state that must be
restored. Always use `try/finally` inside test methods for state
that must be restored even when the test fails:

```python
def test_force_laser_fail_lifecycle(self, outlet, app):
    original_flag = _read_force_laser_fail()
    try:
        # ... toggle flag, run scan, assert toast appears ...
    finally:
        # ALWAYS restore — even if the assertions failed:
        _kill_bloodflow_processes()
        _set_force_laser_fail(original_flag)
        outlet.power_cycle(off_time=5.0)
        _launch_app()
```

**Symptom of getting this wrong**: a single failing assertion
poisons the bench config for every subsequent test that runs in the
same session — sometimes for hours.

---

## 5. Test structure: AAA

Use the **Arrange / Act / Assert** pattern. One state-changing
"act" per test. The arrange step gets everything ready; the act
fires the behaviour; the assert verifies the resulting state.

```python
def test_03_user_label(self, app):
    # ── Arrange
    require_focus()
    pyautogui.press("tab")     # land focus inside modal
    time.sleep(0.5)

    # ── Act
    win = uia_window()
    found = any(
        (e.window_text() or "").strip() in ("User Label:", "User Label")
        for e in win.descendants()
    )

    # ── Assert
    assert found, "'User Label' text not found in scan-settings modal."
```

Multiple acts per test means multiple things can fail — and you
won't know which one. If you find yourself needing two acts, split
into two tests with `@pytest.mark.incremental` so the second is
xfailed automatically when the first fails.

---

## 6. Stable interaction patterns

This is the section where most flakiness lives. The single highest-
leverage rule: **never click hardcoded screen coordinates**. Every
public guide on UI test stability says this; we relearned it the
hard way through three full HIL runs of "click missed the hitbox."

### Order of preference for finding a UI target

1. **UIA descendants by exact title** — most stable, works
   regardless of DPI / resolution / window size.
   ```python
   for e in uia_window().descendants(title="Download"):
       click_element_center(e)
   ```

2. **UIA descendants walked + matched by `window_text()`** — same
   reliability when Qt's accessibility bridge surfaces the text but
   not as the title. This is the path our `calibrate_panel_buttons`
   uses for sidebar buttons.

3. **Calibrated screen coordinates derived from QML layout
   constants** — only when UIA fails. Math against `w.left`,
   `w.top`, `w.bottom` from `pygetwindow` so the value follows the
   window if it moves.

4. **Static rx/ry ratios** — fallback only. These break the moment
   DPI scaling, window size, or banner state changes.

### Concretely: use `click_panel(label)`

Never write `click_sidebar(*SIDEBAR_X, "label")` for a panel
button. Use `click_panel("History")`. The first call calibrates
once via UIA → QML layout → ratio fallback, and caches the result
for the session. Subsequent calls are instant lookups.

### Recalibrate after window changes

If a test resizes or moves the app window, call
`recalibrate_panel_buttons()` afterwards. Cached coordinates from
the prior window state are now wrong. **Symptom**: the next click
lands on the desktop, focus is lost, the next test sees "App window
not found."

---

## 7. Waits: poll, don't sleep

A `time.sleep(N)` for any UI condition is a flakiness time-bomb.
Sleep is right for *exactly two* things:

1. Letting an animation visibly finish (max ~500 ms).
2. Pacing keypresses (`time.sleep(0.05)` between
   `pyautogui.typewrite` characters).

For everything else — modal opened, button enabled, scan complete,
state transition logged — **poll until the condition is true**:

```python
def _wait_for_combobox(idx: int, timeout: int = 15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        cbs = uia_window().descendants(control_type="ComboBox")
        if len(cbs) > idx:
            return cbs[idx]
        time.sleep(0.5)
    return None
```

For log-driven waits (e.g. "did the SDK log a CONNECTED state
transition?"), use the `wait_for_pattern` helper in `utils.py` —
tails the bloodflow app log file for a regex match with a timeout.

**Symptom of using sleep instead**: the test passes on a fast
machine and fails on the runner. Or vice versa. Fix the wait, not
the timeout.

---

## 8. Modal hygiene

The QML app uses overlay modals (Scan Settings, Notes, History,
Session Notes, Settings, Contact Quality). Two rules:

1. **Every test that opens a modal closes it.** Either explicitly
   (`pyautogui.press("escape")`) or by completing the test's
   workflow that ends in a closed-modal state.

2. **Every test class that scans assumes Session Notes will
   auto-open at scan end and dismisses it before the test method
   returns.** This is the rule we forgot, and it caused a Session
   Notes modal from `test_connection_redesign.test_03` to mask
   every subsequent class's UIA queries with `"Scan stopped"` text.

Defence in depth: the autouse `_dismiss_leftover_modals_per_class`
fixture in `conftest.py` sends Escape at every class boundary as a
catch-all. Don't rely on it as your primary cleanup — fix the
source, but keep the safety net.

---

## 9. Diagnostic logging

When a test fails, the failure message should point at the root
cause. Generic asserts (`assert result`) waste hours.

### Good failure messages

```python
assert _wait_for_safety_toast(SAFETY_TRIP_TIMEOUT), (
    f"Laser safety toast did not appear within "
    f"{SAFETY_TRIP_TIMEOUT}s with forceLaserFail=true. The fault "
    f"laser params should have tripped the safety interlock "
    f"immediately on first laser pulse."
)
```

### Diagnostic dumps on UIA misses

When a UIA-based assertion fails, dump what UIA *did* see:

```python
log.warning(
    f"  test_03_user_label diagnostic — first 30 UIA texts seen "
    f"in window: {last_seen[:30]}"
)
pytest.fail("'User Label' not found ...")
```

That single line of diagnostic was the turning point on a session
that had 50 cascading "App window not found" failures — it told us
the only thing visible to UIA was a leftover Session Notes modal.

### Logging in steady-state operations

`log.info(...)` everything that affects state: clicks, key presses,
config changes, power events, modal state. Not every line will
matter, but the *one* you need always seems to be the one you
didn't log.

---

## 10. Race tolerance

UI / hardware tests live with races: a button click fires before the
modal renders, a state transition fires before the slot's
prior call returns. Three patterns:

1. **Wrap risky hardware calls in try/except inside Qt slots.** A
   raised exception from a Qt slot terminates the bloodflow
   process. We learned this when a single-frame race in
   `_on_handle_state_changed` calling `console.tec_voltage` after
   the console disconnected killed the app mid-test:

   ```python
   try:
       self._on_handle_state_changed_impl(handle, old, new, reason)
   except Exception:
       logger.exception("state-change handler crashed; swallowing")
   ```

2. **Retry on empty results.** Clipboard reads, UIA descendant
   walks, COM-port enumeration — if the result is empty *or* the
   first attempt looks suspicious, retry up to 3 times with a
   short backoff. Don't retry indefinitely.

3. **Tolerate "uncertain" detection states.** Some signals (banner
   visible? scan running?) UIA can't reliably answer. When the
   detector returns `None`, fall back to a behaviour that hits
   either branch — for example, click in the overlap zone of the
   "with banner" and "without banner" hitboxes.

---

## 11. Cross-machine portability

Different machines have different DPI scales, default window
sizes, and tool environments. Three concrete rules:

- **Don't hardcode pixel offsets without deriving them from the
  current window's rect.** `(284, 248)` works on one machine and
  misses on another; `w.left + 56, w.top + 113` works on both.
- **Calibrate at runtime, cache for the session.** Calibration is
  a one-time cost per session — don't re-discover for every click.
  Use the patterns in `utils.py: calibrate_panel_buttons`.
- **Always assume the runner has a polluted clipboard and
  unrelated processes running.** Clear the clipboard before
  reading from it; identify your own app process by name; never
  enumerate "all windows" without filtering by `APP_KEYWORDS`.

---

## 12. Anti-patterns we've burned ourselves on

A short list of things that cost us a HIL run apiece:

- **`time.sleep(60)` instead of polling for the actual condition.**
  The runner needs longer sometimes; the dev box less. Fix the
  wait, not the constant.
- **Using `pyautogui.click(x, y)` with hardcoded `(x, y)`.** Same
  failure mode at every DPI scale, but we forget every time.
- **Multiple `act` steps in one test.** Now you don't know which
  step failed when the assert fires.
- **Forgetting to dismiss Session Notes modal after a scan.** The
  modal lingers across classes, UIA exposes only its contents,
  every later class's modal lookup fails.
- **No `try/finally` around state-mutating tests.** A failing
  assertion leaves the bench in the modified state for the rest
  of the session, which can take an hour to surface.
- **`assert result` with no message.** "AssertionError" with no
  context is the worst possible failure mode.
- **Catching `Exception` and silently passing.** Now the test
  passes (because it didn't raise) but didn't actually verify
  anything. If a flaky API needs a try/except, log a warning at
  minimum and fail the assertion, not the test fixture.

---

## 13. Quick checklist for a new test

Before you commit a new HIL test, run through:

- [ ] One feature per file, marker set (`dev` or `release`).
- [ ] File-header docstring names the feature, the preconditions,
      and the expected outcome.
- [ ] Sequential method numbering (`test_01_…`, `test_02_…`) if
      using `@pytest.mark.incremental`.
- [ ] Setup is in a `class`-scoped autouse fixture (not at module
      load time).
- [ ] All clicks on sidebar panel buttons use `click_panel(label)`,
      not `click_sidebar(*SIDEBAR_X, ...)`.
- [ ] All waits are polling-based; the only `time.sleep(N)` calls
      are < 1 s and pace input or animations.
- [ ] State-mutating tests use `try/finally` to restore state.
- [ ] Tests that open a modal close it (or end in a workflow that
      closes it).
- [ ] Tests that run a scan dismiss the auto-opened Session Notes
      modal at the end.
- [ ] Failure messages name what was checked, what the actual
      value was, and a hint at the likely cause.
- [ ] If the test relies on hardware (Shelly outlet, USB cameras),
      it's `release`-marked and `pytest.skip`s cleanly when the
      hardware isn't reachable.

---

## References

External guidance this guide is built on:

- [Flaky tests — pytest documentation](https://docs.pytest.org/en/stable/explanation/flaky.html) — the canonical explanation of what makes a test flaky and how to control state.
- [Anatomy of a test — pytest documentation](https://docs.pytest.org/en/stable/explanation/anatomy.html) — the AAA pattern in pytest's own words.
- [Hardware Test Automation Guide: Pytest Tutorial — FixturFab](https://www.fixturfab.com/articles/pytest-hardware-test-automation) — fixture organization for HIL tests, the conftest pattern.
- [Automated hardware testing using pytest — Golioth](https://blog.golioth.io/automated-hardware-testing-using-pytest/) — running pytest against real embedded hardware.
- [How Golioth uses Hardware-in-the-Loop (HIL) Testing: Part 2](https://blog.golioth.io/golioth-hil-testing-part2/) — fixture lifecycle, board provisioning, multi-device tests.
- [Parameterized Tests Using Pytest for HIL Testing — Altium](https://resources.altium.com/p/parameterized-tests-using-pytest-hardware-loop-testing) — parametrized HIL test design, `@pytest.mark.parametrize` patterns.
- [Test Automation Best Practice #4: Use Reliable Locators — Ranorex](https://www.ranorex.com/blog/best-practices-4-use-stable-locators/) — why coordinate-based clicks are the worst kind of locator.
- [UI Testing Locators Guide — Python in Plain English](https://python.plainenglish.io/ui-testing-locators-guide-how-to-write-stable-and-maintainable-selectors-c922ebde86d2) — order of preference for selectors.
- [Mastering Waits in UI Automation](https://learnautomatedtesting.com/blog/mastering_waits_in_ui_automation/) — explicit / fluent waits vs hardcoded sleeps.
- [Hardcoded Waits in Test Automation: When (If Ever) Are They Justified? — CloudQA](https://cloudqa.io/hardcoded-waits-test-automation/) — the short answer: almost never.
- [Arrange-Act-Assert: A Pattern for Writing Good Tests — Automation Panda](https://automationpanda.com/2020/07/07/arrange-act-assert-a-pattern-for-writing-good-tests/) — clearest write-up of AAA in practice.
- [7 pytest Fixture & Param Tricks That Kill Flaky Tests — Modexa](https://medium.com/@Modexa/7-pytest-fixture-param-tricks-that-kill-flaky-tests-b985d527064a) — fixture-based determinism / isolation tricks.
- [How to Find and Fix Flaky Tests in pytest — DEV](https://dev.to/byteframe/how-to-find-and-fix-flaky-tests-in-pytest-1p9a) — `pytest-repeat`, `pytest-rerunfailures`, `pytest-replay` for flakiness diagnosis.

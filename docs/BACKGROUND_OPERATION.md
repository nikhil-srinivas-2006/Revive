# Background operation and operator controls

Updated: 2026-10-21. Windowless start/stop/restart, live status, cooperative worker
interruption, watcher recovery and a reviewable login-task export are implemented.
This supports supervised background trials. Native failure toasts, actual login
deployment and longer unattended qualification remain pending.

## Commands

The current development `config.local.toml` and `.runtime/background.toml` share
one automatic quality profile: `test_photos/` input, `.runtime/quality_output/`
derivatives, and `.runtime/quality_state/` state. The earlier automatic profile
is retained in `.runtime/legacy_automatic.toml` for comparison.
Colorization and upscaling are selected independently from each photo's pixels
and dimensions. There is no per-photo command. The new state processes existing
inputs on its first run and retains earlier qualification outputs separately.

```powershell
revive --config config.local.toml start
revive --config config.local.toml status
revive --config config.local.toml stop
revive --config config.local.toml restart
```

The foreground `run` and batch `run --once` commands remain available. Both expose
their run status and accept the same stop requests. Control commands accept
`--timeout` in seconds, default 20, up to 120. A timeout reports that readiness or
shutdown has not completed; it never kills an arbitrary PID.

Start uses Windows `CREATE_NO_WINDOW`, without a shell or elevated account. The
parent waits until the new monitor has opened its state and started event watching
or reconciliation fallback. The configured settings are hashed and verified by
the child before processing: editing configuration during launch fails the launch.
An existing monitor with different settings requires an explicit restart.

Stop closes the active worker's Windows Job Object before draining inherited
pipes. Inference and snapshot work are interrupted at their control checks; the
job becomes `retry_pending` / `INTERRUPTED` with no consumed retry attempt. Partial
private outputs are discarded. A derivative already in validated publication may
finish. Restart resumes the durable queue. It does not implicitly reprocess
completed revisions under changed model/settings policies.

Failed jobs can still be retried through the existing stopped-monitor workflow:

```powershell
revive --config config.local.toml stop
revive --config config.local.toml retry 123
revive --config config.local.toml start
```

Input/output root changes still require a new application-data directory; the
existing database scope guard is retained. Controls do not reset history or delete
outputs. Reprocessing completed revisions is not implemented by this milestone.

## Status and control ownership

`status` adds `monitor` and `needs_attention` to the existing counts/incidents.
It also reports `resources`: available physical RAM and system commit, plus the
effective worker physical-memory budget and committed-memory ceiling, all in MiB.
These are current readings; earlier attempts may have seen different budgets.
`RAM_ADMISSION` keeps a job pending without consuming further retry attempts,
even if previous attempts reached the normal retry limit. The running monitor
checks again after at least 60 seconds (or the first configured retry delay if
longer). It does not increase memory limits, disable requested AI stages, or
publish an unenhanced result as a successful enhancement. A stopped monitor
cannot retry; batch `run --once` leaves deferred work for the next invocation.
The monitor records its run token, diagnostic PID, state, heartbeat, configuration
identity and event/reconciliation mode. The latest 20 failed/retry-pending jobs
include error codes, attempts, next retry time and suggested actions. Incidents
are retained history, not a claim that every old condition is still active.

`monitor-run.lock` proves that a monitor owns the profile. A stale JSON file or
reused PID alone never counts as a running monitor. A separate manager lock
serializes start/stop/restart; the original mutator/single-instance lock still
protects SQLite and publication. SQLite is accessed only by its owning monitor
thread; the control listener does not write job state.

On Windows, stop signals a native named event containing a random per-run token.
A small token-matched request file provides a fallback, including across Windows
logon sessions. The listener waits between checks and refreshes the heartbeat
every five seconds. Losing control or heartbeat storage stops processing and
reports a failure rather than leaving an unmanageable hidden monitor active.
Session JSON updates use temporary files and atomic replacement. None of this
imports image/ML libraries into the monitor or adds a network service.

## Watch recovery and scheduling gaps

The monitor compares input-directory identity during reconciliation. If the
directory disappears or is replaced, it stops the stale observer and attaches a
new observer when a valid directory returns. While event watching is unavailable,
scans occur at least every five seconds (or the shorter configured interval).
Reparse/link checks remain in force.

A loop gap exceeding five seconds forces reconciliation and watch reattachment
before more queue processing. Tests simulate this without suspending the machine.
Real sleep/wake cycles still require hardware qualification. Worker wall-time
deadlines continue to include elapsed suspension; this milestone does not claim
suspend-adjusted compute budgets. Slow/unavailable storage can delay shutdown;
the control command returns a bounded timeout instead of force-killing a PID.

An interrupted first scan does not mark first-run initialization complete. This
preserves `process_existing=false` when scanning resumes, so unvisited existing
photos do not accidentally become new arrivals.

## Login startup: exported definition

```powershell
revive --config config.local.toml startup export
```

The definition is written as BOM-marked UTF-16 `startup-task.xml` in the profile's
application data. It uses the current user's SID, `InteractiveToken`,
`LeastPrivilege`, a 15-second login delay, `pythonw.exe`, an unlimited monitor
execution time and up to three restart attempts one minute apart. It neither
requires network availability nor wakes the laptop. Battery changes do not stop
the task. Task Scheduler's duplicate-instance policy is IgnoreNew, in addition to
Revive's own instance lock.

Export is separate from task registration. No task, service, registry startup key
or application notification identity was installed during this milestone. The
definition binds to the current Python environment/configuration paths and needs
regeneration after moving that environment. Native notification registration and
login/restart/uninstall deployment are later explicit setup work.

Windows accepted the exported definition through a temporary Task Scheduler
`ITaskDefinition.XmlText` object. This validates native XML parsing/schema handling;
no task was registered, and no actual login/crash-restart behavior is implied.

## Development evidence

The earlier `.runtime/background.toml` smoke profile pointed at `test_photos/`,
`.runtime/background_output/` and `.runtime/background_state/`. On its first scan
it skipped the ten existing inputs, as configured. Its windowless monitor stayed
responsive with native events active across separate CLI invocations and then
stopped cleanly. No photo inference or source edits were needed for this smoke
test. The test monitor is stopped. Its validated task definition is
`.runtime/background_state/startup-task.xml`.

Automated tests use synthetic images and temporary folders. They cover native
start/stop/restart and live arrival, duplicate start, changed configuration,
token isolation, stale state/PIDs, active-worker interruption and resumption,
multiple worker communication timeouts, directory replacement, simulated resume,
readiness failure cleanup, status guidance, interrupted initial scanning and
control failure. The complete suite result is recorded in IMPLEMENTATION_STATUS.md.

Remaining acceptance work includes native failure toasts and their delivery test,
actual login/crash-restart deployment, manual sleep/wake tests, read-only/disk-full
control recovery, a seven-day soak, idle-resource measurements and representative
laptop/AI-quality reviews. A syntactically valid task and passing functional tests
do not establish unattended production readiness.

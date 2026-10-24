"""The governing loop: sense -> decide -> act -> publish -> record, once per poll.

Which containers it touches:

* containers labelled ``heat-governor.role=batch`` -- duty-cycled and CPU-capped;
* containers labelled ``heat-governor.role=serving`` -- never paused; CPU-capped
  only with ``heat-governor.cpu-cap=true``;
* plus any the operator names with ``--batch`` / ``--serving`` (for containers
  started without labels). Everything else is left alone.

And, only when the config lists them (``[[units]]``), systemd user units such
as ``app.slice``: a CPUQuota by level (:mod:`heat_governor.units`), or sized by
the learning loop of :mod:`heat_governor.adaptive` (``mode = "adaptive"``).

Without ``apply`` the loop still senses, publishes the admission signal and
records -- all harmless -- but changes no container (it logs what it would do).
"""

from __future__ import annotations

import os
import signal
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from heat_governor import __version__, platforms
from heat_governor.actuators import CpuCapper, DutyCycler, Ledger
from heat_governor.adaptive import AdaptiveLoop, ModelStore, Trend, UsageMeter
from heat_governor.config import Config, ConfigError, ContainerRule, UnitRule
from heat_governor.config import changed as config_changed
from heat_governor.config import diff as config_diff
from heat_governor.config import load as load_config
from heat_governor.docker import Container, Docker, DockerError
from heat_governor.gpuclock import GpuClock
from heat_governor.platforms import Profile
from heat_governor.policy import Controller, Decision, unit_cpus
from heat_governor.recorder import Recorder, boot_id
from heat_governor.sense import Reading, Sensor
from heat_governor.state import SCHEMA, StateServer, write_state
from heat_governor.units import Systemctl, SystemctlLike, UnitCapper, containing


@dataclass
class Options:
    apply: bool = False
    poll_s: float = 1.0
    state_path: Path | None = None
    data_dir: Path | None = None
    listen: tuple[str, int] | None = None
    batch: tuple[str, ...] = ()
    serving: tuple[str, ...] = ()
    serving_cpu_cap: bool = False  # cap named --serving containers too
    use_labels: bool = True
    discover_every_s: float = 5.0
    containers: tuple[ContainerRule, ...] = ()  # from the config file
    units: tuple[UnitRule, ...] = ()  # systemd user units to CPU-cap (config [[units]])
    gpu_clock: bool = False  # lower the GPU clock ceiling per gpu_clock_bands
    platform: str | None = None  # explicit --platform (else config, else detected)
    config: Config | None = None  # re-read while running when it changes
    cli_set: frozenset = frozenset()  # option names given on the command line (win over the file)
    reload_every_s: float = 2.0
    record: bool = True
    fsync: bool = True
    log: Callable[[str], None] = print
    extra: dict = field(default_factory=dict)


def utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Daemon:
    def __init__(
        self,
        profile: Profile,
        opts: Options,
        *,
        sensor: Sensor | None = None,
        docker: Docker | None = None,
        gpuclock: GpuClock | None = None,
        systemctl: SystemctlLike | None = None,
        clock: Callable[[], float] = time.time,
        host_cpus: int | None = None,
    ) -> None:
        self.profile = profile
        self.opts = opts
        self.sensor = sensor or Sensor(profile.zones, gpu=profile.gpu)
        self.docker = docker
        self.clock = clock
        self.ctrl = Controller(profile)
        data = opts.data_dir
        self.ledger = Ledger((data or Path(".")) / "ledger.json")
        self.capper = (
            CpuCapper(self.docker, self.ledger, self._log, apply=opts.apply) if docker else None
        )
        self.cycler = (
            DutyCycler(
                self.docker,
                self.ledger,
                self._log,
                apply=opts.apply,
                period_s=profile.duty_period_s,
            )
            if docker
            else None
        )
        self.recorder = (
            Recorder(data / "records", fsync=opts.fsync) if (data and opts.record) else None
        )
        self.server = StateServer(*opts.listen) if opts.listen else None
        self.containers: dict[str, Container] = {}
        self._discovered_at = 0.0
        self._observed_at = 0.0
        self._stop = threading.Event()
        self.last: Decision | None = None
        self.host = socket.gethostname()
        if gpuclock is None and opts.gpu_clock:
            gpuclock = GpuClock()
        self.gpuclock = gpuclock
        self._reload_at = 0.0
        self._gpu_retry_at = 0.0
        self.config_error: str | None = None
        self.systemctl = systemctl
        self.unit_capper: UnitCapper | None = None
        self._unbanded: set[str] = set()
        self.host_cpus = host_cpus or os.cpu_count() or 1
        self.adaptive: dict[str, AdaptiveLoop] = {}  # unit -> its loop (mode = adaptive)
        self.trends: dict[str, Trend] = {}  # unit -> temperature readings since its last step
        self._adaptive_store: ModelStore | None = None
        self.unit_use: dict[str, float | None] = {}  # unit -> CPUs used, last measured
        self.meter = UsageMeter(self._unit_usage)
        if opts.units:
            self._ensure_unit_capper().set_dry({u.name for u in opts.units if u.dry_run})
            self._sync_adaptive(opts.units)

    # -- logging: to the log sink and into the record
    def _log(self, msg: str) -> None:
        self.opts.log(f"{utc()} {msg}")
        if self.recorder:
            self.recorder.write({"t": self.clock(), "event": "action", "msg": msg})

    # -- containers
    def discover(self) -> None:
        if not self.docker:
            return
        found = self._find_containers()
        gone = set(self.containers) - set(found)
        for cid in gone:
            self._left(self.containers[cid])
        for cid in set(found) - set(self.containers):
            c = found[cid]
            capped = "yes" if c.cpu_cap or c.role == "batch" else "no"
            self._log(f"governing {c.name} role={c.role} cpu-cap={capped}")
        self.containers = found

    def _find_containers(self) -> dict[str, Container]:
        found: dict[str, Container] = {}
        if self.opts.use_labels:
            try:
                for c in self.docker.labelled():
                    found[c.id] = c
            except DockerError as exc:
                self._log(f"discover: {exc}")
        rules = [ContainerRule(n, "batch", True) for n in self.opts.batch]
        rules += [ContainerRule(n, "serving", self.opts.serving_cpu_cap) for n in self.opts.serving]
        rules += list(self.opts.containers)
        for rule in rules:
            try:
                c = self.docker.inspect(rule.name)
            except DockerError:
                continue
            cap = rule.role == "batch" or rule.cpu_cap or c.cpu_cap
            found[c.id] = Container(c.id, c.name, c.state, rule.role, cap, c.nano_cpus)
        return found

    def _left(self, old: Container) -> None:
        """``old`` left the governed set: give back any CPU cap we put on it."""
        self._log(f"container {old.name} left the governed set")
        cid = old.id
        if self.capper and (cid in self.ledger.nano_cpus or self.capper.current.get(cid)):
            self._fenced(f"release {old.name}", lambda: self.capper.set(old, None))
            self.capper.current.pop(cid, None)

    def batch(self) -> dict[str, Container]:
        return {k: c for k, c in self.containers.items() if c.role == "batch"}

    def capped(self) -> list[Container]:
        return [c for c in self.containers.values() if c.role == "batch" or c.cpu_cap]

    # -- systemd user units
    def _ensure_unit_capper(self) -> UnitCapper:
        if self.unit_capper is None:
            if self.systemctl is None:
                self.systemctl = Systemctl()
            self.unit_capper = UnitCapper(
                self.systemctl, self.ledger, self._log, apply=self.opts.apply
            )
        return self.unit_capper

    @property
    def adaptive_store(self) -> ModelStore:
        """What the adaptive loops learned, ``<data_dir>/adaptive.json`` (read on
        first use, written after each accepted sample)."""
        if self._adaptive_store is None:
            path = (self.opts.data_dir or Path(".")) / "adaptive.json"
            self._adaptive_store = ModelStore(path, self._log)
        return self._adaptive_store

    def _unit_usage(self, unit: str) -> int | None:
        return self.systemctl.cpu_usage_usec(unit) if self.systemctl else None

    def _sync_adaptive(self, units: tuple[UnitRule, ...]) -> None:
        """One loop per adaptive unit; a reload keeps each loop's cap and model."""
        want = {u.name: u for u in units if u.mode == "adaptive"}
        for name in set(self.adaptive) - set(want):
            del self.adaptive[name]
            self.trends.pop(name, None)
        for name, r in want.items():
            self.trends.setdefault(name, Trend())
            loop = self.adaptive.setdefault(name, AdaptiveLoop())
            loop.configure(r.min_cpus, r.engage, r.interval_s, r.target_rate_c_min)
            loop.model = self.adaptive_store.model(self.profile.name, name)

    def _unit_step(self, rule: UnitRule, d: Decision, now: float) -> None:
        """Measure the unit, size its cap (by level or by its loop), hand it to the
        capper (which applies the ledger, dry-run and missing-unit rules)."""
        name = rule.name
        loop = self.adaptive.get(name)
        if loop is None:
            self.unit_use[name] = self.meter.sample(name, now)
            self.unit_capper.set(name, unit_cpus(d.level, self.unit_bands(rule)))
            return
        trend = self.trends[name]
        trend.add(now, None if d.reason == "sensor_lost" else d.temp_c)
        if loop.due(now):
            use = self.unit_use[name] = self.meter.sample(name, now)
            live = self.unit_capper.applies(name)
            if not live:
                loop.pending = None  # a change not made has no response to learn from
            rate = trend.take()
            step = loop.step(d.level, rate, use, self.host_cpus, now, learn=live)
            if step.lesson:
                self._log(f"adaptive {name}: {step.lesson}")
            if step.changed:
                self._log(step.describe(name, dry=not live))
            if step.learned:
                self.adaptive_store.save()
        self.unit_capper.set(name, loop.cap)

    def unit_bands(self, rule: UnitRule) -> tuple[tuple[str, float], ...]:
        """A unit's own bands replace the platform default entirely."""
        bands = rule.bands or self.profile.unit_bands
        if not bands and rule.name not in self._unbanded:
            self._unbanded.add(rule.name)
            self._log(
                f"unit {rule.name}: no bands and platform {self.profile.name} declares none: "
                "never capped (give warm/hot/critical in [[units]])"
            )
        return bands

    def _set_units(self, units: tuple[UnitRule, ...]) -> None:
        old = {u.name for u in self.opts.units}
        gone = old - {u.name for u in units}
        self.opts.units = units
        if gone and self.unit_capper:
            self.unit_capper.release(gone)
        self._sync_adaptive(units)
        if units:
            self._ensure_unit_capper().set_dry({u.name for u in units if u.dry_run})
            if {u.name for u in units} != old:
                self._note_self_inside()
        self._unbanded.clear()  # re-report an unbanded unit after a reload

    def _note_self_inside(self, cgroup_file: Path = Path("/proc/self/cgroup")) -> None:
        for name in containing([u.name for u in self.opts.units], cgroup_file):
            self._log(
                f"note: heat-governor itself runs inside {name}, so that unit's cap bounds it "
                "too; it needs little CPU, but under heavy contention its share follows "
                "CPUWeight (e.g. CPUWeight=1000 on heat-governor.service)"
            )

    # -- configuration reload
    def maybe_reload(self, now: float) -> None:
        cfg = self.opts.config
        if cfg is None or now - self._reload_at < self.opts.reload_every_s:
            return
        self._reload_at = now
        if not config_changed(cfg):
            return
        try:
            new = load_config(cfg.path)
        except ConfigError as exc:
            msg = f"config {cfg.path} rejected, keeping the last good one: {exc}"
            if msg != self.config_error:
                self._log(msg)
                self.config_error = msg
            cfg.mtime = cfg.path.stat().st_mtime if cfg.path and cfg.path.exists() else None
            return
        try:
            profile = platforms.resolve(self.opts.platform, config=new.data)
        except ValueError as exc:
            self._log(f"config {cfg.path} rejected, keeping the last good one: {exc}")
            cfg.mtime = new.mtime
            return
        changes = config_diff(cfg.data, new.data)
        later = self._restart_only_changes(cfg.daemon, new.daemon)
        self.apply_config(new, profile)
        self.config_error = None
        self._log(
            f"config reloaded from {new.path}: " + ("; ".join(changes) or "no effective change")
        )
        if later:
            self._log(
                "these [daemon] keys take effect only after a restart "
                f"(systemctl --user restart heat-governor): {', '.join(later)}"
            )

    def apply_config(self, cfg: Config, profile: Profile) -> None:
        self.opts.config = cfg
        self.profile = profile
        self.ctrl.set_profile(profile)
        self.sensor.watched = profile.zones
        self.sensor.gpu = profile.gpu
        if self.cycler:
            self.cycler.period_s = profile.duty_period_s
        d = cfg.daemon
        for key in ("poll_s", "use_labels", "discover_every_s"):
            if key in d and key not in self.opts.cli_set:
                setattr(self.opts, key, d[key])
        if "gpu_clock" in d and "gpu_clock" not in self.opts.cli_set:
            self.set_gpu_clock(bool(d["gpu_clock"]))
        self.opts.containers = tuple(cfg.containers)
        self._set_units(tuple(cfg.units))
        self._discovered_at = 0.0  # re-discover on the next tick

    RESTART_ONLY = ("apply", "listen", "state", "data_dir", "record", "fsync", "reload")

    def _restart_only_changes(self, old: dict, new: dict) -> list[str]:
        return [k for k in self.RESTART_ONLY if old.get(k) != new.get(k)]

    def set_gpu_clock(self, on: bool) -> None:
        if on == self.opts.gpu_clock:
            return
        self.opts.gpu_clock = on
        if on:
            self.gpuclock = self.gpuclock or GpuClock()
            self.gpuclock.probe()
            self._log(f"gpu clock control on: {self.gpuclock.reason}")
        else:
            self._release_gpu("gpu clock control off")

    # -- GPU clock ceiling
    GPU_RETRY_S = 30.0  # after a failed helper call, wait before trying again

    def _gpu(self, want: int | None, now: float = 0.0) -> None:
        g = self.gpuclock
        if not (self.opts.gpu_clock and g and g.available) or want == g.ceiling_mhz:
            return
        if now < self._gpu_retry_at:
            return
        if not self.opts.apply:
            if want != getattr(self, "_gpu_would", "unset"):
                self._gpu_would = want
                self._log(f"dry-run would set gpu ceiling {want or 'none'}")
            return
        if want is None:
            self._gpu_reset(g, now)
        else:
            self._gpu_lock(g, want, now)

    def _gpu_reset(self, g: GpuClock, now: float) -> None:
        ok, out = g.reset()
        if ok:
            self.ledger.gpu_locked = False
            self.ledger.save()
        else:
            self._gpu_retry_at = now + self.GPU_RETRY_S
        self._log("gpu ceiling removed" if ok else f"gpu reset FAILED: {out}")

    def _gpu_lock(self, g: GpuClock, want: int, now: float) -> None:
        self.ledger.gpu_locked = True
        self.ledger.save()  # record BEFORE acting
        ok, out = g.lock(want)
        if not ok:
            self._gpu_retry_at = now + self.GPU_RETRY_S
        self._log(f"gpu ceiling {want} MHz" if ok else f"gpu lock {want} FAILED: {out}")

    def _release_gpu(self, why: str) -> None:
        g = self.gpuclock
        if g and (g.ceiling_mhz is not None or self.ledger.gpu_locked) and self.opts.apply:
            ok, out = g.reset()
            if ok:
                self.ledger.gpu_locked = False
                self.ledger.save()
            self._log(f"{why}: gpu ceiling removed" if ok else f"{why}: gpu reset FAILED: {out}")

    # -- one tick
    def tick(self) -> Decision:
        """Sense, decide and PUBLISH first; only then touch docker or the GPU
        helper. A slow or failing actuator can delay its own action, never the
        admission signal clients depend on."""
        now = self.clock()
        self.maybe_reload(now)
        reading = self.sensor.read()
        d = self.ctrl.step(reading.temp_c, now, reading.gpu_temp_c)
        if self.last is None or d.level != self.last.level:
            self._log(
                f"level {self.last.level if self.last else '-'} -> {d.level} "
                f"temp={d.temp_c} eff={d.effective_c} source={d.source} ({d.reason})"
            )
        self.last = d
        doc = self.state_doc(d, reading, now)
        if self.opts.state_path:
            write_state(self.opts.state_path, doc)
        if self.server:
            self.server.doc = doc
        self._act(d, now)
        if self.recorder:
            self.recorder.write(
                {
                    "t": round(now, 3),
                    "temp_c": d.temp_c,
                    "board_temp_c": reading.temp_c,
                    "effective_c": d.effective_c,
                    "rate_c_s": d.rate_c_s,
                    "level": d.level,
                    "duty": d.duty,
                    "cpus": d.cpus,
                    "gpu_mhz": d.gpu_mhz,
                    "source": d.source,
                    "zones": {z.key: z.temp_c for z in reading.zones},
                    "fans": reading.fans,
                    "gpu": reading.gpu,
                    "system": reading.system,
                }
            )
        return d

    def _act(self, d: Decision, now: float) -> None:
        """Every actuator fenced: one failure is logged and the rest still run."""
        if self.docker and now - self._discovered_at >= self.opts.discover_every_s:
            self._observed_at = time.time()
            self._discovered_at = now
            self._fenced("discover", self.discover)
        if self.cycler:
            self.cycler.set(d.duty, self.batch())
        if self.capper:
            for c in self.capped():
                self._fenced(
                    f"cap {c.name}",
                    lambda c=c: self.capper.set(c, d.cpus, observed_at=self._observed_at),
                )
        self._fenced("gpu ceiling", lambda: self._gpu(d.gpu_mhz, now))
        if self.unit_capper:
            for rule in self.opts.units:
                self._fenced(f"unit {rule.name}", lambda r=rule: self._unit_step(r, d, now))

    def _fenced(self, what: str, fn) -> None:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - one actuator must not stop the loop
            self._log(f"{what}: {exc!r}")

    def state_doc(self, d: Decision, reading: Reading, now: float) -> dict:
        lim = self.profile.limits
        hot = reading.hottest
        return {
            "schema": SCHEMA,
            "host": self.host,
            "platform": self.profile.name,
            "version": __version__,
            "at": round(now, 3),
            "at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "stale_after_s": max(5.0, 3 * self.opts.poll_s + 2),
            "level": d.level,
            "admit": d.admit,
            "concurrency_factor": d.concurrency_factor,
            "duty": d.duty,
            "cpus": d.cpus,
            "gpu_ceiling_mhz": (
                self.gpuclock.ceiling_mhz if (self.gpuclock and self.opts.gpu_clock) else None
            ),
            "gpu_clock": (
                self.gpuclock.reason if (self.gpuclock and self.opts.gpu_clock) else "off"
            ),
            "config": (
                str(self.opts.config.path) if self.opts.config and self.opts.config.path else None
            ),
            "config_error": self.config_error,
            "temp_c": d.temp_c,
            "effective_c": d.effective_c,
            "rate_c_s": d.rate_c_s,
            "hottest_zone": hot.key if hot else None,
            "source": d.source,
            "sensors": reading.sensors(),
            "gpu_temp_c": d.gpu_temp_c,
            "gpu_limit_c": reading.gpu.get("limit_c"),
            "reason": d.reason,
            "limits": {"warm_c": lim.warm_c, "hot_c": lim.hot_c, "crit_c": lim.crit_c},
            "applying": self.opts.apply,
            "containers": [
                {
                    "name": c.name,
                    "role": c.role,
                    "paused_by_governor": bool(self.cycler and c.id in self.cycler.held),
                    "cpus": self.capper.current.get(c.id) if self.capper else None,
                }
                for c in self.containers.values()
            ],
            "units": [self._unit_doc(u) for u in self.opts.units],
        }

    def _unit_doc(self, u: UnitRule) -> dict:
        doc = {
            "name": u.name,
            "mode": u.mode,
            "dry_run": u.dry_run,
            "cpus": self.unit_capper.current.get(u.name) if self.unit_capper else None,
            "use_cpus": self.unit_use.get(u.name),
            "missing": bool(self.unit_capper and u.name in self.unit_capper.missing_units()),
        }
        loop = self.adaptive.get(u.name)
        if loop is not None:
            doc["gain"] = loop.model.gain
            doc["samples"] = loop.model.samples
        return doc

    # -- lifecycle
    def start(self) -> None:
        if self.recorder:
            self.recorder.write(
                {
                    "t": self.clock(),
                    "event": "start",
                    "boot_id": boot_id(),
                    "platform": self.profile.name,
                    "version": __version__,
                    "apply": self.opts.apply,
                    "pid": os.getpid(),
                }
            )
        if self.opts.apply:
            units = self.systemctl
            if units is None and self.ledger.unit_quota:
                units = self.systemctl = Systemctl()
            self.ledger.recover(
                self.docker, self._log, gpu=self.gpuclock or GpuClock(), units=units
            )
        if self.opts.gpu_clock and self.gpuclock:
            self.gpuclock.probe()
            self._log(f"gpu clock control: {self.gpuclock.reason}")
        if self.opts.units:
            self._note_self_inside()
        if self.cycler:
            self.cycler.start()
        if self.server:
            self.server.start()
        self._log(
            f"start platform={self.profile.name} apply={self.opts.apply} poll={self.opts.poll_s}s "
            f"limits warm/hot/crit={self.profile.limits.warm_c}/{self.profile.limits.hot_c}/"
            f"{self.profile.limits.crit_c} ({self.profile.provenance})"
        )

    def shutdown(self) -> None:
        if self.cycler:
            self.cycler.stop()
        if self.capper:
            self.capper.release_all(self.containers)
        if self.unit_capper:
            self.unit_capper.release_all()
        self._release_gpu("stop")
        if self.server:
            self.server.stop()
        self._log("stop: own pauses released, CPU limits, unit quotas and GPU ceiling restored")
        if self.recorder:
            self.recorder.close()

    def run(self, *, iterations: int | None = None) -> None:  # pragma: no cover - loop shell
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: self._stop.set())
        self.start()
        n = 0
        try:
            while not self._stop.is_set():
                t0 = time.monotonic()
                try:
                    self.tick()
                except Exception as exc:  # noqa: BLE001 - keep governing
                    self._log(f"tick error: {exc}")
                n += 1
                if iterations is not None and n >= iterations:
                    break
                self._stop.wait(max(0.0, self.opts.poll_s - (time.monotonic() - t0)))
        finally:
            self.shutdown()

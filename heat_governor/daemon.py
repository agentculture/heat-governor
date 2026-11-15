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

With ``[pair]`` (one model across two boxes, :mod:`heat_governor.pair`), the
containers it names are this box's rank: governed as serving (never paused),
CPU-capped by the hotter of the two boxes' decisions. Everything else keeps
using this box's readings only.

With ``[memory]`` a :class:`~heat_governor.memguard.MemGuard` runs on its own thread
(started and stopped with the daemon, rebuilt or stopped by a live reload); its kill, OOM and
budget events become record rows, and with ``[ship]`` the kill / OOM / budget events, the black
box and (after an unclean boot) a postmortem go to S3 through a
:class:`~heat_governor.ship.Shipper`. The thermal path never reads memory: level, admit and
concurrency_factor are the same with or without the guard.

Without ``apply`` the loop still senses, publishes the admission signal and
records -- all harmless -- but changes no container (it logs what it would do).
"""

from __future__ import annotations

import json
import os
import signal
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from heat_governor import __version__, platforms
from heat_governor.actuators import CpuCapper, DutyCycler, Ledger
from heat_governor.adaptive import AdaptiveLoop, ModelStore, Trend, UsageMeter
from heat_governor.config import (
    PAIR_RESTART_ONLY,
    Config,
    ConfigError,
    ContainerRule,
    MemoryRule,
    PairRule,
    ShipRule,
    UnitRule,
)
from heat_governor.config import changed as config_changed
from heat_governor.config import diff as config_diff
from heat_governor.config import (
    host_port,
    listen_conflict,
)
from heat_governor.config import load as load_config
from heat_governor.docker import Container, Docker, DockerError
from heat_governor.gpuclock import GpuClock
from heat_governor.gpuclock import unsupported_reason as gpuclock_unsupported
from heat_governor.gpulaw import GpuGovernor, Plan
from heat_governor.memguard import EV_TOP, MemGuard
from heat_governor.memsink import MemorySink
from heat_governor.operator_ceiling import OperatorCeiling, hold_path
from heat_governor.pair import PairDecision, PeerLink, PeerStatus
from heat_governor.pair import decide as pair_decide
from heat_governor.pair import lost
from heat_governor.platforms import Profile
from heat_governor.policy import Controller, Decision, unit_cpus
from heat_governor.recorder import MEMORY_EVENTS, Recorder, _reboot_entry, boot_id
from heat_governor.sense import Reading, Sensor
from heat_governor.ship import Shipper
from heat_governor.state import SCHEMA, StateServer, write_state
from heat_governor.units import Systemctl, SystemctlLike, UnitCapper, containing


@dataclass
class Options:
    apply: bool = False
    poll_s: float = 1.0
    state_path: Path | None = None
    data_dir: Path | None = None
    listen: tuple[str, int] | None = None
    listeners: bool = True  # False (run --once): bind no state server at all
    batch: tuple[str, ...] = ()
    serving: tuple[str, ...] = ()
    serving_cpu_cap: bool = False  # cap named --serving containers too
    use_labels: bool = True
    discover_every_s: float = 5.0
    containers: tuple[ContainerRule, ...] = ()  # from the config file
    units: tuple[UnitRule, ...] = ()  # systemd user units to CPU-cap (config [[units]])
    pair: PairRule | None = None  # one half of a tensor-parallel pair (config [pair])
    memory: MemoryRule | None = None  # the memory guard (config [memory]); None: no guard
    ship: ShipRule | None = None  # shipping to S3 (config [ship], enabled); None: no shipper
    gpu_clock: bool = False  # lower the GPU clock ceiling per gpu_clock_bands
    gpu_ceiling_mhz: int | None = None  # the operator's ceiling from the config (law)
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


SHIP_EVENTS = MEMORY_EVENTS  # memory_kill, oom_kill, memory_budget_exhausted (not would_kill)
POSTMORTEM_FILES = 2  # the newest day files read for the postmortem (never the whole archive)


def memory_rule(cfg: Config) -> MemoryRule | None:
    """``[memory]`` when the table is present; absent section = no guard."""
    return cfg.memory if isinstance(cfg.data.get("memory"), dict) else None


def ship_rule(cfg: Config) -> ShipRule | None:
    """``[ship]`` when the table is present and enabled."""
    if not isinstance(cfg.data.get("ship"), dict):
        return None
    rule = cfg.ship
    return rule if rule.enabled else None


def _decision_fields(dec: PairDecision | None) -> dict:
    """The pair's shared decision, or all None before the first one."""
    keys = ("level", "admit", "concurrency_factor", "cpus", "basis")
    return {k: getattr(dec, k) if dec else None for k in keys}


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
        peer_link: PeerLink | None = None,
        proc_root: Path = Path("/proc"),
        sys_root: Path = Path("/sys"),
        make_guard: Callable[..., MemGuard] | None = None,
        make_shipper: Callable[..., Shipper] | None = None,
    ) -> None:
        self.profile = profile
        self.opts = opts
        self.sensor = sensor or Sensor(profile.zones, gpu=profile.gpu)
        self.docker = docker
        self.clock = clock
        self.ctrl = Controller(profile)
        data = opts.data_dir
        self.ledger = Ledger((data or Path(".")) / "ledger.json")
        self.capper, self.cycler = self._actuators(docker, profile, opts)
        self.recorder = (
            Recorder(data / "records", fsync=opts.fsync) if (data and opts.record) else None
        )
        self.server = StateServer(*opts.listen) if (opts.listen and opts.listeners) else None
        self._init_pair(peer_link, opts)
        self.pair_ids: set[str] = set()  # ids of this box's rank containers
        self._pair_forced: set[str] = set()  # pair containers found as batch, logged once
        self._pair: PairDecision | None = None
        self._peer: PeerStatus | None = None
        self._peer_ok: bool | None = None  # last logged peer health
        self._ceilings: tuple | None = None  # (ours, the peer's) operator ceiling, last logged
        self._started = False
        self._started_at = 0.0
        self.containers: dict[str, Container] = {}
        self._discovered_at = 0.0
        self._observed_at = 0.0
        self._stop = threading.Event()
        self.last: Decision | None = None
        self.host = socket.gethostname()
        self.operator = OperatorCeiling(hold_path(data) if data else None, opts.gpu_ceiling_mhz)
        self.gpu = GpuGovernor(
            gpuclock if gpuclock is not None else self._default_gpuclock(opts),
            self.ledger,
            self.operator,
            self._log,
            apply=opts.apply,
            enabled=opts.gpu_clock,
            unsupported=gpuclock_unsupported(profile),
        )
        self._gpu_plan = Plan(False, None, None, False)
        self._reload_at = 0.0
        self._operator_at = 0.0
        self.config_error: str | None = None
        self.systemctl = systemctl
        self.unit_capper: UnitCapper | None = None
        self._unbanded: set[str] = set()
        self.host_cpus = host_cpus or os.cpu_count() or 1
        self.adaptive: dict[str, AdaptiveLoop] = {}  # unit -> its loop (mode = adaptive)
        self.trends: dict[str, Trend] = {}  # unit -> temperature readings since its last step
        self._adaptive_store: ModelStore | None = None
        self.unit_use: dict[str, float | None] = {}  # unit -> CPUs used, last measured
        self._use_sampled: dict[str, float] = {}  # levels-mode unit -> when use was last read
        self.meter = UsageMeter(self._unit_usage)
        if opts.units:
            self._ensure_unit_capper().set_dry({u.name for u in opts.units if u.dry_run})
            self._sync_adaptive(opts.units)
        self._init_memory(opts, proc_root, sys_root, make_guard, make_shipper)

    def _init_pair(self, peer_link: PeerLink | None, opts: Options) -> None:
        """The tensor-parallel pair ([pair]): its peer link and server; None without one."""
        self.pair_link: PeerLink | None = peer_link
        if self.pair_link is None and opts.pair is not None:
            self.pair_link = PeerLink(opts.pair)
        self.pair_server = self._pair_server(opts.pair) if opts.listeners else None

    def _init_memory(
        self,
        opts: Options,
        proc_root: Path,
        sys_root: Path,
        make_guard: Callable[..., MemGuard] | None,
        make_shipper: Callable[..., Shipper] | None,
    ) -> None:
        """The memory guard ([memory]) and the shipper ([ship]); both start with start()."""
        self.proc_root = Path(proc_root)  # /proc for the guard, the boot id and boot time
        self.sys_root = Path(sys_root)  # /sys: the guard reads cgroup memory.events here
        self._make_guard = make_guard or MemGuard
        self._make_shipper = make_shipper or Shipper.from_rule
        self.guard: MemGuard | None = None
        self.shipper: Shipper | None = None
        self._mem_lock = threading.Lock()  # the guard thread hands its top snapshot over
        self._top: list | None = None
        self._mem_total: float | None = None
        self._postmortem_done = False
        # recorder/spool writes of guard events happen here, never on the guard thread
        self.memsink = MemorySink(self._route_memory_event, log=self._log)
        if opts.config is not None:
            if opts.memory is None:
                opts.memory = memory_rule(opts.config)
            if opts.ship is None:
                opts.ship = ship_rule(opts.config)

    def _actuators(
        self, docker: Docker | None, profile: Profile, opts: Options
    ) -> tuple[CpuCapper | None, DutyCycler | None]:
        """The container CPU capper and duty cycler; neither without a docker client."""
        if not docker:
            return None, None
        capper = CpuCapper(docker, self.ledger, self._log, apply=opts.apply)
        cycler = DutyCycler(
            docker, self.ledger, self._log, apply=opts.apply, period_s=profile.duty_period_s
        )
        return capper, cycler

    def _default_gpuclock(self, opts: Options) -> GpuClock | None:
        """A GPU clock driver when clock control or an operator ceiling needs one."""
        if opts.gpu_clock or self.operator.mhz is not None:
            # VENTUNO Q / devfreq boards: never probe or spawn the root helper (spec c5)
            return GpuClock(unsupported=gpuclock_unsupported(self.profile))
        return None

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
        for cid in set(found) & set(self.containers):
            if _is_capped(self.containers[cid]) and not _is_capped(found[cid]):
                self._uncapped(found[cid])
        for cid in set(found) - set(self.containers):
            c = found[cid]
            capped = "yes" if c.cpu_cap or c.role == "batch" else "no"
            pair = " pair=yes" if cid in self.pair_ids else ""
            self._log(f"governing {c.name} role={c.role} cpu-cap={capped}{pair}")
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
        if self.opts.pair is not None:
            self._find_pair(found)
        return found

    def _find_pair(self, found: dict[str, Container]) -> None:
        """This box's rank: every container a ``[pair]`` pattern names, governed as
        serving (pausing a rank hangs NCCL on the other box).

        Membership is decided by NAME. A container already in ``found`` (by label or
        a named rule) is converted in place, with no second inspect that could fail
        and leave a labelled-batch rank to the duty cycler. Only matching names not
        yet in ``found`` (unlabelled ranks) are listed and inspected."""
        pair = self.opts.pair
        ids: set[str] = set()
        for cid, c in list(found.items()):
            if pair.matches(c.name):
                found[cid] = self._as_pair(c, had=c)
                ids.add(cid)
        known = {c.name for c in found.values()}
        try:
            names = [n for n in self.docker.names() if pair.matches(n) and n not in known]
        except DockerError as exc:
            self._log(f"discover pair: {exc}")
            names = []
        for name in names:
            try:
                c = self.docker.inspect(name)
            except DockerError:
                continue  # not governed at all this round: nothing will pause it
            found[c.id] = self._as_pair(c, had=found.get(c.id))
            ids.add(c.id)
        self.pair_ids = ids

    def _as_pair(self, c: Container, *, had: Container | None) -> Container:
        """``c`` as a pair container: serving, capped by the pair band when allowed."""
        if (c.role == "batch" or (had is not None and had.role == "batch")) and (
            c.id not in self._pair_forced
        ):
            self._pair_forced.add(c.id)
            self._log(
                f"pair: {c.name} is a pair container: governed as serving, never "
                "paused (it was named or labelled batch)"
            )
        own_cap = c.cpu_cap if c.role != "batch" else False
        cap = self.opts.pair.cpu_cap or own_cap
        return Container(c.id, c.name, c.state, "serving", cap, c.nano_cpus)

    def _left(self, old: Container) -> None:
        """``old`` left the governed set: give back any CPU cap we put on it."""
        self._log(f"container {old.name} left the governed set")
        self._release_cap(old)

    def _uncapped(self, c: Container) -> None:
        """``c`` stays governed but may no longer be capped (a reload turned its
        cpu_cap off, or dropped ``[pair]``): give back the cap we put on it."""
        self._log(f"{c.name}: no longer CPU-capped -- restoring its own limit")
        self._release_cap(c)

    def _release_cap(self, c: Container) -> None:
        cid = c.id
        if self.capper and (cid in self.ledger.nano_cpus or self.capper.current.get(cid)):
            self._fenced(f"release {c.name}", lambda: self.capper.set(c, None))
            self.capper.current.pop(cid, None)

    def batch(self) -> dict[str, Container]:
        """Duty-cycled containers. A pair container is never one, whatever it says."""
        return {
            k: c for k, c in self.containers.items() if c.role == "batch" and not self.is_pair(c)
        }

    def capped(self) -> list[Container]:
        return [c for c in self.containers.values() if _is_capped(c)]

    def is_pair(self, c: Container) -> bool:
        """A pair container by id OR by name: never paused, even when a discovery
        could not inspect it again."""
        return c.id in self.pair_ids or (
            self.opts.pair is not None and self.opts.pair.matches(c.name)
        )

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

    USE_SAMPLE_S = 5.0  # a levels-mode unit's use is read at most this often

    def _unit_step(self, rule: UnitRule, d: Decision, now: float) -> None:
        """Measure the unit, size its cap (by level or by its loop), hand it to the
        capper (which applies the ledger, dry-run and missing-unit rules)."""
        name = rule.name
        loop = self.adaptive.get(name)
        if loop is None:
            if now - self._use_sampled.get(name, float("-inf")) >= self.USE_SAMPLE_S:
                self._use_sampled[name] = now  # use only feeds the state: no need per tick
                self.unit_use[name] = self.meter.sample(name, now)
            self.unit_capper.set(name, unit_cpus(d.level, self.unit_bands(rule)))
            return
        trend = self.trends[name]
        trend.add(now, None if d.reason == "sensor_lost" else d.temp_c, d.source)
        if loop.due(now):
            self._evaluate(name, loop, trend, d.level, now)
        self.unit_capper.set(name, loop.cap)

    def _evaluate(self, name: str, loop: AdaptiveLoop, trend: Trend, level: str, now: float):
        """One adaptive evaluation: measure use, fit the trend, step the loop, log."""
        use = self.unit_use[name] = self.meter.sample(name, now)
        live = self.unit_capper.applies(name)
        if not live:
            loop.pending = None  # a change not made has no response to learn from
        rate = trend.take()
        step = loop.step(
            level,
            rate,
            use,
            self.host_cpus,
            now,
            learn=live,
            noise=trend.noise,
            long=trend.long(),
            switched=trend.switched,
        )
        if step.lesson:
            self._log(f"adaptive {name}: {step.lesson}")
        if step.changed:
            trend.restart_long()  # the readings before a change describe another cap
            self._log(step.describe(name, dry=not live))
        if step.learned:
            self.adaptive_store.save()

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
        old_pair, new_pair = cfg.data.get("pair") or {}, new.data.get("pair") or {}
        later += [f"pair.{k}" for k in PAIR_RESTART_ONLY if old_pair.get(k) != new_pair.get(k)]
        self.apply_config(new, profile)
        self.config_error = None
        self._log(
            f"config reloaded from {new.path}: " + ("; ".join(changes) or "no effective change")
        )
        if later:
            self._log(
                "these keys take effect only after a restart "
                f"(systemctl --user restart heat-governor): {', '.join(later)}"
            )

    def apply_config(self, cfg: Config, profile: Profile) -> None:
        self.opts.config = cfg
        self.profile = profile
        self.ctrl.set_profile(profile)
        self.sensor.watched = profile.zones
        self.sensor.gpu = profile.gpu
        self._set_gpu_unsupported(gpuclock_unsupported(profile))
        if self.cycler:
            self.cycler.period_s = profile.duty_period_s
        d = cfg.daemon
        for key in ("poll_s", "use_labels", "discover_every_s"):
            if key in d and key not in self.opts.cli_set:
                setattr(self.opts, key, d[key])
        if "gpu_clock" in d and "gpu_clock" not in self.opts.cli_set:
            self.set_gpu_clock(bool(d["gpu_clock"]))
        self.set_gpu_ceiling(d.get("gpu_ceiling_mhz"))
        self.opts.containers = tuple(cfg.containers)
        self._set_units(tuple(cfg.units))
        self.set_pair(cfg.pair)
        self.set_memory(memory_rule(cfg))
        self.set_ship(ship_rule(cfg))
        self._discovered_at = 0.0  # re-discover on the next tick

    RESTART_ONLY = ("apply", "listen", "state", "data_dir", "record", "fsync", "reload")

    def _restart_only_changes(self, old: dict, new: dict) -> list[str]:
        return [k for k in self.RESTART_ONLY if old.get(k) != new.get(k)]

    # -- the tensor-parallel pair
    def _pair_server(self, pair: PairRule | None) -> StateServer | None:
        """The extra state server the peer reads (``[pair] listen``), unless it is
        the same address as ``--listen``."""
        addr = host_port(pair.listen) if pair and pair.listen else None
        if addr is None:
            return None
        how = listen_conflict(addr, self.opts.listen) if self.opts.listen else None
        if how == "same":
            return None
        if how == "clash":
            self._log(
                f"pair: listen {pair.listen} shares port {addr[1]} with --listen "
                f"{self.opts.listen[0]}:{self.opts.listen[1]} and one is a wildcard: not "
                "binding it (the wildcard already serves that port; drop [pair] listen "
                "or use another port)"
            )
            return None
        return StateServer(*addr)

    def set_pair(self, pair: PairRule | None) -> None:
        """A reload: adopt, change or drop the pair (its ``listen`` waits for a restart)."""
        if pair == self.opts.pair:
            return
        self.opts.pair = pair
        if pair is None:
            if self.pair_link:
                self.pair_link.stop()
            self.pair_link, self.pair_ids, self._pair, self._peer = None, set(), None, None
            self._peer_ok = self._ceilings = None
            self._log("pair: off -- pair containers follow this box's readings again")
            return
        if self.pair_link is None:
            self.pair_link = PeerLink(pair)
            if self._started:
                self.pair_link.poll_once()
                self.pair_link.start()
        else:
            self.pair_link.configure(pair)
        self._log(f"pair: peer {pair.peer}, containers {', '.join(pair.containers)}")

    def _pair_step(self, d: Decision, now: float) -> None:
        """Read the peer's last snapshot, log health / ceiling transitions, and
        decide the pair's level and CPU cap. Never leaves the decision frozen: any
        failure here counts as a lost peer (one band hotter than local)."""
        link = self.pair_link
        if link is None:
            return
        peer = self._peer = self._peer_status(link, now)
        try:
            if peer.ok != self._peer_ok:
                self._log_peer_health(peer)
                self._peer_ok = peer.ok
            if peer.ok and peer.reading is not None:
                self._check_ceilings(peer.reading)
        except Exception as exc:  # noqa: BLE001 - logging must not freeze the decision
            self._log(f"pair: {exc!r}")
        prev = self._pair
        self._pair = self._decide_pair(d, peer)
        if prev is None or prev.level != self._pair.level:
            peer_level = peer.reading.level if peer.ok and peer.reading else "?"
            self._log(
                f"pair level {prev.level if prev else '-'} -> {self._pair.level} "
                f"(here {d.level}, peer {peer_level}, {self._pair.basis}) "
                f"cpus={self._pair.cpus}"
            )

    def _peer_status(self, link: PeerLink, now: float) -> PeerStatus:
        try:
            if not link.started:
                link.poll_once()  # --once, tests, or before start(): read it in-line
            elif not link.running:
                link.start()  # the poller died: restart it, never poll in the tick
            return link.status(now)
        except Exception as exc:  # noqa: BLE001 - a broken read is a lost peer
            return lost(f"pair read failed: {exc!r}"[:200])

    def _decide_pair(self, d: Decision, peer: PeerStatus) -> PairDecision:
        kw = {
            "cpu_bands": sorted({c for _, c in self.profile.cpu_bands}),
            "concurrency": self.profile.concurrency,
        }
        try:
            return pair_decide(d.level, d.cpus, peer, **kw)
        except Exception as exc:  # noqa: BLE001 - fall through to the conservative answer
            self._log(f"pair: decision failed ({exc!r}) -- treating the peer as lost")
            return pair_decide(d.level, d.cpus, lost(repr(exc)), **kw)

    def _log_peer_health(self, peer: PeerStatus) -> None:
        rule = self.opts.pair
        if peer.ok:
            host = peer.reading.host if peer.reading else "?"
            self._log(f"pair: peer {rule.peer} ({host}) reached -- sharing the hotter band")
            return
        why = peer.error or f"no new state for {peer.age_s} s"
        self._log(
            f"pair: peer {rule.peer} {peer.status} ({why}) -- pair containers held one "
            "band hotter than local until it is back"
        )

    def _check_ceilings(self, r) -> None:
        """Warn (once per change) when the two boxes' operator GPU ceilings differ:
        the slower rank sets the pair's pace. Never changes a clock."""
        if not r.has_gpu_operator:
            return
        pair = (self.operator.mhz, r.gpu_operator_mhz)
        if pair == self._ceilings:
            return
        was = self._ceilings
        self._ceilings = pair
        if pair[0] != pair[1]:
            self._log(
                f"pair: WARNING operator GPU ceiling differs: here {_mhz(pair[0])}, peer "
                f"{r.host} {_mhz(pair[1])} -- the slower rank sets the pair's pace; "
                "not changing clocks (set the same [daemon] gpu_ceiling_mhz on both)"
            )
        elif was is not None and was[0] != was[1]:
            self._log(f"pair: operator GPU ceilings agree again ({_mhz(pair[0])})")

    def pair_doc(self) -> dict | None:
        rule = self.opts.pair
        if rule is None:
            return None
        return {
            "peer": rule.peer,
            **self._peer_fields(),
            **_decision_fields(self._pair),
            "containers": sorted(
                c.name for cid, c in self.containers.items() if cid in self.pair_ids
            ),
        }

    def _peer_fields(self) -> dict:
        """What the pair document says about the peer (all None when it is unreachable)."""
        peer = self._peer
        r = peer.reading if peer else None
        doc: dict = {
            "peer_status": peer.status if peer else "unreachable",
            "peer_error": peer.error if peer and not peer.ok else None,
            "peer_age_s": peer.age_s if peer else None,
            "stale_after_s": self.pair_link.stale_after(r) if self.pair_link else None,
        }
        keys = ("host", "level", "cpus", "effective_c", "gpu_operator_mhz")
        doc.update({f"peer_{k}": getattr(r, k) if r else None for k in keys})
        match = None
        if r and r.has_gpu_operator and peer and peer.ok:
            match = self.operator.mhz == r.gpu_operator_mhz
        doc["gpu_operator_match"] = match
        return doc

    def cpus_for(self, c: Container, d: Decision) -> int | None:
        """A pair container follows the pair's band; everything else, this box's."""
        if c.id in self.pair_ids and self._pair is not None:
            return self._pair.cpus
        return d.cpus

    def _set_gpu_unsupported(self, reason: str | None) -> None:
        """A reload that resolves to a platform with no GPU clock control: the governor,
        and any clock it already built, never touch the root helper from now on."""
        self.gpu.unsupported = reason
        if reason and self.gpu.clock is not None:
            self.gpu.clock.unsupported = reason
            self.gpu.clock.available, self.gpu.clock.reason = False, reason

    @property
    def gpuclock(self) -> GpuClock | None:
        return self.gpu.clock

    def set_gpu_clock(self, on: bool) -> None:
        if on == self.opts.gpu_clock:
            return
        self.opts.gpu_clock = self.gpu.enabled = on
        if on:
            self._log(f"gpu clock control on: {self.gpu.probe()}")
        else:
            self.gpu.release("gpu clock control off")

    def set_gpu_ceiling(self, mhz: int | None) -> None:
        """The config's operator ceiling (``[daemon] gpu_ceiling_mhz``)."""
        if mhz == self.operator.config_mhz:
            return
        self.opts.gpu_ceiling_mhz = self.operator.config_mhz = mhz
        self._operator_changed()

    # -- the operator's GPU ceiling (law)
    def _poll_operator(self, now: float) -> None:
        if now - self._operator_at < self.opts.reload_every_s:
            return
        self._operator_at = now
        if self.operator.poll():
            self._operator_changed()

    def _operator_changed(self) -> None:
        if self.operator.mhz is None:
            self._log("gpu: operator ceiling cleared -- the daemon's own ceiling rules again")
        else:
            self._log(f"gpu: operator ceiling {self.operator.describe()} is law")
        self.gpu.operator_changed()

    # -- the memory guard and the shipper (their own threads; the tick never waits on them)
    def set_memory(self, rule: MemoryRule | None) -> None:
        """A reload: start, rebuild (any change) or stop the guard."""
        if rule == self.opts.memory:
            return
        self.opts.memory = rule
        if self._started:
            self._stop_guard()
            self._start_guard()
            if rule is None:
                self._log("memory guard: off")

    def set_ship(self, rule: ShipRule | None) -> None:
        """A reload: start, rebuild (any change) or stop the shipper."""
        if rule == self.opts.ship:
            return
        self.opts.ship = rule
        if self._started:
            self._stop_shipper()
            self._start_shipper()
            if rule is None:
                self._log("ship: off")

    def _start_guard(self) -> None:
        rule = self.opts.memory
        if rule is None or self.guard is not None:
            return
        try:
            guard = self._make_guard(
                rule,
                proc_root=self.proc_root,
                sys_root=self.sys_root,
                emit=self._on_memory_event,
                log=self._log,
                cgroups=self._own_cgroup(),
                platform_limits=self.profile.memory_limits_mib,
            )
            self.memsink.start()
            guard.start()
        except Exception as exc:  # noqa: BLE001 - running without a guard is today's behaviour
            self._log(f"memory guard: cannot start ({exc!r}); running without it")
            return
        self.guard = guard
        if rule.floor_mib is not None:
            limits = f"floor={rule.floor_mib} MiB soft={rule.soft_mib} MiB (config)"
        elif self.profile.memory_limits_mib is not None:
            floor, soft = self.profile.memory_limits_mib
            limits = f"floor={floor} MiB soft={soft} MiB (platform {self.profile.name})"
        else:
            limits = f"floor={rule.floor_pct}% soft={rule.soft_pct}% (percent)"
        self._log(f"memory guard: on action={rule.action} {limits} poll={rule.poll_s}s")

    def _stop_guard(self) -> None:
        guard, self.guard = self.guard, None
        if guard is not None:
            self._fenced("memory guard stop", guard.stop)
        self._fenced("memory sink stop", self.memsink.stop)  # drains queued events first
        with self._mem_lock:
            self._top, self._mem_total = None, None

    def _own_cgroup(self) -> tuple[str, ...]:
        """The daemon's own (delegated) cgroup, relative to the cgroup root."""
        try:
            text = (self.proc_root / "self" / "cgroup").read_text()
        except OSError:
            return ()
        unified = [
            line[3:].strip().strip("/")
            for line in text.splitlines()
            if line.startswith("0::") and line[3:].strip("/")
        ]
        return tuple(unified[:1])  # the first unified (v2) entry, or none

    def _start_shipper(self) -> None:
        rule = self.opts.ship
        if rule is None or self.shipper is not None:
            return
        machine = rule.machine or self._fleet_machine()
        if not machine:
            self._log("ship: set [ship] machine (no local fleet entry); shipping disabled")
            return
        try:
            shipper = self._make_shipper(
                rule,
                default_machine=machine,
                boot_id=boot_id(self.proc_root),
                spool_dir=(self.opts.data_dir or Path(".")) / "spool",
                records_dir=self.recorder.directory if self.recorder else None,
                log=self._log,
                low_memory=self._low_memory,
            )
            shipper.start()
        except Exception as exc:  # noqa: BLE001 - ValueError: endpoint/bucket/credentials
            self._log(f"ship: {exc}; shipping disabled")
            return
        self.shipper = shipper
        self._log(f"ship: on, to {rule.endpoint} bucket {rule.bucket} as {machine}")
        self._fenced("ship postmortem", self._submit_postmortem)

    def _stop_shipper(self) -> None:
        shipper, self.shipper = self.shipper, None
        if shipper is not None:
            self._fenced("ship stop", shipper.stop)

    def _fleet_machine(self) -> str | None:
        """The name of fleet.toml's ``transport = "local"`` machine (never the hostname)."""
        from heat_governor.fleet import default_fleet_path, load_fleet

        cfg = self.opts.config
        path = cfg.path.parent / "fleet.toml" if cfg and cfg.path else default_fleet_path()
        if not path.exists():
            return None
        try:
            fleet = load_fleet(path)
        except Exception as exc:  # noqa: BLE001 - CliError from a bad fleet.toml
            self._log(f"ship: cannot read {path} ({exc})")
            return None
        return next((m.name for m in fleet.machines if m.transport == "local"), None)

    def _submit_postmortem(self) -> None:
        """Once per daemon: if the previous boot ended without a clean stop, ship its
        reboot entry. Reads only the newest day files, up to this run's start row."""
        if self._postmortem_done or self.shipper is None or self.recorder is None:
            return
        self._postmortem_done = True
        entry = previous_boot_postmortem(
            self.recorder.directory,
            boot_id(self.proc_root),
            self._started_at,
            boot_time(self.proc_root),
            pid=os.getpid(),
        )
        if entry is not None:
            self.shipper.submit_postmortem(entry)
            self._log(
                f"ship: postmortem queued for unclean boot {entry['previous_boot']} "
                f"(last record {entry['last_record_utc']})"
            )

    def _low_memory(self) -> bool:
        """For the shipper's black-box cadence: the guard's avail under its soft limit."""
        guard = self.guard
        if guard is None:
            return False
        doc = guard.doc()
        avail = doc.get("avail_mib")
        soft = (doc.get("limits") or {}).get("soft_mib")
        if soft is None:  # an older/fake guard without limits: fall back to the percentage
            with self._mem_lock:
                total = self._mem_total
            soft = total * float(guard.rule.soft_pct) / 100.0 if total else None
        return avail is not None and soft is not None and avail < soft

    def _on_memory_event(self, ev: dict) -> None:
        """The guard's sink, called ON THE GUARD THREAD. Never blocks on disk: the top snapshot
        is handed over under a lock, everything else is queued on :attr:`memsink`, whose own
        thread does the recorder write and the ship submit (review #5)."""
        kind = ev.get("event") if isinstance(ev, dict) else None
        if kind == EV_TOP:
            with self._mem_lock:
                self._top, self._mem_total = ev.get("top"), ev.get("total_mib")
            return
        if kind is None:
            return
        self.memsink.submit(ev)

    def _route_memory_event(self, ev: dict) -> None:
        """Record and ship one guard event (on the sink thread). Never raises: the recorder
        write and the ship submit are each fenced by ``_quiet``."""
        kind = ev.get("event")
        if self.recorder:
            self._quiet(f"record {kind}", lambda: self.recorder.write(ev))
        shipper = self.shipper
        if shipper is not None and kind in SHIP_EVENTS:
            self._quiet(f"ship {kind}", lambda: shipper.submit(kind, ev, t=ev.get("t")))

    def _quiet(self, what: str, fn) -> None:
        """``_fenced`` for the guard thread: even a failing log is swallowed."""
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - routing must not reach the guard
            try:
                self._log(f"memory guard: {what}: {exc!r}")
            except Exception:  # noqa: BLE001 - nowhere left to report it # nosec B110
                pass

    def _top_record(self) -> dict:
        """The latest top snapshot, attached to one tick row and then cleared."""
        with self._mem_lock:
            top, self._top = self._top, None
        return {"top": top} if top else {}

    # -- one tick
    def tick(self) -> Decision:
        """Sense, decide and PUBLISH first; only then touch docker or the GPU
        helper. A slow or failing actuator can delay its own action, never the
        admission signal clients depend on."""
        now = self.clock()
        self.maybe_reload(now)
        self._poll_operator(now)
        reading = self.sensor.read()
        d = self.ctrl.step(reading.temp_c, now, reading.gpu_temp_c)
        if self.last is None or d.level != self.last.level:
            self._log(
                f"level {self.last.level if self.last else '-'} -> {d.level} "
                f"temp={d.temp_c} eff={d.effective_c} source={d.source} ({d.reason})"
            )
        self.last = d
        self._fenced("pair", lambda: self._pair_step(d, now))
        self._gpu_plan = self.gpu.plan(d.gpu_mhz)
        doc = self.state_doc(d, reading, now)
        if self.opts.state_path:
            write_state(self.opts.state_path, doc)
        if self.server:
            self.server.doc = doc
        if self.pair_server:
            self.pair_server.doc = doc
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
                    **self._gpu_record(),
                    "source": d.source,
                    **self._pair_record(),
                    "zones": {z.key: z.temp_c for z in reading.zones},
                    "fans": reading.fans,
                    "gpu": reading.gpu,
                    "system": reading.system,
                    **self._top_record(),
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
                    lambda c=c: self.capper.set(
                        c, self.cpus_for(c, d), observed_at=self._observed_at
                    ),
                )
        self._fenced("gpu ceiling", lambda: self.gpu.step(d.gpu_mhz, d.level, now))
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
        doc = {
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
            "gpu_ceiling_mhz": self.gpuclock.ceiling_mhz if self.gpuclock else None,
            "gpu_clock": (
                self.gpuclock.reason if (self.gpuclock and self.opts.gpu_clock) else "off"
            ),
            **self.gpu.doc(self._gpu_plan),
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
                    "pair": c.id in self.pair_ids,
                }
                for c in self.containers.values()
            ],
            "units": [self._unit_doc(u) for u in self.opts.units],
            "pair": self.pair_doc(),
        }
        guard = self.guard
        if guard is not None:
            doc["mem_guard"] = guard.doc()  # optional; clients ignore unknown fields
        return doc

    def _pair_record(self) -> dict:
        if self._pair is None:
            return {}
        return {
            "pair_level": self._pair.level,
            "pair_cpus": self._pair.cpus,
            "peer_status": self._peer.status if self._peer else None,
        }

    def _gpu_record(self) -> dict:
        p = self._gpu_plan
        return {
            "gpu_operator_mhz": self.operator.mhz,
            "gpu_wanted_mhz": p.wanted,
            "gpu_held_by_operator": p.held,
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
        self._started_at = self.clock()
        if self.recorder:
            self.recorder.write(
                {
                    "t": self._started_at,
                    "event": "start",
                    "boot_id": boot_id(self.proc_root),
                    "platform": self.profile.name,
                    "version": __version__,
                    "apply": self.opts.apply,
                    "pid": os.getpid(),
                }
            )
        if self.opts.apply:
            self._recover()
        if self.gpu.needed():
            self._log(f"gpu clock control: {self.gpu.probe()}")
        if self.operator.mhz is not None:
            self._log(f"gpu: operator ceiling {self.operator.describe()} is law")
        if self.opts.units:
            self._note_self_inside()
        if self.cycler:
            self.cycler.start()
        if self.server:
            self.server.start()
        if self.pair_server:
            self.pair_server.start()
        if self.pair_link:
            self.pair_link.poll_once()  # the first tick then knows the peer already
            self.pair_link.start()
            self._log(
                f"pair: peer {self.opts.pair.peer}, containers "
                f"{', '.join(self.opts.pair.containers)}"
            )
        self._started = True
        self._start_guard()
        self._start_shipper()
        self._log(
            f"start platform={self.profile.name} apply={self.opts.apply} poll={self.opts.poll_s}s "
            f"limits warm/hot/crit={self.profile.limits.warm_c}/{self.profile.limits.hot_c}/"
            f"{self.profile.limits.crit_c} ({self.profile.provenance})"
        )

    def _recover(self) -> None:
        """Undo what a crashed run left on the ledger (containers, units, GPU)."""
        units = self.systemctl
        if units is None and (self.ledger.unit_quota or self.ledger.unit_orphans):
            units = self.systemctl = Systemctl()
        restored = self.ledger.recover(
            self.docker,
            self._log,
            gpu=self.gpuclock or GpuClock(unsupported=gpuclock_unsupported(self.profile)),
            units=units,
            gpu_operator_mhz=self.operator.mhz,
        )
        if self.capper:
            self.capper.restored.update(restored)  # so 0 ("no limit") survives as 0

    def shutdown(self) -> None:
        self._stop_guard()
        if self.cycler:
            self.cycler.stop()
        if self.capper:
            self.capper.release_all(self.containers)
        if self.unit_capper:
            self.unit_capper.release_all()
        self.gpu.release("stop")
        if self.server:
            self.server.stop()
        if self.pair_link:
            self.pair_link.stop()
        if self.pair_server:
            self.pair_server.stop()
        self._log("stop: own pauses released, CPU limits, unit quotas and GPU ceiling restored")
        self._stop_shipper()
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


def boot_time(proc_root: Path = Path("/proc")) -> float | None:
    """The kernel's boot time (``btime`` of /proc/stat, epoch seconds), or None."""
    try:
        text = (proc_root / "stat").read_text()
    except OSError:
        return None
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == "btime" and parts[1].isdigit():
            return float(parts[1])
    return None


def previous_boot_postmortem(
    directory: Path,
    this_boot: str,
    now: float,
    btime: float | None,
    *,
    pid: int | None = None,
    files: int = POSTMORTEM_FILES,
    window_s: float = 60.0,
) -> dict | None:
    """The reboot entry (:func:`heat_governor.recorder.report`'s shape) for the run before
    this one, when it ended without a clean stop on an earlier boot; else None.

    Reads only the newest ``files`` day files and stops at this run's own start row (same
    pid and boot id). Lines without an ``"event"`` key are ticks: kept raw (the last 4000)
    and parsed only at the end, so a 74 MB day file costs a substring scan, not a JSON
    parse per line. When the previous run's start row is older than those files, its boot
    is "unknown" and the kernel boot time decides whether a reboot came in between."""
    scan = _PostmortemScan(this_boot, pid)
    for path in sorted(Path(directory).glob("*.jsonl"))[-files:]:
        if scan.ours:
            break
        scan.scan_file(path)
    if scan.clean:
        return None
    tail = _tick_tail(scan.ticks)
    if not tail:
        return None
    boot = scan.boot or "unknown"
    if not _rebooted_since(boot, this_boot, btime, tail):
        return None
    return _reboot_entry(boot, this_boot, {"t": now}, tail, window_s, scan.events)


class _PostmortemScan:
    """The running state of :func:`previous_boot_postmortem`'s pass over the day files."""

    def __init__(self, this_boot: str, pid: int | None) -> None:
        self.this_boot = this_boot
        self.pid = pid
        self.boot: str | None = None
        self.ticks: deque = deque(maxlen=4000)
        self.events: list[dict] = []
        self.clean = False
        self.ours = False

    def scan_file(self, path: Path) -> None:
        """Feed one day file; stops (and sets ``ours``) at this run's own start row."""
        try:
            fh = open(path, encoding="utf-8")
        except OSError:
            return
        with fh:
            for line in fh:
                if self._feed(line):
                    self.ours = True  # this run's own start: everything after is ours
                    return

    def _feed(self, line: str) -> bool:
        """Take one line; True at this run's own start row."""
        if '"event"' not in line:
            self.ticks.append(line)
            self.clean = False
            return False
        try:
            rec = json.loads(line)
        except ValueError:
            return False
        kind = rec.get("event") if isinstance(rec, dict) else None
        if kind == "start":
            return self._start(rec)
        if kind == "action" and str(rec.get("msg", "")).startswith("stop:"):
            self.clean = True
        elif kind in MEMORY_EVENTS and isinstance(rec.get("t"), (int, float)):
            self.events = (self.events + [rec])[-500:]
        return False

    def _start(self, rec: dict) -> bool:
        """A start row: True when it is this run's own; else a new previous run begins."""
        if self.pid is not None and (rec.get("pid"), rec.get("boot_id")) == (
            self.pid,
            self.this_boot,
        ):
            return True
        self.boot, self.events, self.clean = str(rec.get("boot_id") or "unknown"), [], False
        self.ticks.clear()
        return False


def _tick_tail(ticks: deque) -> list[dict]:
    """The raw tick lines parsed: the dicts with a temperature and a numeric time."""
    tail = []
    for line in ticks:
        try:
            rec = json.loads(line)
        except ValueError:
            continue  # a torn last line after a power loss
        if isinstance(rec, dict) and "temp_c" in rec and isinstance(rec.get("t"), (int, float)):
            tail.append(rec)
    return tail


def _rebooted_since(boot: str, this_boot: str, btime: float | None, tail: list[dict]) -> bool:
    """Whether a reboot came between the previous run's last tick and this boot."""
    if "unknown" not in (boot, this_boot):
        return boot != this_boot
    return btime is not None and tail[-1]["t"] < btime


def _mhz(v: int | None) -> str:
    return "none" if v is None else f"{v} MHz"


def _is_capped(c: Container) -> bool:
    return c.role == "batch" or c.cpu_cap

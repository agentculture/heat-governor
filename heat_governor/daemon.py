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
from heat_governor.config import (
    PAIR_RESTART_ONLY,
    Config,
    ConfigError,
    ContainerRule,
    PairRule,
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
from heat_governor.gpulaw import GpuGovernor, Plan
from heat_governor.operator_ceiling import OperatorCeiling, hold_path
from heat_governor.pair import PairDecision, PeerLink, PeerStatus
from heat_governor.pair import decide as pair_decide
from heat_governor.pair import lost
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
    listeners: bool = True  # False (run --once): bind no state server at all
    batch: tuple[str, ...] = ()
    serving: tuple[str, ...] = ()
    serving_cpu_cap: bool = False  # cap named --serving containers too
    use_labels: bool = True
    discover_every_s: float = 5.0
    containers: tuple[ContainerRule, ...] = ()  # from the config file
    units: tuple[UnitRule, ...] = ()  # systemd user units to CPU-cap (config [[units]])
    pair: PairRule | None = None  # one half of a tensor-parallel pair (config [pair])
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
        # -- the tensor-parallel pair ([pair]); None without one
        self.pair_link: PeerLink | None = peer_link
        if self.pair_link is None and opts.pair is not None:
            self.pair_link = PeerLink(opts.pair)
        self.pair_server = self._pair_server(opts.pair) if opts.listeners else None
        self.pair_ids: set[str] = set()  # ids of this box's rank containers
        self._pair_forced: set[str] = set()  # pair containers found as batch, logged once
        self._pair: PairDecision | None = None
        self._peer: PeerStatus | None = None
        self._peer_ok: bool | None = None  # last logged peer health
        self._ceilings: tuple | None = None  # (ours, the peer's) operator ceiling, last logged
        self._started = False
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
            return GpuClock()
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
            gpu=self.gpuclock or GpuClock(),
            units=units,
            gpu_operator_mhz=self.operator.mhz,
        )
        if self.capper:
            self.capper.restored.update(restored)  # so 0 ("no limit") survives as 0

    def shutdown(self) -> None:
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


def _mhz(v: int | None) -> str:
    return "none" if v is None else f"{v} MHz"


def _is_capped(c: Container) -> bool:
    return c.role == "batch" or c.cpu_cap

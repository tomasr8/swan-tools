"""SWAN cluster status dashboard.

A Textual-based TUI that reads KUBECONFIG and displays
live cluster health metrics relevant to SWAN operations.
"""

from __future__ import annotations

import enum
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime

from kubernetes import client, config  # type: ignore[import-untyped]
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.reactive import reactive
from textual.widgets import Footer, Header, Static


REFRESH_INTERVAL = 10  # seconds

# Namespaces where SWAN components typically live
SWAN_NAMESPACES = ("swan", "swan-cern-system", "kube-system")


class Severity(enum.Enum):
    OK = "ok"
    WARN = "warn"
    CRITICAL = "critical"


SEVERITY_STYLES = {
    Severity.OK: "green",
    Severity.WARN: "yellow",
    Severity.CRITICAL: "red",
}


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class PodPhaseStats:
    running: int = 0
    pending: int = 0
    succeeded: int = 0
    failed: int = 0
    unknown: int = 0
    terminating: int = 0
    total: int = 0

    @property
    def severity(self) -> Severity:
        if self.terminating > 10 or self.failed > 5:
            return Severity.CRITICAL
        if self.terminating > 0 or self.pending > 5 or self.failed > 0:
            return Severity.WARN
        return Severity.OK


@dataclass
class NodeHealth:
    name: str
    ready: bool
    disk_pressure: bool
    memory_pressure: bool
    pid_pressure: bool
    cpu_capacity: str
    mem_capacity: str
    cpu_allocatable: str
    mem_allocatable: str
    is_gpu: bool = False
    gpu_count: str = "0"
    roles: str = ""

    @property
    def severity(self) -> Severity:
        if not self.ready:
            return Severity.CRITICAL
        if self.disk_pressure or self.memory_pressure or self.pid_pressure:
            return Severity.WARN
        return Severity.OK


@dataclass
class ComponentStatus:
    name: str
    ready: bool
    restarts: int
    age_seconds: float
    memory_usage: str = ""  # from resource requests
    status: str = "Unknown"

    @property
    def severity(self) -> Severity:
        if not self.ready:
            return Severity.CRITICAL
        if self.restarts > 3:
            return Severity.WARN
        return Severity.OK


@dataclass
class K8sEvent:
    timestamp: datetime | None
    kind: str
    name: str
    reason: str
    message: str
    count: int


@dataclass
class ResourceSummary:
    cpu_requests_m: int = 0
    cpu_limits_m: int = 0
    cpu_capacity_m: int = 0
    mem_requests_mi: int = 0
    mem_limits_mi: int = 0
    mem_capacity_mi: int = 0


@dataclass
class ClusterSnapshot:
    """A point-in-time snapshot of cluster health."""

    timestamp: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    context: str = ""
    server: str = ""
    pod_stats: PodPhaseStats = field(default_factory=PodPhaseStats)
    nodes: list[NodeHealth] = field(default_factory=list)
    components: list[ComponentStatus] = field(default_factory=list)
    events: list[K8sEvent] = field(default_factory=list)
    resources: ResourceSummary = field(default_factory=ResourceSummary)
    user_pods: int = 0
    namespaces_checked: list[str] = field(default_factory=list)
    error: str | None = None


# ---------------------------------------------------------------------------
# Kubernetes data collection
# ---------------------------------------------------------------------------


def _parse_cpu(val: str) -> int:
    """Parse CPU string to millicores."""
    if not val:
        return 0
    if val.endswith("m"):
        return int(val[:-1])
    if val.endswith("n"):
        return int(val[:-1]) // 1_000_000
    return int(float(val) * 1000)


def _parse_mem(val: str) -> int:
    """Parse memory string to MiB."""
    if not val:
        return 0
    val = val.strip()
    units = {"Ki": 1 / 1024, "Mi": 1, "Gi": 1024, "Ti": 1024 * 1024, "K": 1 / 1024, "M": 1, "G": 1024}
    for suffix, factor in units.items():
        if val.endswith(suffix):
            return int(float(val[: -len(suffix)]) * factor)
    return int(val) // (1024 * 1024)  # assume bytes


def _is_swan_component(pod_name: str, namespace: str) -> bool:
    """Check if a pod is a core SWAN infrastructure component."""
    swan_prefixes = ("hub-", "chp-", "proxy-", "swan-", "sentry-", "grafana-", "prometheus-")
    kube_system_prefixes = (
        "coredns-",
        "etcd-",
        "kube-apiserver-",
        "kube-controller-",
        "kube-scheduler-",
        "kube-proxy-",
    )
    if namespace == "kube-system":
        return any(pod_name.startswith(p) for p in kube_system_prefixes)
    return any(pod_name.startswith(p) for p in swan_prefixes)


def _is_user_pod(pod_name: str) -> bool:
    """Check if a pod is a user session (jupyter-*)."""
    return pod_name.startswith("jupyter-")


def collect_snapshot() -> ClusterSnapshot:
    """Collect a full cluster health snapshot."""
    snap = ClusterSnapshot()

    try:
        contexts, active = config.list_kube_config_contexts()
        if active:
            snap.context = active.get("name", "unknown")
            cluster_info = active.get("context", {})
            snap.server = cluster_info.get("cluster", "")
    except config.ConfigException:
        pass

    try:
        config.load_kube_config()
    except config.ConfigException:
        snap.error = "Cannot load kubeconfig. Is KUBECONFIG set?"
        return snap

    v1 = client.CoreV1Api()

    # --- Nodes ---
    try:
        nodes = v1.list_node()
        for node in nodes.items:
            conditions = {c.type: c.status for c in (node.status.conditions or [])}
            labels = node.metadata.labels or {}
            roles_list = [
                k.removeprefix("node-role.kubernetes.io/") for k in labels if k.startswith("node-role.kubernetes.io/")
            ]

            is_gpu = bool(
                labels.get("nvidia.com/gpu.present") == "true"
                or "gpu" in labels.get("node.kubernetes.io/instance-type", "").lower()
                or labels.get("feature.node.kubernetes.io/pci-10de.present") == "true",  # NVIDIA PCI vendor
            )
            gpu_count = node.status.capacity.get("nvidia.com/gpu", "0") if node.status.capacity else "0"

            alloc = node.status.allocatable or {}
            cap = node.status.capacity or {}

            snap.nodes.append(
                NodeHealth(
                    name=node.metadata.name,
                    ready=conditions.get("Ready") == "True",
                    disk_pressure=conditions.get("DiskPressure") == "True",
                    memory_pressure=conditions.get("MemoryPressure") == "True",
                    pid_pressure=conditions.get("PIDPressure") == "True",
                    cpu_capacity=cap.get("cpu", "0"),
                    mem_capacity=cap.get("memory", "0"),
                    cpu_allocatable=alloc.get("cpu", "0"),
                    mem_allocatable=alloc.get("memory", "0"),
                    is_gpu=is_gpu,
                    gpu_count=gpu_count,
                    roles=", ".join(roles_list) or "worker",
                ),
            )

            snap.resources.cpu_capacity_m += _parse_cpu(alloc.get("cpu", "0"))
            snap.resources.mem_capacity_mi += _parse_mem(alloc.get("memory", "0"))
    except client.ApiException as e:
        snap.error = f"Failed to list nodes: {e.reason}"
        return snap

    # --- Pods across all relevant namespaces ---
    try:
        all_pods = v1.list_pod_for_all_namespaces()
    except client.ApiException as e:
        snap.error = f"Failed to list pods: {e.reason}"
        return snap

    checked_ns: set[str] = set()

    for pod in all_pods.items:
        ns = pod.metadata.namespace
        name = pod.metadata.name
        phase = pod.status.phase or "Unknown"
        is_terminating = pod.metadata.deletion_timestamp is not None

        # Count resource requests
        for container in pod.spec.containers or []:
            requests = (container.resources.requests or {}) if container.resources else {}
            limits = (container.resources.limits or {}) if container.resources else {}
            snap.resources.cpu_requests_m += _parse_cpu(requests.get("cpu", "0"))
            snap.resources.mem_requests_mi += _parse_mem(requests.get("memory", "0"))
            snap.resources.cpu_limits_m += _parse_cpu(limits.get("cpu", "0"))
            snap.resources.mem_limits_mi += _parse_mem(limits.get("memory", "0"))

        # Pod phase stats (cluster-wide)
        snap.pod_stats.total += 1
        if is_terminating:
            snap.pod_stats.terminating += 1
        elif phase == "Running":
            snap.pod_stats.running += 1
        elif phase == "Pending":
            snap.pod_stats.pending += 1
        elif phase == "Succeeded":
            snap.pod_stats.succeeded += 1
        elif phase == "Failed":
            snap.pod_stats.failed += 1
        else:
            snap.pod_stats.unknown += 1

        # SWAN components & user pods (in SWAN namespaces)
        if ns not in SWAN_NAMESPACES:
            continue
        checked_ns.add(ns)

        if _is_user_pod(name):
            if phase == "Running" and not is_terminating:
                snap.user_pods += 1

        if _is_swan_component(name, ns):
            container_statuses = pod.status.container_statuses or []
            all_ready = all(cs.ready for cs in container_statuses) if container_statuses else False
            total_restarts = sum(cs.restart_count for cs in container_statuses)
            created = pod.metadata.creation_timestamp
            age = (datetime.now(tz=UTC) - created).total_seconds() if created else 0

            mem_req = ""
            for container in pod.spec.containers or []:
                requests = (container.resources.requests or {}) if container.resources else {}
                if requests.get("memory"):
                    mem_req = requests["memory"]

            display_status = phase
            if is_terminating:
                display_status = "Terminating"
            elif not all_ready and phase == "Running":
                display_status = "NotReady"

            snap.components.append(
                ComponentStatus(
                    name=f"{ns}/{name}",
                    ready=all_ready and not is_terminating,
                    restarts=total_restarts,
                    age_seconds=age,
                    memory_usage=mem_req,
                    status=display_status,
                ),
            )

    snap.namespaces_checked = sorted(checked_ns)

    # --- Warning events (last hour, in SWAN namespaces) ---
    try:
        for ns in checked_ns:
            events = v1.list_namespaced_event(ns)
            for ev in events.items:
                if ev.type != "Warning":
                    continue
                ts = ev.last_timestamp or ev.event_time
                if ts and (datetime.now(tz=UTC) - ts).total_seconds() > 3600:
                    continue
                snap.events.append(
                    K8sEvent(
                        timestamp=ts,
                        kind=ev.involved_object.kind or "",
                        name=ev.involved_object.name or "",
                        reason=ev.reason or "",
                        message=(ev.message or "")[:120],
                        count=ev.count or 1,
                    ),
                )
    except client.ApiException:
        pass  # events are best-effort

    # Sort events newest first
    snap.events.sort(key=lambda e: e.timestamp or datetime.min.replace(tzinfo=UTC), reverse=True)
    snap.events = snap.events[:20]  # keep top 20

    return snap


# ---------------------------------------------------------------------------
# TUI Widgets
# ---------------------------------------------------------------------------


def _severity_markup(text: str, severity: Severity) -> str:
    color = SEVERITY_STYLES[severity]
    return f"[{color}]{text}[/{color}]"


def _bar(used: int, total: int, width: int = 20) -> str:
    """Render a text-based usage bar."""
    if total == 0:
        return "[dim]n/a[/dim]"
    pct = min(used / total, 1.0)
    filled = int(pct * width)
    empty = width - filled
    color = "green" if pct < 0.7 else "yellow" if pct < 0.9 else "red"
    return f"[{color}]{'█' * filled}[/{color}][dim]{'░' * empty}[/dim] {pct:.0%}"


def _format_age(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


class ClusterHeader(Static):
    """Shows cluster context and last refresh time."""

    def render(self) -> str:
        snap: ClusterSnapshot | None = self.app.snapshot  # type: ignore[attr-defined]
        if snap is None:
            return "[dim]Connecting to cluster...[/dim]"
        if snap.error:
            return f"[red bold]✗ Error:[/red bold] [red]{snap.error}[/red]"

        ts = snap.timestamp.strftime("%H:%M:%S")
        parts = [
            f"[bold]⎈ {snap.context}[/bold]",
            f"[dim]|[/dim] refreshed {ts}",
            f"[dim]|[/dim] namespaces: {', '.join(snap.namespaces_checked) or 'none found'}",
        ]
        return "  ".join(parts)


class PodOverviewPanel(Static):
    """Pod phase distribution with terminating highlight."""

    def render(self) -> str:
        snap: ClusterSnapshot | None = self.app.snapshot  # type: ignore[attr-defined]
        if snap is None or snap.error:
            return ""

        s = snap.pod_stats
        sev = s.severity

        lines = [
            f"[bold]Pods[/bold]  {_severity_markup('●', sev)}",
            "",
            f"  [green]Running[/green]      {s.running:>5}",
            f"  [yellow]Pending[/yellow]      {s.pending:>5}",
            f"  [red]Failed[/red]       {s.failed:>5}",
            f"  [dim]Succeeded[/dim]    {s.succeeded:>5}",
            f"  [dim]Unknown[/dim]      {s.unknown:>5}",
        ]

        # Terminating gets special treatment — this was the source of outages
        term_color = "red bold" if s.terminating > 10 else "yellow bold" if s.terminating > 0 else "dim"
        lines.append(f"  [{term_color}]Terminating  {s.terminating:>5}[/{term_color}]")
        lines.append("  [dim]─────────────────[/dim]")
        lines.append(f"  Total        {s.total:>5}")

        return "\n".join(lines)


class UserSessionsPanel(Static):
    """Active user sessions count."""

    def render(self) -> str:
        snap: ClusterSnapshot | None = self.app.snapshot  # type: ignore[attr-defined]
        if snap is None or snap.error:
            return ""

        return f"[bold]User Sessions[/bold]\n\n  [cyan]{snap.user_pods}[/cyan] active jupyter pods"


class ResourcePanel(Static):
    """Cluster resource allocation bars."""

    def render(self) -> str:
        snap: ClusterSnapshot | None = self.app.snapshot  # type: ignore[attr-defined]
        if snap is None or snap.error:
            return ""

        r = snap.resources
        cpu_bar = _bar(r.cpu_requests_m, r.cpu_capacity_m)
        mem_bar = _bar(r.mem_requests_mi, r.mem_capacity_mi)

        return (
            f"[bold]Resource Allocation[/bold]\n\n"
            f"  CPU  {cpu_bar}\n"
            f"       [dim]{r.cpu_requests_m}m / {r.cpu_capacity_m}m requested[/dim]\n\n"
            f"  MEM  {mem_bar}\n"
            f"       [dim]{r.mem_requests_mi}Mi / {r.mem_capacity_mi}Mi requested[/dim]"
        )


class NodesPanel(Static):
    """Node health summary with GPU/pressure flags."""

    def render(self) -> str:
        snap: ClusterSnapshot | None = self.app.snapshot  # type: ignore[attr-defined]
        if snap is None or snap.error:
            return ""

        nodes = snap.nodes
        ready = sum(1 for n in nodes if n.ready)
        gpu_nodes = [n for n in nodes if n.is_gpu]
        gpu_ready = sum(1 for n in gpu_nodes if n.ready)
        disk_pressure = [n for n in nodes if n.disk_pressure]
        mem_pressure = [n for n in nodes if n.memory_pressure]

        total_sev = Severity.OK
        if any(not n.ready for n in nodes):
            total_sev = Severity.CRITICAL
        elif disk_pressure or mem_pressure:
            total_sev = Severity.WARN

        lines = [
            f"[bold]Nodes[/bold]  {_severity_markup('●', total_sev)}",
            "",
            f"  Ready    [green]{ready}[/green] / {len(nodes)}",
        ]

        if gpu_nodes:
            gpu_color = "green" if gpu_ready == len(gpu_nodes) else "red"
            total_gpus = sum(int(n.gpu_count) for n in gpu_nodes)
            lines.append(
                f"  GPU      [{gpu_color}]{gpu_ready}[/{gpu_color}] / {len(gpu_nodes)} nodes ({total_gpus} GPUs)"
            )

        if disk_pressure:
            names = ", ".join(n.name for n in disk_pressure)
            lines.append(f"  [yellow]⚠ DiskPressure:[/yellow] {names}")

        if mem_pressure:
            names = ", ".join(n.name for n in mem_pressure)
            lines.append(f"  [yellow]⚠ MemoryPressure:[/yellow] {names}")

        not_ready = [n for n in nodes if not n.ready]
        if not_ready:
            for n in not_ready:
                lines.append(f"  [red]✗ NotReady:[/red] {n.name} ({n.roles})")

        return "\n".join(lines)


class ComponentsTable(Static):
    """SWAN infrastructure components (hub, CHP, proxy, etc.)."""

    def render(self) -> str:
        snap: ClusterSnapshot | None = self.app.snapshot  # type: ignore[attr-defined]
        if snap is None or snap.error:
            return ""

        components = snap.components
        if not components:
            return "[bold]Components[/bold]\n\n  [dim]No SWAN components found in watched namespaces[/dim]"

        lines = [
            "[bold]Components[/bold]",
            "",
            f"  {'Name':<45} {'Status':<14} {'Restarts':>8}  {'Age':>5}  {'Mem':>8}",
            f"  [dim]{'─' * 88}[/dim]",
        ]

        # Sort: unhealthy first, then by name
        components.sort(key=lambda c: (c.severity == Severity.OK, c.name))

        for c in components:
            color = SEVERITY_STYLES[c.severity]
            status_icon = "✓" if c.ready else "✗"
            restart_str = str(c.restarts)
            if c.restarts > 3:
                restart_str = f"[yellow]{c.restarts}[/yellow]"
            elif c.restarts > 10:
                restart_str = f"[red]{c.restarts}[/red]"

            lines.append(
                f"  [{color}]{status_icon}[/{color}] {c.name:<43} "
                f"[{color}]{c.status:<12}[/{color}] "
                f"{restart_str:>8}  "
                f"{_format_age(c.age_seconds):>5}  "
                f"[dim]{c.memory_usage or '-':>8}[/dim]",
            )

        return "\n".join(lines)


class EventsPanel(Static):
    """Recent warning events from SWAN namespaces."""

    def render(self) -> str:
        snap: ClusterSnapshot | None = self.app.snapshot  # type: ignore[attr-defined]
        if snap is None or snap.error:
            return ""

        if not snap.events:
            return "[bold]Recent Warnings[/bold]  [green]●[/green]\n\n  [dim]No warnings in the last hour[/dim]"

        lines = [
            f"[bold]Recent Warnings[/bold]  [yellow]●[/yellow]  [dim]({len(snap.events)} in last hour)[/dim]",
            "",
        ]

        for ev in snap.events[:12]:
            ts = ev.timestamp.strftime("%H:%M") if ev.timestamp else "??:??"
            count_str = f"(×{ev.count})" if ev.count > 1 else ""
            lines.append(
                f"  [dim]{ts}[/dim]  [yellow]{ev.reason:<20}[/yellow] {ev.kind}/{ev.name} {count_str}",
            )
            lines.append(f"         [dim]{ev.message}[/dim]")

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

DASHBOARD_CSS = """
Screen {
    layout: vertical;
}

#cluster-header {
    dock: top;
    height: 1;
    padding: 0 1;
    background: $surface;
    color: $text;
}

#top-row {
    height: auto;
    max-height: 16;
    padding: 1 1 0 1;
}

#pod-overview {
    width: 1fr;
    min-width: 24;
    padding: 1 2;
    border: round $primary-lighten-2;
}

#user-sessions {
    width: 1fr;
    min-width: 24;
    padding: 1 2;
    border: round $primary-lighten-2;
}

#resources {
    width: 2fr;
    min-width: 40;
    padding: 1 2;
    border: round $primary-lighten-2;
}

#nodes {
    width: 2fr;
    min-width: 40;
    padding: 1 2;
    border: round $primary-lighten-2;
}

#components {
    margin: 1;
    padding: 1 2;
    border: round $primary-lighten-2;
    height: auto;
    max-height: 20;
}

#events {
    margin: 0 1 1 1;
    padding: 1 2;
    border: round $primary-lighten-2;
    height: auto;
    max-height: 20;
}
"""


class SwanStatusApp(App):
    """SWAN cluster health dashboard."""

    CSS = DASHBOARD_CSS
    TITLE = "SWAN Status"
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
    ]

    snapshot: reactive[ClusterSnapshot | None] = reactive(None)  # type: ignore[assignment]

    def compose(self) -> ComposeResult:
        yield Header()
        yield ClusterHeader(id="cluster-header")
        with Horizontal(id="top-row"):
            yield PodOverviewPanel(id="pod-overview")
            yield UserSessionsPanel(id="user-sessions")
            yield ResourcePanel(id="resources")
            yield NodesPanel(id="nodes")
        yield ComponentsTable(id="components")
        yield EventsPanel(id="events")
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_data()
        self.set_interval(REFRESH_INTERVAL, self.refresh_data)

    @work(thread=True, exclusive=True)
    def refresh_data(self) -> None:
        snap = collect_snapshot()
        self.call_from_thread(setattr, self, "snapshot", snap)

    def watch_snapshot(self) -> None:
        """Called whenever self.snapshot changes — triggers a repaint of all panels."""
        for widget_id in (
            "cluster-header",
            "pod-overview",
            "user-sessions",
            "resources",
            "nodes",
            "components",
            "events",
        ):
            try:
                self.query_one(f"#{widget_id}").refresh()
            except Exception:  # noqa: BLE001
                pass

    def action_refresh(self) -> None:
        self.refresh_data()


def run_dashboard() -> None:
    """Entry point called from the CLI."""
    kubeconfig = os.environ.get("KUBECONFIG")
    if not kubeconfig:
        import click

        click.secho(
            "  ⚠ KUBECONFIG is not set — will try default (~/.kube/config)",
            fg="yellow",
        )
    app = SwanStatusApp()
    app.run()

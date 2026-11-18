from pathlib import Path
import shutil
import uuid

from app.tools.web.contracts import WebReaderError


class ReaderResourceScope:
    def __init__(self, *, max_memory_mb=1536, max_cpu_percent=100, max_tasks=256):
        self.max_memory_bytes = max_memory_mb * 1048576
        self.max_cpu_percent = max_cpu_percent
        self.max_tasks = max_tasks
        self.unit = f"astra-web-reader-{uuid.uuid4().hex}.scope"

    def command(self, command):
        executable = shutil.which("systemd-run")
        if executable is None:
            raise WebReaderError("reader_unavailable", "The isolated reader requires systemd user scopes with cgroup v2 memory, CPU and task controls.")
        return [
            executable, "--user", "--scope", "--quiet", "--collect", "--no-ask-password",
            "--expand-environment=no", f"--unit={self.unit}",
            "--description=Astra isolated page reader",
            f"--property=MemoryMax={self.max_memory_bytes}",
            f"--property=MemoryHigh={self.max_memory_bytes * 4 // 5}",
            "--property=MemorySwapMax=0", "--property=OOMPolicy=kill",
            f"--property=CPUQuota={self.max_cpu_percent}%", f"--property=TasksMax={self.max_tasks}",
            *command,
        ]

    def verify(self, membership, *, root=Path("/sys/fs/cgroup")):
        if not isinstance(membership, str) or not membership.startswith("0::/") or "\n" in membership:
            raise WebReaderError("resource_isolation", "The reader did not report a valid unified cgroup membership.")
        relative = membership[len("0::/"):]
        if Path(relative).is_absolute() or ".." in Path(relative).parts or Path(relative).name != self.unit:
            raise WebReaderError("resource_isolation", "The reader is not running in its own expected resource scope.")
        directory = (root / relative).resolve()
        if not directory.is_relative_to(root.resolve()):
            raise WebReaderError("resource_isolation", "The reported reader resource scope is outside the cgroup hierarchy.")
        try:
            memory = int((directory / "memory.max").read_text().strip())
            swap = int((directory / "memory.swap.max").read_text().strip())
            oom_group = int((directory / "memory.oom.group").read_text().strip())
            tasks = int((directory / "pids.max").read_text().strip())
            quota, period = map(int, (directory / "cpu.max").read_text().split())
        except (OSError, ValueError) as error:
            raise WebReaderError("resource_isolation", "Kernel memory, CPU and task limits could not be verified; page reading was not started.") from error
        if not (0 < memory <= self.max_memory_bytes and swap == 0 and oom_group == 1
                and 0 < tasks <= self.max_tasks and 0 < quota and 0 < period
                and quota * 100 <= period * self.max_cpu_percent):
            raise WebReaderError("resource_isolation", "The reader resource scope does not enforce its configured hard limits.")
        return {"memory_max_bytes": memory, "swap_max_bytes": swap,
                "cpu_quota_us": quota, "cpu_period_us": period, "tasks_max": tasks,
                "oom_group": True}

"""get_io_info monitoring tool."""

from __future__ import annotations

import psutil


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def get_io_info() -> str:
    """Collect disk and network I/O counters."""
    lines = []

    # Disk I/O
    disk_io = psutil.disk_io_counters(perdisk=True)
    if disk_io:
        lines.append("Disk I/O:")
        for name, counters in disk_io.items():
            lines.append(
                f"  {name}: read={_fmt_bytes(counters.read_bytes)}, "
                f"write={_fmt_bytes(counters.write_bytes)}, "
                f"read_ops={counters.read_count}, write_ops={counters.write_count}"
            )
    else:
        lines.append("Disk I/O: not available")

    # Network I/O
    net_io = psutil.net_io_counters(pernic=True)
    if net_io:
        lines.append("Network I/O:")
        for name, counters in net_io.items():
            lines.append(
                f"  {name}: sent={_fmt_bytes(counters.bytes_sent)}, "
                f"recv={_fmt_bytes(counters.bytes_recv)}, "
                f"packets_sent={counters.packets_sent}, "
                f"packets_recv={counters.packets_recv}, "
                f"errin={counters.errin}, errout={counters.errout}, "
                f"dropin={counters.dropin}, dropout={counters.dropout}"
            )
    else:
        lines.append("Network I/O: not available")

    return "\n".join(lines)

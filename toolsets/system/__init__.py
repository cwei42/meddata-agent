"""System toolset: read-only status of the Orin (memory, disk, temperature, GPU load, network)."""
import glob
import os
import shutil
import subprocess

from harness import Toolset, tool


def _read(path, default=""):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def setup(params):
    @tool("get_system_status", "Memory, disk, CPU load, uptime and power mode of the Orin.")
    def get_system_status():
        mem = {}
        for line in _read("/proc/meminfo").splitlines():
            k, v = line.split(":", 1)
            mem[k] = int(v.split()[0]) // 1024
        du = shutil.disk_usage("/")
        load = _read("/proc/loadavg").split()[:3]
        up = float(_read("/proc/uptime", "0").split()[0])
        try:
            pm = subprocess.run(["sudo", "-n", "nvpmodel", "-q"], capture_output=True, text=True, timeout=5).stdout.split("\n")[0]
        except Exception:
            pm = "unknown"
        return (f"memory: {mem.get('MemTotal', 0) - mem.get('MemAvailable', 0)} MB used of {mem.get('MemTotal', 0)} MB "
                f"({mem.get('MemAvailable', 0)} MB available)\n"
                f"disk /: {du.used // 2**30} GB used of {du.total // 2**30} GB\n"
                f"cpu load (1/5/15 min): {' '.join(load)}\nuptime: {up / 3600:.1f} h\npower mode: {pm}")

    @tool("get_temperatures", "Temperatures of CPU, GPU and other thermal zones in °C, plus GPU load.")
    def get_temperatures():
        out = []
        for z in sorted(glob.glob("/sys/class/thermal/thermal_zone*")):
            t = _read(z + "/temp")
            if t.lstrip("-").isdigit():
                out.append(f"{_read(z + '/type')}: {int(t) / 1000:.1f} °C")
        for p in glob.glob("/sys/devices/platform/*/*gpu*/load") + glob.glob("/sys/devices/platform/gpu.0/load"):
            v = _read(p)
            if v.isdigit():
                out.append(f"GPU load: {int(v) / 10:.0f} %")
                break
        return "\n".join(out) or "no thermal data"

    @tool("get_network", "Network interfaces with IPv4 addresses and the connected WiFi.")
    def get_network():
        ip = subprocess.run(["ip", "-4", "-br", "addr"], capture_output=True, text=True).stdout
        wifi = subprocess.run(["nmcli", "-t", "-f", "DEVICE,STATE,CONNECTION", "dev"],
                              capture_output=True, text=True).stdout
        return "interfaces:\n" + ip + "\nnetworkmanager:\n" + wifi

    return Toolset(name="system", tools=[get_system_status, get_temperatures, get_network],
                   describe=f"Orin status ({os.uname().nodename})")

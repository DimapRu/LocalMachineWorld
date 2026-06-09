"""System resource monitoring service."""

import psutil
from typing import Optional

from app.schemas import ResourceStatus


class ResourceMonitor:
    """Monitor system resources (RAM, VRAM)."""

    def get_ram_info(self) -> dict:
        """Get RAM usage information."""
        mem = psutil.virtual_memory()
        return {
            "total_gb": round(mem.total / (1024**3), 2),
            "used_gb": round(mem.used / (1024**3), 2),
            "available_gb": round(mem.available / (1024**3), 2),
            "percent": mem.percent,
        }

    def get_vram_info(self) -> Optional[dict]:
        """Get VRAM usage information (NVIDIA GPU)."""
        try:
            import pynvml

            pynvml.nvmlInit()
            handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            mem_info = pynvml.nvmlDeviceGetMemoryInfo(handle)
            gpu_name = pynvml.nvmlDeviceGetName(handle)
            if isinstance(gpu_name, bytes):
                gpu_name = gpu_name.decode("utf-8")

            result = {
                "gpu_name": gpu_name,
                "total_gb": round(mem_info.total / (1024**3), 2),
                "used_gb": round(mem_info.used / (1024**3), 2),
                "free_gb": round(mem_info.free / (1024**3), 2),
                "percent": round(mem_info.used / mem_info.total * 100, 1),
            }
            pynvml.nvmlShutdown()
            return result
        except Exception:
            return None

    def get_status(self) -> ResourceStatus:
        """Get full resource status."""
        ram = self.get_ram_info()
        vram = self.get_vram_info()

        return ResourceStatus(
            ram_total_gb=ram["total_gb"],
            ram_used_gb=ram["used_gb"],
            ram_percent=ram["percent"],
            vram_total_gb=vram["total_gb"] if vram else None,
            vram_used_gb=vram["used_gb"] if vram else None,
            vram_percent=vram["percent"] if vram else None,
            gpu_available=vram is not None,
        )

    def can_load_model(self, estimated_gb: float, use_gpu: bool = False) -> bool:
        """Check if there's enough memory to load a model."""
        if use_gpu:
            vram = self.get_vram_info()
            if not vram:
                return False
            return vram["free_gb"] >= estimated_gb
        else:
            ram = self.get_ram_info()
            return ram["available_gb"] >= estimated_gb


# Singleton instance
resource_monitor = ResourceMonitor()

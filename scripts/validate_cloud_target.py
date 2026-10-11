"""Cloud target resource and API health validator.
Validates memory service healthz, API authentication, inference proxy, and system resources.
"""
from __future__ import annotations
import argparse
import json
import os
import shutil
import sys
import httpx
from typing import Optional


def check_system_resources() -> dict:
    """Check memory, swap, and disk on host."""
    res = {}
    total_disk, used_disk, free_disk = shutil.disk_usage("/")
    res["disk"] = {
        "total_gb": round(total_disk / (1024**3), 2),
        "free_gb": round(free_disk / (1024**3), 2),
    }

    # Check Linux /proc/meminfo if available
    meminfo_path = "/proc/meminfo"
    if os.path.exists(meminfo_path):
        try:
            with open(meminfo_path, "r") as f:
                lines = f.readlines()
            info = {}
            for line in lines:
                parts = line.split(":")
                if len(parts) == 2:
                    info[parts[0].strip()] = parts[1].strip()
            res["memory"] = {
                "mem_total": info.get("MemTotal"),
                "mem_available": info.get("MemAvailable"),
                "swap_total": info.get("SwapTotal"),
                "swap_free": info.get("SwapFree"),
            }
        except Exception as exc:
            res["memory_error"] = str(exc)
    else:
        res["os"] = sys.platform

    return res


def validate_cloud(
    api_url: str,
    token: Optional[str] = None,
    proxy_url: Optional[str] = None,
    proxy_key: Optional[str] = None,
) -> dict:
    results = {"target_api": api_url, "checks": []}
    api_url = api_url.rstrip("/")

    with httpx.Client(timeout=10.0) as client:
        # 1. Check healthz
        try:
            r = client.get(f"{api_url}/healthz")
            if r.status_code == 200:
                results["checks"].append({"name": "healthz", "status": "pass", "details": r.json()})
            else:
                results["checks"].append({"name": "healthz", "status": "fail", "details": f"HTTP {r.status_code}"})
        except Exception as exc:
            results["checks"].append({"name": "healthz", "status": "fail", "error": str(exc)})

        # 2. Check authenticated health
        if token:
            try:
                headers = {"Authorization": f"Bearer {token}"}
                r = client.get(f"{api_url}/api/v1/health", headers=headers)
                if r.status_code == 200:
                    results["checks"].append({"name": "authenticated_health", "status": "pass", "details": r.json()})
                else:
                    results["checks"].append({"name": "authenticated_health", "status": "fail", "details": f"HTTP {r.status_code}: {r.text}"})
            except Exception as exc:
                results["checks"].append({"name": "authenticated_health", "status": "fail", "error": str(exc)})

        # 3. Check Inference Proxy
        if proxy_url:
            proxy_url = proxy_url.rstrip("/")
            try:
                p_headers = {"Authorization": f"Bearer {proxy_key}"} if proxy_key else {}
                r = client.get(f"{proxy_url}/models", headers=p_headers)
                if r.status_code == 200:
                    results["checks"].append({"name": "inference_proxy", "status": "pass", "details": r.json()})
                else:
                    results["checks"].append({"name": "inference_proxy", "status": "fail", "details": f"HTTP {r.status_code}"})
            except Exception as exc:
                results["checks"].append({"name": "inference_proxy", "status": "fail", "error": str(exc)})

    results["system_resources"] = check_system_resources()
    results["all_passed"] = all(c.get("status") == "pass" for c in results["checks"])
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate Hermes Cloud Target Resources and Endpoints")
    parser.add_argument("--api-url", default=os.environ.get("HERMES_SYNC_URL", "http://127.0.0.1:8787"))
    parser.add_argument("--token", default=os.environ.get("HERMES_READ_TOKEN"))
    parser.add_argument("--proxy-url", default=os.environ.get("HERMES_PROXY_URL"))
    parser.add_argument("--proxy-key", default=os.environ.get("HERMES_PROXY_KEY"))
    args = parser.parse_args()

    res = validate_cloud(
        api_url=args.api_url,
        token=args.token,
        proxy_url=args.proxy_url,
        proxy_key=args.proxy_key,
    )
    print(json.dumps(res, indent=2))
    sys.exit(0 if res["all_passed"] else 1)


if __name__ == "__main__":
    main()

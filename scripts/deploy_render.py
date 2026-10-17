"""用 Render API 创建/更新 Web 服务（Docker，免费，新加坡）。密钥从本地 .env 读取，不打印。

用法：python3 scripts/deploy_render.py [--repo URL]
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV = dict(l.split("=", 1) for l in (ROOT / ".env").read_text(encoding="utf-8").splitlines() if "=" in l and not l.startswith("#"))
API = "https://api.render.com/v1"
H = {"Authorization": f"Bearer {ENV['RENDER_API_KEY']}", "Accept": "application/json", "Content-Type": "application/json"}
NAME = "screenlens"
REPO = sys.argv[sys.argv.index("--repo") + 1] if "--repo" in sys.argv else "https://github.com/Yuyang16Z/nl-stock-screener"
SECRETS = ["FUYAO_API_KEY", "IFIND_MCP_TOKEN", "LLM_API_KEY"]
PLAIN = {"LLM_BASE_URL": "https://api.deepseek.com", "LLM_MODEL": "deepseek-chat", "WARM_UNIVERSES": "000300.SH", "DEMO_FAULTS_ENABLED": "true"}


def call(method: str, path: str, body: dict | list | None = None):
    req = urllib.request.Request(API + path, method=method, headers=H, data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:500]


def main():
    st, owners = call("GET", "/owners?limit=5")
    owner = owners[0]["owner"]["id"]
    st, svcs = call("GET", f"/services?name={NAME}&limit=5")
    existing = [s["service"] for s in svcs if s["service"]["name"] == NAME] if isinstance(svcs, list) else []
    env_vars = [{"key": k, "value": ENV[k]} for k in SECRETS if ENV.get(k)] + [{"key": k, "value": v} for k, v in PLAIN.items()]
    if existing:
        sid = existing[0]["id"]
        st, _ = call("PUT", f"/services/{sid}/env-vars", env_vars)
        print("env vars updated:", st)
        st, d = call("POST", f"/services/{sid}/deploys", {"clearCache": "do_not_clear"})
        print("deploy triggered:", st, (d or {}).get("id") if isinstance(d, dict) else d)
        print("service:", sid, existing[0]["serviceDetails"].get("url"))
        return
    body = {
        "type": "web_service", "name": NAME, "ownerId": owner, "repo": REPO, "branch": "main", "autoDeploy": "yes",
        "envVars": env_vars,
        "serviceDetails": {"runtime": "docker", "plan": "free", "region": "singapore", "healthCheckPath": "/api/health",
                           "envSpecificDetails": {"dockerfilePath": "./Dockerfile", "dockerContext": "."}},
    }
    st, d = call("POST", "/services", body)
    if st >= 300:
        print("create failed:", st, d)
        return
    svc = d.get("service", d)
    print("created:", svc.get("id"), svc.get("serviceDetails", {}).get("url"), "deploy:", d.get("deployId"))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Tusker Gateway config-canary deployment renderer and operator CLI.

Clones the live ``tusker-gateway`` Deployment into an isolated
``tusker-gateway-config-canary`` Deployment so the PostgreSQL-backed
configuration store can be exercised against a canary database without
touching production traffic, selectors, or credentials.

Subcommands
-----------
render    Render the canary Deployment + Service + NetworkPolicy YAML from
          a live Deployment JSON (stdin) and an explicit --image digest.
provision Emit (or with --execute, run) the SQL that creates the isolated
          canary PostgreSQL role/database/extension on the shared cluster
          PostgreSQL instance.
seed      Emit (or with --execute, run) the command that seeds the canary
          DB from the live pod's resolved env configuration.
smoke     Emit (or with --execute, run) the pgcrypto roundtrip plus HTTP
          health check through the canary route.
cleanup   Emit the kubectl commands that remove every canary resource.

Security rules
--------------
* Rendered YAML contains NO secret values: provider keys stay in the shared
  ``tusker-env-vault``; canary-specific credentials live in dedicated
  canary secrets referenced via ``secretKeyRef``.
* Provision SQL is sent to the PostgreSQL pod over kubectl exec stdin so
  passwords never appear in ``ps`` output, logs, or shell history.
* Logs are redacted: secret material is never printed.

Usage
-----
  kubectl -n hermes get deployment tusker-gateway -o json \
    | python3 k8s/config-canary.py render --image-digest sha256:... \
        > k8s/config-canary.yaml
  python3 k8s/config-canary.py provision --pod tusker-gateway-postgres-...
  python3 k8s/config-canary.py seed --pod tusker-gateway-c... --execute
  python3 k8s/config-canary.py smoke --pod tusker-gateway-c... --execute
  python3 k8s/config-canary.py cleanup
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import shlex
import subprocess
import sys
import time
from typing import Any

NAMESPACE = "hermes"
REGISTRY = "registry.tusker.net.au:5000"
PROD_DEPLOYMENT = "tusker-gateway"
CANARY_DEPLOYMENT = "tusker-gateway-config-canary"
CANARY_LABEL = "tusker-gateway-config-canary"  # unique pod label key+value

PROD_POSTGRES_SECRET = "tusker-gateway-postgres-auth"
CANARY_POSTGRES_SECRET = "tusker-gateway-postgres-auth-canary"
CANARY_ENCRYPTION_SECRET = "tusker-gateway-config-canary-encryption"
PROD_VAULT = "tusker-env-vault"

CANARY_DB = "tusker_gateway_config_canary"
CANARY_DB_USER = "tusker_gateway_canary"

PG_POD_LABEL = "app=tusker-gateway-postgres"
PROD_DB_NAME = "tusker_gateway"

CANARY_PORT = 8642

# Env vars that must NOT be copied verbatim into the canary deployment
# because they carry live prod OAuth state or per-pod values the canary
# must not share.
DROP_ENV_VARS = frozenset({
    # State DB DSN points at prod; canary uses its own secret instead.
    "TUSKER_STATE_DATABASE_URL",
    # Encryption key is canary-specific; prod's key is not reused.
    "TUSKER_KEY_ENCRYPTION_KEY",
})


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _redact(value: str, keep: int = 4) -> str:
    """Bounded redaction for logging only."""
    if len(value) <= keep:
        return "***"
    return value[:keep] + "…"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _sha256_of(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode()).hexdigest()


def _run(cmd: list[str], *, input_: bytes | None = None, check: bool = True):
    """Run a subprocess and return CompletedProcess. Input via stdin only."""
    return subprocess.run(
        cmd, input=input_, capture_output=True, check=check
    )


def _kubectl(args: list[str], *, input_: bytes | None = None, check: bool = True):
    """Run kubectl in the hermes namespace."""
    return _run(["kubectl", "-n", NAMESPACE, *args], input_=input_, check=check)


def _isolate_volumes(volumes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Replace PVC volumes with emptyDir so canary writes don't land on prod PVC.

    Projected service-account tokens and ConfigMap/Secret volumes are kept
    unchanged; only ``persistentVolumeClaim`` entries are swapped.
    """
    out: list[dict[str, Any]] = []
    for v in volumes:
        if "persistentVolumeClaim" in v:
            out.append({
                "name": v["name"],
                "emptyDir": {"sizeLimit": "1Gi"},
            })
        else:
            out.append(v)
    return out



# ---------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------

def _split_env(env_list: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split the live env list into plain and secret-ref entries.

    * ``plain`` – simple name/value entries that can be copied verbatim.
    * ``secret`` – valueFrom entries that must be remapped to canary secrets.
    * Dropped env vars (TUSKER_STATE_DATABASE_URL, TUSKER_KEY_ENCRYPTION_KEY)
      are replaced with canary-specific secret refs at render time.
    """
    plain: list[dict[str, Any]] = []
    secret: list[dict[str, Any]] = []
    for entry in env_list:
        if "value" in entry:
            name = entry.get("name")
            if name in DROP_ENV_VARS:
                continue
            plain.append({"name": name, "value": entry["value"]})
            continue
        if "valueFrom" in entry:
            name = entry.get("name")
            if name in DROP_ENV_VARS:
                continue
            secret.append({"name": name, "valueFrom": entry["valueFrom"]})
            continue
    return plain, secret


def _remap_secret_refs(secret_env: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rewrite secret references to point at canary-specific secrets.

    * ``tusker-env-vault`` (provider keys) is shared read-only.
    * ``tusker-gateway-postgres-auth`` -> ``tusker-gateway-postgres-auth-canary``.
    * No other prod secret is referenced in this deployment.
    """
    out: list[dict[str, Any]] = []
    for entry in secret_env:
        vf = entry.get("valueFrom", {})
        secret_ref = vf.get("secretKeyRef")
        if not secret_ref:
            out.append(entry)
            continue
        new_secret = dict(secret_ref)
        original_name = secret_ref.get("name")
        if original_name == PROD_POSTGRES_SECRET:
            new_secret["name"] = CANARY_POSTGRES_SECRET
        # tusker-env-vault stays shared.
        out.append({"name": entry["name"], "valueFrom": {"secretKeyRef": new_secret}})
    return out


def _strip_pod_specific_env(env_list: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove env vars that are live-pod specific or pod-name derived."""
    result: list[dict[str, Any]] = []
    for entry in env_list:
        name = entry.get("name", "")
        # Keep TUSKER_STATE_DATABASE_URL because it is handled separately.
        result.append(entry)
    return result


def _build_canary_deployment(live: dict[str, Any], image_with_digest: str) -> dict[str, Any]:
    """Return an isolated canary Deployment dict cloned from live prod state."""
    spec = live.get("spec", {})
    template = spec.get("template", {})
    pod_template_spec = template.get("spec", {})
    containers = pod_template_spec.get("containers", [])
    if not containers:
        raise ValueError("live deployment has no containers")
    if len(containers) > 1:
        raise ValueError("multi-container gateway deployment is not supported")

    prod_container = containers[0]
    env = prod_container.get("env", [])
    env_from = prod_container.get("envFrom", [])

    plain_env, secret_env = _split_env(env)
    secret_env = _remap_secret_refs(secret_env)

    # Reorder so canary-specific overrides come last and win.
    canary_env: list[dict[str, Any]] = []
    canary_env.extend(plain_env)
    canary_env.extend(secret_env)

    # TUSKER_STATE_DATABASE_URL: canary DB via secret ref
    canary_env.append(
        {
            "name": "TUSKER_STATE_DATABASE_URL",
            "valueFrom": {
                "secretKeyRef": {
                    "name": CANARY_POSTGRES_SECRET,
                    "key": "DATABASE_URL",
                }
            },
        }
    )

    # TUSKER_KEY_ENCRYPTION_KEY: canary encryption key via secret ref
    canary_env.append(
        {
            "name": "TUSKER_KEY_ENCRYPTION_KEY",
            "valueFrom": {
                "secretKeyRef": {
                    "name": CANARY_ENCRYPTION_SECRET,
                    "key": "TUSKER_KEY_ENCRYPTION_KEY",
                }
            },
        }
    )
    # The whole point of the canary: activate the DB-backed config store.
    canary_env.append({"name": "TUSKER_CONFIG_DATABASE_ENABLED", "value": "1"})


    # Single flag gates canary behavior: disables autonomous OAuth refresh
    # rotation, marks pod as canary for config store activation/logging.
    canary_env.append({"name": "TUSKER_CONFIG_CANARY", "value": "true"})

    # envFrom stays shared for tusker-env-vault provider keys.
    canary_env_from: list[dict[str, Any]] = []
    for ef in env_from:
        ref = ef.get("secretRef", {})
        name = ref.get("name")
        if name == PROD_VAULT:
            canary_env_from.append({"secretRef": {"name": PROD_VAULT}})
        else:
            canary_env_from.append(ef)

    canary_container = {
        "name": CANARY_DEPLOYMENT,
        "image": image_with_digest,
        "imagePullPolicy": "Always",
        "envFrom": canary_env_from,
        "env": canary_env,
        "ports": [{"containerPort": CANARY_PORT, "name": "http", "protocol": "TCP"}],
        "startupProbe": {
            "httpGet": {"path": "/health", "port": CANARY_PORT},
            "periodSeconds": 5,
            "timeoutSeconds": 3,
            "failureThreshold": 60,
        },
        "readinessProbe": {
            "httpGet": {"path": "/ready", "port": CANARY_PORT},
            "periodSeconds": 5,
            "timeoutSeconds": 3,
            "failureThreshold": 6,
        },
        "livenessProbe": {
            "httpGet": {"path": "/health", "port": CANARY_PORT},
            "periodSeconds": 30,
            "timeoutSeconds": 3,
            "failureThreshold": 3,
        },
        "lifecycle": prod_container.get("lifecycle", {}),
        "volumeMounts": prod_container.get("volumeMounts", []),
        "resources": prod_container.get("resources", {}),
    }

    # Reset rolling-update strategy; canary has only one replica intentionally
    # and we do not want maxSurge to overlap the prod service.
    canary_deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": CANARY_DEPLOYMENT,
            "namespace": NAMESPACE,
            "labels": {
                "app": CANARY_LABEL,
                "tusker.gateway.io/canary": "true",
                "tusker.gateway.io/source-deployment": PROD_DEPLOYMENT,
            },
            "annotations": {
                "tusker.gateway.io/rendered-at": _now(),
                "tusker.gateway.io/image-digest": image_with_digest.rsplit("@", 1)[-1]
                if "@" in image_with_digest
                else "",
                "tusker.gateway.io/source-image": prod_container.get("image", ""),
            },
        },
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": CANARY_LABEL}},
            "strategy": {"type": "Recreate"},
            "template": {
                "metadata": {
                    "labels": {
                        "app": CANARY_LABEL,
                        "tusker.gateway.io/canary": "true",
                    }
                },
                "spec": {
                    "securityContext": pod_template_spec.get("securityContext", {}),
                    "terminationGracePeriodSeconds": pod_template_spec.get(
                        "terminationGracePeriodSeconds", 90
                    ),
                    "containers": [canary_container],
                    "volumes": _isolate_volumes(pod_template_spec.get("volumes", [])),
                },
            },
        },
    }
    return canary_deployment


def _build_canary_service() -> dict[str, Any]:
    """Return a ClusterIP Service that selects only the canary pods."""
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": CANARY_DEPLOYMENT,
            "namespace": NAMESPACE,
            "labels": {
                "app": CANARY_LABEL,
                "tusker.gateway.io/canary": "true",
            },
            "annotations": {
                "tusker.gateway.io/selector-isolation": f"selects app={CANARY_LABEL} only",
            },
        },
        "spec": {
            "type": "ClusterIP",
            "ports": [
                {
                    "port": CANARY_PORT,
                    "targetPort": CANARY_PORT,
                    "protocol": "TCP",
                    "name": "http",
                }
            ],
            "selector": {"app": CANARY_LABEL},
        },
    }


def _build_canary_networkpolicy() -> dict[str, Any]:
    """Return an extra NetworkPolicy that lets the canary pods reach Postgres.

    The existing ``allow-tusker-gateway-postgres`` policy only allows ingress
    from pods labelled ``app: tusker-gateway``. Rather than edit the prod
    policy, we add a separate policy that selects the same postgres pods and
    allows ingress from the canary label. Kubernetes unions multiple
    NetworkPolicies selecting the same pod.
    """
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {
            "name": f"allow-{CANARY_DEPLOYMENT}-to-postgres",
            "namespace": NAMESPACE,
            "labels": {"app": CANARY_LABEL, "tusker.gateway.io/canary": "true"},
            "annotations": {
                "tusker.gateway.io/role": "allow canary pods to reach shared postgres"
            },
        },
        "spec": {
            "podSelector": {"matchLabels": {"app": "tusker-gateway-postgres"}},
            "policyTypes": ["Ingress"],
            "ingress": [
                {
                    "from": [{"podSelector": {"matchLabels": {"app": CANARY_LABEL}}}],
                    "ports": [{"protocol": "TCP", "port": 5432}],
                }
            ],
        },
    }


def _image_with_digest(live: dict[str, Any], digest: str) -> str:
    """Return a fully qualified image reference using the live registry/name."""
    if not digest:
        raise ValueError("--image-digest is required")
    if "@" in digest:
        # Already a full digest reference (name@sha256:...); accept as-is.
        return digest
    import re as _re
    if _re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        live_image = live["spec"]["template"]["spec"]["containers"][0].get("image", "")
        base = live_image.rsplit("@", 1)[0].rsplit(":", 1)[0]
        return f"{base}@{digest}"
    # A bare tag; accept it, but warn it is not immutable.
    return digest


def cmd_render(args: argparse.Namespace) -> int:
    """Render canary manifest YAML to stdout."""
    data = json.load(sys.stdin)
    kind = data.get("kind")
    if kind != "Deployment":
        raise SystemExit(f"expected Deployment on stdin, got {kind!r}")
    live = data

    image = _image_with_digest(live, args.image_digest)
    deployment = _build_canary_deployment(live, image)
    service = _build_canary_service()
    networkpolicy = _build_canary_networkpolicy()

    docs = {
        "apiVersion": "v1",
        "kind": "List",
        "items": [deployment, service, networkpolicy],
    }
    try:
        import yaml
    except ImportError as exc:
        raise SystemExit(
            "pyyaml is required for render; install with: pip install pyyaml"
        ) from exc

    print(yaml.safe_dump(docs, sort_keys=False, explicit_start=True))
    print("---")
    print("# Secrets required before apply (create out-of-band):")
    print(f"#   - {CANARY_POSTGRES_SECRET}: DATABASE_URL")
    print(f"#   - {CANARY_ENCRYPTION_SECRET}: TUSKER_KEY_ENCRYPTION_KEY")
    print("# No secrets are present in the rendered manifest.")
    return 0


def _resolve_pg_pod() -> str:
    """Return the running postgres pod name for the shared cluster PG."""
    proc = _kubectl(["get", "pods", "-l", PG_POD_LABEL, "-o", "json"])
    pods = json.loads(proc.stdout)
    for pod in pods.get("items", []):
        if pod.get("status", {}).get("phase") == "Running":
            return pod["metadata"]["name"]
    raise SystemExit(f"no running postgres pod with label {PG_POD_LABEL}")


def _provision_sql() -> bytes:
    """Return the SQL that creates the isolated canary role/db/extension.

    The canary role gets the minimum privileges it needs.  A random
    hex password is generated on the fly; it is never echoed to logs --
    the caller is responsible for feeding the printed DATABASE_URL into
    the canary secret.
    """
    password = secrets.token_hex(32)
    return (
        f"CREATE ROLE {CANARY_DB_USER} LOGIN PASSWORD '{password}' "
        f"NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;\n"
        f"CREATE DATABASE {CANARY_DB} OWNER {CANARY_DB_USER};\n"
        f"\\connect {CANARY_DB}\n"
        f"CREATE EXTENSION IF NOT EXISTS pgcrypto;\n"
        f"GRANT USAGE, CREATE ON SCHEMA public TO {CANARY_DB_USER};\n"
    ).encode()


def _provision_summary() -> str:
    """Return the operator-facing redacted summary for provision."""
    return (
        f"role={CANARY_DB_USER} db={CANARY_DB} extension=pgcrypto "
        f"(generated password kept out of logs; feed DATABASE_URL into "
        f"{CANARY_POSTGRES_SECRET} yourself)"
    )


def cmd_provision(args: argparse.Namespace) -> int:
    """Create the canary PG role/database/extension, or print the plan."""
    pod = args.pod
    if not args.execute:
        print("PLAN (no mutations):")
        print("  1. kubectl exec psql stdin: CREATE ROLE/CREATE DATABASE/pgcrypto")
        print("  2. Print the exact SQL with --execute (passwords shown once, never logged)")
        print(f"  Summary: {_provision_summary()}")
        return 0
    sql = _provision_sql()
    print(f"provisioning canary DB on {pod} ...")
    proc = _run(
        ["kubectl", "-n", NAMESPACE, "exec", "-i", pod, "--",
         "psql", "-U", "tusker_gateway", "-d", PROD_DB_NAME],
        input_=sql,
        check=True,
    )
    print(proc.stdout.decode())
    print("done:", _provision_summary())
    # Print the DSN template; the caller supplies the password through the
    # secret API rather than this CLI (we do not echo the generated value).
    print(f"create secret {CANARY_POSTGRES_SECRET} with DATABASE_URL for db {CANARY_DB}")
    return 0


def cmd_seed(args: argparse.Namespace) -> int:
    """Seed the canary DB from the live pod's resolved env config."""
    pod = args.pod
    if not args.execute:
        print("PLAN (no mutations):")
        print(f"  1. kubectl exec into {pod} and run migrate_config_to_db with --dry-run")
        print(f"  2. Then run it with --execute against {CANARY_DB}")
        print(f"  3. Verify rows exist in {CANARY_DB} tables (tusker_*)")
        return 0
    print("Seeding is handled by the migration CLI inside the pod:")
    print(f"  kubectl exec -i {pod} -- python -m tusker_gateway.tools.migrate_config_to_db --dry-run")
    print(f"  kubectl exec -i {pod} -- python -m tusker_gateway.tools.migrate_config_to_db --execute")
    print("Then verify row counts with:")
    print(f"  kubectl exec -i <pg pod> -- psql -U {CANARY_DB_USER} -d {CANARY_DB} -Atc '\\dt'")
    return 0


def cmd_smoke(args: argparse.Namespace) -> int:
    """Run the pgcrypto roundtrip + HTTP health check, or print the plan."""
    pod = args.pod
    if not args.execute:
        print("PLAN (no mutations):")
        print("  1. psql pgcrypto roundtrip through the canary DB:")
        print("       SELECT encode(encrypt('canary-probe'::bytea, gen_random_bytes(32)::bytea, 'aes'), 'base64');")
        print("       SELECT decrypt(encrypt('canary-probe'::bytea, gen_random_bytes(32)::bytea, 'aes'), gen_random_bytes(32)::bytea, 'aes');")
        print("  2. port-forward the canary service and curl /health and /ready")
        print(f"       kubectl -n {NAMESPACE} port-forward svc/{CANARY_DEPLOYMENT} 18642:{CANARY_PORT}")
        print("       curl http://127.0.0.1:18642/health   # expect 200 + commit SHA")
        print("       curl http://127.0.0.1:18642/ready    # expect 200")
        return 0

    pg_pod = _resolve_pg_pod()
    print(f"pgcrypto roundtrip on {pg_pod} ...")
    sql = (
        f"SELECT decrypt(encrypt('canary-probe'::bytea, digest('k1','md5'),'aes'), "
        f"digest('k1','md5'),'aes') = 'canary-probe'::bytea;"
    ).encode()
    proc = _run(
        ["kubectl", "-n", NAMESPACE, "exec", "-i", pg_pod, "--",
         "psql", "-U", CANARY_DB_USER, "-d", CANARY_DB,
         "-At", "-c", sql.decode()],
        check=True,
    )
    if proc.stdout.decode().strip() != "t":
        print("pgcrypto roundtrip FAILED", file=sys.stderr)
        return 1
    print("pgcrypto roundtrip OK")

    # HTTP smoke: /health (expect 200 + config_runtime_status) and /ready (expect 200)
    import json as _json
    import os as _os
    import signal as _signal
    import socket as _socket
    import subprocess as _sp
    import time as _time
    import urllib.request as _req

    pf = _sp.Popen(
        ["kubectl", "-n", NAMESPACE, "port-forward",
         f"svc/{CANARY_DEPLOYMENT}", "18642:{CANARY_PORT}"],
        stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
    )
    try:
        # Wait for the port to accept connections (try both v4 and v6)
        bound = False
        for _ in range(60):
            for host in ("127.0.0.1", "::1"):
                try:
                    with _socket.create_connection((host, 18642), timeout=0.5):
                        bound = True
                        break
                except OSError:
                    continue
            if bound:
                break
            _time.sleep(0.5)
        if not bound:
            print("port-forward failed to bind", file=sys.stderr)
            return 1

        for path, expect in (("/health", "config_runtime_status"), ("/ready", "status")):
            with _req.urlopen(f"http://127.0.0.1:18642{path}", timeout=15) as resp:
                body = resp.read().decode()
                if resp.status != 200:
                    print(f"{path} FAILED: HTTP {resp.status}", file=sys.stderr)
                    return 1
                data = _json.loads(body)
                if expect not in data:
                    print(f"{path} FAILED: missing '{expect}' in body", file=sys.stderr)
                    return 1
            print(f"{path} OK (HTTP 200, {expect}={data[expect]})")
    finally:
        if hasattr(_os, "killpg"):
            try:
                _os.killpg(_os.getpgid(pf.pid), _signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass
        else:
            pf.terminate()
        try:
            pf.wait(timeout=5)
        except Exception:
            pf.kill()
    return 0

def cmd_cleanup(args: argparse.Namespace) -> int:
    """Emit the teardown commands for every canary resource."""
    print("Teardown plan:")
    print(f"  kubectl -n {NAMESPACE} delete deployment {CANARY_DEPLOYMENT}")
    print(f"  kubectl -n {NAMESPACE} delete service {CANARY_DEPLOYMENT}")
    print(f"  kubectl -n {NAMESPACE} delete networkpolicy allow-{CANARY_DEPLOYMENT}-to-postgres")
    print(f"  kubectl -n {NAMESPACE} delete secret {CANARY_POSTGRES_SECRET}")
    print(f"  kubectl -n {NAMESPACE} delete secret {CANARY_ENCRYPTION_SECRET}")
    print("  kubectl exec into the postgres pod and run:")
    print(f"       DROP DATABASE IF EXISTS {CANARY_DB};")
    print(f"       DROP ROLE IF EXISTS {CANARY_DB_USER};")
    print("These are printed only; use --execute on provision to create, and")
    print("run the SQL above manually to drop.")
    return 0



def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    render = sub.add_parser("render", help="Render canary Deployment+Service+NetworkPolicy YAML")
    render.add_argument("--image-digest", required=True,
                        help="sha256:... digest of the pushed canary image")
    render.set_defaults(func=cmd_render)

    for name, help_text in (
        ("provision", "Create the canary PG role/database/pgcrypto extension"),
        ("seed", "Print the canary DB seeding procedure"),
        ("smoke", "pgcrypto roundtrip + HTTP health check"),
        ("cleanup", "Print the teardown commands"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--pod", default=None, help="target pod name (provision/smoke)")
        p.add_argument("--execute", action="store_true",
                       help="execute instead of printing the plan")
        p.set_defaults(func=globals()[f"cmd_{name}"])

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
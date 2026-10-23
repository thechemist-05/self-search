"""
Deployment security checks: published ports must bind to loopback by default.

Parses docker-compose.yml as text (no YAML dependency) so these checks run
even where Docker is not installed.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# A compose short-syntax port entry inside a "ports:" list, e.g.  - "127.0.0.1:8000:8000"
_PORT_ENTRY = re.compile(r'^\s*-\s*["\']?([0-9.:]+)["\']?\s*$')


def _published_ports(compose_text: str) -> list[str]:
    ports: list[str] = []
    in_ports = False
    for line in compose_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("ports:"):
            in_ports = True
            continue
        if in_ports:
            m = _PORT_ENTRY.match(line)
            if m:
                ports.append(m.group(1))
                continue
            if stripped and not stripped.startswith("#"):
                in_ports = False
    return ports


def test_compose_ports_bind_to_loopback_only() -> None:
    ports = _published_ports((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    assert ports == ["127.0.0.1:8000:8000", "127.0.0.1:8501:8501"]
    for entry in ports:
        assert entry.split(":")[0] == "127.0.0.1", f"{entry} is reachable from the network"


def test_parser_flags_exposed_ports() -> None:
    exposed = 'services:\n  api:\n    ports:\n      - "8000:8000"\n      - "0.0.0.0:9000:9000"\n'
    ports = _published_ports(exposed)
    assert ports == ["8000:8000", "0.0.0.0:9000:9000"]
    assert all(not p.startswith("127.0.0.1:") for p in ports)


def test_ui_container_reaches_api_over_compose_network_with_server_side_key() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "SHELFSENSE_API_URL=http://api:8000" in compose
    assert "SHELFSENSE_API_KEY=${API_KEY:-}" in compose
    assert "--server.address 0.0.0.0" in compose  # container interface; host binding stays loopback


def test_streamlit_local_config_is_loopback_without_telemetry() -> None:
    import tomllib

    cfg = tomllib.loads((ROOT / ".streamlit" / "config.toml").read_text(encoding="utf-8"))
    assert cfg["server"]["address"] == "127.0.0.1"
    assert cfg["browser"]["gatherUsageStats"] is False


def test_api_host_setting_defaults_to_loopback() -> None:
    from src.config.settings import Settings

    assert Settings.model_fields["api_host"].default == "127.0.0.1"
    assert re.search(r'api_host:\s*str\s*=\s*"127\.0\.0\.1"', (ROOT / "src/config/settings.py").read_text())

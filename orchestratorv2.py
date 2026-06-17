#!/usr/bin/env python3

import os
import json
import re
import subprocess
import ipaddress
from pathlib import Path

import requests
from dotenv import load_dotenv
from openai import OpenAI


BASE_DIR = Path("/root/ai_netmgr")
GENERATED_DIR = BASE_DIR / "generated"
GENERATED_DIR.mkdir(parents=True, exist_ok=True)

load_dotenv(BASE_DIR / "openai.env")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.5")

if not OPENAI_API_KEY:
    raise RuntimeError("Falta OPENAI_API_KEY en /root/ai_netmgr/openai.env")

NODES = {
    "h1": {
        "mgmt_ip": "10.200.0.2",
        "role": "host",
        "interfaces": {"eth1": None},
        "forwarding": False,
    },
    "h2": {
        "mgmt_ip": "10.200.0.6",
        "role": "host",
        "interfaces": {"eth1": None},
        "forwarding": False,
    },
    "r1": {
        "mgmt_ip": "10.200.0.18",
        "role": "router",
        "interfaces": {"eth1": None, "eth2": None},
        "forwarding": True,
    },
    "r2": {
        "mgmt_ip": "10.200.0.22",
        "role": "router",
        "interfaces": {"eth1": None, "eth2": None},
        "forwarding": True,
    },
    "h3": {
        "mgmt_ip": "10.200.0.10",
        "role": "host",
        "interfaces": {"eth1": None},
        "forwarding": False,
    },
    "h4": {
        "mgmt_ip": "10.200.0.14",
        "role": "host",
        "interfaces": {"eth1": None},
        "forwarding": False,
    },
}


def run(cmd, check=True, capture=False):
    print("+", " ".join(cmd))
    return subprocess.run(
        cmd,
        check=check,
        text=True,
        capture_output=capture,
    )


def ssh(mgmt_ip, remote_cmd, check=True, capture=False):
    return run(
        [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=5",
            f"root@{mgmt_ip}",
            remote_cmd,
        ],
        check=check,
        capture=capture,
    )


def scp(local_path, mgmt_ip, remote_path):
    run(
        [
            "scp",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=5",
            str(local_path),
            f"root@{mgmt_ip}:{remote_path}",
        ]
    )


def openai_generate_all_configs():

    client = OpenAI(api_key=OPENAI_API_KEY)

    prompt = """
Genera la configuración de red del plano de datos para un escenario VNX.

La respuesta debe ser exclusivamente JSON válido, sin Markdown, sin explicaciones y sin bloques ```.

Formato exacto de salida:

{
  "h1": {"interfaces_file": "..."},
  "h2": {"interfaces_file": "..."},
  "r1": {"interfaces_file": "..."},
  "r2": {"interfaces_file": "..."},
  "h3": {"interfaces_file": "..."},
  "h4": {"interfaces_file": "..."}
}

Topología del plano de datos:

- h1 y h2 están conectados a r1 en una misma LAN.
- r1 está conectado a r2 mediante un enlace punto a punto.
- h3 y h4 están conectados a r2 en una misma LAN.

Interfaces del plano de datos:

- h1: eth1
- h2: eth1
- r1: eth1 hacia la LAN de h1/h2, eth2 hacia r2
- r2: eth1 hacia r1, eth2 hacia la LAN de h3/h4
- h3: eth1
- h4: eth1

Usa exclusivamente el prefijo 10.1.0.0/24 para el plano de datos.

Divide ese prefijo en subredes CIDR válidas, alineadas correctamente y no solapadas:
- una subred para la LAN h1-h2-r1;
- una subred para el enlace punto a punto r1-r2;
- una subred para la LAN r2-h3-h4.

Asigna direcciones IP y máscaras a todas las interfaces del plano de datos.
Configura las rutas estáticas necesarias para que todos los nodos tengan conectividad extremo a extremo.
Configura el forwarding IPv4 en r1 y r2 dentro del propio interfaces_file.

No configures eth0, eth8, eth9 ni loopback.
No uses el prefijo 10.200.0.0/24.
No uses gateway.
Usa rutas estáticas con post-up ip route replace y pre-down ip route del.
"""

    system_instruction = """
Eres un generador determinista de configuraciones de red para Linux.

Debes calcular subredes válidas, no solapadas y alineadas correctamente.
Debes asignar direcciones IP, rutas estáticas y activar forwarding IPv4 en los routers.

Devuelve exclusivamente JSON válido.
No uses Markdown.
No expliques nada.
No inventes interfaces.
No uses eth0, eth8 ni eth9.
No uses gateway.
No uses direcciones fuera del prefijo indicado para el plano de datos.
"""

    response = client.responses.create(
        model=OPENAI_MODEL,
        instructions=system_instruction,
        input=prompt,
        reasoning={"effort": "medium"},
    )

    text = response.output_text.strip()

    if not text:
        raise RuntimeError("OpenAI devolvió respuesta vacía")
    #Limpieza preventiva de la respuesta
    text = (
        text
        .replace("```json", "")
        .replace("```", "")
        .strip()
    )

    try:
        return json.loads(text)

    except Exception as e:
        print("=== RESPUESTA RAW OPENAI ===")
        print(text)
        print("============================")
        raise RuntimeError(f"JSON inválido devuelto por OpenAI: {e}")





def clean_model_output(text):
    text = text.strip()
    text = text.replace("```bash", "")
    text = text.replace("```text", "")
    text = text.replace("```", "")
    return text.strip() + "\n"

def parse_interface_info(text):
    info = {}
    current_iface = None
    current_addr = None
    current_mask = None

    for raw_line in text.splitlines():
        line = raw_line.strip()

        m = re.match(r"iface\s+(\S+)\s+inet\s+static", line)
        if m:
            current_iface = m.group(1)
            current_addr = None
            current_mask = None
            continue

        if current_iface is None:
            continue

        if line.startswith("address "):
            current_addr = line.split()[1]

        if line.startswith("netmask "):
            current_mask = line.split()[1]

        if current_addr and current_mask:
            try:
                iface = ipaddress.IPv4Interface(f"{current_addr}/{current_mask}")
            except Exception as e:
                raise RuntimeError(
                    f"Dirección o máscara inválida en {current_iface}: {current_addr} {current_mask}: {e}"
                )

            info[current_iface] = iface
            current_addr = None
            current_mask = None

    return info


def validate_single_config(name, text):
    if not text.strip():
        raise RuntimeError(f"Config vacía para {name}")

    forbidden = ["eth0", "eth8", "eth9", "10.200.", "192.168.", "gateway", "ip route add"]
    for item in forbidden:
        if item in text:
            raise RuntimeError(f"Config no válida para {name}: contiene {item}")

    if "```" in text:
        raise RuntimeError(f"Config inválida para {name}: contiene Markdown")

    expected_ifaces = set(NODES[name]["interfaces"].keys())
    found_ifaces = set(re.findall(r"^\s*iface\s+(\S+)\s+inet\s+static", text, flags=re.M))

    if found_ifaces != expected_ifaces:
        raise RuntimeError(
            f"Interfaces incorrectas para {name}. Esperaba {expected_ifaces}, recibí {found_ifaces}"
        )

    parsed = parse_interface_info(text)

    data_prefix = ipaddress.IPv4Network("10.1.0.0/24")

    for iface_name, iface in parsed.items():
        if iface.ip not in data_prefix:
            raise RuntimeError(
                f"IP fuera del plano de datos en {name}:{iface_name}: {iface.ip}"
            )

    if NODES[name].get("forwarding"):
        if "net.ipv4.ip_forward=1" not in text:
            raise RuntimeError(f"Falta forwarding IPv4 en router {name}")
    else:
        if "net.ipv4.ip_forward" in text:
            raise RuntimeError(f"{name} no es router y no debe activar forwarding")

    return parsed


def validate_topology_networks(parsed):
    lan_left = {
        parsed["h1"]["eth1"].network,
        parsed["h2"]["eth1"].network,
        parsed["r1"]["eth1"].network,
    }

    link_r1_r2 = {
        parsed["r1"]["eth2"].network,
        parsed["r2"]["eth1"].network,
    }

    lan_right = {
        parsed["r2"]["eth2"].network,
        parsed["h3"]["eth1"].network,
        parsed["h4"]["eth1"].network,
    }

    if len(lan_left) != 1:
        raise RuntimeError(f"h1, h2 y r1 eth1 no están en la misma subred: {lan_left}")

    if len(link_r1_r2) != 1:
        raise RuntimeError(f"r1 eth2 y r2 eth1 no están en la misma subred: {link_r1_r2}")

    if len(lan_right) != 1:
        raise RuntimeError(f"r2 eth2, h3 y h4 no están en la misma subred: {lan_right}")

    networks = [next(iter(lan_left)), next(iter(link_r1_r2)), next(iter(lan_right))]

    if len(set(networks)) != 3:
        raise RuntimeError(f"Las tres partes de la topología no usan subredes distintas: {networks}")

    for i, net_a in enumerate(networks):
        for net_b in networks[i + 1:]:
            if net_a.overlaps(net_b):
                raise RuntimeError(f"Subredes solapadas: {net_a} y {net_b}")



def generate_all():
    print("=== Generando TODAS las configs con un único request a OpenAI ===")

    configs = openai_generate_all_configs()

    expected_nodes = {"h1", "h2", "r1", "r2", "h3", "h4"}
    received_nodes = set(configs.keys())

    if received_nodes != expected_nodes:
        raise RuntimeError(
            f"Respuesta inesperada de OpenAI. Esperaba {expected_nodes}, recibí {received_nodes}"
        )

    parsed = {}
    cleaned_configs = {}

    for name in expected_nodes:
        interfaces_content = configs[name].get("interfaces_file", "")

        parsed[name] = validate_single_config(name, interfaces_content)
        cleaned_configs[name] = interfaces_content.strip() + "\n"

    validate_topology_networks(parsed)

    for name in expected_nodes:
        out = GENERATED_DIR / f"{name}-99-vnx-data.cfg"
        out.write_text(cleaned_configs[name])
        print(f"[OK] {name}: {out}")


def check_mgmt():
    print("=== Comprobando SSH por red de gestión ===")

    ok = True

    for name, spec in NODES.items():
        mgmt_ip = spec["mgmt_ip"]
        print(f"\n--- {name} {mgmt_ip} ---")

        result = ssh(
            mgmt_ip,
            "hostname && ip -br addr && ip route",
            check=False,
            capture=True,
        )

        if result.returncode == 0:
            print(result.stdout)

        else:
            ok = False
            print("[ERROR] No accesible por SSH")
            print(result.stderr)

    return ok


def push_all():
    print("=== Copiando configs por SCP ===")

    for name, spec in NODES.items():
        mgmt_ip = spec["mgmt_ip"]

        local_cfg = GENERATED_DIR / f"{name}-99-vnx-data.cfg"
        if not local_cfg.exists():
            raise RuntimeError(f"No existe {local_cfg}. Ejecuta primero --generate")

        scp(local_cfg, mgmt_ip, "/tmp/99-vnx-data.cfg")

        #if spec.get("forwarding"):
        #    local_sysctl = GENERATED_DIR / f"{name}-99-vnx-forwarding.conf"
         #   scp(local_sysctl, mgmt_ip, "/tmp/99-vnx-forwarding.conf")


def apply_all():
    print("=== Aplicando configs en máquinas remotas ===")

    for name, spec in NODES.items():
        mgmt_ip = spec["mgmt_ip"]
        data_interfaces = " ".join(spec["interfaces"].keys())

        remote_cmd = f"""
set -e

echo "[{name}] Backup configs"
cp -a /etc/network/interfaces /etc/network/interfaces.bak.$(date +%s) || true

mkdir -p /etc/network/interfaces.d
cp /tmp/99-vnx-data.cfg /etc/network/interfaces.d/99-vnx-data.cfg

grep -q '^source /etc/network/interfaces.d/\\*' /etc/network/interfaces || \\
  printf '\\nsource /etc/network/interfaces.d/*\\n' >> /etc/network/interfaces


echo "[{name}] Applying data interfaces: {data_interfaces}"

for iface in {data_interfaces}; do
  ip link set "$iface" up || true
  ifdown "$iface" 2>/dev/null || true
  ifup "$iface" || true
done

echo "[{name}] State after apply"
ip -br addr
ip route
"""

        ssh(mgmt_ip, remote_cmd)


def verify_data_plane():
    print("=== Verificando plano de datos ===")

    parsed = {}

    for name in NODES.keys():
        cfg_path = GENERATED_DIR / f"{name}-99-vnx-data.cfg"

        if not cfg_path.exists():
            raise RuntimeError(f"No existe {cfg_path}. Ejecuta primero --generate")

        parsed[name] = parse_interface_info(cfg_path.read_text())

    tests = [
        ("h1", str(parsed["r1"]["eth1"].ip)),  # h1 -> r1
        ("h1", str(parsed["h2"]["eth1"].ip)),  # h1 -> h2
        ("h1", str(parsed["h3"]["eth1"].ip)),  # h1 -> h3
        ("h1", str(parsed["h4"]["eth1"].ip)),  # h1 -> h4
        ("h3", str(parsed["r2"]["eth2"].ip)),  # h3 -> r2
        ("h3", str(parsed["h1"]["eth1"].ip)),  # h3 -> h1
        ("h4", str(parsed["h2"]["eth1"].ip)),  # h4 -> h2
        ("r1", str(parsed["r2"]["eth1"].ip)),  # r1 -> r2
        ("r2", str(parsed["r1"]["eth2"].ip)),  # r2 -> r1
    ]

    failed = 0

    for src, dst_ip in tests:
        mgmt_ip = NODES[src]["mgmt_ip"]
        print(f"\n--- {src} ping {dst_ip} ---")

        result = ssh(
            mgmt_ip,
            f"ping -c 2 -W 2 {dst_ip}",
            check=False,
            capture=True,
        )

        if result.returncode == 0:
            print("[OK]")
            print(result.stdout)
        else:
            failed += 1
            print("[FAIL]")
            print(result.stdout)
            print(result.stderr)

    if failed:
        print(f"\n[RESULTADO] Fallaron {failed} pruebas")
        return False

    print("\n[RESULTADO] Plano de datos OK")
    return True


def main():
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--check-mgmt", action="store_true")
    parser.add_argument("--generate", action="store_true")
    parser.add_argument("--push", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--all", action="store_true")

    args = parser.parse_args()

    if args.all:
        if not check_mgmt():
            raise SystemExit("Falla la red de gestión, no sigo.")
        generate_all()
        push_all()
        apply_all()
        verify_data_plane()
        return

    if args.check_mgmt:
        check_mgmt()

    if args.generate:
        generate_all()

    if args.push:
        push_all()

    if args.apply:
        apply_all()

    if args.verify:
        verify_data_plane()


if __name__ == "__main__":
    main()
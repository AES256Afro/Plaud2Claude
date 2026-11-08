"""One-time provisioning for a PLAUD NotePin S (or Note Pro) with an ordinary Plaud account.

Run this yourself in a terminal; it asks for your Plaud e-mail and password locally (or takes
a token with --token) and never prints secrets. It writes identity.json beside this script with:
  - the Plaud API token and its user id (the BLE handshake token is derived from it),
  - the device serial (from a Bluetooth scan, or --serial),
  - the serial signature from POST /device/sn-sign (the device's authorization gate),
  - the account's E2EE private key from POST /e2ee/gen_key (decrypts recordings),
  - a locally generated RSA-2048 pair used for the Bluetooth session key exchange.
Only the three API calls touch Plaud's cloud; everything afterwards (collect.py) is local.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import getpass
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
IDENTITY = HERE / "identity.json"
REGIONS = {"us": "https://api.plaud.ai", "eu": "https://api-euc1.plaud.ai"}
APSE1 = "https://api-apse1.plaud.ai"
COMPANY_REALTEK = 0x005D


def redact(value: str) -> str:
    return f"<{len(value)} chars>" if value else "<empty>"


def jwt_claims(token: str) -> dict:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


# ---------------------------------------------------------------- Plaud cloud

def login(email: str, password: str, region: str) -> tuple[str, str]:
    """POST /auth/access-token with region redirect handling. Returns (token, api_base)."""
    import requests
    base = REGIONS[region]
    for _ in range(3):
        r = requests.post(f"{base}/auth/access-token", data={"username": email, "password": password},
                          headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=30)
        data = r.json()
        if data.get("status") == -302 and data.get("data", {}).get("domains", {}).get("api"):
            base = data["data"]["domains"]["api"].rstrip("/")
            if not base.startswith("http"):
                base = "https://" + base
            print(f"  Plaud says this account lives at {base}; retrying there")
            continue
        if data.get("status") == 0 and data.get("access_token"):
            return data["access_token"], base
        raise SystemExit(f"Login failed: {data.get('msg') or data}")
    raise SystemExit("Login failed: too many region redirects")


def check_token(token: str, base: str) -> dict:
    import requests
    r = requests.get(f"{base}/user/me", headers={"Authorization": f"bearer {token}"}, timeout=30)
    data = r.json()
    if data.get("status") != 0:
        raise SystemExit(f"Token rejected by {base}/user/me: {data.get('msg') or data}")
    return data.get("data") or {}


def device_list(token: str, base: str) -> list[dict]:
    import requests
    r = requests.get(f"{base}/device/list", headers={"Authorization": f"bearer {token}"}, timeout=30)
    data = r.json()
    items = data.get("data") or []
    if isinstance(items, dict):
        items = items.get("list", [])
    return items if isinstance(items, list) else []


def sn_sign(token: str, base: str, serial: str, public_key_pem: str | None) -> str:
    import requests
    body: dict = {"sns": [serial]}
    if public_key_pem:
        body["public_key_base64"] = "".join(
            line for line in public_key_pem.splitlines() if "PUBLIC KEY" not in line)
    r = requests.post(f"{base}/device/sn-sign", json=body,
                      headers={"Authorization": f"bearer {token}"}, timeout=30)
    data = r.json()
    sig = (data.get("data") or {}).get("signature", {}).get(serial)
    if data.get("status") != 0 or not sig:
        raise SystemExit(f"sn-sign failed: {data.get('msg') or data}")
    return sig


def e2ee_key(token: str, base: str) -> str:
    import requests
    last = None
    for b in (base, APSE1):
        r = requests.post(f"{b}/e2ee/gen_key", json={}, headers={"Authorization": f"Bearer {token}"}, timeout=30)
        try:
            data = r.json()
        except ValueError:
            last = f"{b}: HTTP {r.status_code}"
            continue
        pem_b64 = (data.get("data") or {}).get("private_key_base64")
        if pem_b64:
            return base64.b64decode(pem_b64).decode()
        last = f"{b}: {data.get('msg') or data}"
    raise SystemExit(f"e2ee/gen_key failed: {last}")


# ---------------------------------------------------------------- device serial over BLE

def serial_from_manufacturer(data: bytes) -> str | None:
    """Serial from Plaud manufacturer data (company id already stripped, as bleak does on Windows).
    NotePin S: 04 <4 bytes> 08 <8-byte serial> ... ; Note Pro: 02 <project u16> 04 <4 bytes> 08 <serial>."""
    if len(data) >= 14 and data[0] == 0x04 and data[5] == 0x08:
        return data[6:14].hex().upper()
    if len(data) >= 17 and data[0] == 0x02 and data[8] == 0x08:
        return data[9:17].hex().upper()
    return None


async def scan_serial(seconds: float) -> tuple[str, str] | None:
    from bleak import BleakScanner
    found: dict[str, tuple[str, str]] = {}

    def cb(d, adv):
        name = d.name or adv.local_name or ""
        mfr = adv.manufacturer_data.get(COMPANY_REALTEK)
        if "plaud" in name.lower() and mfr:
            serial = serial_from_manufacturer(bytes(mfr))
            if serial:
                found[serial] = (name, d.address)

    scanner = BleakScanner(cb)
    await scanner.start()
    await asyncio.sleep(seconds)
    await scanner.stop()
    if not found:
        return None
    if len(found) > 1:
        print("  Several Plaud devices seen:")
        for i, (serial, (name, addr)) in enumerate(found.items()):
            print(f"    [{i}] {name}  {addr}  serial ...{serial[-4:]}")
        idx = int(input("  Which one? [0] ") or "0")
        serial = list(found)[idx]
    else:
        serial = next(iter(found))
    return serial, found[serial][0]


# ---------------------------------------------------------------- main

def restrict(path: Path) -> None:
    if os.name == "nt":
        user = os.environ.get("USERNAME", "")
        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"], capture_output=True)
    else:
        path.chmod(0o600)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--token", help="Plaud API token (skips the e-mail/password login)")
    ap.add_argument("--region", choices=REGIONS, default="us", help="where to try the login first")
    ap.add_argument("--serial", help="device serial, 16 hex characters (skips the Bluetooth scan)")
    ap.add_argument("--scan-seconds", type=float, default=15)
    ap.add_argument("--sign-with-key", action="store_true",
                    help="also send the new Bluetooth public key to sn-sign (try if the handshake is refused)")
    ap.add_argument("--out", type=Path, default=IDENTITY)
    args = ap.parse_args()

    print("1. Plaud account")
    if args.token:
        token = args.token.strip()
        for prefix in ("Bearer ", "bearer "):
            if token.startswith(prefix):
                token = token[len(prefix):]
        base = REGIONS[args.region]
        try:
            check_token(token, base)
        except SystemExit:
            base = REGIONS["eu" if args.region == "us" else "us"]
            check_token(token, base)
    else:
        email = input("   Plaud e-mail: ").strip()
        password = getpass.getpass("   Plaud password (not echoed): ")
        token, base = login(email, password, args.region)
    claims = jwt_claims(token)
    user_id = claims.get("user_id") or claims.get("sub") or ""
    exp = claims.get("exp")
    expires = time.strftime("%Y-%m-%d", time.localtime(exp)) if exp else "unknown"
    print(f"   signed in at {base}; user id {redact(user_id)}; token expires {expires}")

    print("2. Device serial")
    if args.serial:
        serial, name = args.serial.upper(), "(given)"
    else:
        print(f"   scanning Bluetooth for {args.scan_seconds:.0f}s (wake the pin by pressing its button)")
        got = asyncio.run(scan_serial(args.scan_seconds))
        if not got:
            raise SystemExit("   no Plaud device with manufacturer data seen; pass --serial or move closer")
        serial, name = got
    print(f"   {name}: serial ...{serial[-4:]}")
    try:
        devices = device_list(token, base)
        serials = {str(d.get("sn") or d.get("serial") or d.get("serial_number") or "").upper() for d in devices}
        if serial in serials:
            print("   this serial is registered to your Plaud account")
        else:
            fields = sorted({k for d in devices for k in d})[:8]
            print(f"   warning: your account lists {len(devices)} device(s) and none matches this serial (fields seen: {fields})")
    except Exception as err:  # the check is advisory
        print(f"   could not read /device/list ({type(err).__name__}); continuing")

    print("3. Bluetooth key pair (local)")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                 serialization.NoEncryption()).decode()
    pub_pem = key.public_key().public_bytes(serialization.Encoding.PEM,
                                            serialization.PublicFormat.SubjectPublicKeyInfo).decode()

    print("4. Serial signature from Plaud (POST /device/sn-sign)")
    signature = sn_sign(token, base, serial, pub_pem if args.sign_with_key else None)
    print(f"   got signature {redact(signature)}")

    print("5. Account decryption key (POST /e2ee/gen_key)")
    old = {}
    if args.out.exists():
        old = json.loads(args.out.read_text())
        backup = args.out.with_name(f"{args.out.name}.bak.{int(time.time())}")
        backup.write_text(json.dumps(old, indent=2))
        restrict(backup)
        print(f"   previous identity backed up to {backup.name}")
    e2ee_pem = e2ee_key(token, base)
    if old.get("e2ee_private_key_pem") and old["e2ee_private_key_pem"] != e2ee_pem:
        print("   WARNING: Plaud returned a different decryption key than last time; the old one is in the backup")
    print(f"   got key {redact(e2ee_pem)}")

    identity = {
        "api_base": base, "api_token": token, "user_id": user_id, "device_name": name, "device_sn": serial,
        "sn_signature_b64": signature, "transport_private_key_pem": priv_pem, "transport_public_key_pem": pub_pem,
        "e2ee_private_key_pem": e2ee_pem, "signed_with_key": bool(args.sign_with_key),
        "provisioned_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    args.out.write_text(json.dumps(identity, indent=2))
    restrict(args.out)
    print(f"\nWrote {args.out} (readable only by {os.environ.get('USERNAME') or 'you'}).")
    print("Next: python collect.py probe  (Windows: collect.cmd probe)")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)

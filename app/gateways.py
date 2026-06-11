"""SIP trunk gateways generated from DB profiles (Plan.md §3, §8 — stage 5).

The web UI stores SIP trunks in `sip_profile`, but FreeSWITCH dials through a
`gateway` in the external sofia profile. This module materializes a DB profile
into `fs/sip_profiles/external/gw_profile_<id>.xml` and rescans sofia so the
campaign can dial `sofia/gateway/gw_profile_<id>/<number>`.

Without this the only gateway is the static `flysip` from .env; a profile added
in the UI would never be used and every call fails with
NORMAL_TEMPORARY_FAILURE (the gateway is unavailable).

SECURITY: the trunk password is written ONLY into the generated XML (gitignored
volume) — never logged, never returned by the API.
"""

import logging
import os
from xml.sax.saxutils import escape

logger = logging.getLogger(__name__)

FS_CONF_DIR = os.environ.get("FS_CONF_DIR", "/app/fs")


def _attr(value):
    return '"' + escape(str(value), {'"': "&quot;"}) + '"'


def gateway_name(profile_id):
    return f"gw_profile_{int(profile_id)}"


def gateway_path(name):
    return os.path.join(FS_CONF_DIR, "sip_profiles", "external", f"{name}.xml")


def gateway_xml(name, server, port, username, password):
    """Register-mode gateway for a DB trunk profile."""
    proxy = f"{server}:{int(port)}"
    return f"""<include>
  <gateway name={_attr(name)}>
    <param name="proxy" value={_attr(proxy)}/>
    <param name="realm" value={_attr(server)}/>
    <param name="username" value={_attr(username)}/>
    <param name="from-user" value={_attr(username)}/>
    <param name="from-domain" value={_attr(server)}/>
    <param name="password" value={_attr(password)}/>
    <param name="register" value="true"/>
    <param name="register-transport" value="udp"/>
    <param name="expire-seconds" value="600"/>
    <param name="retry-seconds" value="30"/>
    <param name="caller-id-in-from" value="true"/>
    <param name="ping" value="30"/>
  </gateway>
</include>
"""


def write_gateway(profile):
    """Write the gateway XML for a profile row. Returns the gateway name."""
    name = gateway_name(profile["id"])
    path = gateway_path(name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(gateway_xml(name, profile["server"], profile["port"],
                            profile["username"], profile["password"]))
    logger.info("Wrote gateway %s for profile %s (%s)",
                name, profile["id"], profile["server"])
    return name


def remove_gateway(profile_id):
    try:
        os.remove(gateway_path(gateway_name(profile_id)))
    except FileNotFoundError:
        pass


async def gateway_state(client, name):
    """REGED / NOREG / TRYING / FAIL... or '' if the gateway is unknown."""
    body = (await client.api(f"sofia status gateway external::{name}")).strip()
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("State") and "\t" in line:
            return line.split("\t", 1)[1].strip()
        if line.lower().startswith("state"):
            parts = line.split(None, 1)
            if len(parts) == 2:
                return parts[1].strip()
    return ""


async def ensure_gateway(client, profile, wait_registered=10.0):
    """Generate the gateway, rescan sofia, and (best-effort) wait for REGED.

    Returns (gateway_name, state). A trunk that authenticates by IP (no
    registration) stays NOREG yet still places calls — so this never raises;
    the worker dials regardless and maps the real outcome.
    """
    import asyncio
    name = write_gateway(profile)
    await client.api("reloadxml")
    await client.api("sofia profile external rescan")
    state = ""
    waited = 0.0
    while waited < wait_registered:
        state = await gateway_state(client, name)
        if state in ("REGED", "FAILED"):
            break
        await asyncio.sleep(1.0)
        waited += 1.0
    logger.info("gateway %s state=%s", name, state or "?")
    return name, state

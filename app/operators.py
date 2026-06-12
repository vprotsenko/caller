"""Operator extensions: directory XML generation, live registration
checks, free-operator pool for bridge nodes.

The controller writes one fs/directory/default/<ext>.xml per operator and
runs `reloadxml`; softphones register against the internal profile.

Registration is checked live with `sofia_contact <ext>@<domain>` instead of
tracking sofia::register events: the query can't drift from reality and
survives ESL reconnects.

SECURITY: operator SIP passwords are written ONLY into the generated XML
(volume, gitignored) and the DB — never logged, never returned by the API.
"""

import logging
import os
import re
from xml.sax.saxutils import escape

from . import db

logger = logging.getLogger(__name__)

FS_CONF_DIR = os.environ.get("FS_CONF_DIR", "/app/fs")

EXTENSION_RE = re.compile(r"^\d{3,6}$")  # 1001..1009 recommended


def _attr(value):
    """Double-quoted XML attribute with deterministic escaping."""
    return '"' + escape(value, {'"': "&quot;"}) + '"'


def extension_xml(extension, password):
    """Directory entry for one operator softphone."""
    return f"""<include>
  <user id={_attr(extension)}>
    <params>
      <param name="password" value={_attr(password)}/>
    </params>
    <variables>
      <variable name="user_context" value="default"/>
    </variables>
  </user>
</include>
"""


def extension_path(extension):
    return os.path.join(FS_CONF_DIR, "directory", "default", f"{extension}.xml")


def write_extension(extension, password):
    path = extension_path(extension)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(extension_xml(extension, password))
    logger.info("Wrote directory entry for extension %s", extension)


def remove_extension(extension):
    try:
        os.remove(extension_path(extension))
        logger.info("Removed directory entry for extension %s", extension)
    except FileNotFoundError:
        pass


# --- live registration state -----------------------------------------------------

_domain_cache = None


async def get_domain(client):
    """FreeSWITCH's default domain ($${domain} = local IP), cached."""
    global _domain_cache
    if not _domain_cache:
        _domain_cache = (await client.api("global_getvar domain")).strip()
    return _domain_cache


async def is_registered(client, extension):
    domain = await get_domain(client)
    body = (await client.api(f"sofia_contact {extension}@{domain}")).strip()
    return bool(body) and not body.startswith("error/")


async def reloadxml(client):
    return (await client.api("reloadxml")).strip()


class OperatorPool:
    """Free-operator bookkeeping for one running campaign:
    free = enabled + registered (live sofia_contact) + not in a bridge."""

    def __init__(self, client):
        self._client = client
        self._busy = set()
        self._domain = None

    async def acquire(self):
        """Reserve the first free registered operator; None if nobody is."""
        for op in db.list_operators(enabled_only=True):
            ext = op["extension"]
            if ext in self._busy:
                continue
            if await is_registered(self._client, ext):
                self._domain = await get_domain(self._client)
                self._busy.add(ext)
                return ext
        return None

    def release(self, extension):
        self._busy.discard(extension)

    def dial_target(self, extension):
        return f"user/{extension}@{self._domain}"

    def busy_extensions(self):
        return set(self._busy)

    async def free_count(self):
        """Operators able to take a call right now (pacing)."""
        n = 0
        for op in db.list_operators(enabled_only=True):
            if op["extension"] in self._busy:
                continue
            if await is_registered(self._client, op["extension"]):
                n += 1
        return n

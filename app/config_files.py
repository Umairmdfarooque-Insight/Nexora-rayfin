"""Where a committed `config/*.yaml` file is read from at run time.

`deploy_home` (T-005) publishes every `config/*.yaml` to `Files/config/` of the
control Lakehouse `nexora_control`. A notebook job has that Lakehouse reachable
through OneLake, so it should read the published copy there instead of a local
checkout it may not have; a User Data Function and local development (including
every offline check, which never sets the home-workspace settings) keep reading
the local `config/` folder unchanged, through each loader's own `CONFIG_PATH`.
"""

from app.config import NEXORA_CONTROL_LAKEHOUSE_ID, NEXORA_HOME_WORKSPACE_ID
from app.runtime import RUNTIME


def from_fabric() -> bool:
    """Whether a config file should come from Files/config/ of nexora_control
    instead of the local config/ directory: only a Fabric job with the home
    workspace and control Lakehouse configured."""
    return RUNTIME == "fabric_job" and bool(NEXORA_HOME_WORKSPACE_ID) and bool(NEXORA_CONTROL_LAKEHOUSE_ID)


def read_text(name: str) -> str:
    """The text of Files/config/<name> of nexora_control. Raises on anything
    the OneLake Files connector raises (a missing file, a network error, …);
    callers wrap this in their own config-file error."""
    from app.connectors.onelake_files import read_file

    data = read_file(NEXORA_HOME_WORKSPACE_ID, NEXORA_CONTROL_LAKEHOUSE_ID, f"config/{name}")
    return data.decode("utf-8")

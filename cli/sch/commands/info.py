"""`sch info`: show resolved runtime/region."""

from .. import config as config_mod
from .. import userenv


def cmd_info(cfg, args):
    print("region      : {}".format(cfg.region))
    print("runtime ARN : {}".format(config_mod.runtime_arn(cfg)))
    print("workspaces  : {}".format(cfg.ws_dir))
    print("default harness (new workspaces): {}".format(cfg.default_harness))
    print("default storage (new workspaces): {}".format(cfg.default_storage))
    print("workspace registry: {}".format(cfg.workspace_registry_url or "disabled (local index mode)"))
    keys = cfg.provider_keys
    env_path = userenv.user_env_path()
    if keys:
        print("provider keys: {} (in {})".format(
            ", ".join(sorted(keys)), env_path
        ))
    else:
        print(
            "provider keys: none — create {} (chmod 600) to add "
            "external providers".format(env_path)
        )
    return 0

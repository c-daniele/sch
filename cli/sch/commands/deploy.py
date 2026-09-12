"""`sch deploy`: run the support repo's deploy script from anywhere.

The deployment tooling (CloudFormation templates, the image build context)
deliberately lives in the support repo, not in the wheel — but that is an
implementation detail no operator should have to know. Before this command the
only way to deploy was to `cd` into the managed checkout
(`~/.local/share/sch/repo`) and run `./infra/deploy.sh` by hand, which is why
`sch setup` had to print a two-line incantation with a path in it.

This is a thin, honest pass-through: every option belongs to `infra/deploy.sh`
(`-r`, `-p`, `-e`, `-v`, `-s`, `-l`, `-c`, `-h`) and the environment is
inherited, so feature switches like `ENABLE_TELEGRAM_INTERACTION=true sch
deploy` keep working exactly as documented for the script.
"""

import subprocess
import sys

from .. import repo


def cmd_deploy(cfg, args):
    if args and args[0] in ("--help",):
        # -h is the script's own help; --help is the pythonic spelling of it.
        args = ["-h"] + list(args[1:])

    root = repo.repo_root()
    if root is None:
        print(
            "sch: deploy requires the SCH support repo (deploy script and"
            " CloudFormation templates), but no checkout was found; run:\n"
            "  sch setup",
            file=sys.stderr,
        )
        return 1

    script = root / "infra" / "deploy.sh"
    if not script.is_file():
        print(
            "sch: support repo at '{}' has no infra/deploy.sh — layout"
            " unexpected".format(root),
            file=sys.stderr,
        )
        return 1

    if sys.platform == "win32":
        # The script needs a bash host; saying so beats a cryptic exec failure.
        print(
            "sch: the deploy script requires bash — run it from WSL or Git"
            " Bash:\n"
            "  bash '{}' {}".format(script, " ".join(args)).rstrip(),
            file=sys.stderr,
        )
        return 1

    result = subprocess.run(["bash", str(script), *args])
    return result.returncode

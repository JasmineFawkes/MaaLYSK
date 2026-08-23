import os
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")  # pyright: ignore[reportAttributeAccessIssue]
except AttributeError:
    pass

current_file_path = os.path.abspath(__file__)
agent_dir = os.path.dirname(current_file_path)
project_root_dir = os.path.dirname(agent_dir)

if os.getcwd() != project_root_dir:
    os.chdir(project_root_dir)

if agent_dir not in sys.path:
    sys.path.insert(0, agent_dir)

from deploy.deploy import deploy  # noqa: E402


def run_agent() -> int:
    from maa.agent.agent_server import AgentServer
    from maa.toolkit import Toolkit

    import custom

    custom.register_all()
    Toolkit.init_option("./")

    socket_id = sys.argv[-1]
    print(f"socket_id: {socket_id}")

    AgentServer.start_up(socket_id)
    AgentServer.join()
    AgentServer.shut_down()
    return 0


def main() -> int:
    # Development projects keep interface.json under assets.
    project_root = Path(project_root_dir)
    is_dev_mode = not (project_root / "interface.json").is_file() and (
        project_root / "assets" / "interface.json"
    ).is_file()

    if not deploy():
        print("error: deployment check failed", file=sys.stderr)
        return 1

    if is_dev_mode:
        os.chdir(project_root / "assets")

    return run_agent()


if __name__ == "__main__":
    sys.exit(main())

"""Run the local platform agent from the command line."""

import argparse

from apps.cli.wiring import build_cli_services


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a Weaves agent with the configured local model profile"
    )
    parser.add_argument("task", help="task for the local assistant")
    args = parser.parse_args()

    services = build_cli_services()
    try:
        result = services.run_agent(args.task)
        print(result.artifact.model_dump_json(indent=2))
    finally:
        services.close()


if __name__ == "__main__":
    main()

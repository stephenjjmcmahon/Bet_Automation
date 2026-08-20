import sys

from backend.eval import harness, parse_audit

COMMANDS = {
    "match": harness.report,
    "parses": parse_audit.report,
}


def main(argv: list) -> int:
    if len(argv) != 1 or argv[0] not in COMMANDS:
        print(f"usage: python -m backend.eval {{{'|'.join(COMMANDS)}}}")
        return 2
    return COMMANDS[argv[0]]()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

"""Greeting module: prints a hello message."""

from __future__ import annotations


def greet(name: str) -> None:
    """Print a greeting message for the given name.

    Args:
        name: The name to greet.
    """
    message = "Hello, " + name
    print(message)


def main() -> None:
    """Run the greeting demo."""
    greet("Claude")


if __name__ == "__main__":
    main()

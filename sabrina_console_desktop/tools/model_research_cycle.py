"""Compatibility entrypoint for Sabrina's durable autonomous research worker."""
from tools.autonomous_research import ResearchDirector, main


def run_cycle():
    """Advance a durable experiment without blocking on lab inference."""
    return ResearchDirector().step()


if __name__ == "__main__":
    main()

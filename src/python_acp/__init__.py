"""python-acp package."""

__all__ = ["__version__"]

#: The agent's version, reported on the wire as `initialize`'s `agentInfo.version`
#: (`agent.py`). **It is a literal, and `pyproject.toml` holds the other copy.**
#:
#: A literal rather than `importlib.metadata.version(...)` so that importing this package
#: never depends on it having been installed — but that leaves two numbers that can drift,
#: and `pyacp-xzo` is what happens when they do: `0.2.0` in `pyproject.toml`, `0.1.0` here,
#: and a wheel whose agent introduced itself as the previous release.
#:
#: So the two are bound by `tests/test_version.py`, which compares this against the
#: installed distribution's metadata. Bump both, in the same commit.
__version__ = "0.2.0"

"""A scheduled pass waits for the network rather than failing into a hole."""

from __future__ import annotations

import socket

from defi_tracker import netwait


def test_one_resolvable_host_is_enough(monkeypatch):
    def gai(host, port):
        if host == "b.example":
            return [()]
        raise socket.gaierror("nope")

    monkeypatch.setattr(netwait.socket, "getaddrinfo", gai)
    assert netwait.network_up(["a.example", "b.example"])
    assert not netwait.network_up(["a.example"])


def test_waits_until_the_network_comes_up(monkeypatch):
    state = {"up_after": 3, "calls": 0}

    def up(hosts=None):
        state["calls"] += 1
        return state["calls"] >= state["up_after"]

    monkeypatch.setattr(netwait, "network_up", up)
    slept: list[float] = []
    assert netwait.wait_for_network(timeout_s=100, interval_s=10, sleep=slept.append,
                                    clock=lambda: len(slept) * 10.0)  # fmt: skip
    assert slept == [10, 10]


def test_gives_up_after_the_timeout(monkeypatch):
    monkeypatch.setattr(netwait, "network_up", lambda hosts=None: False)
    slept: list[float] = []
    assert not netwait.wait_for_network(timeout_s=30, interval_s=10, sleep=slept.append,
                                        clock=lambda: len(slept) * 10.0)  # fmt: skip
    assert len(slept) == 3


def test_probes_never_include_a_url_path(monkeypatch):
    monkeypatch.setenv("ALCHEMY_API_KEY", "s3cret")
    assert all("s3cret" not in h and "/" not in h for h in netwait.probe_hosts())

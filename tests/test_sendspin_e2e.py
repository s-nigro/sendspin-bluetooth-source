"""End-to-end: our source client against a real aiosendspin server (as Music Assistant uses it).

Covers pairing with the dynamic pairing code, line-sense signal reporting, server-driven
start, and audio arriving at the server in the announced format.
"""

import asyncio
import socket

import numpy as np
import pytest
from aiosendspin.models.types import PairingCodeFormat, PairMethod, SignalState
from aiosendspin.noise import Identity, InMemoryServerPairingStore
from aiosendspin.noise.pairing import PairingAttempt
from aiosendspin.server import SendspinServer
from aiosendspin.server.roles.source.events import (
    SourceSignalChangedEvent,
    SourceStreamStartedEvent,
)

from btsource.config import Options
from btsource.sendspin_link import SendspinLink


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _wait_for(pred, timeout=10.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError("condition not met")
        await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_pair_signal_and_stream(tmp_path):
    loop = asyncio.get_running_loop()
    server = SendspinServer(
        loop, Identity.generate(), "Test MA", pairing_store=InMemoryServerPairingStore()
    )
    server_port = _free_port()
    await server.start_server(port=server_port, host="127.0.0.1", discover_clients=False)

    code_future: asyncio.Future[str] = loop.create_future()

    async def on_code(raw, grouped):
        if raw and not code_future.done():
            code_future.set_result(raw)

    opts = Options(device_name="Test BT", listen_port=_free_port())
    link = SendspinLink(opts, tmp_path, on_pairing_code=on_code, advertise_mdns=False)
    await link.start()

    events = []
    try:
        attempt = PairingAttempt(
            method=PairMethod.DYNAMIC_PAIRING_CODE,
            pairing_code_provider=lambda: asyncio.wait_for(asyncio.shield(code_future), 10),
            pairing_format=PairingCodeFormat.DIGITS,
        )
        await asyncio.wait_for(
            server.connect_to_client_and_wait(
                f"ws://127.0.0.1:{opts.listen_port}/sendspin", pairing_attempt=attempt
            ),
            20,
        )
        client_id = link.client.identity.peer_id

        def source_role():
            c = server.get_client(client_id)
            return c.role("source@v1") if c is not None else None

        await _wait_for(lambda: source_role() is not None and server.get_client(client_id).is_paired, 20)
        server_client = server.get_client(client_id)
        server_client.add_event_listener(lambda _c, ev: events.append(ev))

        # line sense: signal goes present
        link.set_signal(True)
        await _wait_for(
            lambda: any(isinstance(e, SourceSignalChangedEvent) and e.signal is SignalState.PRESENT for e in events)
        )

        # server asks for audio -> client opens the stream
        await _wait_for(lambda: source_role().can_start)
        source_role().request_start()
        await _wait_for(lambda: any(isinstance(e, SourceStreamStartedEvent) for e in events))
        started = next(e for e in events if isinstance(e, SourceStreamStartedEvent))
        assert started.audio_format.sample_rate == 48000
        assert started.audio_format.channels == 2
        await _wait_for(lambda: link.streaming)

        # 200 ms of a 440 Hz tone in 20 ms packets
        t = np.arange(960) / 48000
        received = bytearray()

        async def reader():
            async for pcm, _ts in started.handle:
                received.extend(pcm)
                if len(received) >= 4 * 960 * 8:
                    return

        read_task = asyncio.create_task(reader())
        for i in range(10):
            tone = (np.sin(2 * np.pi * 440 * (t + i * 0.02)) * 8000).astype(np.int16)
            pcm = np.repeat(tone[:, None], 2, axis=1).tobytes()
            import time as _t

            ns = _t.monotonic_ns()
            await link.feed(pcm, ns, ns)
            await asyncio.sleep(0.02)
        await asyncio.wait_for(read_task, 5)
        samples = np.frombuffer(bytes(received), dtype=np.int16)
        assert np.max(np.abs(samples)) > 7000

        # signal absent again
        link.set_signal(False)
        await _wait_for(
            lambda: any(isinstance(e, SourceSignalChangedEvent) and e.signal is SignalState.ABSENT for e in events)
        )
    finally:
        await link.stop()
        await server.close()

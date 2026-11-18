import asyncio

from disc_ripper import events


def test_format_sse_multiline():
    assert events.format_sse("x", "a\nb") == "event: x\ndata: a\ndata: b\n\n"
    assert events.format_sse("x", "") == "event: x\ndata: \n\n"


def test_broker_delivers_to_subscribers():
    async def main():
        broker = events.Broker()
        with broker.subscribe() as sub:
            broker.publish("guard", "<p>hi</p>")
            assert await sub.queue.get() == ("guard", "<p>hi</p>")
        broker.publish("guard", "after")  # no subscribers left: fine
        assert broker.subscriber_count == 0

    asyncio.run(main())


def test_broker_drops_slow_subscriber():
    async def main():
        broker = events.Broker(maxsize=2)
        with broker.subscribe() as slow, broker.subscribe() as fast:
            for i in range(3):
                broker.publish("e", str(i))
                if i < 2:
                    await fast.queue.get()
            assert slow.dropped is True
            assert fast.dropped is False
            assert broker.subscriber_count == 1
            broker.publish("e", "later")

    asyncio.run(main())

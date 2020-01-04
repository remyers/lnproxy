import functools
import trio

import lnproxy.config as config
import lnproxy.ln_msg as ln_msg
import lnproxy.util as util


log = config.log


async def queue_to_stream(queue, stream, initiator):
    """Read from a queue and write to a stream
    Will handle lightning message parsing for inbound messages.
    Will put the messages from the queue into a one-way memory stream
    """
    i = 0
    hs_acts = 2 if initiator else 1
    send_stream, recv_stream = trio.testing.memory_stream_one_way_pair()

    async def q_2_stream(_queue, _stream):
        """Sends to a temporary stream so that we can retrieve it by byte lengths
        """
        try:
            while True:
                if _queue.empty():
                    await trio.sleep(5)
                else:
                    msg = _queue.get()
                    await _stream.send_all(msg)
        except:
            raise

    async def parse_stream(read, write, _i, _initiator):
        try:
            while True:
                if _i < hs_acts:
                    message = await ln_msg.handshake(read, _i, _initiator)
                else:
                    message = await ln_msg.read_lightning_message(read)
                await write.send_all(message)
                _i += 1
        except:
            raise

    async with trio.open_nursery() as nursery:
        try:
            nursery.start_soon(q_2_stream, queue, send_stream)
            nursery.start_soon(parse_stream, recv_stream, stream, i, initiator)
        except:
            raise


async def stream_to_queue(stream, queue, initiator):
    """Read from a stream and write to a queue.
    Will handle lightning message parsing for outbound messages.
    """
    i = 0
    hs_acts = 2 if initiator else 1
    try:
        while True:
            if i < hs_acts:
                message = await ln_msg.handshake(stream, i, initiator)
            else:
                message = await ln_msg.read_lightning_message(stream)
            queue.put(message)
            i += 1
    except:
        raise


async def proxy_streams(stream, _pubkey, stream_init, q_init):
    log(f"Proxying between stream and queue {_pubkey}")
    try:
        async with trio.open_nursery() as nursery:
            nursery.start_soon(
                stream_to_queue, stream, config.QUEUE[_pubkey]["to_send"], stream_init
            )
            nursery.start_soon(
                queue_to_stream, config.QUEUE[_pubkey]["recvd"], stream, q_init
            )
    except trio.ClosedResourceError as e:
        log(f"Attempted to use resource after we closed:\n{e}")
    except Exception as e:
        config.log(f"proxy_streams: {e}", level="error")
    finally:
        await stream.aclose()


async def handle_inbound(_pubkey):
    log(f"Handling new incoming connection from pubkey: {_pubkey}")
    # first connect to our local C-Lightning node
    stream = await trio.open_unix_socket(config.node_info["binding"][0]["socket"])
    log("Connection made to local C-Lightning node")
    # next proxy between the queue and the socket
    await proxy_streams(stream, _pubkey, stream_init=False, q_init=True)


async def handle_outbound(stream, pubkey: str):
    """Started for each outbound connection.
    """
    _pubkey = pubkey[0:4]
    log(f"Handling new outbound connection to {_pubkey}")
    if pubkey not in config.QUEUE:
        util.create_queue(_pubkey)
    log(f"Created mesh queue for {_pubkey}")
    await proxy_streams(stream, _pubkey, stream_init=True, q_init=False)


async def serve_outbound(listen_addr, pubkey: str):
    """Serve a listening socket at listen_addr.
    Start a handler for each new connection.
    """
    # Setup the listening socket
    sock = trio.socket.socket(trio.socket.AF_UNIX, trio.socket.SOCK_STREAM)
    await sock.bind(listen_addr)
    sock.listen()
    log(f"Listening for new outbound connection on {listen_addr}")
    # Start a single handle_outbound for each connection
    try:
        async with trio.open_nursery() as nursery:
            nursery.start_soon(
                trio._highlevel_serve_listeners._serve_one_listener,
                trio.SocketListener(sock),
                nursery,
                functools.partial(handle_outbound, pubkey=pubkey),
            )
    except Exception as e:
        log(f"proxy.serve_outbound error:\n{e}")

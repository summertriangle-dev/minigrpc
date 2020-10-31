import base64
import struct
from collections import deque
from typing import (
    AsyncIterator,
    Deque,
    Dict,
    Iterator,
    List,
    Optional,
    Sequence,
    Tuple,
    Union,
    cast,
)
from urllib.parse import unquote

import h2.events
from httpcore._async.connection import AsyncHTTPConnection
from httpcore._async.http2 import AsyncHTTP2Stream
from httpcore._bytestreams import (
    AsyncByteStream,
    AsyncIteratorByteStream,
    PlainByteStream,
)
from httpcore._types import URL, Headers, Origin, TimeoutDict

USE_GRPC_BEHAVIOUR_MAGIC = "please-use-grpc-behaviour-thanks"
USE_GRPC_STREAM = "grpc_stream"

# -- PATCHES -----------------------------------------------------------


def install_patches():
    global orig_AsyncHTTP2Stream_arequest
    if not orig_AsyncHTTP2Stream_arequest:
        orig_AsyncHTTP2Stream_arequest = AsyncHTTP2Stream.arequest
        AsyncHTTP2Stream.arequest = patched_AsyncHTTP2Stream_arequest


orig_AsyncHTTP2Stream_arequest = None


async def patched_AsyncHTTP2Stream_arequest(
    self: AsyncHTTP2Stream,
    method: bytes,
    url: URL,
    headers: Headers = None,
    stream: AsyncByteStream = None,
    ext: dict = None,
):
    if not ext or USE_GRPC_BEHAVIOUR_MAGIC not in ext:
        return await orig_AsyncHTTP2Stream_arequest(
            self, method, url, headers, stream, ext
        )  # type: ignore

    headers = [] if headers is None else [(k.lower(), v) for (k, v) in headers]
    if stream is None:
        stream = PlainByteStream(b"")
    ext = {} if ext is None else ext
    timeout = ext.get("timeout", {})

    # Send the request.
    seen_headers = set(key for key, value in headers)
    await self.send_headers(method, url, headers, True, timeout)
    await self.send_body(stream, timeout)

    # Receive the response.
    status_code, headers = await self.receive_response(timeout)
    response_obj = gRPCResponse(
        status_code, headers, save_msgs=not ext.get(USE_GRPC_STREAM, False)
    )
    body_iter = AsyncIteratorByteStream(
        aiterator=response_obj._body_iterator(self, timeout),
        aclose_func=self._response_closed,
    )
    ext = {"http_version": "HTTP/2.0", "grpc_response": response_obj}
    return (status_code, headers, body_iter, ext)


def grpc_extract_msg_from_buffer(length: int, buffer: deque) -> bytes:
    chunks: List[bytes] = [buffer.popleft()[5:]]
    length -= len(chunks[0])
    while length > 0:
        chunk = buffer.popleft()
        chunks.append(chunk)
        length -= len(chunk)

    if length < 0:
        buffer.appendleft(chunks[-1][length:])
        chunks[-1] = chunks[-1][:length]

    return b"".join(chunks)


def grpc_try_extract(buffer: deque) -> Iterator[bytes]:
    while True:
        if not buffer or len(buffer[0]) < 5:
            return

        is_cmp, length = struct.unpack(">BI", buffer[0][:5])
        if is_cmp != 0:
            raise ValueError("Compression isn't implemented yet. Sorry.")

        have_bytes = 0
        for chunk in buffer:
            have_bytes += len(chunk)
            if have_bytes >= length + 5:
                yield grpc_extract_msg_from_buffer(length, buffer)
                break
        else:
            return


# ----------------------------------------------------------------------

DEFAULT_PORT = 443


class gRPCResponse(object):
    def __init__(self, http_status: int, http_headers: Headers, save_msgs: bool):
        self.http_status = http_status
        self.http_headers = self.header_list_to_dict(http_headers)
        self._trailers: Optional[Dict[str, Union[str, bytes]]] = None
        self._responses: Optional[List[bytes]] = [] if save_msgs else None

    @staticmethod
    def header_list_to_dict(
        headers: Headers, is_grpc=False
    ) -> Dict[str, Union[str, bytes]]:
        headers_ret: Dict[str, Union[str, bytes]]
        if is_grpc:
            headers_ret = {}
            for (name, value) in headers:
                key = name.decode("ascii")
                if key.endswith("-bin"):
                    headers_ret[key[:-4]] = base64.b64decode(value)
                else:
                    headers_ret[key] = value.decode("ascii")
        else:
            headers_ret = {
                name.decode("ascii"): value.decode("utf8") for name, value in headers
            }

        return headers_ret

    @property
    def grpc_headers(self) -> Dict[str, Union[str, bytes]]:
        if self._trailers is None:
            raise ValueError(
                "gRPC headers are not available until you exhaust the response iterator."
            )

        return self._trailers

    @property
    def grpc_status(self) -> int:
        if self._trailers is None:
            raise ValueError(
                "Status is not available until you exhaust the response iterator."
            )

        return int(self._trailers["grpc-status"])

    @property
    def responses(self) -> Sequence[bytes]:
        if self._responses is None:
            raise ValueError("The response list is not available from streamed calls.")
        return self._responses

    @property
    def response(self) -> bytes:
        if self._responses is None:
            raise ValueError("The response list is not available from streamed calls.")
        return self._responses[0]

    def _save_response(self, msg):
        if self._responses is not None:
            self._responses.append(msg)

    def _make_trailers_available(self, trailers: Headers):
        self._trailers = self.header_list_to_dict(trailers, is_grpc=True)

    async def _body_iterator(
        self, stream: AsyncHTTP2Stream, timeout: TimeoutDict
    ) -> AsyncIterator[bytes]:
        buf: Deque[bytes] = deque()
        while True:
            event = await stream.connection.wait_for_event(stream.stream_id, timeout)
            if isinstance(event, h2.events.DataReceived):
                amount = event.flow_controlled_length
                await stream.connection.acknowledge_received_data(
                    stream.stream_id, amount, timeout
                )

                if buf and len(buf[-1]) < 5:
                    # We expect the frame indicator to be in one piece.
                    buf[-1] += event.data
                else:
                    buf.append(event.data)

                for reconstructed_msg in grpc_try_extract(buf):
                    self._save_response(reconstructed_msg)
                    yield reconstructed_msg
            elif isinstance(event, h2.events.TrailersReceived):
                for reconstructed_msg in grpc_try_extract(buf):
                    self._save_response(reconstructed_msg)
                    yield reconstructed_msg

                assert (
                    len(buf) == 0
                ), "BUG in minigrpc: there is response data remaining in buffer"
                self._make_trailers_available(event.headers)
            elif isinstance(event, (h2.events.StreamEnded, h2.events.StreamReset)):
                break


class gRPCSession(object):
    def __init__(
        self,
        target: str,
        port: int,
    ):
        install_patches()
        self.host = target.encode("utf8")
        self.port = port
        self.authority = f"https://{target}" + (
            f":{port}" if port != DEFAULT_PORT else ""
        )
        self.connection = AsyncHTTPConnection(
            origin=(b"https", target.encode("utf8"), port),
            http2=True,
        )
        self.connection.ssl_context.set_alpn_protocols(["h2"])

    def build_headers(self, headers):
        base_headers = [
            (b"te", b"trailers"),
            (b"content-type", b"application/grpc"),
            (b"grpc-accept-encoding", b"identity"),
            (b"accept-encoding", b"identity"),
        ]

        for k, v in headers.items():
            if isinstance(v, bytes):
                base_headers.append(
                    (f"{k.lower()}-bin".encode("utf8"), base64.b64encode(v))
                )
            else:
                base_headers.append((k.lower().encode("ascii"), v.encode("ascii")))

        return base_headers

    def add_length_prefix(self, to_buf):
        chunks = [struct.pack(">BI", 0, len(to_buf)), to_buf]
        return b"".join(chunks)

    async def add_length_prefixes_async(
        self, it: AsyncIterator[bytes]
    ) -> AsyncIterator[bytes]:
        async for t in it:
            yield struct.pack(">BI", 0, len(t))
            yield t

    async def wrap_output_iter(self, it: AsyncByteStream) -> AsyncIterator[bytes]:
        async for t in it:
            yield t
        await it.aclose()

    async def send_stream(
        self,
        svc: str,
        method: str,
        headers: Dict[str, Union[str, bytes]],
        payload: Union[bytes, AsyncIterator[bytes]],
    ) -> Tuple[AsyncIterator[bytes], gRPCResponse]:
        stream: AsyncByteStream
        if isinstance(payload, bytes):
            stream = PlainByteStream(self.add_length_prefix(payload))
        else:
            stream = AsyncIteratorByteStream(self.add_length_prefixes_async(payload))

        status, http_headers, resp_iter, ext = await self.connection.arequest(
            method=b"POST",
            headers=self.build_headers(headers),
            url=(b"https", self.host, self.port, f"/{svc}/{method}".encode("ascii")),
            stream=stream,
            ext={
                USE_GRPC_BEHAVIOUR_MAGIC: True,
                USE_GRPC_STREAM: True,
            },
        )

        return (
            self.wrap_output_iter(resp_iter),
            cast(gRPCResponse, ext.get("grpc_response")),
        )

    async def send(
        self,
        svc: str,
        method: str,
        headers: Dict[str, Union[str, bytes]],
        payload: Union[bytes, AsyncIterator[bytes]],
    ) -> gRPCResponse:
        stream: AsyncByteStream
        if isinstance(payload, bytes):
            stream = PlainByteStream(self.add_length_prefix(payload))
        else:
            stream = AsyncIteratorByteStream(self.add_length_prefixes_async(payload))

        status, http_headers, resp_iter, ext = await self.connection.arequest(
            method=b"POST",
            headers=self.build_headers(headers),
            url=(b"https", self.host, self.port, f"/{svc}/{method}".encode("ascii")),
            stream=stream,
            ext={
                USE_GRPC_BEHAVIOUR_MAGIC: True,
                USE_GRPC_STREAM: False,
            },
        )

        async for _ in resp_iter:
            pass

        return cast(gRPCResponse, ext.get("grpc_response"))

    async def close(self):
        await self.connection.aclose()

# minigrpc

This is a hacked up implementation of the gRPC HTTP/2 transport for, uh, research
purposes. Please do not use it in production software.

It only supports HTTPS connections as the underlying library only supports
HTTP/2 over TLS.

## API

You can use the oneshot API that reads all response messages before returning
them to you:

```python
from libminigrpc import gRPCSession
session = gRPCSession("localhost", 50052)
# Totally legit gRPC message
payload = binascii.unhexlify("0a03796f75")

resp = await session.send("helloworld.Greeter", "SayHello", {
    "custom_header": "hi",
    # Binary headers are automatically suffixed with -bin and base64 encoded.
    "binary_header": b"\x13\x37",
}, payload)

# Get the only message (or the first one if there are multiple)
print(resp.response)
# List of all messages
print(resp.responses)
# Various status bits, headers, trailers
print(resp.grpc_status, resp.grpc_headers, resp.http_headers, resp.http_status)
await session.close()
```

Or you can stream in response messages:

```python
messages, response = await session.send_stream("helloworld.Greeter", "SayHello", {}, payload)
async for msg in messages:
    print("Got a message:", msg)

# You need to iterate through all messages before reading the status bits.
print(resp.grpc_status, resp.grpc_headers, resp.http_headers, resp.http_status)
```

You can also pass in an iterator as a payload to stream request messages
(but you cannot yield partial messages, it will confuse the library):

```python
async def fun():
    yield payload
    yield payload
    yield payload

# Or send(), both work.
messages, response = await session.send_stream("helloworld.Greeter", "SayHello", {}, fun())
async for msg in messages:
    print("Got a message:", msg)
```

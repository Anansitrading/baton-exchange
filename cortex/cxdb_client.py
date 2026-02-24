"""
cxdb Python client for Oracle-Cortex conversation branching.

Speaks the cxdb binary protocol (:9009) for writes and HTTP (:9010) for reads.
Provides conversation forking, turn appending, and typed JSON projection queries.

Usage:
    from cortex.cxdb_client import CxdbClient

    client = CxdbClient()
    ctx = client.create_context()
    client.append_turn(ctx.context_id, "user", "Hello!")
    client.append_turn(ctx.context_id, "assistant", "Hi there!")

    # Fork at any turn
    fork = client.fork(turn_id=1)
    client.append_turn(fork.context_id, "assistant", "Different response!")

    # Read back
    turns = client.get_turns(ctx.context_id)
"""

import json
import socket
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import blake3
import httpx
import msgpack


# Protocol constants
MSG_HELLO = 1
MSG_CTX_CREATE = 2
MSG_CTX_FORK = 3
MSG_GET_HEAD = 4
MSG_APPEND_TURN = 5
MSG_GET_LAST = 6
MSG_GET_BLOB = 9
MSG_PUT_BLOB = 11
MSG_ERROR = 255

# Default type for Oracle conversation turns
DEFAULT_TYPE_ID = "com.oracle.conversation.Turn"
DEFAULT_TYPE_VERSION = 1

# Frame header: len(u32) + msg_type(u16) + flags(u16) + req_id(u64) = 16 bytes
FRAME_HEADER_SIZE = 16


@dataclass
class ContextHead:
    context_id: int
    head_turn_id: int
    head_depth: int


@dataclass
class TurnRecord:
    turn_id: int
    parent_turn_id: int
    depth: int
    type_id: str
    type_version: int
    encoding: int
    compression: int
    uncompressed_len: int
    content_hash: bytes
    payload: Optional[bytes] = None

    @property
    def data(self) -> Optional[dict]:
        if self.payload is None:
            return None
        return msgpack.unpackb(self.payload, raw=False, strict_map_key=False)


@dataclass
class CxdbError(Exception):
    code: int
    detail: str

    def __str__(self):
        return f"CxdbError({self.code}): {self.detail}"


class CxdbClient:
    """Python client for cxdb binary protocol (writes) and HTTP API (reads)."""

    def __init__(
        self,
        binary_host: str = "127.0.0.1",
        binary_port: int = 9009,
        http_host: str = "127.0.0.1",
        http_port: int = 9010,
        client_tag: str = "oracle-cortex-py",
        timeout: float = 30.0,
    ):
        self._binary_host = binary_host
        self._binary_port = binary_port
        self._http_base = f"http://{http_host}:{http_port}"
        self._client_tag = client_tag
        self._timeout = timeout
        self._req_id = 0
        self._lock = threading.Lock()
        self._sock: Optional[socket.socket] = None
        self._session_id: Optional[int] = None
        self._http = httpx.Client(base_url=self._http_base, timeout=timeout)

    def _next_req_id(self) -> int:
        self._req_id += 1
        return self._req_id

    def _ensure_connected(self):
        if self._sock is not None:
            return
        self._sock = socket.create_connection(
            (self._binary_host, self._binary_port), timeout=self._timeout
        )
        self._sock.settimeout(self._timeout)
        self._handshake()

    def _handshake(self):
        tag_bytes = self._client_tag.encode("utf-8")
        payload = struct.pack("<HH", 1, len(tag_bytes)) + tag_bytes + struct.pack("<I", 0)
        self._send_frame(MSG_HELLO, 0, payload)
        resp_type, _flags, _req_id, resp_data = self._recv_frame()
        if resp_type == MSG_ERROR:
            raise self._parse_error(resp_data)
        self._session_id = struct.unpack_from("<Q", resp_data, 0)[0]

    def _send_frame(self, msg_type: int, flags: int, payload: bytes):
        req_id = self._next_req_id()
        header = struct.pack("<IHHI", len(payload), msg_type, flags, 0)
        header += struct.pack("<I", req_id)
        # Actually: len(u32) + msg_type(u16) + flags(u16) + req_id(u64)
        frame = struct.pack("<I", len(payload))
        frame += struct.pack("<H", msg_type)
        frame += struct.pack("<H", flags)
        frame += struct.pack("<Q", req_id)
        frame += payload
        self._sock.sendall(frame)

    def _recv_frame(self) -> tuple[int, int, int, bytes]:
        header = self._recv_exact(FRAME_HEADER_SIZE)
        payload_len = struct.unpack_from("<I", header, 0)[0]
        msg_type = struct.unpack_from("<H", header, 4)[0]
        flags = struct.unpack_from("<H", header, 6)[0]
        req_id = struct.unpack_from("<Q", header, 8)[0]
        payload = self._recv_exact(payload_len) if payload_len > 0 else b""
        return msg_type, flags, req_id, payload

    def _recv_exact(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("Connection closed by server")
            buf.extend(chunk)
        return bytes(buf)

    def _parse_error(self, data: bytes) -> CxdbError:
        code = struct.unpack_from("<I", data, 0)[0]
        detail_len = struct.unpack_from("<I", data, 4)[0]
        detail = data[8 : 8 + detail_len].decode("utf-8", errors="replace")
        return CxdbError(code, detail)

    def _binary_request(self, msg_type: int, flags: int, payload: bytes) -> bytes:
        with self._lock:
            self._ensure_connected()
            self._send_frame(msg_type, flags, payload)
            resp_type, _flags, _req_id, resp_data = self._recv_frame()
            if resp_type == MSG_ERROR:
                raise self._parse_error(resp_data)
            return resp_data

    def _parse_context_head(self, data: bytes) -> ContextHead:
        ctx_id, head_turn, head_depth = struct.unpack_from("<QQI", data, 0)
        return ContextHead(ctx_id, head_turn, head_depth)

    # ── Write operations (binary protocol) ──────────────────────────

    def create_context(self, base_turn_id: int = 0) -> ContextHead:
        """Create a new empty context (conversation branch)."""
        payload = struct.pack("<Q", base_turn_id)
        resp = self._binary_request(MSG_CTX_CREATE, 0, payload)
        return self._parse_context_head(resp)

    def fork(self, turn_id: int) -> ContextHead:
        """Fork a new context from an existing turn. O(1) operation."""
        payload = struct.pack("<Q", turn_id)
        resp = self._binary_request(MSG_CTX_FORK, 0, payload)
        return self._parse_context_head(resp)

    def append_turn(
        self,
        context_id: int,
        role: str,
        content: str,
        parent_turn_id: int = 0,
        type_id: str = DEFAULT_TYPE_ID,
        type_version: int = DEFAULT_TYPE_VERSION,
        metadata: Optional[dict] = None,
        idempotency_key: str = "",
    ) -> TurnRecord:
        """Append a conversation turn to a context.

        Args:
            context_id: Target context
            role: "user", "assistant", "system", or "tool"
            content: Message content
            parent_turn_id: 0 = use current head, or specific turn ID
            type_id: Type identifier for registry
            type_version: Type version
            metadata: Optional extra metadata dict
            idempotency_key: For safe retries
        """
        # Build msgpack payload with numeric tags (matching type registry)
        turn_data = {1: role, 2: content, 3: int(time.time() * 1000)}
        if metadata:
            turn_data[4] = metadata
        raw_payload = msgpack.packb(turn_data, use_bin_type=True)

        # Compute BLAKE3 hash
        content_hash = blake3.blake3(raw_payload).digest()

        # Build binary frame payload
        type_id_bytes = type_id.encode("utf-8")
        idem_bytes = idempotency_key.encode("utf-8")

        buf = struct.pack("<QQ", context_id, parent_turn_id)
        buf += struct.pack("<I", len(type_id_bytes)) + type_id_bytes
        buf += struct.pack("<I", type_version)
        buf += struct.pack("<III", 1, 0, len(raw_payload))  # encoding=msgpack, compression=none
        buf += content_hash
        buf += struct.pack("<I", len(raw_payload)) + raw_payload
        buf += struct.pack("<I", len(idem_bytes))
        if idem_bytes:
            buf += idem_bytes

        resp = self._binary_request(MSG_APPEND_TURN, 0, buf)
        resp_ctx_id, new_turn_id, new_depth = struct.unpack_from("<QQI", resp, 0)
        resp_hash = resp[20:52]

        return TurnRecord(
            turn_id=new_turn_id,
            parent_turn_id=parent_turn_id,
            depth=new_depth,
            type_id=type_id,
            type_version=type_version,
            encoding=1,
            compression=0,
            uncompressed_len=len(raw_payload),
            content_hash=resp_hash,
            payload=raw_payload,
        )

    def append_raw(
        self,
        context_id: int,
        payload: bytes,
        type_id: str = DEFAULT_TYPE_ID,
        type_version: int = DEFAULT_TYPE_VERSION,
        parent_turn_id: int = 0,
        idempotency_key: str = "",
    ) -> TurnRecord:
        """Append a raw msgpack payload as a turn."""
        content_hash = blake3.blake3(payload).digest()
        type_id_bytes = type_id.encode("utf-8")
        idem_bytes = idempotency_key.encode("utf-8")

        buf = struct.pack("<QQ", context_id, parent_turn_id)
        buf += struct.pack("<I", len(type_id_bytes)) + type_id_bytes
        buf += struct.pack("<I", type_version)
        buf += struct.pack("<III", 1, 0, len(payload))
        buf += content_hash
        buf += struct.pack("<I", len(payload)) + payload
        buf += struct.pack("<I", len(idem_bytes))
        if idem_bytes:
            buf += idem_bytes

        resp = self._binary_request(MSG_APPEND_TURN, 0, buf)
        resp_ctx_id, new_turn_id, new_depth = struct.unpack_from("<QQI", resp, 0)
        resp_hash = resp[20:52]

        return TurnRecord(
            turn_id=new_turn_id,
            parent_turn_id=parent_turn_id,
            depth=new_depth,
            type_id=type_id,
            type_version=type_version,
            encoding=1,
            compression=0,
            uncompressed_len=len(payload),
            content_hash=resp_hash,
            payload=payload,
        )

    def get_head(self, context_id: int) -> ContextHead:
        """Get the current head of a context."""
        payload = struct.pack("<Q", context_id)
        resp = self._binary_request(MSG_GET_HEAD, 0, payload)
        return self._parse_context_head(resp)

    def get_last(
        self, context_id: int, limit: int = 64, include_payload: bool = True
    ) -> list[TurnRecord]:
        """Get the last N turns from a context via binary protocol."""
        payload = struct.pack("<QII", context_id, limit, 1 if include_payload else 0)
        resp = self._binary_request(MSG_GET_LAST, 0, payload)

        offset = 0
        count = struct.unpack_from("<I", resp, offset)[0]
        offset += 4

        turns = []
        for _ in range(count):
            turn_id = struct.unpack_from("<Q", resp, offset)[0]; offset += 8
            parent_id = struct.unpack_from("<Q", resp, offset)[0]; offset += 8
            depth = struct.unpack_from("<I", resp, offset)[0]; offset += 4

            tid_len = struct.unpack_from("<I", resp, offset)[0]; offset += 4
            tid = resp[offset:offset + tid_len].decode("utf-8"); offset += tid_len
            tver = struct.unpack_from("<I", resp, offset)[0]; offset += 4

            encoding = struct.unpack_from("<I", resp, offset)[0]; offset += 4
            compression = struct.unpack_from("<I", resp, offset)[0]; offset += 4
            uncompressed_len = struct.unpack_from("<I", resp, offset)[0]; offset += 4
            content_hash = resp[offset:offset + 32]; offset += 32

            turn_payload = None
            if include_payload:
                plen = struct.unpack_from("<I", resp, offset)[0]; offset += 4
                turn_payload = resp[offset:offset + plen]; offset += plen

            turns.append(TurnRecord(
                turn_id=turn_id,
                parent_turn_id=parent_id,
                depth=depth,
                type_id=tid,
                type_version=tver,
                encoding=encoding,
                compression=compression,
                uncompressed_len=uncompressed_len,
                content_hash=content_hash,
                payload=turn_payload,
            ))

        return turns

    # ── Read operations (HTTP API) ──────────────────────────────────

    def list_contexts(self, limit: int = 20, tag: Optional[str] = None) -> list[dict]:
        """List all contexts via HTTP API."""
        params = {"limit": limit}
        if tag:
            params["tag"] = tag
        resp = self._http.get("/v1/contexts", params=params)
        resp.raise_for_status()
        return resp.json().get("contexts", [])

    def get_turns_typed(
        self,
        context_id: int,
        limit: int = 64,
        view: str = "typed",
    ) -> dict:
        """Get turns with typed JSON projection via HTTP API."""
        resp = self._http.get(
            f"/v1/contexts/{context_id}/turns",
            params={"limit": limit, "view": view},
        )
        resp.raise_for_status()
        return resp.json()

    def health(self) -> bool:
        """Check if cxdb server is reachable."""
        try:
            resp = self._http.get("/healthz")
            return resp.status_code == 200
        except Exception:
            return False

    def publish_type_bundle(self, bundle: dict) -> bool:
        """Publish a type registry bundle via HTTP PUT."""
        bundle_id = bundle.get("bundle_id", "")
        resp = self._http.put(
            f"/v1/registry/bundles/{bundle_id}",
            json=bundle,
        )
        return resp.status_code in (201, 204)

    # ── Convenience methods ─────────────────────────────────────────

    def record_session(
        self,
        session_name: str,
        turns: list[dict],
    ) -> ContextHead:
        """Record a complete session as a context with turns.

        Args:
            session_name: Human-readable session identifier
            turns: List of dicts with 'role' and 'content' keys

        Returns:
            ContextHead pointing to the last turn
        """
        ctx = self.create_context()
        for turn in turns:
            self.append_turn(
                ctx.context_id,
                role=turn["role"],
                content=turn["content"],
                metadata=turn.get("metadata"),
            )
        return self.get_head(ctx.context_id)

    def fork_and_replay(
        self,
        from_turn_id: int,
        new_turns: list[dict],
    ) -> ContextHead:
        """Fork from a turn and append new turns to the branch.

        This is the core conversation branching operation.
        Useful for:
        - Trinity A/B testing: fork at a decision point, try different prompts
        - Rollback: fork from before a mistake, continue differently
        - Exploration: try multiple approaches from the same starting point

        Args:
            from_turn_id: Turn to branch from (this turn is shared)
            new_turns: New turns to append to the branch

        Returns:
            ContextHead of the new branch
        """
        fork_ctx = self.fork(from_turn_id)
        for turn in new_turns:
            self.append_turn(
                fork_ctx.context_id,
                role=turn["role"],
                content=turn["content"],
                metadata=turn.get("metadata"),
            )
        return self.get_head(fork_ctx.context_id)

    def close(self):
        """Close the connection."""
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def __del__(self):
        self.close()

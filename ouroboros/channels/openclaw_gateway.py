"""
OpenClaw Gateway WebSocket Client
Connects Ouroboros as an Agent Runtime to OpenClaw Gateway.

Protocol: WebSocket with JSON Schema wire protocol
Role: node (capability host)
Capabilities: agent runtime with full tool access
"""

import asyncio
import json
import os
import uuid
import logging
from typing import Any, Callable, Dict, List, Optional
from datetime import datetime
import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatusCode

log = logging.getLogger(__name__)


class OpenClawWireProtocol:
    """OpenClaw Wire Protocol message builders and validators."""

    PROTOCOL_VERSION_MIN = 1
    PROTOCOL_VERSION_MAX = 1

    @staticmethod
    def connect_request(
        role: str = "node",
        caps: List[str] = None,
        scopes: List[str] = None,
        device_id: str = None,
    ) -> Dict[str, Any]:
        """Build connect request message."""
        return {
            "type": "req",
            "id": str(uuid.uuid4()),
            "method": "connect",
            "params": {
                "role": role,
                "caps": caps or ["agent-runtime"],
                "scopes": scopes or ["runtime.execute", "runtime.tools"],
                "minProtocol": OpenClawWireProtocol.PROTOCOL_VERSION_MIN,
                "maxProtocol": OpenClawWireProtocol.PROTOCOL_VERSION_MAX,
                "device": {
                    "id": device_id or str(uuid.uuid4()),
                    "name": "Ouroboros Agent Runtime",
                    "version": os.environ.get("OUROBOROS_VERSION", "7.2.0"),
                },
            },
        }

    @staticmethod
    def response(request_id: str, payload: Any, ok: bool = True) -> Dict[str, Any]:
        """Build response message."""
        msg = {
            "type": "res",
            "id": request_id,
            "ok": ok,
        }
        if ok:
            msg["payload"] = payload
        else:
            msg["error"] = payload if isinstance(payload, dict) else {"message": str(payload)}
        return msg

    @staticmethod
    def event(event_type: str, payload: Any, seq: Optional[int] = None) -> Dict[str, Any]:
        """Build event message."""
        msg = {
            "type": "event",
            "event": event_type,
            "payload": payload,
        }
        if seq is not None:
            msg["seq"] = seq
        return msg

    @staticmethod
    def parse_message(data: str) -> Optional[Dict[str, Any]]:
        """Parse and validate incoming message."""
        try:
            msg = json.loads(data)
            if not isinstance(msg, dict):
                return None
            return msg
        except json.JSONDecodeError:
            return None


class OpenClawAgentRuntime:
    """
    Ouroboros Agent Runtime for OpenClaw Gateway.

    Connects as a 'node' role with agent-runtime capabilities.
    Handles tool execution, state management, and bidirectional communication.
    """

    def __init__(
        self,
        gateway_url: str,
        api_token: Optional[str] = None,
        on_message_callback: Optional[Callable[[Dict], None]] = None,
    ):
        self.gateway_url = gateway_url
        self.api_token = api_token or os.environ.get("OPENCLAW_API_TOKEN")
        self.on_message = on_message_callback

        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.connected = False
        self.session_id: Optional[str] = None
        self.protocol_version: int = 1
        self._message_handlers: Dict[str, Callable] = {}
        self._pending_requests: Dict[str, asyncio.Future] = {}
        self._seq_counter = 0
        self._reconnect_delay = 5
        self._max_reconnect_delay = 300
        self._should_run = False

        # Register default handlers
        self._register_default_handlers()

    def _register_default_handlers(self):
        """Register default message handlers."""
        self._message_handlers["runtime.execute"] = self._handle_execute
        self._message_handlers["runtime.tools.list"] = self._handle_tools_list
        self._message_handlers["runtime.tools.call"] = self._handle_tool_call
        self._message_handlers["runtime.state.get"] = self._handle_state_get
        self._message_handlers["runtime.state.set"] = self._handle_state_set

    async def connect(self) -> bool:
        """Connect to OpenClaw Gateway."""
        try:
            headers = {}
            if self.api_token:
                headers["Authorization"] = f"Bearer {self.api_token}"

            log.info(f"Connecting to OpenClaw Gateway: {self.gateway_url}")
            self.ws = await websockets.connect(
                self.gateway_url,
                extra_headers=headers,
                ping_interval=30,
                ping_timeout=10,
            )

            # Send connect request
            connect_msg = OpenClawWireProtocol.connect_request()
            await self.ws.send(json.dumps(connect_msg))

            # Wait for connect response
            response_data = await asyncio.wait_for(self.ws.recv(), timeout=10)
            response = OpenClawWireProtocol.parse_message(response_data)

            if not response or not response.get("ok"):
                error = response.get("error", "Unknown error") if response else "No response"
                log.error(f"Connect failed: {error}")
                await self.ws.close()
                return False

            self.session_id = response.get("payload", {}).get("sessionId")
            self.protocol_version = response.get("payload", {}).get("protocol", 1)
            self.connected = True
            self._reconnect_delay = 5

            log.info(f"Connected to OpenClaw Gateway. Session: {self.session_id}")

            # Start message loop
            asyncio.create_task(self._message_loop())

            return True

        except InvalidStatusCode as e:
            log.error(f"Connection rejected: HTTP {e.status_code}")
            return False
        except Exception as e:
            log.error(f"Connection failed: {e}")
            return False

    async def disconnect(self):
        """Disconnect from gateway."""
        self._should_run = False
        self.connected = False
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass
        log.info("Disconnected from OpenClaw Gateway")

    async def _message_loop(self):
        """Main message receive loop."""
        self._should_run = True
        while self._should_run and self.ws:
            try:
                data = await self.ws.recv()
                msg = OpenClawWireProtocol.parse_message(data)

                if not msg:
                    log.warning(f"Received invalid message: {data[:200]}")
                    continue

                await self._handle_message(msg)

            except ConnectionClosed:
                log.warning("WebSocket connection closed")
                self.connected = False
                await self._attempt_reconnect()
                break
            except Exception as e:
                log.error(f"Message loop error: {e}")
                await asyncio.sleep(1)

    async def _handle_message(self, msg: Dict[str, Any]):
        """Route message to appropriate handler."""
        msg_type = msg.get("type")

        if msg_type == "req":
            # Incoming request - handle it
            await self._handle_request(msg)
        elif msg_type == "res":
            # Response to our request
            await self._handle_response(msg)
        elif msg_type == "event":
            # Event from gateway
            await self._handle_event(msg)
        else:
            log.warning(f"Unknown message type: {msg_type}")

    async def _handle_request(self, req: Dict[str, Any]):
        """Handle incoming request from gateway."""
        method = req.get("method", "")
        req_id = req.get("id", "")
        params = req.get("params", {})

        handler = self._message_handlers.get(method)

        if handler:
            try:
                result = await handler(params)
                response = OpenClawWireProtocol.response(req_id, result, ok=True)
            except Exception as e:
                log.error(f"Handler error for {method}: {e}")
                response = OpenClawWireProtocol.response(
                    req_id, {"message": str(e), "code": "EXECUTION_ERROR"}, ok=False
                )
        else:
            response = OpenClawWireProtocol.response(
                req_id, {"message": f"Unknown method: {method}", "code": "UNKNOWN_METHOD"},
                ok=False
            )

        await self.send_message(response)

    async def _handle_response(self, res: Dict[str, Any]):
        """Handle response to our request."""
        req_id = res.get("id")
        future = self._pending_requests.pop(req_id, None)
        if future and not future.done():
            future.set_result(res)

    async def _handle_event(self, event: Dict[str, Any]):
        """Handle event from gateway."""
        event_type = event.get("event")
        payload = event.get("payload", {})

        log.debug(f"Received event: {event_type}")

        if self.on_message:
            try:
                self.on_message({
                    "type": "event",
                    "event": event_type,
                    "payload": payload,
                })
            except Exception as e:
                log.error(f"Event callback error: {e}")

    async def send_message(self, msg: Dict[str, Any]) -> bool:
        """Send message to gateway."""
        if not self.ws or not self.connected:
            log.warning("Cannot send message: not connected")
            return False

        try:
            await self.ws.send(json.dumps(msg))
            return True
        except Exception as e:
            log.error(f"Send failed: {e}")
            return False

    async def send_event(self, event_type: str, payload: Any) -> bool:
        """Send event to gateway."""
        self._seq_counter += 1
        event = OpenClawWireProtocol.event(event_type, payload, seq=self._seq_counter)
        return await self.send_message(event)

    async def _attempt_reconnect(self):
        """Attempt to reconnect with exponential backoff."""
        if not self._should_run:
            return

        log.info(f"Attempting reconnect in {self._reconnect_delay}s...")
        await asyncio.sleep(self._reconnect_delay)

        self._reconnect_delay = min(self._reconnect_delay * 2, self._max_reconnect_delay)

        if await self.connect():
            log.info("Reconnected successfully")

    # --- Request Handlers ---

    async def _handle_execute(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle runtime.execute - main agent execution."""
        prompt = params.get("prompt", "")
        context = params.get("context", {})
        tools = params.get("tools", [])

        # Import here to avoid circular dependency
        from ..agent import run_agent_loop

        # Execute agent loop
        result = await run_agent_loop(
            user_message=prompt,
            context=context,
            available_tools=tools,
        )

        return {
            "response": result.get("response", ""),
            "tool_calls": result.get("tool_calls", []),
            "tokens_used": result.get("tokens_used", 0),
            "execution_time_ms": result.get("execution_time_ms", 0),
        }

    async def _handle_tools_list(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle runtime.tools.list - return available tools."""
        from ..tools import get_all_tools

        tools = get_all_tools()
        tool_list = []

        for name, tool_fn in tools.items():
            # Extract schema from tool function
            schema = getattr(tool_fn, "__schema__", {})
            tool_list.append({
                "name": name,
                "description": getattr(tool_fn, "__doc__", ""),
                "parameters": schema.get("parameters", {}),
            })

        return {"tools": tool_list}

    async def _handle_tool_call(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle runtime.tools.call - execute a specific tool."""
        tool_name = params.get("name", "")
        tool_params = params.get("parameters", {})

        from ..tools import get_all_tools

        tools = get_all_tools()
        if tool_name not in tools:
            raise ValueError(f"Tool not found: {tool_name}")

        tool_fn = tools[tool_name]

        # Execute tool
        import asyncio
        if asyncio.iscoroutinefunction(tool_fn):
            result = await tool_fn(**tool_params)
        else:
            result = tool_fn(**tool_params)

        return {"result": result}

    async def _handle_state_get(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle runtime.state.get - get agent state."""
        key = params.get("key")

        from ..memory import get_scratchpad, get_identity

        if key == "scratchpad":
            return {"value": get_scratchpad()}
        elif key == "identity":
            return {"value": get_identity()}
        elif key == "state":
            from ..memory import load_state
            return {"value": load_state()}

        return {"value": None}

    async def _handle_state_set(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle runtime.state.set - set agent state."""
        key = params.get("key")
        value = params.get("value")

        if key == "scratchpad":
            from ..memory import update_scratchpad
            update_scratchpad(value)
        elif key == "identity":
            from ..memory import update_identity
            update_identity(value)

        return {"success": True}


# Singleton instance
_runtime: Optional[OpenClawAgentRuntime] = None


def get_runtime() -> Optional[OpenClawAgentRuntime]:
    """Get the singleton runtime instance."""
    return _runtime


async def start_openclaw_runtime(
    gateway_url: Optional[str] = None,
    api_token: Optional[str] = None,
) -> bool:
    """
    Start the OpenClaw Agent Runtime.

    Args:
        gateway_url: OpenClaw Gateway WebSocket URL
        api_token: Authentication token

    Returns:
        True if connected successfully
    """
    global _runtime

    gateway_url = gateway_url or os.environ.get(
        "OPENCLAW_GATEWAY_URL", "wss://gateway.openclaw.ai/ws"
    )

    _runtime = OpenClawAgentRuntime(
        gateway_url=gateway_url,
        api_token=api_token,
    )

    return await _runtime.connect()


async def stop_openclaw_runtime():
    """Stop the OpenClaw Agent Runtime."""
    global _runtime
    if _runtime:
        await _runtime.disconnect()
        _runtime = None

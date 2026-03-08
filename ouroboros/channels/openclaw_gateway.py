"""
OpenClaw Gateway WebSocket Client
Connects Ouroboros as an Agent Runtime to OpenClaw Gateway
"""

import asyncio
import json
import os
import time
import uuid
from typing import Callable, Dict, Any, Optional
import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatusCode


class OpenClawGatewayClient:
    """
    WebSocket client for OpenClaw Gateway.
    Implements the Wire Protocol for Agent Runtime communication.
    """
    
    PROTOCOL_VERSION = "0.1.0"
    RECONNECT_DELAY = 5.0
    PING_INTERVAL = 30.0
    
    def __init__(
        self,
        gateway_url: str,
        agent_id: str,
        agent_secret: Optional[str] = None,
        capabilities: Optional[list] = None
    ):
        self.gateway_url = gateway_url
        self.agent_id = agent_id
        self.agent_secret = agent_secret or os.getenv("OPENCLAW_AGENT_SECRET", "")
        self.capabilities = capabilities or ["chat", "tools", "memory"]
        
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.connected = False
        self.authenticated = False
        self._shutdown = False
        
        # Message handlers
        self._handlers: Dict[str, Callable] = {}
        self._pending_requests: Dict[str, asyncio.Future] = {}
        self._message_queue: asyncio.Queue = asyncio.Queue()
        
        # Stats
        self.messages_sent = 0
        self.messages_received = 0
        self.connected_at: Optional[float] = None
    
    def on(self, event: str, handler: Callable):
        """Register event handler."""
        self._handlers[event] = handler
    
    async def connect(self):
        """Establish WebSocket connection with auto-reconnect."""
        while not self._shutdown:
            try:
                await self._connect_once()
            except Exception as e:
                print(f"[OpenClaw] Connection error: {e}")
            
            if not self._shutdown:
                print(f"[OpenClaw] Reconnecting in {self.RECONNECT_DELAY}s...")
                await asyncio.sleep(self.RECONNECT_DELAY)
    
    async def _connect_once(self):
        """Single connection attempt."""
        print(f"[OpenClaw] Connecting to {self.gateway_url}...")
        
        headers = {
            "X-Agent-ID": self.agent_id,
            "X-Agent-Secret": self.agent_secret,
        }
        
        try:
            self.ws = await websockets.connect(
                self.gateway_url,
                extra_headers=headers,
                ping_interval=self.PING_INTERVAL,
                ping_timeout=10.0,
            )
            
            self.connected = True
            self.connected_at = time.time()
            print(f"[OpenClaw] WebSocket connected")
            
            # Send connect handshake
            await self._send_handshake()
            
            # Start message loop
            await self._message_loop()
            
        except InvalidStatusCode as e:
            print(f"[OpenClaw] Connection rejected: HTTP {e.status_code}")
            raise
        except Exception as e:
            print(f"[OpenClaw] Connection failed: {e}")
            raise
        finally:
            self.connected = False
            self.authenticated = False
            if self.ws:
                await self.ws.close()
                self.ws = None
    
    async def _send_handshake(self):
        """Send connect request per OpenClaw protocol."""
        connect_msg = {
            "type": "req",
            "id": self._generate_id(),
            "method": "connect",
            "params": {
                "role": "node",  # Agent Runtime acts as a node
                "scopes": ["agent"],
                "capabilities": self.capabilities,
                "protocol": {
                    "min": self.PROTOCOL_VERSION,
                    "max": self.PROTOCOL_VERSION,
                },
                "identity": {
                    "id": self.agent_id,
                    "type": "ouroboros-agent",
                    "version": "7.2.0",
                }
            }
        }
        
        await self.ws.send(json.dumps(connect_msg))
        self.messages_sent += 1
        print(f"[OpenClaw] Handshake sent")
    
    async def _message_loop(self):
        """Main message receive loop."""
        try:
            async for message in self.ws:
                self.messages_received += 1
                
                try:
                    data = json.loads(message)
                    await self._handle_message(data)
                except json.JSONDecodeError:
                    print(f"[OpenClaw] Invalid JSON received")
                    
        except ConnectionClosed as e:
            print(f"[OpenClaw] Connection closed: {e.code} - {e.reason}")
    
    async def _handle_message(self, data: Dict[str, Any]):
        """Dispatch incoming messages."""
        msg_type = data.get("type")
        
        if msg_type == "res":
            await self._handle_response(data)
        elif msg_type == "req":
            await self._handle_request(data)
        elif msg_type == "event":
            await self._handle_event(data)
        else:
            print(f"[OpenClaw] Unknown message type: {msg_type}")
    
    async def _handle_response(self, data: Dict[str, Any]):
        """Handle response to our request."""
        req_id = data.get("id")
        
        # Check if it's the handshake response
        if not self.authenticated and data.get("ok"):
            self.authenticated = True
            print(f"[OpenClaw] Authenticated with Gateway")
            
            # Trigger connect handler
            handler = self._handlers.get("connect")
            if handler:
                await handler(data.get("payload", {}))
            return
        
        # Resolve pending request
        if req_id in self._pending_requests:
            future = self._pending_requests.pop(req_id)
            if data.get("ok"):
                future.set_result(data.get("payload"))
            else:
                future.set_exception(Exception(data.get("error", "Unknown error")))
    
    async def _handle_request(self, data: Dict[str, Any]):
        """Handle incoming request from Gateway."""
        method = data.get("method")
        params = data.get("params", {})
        req_id = data.get("id")
        
        handler = self._handlers.get(f"request:{method}")
        
        if handler:
            try:
                result = await handler(params)
                await self._send_response(req_id, True, result)
            except Exception as e:
                await self._send_response(req_id, False, {"error": str(e)})
        else:
            await self._send_response(req_id, False, {"error": f"Unknown method: {method}"})
    
    async def _handle_event(self, data: Dict[str, Any]):
        """Handle event from Gateway."""
        event_type = data.get("event")
        payload = data.get("payload", {})
        
        handler = self._handlers.get(f"event:{event_type}")
        if handler:
            await handler(payload)
        else:
            # Generic event handler
            handler = self._handlers.get("event")
            if handler:
                await handler(event_type, payload)
    
    async def _send_response(self, req_id: str, ok: bool, payload: Any):
        """Send response to a request."""
        response = {
            "type": "res",
            "id": req_id,
            "ok": ok,
        }
        
        if ok:
            response["payload"] = payload
        else:
            response["error"] = payload if isinstance(payload, str) else payload.get("error", "Unknown error")
        
        await self.ws.send(json.dumps(response))
        self.messages_sent += 1
    
    async def send_request(self, method: str, params: Dict[str, Any] = None) -> Any:
        """Send request and wait for response."""
        if not self.connected or not self.authenticated:
            raise RuntimeError("Not connected to Gateway")
        
        req_id = self._generate_id()
        request = {
            "type": "req",
            "id": req_id,
            "method": method,
            "params": params or {},
        }
        
        # Create future for response
        future = asyncio.get_event_loop().create_future()
        self._pending_requests[req_id] = future
        
        await self.ws.send(json.dumps(request))
        self.messages_sent += 1
        
        # Wait for response with timeout
        try:
            return await asyncio.wait_for(future, timeout=30.0)
        except asyncio.TimeoutError:
            self._pending_requests.pop(req_id, None)
            raise TimeoutError(f"Request {method} timed out")
    
    async def send_event(self, event: str, payload: Dict[str, Any]):
        """Send event to Gateway."""
        if not self.connected:
            raise RuntimeError("Not connected to Gateway")
        
        message = {
            "type": "event",
            "event": event,
            "payload": payload,
            "seq": self.messages_sent + 1,
        }
        
        await self.ws.send(json.dumps(message))
        self.messages_sent += 1
    
    def _generate_id(self) -> str:
        """Generate unique request ID."""
        return f"{self.agent_id}-{uuid.uuid4().hex[:12]}"
    
    async def disconnect(self):
        """Graceful shutdown."""
        self._shutdown = True
        
        if self.ws:
            await self.ws.close()
        
        # Cancel pending requests
        for future in self._pending_requests.values():
            if not future.done():
                future.cancel()
        
        print(f"[OpenClaw] Disconnected")
    
    def get_stats(self) -> Dict[str, Any]:
        """Get connection statistics."""
        return {
            "connected": self.connected,
            "authenticated": self.authenticated,
            "messages_sent": self.messages_sent,
            "messages_received": self.messages_received,
            "connected_at": self.connected_at,
            "uptime": time.time() - self.connected_at if self.connected_at else 0,
        }


class OpenClawRuntime:
    """
    High-level OpenClaw Agent Runtime integration.
    Bridges Ouroboros agent loop with OpenClaw Gateway.
    """
    
    def __init__(self, agent_core: Any, config: Optional[Dict] = None):
        self.agent_core = agent_core
        self.config = config or {}
        
        # Gateway connection
        gateway_url = self.config.get("gateway_url") or os.getenv("OPENCLAW_GATEWAY_URL")
        agent_id = self.config.get("agent_id") or os.getenv("OPENCLAW_AGENT_ID", "ouroboros")
        agent_secret = self.config.get("agent_secret") or os.getenv("OPENCLAW_AGENT_SECRET")
        
        if not gateway_url:
            raise ValueError("OpenClaw Gateway URL not configured")
        
        self.client = OpenClawGatewayClient(
            gateway_url=gateway_url,
            agent_id=agent_id,
            agent_secret=agent_secret,
        )
        
        # Setup handlers
        self._setup_handlers()
        
        self._task: Optional[asyncio.Task] = None
    
    def _setup_handlers(self):
        """Register protocol handlers."""
        self.client.on("connect", self._on_connect)
        self.client.on("request:chat.message", self._on_chat_message)
        self.client.on("request:tools.execute", self._on_tools_execute)
        self.client.on("request:memory.query", self._on_memory_query)
        self.client.on("event:channel.message", self._on_channel_message)
    
    async def _on_connect(self, payload: Dict):
        """Handle successful connection."""
        print(f"[OpenClawRuntime] Connected to Gateway: {payload}")
    
    async def _on_chat_message(self, params: Dict) -> Dict:
        """Handle chat message request."""
        message = params.get("message", "")
        context = params.get("context", {})
        
        # Delegate to agent core
        if hasattr(self.agent_core, 'process_message'):
            response = await self.agent_core.process_message(message, context)
            return {"response": response}
        
        return {"response": "Agent core not configured for chat"}
    
    async def _on_tools_execute(self, params: Dict) -> Dict:
        """Handle tool execution request."""
        tool_name = params.get("tool")
        tool_params = params.get("params", {})
        
        # Delegate to agent tools
        if hasattr(self.agent_core, 'execute_tool'):
            result = await self.agent_core.execute_tool(tool_name, tool_params)
            return {"result": result}
        
        return {"error": "Tool execution not available"}
    
    async def _on_memory_query(self, params: Dict) -> Dict:
        """Handle memory query request."""
        query = params.get("query", "")
        
        if hasattr(self.agent_core, 'query_memory'):
            results = await self.agent_core.query_memory(query)
            return {"results": results}
        
        return {"results": []}
    
    async def _on_channel_message(self, payload: Dict):
        """Handle channel message event."""
        channel = payload.get("channel")
        message = payload.get("message")
        
        print(f"[OpenClawRuntime] Message from {channel}: {message}")
    
    async def start(self):
        """Start the runtime."""
        print("[OpenClawRuntime] Starting...")
        self._task = asyncio.create_task(self.client.connect())
    
    async def stop(self):
        """Stop the runtime."""
        print("[OpenClawRuntime] Stopping...")
        await self.client.disconnect()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
    
    def is_running(self) -> bool:
        """Check if runtime is active."""
        return self.client.connected and self.client.authenticated


# Singleton instance
_runtime: Optional[OpenClawRuntime] = None


def init_openclaw_runtime(agent_core: Any, config: Optional[Dict] = None) -> OpenClawRuntime:
    """Initialize and return the OpenClaw runtime."""
    global _runtime
    _runtime = OpenClawRuntime(agent_core, config)
    return _runtime


def get_runtime() -> Optional[OpenClawRuntime]:
    """Get the current runtime instance."""
    return _runtime
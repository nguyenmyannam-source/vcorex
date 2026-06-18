
import asyncio
import json
import random
import websockets
import time
from loguru import logger

class OkxWebSocket:
    def __init__(self, url: str, api_key: str, passphrase: str, secret_key: str):
        self.url = url
        self.api_key = api_key
        self.passphrase = passphrase
        self.secret_key = secret_key
        self.is_connected = False
        self.connection = None
        self.on_message_callback = None

    def set_on_message_callback(self, callback):
        self.on_message_callback = callback

    async def connect(self):
        logger.info("[WS-INFO] Attempting to connect to OKX WebSocket...")
        self.connection = await websockets.connect(self.url)
        self.is_connected = True
        logger.success("[WS-SUCCESS] WebSocket connection established.")
        # You might need to send a login/auth message here
        # await self.authenticate()

    async def authenticate(self):
        # Placeholder for authentication logic
        # This usually involves signing a message with API keys
        logger.info("[WS-INFO] Authenticating...")
        # await self.connection.send(auth_payload)
        logger.success("[WS-SUCCESS] Authenticated.")

    async def subscribe(self, channels: list):
        if not self.is_connected:
            raise ConnectionError("WebSocket is not connected.")

        payload = {
            "op": "subscribe",
            "args": channels
        }
        await self.connection.send(json.dumps(payload))
        logger.info(f"[WS-INFO] Subscribed to channels: {channels}")

    async def _ping_pong_heartbeat(self):
        while self.is_connected:
            try:
                await self.connection.send("ping")
                await asyncio.sleep(25)  # OKX requires a ping every 30s
            except websockets.ConnectionClosed:
                break

    async def _listen_for_messages(self):
        while self.is_connected:
            try:
                message = await self.connection.recv()
                if message == "pong":
                    continue
                if self.on_message_callback:
                    await self.on_message_callback(json.loads(message))
            except websockets.ConnectionClosed:
                logger.warning("[WS-WARN] Connection closed during listen.")
                break
            except Exception as e:
                logger.error(f"[WS-ERROR] Error while listening for messages: {e}")
                break

    async def run_forever(self):
        while True:
            try:
                await self.connect()
                # Example subscription
                # await self.subscribe([{"channel": "tickers", "instId": "BTC-USDT"}])

                # Start heartbeat and listener tasks
                heartbeat_task = asyncio.create_task(self._ping_pong_heartbeat())
                listen_task = asyncio.create_task(self._listen_for_messages())

                done, pending = await asyncio.wait(
                    [heartbeat_task, listen_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )

                for task in pending:
                    task.cancel()

            except Exception as e:
                logger.error(f"[WS-ERROR] Main run loop error: {e}")

            finally:
                if self.connection:
                    await self.connection.close()
                self.is_connected = False
                logger.info("[WS-INFO] Connection terminated. Proceeding to reconnect...")
                await self._handle_reconnection()

    async def _handle_reconnection(self):
        retry_delay = 1  # Start with 1 second
        max_delay = 60   # Cap at 60 seconds

        while not self.is_connected:
            # Add jitter: random fraction of the delay to avoid thundering herd
            jitter = random.uniform(0.5, 1.5)
            wait_time = retry_delay * jitter

            logger.info(f"[WS-RECONNECT] Reconnecting in {wait_time:.2f} seconds...")
            await asyncio.sleep(wait_time)

            # Increase delay for the next attempt (Exponential Backoff)
            retry_delay = min(retry_delay * 2, max_delay)

            # The main loop will automatically try to connect again
            break

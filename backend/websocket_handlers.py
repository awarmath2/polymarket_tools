"""
WebSocket stream handlers for market data and user order updates.
"""

import json
import asyncio
import threading
import time
from typing import Callable, List, Tuple, Dict, Optional
from websocket import WebSocketApp
from datetime import datetime

from .utilities import MarketData


# =============================================================================
# Market Data Stream Handler
# =============================================================================

class MarketDataStream:
    """Handles real-time market data from WebSocket feed"""
    
    def __init__(self, asset_ids: List[str], auth: dict, 
                 market_update_callback: Callable):
        self.asset_ids = asset_ids
        self.auth = auth
        self.market_update_callback = market_update_callback
        self.url = "wss://ws-subscriptions-clob.polymarket.com"
        self.ws: Optional[WebSocketApp] = None
        self.order_books: Dict[str, dict] = {}
        self.running = False
        self.ping_thread: Optional[threading.Thread] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
    
    async def start(self) -> None:
        """Start the market data WebSocket connection"""
        print(f"Starting market data stream for assets: {self.asset_ids}")
        self.running = True
        
        # Store reference to the current event loop
        self.loop = asyncio.get_event_loop()
        
        # Create WebSocket connection
        furl = f"{self.url}/ws/market"
        self.ws = WebSocketApp(
            furl,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
            on_open=self._on_open,
        )
        
        # Start WebSocket in a separate thread
        def run_ws():
            if self.ws is not None:
                self.ws.run_forever()
        
        ws_thread = threading.Thread(target=run_ws, daemon=True)
        ws_thread.start()
        
        # Give it a moment to connect
        await asyncio.sleep(1)
    
    async def stop(self) -> None:
        """Stop the market data stream"""
        print("Stopping market data stream")
        self.running = False
        
        if self.ws:
            self.ws.close()
        
        if self.ping_thread and self.ping_thread.is_alive():
            self.ping_thread.join(timeout=2)
    
    def get_top_of_book(self, asset_id: str) -> Tuple[Optional[float], Optional[float]]:
        """Get current best bid and ask prices"""
        if asset_id not in self.order_books:
            return None, None
        
        book = self.order_books[asset_id]
        
        # Extract best bid and ask from order book
        bids = book.get('bids', [])
        asks = book.get('asks', [])
        
        best_bid = float(bids[-1]['price']) if bids else None
        best_ask = float(asks[-1]['price']) if asks else None
        
        return best_bid, best_ask
    
    def get_order_book_depth(self, asset_id: str, levels: int = 5) -> dict:
        """Get order book depth for analysis"""
        if asset_id not in self.order_books:
            return {"bids": [], "asks": []}
        
        book = self.order_books[asset_id]
        bids = book.get('bids', [])
        asks = book.get('asks', [])
        
        # Return best levels first (reverse the stored order for display)
        # bids are stored worst->best, so reverse to get best->worst
        # asks are stored worst->best, so reverse to get best->worst  
        best_bids = list(reversed(bids))[:levels]
        best_asks = list(reversed(asks))[:levels]
        
        return {
            "bids": [(float(b['price']), float(b['size'])) for b in best_bids],
            "asks": [(float(a['price']), float(a['size'])) for a in best_asks]
        }
    
    def _on_message(self, ws, message):
        """Handle incoming WebSocket messages"""
        if message == "PONG":
            return
        
        try:
            data = json.loads(message)
            if isinstance(data, list):
                for msg in data:
                    event_type = msg.get('event_type')
                    if event_type == 'book':
                        if self.loop and not self.loop.is_closed():
                            asyncio.run_coroutine_threadsafe(self._handle_book_update(msg), self.loop)
                    elif event_type == 'price_change':
                        if self.loop and not self.loop.is_closed():
                            asyncio.run_coroutine_threadsafe(self._handle_price_change(msg), self.loop)
        except json.JSONDecodeError:
            print(f"Failed to parse message: {message}")
        except Exception as e:
            print(f"Error processing message: {e}")
    
    async def _handle_book_update(self, message: dict) -> None:
        """Process full order book updates"""
        asset_id = message.get('asset_id')
        if not asset_id:
            return
        
        # Store the full order book
        self.order_books[asset_id] = message
        
        # Create MarketData object
        bids = message.get('bids', [])
        asks = message.get('asks', [])
        
        if bids and asks:
            market_data = MarketData(
                asset_id=asset_id,
                top_bid=float(bids[-1]['price']),
                top_ask=float(asks[-1]['price']),
                bid_size=float(bids[-1]['size']),
                ask_size=float(asks[-1]['size']),
                timestamp=datetime.now()
            )
            
            # Call the callback
            await self.market_update_callback(market_data)
    
    async def _handle_price_change(self, message: dict) -> None:
        """Process individual price level changes"""
        asset_id = message.get('asset_id')
        if not asset_id or asset_id not in self.order_books:
            return
        
        # Update the order book with the price change
        changes = message.get('changes', [])
        book = self.order_books[asset_id]
        
        for change in changes:
            price = change.get('price')
            side = change.get('side')  # BUY or SELL
            size = change.get('size')
            
            if not all([price, side, size]):
                continue
            
            # Update the appropriate side of the book
            book_side = 'bids' if side == 'BUY' else 'asks'
            if book_side in book:
                # Find and update the price level
                for i, level in enumerate(book[book_side]):
                    if level['price'] == price:
                        if float(size) == 0:
                            # Remove the level if size is 0
                            book[book_side].pop(i)
                        else:
                            # Update the size
                            level['size'] = size
                        break
                else:
                    # Add new level if not found and size > 0
                    if float(size) > 0:
                        book[book_side].append({'price': price, 'size': size})
                        # Keep the book sorted - bids ascending (worst to best), asks descending (worst to best) 
                        if book_side == 'bids':
                            book[book_side].sort(key=lambda x: float(x['price']))  # Low to high
                        else:  # asks
                            book[book_side].sort(key=lambda x: float(x['price']), reverse=True)  # High to low
        
        # Create updated MarketData and call callback
        bids = book.get('bids', [])
        asks = book.get('asks', [])
        
        if bids and asks:
            market_data = MarketData(
                asset_id=asset_id,
                top_bid=float(bids[-1]['price']),
                top_ask=float(asks[-1]['price']),
                bid_size=float(bids[-1]['size']),
                ask_size=float(asks[-1]['size']),
                timestamp=datetime.now()
            )
            
            await self.market_update_callback(market_data)
    
    def _on_error(self, ws, error):
        """Handle WebSocket errors"""
        print(f"Market stream error: {error}")
    
    def _on_close(self, ws, close_status_code, close_msg):
        """Handle WebSocket close"""
        print(f"Market stream closed: {close_status_code} - {close_msg}")
        self.running = False
    
    def _on_open(self, ws):
        """Handle WebSocket connection open"""
        print("Market stream connected")
        
        # Subscribe to market data
        subscribe_msg = {
            "assets_ids": self.asset_ids, 
            "type": "market"
        }
        ws.send(json.dumps(subscribe_msg))
        
        # Start ping thread
        def ping():
            while self.running:
                try:
                    if ws and self.running:
                        ws.send("PING")
                    time.sleep(10)
                except:
                    break
        
        self.ping_thread = threading.Thread(target=ping, daemon=True)
        self.ping_thread.start()


# =============================================================================
# User Data Stream Handler
# =============================================================================

class UserDataStream:
    """Handles user-specific order updates from WebSocket feed"""
    
    def __init__(self, condition_ids: List[str], auth: dict, 
                 order_update_callback: Callable):
        self.condition_ids = condition_ids
        self.auth = auth
        self.order_update_callback = order_update_callback
        self.url = "wss://ws-subscriptions-clob.polymarket.com"
        self.ws: Optional[WebSocketApp] = None
        self.running = False
        self.ping_thread: Optional[threading.Thread] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
    
    async def start(self) -> None:
        """Start the user data WebSocket connection"""
        print("Starting user data stream")
        self.running = True
        
        # Store reference to the current event loop
        self.loop = asyncio.get_event_loop()
        
        # Create WebSocket connection
        furl = f"{self.url}/ws/user"
        self.ws = WebSocketApp(
            furl,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
            on_open=self._on_open,
        )
        
        # Start WebSocket in a separate thread
        def run_ws():
            if self.ws is not None:
                self.ws.run_forever()
        
        ws_thread = threading.Thread(target=run_ws, daemon=True)
        ws_thread.start()
        
        # Give it a moment to connect
        await asyncio.sleep(1)
    
    async def stop(self) -> None:
        """Stop the user data stream"""
        print("Stopping user data stream")
        self.running = False
        
        if self.ws:
            self.ws.close()
        
        if self.ping_thread and self.ping_thread.is_alive():
            self.ping_thread.join(timeout=2)
    
    def _on_message(self, ws, message):
        """Handle incoming WebSocket messages"""
        if message == "PONG":
            return
        
        try:
            data = json.loads(message)
            if isinstance(data, list):
                for msg in data:
                    event_type = msg.get('event_type')
                    msg_type = msg.get('type')
                    
                    if event_type == 'order':
                        if msg_type == 'PLACEMENT':
                            if self.loop and not self.loop.is_closed():
                                asyncio.run_coroutine_threadsafe(self._handle_order_placement(msg), self.loop)
                        elif msg_type == 'CANCELLATION':
                            if self.loop and not self.loop.is_closed():
                                asyncio.run_coroutine_threadsafe(self._handle_order_cancellation(msg), self.loop)
                    elif event_type == 'trade':
                        if self.loop and not self.loop.is_closed():
                            asyncio.run_coroutine_threadsafe(self._handle_trade_execution(msg), self.loop)
        except json.JSONDecodeError:
            print(f"Failed to parse user message: {message}")
        except Exception as e:
            print(f"Error processing user message: {e}")
    
    async def _handle_order_placement(self, message: dict) -> None:
        """Process order placement confirmations"""
        try:
            await self.order_update_callback('placement', message)
        except Exception as e:
            print(f"Error handling order placement: {e}")
    
    async def _handle_order_cancellation(self, message: dict) -> None:
        """Process order cancellation confirmations"""
        try:
            await self.order_update_callback('cancellation', message)
        except Exception as e:
            print(f"Error handling order cancellation: {e}")
    
    async def _handle_trade_execution(self, message: dict) -> None:
        """Process trade execution updates"""
        try:
            await self.order_update_callback('trade', message)
        except Exception as e:
            print(f"Error handling trade execution: {e}")
    
    def _on_error(self, ws, error):
        """Handle WebSocket errors"""
        print(f"User stream error: {error}")
    
    def _on_close(self, ws, close_status_code, close_msg):
        """Handle WebSocket close"""
        print(f"User stream closed: {close_status_code} - {close_msg}")
        self.running = False
    
    def _on_open(self, ws):
        """Handle WebSocket connection open"""
        print("User stream connected")
        
        # Subscribe to user updates
        subscribe_msg = {
            "markets": self.condition_ids,
            "type": "user",
            "auth": self.auth
        }
        ws.send(json.dumps(subscribe_msg))
        
        # Start ping thread
        def ping():
            while self.running:
                try:
                    if ws and self.running:
                        ws.send("PING")
                    time.sleep(10)
                except:
                    break
        
        self.ping_thread = threading.Thread(target=ping, daemon=True)
        self.ping_thread.start() 
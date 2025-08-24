"""
Utility classes and data models for the Polymarket order management system.
"""

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Callable


# =============================================================================
# Constants
# =============================================================================

MIN_ORDER_SIZE = 5.0  # Minimum order size in shares


# =============================================================================
# Data Models
# =============================================================================

@dataclass
class StrategyConfig:
    """Configuration for the order management strategy"""
    token_id: str
    limit_price: float
    total_quantity: float
    child_order_size: float
    order_price_min_tick_size: float  # Minimum price increment (0.01 or 0.001)
    side: str = "BUY"  # Order side: BUY or SELL
    timeout_seconds: int = 3600
    rate_limit_per_second: float = 5.0
    max_pending_orders: int = 3
    price_improvement_ticks: int = 1  # How many ticks to improve price by
    match_top_of_book: bool = False  # If True, match instead of beating the top of book
    inside_liquidity_mode: bool = False  # If True, only take liquidity within limit price range


@dataclass
class OrderState:
    """Represents the state of an individual order"""
    order_id: str
    price: float
    size: float
    status: str  # LIVE, CANCELED, MATCHED, etc.
    created_at: datetime
    updated_at: datetime
    filled_size: float = 0.0


@dataclass
class MarketData:
    """Current market data snapshot"""
    asset_id: str
    top_bid: float
    top_ask: float
    bid_size: float
    ask_size: float
    timestamp: datetime
    

@dataclass
class PositionState:
    """Current position and target state"""
    target_quantity: float
    filled_quantity: float
    pending_quantity: float
    average_fill_price: float
    unrealized_pnl: float = 0.0


# =============================================================================
# Rate Limiter
# =============================================================================

class RateLimiter:
    """Token bucket rate limiter for API calls"""
    
    def __init__(self, max_requests_per_second: float):
        self.max_requests_per_second = max_requests_per_second
        self.tokens = max_requests_per_second
        self.last_update = time.time()
        self._lock = asyncio.Lock()
    
    async def acquire(self) -> bool:
        """Acquire permission to make a request"""
        async with self._lock:
            now = time.time()
            # Add tokens based on elapsed time
            elapsed = now - self.last_update
            self.tokens = min(
                self.max_requests_per_second,
                self.tokens + elapsed * self.max_requests_per_second
            )
            self.last_update = now
            
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return True
            return False
    
    def get_current_rate(self) -> float:
        """Get current request rate"""
        return self.tokens
    
    def reset(self) -> None:
        """Reset the rate limiter"""
        self.tokens = self.max_requests_per_second
        self.last_update = time.time()


# =============================================================================
# Position Tracker
# =============================================================================

class PositionTracker:
    """Tracks current positions, orders, and target state"""
    
    def __init__(self, token_id: str, target_quantity: float):
        self.token_id = token_id
        self.target_quantity = target_quantity
        self.filled_quantity = 0.0
        self.pending_orders: Dict[str, OrderState] = {}
        self.fill_history: List[dict] = []
        self.total_fill_value = 0.0
    
    def update_filled_quantity(self, fill_size: float, fill_price: float) -> None:
        """Update position based on trade execution"""
        # Cap fill size to remaining quantity to prevent overfills (max 100% filled)
        remaining_quantity = self.get_remaining_quantity()
        actual_fill_size = min(fill_size, remaining_quantity)
        
        if actual_fill_size <= 0:
            print(f"Warning: Ignoring fill of {fill_size} - target already reached (filled: {self.filled_quantity}/{self.target_quantity})")
            return
        
        if actual_fill_size < fill_size:
            print(f"Warning: Capping fill from {fill_size} to {actual_fill_size} to prevent overfill")
        
        self.filled_quantity += actual_fill_size
        self.total_fill_value += actual_fill_size * fill_price
        
        # Record fill history
        self.fill_history.append({
            'size': actual_fill_size,
            'price': fill_price,
            'timestamp': datetime.now()
        })
    
    def add_pending_order(self, order_id: str, price: float, size: float) -> None:
        """Track a new pending order"""
        self.pending_orders[order_id] = OrderState(
            order_id=order_id,
            price=price,
            size=size,
            status="LIVE",
            created_at=datetime.now(),
            updated_at=datetime.now()
        )
    
    def remove_pending_order(self, order_id: str) -> None:
        """Remove a cancelled or filled order"""
        if order_id in self.pending_orders:
            del self.pending_orders[order_id]
    
    def get_remaining_quantity(self) -> float:
        """Get remaining quantity to be filled"""
        return max(0, self.target_quantity - self.filled_quantity)
    
    def get_pending_quantity(self) -> float:
        """Get total quantity in pending orders"""
        return sum(order.size for order in self.pending_orders.values())
    
    def get_pending_orders(self) -> List[OrderState]:
        """Get list of all pending orders"""
        return list(self.pending_orders.values())
    
    def get_average_fill_price(self) -> float:
        """Get volume-weighted average fill price"""
        if self.filled_quantity == 0:
            return 0.0
        return self.total_fill_value / self.filled_quantity
    
    def is_target_reached(self) -> bool:
        """Check if target quantity has been reached"""
        return self.filled_quantity >= self.target_quantity
    
    def get_position_summary(self) -> PositionState:
        """Get comprehensive position summary"""
        return PositionState(
            target_quantity=self.target_quantity,
            filled_quantity=self.filled_quantity,
            pending_quantity=self.get_pending_quantity(),
            average_fill_price=self.get_average_fill_price(),
            unrealized_pnl=0.0  # TODO: Calculate based on current market price
        )
    
    def update_order_status(self, order_id: str, new_status: str, 
                           filled_size: float = 0.0) -> None:
        """Update order status from WebSocket feed"""
        if order_id in self.pending_orders:
            order = self.pending_orders[order_id]
            order.status = new_status
            order.updated_at = datetime.now()
            order.filled_size += filled_size
            
            # If order is fully filled or cancelled, remove it
            if new_status in ["MATCHED", "CANCELED"]:
                self.remove_pending_order(order_id)


# =============================================================================
# Stop Condition Manager
# =============================================================================

class StopConditionManager:
    """Manages various stop conditions for the strategy"""
    
    def __init__(self, timeout_seconds: int = 3600):
        self.timeout_seconds = timeout_seconds
        self.start_time = datetime.now()
        self.stop_requested = False
        self.large_order_threshold = 1000.0  # Size threshold for "large" orders
        self.stop_callback: Optional[Callable] = None
    
    def set_stop_callback(self, callback: Callable) -> None:
        """Set callback to be called when stop condition is triggered"""
        self.stop_callback = callback
    
    def request_stop(self) -> None:
        """Manually request strategy stop"""
        self.stop_requested = True
        if self.stop_callback:
            asyncio.create_task(self.stop_callback("manual_stop"))
    
    def check_timeout(self) -> bool:
        """Check if strategy has timed out"""
        elapsed = datetime.now() - self.start_time
        if elapsed > timedelta(seconds=self.timeout_seconds):
            if self.stop_callback:
                asyncio.create_task(self.stop_callback("timeout"))
            return True
        return False
    
    def check_large_order_impact(self, market_data: MarketData, 
                                previous_market_data: MarketData) -> bool:
        """Check if a large order has significantly impacted the market"""
        # TODO: Implement logic to detect large order execution
        # - Compare bid/ask sizes before and after
        # - Check for significant price movements
        # - Detect unusual volume spikes
        return False
    
    def should_stop(self) -> bool:
        """Check all stop conditions"""
        if self.stop_requested:
            return True
        
        if self.check_timeout():
            return True
        
        return False
    
    def get_remaining_time(self) -> int:
        """Get remaining time in seconds before timeout"""
        elapsed = datetime.now() - self.start_time
        remaining = self.timeout_seconds - elapsed.total_seconds()
        return max(0, int(remaining))
    
    def reset_timer(self) -> None:
        """Reset the timeout timer"""
        self.start_time = datetime.now()
    
    def extend_timeout(self, additional_seconds: int) -> None:
        """Extend the timeout by additional seconds"""
        self.timeout_seconds += additional_seconds
        print(f"Timeout extended by {additional_seconds} seconds. New timeout: {self.timeout_seconds} seconds") 
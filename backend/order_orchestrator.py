"""
Order orchestration, strategy logic, and CLI for the Polymarket order management system.
"""

import asyncio, argparse, json, os, time, sys, logging, requests
from typing import Optional, List, Dict
import msvcrt  # Windows
import uuid

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType, BalanceAllowanceParams, AssetType
from py_clob_client.order_builder.constants import BUY, SELL

from .utilities import (
    StrategyConfig, OrderState, MarketData, 
    RateLimiter, PositionTracker, StopConditionManager, MIN_ORDER_SIZE
)
from .websocket_handlers import MarketDataStream, UserDataStream

logger = logging.getLogger(__name__)


# =============================================================================
# Order Execution Engine
# =============================================================================

class OrderExecutor:
    """Handles order placement, modification, and cancellation"""
    
    def __init__(self, client: ClobClient, rate_limiter: RateLimiter):
        self.client = client
        self.rate_limiter = rate_limiter
        self.pending_orders: Dict[str, OrderState] = {}
    
    async def place_order(self, 
                         token_id: str, 
                         price: float, 
                         size: float, 
                         side: str = BUY,
                         timeout_seconds: int = 30) -> Optional[str]:
        """Place a new order and return order ID"""
        
        # Validate parameters
        if not self._validate_order_params(price, size):
            print(f"Invalid order parameters: price={price}, size={size}")
            return None
        
        # Check rate limit
        if not await self.rate_limiter.acquire():
            print("Rate limit exceeded, skipping order placement")
            return None
        
        try:
            print(f"Placing order: {side} {size} @ ${price:.4f} for token {token_id}")
            
            # Create order arguments
            order_args = OrderArgs(
                price=price,
                size=size,
                side=side,
                token_id=token_id,
            )
            
            # Create and sign the order
            signed_order = self.client.create_order(order_args)
            
            # Submit the order  
            resp = self.client.post_order(signed_order, "GTC")  # type: ignore
            
            if resp:
                # Handle response based on its type
                if isinstance(resp, dict):
                    if resp.get('success', False):
                        order_id = resp.get('orderID')
                        if order_id:
                            print(f"Order placed successfully: {order_id}")
                            return order_id
                        else:
                            print("Order placed but no order ID returned")
                            return None
                    else:
                        print(f"Failed to place order: {resp}")
                        return None
                elif isinstance(resp, str):
                    # If response is a string, assume it's the order ID
                    print(f"Order placed successfully: {resp}")
                    return resp
                else:
                    print(f"Unexpected response type: {type(resp)}, value: {resp}")
                    return None
            else:
                print("No response received from order placement")
                return None
                
        except Exception as e:
            error_msg = str(e)
            print(f"Error placing order: {error_msg}")
            
            # Check for critical balance/allowance errors that should be fatal
            if "not enough balance" in error_msg.lower() or "allowance" in error_msg.lower():
                print(f"FATAL ERROR: {error_msg}")
                # Raise the exception to stop the strategy
                raise Exception(f"Fatal error - insufficient balance/allowance: {error_msg}")
            
            return None
    
    async def cancel_order(self, order_id: str, 
                          timeout_seconds: int = 10, 
                          max_retries: int = 5) -> bool:
        """Cancel an existing order with retry logic"""
        
        for attempt in range(max_retries):
            if not await self.rate_limiter.acquire():
                if attempt == max_retries - 1:
                    raise Exception(f"Failed to cancel order {order_id} after {max_retries} attempts due to rate limiting")
                print(f"Rate limit exceeded on cancel attempt {attempt + 1}/{max_retries}, retrying...")
                await asyncio.sleep(1)  # Wait 1 second before retry
                continue
            
            try:
                print(f"Cancelling order: {order_id} (attempt {attempt + 1}/{max_retries})")
                
                # Use the client's cancel order method
                resp = self.client.cancel_orders([order_id])
                
                if resp:
                    # Handle response based on its type
                    if isinstance(resp, dict):
                        # Check if order was successfully canceled
                        canceled_orders = resp.get('canceled', [])
                        if order_id in canceled_orders:
                            print(f"Order cancelled successfully: {order_id}")
                            return True
                        
                        # Check if there's a failure reason
                        not_canceled = resp.get('not_canceled', {})
                        if order_id in not_canceled:
                            error_reason = not_canceled[order_id]
                            if error_reason == 'order already canceled':
                                print(f"Order was already cancelled: {order_id}")
                                return True  # Treat as success since order is canceled
                            else:
                                print(f"Failed to cancel order {order_id}: {error_reason}")
                        else:
                            print(f"Failed to cancel order {order_id}: {resp}")
                    elif isinstance(resp, list) and len(resp) > 0:
                        # If response is a list, check if order was cancelled
                        print(f"Order cancelled successfully: {order_id}")
                        return True
                    else:
                        print(f"Failed to cancel order {order_id}: {resp}")
                else:
                    print(f"No response received for cancel order {order_id}")
                
                # If we reach here, the cancellation failed but didn't raise an exception
                if attempt == max_retries - 1:
                    raise Exception(f"Failed to cancel order {order_id} after {max_retries} attempts")
                    
            except Exception as e:
                if attempt == max_retries - 1:
                    raise Exception(f"Failed to cancel order {order_id} after {max_retries} attempts: {e}")
                print(f"Error cancelling order {order_id} on attempt {attempt + 1}: {e}")
                await asyncio.sleep(1)  # Wait 1 second before retry
        
        return False
    
    async def cancel_all_orders(self, token_id: str) -> List[str]:
        """Cancel all orders for a given token"""
        cancelled_orders = []
        
        # Get all open orders for the token
        try:
            from py_clob_client.clob_types import OpenOrderParams
            
            # Get market from token_id (this might need adjustment based on your setup)
            resp = self.client.get_orders(OpenOrderParams())
            
            if resp and isinstance(resp, list):
                for order in resp:
                    if order.get('asset_id') == token_id:
                        order_id = order.get('id')
                        if order_id and await self.cancel_order(order_id):
                            cancelled_orders.append(order_id)
        except Exception as e:
            print(f"Error getting orders for cancellation: {e}")
        
        return cancelled_orders
    
    def _validate_order_params(self, price: float, size: float) -> bool:
        """Validate order parameters before submission"""
        if price <= 0 or size <= 0:
            return False
        if price > 1.0:  # Polymarket prices are 0-1
            return False
        if size < MIN_ORDER_SIZE:  # Minimum order size check
            return False
        return True


# =============================================================================
# Strategy Logic
# =============================================================================

class TopOfBookStrategy:
    """Trading strategy that maintains orders at the top of the book"""
    
    def __init__(self, 
                 order_executor: OrderExecutor, 
                 position_tracker: PositionTracker,
                 child_order_size: float,
                 limit_price: float,
                 order_price_min_tick_size: float,
                 side: str = BUY,
                 price_improvement_ticks: int = 1,
                 match_top_of_book: bool = False):
        
        self.order_executor = order_executor
        self.position_tracker = position_tracker
        self.child_order_size = child_order_size
        self.limit_price = limit_price
        self.side = side
        self.order_price_min_tick_size = order_price_min_tick_size
        self.price_improvement_ticks = price_improvement_ticks
        self.match_top_of_book = match_top_of_book
        self.critical_error = False
        self.critical_error_message = ""
        
        # Add error tracking attributes needed by the methods
        self.consecutive_order_failures = 0
        self.max_consecutive_failures = 3
        self.critical_error_occurred = False
        self.current_market_data: Optional[MarketData] = None
        
        print(f"TopOfBookStrategy initialized - side: {side}, limit: ${limit_price:.4f}, size: {child_order_size}")
        
    async def process_market_update(self, market_data: MarketData) -> None:
        """React to market data changes"""
        self.current_market_data = market_data
        
        print(f"Market update - Bid: ${market_data.top_bid:.4f}, Ask: ${market_data.top_ask:.4f}")
        
        # Check if we need to adjust our orders
        if self._should_adjust_orders(market_data):
            await self._adjust_orders(market_data)
    
    async def process_order_update(self, update_type: str, order_data: dict) -> None:
        """React to order status changes"""
        order_id = order_data.get('id')
        
        if update_type == 'placement':
            # Order was successfully placed
            print(f"Order placement confirmed: {order_id}")
            self._handle_order_placement(order_data)
        elif update_type == 'cancellation':
            # Order was cancelled
            print(f"Order cancellation confirmed: {order_id}")
            self._handle_order_cancellation(order_data)
        elif update_type == 'trade':
            # Order was executed (fully or partially) - validation happens in _handle_trade_execution
            await self._handle_trade_execution(order_data)
    
    def _should_adjust_orders(self, market_data: MarketData) -> bool:
        """Determine if orders need adjustment based on market changes"""
        if self.position_tracker.is_target_reached():
            return False
        
        pending_orders = self.position_tracker.get_pending_orders()
        
        # If no pending orders and we still have quantity to fill, place new order
        if not pending_orders:
            return True
        
        # Check if our top order is no longer competitive
        # Use appropriate market side based on order side
        current_top_price = market_data.top_bid if self.side == BUY else market_data.top_ask
        target_price = self._calculate_target_price(current_top_price)
        for order in pending_orders:
            if abs(order.price - target_price) > self.order_price_min_tick_size:
                return True
        
        return False
    
    async def _adjust_orders(self, market_data: MarketData) -> None:
        """Adjust existing orders to maintain top-of-book"""
        # Cancel existing orders that are no longer optimal
        pending_orders = self.position_tracker.get_pending_orders()
        # Use appropriate market side based on order side
        current_top_price = market_data.top_bid if self.side == BUY else market_data.top_ask
        target_price = self._calculate_target_price(current_top_price)
        
        for order in pending_orders:
            if abs(order.price - target_price) > self.order_price_min_tick_size:
                await self.order_executor.cancel_order(order.order_id)
                self.position_tracker.remove_pending_order(order.order_id)
        
        # Place new order if we have remaining quantity
        remaining_qty = self.position_tracker.get_remaining_quantity()
        if remaining_qty > 0 and self._should_place_order(target_price):
            order_size = self._get_optimal_order_size(remaining_qty)
            # Only place order if size is above 0 (meets minimum size requirement)
            if order_size > 0:
                await self._place_new_order(target_price, order_size)
    
    def _calculate_target_price(self, current_top_price: float) -> float:
        """Calculate the price for the next order"""
        pending_orders = self.position_tracker.get_pending_orders()
        
        if self.side == BUY:
            # For BUY orders, we want to be on top of the bid side
            our_best_price = max([order.price for order in pending_orders], default=0.0)
            
            # If the current top bid is our own order (or very close to it), don't compete with ourselves
            if abs(current_top_price - our_best_price) <= self.order_price_min_tick_size:
                print(f"Current top bid ${current_top_price:.4f} is our own order, not competing with ourselves")
                return our_best_price  # Keep our current price
            
            if self.match_top_of_book:
                # Match the current top bid exactly
                target_price = current_top_price
            else:
                # Improve the current top bid by going higher
                target_price = current_top_price + (self.price_improvement_ticks * self.order_price_min_tick_size)
            
            # Ensure we don't exceed our limit price (max we're willing to pay)
            target_price = min(target_price, self.limit_price)
            
        else:  # SELL
            # For SELL orders, we want to be on top of the ask side
            our_best_price = min([order.price for order in pending_orders], default=float('inf'))
            
            # If the current top ask is our own order (or very close to it), don't compete with ourselves
            if abs(current_top_price - our_best_price) <= self.order_price_min_tick_size:
                print(f"Current top ask ${current_top_price:.4f} is our own order, not competing with ourselves")
                return our_best_price  # Keep our current price
            
            if self.match_top_of_book:
                # Match the current top ask exactly
                target_price = current_top_price
            else:
                # Improve the current top ask by going lower
                target_price = current_top_price - (self.price_improvement_ticks * self.order_price_min_tick_size)
            
            # Ensure we don't go below our limit price (min we're willing to accept)
            target_price = max(target_price, self.limit_price)
        
        # Round to the minimum tick size precision
        decimal_places = 3 if self.order_price_min_tick_size == 0.001 else 2
        return round(target_price, decimal_places)
    
    async def _place_new_order(self, price: float, size: float) -> None:
        """Place a new order with the given parameters"""
        try:
            # Validate that we should place an order at this price
            if not self._should_place_order(price):
                return
            
            # Place the order
            order_id = await self.order_executor.place_order(
                token_id=self.position_tracker.token_id,
                price=price,
                size=size,
                side=self.side
            )
            
            if order_id:
                # Track the order
                self.position_tracker.add_pending_order(order_id, price, size)
                print(f"Successfully placed and tracking order: {order_id}")
                
                # Reset consecutive failure counter
                self.consecutive_order_failures = 0
            else:
                # Increment failure counter
                self.consecutive_order_failures += 1
                print(f"Failed to place order (consecutive failures: {self.consecutive_order_failures})")
                
                                # Check if we should set critical error
                if self.consecutive_order_failures >= self.max_consecutive_failures:
                    self.critical_error_occurred = True
                    self.critical_error_message = f"Failed to place orders {self.consecutive_order_failures} consecutive times"
                    print(f"Critical error: {self.critical_error_message}")
                    
        except Exception as e:
            error_msg = f"Error placing order: {e}"
            print(error_msg)
            self.consecutive_order_failures += 1
            
            # Check for critical balance/allowance errors that should be fatal
            if ("not enough balance" in str(e).lower() or 
                "allowance" in str(e).lower() or
                "insufficient balance" in str(e).lower()):
                self.critical_error_occurred = True
                self.critical_error_message = f"Fatal error - insufficient balance/allowance: {str(e)}"
                print(f"CRITICAL ERROR: {self.critical_error_message}")
                # Re-raise to stop the strategy immediately
                raise e
            
            # Check if we should set critical error for other failures
            if self.consecutive_order_failures >= self.max_consecutive_failures:
                self.critical_error_occurred = True
                self.critical_error_message = f"Order placement errors: {error_msg}"
    
    def has_critical_error(self) -> bool:
        """Check if a critical error has occurred"""
        return self.critical_error_occurred
    
    def get_critical_error_message(self) -> str:
        """Get the critical error message"""
        return self.critical_error_message
    
    def _should_place_order(self, price: float) -> bool:
        """Determine if a new order should be placed"""
        # Don't place if price violates limit
        if self.side == BUY and price > self.limit_price:
            return False
        elif self.side == SELL and price < self.limit_price:
            return False
        
        # Don't place if we already have too many pending orders
        if len(self.position_tracker.get_pending_orders()) >= 3:
            return False
        
        return True
    
    def _get_optimal_order_size(self, remaining_quantity: float) -> float:
        """Calculate optimal order size based on remaining quantity and minimum order size"""
        # If remaining quantity is less than minimum, don't place an order
        if remaining_quantity < MIN_ORDER_SIZE:
            print(f"Remaining quantity {remaining_quantity} is below minimum order size {MIN_ORDER_SIZE}, not placing order")
            return 0.0
        
        # If remaining quantity is less than or equal to child order size, use it all
        if remaining_quantity <= self.child_order_size:
            print(f"Using remaining quantity {remaining_quantity} for final order (≤ child order size {self.child_order_size})")
            return remaining_quantity
        
        # Check if placing a normal child order would leave an unusable remainder
        remainder_after_child_order = remaining_quantity - self.child_order_size
        if 0 < remainder_after_child_order < MIN_ORDER_SIZE:
            # Combine the child order and remainder into one larger order
            combined_size = remaining_quantity
            print(f"Combining child order {self.child_order_size} + remainder {remainder_after_child_order} = {combined_size} to avoid unusable remainder")
            return combined_size
        
        # Normal case: use child order size
        return self.child_order_size
    
    def _handle_order_placement(self, order_data: dict) -> None:
        """Handle successful order placement"""
        # Order tracking is handled when we place the order
        pass
    
    def _handle_order_cancellation(self, order_data: dict) -> None:
        """Handle order cancellation"""
        order_id = order_data.get('id')
        if order_id:
            self.position_tracker.remove_pending_order(order_id)
    
    async def _handle_trade_execution(self, trade_data: dict) -> None:
        """Handle trade execution"""
        try:
            # Extract order IDs from the trade data
            taker_order_id = trade_data.get('taker_order_id')
            maker_orders = trade_data.get('maker_orders', [])
            
            # Check if this trade involves any of our orders
            is_our_trade = False
            our_order_id = None
            
            # Check if we're the taker
            if taker_order_id and taker_order_id in self.position_tracker.pending_orders:
                is_our_trade = True
                our_order_id = taker_order_id
            
            # Check if we're one of the makers
            if not is_our_trade and maker_orders:
                for maker_order in maker_orders:
                    maker_order_id = maker_order.get('order_id')
                    if maker_order_id and maker_order_id in self.position_tracker.pending_orders:
                        is_our_trade = True
                        our_order_id = maker_order_id
                        break
            
            # Only process if this trade involves our orders
            if not is_our_trade:
                print(f"Ignoring trade - not our order (taker: {taker_order_id}, makers: {[m.get('order_id') for m in maker_orders]})")
                return
            
            size = float(trade_data.get('size', 0))
            price = float(trade_data.get('price', 0))
            
            if size > 0:
                # Get the original order to validate fill size
                original_order = None
                if our_order_id and our_order_id in self.position_tracker.pending_orders:
                    original_order = self.position_tracker.pending_orders[our_order_id]
                
                # Validate fill size doesn't exceed order size
                if original_order and size > original_order.size:
                    print(f"WARNING: Fill size {size} exceeds original order size {original_order.size} for order {our_order_id}")
                    # Cap the fill to the original order size to prevent overfills
                    size = original_order.size
                    print(f"Capping trade execution to original order size: {size}")
                
                print(f"[{self.position_tracker.token_id[:8]}...] Received trade for order {our_order_id}")
                print(f"Trade executed: {size} @ ${price:.4f} (order: {our_order_id})")
                
                # Update position (this will also cap to remaining quantity)
                self.position_tracker.update_filled_quantity(size, price)
                
                # Remove the order from pending orders if it was our order
                if our_order_id:
                    self.position_tracker.remove_pending_order(our_order_id)
                
                # If we still have quantity to fill, try to place another order
                if not self.position_tracker.is_target_reached() and self.current_market_data:
                    await self._adjust_orders(self.current_market_data)
        except Exception as e:
            print(f"Error handling trade execution: {e}")


class InsideLiquidityStrategy:
    """Strategy that takes liquidity when orders appear within limit price range"""
    
    def __init__(self, 
                 order_executor: OrderExecutor, 
                 position_tracker: PositionTracker,
                 child_order_size: float,
                 limit_price: float,
                 order_price_min_tick_size: float,
                 side: str = BUY,
                 max_slippage: float = 0.01):
        
        self.order_executor = order_executor
        self.position_tracker = position_tracker
        self.child_order_size = child_order_size
        self.limit_price = limit_price
        self.side = side
        self.order_price_min_tick_size = order_price_min_tick_size
        self.max_slippage = max_slippage
        self.critical_error = False
        self.critical_error_message = ""
        self.last_market_data = None
        
        # Create market order executor with same client and rate limiter
        rate_limiter = RateLimiter(5.0)  # Use same rate as main executor
        self.market_executor = MarketOrderExecutor(order_executor.client, rate_limiter)
        
        print(f"InsideLiquidityStrategy initialized - side: {side}, limit: ${limit_price:.4f}, size: {child_order_size}")
        
    async def process_market_update(self, market_data: MarketData) -> None:
        """Process market data update and check for liquidity opportunities"""
        try:
            self.last_market_data = market_data
            
            # Check if we should take liquidity
            if await self._should_take_liquidity(market_data):
                await self._take_liquidity(market_data)
                
        except Exception as e:
            print(f"Error processing market update in inside liquidity strategy: {e}")
            self.critical_error = True
            self.critical_error_message = str(e)
    
    async def process_order_update(self, update_type: str, order_data: dict) -> None:
        """Process order update to track fills from market orders"""
        # Handle trade executions to track position updates
        if update_type == "trade" and order_data:
            await self._handle_trade_execution(order_data)
    
    async def _should_take_liquidity(self, market_data: MarketData) -> bool:
        """Check if we should take liquidity based on current market conditions"""
        # Check if we've reached our target
        if self.position_tracker.is_target_reached():
            return False
        
        # Get the current best price for taking liquidity
        target_price = None
        available_size = 0
        
        if self.side == BUY:
            # For buy orders, check if ask price is within our limit
            target_price = market_data.top_ask
            available_size = market_data.ask_size
            
            # Check if ask price is within our limit price
            if target_price > self.limit_price:
                return False
                
        elif self.side == SELL:
            # For sell orders, check if bid price is within our limit
            target_price = market_data.top_bid
            available_size = market_data.bid_size
            
            # Check if bid price is within our limit price
            if target_price < self.limit_price:
                return False
        
        # Check if there's enough size available
        if available_size < self.child_order_size:
            return False
        
        print(f"Liquidity opportunity detected: {self.side} @ ${target_price:.4f} (limit: ${self.limit_price:.4f}), size: {available_size}")
        return True
    
    async def _take_liquidity(self, market_data: MarketData) -> None:
        """Take available liquidity using market order"""
        try:
            # Calculate order size based on remaining quantity needed
            remaining_qty = self.position_tracker.get_remaining_quantity()
            order_size = min(self.child_order_size, remaining_qty)
            
            if order_size < 5.0:  # MIN_ORDER_SIZE
                print(f"Order size too small ({order_size}), skipping")
                return
            
            print(f"Taking liquidity: {self.side} {order_size} shares @ market")
            
            # Place market order using the market executor
            order_id = await self.market_executor.place_market_order(
                token_id=self.position_tracker.token_id,
                size=order_size,
                side=self.side,
                max_slippage=self.max_slippage,
                timeout_seconds=30
            )
            
            if order_id:
                print(f"Market order placed successfully: {order_id}")
                # Note: Position tracking will be updated via WebSocket order updates
            else:
                print("Failed to place market order")
                
        except Exception as e:
            print(f"Error taking liquidity: {e}")
            self.critical_error = True
            self.critical_error_message = str(e)
    
    def has_critical_error(self) -> bool:
        """Check if strategy has encountered a critical error"""
        return self.critical_error
    
    def get_critical_error_message(self) -> str:
        """Get critical error message"""
        return self.critical_error_message
    
    async def _handle_trade_execution(self, trade_data: dict) -> None:
        """Handle trade execution for market orders"""
        try:
            # This is similar to TopOfBookStrategy's trade handling
            # but adapted for market orders
            taker_order_id = trade_data.get('taker_order_id')
            maker_orders = trade_data.get('maker_orders', [])
            
            # Check if this trade involves any of our orders
            # For market orders, we're typically the taker
            is_our_trade = False
            our_order_id = None
            
            # Check if we're the taker (most common for market orders)
            if taker_order_id:
                # We don't track market order IDs in pending_orders since they execute immediately
                # but we can still process the trade data
                is_our_trade = True
                our_order_id = taker_order_id
            
            # Only process if this could be our trade
            if is_our_trade:
                size = float(trade_data.get('size', 0))
                price = float(trade_data.get('price', 0))
                
                if size > 0:
                    print(f"[{self.position_tracker.token_id[:8]}...] Market order fill: {size} @ ${price:.4f}")
                    
                    # Update position
                    self.position_tracker.update_filled_quantity(size, price)
                    
                    print(f"Position updated - filled: {self.position_tracker.filled_quantity:.2f}/{self.position_tracker.target_quantity:.2f}")
                    
        except Exception as e:
            print(f"Error handling trade execution in inside liquidity strategy: {e}")


# =============================================================================
# Main Order Manager
# =============================================================================

class OrderManager:
    """Main orchestrator for managing top-of-book orders"""
    
    def __init__(self, client: ClobClient, config: StrategyConfig, auth: dict):
        self.client = client
        self.config = config
        self.auth = auth
        self.running = False
        
        # Add unique identifier for this orchestrator instance
        self.orchestrator_id = f"ORD_{uuid.uuid4().hex[:8].upper()}"
        
        # Initialize components
        self.rate_limiter = RateLimiter(config.rate_limit_per_second)
        self.order_executor = OrderExecutor(client, self.rate_limiter)
        self.position_tracker = PositionTracker(config.token_id, config.total_quantity)
        
        # Choose strategy based on configuration
        if config.inside_liquidity_mode:
            self.strategy = InsideLiquidityStrategy(
                self.order_executor,
                self.position_tracker,
                config.child_order_size,
                config.limit_price,
                config.order_price_min_tick_size,
                config.side
            )
        else:
            self.strategy = TopOfBookStrategy(
                self.order_executor,
                self.position_tracker,
                config.child_order_size,
                config.limit_price,
                config.order_price_min_tick_size,
                config.side,
                config.price_improvement_ticks,
                config.match_top_of_book
            )
        
        self.stop_manager = StopConditionManager(config.timeout_seconds)
        
        # WebSocket streams
        self.market_stream: Optional[MarketDataStream] = None
        self.user_stream: Optional[UserDataStream] = None
        
        # Set up callbacks
        self.stop_manager.set_stop_callback(self._handle_stop_condition)
    
    async def start_strategy(self) -> None:
        """Initialize and start the order management strategy"""
        if self.running:
            print("Strategy is already running")
            return
        
        print(f"Starting order management strategy for token {self.config.token_id}")
        print(f"Target: {self.config.total_quantity} @ max ${self.config.limit_price}")
        
        self.running = True
        
        # Initialize WebSocket streams
        self.market_stream = MarketDataStream(
            [self.config.token_id],
            self.auth,
            self._handle_market_update
        )
        
        self.user_stream = UserDataStream(
            [],  # Empty condition_ids - we want all user updates
            self.auth,
            self._handle_order_update
        )
        
        # Start streams
        await self.market_stream.start()
        await self.user_stream.start()
        
        # Start monitoring loop
        asyncio.create_task(self._monitoring_loop())
        
        print("Strategy started successfully")
    
    async def stop_strategy(self) -> None:
        """Gracefully shutdown the strategy and cancel all orders"""
        if not self.running:
            return
        
        print("Stopping strategy...")
        self.running = False
        
        # Cancel all pending orders
        cancelled_orders = await self.order_executor.cancel_all_orders(self.config.token_id)
        if cancelled_orders:
            print(f"Cancelled {len(cancelled_orders)} pending orders")
        
        # Stop WebSocket streams
        if self.market_stream:
            await self.market_stream.stop()
        if self.user_stream:
            await self.user_stream.stop()
        
        print("Strategy stopped")
    
    async def update_parameters(self, limit_price: Optional[float] = None, 
                              total_quantity: Optional[float] = None) -> None:
        """Update strategy parameters while running"""
        if limit_price is not None:
            self.config.limit_price = limit_price
            self.strategy.limit_price = limit_price
            print(f"Updated limit price to ${limit_price}")
        
        if total_quantity is not None:
            self.config.total_quantity = total_quantity
            self.position_tracker.target_quantity = total_quantity
            print(f"Updated target quantity to {total_quantity}")
    
    def extend_timeout(self, additional_seconds: int) -> None:
        """Extend the strategy timeout by additional seconds"""
        if self.running and self.stop_manager:
            self.stop_manager.extend_timeout(additional_seconds)
            print(f"[{self.orchestrator_id}] Strategy timeout extended by {additional_seconds} seconds")
        else:
            print(f"[{self.orchestrator_id}] Cannot extend timeout - strategy not running")
    
    def get_status(self) -> dict:
        """Get current strategy status and statistics"""
        position = self.position_tracker.get_position_summary()
        pending_orders = self.position_tracker.get_pending_orders()
        
        return {
            "running": self.running,
            "token_id": self.config.token_id,
            "has_critical_error": self.strategy.has_critical_error() if self.strategy else False,
            "critical_error_message": self.strategy.get_critical_error_message() if self.strategy else "",
            "position": {
                "target_quantity": position.target_quantity,
                "filled_quantity": position.filled_quantity,
                "remaining_quantity": self.position_tracker.get_remaining_quantity(),
                "pending_quantity": position.pending_quantity,
                "average_fill_price": position.average_fill_price,
                "completion_percentage": (position.filled_quantity / position.target_quantity) * 100 if position.target_quantity > 0 else 0
            },
            "orders": {
                "pending_count": len(pending_orders),
                "pending_orders": [
                    {
                        "id": order.order_id,
                        "price": order.price,
                        "size": order.size,
                        "status": order.status
                    } for order in pending_orders
                ]
            },
            "time_remaining": self.stop_manager.get_remaining_time()
        }
    
    async def _handle_market_update(self, market_data: MarketData) -> None:
        """Handle market data updates"""
        if not self.running:
            return
        
        await self.strategy.process_market_update(market_data)
    
    async def _handle_order_update(self, update_type: str, order_data: dict) -> None:
        """Handle order status updates"""
        if not self.running:
            return
        
        order_id = order_data.get('id')
        print(f"[{self.orchestrator_id}] Received {update_type} for order {order_id}")
        
        await self.strategy.process_order_update(update_type, order_data)
    
    async def _handle_stop_condition(self, reason: str) -> None:
        """Handle stop condition triggers"""
        print(f"Stop condition triggered: {reason}")
        await self.stop_strategy()
    
    async def _monitoring_loop(self) -> None:
        """Monitor strategy conditions and handle timeouts"""
        while self.running:
            try:
                # Check if we should exit the strategy
                if self.should_exit_strategy():
                    await self.stop_strategy()
                    break
                
                # Check stop conditions
                if self.stop_manager.should_stop():
                    await self._handle_stop_condition("timeout")
                    break
                
                await asyncio.sleep(1)  # Check every second
            except Exception as e:
                print(f"Error in monitoring loop: {e}")
                await asyncio.sleep(1)

    def should_exit_strategy(self) -> bool:
        """Check if strategy should exit (no more work to do)"""
        # Exit if critical error occurred
        if self.strategy.has_critical_error():
            print(f"Exiting due to critical error: {self.strategy.get_critical_error_message()}")
            return True
            
        # Exit if target is reached
        if self.position_tracker.is_target_reached():
            print("Target quantity reached, exiting strategy")
            return True
        
        # Exit if timeout exceeded
        if self.stop_manager.should_stop():
            print("Strategy timeout exceeded, exiting strategy")
            return True
        
        # Exit if no pending orders and remaining quantity is 0
        remaining_qty = self.position_tracker.get_remaining_quantity()
        pending_orders = self.position_tracker.get_pending_orders()
        if remaining_qty <= 0 and not pending_orders:
            print("No remaining quantity and no pending orders, exiting strategy")
            return True
        
        return False


# =============================================================================
# Command Line Interface
# =============================================================================

class OrderHandlerCLI:
    """Command line interface for the order handler"""
    
    def __init__(self, account_key: str, account_proxy: str):
        self.order_manager: Optional[OrderManager] = None
        self.no_orders_start_time: Optional[float] = None
        self.no_orders_timeout = 5.0  # Exit after 5 seconds with no pending orders
        self.account_key = account_key
        self.account_proxy = account_proxy
    
    async def run_interactive_mode(self, config: StrategyConfig) -> None:
        """Run interactive command line mode"""
        # Initialize client and auth
        client, auth = self._setup_client(self.account_key, self.account_proxy)
        
        # Create order manager
        self.order_manager = OrderManager(client, config, auth)
        
        # Start strategy
        await self.order_manager.start_strategy()
        
        print("\n=== Interactive Mode ===")
        print("Commands: status, stop, update_price <price>, update_qty <qty>, help")
        
        # Create input queue and start background input task
        input_queue = asyncio.Queue()
        input_task = asyncio.create_task(self._input_handler(input_queue))
        
        try:
            # Interactive command loop
            while self.order_manager and self.order_manager.running:
                try:
                    # Check if strategy should naturally exit
                    if self.order_manager.should_exit_strategy():
                        print("\nStrategy completed successfully!")
                        await self.order_manager.stop_strategy()
                        break
                    
                    # Check for timeout condition
                    if self.order_manager.stop_manager.should_stop():
                        print("\nStrategy timeout reached, exiting...")
                        await self.order_manager.stop_strategy()
                        break
                    
                    # Check for no pending orders timeout (5 seconds)
                    if self._should_exit_no_orders():
                        print("\nNo pending orders for 5 seconds, exiting...")
                        await self.order_manager.stop_strategy()
                        break
                    
                    # Check for user input (non-blocking)
                    try:
                        user_input = await asyncio.wait_for(input_queue.get(), timeout=1.0)
                        user_input = user_input.strip()
                        
                        if user_input == "status":
                            await self._show_status()
                        elif user_input == "stop":
                            await self.order_manager.stop_strategy()
                            break
                        elif user_input.startswith("update_price "):
                            parts = user_input.split(" ", 1)
                            if len(parts) > 1:
                                price = float(parts[1])
                                await self.order_manager.update_parameters(limit_price=price)
                        elif user_input.startswith("update_qty "):
                            parts = user_input.split(" ", 1)
                            if len(parts) > 1:
                                qty = float(parts[1])
                                await self.order_manager.update_parameters(total_quantity=qty)
                        elif user_input == "help":
                            self._show_help()
                        else:
                            print("Unknown command. Type 'help' for available commands.")
                            
                    except asyncio.TimeoutError:
                        # No user input received, continue monitoring
                        continue
                        
                except KeyboardInterrupt:
                    print("\nShutting down...")
                    await self.order_manager.stop_strategy()
                    break
                except Exception as e:
                    print(f"Error: {e}")
            else:
                if self.order_manager and self.order_manager.running:
                    await self.order_manager.stop_strategy()
        finally:
            # Clean up input task
            print("Exiting interactive mode...")
            input_task.cancel()
            try:
                # Wait for cancellation but don't hang forever
                await asyncio.wait_for(input_task, timeout=2.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                # Task cancelled or timed out, which is fine
                pass
    
    async def _input_handler(self, input_queue: asyncio.Queue) -> None:
        """Background task to handle user input using non-blocking approach"""
        input_buffer = ""
        prompt_shown = False
        
        while self.order_manager and self.order_manager.running:
            try:
                # Show prompt if not already shown
                if not prompt_shown:
                    print("> ", end="", flush=True)
                    prompt_shown = True
                
                if msvcrt:  # Windows
                    if msvcrt.kbhit():
                        char = msvcrt.getch().decode('utf-8', errors='ignore')
                        if char == '\r':  # Enter key
                            print()  # New line
                            if input_buffer.strip():
                                await input_queue.put(input_buffer.strip())
                            input_buffer = ""
                            prompt_shown = False
                        elif char == '\b':  # Backspace
                            if input_buffer:
                                input_buffer = input_buffer[:-1]
                                print('\b \b', end="", flush=True)
                        elif char.isprintable():
                            input_buffer += char
                            print(char, end="", flush=True)
                else:  # Unix/Linux - fallback to simpler approach
                    raise NotImplementedError("Non-Windows systems are not supported")
                
                await asyncio.sleep(0.05)  # Small delay to prevent busy waiting
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                # If input handling fails, just continue
                await asyncio.sleep(0.1)
    
    async def run_non_interactive_mode(self, config: StrategyConfig) -> None:
        """Run non-interactive mode - strategy runs until completion or timeout"""
        # Initialize client and auth
        client, auth = self._setup_client(self.account_key, self.account_proxy)
        
        # Create order manager
        self.order_manager = OrderManager(client, config, auth)
        
        # Start strategy
        await self.order_manager.start_strategy()
        
        print(f"\n=== Non-Interactive Mode ===")
        print(f"Strategy running for token {config.token_id}")
        print("Strategy will run until completion, timeout, or no orders for 5 seconds")
        print("Press Ctrl+C to stop manually")
        
        # Monitor strategy until completion
        try:
            while self.order_manager and self.order_manager.running:
                # Check if strategy should naturally exit
                if self.order_manager.should_exit_strategy():
                    print("\nStrategy completed successfully!")
                    await self.order_manager.stop_strategy()
                    break
                
                # Check for timeout condition
                if self.order_manager.stop_manager.should_stop():
                    print("\nStrategy timeout reached, exiting...")
                    await self.order_manager.stop_strategy()
                    break
                
                # Check for no pending orders timeout (5 seconds)
                if self._should_exit_no_orders():
                    print("\nNo pending orders for 5 seconds, exiting...")
                    await self.order_manager.stop_strategy()
                    break
                
                await asyncio.sleep(1)  # Check every second
                
        except KeyboardInterrupt:
            print("\nShutting down...")
            await self.order_manager.stop_strategy()
        
        # Final status report
        if self.order_manager:
            print("\n=== Final Status ===")
            await self._show_status()
    
    def _should_exit_no_orders(self) -> bool:
        """Check if we should exit due to no pending orders for 5 seconds"""
        if not self.order_manager:
            return False
            
        pending_orders = self.order_manager.position_tracker.get_pending_orders()
        current_time = time.time()
        
        if not pending_orders:
            # No pending orders
            if self.no_orders_start_time is None:
                # Start tracking no-orders time
                self.no_orders_start_time = current_time
            elif current_time - self.no_orders_start_time >= self.no_orders_timeout:
                # No orders for longer than timeout period
                return True
        else:
            # We have pending orders, reset the timer
            self.no_orders_start_time = None
        
        return False
    
    def _setup_client(self, account_key: str, account_proxy: str) -> tuple:
        """Setup Polymarket client and authentication"""
        host = "https://clob.polymarket.com"
        chain_id = 137
        
        if not account_key or not account_proxy:
            raise ValueError("Account credentials (private key and proxy address) are required")
        
        client = ClobClient(
            host, 
            key=account_key, 
            chain_id=chain_id, 
            signature_type=2, 
            funder=account_proxy
        )
        
        client.set_api_creds(client.create_or_derive_api_creds())
        
        auth = {
            "apiKey": client.creds.api_key,
            "secret": client.creds.api_secret,
            "passphrase": client.creds.api_passphrase
        }
        
        return client, auth
    
    async def _show_status(self) -> None:
        """Display current strategy status"""
        if not self.order_manager:
            print("Order manager not initialized")
            return
        
        status = self.order_manager.get_status()
        
        print(f"\n=== Strategy Status ===")
        print(f"Running: {status['running']}")
        print(f"Token ID: {status['token_id']}")
        print(f"Target: {status['position']['target_quantity']}")
        print(f"Filled: {status['position']['filled_quantity']}")
        print(f"Remaining: {status['position']['remaining_quantity']}")
        print(f"Pending: {status['position']['pending_quantity']}")
        print(f"Avg Fill Price: ${status['position']['average_fill_price']:.4f}")
        print(f"Completion: {status['position']['completion_percentage']:.1f}%")
        print(f"Pending Orders: {status['orders']['pending_count']}")
        print(f"Time Remaining: {status['time_remaining']} seconds")
        
        if status['orders']['pending_orders']:
            print("\nPending Orders:")
            for order in status['orders']['pending_orders']:
                print(f"  {order['id'][:8]}... @ ${order['price']:.4f} x {order['size']}")
    
    def _show_help(self) -> None:
        """Show available commands"""
        print("\nAvailable Commands:")
        print("  status                 - Show current strategy status")
        print("  stop                   - Stop the strategy")
        print("  update_price <price>   - Update limit price")
        print("  update_qty <quantity>  - Update target quantity") 
        print("  help                   - Show this help message")


class MarketOrderExecutor(OrderExecutor):
    """Executes market orders using limit orders at market prices"""
    
    def __init__(self, client: ClobClient, rate_limiter: RateLimiter):
        super().__init__(client, rate_limiter)
        self.market_data_cache: Dict[str, MarketData] = {}
        
    async def place_market_order(self, 
                                token_id: str, 
                                size: float, 
                                side: str = BUY,
                                max_slippage: float = 0.05,
                                timeout_seconds: int = 30) -> Optional[str]:
        """
        Place a market order by using limit order at market price
        
        Args:
            token_id: Token to trade
            size: Order size
            side: BUY or SELL
            max_slippage: Maximum acceptable slippage (0.05 = 5%)
            timeout_seconds: Order timeout
            
        Returns:
            Order ID if successful, None otherwise
        """
        try:
            # Get current market data
            market_data = await self._get_market_data(token_id)
            if not market_data:
                logger.error(f"Could not get market data for token {token_id}")
                return None
            
            # Calculate market price with slippage protection
            market_price = self._calculate_market_price(market_data, side, max_slippage)
            if not market_price:
                logger.error(f"Could not calculate market price for {side} order")
                return None
            
            logger.info(f"Placing market {side} order: {size} @ ${market_price:.4f} (market price with slippage)")
            
            # Place limit order at market price
            return await self.place_order(
                token_id=token_id,
                price=market_price,
                size=size,
                side=side,
                timeout_seconds=timeout_seconds
            )
            
        except Exception as e:
            logger.error(f"Error placing market order: {e}")
            return None
    
    async def _get_market_data(self, token_id: str) -> Optional[MarketData]:
        """Get current market data for the token"""
        try:
            # Get order book from API            
            orderbook_url = "https://clob.polymarket.com/book"
            params = {"token_id": token_id}
            
            response = requests.get(orderbook_url, params=params, timeout=5)
            response.raise_for_status()
            book_data = response.json()
            
            bids = book_data.get('bids', [])
            asks = book_data.get('asks', [])
            
            if not bids or not asks:
                logger.warning(f"Empty order book for token {token_id}")
                return None
            
            # Create MarketData object
            from datetime import datetime
            market_data = MarketData(
                asset_id=token_id,
                top_bid=float(bids[0]['price']),
                top_ask=float(asks[0]['price']),
                bid_size=float(bids[0]['size']),
                ask_size=float(asks[0]['size']),
                timestamp=datetime.now()
            )
            
            # Cache the data
            self.market_data_cache[token_id] = market_data
            return market_data
            
        except Exception as e:
            logger.error(f"Error fetching market data for {token_id}: {e}")
            return None
    
    def _calculate_market_price(self, market_data: MarketData, side: str, max_slippage: float) -> Optional[float]:
        """Calculate the limit price for a market order"""
        try:
            if side == BUY:
                # For buy orders, use ask price (we're taking liquidity)
                base_price = market_data.top_ask
                # Add slippage buffer (pay up to X% more)
                market_price = base_price * (1 + max_slippage)
                # Cap at 1.0 (Polymarket max price)
                return min(market_price, 1.0)
            
            elif side == SELL:
                # For sell orders, use bid price (we're taking liquidity)
                base_price = market_data.top_bid
                # Subtract slippage buffer (accept up to X% less)
                market_price = base_price * (1 - max_slippage)
                # Floor at 0.001 (reasonable minimum)
                return max(market_price, 0.001)
            
            else:
                logger.error(f"Invalid side: {side}")
                return None
                
        except Exception as e:
            logger.error(f"Error calculating market price: {e}")
            return None
    
    async def place_aggressive_order(self, 
                                   token_id: str, 
                                   size: float, 
                                   side: str = BUY,
                                   price_improvement_pct: float = 0.02) -> Optional[str]:
        """
        Place an aggressive order that improves on the current best price
        
        Args:
            token_id: Token to trade
            size: Order size
            side: BUY or SELL
            price_improvement_pct: How much to improve price by (0.02 = 2%)
            
        Returns:
            Order ID if successful, None otherwise
        """
        try:
            # Get current market data
            market_data = await self._get_market_data(token_id)
            if not market_data:
                return None
            
            # Calculate aggressive price
            if side == BUY:
                # Bid higher than current best bid
                aggressive_price = market_data.top_bid * (1 + price_improvement_pct)
                aggressive_price = min(aggressive_price, 1.0)
            else:  # SELL
                # Ask lower than current best ask
                aggressive_price = market_data.top_ask * (1 - price_improvement_pct)
                aggressive_price = max(aggressive_price, 0.001)
            
            logger.info(f"Placing aggressive {side} order: {size} @ ${aggressive_price:.4f}")
            
            return await self.place_order(
                token_id=token_id,
                price=aggressive_price,
                size=size,
                side=side
            )
            
        except Exception as e:
            logger.error(f"Error placing aggressive order: {e}")
            return None
    
    def get_spread(self, token_id: str) -> Optional[float]:
        """Get the current bid-ask spread for a token"""
        market_data = self.market_data_cache.get(token_id)
        if market_data:
            return market_data.top_ask - market_data.top_bid
        return None
    
    def get_cached_market_data(self, token_id: str) -> Optional[MarketData]:
        """Get cached market data for a token"""
        return self.market_data_cache.get(token_id) 

# =============================================================================
# Main Entry Point
# =============================================================================

async def place_single_order(account_key: str, account_proxy: str, token_id: str, price: float, quantity: float, side: str = BUY) -> None:
    """Place a single order without the orchestrator strategy"""
    print(f"Placing single order: {quantity} @ ${price:.4f} ({side}) for token {token_id}")
    
    if not account_key or not account_proxy:
        raise ValueError("Account credentials (account_key and account_proxy) are required")
    
    # Setup client
    host = "https://clob.polymarket.com"
    chain_id = 137
    
    client = ClobClient(
        host, 
        key=account_key, 
        chain_id=chain_id, 
        signature_type=2, 
        funder=account_proxy
    )
    
    client.set_api_creds(client.create_or_derive_api_creds())
    
    # Create order executor
    rate_limiter = RateLimiter(5.0)  # Fixed: increased from 1.0 to 5.0
    order_executor = OrderExecutor(client, rate_limiter)
    
    # Place the order
    order_id = await order_executor.place_order(token_id, price, quantity, side)
    
    if order_id:
        print(f"✅ Order placed successfully!")
        print(f"Order ID: {order_id}")
        print(f"Details: {quantity} @ ${price:.4f} ({side})")
    else:
        print("❌ Failed to place order")


async def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(
        description="Polymarket Order Management System"
    )
    
    # Mode selection
    parser.add_argument(
        "--single-order", action="store_true",
        help="Place a single order (instead of running the orchestrator)"
    )
    
    # Common arguments
    parser.add_argument(
        "--token-id", required=True,
        help="Token ID to trade"
    )
    
    # Single order arguments
    parser.add_argument(
        "--price", type=float,
        help="Order price (required for single order mode)"
    )
    parser.add_argument(
        "--quantity", type=float,
        help="Order quantity (required for single order mode)"
    )
    parser.add_argument(
        "--side", choices=[BUY, SELL], default=BUY,
        help="Order side: BUY or SELL (default: BUY)"
    )
    
    # Strategy mode arguments
    parser.add_argument(
        "--limit-price", type=float,
        help="Maximum price to pay for BUY orders or minimum price to accept for SELL orders (required for strategy mode)"
    )
    parser.add_argument(
        "--total-quantity", type=float,
        help="Total quantity to purchase (required for strategy mode)"
    )
    parser.add_argument(
        "--child-order-size", type=float,
        help="Size of individual child orders (required for strategy mode)"
    )
    parser.add_argument(
        "--timeout", type=int, default=3600,
        help="Strategy timeout in seconds (default: 1 hour)"
    )
    parser.add_argument(
        "--rate-limit", type=float, default=5.0,
        help="Maximum orders per second (default: 5.0)"
    )
    parser.add_argument(
        "--order-price-min-tick-size", type=float, required=True,
        help="Minimum price increment: 0.01 or 0.001 (required for strategy mode)"
    )
    parser.add_argument(
        "--match-top-of-book", action="store_true",
        help="Match the top of book instead of beating it by tick size"
    )
    parser.add_argument(
        "--non-interactive", action="store_true",
        help="Run in non-interactive mode (strategy runs automatically without user input)"
    )
    parser.add_argument(
        "--strategy-side", choices=[BUY, SELL], default=BUY,
        help="Order side for strategy mode: BUY or SELL (default: BUY)"
    )

    parser.add_argument(
        "--account-key", type=str, required=True,
        help="Account key for the account to use"
    )
    parser.add_argument(
        "--account-proxy", type=str, required=True,
        help="Account proxy for the account to use"
    )
    
    args = parser.parse_args()
    
    if args.single_order:
        # Single order mode
        if not args.price or not args.quantity:
            print("Error: --price and --quantity are required for single order mode")
            return
        
        await place_single_order(args.account_key, args.account_proxy, args.token_id, args.price, args.quantity, args.side)
    else:
        # Strategy mode
        if not all([args.limit_price, args.total_quantity, args.child_order_size, args.order_price_min_tick_size]):
            print("Error: --limit-price, --total-quantity, --child-order-size, and --order-price-min-tick-size are required for strategy mode")
            return
        
        # Validate tick size
        if args.order_price_min_tick_size not in [0.01, 0.001]:
            print("Error: --order-price-min-tick-size must be either 0.01 or 0.001")
            return
        
        config = StrategyConfig(
            token_id=args.token_id,
            limit_price=args.limit_price,
            total_quantity=args.total_quantity,
            child_order_size=args.child_order_size,
            order_price_min_tick_size=args.order_price_min_tick_size,
            side=args.strategy_side,
            timeout_seconds=args.timeout,
            rate_limit_per_second=args.rate_limit,
            match_top_of_book=args.match_top_of_book
        )
        
        cli = OrderHandlerCLI(args.account_key, args.account_proxy)
        if args.non_interactive:
            await cli.run_non_interactive_mode(config)
        else:
            await cli.run_interactive_mode(config)


if __name__ == "__main__":
    asyncio.run(main()) 
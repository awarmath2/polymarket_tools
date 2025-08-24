> [!WARNING]
> NO WARRANTY WHATSOEVER. This software is provided "AS IS" without any kind of warranty, express or implied. Use at your own risk. The authors and contributors are not liable for any losses or damages arising from use of this software and are not affiliated with Polymarket. 

# Polymarket Order Orchestrator

Order management system for Polymarket implementing top-of-book and inside liquidity strategies, plus other tools.

### Features

- Top-of-book and inside liquidity strategies via GUI
- Support for multiple accounts simultaneously
- Seperate app to view/cancel active orders across accounts as well as net positions 

### Prerequisites

- Python 3.8+
- Environment variables: `XXXXX_PRIVATE_KEY`, `XXXXX_PROXY_ADDRESS`, where XXXXX is the account alias (apps support multiple accounts)

## Order Manager GUI

A comprehensive graphical interface for managing multiple order orchestrators simultaneously with real-time monitoring and status updates.

![Order Manager Screenshot](./screenshots/Order%20Manager.PNG?raw=true)

**Quick Start:**
```bash
python order_manager_gui.py
```

**Live Status Display:**
- **Quantity**: `filled/total (completion%) (pending orders)`
- **Status**: Running, Completed, Cancelled, Error with color coding
- **Timeout**: Live countdown timers
- **Actions**: One-click cancellation

**Interactive Features:**
- **View Details**: Double-click any row for comprehensive status
- **Cancel Orders**: Double-click Actions column
- **Form Validation**: Input validation with helpful error messages
- **Comprehensive Logging**: All operations logged to `logs/order_manager_gui.log`

## Market Viewer

A standalone GUI tool for exploring converting from market slug to token ID.

**Quick Start:**
```bash
python market_viewer.py
```

**Usage:**
1. Enter or select a market slug (e.g., `will-either-cuomo-or-mamdani-announce-they-are-running-for-mayor-on-non-democrat-slate`)
2. Click "Fetch Market Data" to load market information
3. Toggle "Show Condition/Question IDs" for technical details
4. Double-click any cell to copy its value
5. Browse hierarchical market structure in the tree view

## Positions & Orders GUI

A standalone GUI tool for viewing/cancelling open orders and viewing positions aggregated across accounts.

**Quick Start:**
```bash
python positions_orders_gui.py
```

## CLI

### Top-of-Book Orchestrator

**Beat the top of book (default behavior):**
```bash
python backend/order_orchestrator.py \
  --token-id "TOKEN_ID" \
  --limit-price 0.75 \
  --total-quantity 100 \
  --child-order-size 10 \
  --order-price-min-tick-size 0.01 \
  --timeout 3600
```

**Match the top of book:**
```bash
python backend/order_orchestrator.py \
  --token-id "TOKEN_ID" \
  --limit-price 0.75 \
  --total-quantity 100 \
  --child-order-size 10 \
  --order-price-min-tick-size 0.01 \
  --match-top-of-book \
  --timeout 3600
```

**Non-Interactive Mode:**
```bash
python backend/order_orchestrator.py \
  --token-id "TOKEN_ID" \
  --limit-price 0.75 \
  --total-quantity 100 \
  --child-order-size 10 \
  --order-price-min-tick-size 0.01 \
  --non-interactive \
  --timeout 3600
```

**Interactive Commands:**
- `status` - View current position and orders
- `stop` - Gracefully shutdown strategy
- `update_price <price>` - Adjust limit price
- `update_qty <qty>` - Modify target quantity

**Automatic Exit Conditions:**
- Strategy completes successfully (target quantity reached)
- Timeout is reached
- No pending orders exist for 5 consecutive seconds

### Single Order Mode

```bash
python backend/order_orchestrator.py \
  --single-order \
  --token-id "TOKEN_ID" \
  --price 0.75 \
  --quantity 50 \
  --side BUY
```

### Parameters

| Parameter | Description | Default |
|-----------|-------------|---------|
| `token_id` | Asset identifier | Required |
| `limit_price` | Maximum bid price (BUY) or minimum ask price (SELL) | Required |
| `total_quantity` | Target quantity | Required |
| `child_order_size` | Individual order size | Required |
| `order_price_min_tick_size` | Minimum price increment: 0.01 or 0.001 | Required |
| `strategy_side` | Order side: BUY or SELL | BUY |
| `timeout_seconds` | Strategy timeout | 3600 |
| `rate_limit_per_second` | API rate limit | 5.0 |
| `match_top_of_book` | Match top of book instead of beating it | False |
| `non_interactive` | Run without user interaction | False |

### Single Order Mode Parameters

| Parameter | Description | Default |
|-----------|-------------|---------|
| `token_id` | Asset identifier | Required |
| `price` | Order price | Required |
| `quantity` | Order quantity | Required |
| `side` | Order side (BUY/SELL) | BUY |

### Project Structure

```
polymarket/
├── backend/
│   ├── account_manager.py       # Multi-account management
│   ├── market_analyzer.py       # Market analysis and selection
│   ├── order_orchestrator.py    # Main orchestrator and CLI
│   ├── market_metadata.py       # Market metadata fetcher using Gamma API
│   ├── token_manager.py         # Token metadata management and caching
│   ├── user_positions.py        # User positions management and caching
│   ├── utilities.py             # Core utilities and data structures
│   └── websocket_handlers.py    # WebSocket streaming handlers
├── __cache__/                   # Cache for user_positions
├── market_viewer.py             # Token ID lookup utility
├── order_manager_gui.py         # Primary app with order orchestrator
├── positions_orders_gui.py      # Order/Positions view utility
├── requirements.txt             # Python dependencies
└── README.md                    # This file
```
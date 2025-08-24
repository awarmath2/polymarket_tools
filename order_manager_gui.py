# Order Manager GUI - Polymarket Order Orchestrator Management Interface
import tkinter as tk
from tkinter import ttk, messagebox
import asyncio
import threading
import time
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional
import json
import logging

# Import backend modules
from backend.order_orchestrator import OrderManager
from backend.utilities import StrategyConfig, MIN_ORDER_SIZE
from backend.market_metadata import get_market_metadata_sync, MarketMetadataFetcher
from backend.user_positions import UserPositionsCache
from py_clob_client.order_builder.constants import BUY, SELL
from py_clob_client.client import ClobClient

# Setup logging with file output
import logging.handlers

def setup_logging():
    """Setup logging to both file and console"""
    # Create logs directory if it doesn't exist
    import os
    os.makedirs('logs', exist_ok=True)
    
    # Create logger
    logger = logging.getLogger(__name__)
    # logger.setLevel(logging.DEBUG)  # Changed to DEBUG temporarily
    
    # Prevent duplicate handlers
    if logger.handlers:
        return logger
    
    # File handler with rotation
    file_handler = logging.handlers.RotatingFileHandler(
        'logs/order_manager_gui.log',
        maxBytes=10*1024*1024,  # 10MB
        backupCount=5
    )
    file_handler.setLevel(logging.DEBUG)  # Changed to DEBUG temporarily
    
    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG)  # Changed to DEBUG temporarily
    
    # Create formatter
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s'
    )
    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)
    
    # Add handlers to logger
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    
    return logger

logger = setup_logging()

class OrderManagerGUI:
    def __init__(self, root):
        logger.info("Initializing Order Manager GUI")
        self.root = root
        self.root.title("Polymarket Order Manager")
        self.root.geometry("1400x800")
        
        # Set fullscreen by default
        try:
            self.root.state('zoomed')  # Windows
        except tk.TclError:
            try:
                self.root.attributes('-zoomed', True)  # Linux
            except tk.TclError:
                self.root.attributes('-fullscreen', True)  # macOS fallback
        
        # Add F11 key binding to toggle fullscreen
        self.root.bind('<F11>', self.toggle_fullscreen)
        
        # Configure style
        style = ttk.Style()
        style.theme_use('clam')
        logger.info("GUI theme configured")
        
        # Track active order managers
        self.active_orders: Dict[str, Dict] = {}  # order_id -> {manager, config, start_time, status}
        self.next_order_id = 1
        
        # Event loop for async operations
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.loop_thread: Optional[threading.Thread] = None
        
        # UI update timer
        self.update_timer = None
        
        # Market metadata fetcher
        self.metadata_fetcher = MarketMetadataFetcher()
        self.current_metadata = None
        
        # User positions cache (will be properly initialized after account setup)
        self.positions_cache = None  # Will be initialized when account is selected
        
        # Account management
        self.available_accounts = self._scan_available_accounts()
        self.selected_account = tk.StringVar()
        
        # Set default account if any available
        if self.available_accounts:
            self.selected_account.set(list(self.available_accounts.keys())[0])
        
        # Initialize positions cache with selected account
        self._initialize_positions_cache()
        
        # Check credentials at startup
        self.credentials_available = self._check_credentials()
        
        self.setup_ui()
        self.setup_async_loop()
        self.start_ui_updates()
        logger.info("Order Manager GUI initialization complete")
        
    def _scan_available_accounts(self) -> Dict[str, Dict[str, str]]:
        """Scan environment variables for available account credentials"""
        logger.info("Scanning environment variables for available accounts")
        accounts = {}
        
        # Check for generic pattern accounts (XXXX_PRIVATE_KEY, XXXX_PROXY_ADDRESS)
        env_vars = os.environ.keys()
        potential_accounts = set()
        
        for var in env_vars:
            if var.endswith("_PRIVATE_KEY"):
                account_prefix = var[:-12]  # Remove "_PRIVATE_KEY"
                potential_accounts.add(account_prefix)
            elif var.endswith("_PROXY_ADDRESS"):
                account_prefix = var[:-14]  # Remove "_PROXY_ADDRESS"
                potential_accounts.add(account_prefix)
        
        # Validate each potential account has both required variables
        for account_name in potential_accounts:            
            private_key = os.environ.get(f"{account_name}_PRIVATE_KEY")
            proxy_address = os.environ.get(f"{account_name}_PROXY_ADDRESS")
            
            if private_key and proxy_address:
                accounts[account_name] = {
                    "private_key": private_key.strip(),
                    "proxy_address": proxy_address.strip()
                }
                logger.info(f"Found {account_name} account")
        
        logger.info(f"Found {len(accounts)} total accounts: {list(accounts.keys())}")
        return accounts
    
    def _check_credentials(self) -> bool:
        """Check if credentials are available for the selected account"""
        if not self.available_accounts:
            logger.warning("No accounts found in environment variables")
            return False
        
        selected = self.selected_account.get()
        if not selected or selected not in self.available_accounts:
            logger.warning(f"Selected account '{selected}' not found in available accounts")
            return False
        
        logger.info(f"Credentials available for selected account: {selected}")
        return True
    
    def _get_selected_account_credentials(self) -> tuple:
        """Get credentials for the currently selected account"""
        selected = self.selected_account.get()
        if not selected or selected not in self.available_accounts:
            raise ValueError(f"No credentials available for account: {selected}")
        
        account_data = self.available_accounts[selected]
        return account_data["private_key"], account_data["proxy_address"]
    
    def _setup_client_and_auth(self) -> tuple:
        """Setup Polymarket client and authentication"""
        logger.info("Setting up Polymarket client and authentication")
        if not self.credentials_available:
            error_msg = "Missing required environment variables for selected account"
            logger.error(error_msg)
            raise ValueError(error_msg)
        
        try:
            key, proxy_address = self._get_selected_account_credentials()
        except ValueError as e:
            logger.error(f"Failed to get credentials: {e}")
            raise
        
        host = "https://clob.polymarket.com"
        chain_id = 137
        
        logger.info(f"Creating ClobClient for host: {host}, chain_id: {chain_id}, account: {self.selected_account.get()}")
        client = ClobClient(
            host, 
            key=key, 
            chain_id=chain_id, 
            signature_type=2, 
            funder=proxy_address
        )
        
        logger.info("Setting API credentials")
        client.set_api_creds(client.create_or_derive_api_creds())
        
        auth = {
            "apiKey": client.creds.api_key,
            "secret": client.creds.api_secret,
            "passphrase": client.creds.api_passphrase
        }
        
        logger.info("Client and authentication setup completed successfully")
        return client, auth
    
    def setup_async_loop(self):
        """Setup asyncio loop in separate thread"""
        loop_ready = threading.Event()
        
        def run_loop():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            loop_ready.set()  # Signal that loop is ready
            self.loop.run_forever()
        
        self.loop_thread = threading.Thread(target=run_loop, daemon=True)
        self.loop_thread.start()
        
        # Wait for loop to be ready
        loop_ready.wait(timeout=5.0)  # Wait up to 5 seconds
        if self.loop is None:
            raise RuntimeError("Failed to initialize async event loop")
    
    def setup_ui(self):
        # Main container with minimal padding
        main_frame = ttk.Frame(self.root, padding="3")
        main_frame.grid(row=0, column=0, sticky="nsew")
        
        # Configure grid weights - table gets most space
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        main_frame.columnconfigure(0, weight=1)
        main_frame.rowconfigure(2, weight=1)  # Table row gets the space
        
        # Create main sections (status embedded in table header)
        self.setup_orders_table(main_frame)
        self.setup_add_order_form(main_frame)

    def setup_orders_table(self, parent):
        """Setup the orders table/grid"""
        # Create header with embedded status indicators
        header_frame = ttk.Frame(parent)
        header_frame.grid(row=0, column=0, sticky="ew", pady=(0, 2))
        header_frame.columnconfigure(1, weight=1)
        
        # Title and status indicators in one row
        ttk.Label(header_frame, text="Active Order Orchestrators", 
                 font=("TkDefaultFont", 9, "bold")).grid(row=0, column=0, sticky="w")
        
        # Status indicators on the right
        status_frame = ttk.Frame(header_frame)
        status_frame.grid(row=0, column=1, sticky="e")
        
        # Account selector
        ttk.Label(status_frame, text="Account:", font=("TkDefaultFont", 8)).grid(row=0, column=0, sticky="w", padx=(0, 5))
        
        if self.available_accounts:
            self.account_combo = ttk.Combobox(
                status_frame, 
                textvariable=self.selected_account, 
                values=list(self.available_accounts.keys()),
                state="readonly",
                width=12,
                font=("TkDefaultFont", 8)
            )
            self.account_combo.grid(row=0, column=1, sticky="w", padx=(0, 15))
            self.account_combo.bind('<<ComboboxSelected>>', self.on_account_changed)
        else:
            ttk.Label(status_frame, text="No Accounts", 
                     foreground="red", font=("TkDefaultFont", 8)).grid(row=0, column=1, sticky="w", padx=(0, 15))
        
        # Credentials status
        self.credentials_status_label = ttk.Label(status_frame, text="", font=("TkDefaultFont", 8))
        self.credentials_status_label.grid(row=0, column=2, sticky="w", padx=(0, 15))
        self.update_credentials_status()
        
        # Status indicator (will be updated dynamically)
        self.status_indicator = ttk.Label(status_frame, text="🟢 Ready", 
                                        foreground="green", font=("TkDefaultFont", 8))
        self.status_indicator.grid(row=0, column=3, sticky="w")
        
        # Add separator line for visual distinction
        separator = ttk.Separator(parent, orient='horizontal')
        separator.grid(row=1, column=0, sticky="ew", pady=(2, 2))
        
        # Table frame without LabelFrame to eliminate padding
        table_frame = ttk.Frame(parent)
        table_frame.grid(row=2, column=0, sticky="nsew", pady=(0, 3))
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)
        
        # Define columns
        columns = (
            "Order ID",
            "Market Slug",
            "Outcome", 
            "Side",
            "Quantity",
            "Limit Price",
            "Child Order Size",
            "Tick Size",
            "Match Top Book",
            "Timeout",
            "Status",
            "Actions"
        )
        
        # Create treeview with much larger height to fill space
        self.orders_tree = ttk.Treeview(table_frame, columns=columns, show="headings", height=20)
        
        # Configure column headings and widths
        column_widths = {
            "Order ID": 80,
            "Market Slug": 200,
            "Outcome": 100, 
            "Quantity": 100,
            "Limit Price": 100,
            "Child Order Size": 80,
            "Tick Size": 80,
            "Match Top Book": 80,
            "Side": 80,
            "Timeout": 80,
            "Status": 120,
            "Actions": 120
        }
        
        for col in columns:
            self.orders_tree.heading(col, text=col)
            self.orders_tree.column(col, width=column_widths[col], minwidth=50)
        
        # Configure tag colors for different statuses
        self.orders_tree.tag_configure('running', background='#E8F5E8', foreground='#2E7D32')
        self.orders_tree.tag_configure('completed', background='#E3F2FD', foreground='#1976D2')
        self.orders_tree.tag_configure('cancelled', background='#FFF3E0', foreground='#F57C00')
        self.orders_tree.tag_configure('error', background='#FFEBEE', foreground='#D32F2F')
        self.orders_tree.tag_configure('default', background='white', foreground='black')
        
        # Configure the Treeview
        self.orders_tree.configure(height=15)
        
        # Create scrollbars
        v_scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.orders_tree.yview)
        h_scrollbar = ttk.Scrollbar(table_frame, orient="horizontal", command=self.orders_tree.xview)
        self.orders_tree.configure(yscrollcommand=v_scrollbar.set, xscrollcommand=h_scrollbar.set)
        
        # Grid layout for tree and scrollbars
        self.orders_tree.grid(row=0, column=0, sticky="nsew")
        v_scrollbar.grid(row=0, column=1, sticky="ns")
        h_scrollbar.grid(row=1, column=0, sticky="ew")
        
        # Configure grid weights
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)
        
        # Store reference and bind events
        self.orders_tree.bind("<Double-1>", self.on_row_double_click)
    
    def on_account_changed(self, event=None):
        """Handle account selection change"""
        selected_account = self.selected_account.get()
        logger.info(f"Account changed to: {selected_account}")
        
        # Update credentials status
        self.credentials_available = self._check_credentials()
        self.update_credentials_status()
        
        # Reinitialize positions cache with new account
        self._initialize_positions_cache()
        
        # Update submit button text
        if hasattr(self, 'submit_button'):
            button_text = f"Create Order Orchestrator" if self.credentials_available else f"Create Order Orchestrator - Simulation)"
            self.submit_button.config(text=button_text)
    
    def update_credentials_status(self):
        """Update the credentials status display"""
        if self.credentials_available:
            selected_account = self.selected_account.get()
            self.credentials_status_label.config(
                text=f"🟢 {selected_account} Ready", 
                foreground="green"
            )
        else:
            if self.available_accounts:
                self.credentials_status_label.config(
                    text="🟠 No Valid Account", 
                    foreground="orange"
                )
            else:
                self.credentials_status_label.config(
                    text="🔴 No Accounts Found", 
                    foreground="red"
                )
    
    def _initialize_positions_cache(self):
        """Initialize positions cache with the selected account's proxy address"""
        selected = self.selected_account.get()
        if selected and selected in self.available_accounts:
            proxy_address = self.available_accounts[selected]["proxy_address"]
            self.positions_cache = UserPositionsCache(proxy_address)
            logger.info(f"Initialized positions cache for account {selected}")
        else:
            # Fallback - use a dummy proxy address if no account selected
            self.positions_cache = UserPositionsCache("dummy")
            logger.warning("Initialized positions cache with dummy proxy address - positions will not work")
    
    def setup_add_order_form(self, parent):
        """Setup the form to add new order orchestrators"""
        # Form frame with minimal padding
        form_frame = ttk.LabelFrame(parent, text="Add New Order Orchestrator", padding="5")
        form_frame.grid(row=3, column=0, sticky="ew")
        
        # Configure main form grid - left and right columns
        form_frame.columnconfigure(0, weight=1, minsize=400)  # Left column (metadata)
        form_frame.columnconfigure(1, weight=2, minsize=600)  # Right column (form fields)
        
        # LEFT SIDE: Market metadata section
        metadata_frame = ttk.LabelFrame(form_frame, text="Market Information", padding="10")
        metadata_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        metadata_frame.columnconfigure(1, weight=1)
        
        # Token ID input with fetch button
        ttk.Label(metadata_frame, text="Token ID:", font=("TkDefaultFont", 9, "bold")).grid(
            row=0, column=0, sticky="w", pady=(0, 5))
        self.token_id_var = tk.StringVar()
        self.token_id_entry = ttk.Entry(metadata_frame, textvariable=self.token_id_var, width=35)
        self.token_id_entry.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 5))
        self.token_id_entry.bind('<Return>', lambda event: self.fetch_market_metadata())
        
        self.fetch_metadata_button = ttk.Button(metadata_frame, text="Fetch Market Data", 
                                               command=self.fetch_market_metadata)
        self.fetch_metadata_button.grid(row=2, column=0, columnspan=2, pady=(0, 15))
        
        # Market metadata display fields
        ttk.Label(metadata_frame, text="Market Slug:", font=("TkDefaultFont", 9)).grid(
            row=3, column=0, sticky="w", pady=(0, 3))
        self.market_slug_var = tk.StringVar()
        self.market_slug_label = ttk.Label(metadata_frame, textvariable=self.market_slug_var, 
                                          foreground="blue", font=("TkDefaultFont", 8), wraplength=350)
        self.market_slug_label.grid(row=4, column=0, columnspan=2, sticky="w", pady=(0, 8))
        
        ttk.Label(metadata_frame, text="Outcome:", font=("TkDefaultFont", 9)).grid(
            row=5, column=0, sticky="w", pady=(0, 3))
        self.outcome_var = tk.StringVar()
        self.outcome_label = ttk.Label(metadata_frame, textvariable=self.outcome_var, 
                                      foreground="blue", font=("TkDefaultFont", 8))
        self.outcome_label.grid(row=6, column=0, columnspan=2, sticky="w", pady=(0, 8))
        
        # Pricing information in a sub-frame
        pricing_frame = ttk.Frame(metadata_frame)
        pricing_frame.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(5, 0))
        pricing_frame.columnconfigure(0, weight=1)
        pricing_frame.columnconfigure(1, weight=1)
        
        ttk.Label(pricing_frame, text="Best Bid:", font=("TkDefaultFont", 9)).grid(
            row=0, column=0, sticky="w", pady=(0, 3))
        self.best_bid_var = tk.StringVar()
        self.best_bid_label = ttk.Label(pricing_frame, textvariable=self.best_bid_var, 
                                       foreground="green", font=("TkDefaultFont", 9, "bold"))
        self.best_bid_label.grid(row=1, column=0, sticky="w", padx=(0, 20))
        
        ttk.Label(pricing_frame, text="Best Ask:", font=("TkDefaultFont", 9)).grid(
            row=0, column=1, sticky="w", pady=(0, 3))
        self.best_ask_var = tk.StringVar()
        self.best_ask_label = ttk.Label(pricing_frame, textvariable=self.best_ask_var, 
                                       foreground="red", font=("TkDefaultFont", 9, "bold"))
        self.best_ask_label.grid(row=1, column=1, sticky="w")
        
        # Current position display
        ttk.Label(pricing_frame, text="Your Position:", font=("TkDefaultFont", 9)).grid(
            row=2, column=0, sticky="w", pady=(8, 3))
        self.current_position_var = tk.StringVar()
        self.current_position_label = ttk.Label(pricing_frame, textvariable=self.current_position_var, 
                                              foreground="blue", font=("TkDefaultFont", 9, "bold"))
        self.current_position_label.grid(row=3, column=0, columnspan=2, sticky="w")
        
        # RIGHT SIDE: Form fields
        fields_outer_frame = ttk.LabelFrame(form_frame, text="Order Configuration", padding="10")
        fields_outer_frame.grid(row=0, column=1, sticky="nsew")
        fields_outer_frame.columnconfigure(0, weight=1)
        
        # Order parameters
        params_frame = ttk.Frame(fields_outer_frame)
        params_frame.grid(row=0, column=0, sticky="ew", pady=(0, 15))
        params_frame.columnconfigure(1, weight=1)
        params_frame.columnconfigure(3, weight=1)
        
        # Row 1: Limit Price and Total Quantity
        ttk.Label(params_frame, text="Limit Price:", font=("TkDefaultFont", 9)).grid(
            row=0, column=0, sticky="w", padx=(0, 5), pady=(0, 5))
        self.limit_price_var = tk.StringVar()
        self.limit_price_entry = ttk.Entry(params_frame, textvariable=self.limit_price_var, width=15)
        self.limit_price_entry.grid(row=0, column=1, sticky="ew", padx=(0, 20), pady=(0, 5))
        
        ttk.Label(params_frame, text="Total Quantity:", font=("TkDefaultFont", 9)).grid(
            row=0, column=2, sticky="w", padx=(0, 5), pady=(0, 5))
        self.total_quantity_var = tk.StringVar()
        self.total_quantity_entry = ttk.Entry(params_frame, textvariable=self.total_quantity_var, width=15)
        self.total_quantity_entry.grid(row=0, column=3, sticky="ew", pady=(0, 5))
        
        # Row 2: Child Order Size
        ttk.Label(params_frame, text="Child Order Size:", font=("TkDefaultFont", 9)).grid(
            row=1, column=0, sticky="w", padx=(0, 5), pady=(0, 15))
        self.child_order_size_var = tk.StringVar()
        self.child_order_size_entry = ttk.Entry(params_frame, textvariable=self.child_order_size_var, width=15)
        self.child_order_size_entry.grid(row=1, column=1, sticky="ew", padx=(0, 20), pady=(0, 15))
        
        # Options section
        options_frame = ttk.Frame(fields_outer_frame)
        options_frame.grid(row=1, column=0, sticky="ew", pady=(0, 15))
        options_frame.columnconfigure(1, weight=1)
        options_frame.columnconfigure(3, weight=1)
        
        # Row 1: Side and Tick Size
        ttk.Label(options_frame, text="Side:", font=("TkDefaultFont", 9)).grid(
            row=0, column=0, sticky="w", padx=(0, 5), pady=(0, 5))
        self.side_var = tk.StringVar(value=BUY)
        self.side_combo = ttk.Combobox(options_frame, textvariable=self.side_var, 
                                      values=[BUY, SELL], state="readonly", width=12)
        self.side_combo.grid(row=0, column=1, sticky="w", padx=(0, 20), pady=(0, 5))
        self.side_combo.bind('<<ComboboxSelected>>', self.on_side_changed)
        
        ttk.Label(options_frame, text="Tick Size:", font=("TkDefaultFont", 9)).grid(
            row=0, column=2, sticky="w", padx=(0, 5), pady=(0, 5))
        self.tick_size_var = tk.StringVar(value="0.001")
        self.tick_size_combo = ttk.Combobox(options_frame, textvariable=self.tick_size_var, 
                                           values=["0.001", "0.01"], state="readonly", width=12)
        self.tick_size_combo.grid(row=0, column=3, sticky="w", pady=(0, 5))
        
        # Row 2: Match Top Book and Inside Liquidity Mode
        self.match_top_book_var = tk.BooleanVar()
        self.match_top_book_check = ttk.Checkbutton(
            options_frame, text="Match Top of Book", 
            variable=self.match_top_book_var
        )
        self.match_top_book_check.grid(row=1, column=0, columnspan=2, sticky="w", pady=(5, 5))
        
        # Row 3: Inside Liquidity Mode
        self.inside_liquidity_var = tk.BooleanVar()
        self.inside_liquidity_check = ttk.Checkbutton(
            options_frame, text="Inside Liquidity Mode", 
            variable=self.inside_liquidity_var
        )
        self.inside_liquidity_check.grid(row=2, column=0, columnspan=2, sticky="w", pady=(0, 5))
        
        # Sell All checkbox (only visible when SELL is selected)
        self.sell_all_var = tk.BooleanVar()
        self.sell_all_check = ttk.Checkbutton(
            options_frame, text="Sell All Available", 
            variable=self.sell_all_var,
            command=self.on_sell_all_changed
        )
        self.sell_all_check.grid(row=3, column=0, columnspan=2, sticky="w", pady=(0, 15))
        # Initially hide the sell all checkbox since default side is BUY
        self.sell_all_check.grid_remove()
        
        ttk.Label(options_frame, text="Timeout (sec):", font=("TkDefaultFont", 9)).grid(
            row=3, column=2, sticky="w", padx=(0, 5), pady=(0, 15))
        self.timeout_var = tk.StringVar(value="3600")
        self.timeout_entry = ttk.Entry(options_frame, textvariable=self.timeout_var, width=12)
        self.timeout_entry.grid(row=3, column=3, sticky="w", pady=(0, 15))
        
        # Submit button section
        controls_frame = ttk.Frame(fields_outer_frame)
        controls_frame.grid(row=2, column=0, sticky="ew")
        controls_frame.columnconfigure(0, weight=1)
        
        # Submit button
        if self.credentials_available:
            selected_account = self.selected_account.get()
            button_text = f"Create Order Orchestrator ({selected_account})" if selected_account else "Create Order Orchestrator"
        else:
            selected_account = self.selected_account.get()
            button_text = f"Create Order Orchestrator ({selected_account} - Simulation)" if selected_account else "Create Order Orchestrator (Simulation)"
        
        self.submit_button = ttk.Button(
            controls_frame, text=button_text, 
            command=self.create_order_orchestrator
        )
        self.submit_button.grid(row=0, column=0, pady=(10, 0))
        
        # Extension controls section
        extension_frame = ttk.LabelFrame(form_frame, text="Order Extension", padding="10")
        extension_frame.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        extension_frame.columnconfigure(1, weight=1)
        
        ttk.Label(extension_frame, text="Extension Time (seconds):", font=("TkDefaultFont", 9)).grid(
            row=0, column=0, sticky="w", padx=(0, 10))
        self.extension_time_var = tk.StringVar(value="3600")  # Default 1 hour
        self.extension_time_entry = ttk.Entry(extension_frame, textvariable=self.extension_time_var, width=10)
        self.extension_time_entry.grid(row=0, column=1, sticky="w")
    
    def fetch_market_metadata(self):
        """Fetch and display market metadata for the entered token ID"""
        token_id = self.token_id_var.get().strip()
        if not token_id:
            messagebox.showerror("Error", "Please enter a Token ID first")
            return
        
        # Clear previous metadata
        self.clear_metadata_display()
        
        # Update button state and UI
        self.fetch_metadata_button.config(state='disabled', text="Fetching...")
        self.root.update()
        
        try:
            logger.info(f"Fetching market metadata for token ID: {token_id}")
            
            # Fetch metadata synchronously
            metadata = get_market_metadata_sync(token_id, timeout=10)
            
            if metadata:
                self.current_metadata = metadata
                self.display_metadata(metadata)
                
                # Also fetch and display current position
                self.display_current_position(token_id)
                
                logger.info(f"Successfully fetched metadata for token {token_id}: {metadata.market_title}")
            else:
                self.clear_metadata_display()
                messagebox.showwarning("No Data", f"No market data found for token ID: {token_id}")
                logger.warning(f"No metadata found for token {token_id}")
                
        except Exception as e:
            self.clear_metadata_display()
            error_msg = f"Error fetching market data: {str(e)}"
            messagebox.showerror("Error", error_msg)
            logger.error(f"Error fetching metadata for token {token_id}: {e}")
        finally:
            self.fetch_metadata_button.config(state='normal', text="Fetch Market Data")
    
    def clear_metadata_display(self):
        """Clear all metadata display fields"""
        self.market_slug_var.set("")
        self.outcome_var.set("")
        self.best_bid_var.set("")
        self.best_ask_var.set("")
        self.current_position_var.set("")
        self.current_metadata = None
    
    def display_metadata(self, metadata):
        """Display fetched metadata in the UI and auto-populate form fields"""
        # Display metadata fields
        self.market_slug_var.set(metadata.market_slug or "N/A")
        self.outcome_var.set(f"{metadata.outcome} ({metadata.outcome_index + 1}/{len(metadata.outcomes)})")
        
        # Display pricing information
        if metadata.best_bid is not None:
            self.best_bid_var.set(f"${metadata.best_bid:.4f}")
        else:
            self.best_bid_var.set("N/A")
            
        if metadata.best_ask is not None:
            self.best_ask_var.set(f"${metadata.best_ask:.4f}")
        else:
            self.best_ask_var.set("N/A")
        
        # Auto-populate form fields
        self.auto_populate_form_fields(metadata)
    
    def display_current_position(self, token_id: str):
        """Display current user position for the token"""
        try:
            if not self.positions_cache:
                self.current_position_var.set("Positions not available")
                self.current_position_label.config(foreground="gray")
                return
                
            position = self.positions_cache.get_position_for_token(token_id)
            if position and position.size > 0:
                position_text = f"{position.size:.2f} shares @ ${position.avg_price:.4f} avg"
                if position.cash_pnl != 0:
                    pnl_color = "green" if position.cash_pnl > 0 else "red"
                    position_text += f" (P&L: ${position.cash_pnl:.2f})"
                    self.current_position_label.config(foreground=pnl_color)
                else:
                    self.current_position_label.config(foreground="blue")
                
                self.current_position_var.set(position_text)
                logger.info(f"Displayed position for {token_id}: {position.size} shares")
            else:
                self.current_position_var.set("No position")
                self.current_position_label.config(foreground="gray")
                logger.info(f"No position found for token {token_id}")
        except Exception as e:
            logger.error(f"Error displaying position for {token_id}: {e}")
            self.current_position_var.set("Error loading position")
            self.current_position_label.config(foreground="red")
    
    def auto_populate_form_fields(self, metadata):
        """Auto-populate form fields based on metadata"""
        try:
            # Auto-populate tick size
            self.tick_size_var.set(str(metadata.order_price_min_tick_size))
            
            # Auto-populate limit price based on best bid/ask and side
            current_side = self.side_var.get()
            
            # Use the metadata fetcher's suggest_limit_price method
            suggested_price = self.metadata_fetcher.suggest_limit_price(
                metadata, side=current_side, improve_by_ticks=0
            )
            
            if suggested_price is not None:
                self.limit_price_var.set(f"{suggested_price:.4f}")
                logger.info(f"Auto-populated limit price: ${suggested_price:.4f} for {current_side} side")
            else:
                # Fallback to current price if available
                if metadata.current_price is not None:
                    self.limit_price_var.set(f"{metadata.current_price:.4f}")
                    logger.info(f"Auto-populated with current price: ${metadata.current_price:.4f}")
                    
        except Exception as e:
            logger.error(f"Error auto-populating form fields: {e}")
    
    def on_side_changed(self, event=None):
        """Handle side selection change - update suggested limit price if metadata is available"""
        if self.current_metadata is not None:
            self.auto_populate_form_fields(self.current_metadata)
        
        # Show/hide sell all checkbox based on side
        current_side = self.side_var.get()
        if current_side == SELL:
            self.sell_all_check.grid()
        else:
            self.sell_all_check.grid_remove()
            self.sell_all_var.set(False)  # Clear sell all when switching to BUY
    
    def on_sell_all_changed(self):
        """Handle sell all checkbox change - populate total quantity from user position"""
        if self.sell_all_var.get():
            # User checked "sell all" - populate quantity from their position
            token_id = self.token_id_var.get().strip()
            if token_id:
                try:
                    if not self.positions_cache:
                        messagebox.showwarning("No Positions Cache", "Positions cache not available")
                        self.sell_all_var.set(False)
                        return
                        
                    position = self.positions_cache.get_position_for_token(token_id)
                    if position and position.size > 0:
                        self.total_quantity_var.set(str(position.size))
                        self.total_quantity_entry.config(state='disabled')
                        logger.info(f"Set sell all quantity to {position.size} for token {token_id}")
                    else:
                        messagebox.showwarning("No Position", f"You don't have any position in token {token_id}")
                        self.sell_all_var.set(False)
                except Exception as e:
                    logger.error(f"Error fetching position for sell all: {e}")
                    messagebox.showerror("Error", f"Error fetching your position: {str(e)}")
                    self.sell_all_var.set(False)
            else:
                messagebox.showwarning("Missing Token ID", "Please enter a Token ID first")
                self.sell_all_var.set(False)
        else:
            # User unchecked "sell all" - enable quantity entry
            self.total_quantity_entry.config(state='normal')
        
    def validate_form_inputs(self) -> Optional[StrategyConfig]:
        """Validate form inputs and return StrategyConfig if valid"""
        try:
            logger.info("Validating form inputs")
            
            # Check required fields
            token_id = self.token_id_var.get().strip()
            if not token_id:
                messagebox.showerror("Validation Error", "Token ID is required")
                return None
            
            # Check for existing orchestrator with same token ID
            for order_id, order_data in self.active_orders.items():
                if order_data['config'].token_id == token_id and order_data['status'] == 'Running':
                    messagebox.showerror(
                        "Validation Error", 
                        f"An orchestrator is already running for Token ID: {token_id}\n"
                        f"Only one orchestrator per token is allowed to avoid confusion on fills.\n"
                        f"Please stop the existing orchestrator (Order #{order_id}) first."
                    )
                    return None
            
            # Validate other fields
            try:
                limit_price = float(self.limit_price_var.get())
                if limit_price <= 0 or limit_price > 1:
                    messagebox.showerror("Validation Error", "Limit price must be between 0 and 1")
                    return None
            except ValueError:
                messagebox.showerror("Validation Error", "Invalid limit price")
                return None
            
            total_quantity = float(self.total_quantity_var.get())
            if total_quantity <= 0:
                raise ValueError("Total quantity must be positive")
            if total_quantity < MIN_ORDER_SIZE:
                raise ValueError(f"Total quantity must be at least {MIN_ORDER_SIZE} shares (minimum order size)")
            
            child_order_size = float(self.child_order_size_var.get())
            if child_order_size <= 0 or child_order_size > total_quantity:
                raise ValueError("Child order size must be positive and <= total quantity")
            if child_order_size < MIN_ORDER_SIZE:
                raise ValueError(f"Child order size must be at least {MIN_ORDER_SIZE} shares (minimum order size)")
            
            timeout_seconds = int(self.timeout_var.get())
            if timeout_seconds <= 0:
                raise ValueError("Timeout must be positive")
            
            # Validate tick size
            tick_size = float(self.tick_size_var.get())
            if tick_size not in [0.001, 0.01]:
                raise ValueError("Tick size must be 0.001 or 0.01")
            
            side = self.side_var.get()
            match_top_book = self.match_top_book_var.get()
            inside_liquidity_mode = self.inside_liquidity_var.get()
            
            # Validate option combinations
            if inside_liquidity_mode and match_top_book:
                result = messagebox.askyesno(
                    "Option Compatibility", 
                    "Inside Liquidity Mode and Match Top of Book are both selected.\n\n"
                    "Inside Liquidity Mode only takes liquidity and doesn't post orders, "
                    "so 'Match Top of Book' has no effect.\n\n"
                    "Do you want to continue anyway?"
                )
                if not result:
                    return None
            
            # Create config
            config = StrategyConfig(
                token_id=token_id,
                limit_price=limit_price,
                total_quantity=total_quantity,
                child_order_size=child_order_size,
                order_price_min_tick_size=tick_size,
                side=side,
                timeout_seconds=timeout_seconds,
                match_top_of_book=match_top_book,
                inside_liquidity_mode=inside_liquidity_mode
            )
            
            logger.info(f"Form validation successful: token_id={token_id}, limit_price={limit_price}, total_quantity={total_quantity}, side={side}")
            return config
            
        except ValueError as e:
            logger.warning(f"Form validation failed: {str(e)}")
            messagebox.showerror("Validation Error", str(e))
            return None
        except Exception as e:
            logger.error(f"Unexpected error during form validation: {str(e)}")
            messagebox.showerror("Error", f"Unexpected error: {str(e)}")
            return None
    
    def create_order_orchestrator(self):
        """Create and start a new order orchestrator"""
        config = self.validate_form_inputs()
        if not config:
            return
        
        try:
            order_id = f"ORD_{self.next_order_id:03d}"
            self.next_order_id += 1
            
            # Get selected account info
            selected_account = self.selected_account.get()
            
            # Log order parameters (always logged)
            log_message = f"""
            {'='*60}
            ORDER PARAMETERS - {order_id}
            {'='*60}
            Order ID: {order_id}
            Account: {selected_account}
            Token ID: {config.token_id}
            Limit Price: ${config.limit_price:.4f}
            Total Quantity: {config.total_quantity:.2f}
            Child Order Size: {config.child_order_size:.2f}
            Order Price Min Tick Size: {config.order_price_min_tick_size:.3f}
            Side: {config.side}
            Match Top of Book: {config.match_top_of_book}
            Timeout (seconds): {config.timeout_seconds}
            Mode: {'Real Trading' if self.credentials_available else 'Simulation'}
            {'='*60}
            """
            logger.info(log_message)

            # Update status indicator at the top
            mode_text = "" if self.credentials_available else " (Simulation)"
            self.status_indicator.config(text=f"🔵 Creating {order_id}{mode_text}...", foreground="blue")
            self.submit_button.config(state='disabled')
            self.root.update()
            
            # Store the order configuration and status
            self.active_orders[order_id] = {
                'config': config,
                'manager': None,  # Will be OrderManager instance
                'client': None,
                'auth': None,
                'selected_account': selected_account,  # Store selected account
                'start_time': datetime.now(),
                'status': 'Initializing',
                'filled_quantity': 0.0,
                'pending_orders': [],
                'last_status_update': None,
                'simulation_mode': not self.credentials_available,
                'metadata': self.current_metadata  # Store market metadata for display
            }
            
            # Add to tree
            self.add_order_to_tree(order_id)
            
            # Start the order orchestrator
            if self.loop is not None and not self.loop.is_closed():
                asyncio.run_coroutine_threadsafe(
                    self.start_order_orchestrator(order_id), 
                    self.loop
                )
            else:
                print("Error: Async event loop not available")
            
            # Clear form
            self.clear_form()
            
            mode_text = " (Simulation)" if not self.credentials_available else ""
            self.status_indicator.config(text=f"🟢 {order_id} Created{mode_text}", foreground="green")
            
            # Reset status indicator after 3 seconds
            self.root.after(3000, lambda: self.status_indicator.config(text="🟢 Ready", foreground="green"))
            
        except Exception as e:
            messagebox.showerror("Error", f"Failed to create order orchestrator: {str(e)}")
            self.status_indicator.config(text="🔴 Error Creating Order", foreground="red")
            # Reset status indicator after 3 seconds
            self.root.after(3000, lambda: self.status_indicator.config(text="🟢 Ready", foreground="green"))
        finally:
            self.submit_button.config(state='normal')
    
    async def start_order_orchestrator(self, order_id: str):
        """Start the order orchestrator (async)"""
        logger.info(f"Starting order orchestrator {order_id}")
        try:
            order_data = self.active_orders[order_id]
            config = order_data['config']
            
            if order_data['simulation_mode']:
                # Simulation mode
                logger.info(f"Starting order orchestrator {order_id} in simulation mode")
                print(f"Starting order orchestrator {order_id} in simulation mode")
                await asyncio.sleep(1)  # Simulate initialization
                order_data['status'] = 'Running (Simulation)'
                
                # Start simulation monitoring
                asyncio.create_task(self._simulate_order_progress(order_id))
                logger.info(f"Simulation monitoring started for {order_id}")
            else:
                # Real trading mode
                logger.info(f"Starting order orchestrator {order_id} in real trading mode")
                print(f"Starting order orchestrator {order_id} in real trading mode")
                
                # Setup client and auth
                client, auth = self._setup_client_and_auth()
                order_data['client'] = client
                order_data['auth'] = auth
                
                # Create OrderManager instance
                logger.info(f"Creating OrderManager instance for {order_id}")
                manager = OrderManager(client, config, auth)
                order_data['manager'] = manager
                
                # Start the strategy
                logger.info(f"Starting strategy for {order_id}")
                await manager.start_strategy()
                order_data['status'] = 'Running'
                
                # Start status monitoring
                asyncio.create_task(self._monitor_order_status(order_id))
                logger.info(f"Real trading monitoring started for {order_id}")
                
        except Exception as e:
            error_msg = f"Error starting order orchestrator {order_id}: {e}"
            print(error_msg)
            logger.error(error_msg)
            if order_id in self.active_orders:
                self.active_orders[order_id]['status'] = 'Error'
    
    def add_order_to_tree(self, order_id: str):
        """Add order to the tree view"""
        order_data = self.active_orders[order_id]
        config = order_data['config']
        metadata = order_data.get('metadata')
        
        # Calculate timeout remaining
        elapsed = (datetime.now() - order_data['start_time']).total_seconds()
        timeout_remaining = max(0, config.timeout_seconds - elapsed)
        timeout_str = f"{int(timeout_remaining)}s"
        
        # Format quantity display
        filled = order_data['filled_quantity']
        total = config.total_quantity
        quantity_str = f"{filled:.2f}/{total:.2f}"
        
        # Extract market info from metadata
        market_slug = metadata.market_slug if metadata else config.token_id
        outcome = metadata.outcome if metadata else "Unknown"
        
        values = (
            order_id,
            market_slug,
            outcome,
            quantity_str,
            f"${config.limit_price:.4f}",
            f"{config.child_order_size:.2f}",
            f"{config.order_price_min_tick_size:.3f}",
            "Yes" if config.match_top_of_book else "No",
            config.side,
            timeout_str,
            order_data['status'],
            "Cancel | Extend"
        )
        
        self.orders_tree.insert("", "end", iid=order_id, values=values)
    
    def update_order_in_tree(self, order_id: str):
        """Update order status in tree view"""
        try:
            if order_id not in self.active_orders:
                return
            
            order_data = self.active_orders[order_id]
            config = order_data['config']
            
            # Format quantity display
            filled = order_data['filled_quantity']
            total = config.total_quantity
            completion_pct = (filled / total * 100) if total > 0 else 0
            
            # Count pending orders
            pending_orders = order_data.get('pending_orders', [])
            pending_count = len(pending_orders)
            
            quantity_text = f"{filled:.2f}/{total:.2f} ({completion_pct:.1f}%) ({pending_count} pending)"
            
            # Format timeout display
            start_time = order_data['start_time']
            elapsed = (datetime.now() - start_time).total_seconds()
            timeout_remaining = max(0, config.timeout_seconds - elapsed)
            timeout_text = f"{int(timeout_remaining)}s" if timeout_remaining > 0 else "Expired"
            
            # Extract market info from metadata
            metadata = order_data.get('metadata')
            market_slug = metadata.market_slug if metadata else config.token_id
            outcome = metadata.outcome if metadata else "Unknown"

            # Update tree values (must match column order)
            self.orders_tree.item(order_id, values=(
                order_id,                                                    # Order ID
                market_slug,                                                # Market Slug
                outcome,                                                    # Outcome
                quantity_text,                                              # Quantity
                f"${config.limit_price:.4f}",                              # Limit Price
                f"{config.child_order_size:.2f}",                          # Child Order Size
                f"{config.order_price_min_tick_size:.3f}",                  # Tick Size
                "Yes" if config.match_top_of_book else "No",               # Match Top Book
                config.side,                                                # Side
                timeout_text,                                               # Timeout
                order_data['status'],                                       # Status
                "Cancel | Extend"                                          # Actions
            ))
            
            # Apply color coding based on status
            status = order_data['status']
            if status == 'Completed':
                self.orders_tree.item(order_id, tags=('completed',))
            elif status == 'Running':
                self.orders_tree.item(order_id, tags=('running',))
            elif 'Cancelled' in status:
                self.orders_tree.item(order_id, tags=('cancelled',))
            elif 'Error' in status:
                self.orders_tree.item(order_id, tags=('error',))
            else:
                self.orders_tree.item(order_id, tags=('default',))
                
        except Exception as e:
            print(f"Error updating tree for order {order_id}: {e}")
            logger.error(f"Error updating tree for order {order_id}: {e}")
    
    def on_row_double_click(self, event):
        """Handle double-click on tree rows"""
        try:
            # Get the region clicked
            region = self.orders_tree.identify_region(event.x, event.y)
            
            if region == "cell":
                # Get column clicked
                column = self.orders_tree.identify_column(event.x)
                item = self.orders_tree.identify_row(event.y)
                
                if item and column == "#12":  # Actions column
                    # Determine which action based on click position within the cell
                    bbox = self.orders_tree.bbox(item, column)
                    if bbox:
                        # Get relative position within the cell
                        cell_x = event.x - bbox[0]
                        cell_width = bbox[2]
                        
                        # If clicked on left half, it's Cancel; if right half, it's Extend
                        if cell_x < cell_width * 0.6:  # "Cancel" takes up ~60% of the left side
                            self.cancel_order_orchestrator(item)
                        else:  # "Extend" is on the right side
                            self.extend_order_orchestrator(item)
                    else:
                        # Fallback to cancel if we can't determine position
                        self.cancel_order_orchestrator(item)
                elif item:
                    self.show_order_details(item)
            elif region == "item":
                # Double-click on row (not in actions column)
                item = self.orders_tree.identify_row(event.y)
                if item:
                    self.show_order_details(item)
        except Exception as e:
            print(f"Error handling double-click: {e}")
            logger.error(f"Error handling double-click: {e}")
    
    def show_order_details(self, order_id: str):
        """Show detailed status for an order orchestrator"""
        logger.info(f"User requested detailed view for order {order_id}")
        if order_id not in self.active_orders:
            logger.warning(f"Order {order_id} not found in active orders")
            return
        
        order_data = self.active_orders[order_id]
        config = order_data['config']
        selected_account = order_data.get('selected_account', 'Unknown')
        
        # Create details window
        details_window = tk.Toplevel(self.root)
        details_window.title(f"Order Details - {order_id}")
        details_window.geometry("500x400")
        details_window.transient(self.root)
        details_window.grab_set()
        
        # Main frame with scrollbar
        main_frame = ttk.Frame(details_window, padding="10")
        main_frame.pack(fill=tk.BOTH, expand=True)
        
        # Details text
        details_text = tk.Text(main_frame, wrap=tk.WORD, font=("Consolas", 10))
        scrollbar = ttk.Scrollbar(main_frame, orient=tk.VERTICAL, command=details_text.yview)
        details_text.configure(yscrollcommand=scrollbar.set)
        
        details_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        
        # Compile details
        runtime = datetime.now() - order_data['start_time']
        filled = order_data['filled_quantity']
        total = config.total_quantity
        completion = (filled / total * 100) if total > 0 else 0
        
        details = f"""
ORDER ORCHESTRATOR DETAILS
{'='*50}

Order ID: {order_id}
Account: {selected_account}
Status: {order_data['status']}
Mode: {'Simulation' if order_data['simulation_mode'] else 'Real Trading'}

CONFIGURATION
{'='*50}
Token ID: {config.token_id}
Limit Price: ${config.limit_price:.4f}
Total Quantity: {config.total_quantity:.2f}
Child Order Size: {config.child_order_size:.2f}
Tick Size: {config.order_price_min_tick_size:.3f}
Side: {config.side}
Match Top of Book: {config.match_top_of_book}
Inside Liquidity Mode: {getattr(config, 'inside_liquidity_mode', False)}
Timeout: {config.timeout_seconds} seconds

PROGRESS
{'='*50}
Target Quantity: {total:.2f}
Filled Quantity: {filled:.2f}
Completion: {completion:.1f}%
Pending Orders: {len(order_data.get('pending_orders', []))}

TIMING
{'='*50}
Start Time: {order_data['start_time'].strftime('%Y-%m-%d %H:%M:%S')}
Runtime: {str(runtime).split('.')[0]}
Last Update: {order_data.get('last_status_update', 'Never')}

"""
        
        if order_data.get('pending_orders'):
            details += "\nPENDING ORDERS\n" + "="*50 + "\n"
            for i, order in enumerate(order_data['pending_orders'], 1):
                if isinstance(order, dict):
                    details += f"{i}. Size: {order.get('size', 'N/A')}\n"
                    if 'price' in order:
                        details += f"   Price: ${order['price']:.4f}\n"
                    if 'id' in order:
                        details += f"   ID: {order['id']}\n"
        
        details_text.insert(tk.END, details)
        details_text.config(state=tk.DISABLED)
        
        # Close button
        close_button = ttk.Button(details_window, text="Close", command=details_window.destroy)
        close_button.pack(pady=10)
    
    def cancel_order_orchestrator(self, order_id: str):
        """Cancel an order orchestrator"""
        logger.info(f"User requested cancellation of order {order_id}")
        if order_id not in self.active_orders:
            logger.warning(f"Order {order_id} not found in active orders")
            return
        
        # Confirm cancellation
        result = messagebox.askyesno(
            "Cancel Order Orchestrator",
            f"Are you sure you want to cancel order orchestrator {order_id}?"
        )
        
        if result:
            logger.info(f"User confirmed cancellation of order {order_id}")
            try:
                order_data = self.active_orders[order_id]
                order_data['status'] = 'Cancelling'
                
                # Cancel the order orchestrator (async)
                if self.loop is not None and not self.loop.is_closed():
                    asyncio.run_coroutine_threadsafe(
                        self.stop_order_orchestrator(order_id), 
                        self.loop
                    )
                    logger.info(f"Cancellation request submitted for order {order_id}")
                else:
                    error_msg = "Error: Async event loop not available for cancellation"
                    logger.error(error_msg)
                    print(error_msg)
                
            except Exception as e:
                error_msg = f"Failed to cancel order orchestrator: {str(e)}"
                logger.error(f"Error cancelling order {order_id}: {error_msg}")
                messagebox.showerror("Error", error_msg)
        else:
            logger.info(f"User cancelled the cancellation request for order {order_id}")
    
    def extend_order_orchestrator(self, order_id: str):
        """Extend timeout for an order orchestrator"""
        logger.info(f"User requested extension of order {order_id}")
        if order_id not in self.active_orders:
            logger.warning(f"Order {order_id} not found in active orders")
            return
        
        order_data = self.active_orders[order_id]
        status = order_data['status']
        
        # Only allow extension for running orders
        if 'Running' not in status:
            messagebox.showwarning(
                "Cannot Extend",
                f"Cannot extend order {order_id} - it is not currently running.\nStatus: {status}"
            )
            return
        
        try:
            # Get extension time from the entry box
            extension_seconds = int(self.extension_time_var.get())
            if extension_seconds <= 0:
                raise ValueError("Extension time must be positive")
            
            # Confirm extension
            extension_hours = extension_seconds / 3600
            result = messagebox.askyesno(
                "Extend Order Orchestrator",
                f"Extend order orchestrator {order_id} by {extension_seconds} seconds ({extension_hours:.1f} hours)?"
            )
            
            if result:
                logger.info(f"User confirmed extension of order {order_id} by {extension_seconds} seconds")
                
                # Extend the order orchestrator (async)
                if self.loop is not None and not self.loop.is_closed():
                    asyncio.run_coroutine_threadsafe(
                        self.extend_order_timeout(order_id, extension_seconds), 
                        self.loop
                    )
                    logger.info(f"Extension request submitted for order {order_id}")
                    
                    # Show success message
                    messagebox.showinfo(
                        "Extension Successful",
                        f"Order {order_id} timeout extended by {extension_seconds} seconds ({extension_hours:.1f} hours)"
                    )
                else:
                    error_msg = "Error: Async event loop not available for extension"
                    logger.error(error_msg)
                    messagebox.showerror("Error", error_msg)
            else:
                logger.info(f"User cancelled the extension request for order {order_id}")
                
        except ValueError as e:
            error_msg = f"Invalid extension time: {str(e)}"
            logger.warning(f"Invalid extension input for order {order_id}: {error_msg}")
            messagebox.showerror("Invalid Input", error_msg)
        except Exception as e:
            error_msg = f"Failed to extend order orchestrator: {str(e)}"
            logger.error(f"Error extending order {order_id}: {error_msg}")
            messagebox.showerror("Error", error_msg)
    
    async def _simulate_order_progress(self, order_id: str):
        """Simulate order progress for demonstration purposes"""
        logger.info(f"Starting simulation for order {order_id}")
        if order_id not in self.active_orders:
            logger.warning(f"Order {order_id} not found in active orders")
            return
        
        order_data = self.active_orders[order_id]
        config = order_data['config']
        target = config.total_quantity
        
        # Simulate gradual filling over time
        filled = 0.0
        logger.info(f"Simulating order progress for {order_id}: target={target}")
        while filled < target and order_data['status'] in ['Running (Simulation)']:
            await asyncio.sleep(5)  # Wait 5 seconds between updates
            
            if order_id not in self.active_orders:
                logger.info(f"Order {order_id} removed from active orders, stopping simulation")
                break
                
            # Simulate filling 10-30% of remaining quantity
            remaining = target - filled
            fill_amount = min(remaining, remaining * (0.1 + 0.2 * asyncio.get_event_loop().time() % 1))
            filled += fill_amount
            
            order_data['filled_quantity'] = filled
            
            # Simulate pending orders
            child_size = config.child_order_size
            pending = min(child_size, target - filled) if filled < target else 0
            order_data['pending_orders'] = [{'size': pending}] if pending > 0 else []
            
            logger.info(f"Simulation {order_id}: Filled {filled:.2f}/{target:.2f}")
            print(f"Simulation {order_id}: Filled {filled:.2f}/{target:.2f}")
            
        # Mark as completed if target reached
        if filled >= target:
            order_data['status'] = 'Completed (Simulation)'
            order_data['filled_quantity'] = target
            order_data['pending_orders'] = []
            logger.info(f"Simulation {order_id} completed successfully")

    async def _monitor_order_status(self, order_id: str):
        """Monitor real order orchestrator status"""
        logger.info(f"Starting status monitoring for order {order_id}")
        if order_id not in self.active_orders:
            logger.warning(f"Order {order_id} not found in active orders")
            return
        
        order_data = self.active_orders[order_id]
        manager = order_data['manager']
        
        if not manager:
            logger.error(f"No manager found for order {order_id}")
            return
        
        while order_data['status'] == 'Running' and manager.running:
            try:
                await asyncio.sleep(2)  # Check every 2 seconds
                
                if order_id not in self.active_orders:
                    logger.info(f"Order {order_id} removed from active orders, stopping monitoring")
                    break
                
                # Get current status from OrderManager
                status = manager.get_status()
                
                # Check for critical errors first
                if status.get('has_critical_error', False):
                    error_msg = status.get('critical_error_message', 'Unknown critical error')
                    logger.error(f"Critical error detected for order {order_id}: {error_msg}")
                    order_data['status'] = f'Error: {error_msg}'
                    
                    # Stop the manager
                    try:
                        await manager.stop_strategy()
                    except Exception as e:
                        logger.error(f"Error stopping manager after critical error: {e}")
                    
                    break
                
                # Update our tracking
                old_filled = order_data['filled_quantity']
                order_data['filled_quantity'] = status['position']['filled_quantity']
                order_data['pending_orders'] = status['orders']['pending_orders']
                order_data['last_status_update'] = datetime.now()
                
                # Log fill progress if changed
                if order_data['filled_quantity'] != old_filled:
                    logger.info(f"Fill update {order_id}: {old_filled:.2f} -> {order_data['filled_quantity']:.2f}")
                
                # Check if completed
                if status['position']['completion_percentage'] >= 100:
                    order_data['status'] = 'Completed'
                    logger.info(f"Order {order_id} completed successfully")
                elif not status['running']:
                    # If the orchestrator stopped running, determine why
                    if status.get('has_critical_error', False):
                        error_msg = status.get('critical_error_message', 'Unknown error')
                        order_data['status'] = f'Error: {error_msg}'
                        logger.error(f"Order {order_id} stopped due to error: {error_msg}")
                    else:
                        order_data['status'] = 'Stopped'
                        logger.info(f"Order {order_id} stopped normally")
                
                logger.debug(f"Status {order_id}: {status['position']['filled_quantity']:.2f}/{status['position']['target_quantity']:.2f}")
                print(f"Status {order_id}: {status['position']['filled_quantity']:.2f}/{status['position']['target_quantity']:.2f}")
                
            except Exception as e:
                error_msg = f"Error monitoring {order_id}: {e}"
                logger.error(error_msg)
                print(error_msg)
                
                # If we get repeated monitoring errors, mark as error state
                order_data['status'] = f'Error: Monitoring failed - {e}'
                break
        
        # Check if we exited the loop due to a critical error (manager stopped running)
        if order_id in self.active_orders and order_data['status'] == 'Running':
            try:
                # Manager stopped running, check if it was due to a critical error
                status = manager.get_status()
                if status.get('has_critical_error', False):
                    error_msg = status.get('critical_error_message', 'Unknown critical error')
                    logger.error(f"Critical error detected after monitoring exit for order {order_id}: {error_msg}")
                    order_data['status'] = f'Error: {error_msg}'
                elif not status['running']:
                    # Manager stopped normally
                    if status['position']['completion_percentage'] >= 100:
                        order_data['status'] = 'Completed'
                        logger.info(f"Order {order_id} completed successfully")
                    else:
                        order_data['status'] = 'Stopped'
                        logger.info(f"Order {order_id} stopped normally")
            except Exception as e:
                logger.error(f"Error checking final status for {order_id}: {e}")
                order_data['status'] = f'Error: Status check failed - {e}'
        
        logger.info(f"Stopped monitoring {order_id}")
        print(f"Stopped monitoring {order_id}")

    async def stop_order_orchestrator(self, order_id: str):
        """Stop the order orchestrator (async)"""
        try:
            if order_id not in self.active_orders:
                return
            
            order_data = self.active_orders[order_id]
            
            if order_data['simulation_mode']:
                # Simulation mode - just mark as cancelled
                order_data['status'] = 'Cancelled (Simulation)'
            else:
                # Real trading mode - stop the OrderManager
                if order_data['manager']:
                    await order_data['manager'].stop_strategy()
                order_data['status'] = 'Cancelled'
            
            # Remove from active orders after a delay
            await asyncio.sleep(2)
            if order_id in self.active_orders and 'Cancelled' in self.active_orders[order_id]['status']:
                del self.active_orders[order_id]
                # Remove from tree in UI thread
                self.root.after(0, lambda: self.remove_order_from_tree(order_id))
            
        except Exception as e:
            error_msg = f"Error stopping order orchestrator {order_id}: {e}"
            print(error_msg)
            logger.error(error_msg)
            if order_id in self.active_orders:
                self.active_orders[order_id]['status'] = 'Error'
    
    async def extend_order_timeout(self, order_id: str, extension_seconds: int):
        """Extend the timeout for an order orchestrator (async)"""
        try:
            if order_id not in self.active_orders:
                logger.warning(f"Order {order_id} not found in active orders during extension")
                return
            
            order_data = self.active_orders[order_id]
            
            if order_data['simulation_mode']:
                # Simulation mode - just update our local timeout tracking
                order_data['config'].timeout_seconds += extension_seconds
                logger.info(f"Simulation mode: Extended timeout for {order_id} by {extension_seconds} seconds")
                print(f"Simulation mode: Extended timeout for {order_id} by {extension_seconds} seconds")
            else:
                # Real trading mode - extend the OrderManager timeout
                if order_data['manager']:
                    order_data['manager'].extend_timeout(extension_seconds)
                    # Also update our local config for display purposes
                    order_data['config'].timeout_seconds += extension_seconds
                    logger.info(f"Real trading mode: Extended timeout for {order_id} by {extension_seconds} seconds")
                    print(f"Real trading mode: Extended timeout for {order_id} by {extension_seconds} seconds")
                else:
                    logger.error(f"No manager found for order {order_id} during extension")
                    print(f"Error: No manager found for order {order_id}")
            
        except Exception as e:
            error_msg = f"Error extending timeout for order orchestrator {order_id}: {e}"
            print(error_msg)
            logger.error(error_msg)
    
    def remove_order_from_tree(self, order_id: str):
        """Remove order from tree view"""
        try:
            self.orders_tree.delete(order_id)
        except tk.TclError:
            # Item might already be deleted
            pass
    
    def clear_form(self):
        """Clear all form fields"""
        logger.info("Clearing form fields")
        self.limit_price_var.set("")
        self.total_quantity_var.set("")
        self.child_order_size_var.set("")
        self.side_var.set(BUY)
        self.tick_size_var.set("0.001")
        self.match_top_book_var.set(False)
        self.inside_liquidity_var.set(False)
        self.sell_all_var.set(False)
        self.timeout_var.set("3600")
        
        # Hide sell all checkbox since default side is BUY
        self.sell_all_check.grid_remove()
        
        # Clear metadata display
        self.clear_metadata_display()
        
        logger.info("Form fields cleared")
    
    def start_ui_updates(self):
        """Start periodic UI updates"""
        self.update_ui()
    
    def update_ui(self):
        """Update UI with current data"""
        try:
            # Update all orders in tree
            for order_id in list(self.active_orders.keys()):
                self.update_order_in_tree(order_id)
            
            # Schedule next update
            self.update_timer = self.root.after(1000, self.update_ui)  # Update every second
            
        except Exception as e:
            print(f"Error updating UI: {e}")
            # Schedule next update anyway
            self.update_timer = self.root.after(1000, self.update_ui)
    
    def toggle_fullscreen(self, event=None):
        """Toggle fullscreen mode with F11 key"""
        try:
            # Get current state
            current_state = self.root.state()
            if current_state == 'zoomed':
                self.root.state('normal')
            else:
                self.root.state('zoomed')
        except tk.TclError:
            # Fallback for non-Windows systems
            try:
                current_fullscreen = self.root.attributes('-fullscreen')
                self.root.attributes('-fullscreen', not current_fullscreen)
            except tk.TclError:
                pass
    
    def on_closing(self):
        """Handle application closing"""
        logger.info("Application shutdown initiated by user")
        try:
            print("Shutting down Order Manager GUI...")
            
            # Cancel all running orders
            running_orders = []
            clients_to_cancel = set()  # Track unique clients to avoid duplicate cancellations
            
            for order_id in list(self.active_orders.keys()):
                order_data = self.active_orders[order_id]
                status = order_data['status']
                
                if 'Running' in status or status == 'Initializing':
                    running_orders.append(order_id)
                    
                    # Collect clients for order cancellation (only for real trading mode)
                    if not order_data.get('simulation_mode', True) and order_data.get('client'):
                        clients_to_cancel.add(order_data['client'])
                    
                    # Stop the orchestrator
                    if self.loop is not None and not self.loop.is_closed():
                        asyncio.run_coroutine_threadsafe(
                            self.stop_order_orchestrator(order_id), 
                            self.loop
                        )
            
            # Cancel all orders on each client
            if clients_to_cancel:
                logger.info(f"Cancelling all orders on {len(clients_to_cancel)} client(s)")
                print(f"Cancelling all orders on {len(clients_to_cancel)} client(s)...")
                
                for client in clients_to_cancel:
                    try:
                        # Use cancel_all() to cancel all orders for this client
                        client.cancel_all()
                        logger.info("Successfully cancelled all orders on client")
                        print("✓ All orders cancelled on client")
                    except Exception as e:
                        logger.error(f"Error cancelling orders on client: {e}")
                        print(f"✗ Error cancelling orders: {e}")
            
            if running_orders:
                logger.info(f"Stopping {len(running_orders)} active order orchestrators: {running_orders}")
                print(f"Stopping {len(running_orders)} active order orchestrators...")
                # Give time for cleanup
                time.sleep(2)
            
            # Stop UI updates
            if self.update_timer:
                self.root.after_cancel(self.update_timer)
                logger.info("UI update timer cancelled")
            
            # Stop event loop
            if self.loop and not self.loop.is_closed():
                self.loop.call_soon_threadsafe(self.loop.stop)
                logger.info("Async event loop shutdown requested")
            
            logger.info("Order Manager GUI shutdown completed successfully")
            print("Cleanup completed")
            
        except Exception as e:
            error_msg = f"Error during cleanup: {e}"
            print(error_msg)
            logger.error(error_msg)
        finally:
            # Final safety net - try to cancel orders one more time if we have any clients
            try:
                for order_id, order_data in self.active_orders.items():
                    if not order_data.get('simulation_mode', True) and order_data.get('client'):
                        try:
                            order_data['client'].cancel_all()
                            logger.info(f"Finally block: Cancelled all orders for {order_id}")
                        except Exception as e:
                            logger.error(f"Finally block: Error cancelling orders for {order_id}: {e}")
            except Exception as e:
                logger.error(f"Finally block: Error during final order cancellation: {e}")
            
            self.root.destroy()


def main():
    root = tk.Tk()
    app = OrderManagerGUI(root)
    
    # Handle window closing
    root.protocol("WM_DELETE_WINDOW", app.on_closing)
    
    root.mainloop()


if __name__ == "__main__":
    main() 
import tkinter as tk
from tkinter import ttk, messagebox
import threading
import asyncio
import time
from typing import Dict, List, Optional, Tuple
import logging
import logging.handlers

# Reuse existing project modules
from backend.account_manager import AccountManager
from backend.user_positions import UserPositionsCache, UserPosition
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OpenOrderParams
from backend.market_analyzer import MarketAnalyzer
from backend.token_manager import TokenManager
from backend.market_metadata import get_market_metadata  # Added for token->slug/outcome mapping

# Note: CTF operations (redeem/merge) require Polymarket operator multisig
# These must be done through the official Polymarket web interface


def setup_logging() -> logging.Logger:
    # Configure root logger so all module logs (including backend.*) flow into GUI log
    root_logger = logging.getLogger()
    if not root_logger.handlers:
        import os
        os.makedirs('logs', exist_ok=True)

        file_handler = logging.handlers.RotatingFileHandler(
            'logs/positions_orders_dashboard.log', maxBytes=10*1024*1024, backupCount=3
        )
        console_handler = logging.StreamHandler()

        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(name)s - %(funcName)s:%(lineno)d - %(message)s')
        file_handler.setFormatter(formatter)
        console_handler.setFormatter(formatter)

        root_logger.addHandler(file_handler)
        root_logger.addHandler(console_handler)
        root_logger.setLevel(logging.INFO)

    # Return a module-specific logger that propagates to root
    return logging.getLogger(__name__)


logger = setup_logging()


class PositionsOrdersDashboard:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Polymarket Positions & Orders Dashboard")
        self.root.geometry("1400x900")

        try:
            self.root.state('zoomed')
        except tk.TclError:
            try:
                self.root.attributes('-zoomed', True)
            except tk.TclError:
                pass

        # Backend state
        self.account_manager = AccountManager()
        self.positions_caches: Dict[str, UserPositionsCache] = {}
        self.open_orders_cache: Dict[str, List[dict]] = {}  # account_id -> orders
        self.included_accounts: Dict[str, bool] = {}  # account_id -> include in net calc
        self.market_analyzer = MarketAnalyzer()
        self.token_manager = TokenManager()
        # Sorting state
        self._sort_state: Dict[str, Tuple[str, bool]] = {}  # tree_id -> (col, asc)
        # Netting mode defaults to pairs
        self.net_pair_var = tk.BooleanVar(value=True)
        # Orders auto-refresh
        self._orders_auto_refresh_enabled = True
        self._orders_refresh_inflight = False
        # Positions refresh state
        self._pos_net_refresh_inflight: bool = False
        self._pos_by_acct_refresh_inflight: bool = False
        self._pos_by_acct_pending_account: Optional[str] = None
        # Cache for token_id -> (slug, outcome) used by orders table
        self._token_slug_outcome_cache: Dict[str, Tuple[str, str]] = {}

        # Async loop for background work
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.loop_thread: Optional[threading.Thread] = None

        self._setup_async_loop()
        self._build_ui()

        # Initial load
        self._load_accounts_initial()
        # Kick off orders auto-refresh
        self._schedule_orders_refresh()

    # ------------------------- Async loop -------------------------
    def _setup_async_loop(self) -> None:
        loop_ready = threading.Event()

        def run_loop():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            loop_ready.set()
            self.loop.run_forever()

        self.loop_thread = threading.Thread(target=run_loop, daemon=True)
        self.loop_thread.start()
        loop_ready.wait(timeout=5.0)
        if self.loop is None:
            raise RuntimeError("Failed to initialize async event loop")

    def _run_async(self, coro) -> None:
        if self.loop is None or self.loop.is_closed():
            raise RuntimeError("Async loop is not available")
        asyncio.run_coroutine_threadsafe(coro, self.loop)

    # ------------------------- UI -------------------------
    def _build_ui(self) -> None:
        container = ttk.Frame(self.root, padding="6")
        container.pack(fill=tk.BOTH, expand=True)

        self.notebook = ttk.Notebook(container)
        self.notebook.pack(fill=tk.BOTH, expand=True)

        # Tabs: Combined Positions & Orders, and Config
        self.tab_positions = ttk.Frame(self.notebook)
        self.tab_accounts = ttk.Frame(self.notebook)
        self.tab_redeemable = ttk.Frame(self.notebook) # New tab for redeemable/mergeable indicators

        self.notebook.add(self.tab_positions, text="Positions & Orders")
        self.notebook.add(self.tab_accounts, text="Config (Accounts)")
        self.notebook.add(self.tab_redeemable, text="Redeemable/Mergeable") # Add new tab

        self._build_positions_tab()
        self._build_accounts_tab()
        self._build_redeemable_tab() # Build new tab

    # ------------------------- Accounts (Config) tab -------------------------
    def _build_accounts_tab(self) -> None:
        top_bar = ttk.Frame(self.tab_accounts)
        top_bar.pack(fill=tk.X, padx=4, pady=4)

        ttk.Button(top_bar, text="Load Accounts", command=self._load_accounts_initial).pack(side=tk.LEFT, padx=2)
        ttk.Button(top_bar, text="Enable All", command=self._enable_all_accounts).pack(side=tk.LEFT, padx=2)
        ttk.Button(top_bar, text="Disable All", command=self._disable_all_accounts).pack(side=tk.LEFT, padx=2)
        ttk.Button(top_bar, text="Refresh Balances", command=self._refresh_balances_clicked).pack(side=tk.LEFT, padx=8)

        # Extend columns to include account ID and last used
        self.accounts_tree = ttk.Treeview(self.tab_accounts, columns=(
            "account", "included", "proxy", "balance", "last_used"
        ), show='headings', selectmode='extended')
        self.accounts_tree.heading("account", text="Account")
        self.accounts_tree.heading("included", text="Included")
        self.accounts_tree.heading("proxy", text="Proxy Address")
        self.accounts_tree.heading("balance", text="USDC Balance")
        self.accounts_tree.heading("last_used", text="Last Used")
        self.accounts_tree.column("account", width=160)
        self.accounts_tree.column("included", width=90, anchor=tk.CENTER)
        self.accounts_tree.column("proxy", width=420)
        self.accounts_tree.column("balance", width=140, anchor=tk.E)
        self.accounts_tree.column("last_used", width=160, anchor=tk.W)
        self.accounts_tree.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        self.accounts_tree.bind('<Double-1>', self._toggle_account_included)
        self._make_treeview_sortable(self.accounts_tree)

        hint = ttk.Label(self.tab_accounts, text="Double-click a row to toggle inclusion in net positions and order queries.", foreground="gray")
        hint.pack(anchor='w', padx=6, pady=(0,6))

    def _load_accounts_initial(self) -> None:
        try:
            count = self.account_manager.load_accounts_from_env()
            logger.info(f"Loaded {count} accounts from environment")

            # Initialize included state if new
            for account_id in self.account_manager.accounts.keys():
                if account_id not in self.included_accounts:
                    self.included_accounts[account_id] = True
                    # Also set AccountManager enabled flag to track balances efficiently
                    self.account_manager.enable_account(account_id, True)

            self._refresh_accounts_table()
            self._refresh_positions_account_dropdown()
            # Kick balance refresh in background
            self._run_async(self._refresh_balances_async())
        except Exception as e:
            logger.error(f"Error loading accounts: {e}")
            messagebox.showerror("Error", f"Failed to load accounts: {e}")

    def _refresh_accounts_table(self) -> None:
        for item in self.accounts_tree.get_children():
            self.accounts_tree.delete(item)

        info = self.account_manager.get_account_info()
        for account_id, meta in sorted(info.items()):
            included = self.included_accounts.get(account_id, False)
            included_txt = "Yes" if included else "No"
            proxy = meta.get("proxy_address", "")
            bal = meta.get("balance_usd", 0.0)
            bal_txt = f"${bal:,.2f}" if meta.get("balance_fetched") else "(loading)"
            last_used = meta.get("last_used")
            last_used_txt = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(last_used)) if last_used else ""
            self.accounts_tree.insert('', tk.END, iid=account_id, values=(account_id, included_txt, proxy, bal_txt, last_used_txt))

    def _toggle_account_included(self, event=None) -> None:
        sel = self.accounts_tree.selection()
        if not sel:
            return
        for account_id in sel:
            current = self.included_accounts.get(account_id, False)
            new_state = not current
            self.included_accounts[account_id] = new_state
            # Mirror in AccountManager for balance/position refresh helpers
            self.account_manager.enable_account(account_id, new_state)
        self._refresh_accounts_table()
        self._refresh_positions_account_dropdown()

    def _enable_all_accounts(self) -> None:
        for account_id in list(self.account_manager.accounts.keys()):
            self.included_accounts[account_id] = True
            self.account_manager.enable_account(account_id, True)
        self._refresh_accounts_table()
        self._refresh_positions_account_dropdown()

    def _disable_all_accounts(self) -> None:
        for account_id in list(self.account_manager.accounts.keys()):
            self.included_accounts[account_id] = False
            self.account_manager.enable_account(account_id, False)
        self._refresh_accounts_table()
        self._refresh_positions_account_dropdown()

    def _refresh_balances_clicked(self) -> None:
        self._run_async(self._refresh_balances_async())

    async def _refresh_balances_async(self) -> None:
        try:
            await self.account_manager.update_balances()
        except Exception as e:
            logger.error(f"Error refreshing balances: {e}")
        finally:
            self.root.after(0, self._refresh_accounts_table)
            # Also refresh the balance row in the By Account view from cache
            def _refresh_current_account_positions_from_cache():
                acct = self.pos_account_var.get()
                if not acct:
                    return
                async def _reload_cached():
                    try:
                        cache = await self._ensure_positions_cache(acct)
                        cached = cache.get_cached_positions() if hasattr(cache, 'get_cached_positions') else {}
                        self.root.after(0, lambda: self._populate_positions_by_account(acct, cached))
                    except Exception:
                        pass
                self._run_async(_reload_cached())
            self.root.after(0, _refresh_current_account_positions_from_cache)

    # ------------------------- Price fetching helpers -------------------------
    async def _get_market_prices(self, slug: str) -> Tuple[Optional[float], Optional[float]]:
        """Get current YES and NO prices for a market slug"""
        try:
            # Use market analyzer to get current market data
            market_data = await self.market_analyzer.refresh_market_data(slug)
            if not market_data:
                logger.warning(f"No market data available for slug: {slug}")
                return None, None
            
            # Market analyzer returns bid/ask from YES perspective
            # For YES token: use ask price (what we'd pay to buy YES)
            # For NO token: use (1 - bid) since NO = 1 - YES
            yes_price = market_data.best_ask if market_data.best_ask else None
            no_price = (1.0 - market_data.best_bid) if market_data.best_bid else None
            
            logger.debug(f"Market {slug}: YES=${yes_price}, NO=${no_price}")
            return yes_price, no_price
            
        except Exception as e:
            logger.error(f"Error fetching prices for {slug}: {e}")
            return None, None

    # ------------------------- Combined Positions tab -------------------------
    def _build_positions_tab(self) -> None:
        # Layout: Top Orders panel, Bottom Notebook (Net Pairs, By Account)
        container = ttk.Frame(self.tab_positions)
        container.pack(fill=tk.BOTH, expand=True)

        # Top Orders Panel
        orders_frame = ttk.LabelFrame(container, text="Open Orders")
        orders_frame.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        top = ttk.Frame(orders_frame)
        top.pack(fill=tk.X, padx=4, pady=4)
        ttk.Button(top, text="Refresh Orders", command=self._refresh_orders_clicked).pack(side=tk.LEFT)
        ttk.Button(top, text="Cancel Selected", command=self._cancel_selected_orders_clicked).pack(side=tk.LEFT, padx=6)
        ttk.Button(top, text="Cancel All (Included)", command=self._cancel_all_orders_clicked).pack(side=tk.LEFT, padx=6)

        self.orders_tree = ttk.Treeview(orders_frame, columns=(
            "account", "slug", "outcome", "side", "price", "size", "status"
        ), show='headings', selectmode='extended')
        for col, txt, w, anchor in (
            ("account", "Account", 120, tk.W),
            ("slug", "Market Slug", 300, tk.W),
            ("outcome", "Outcome", 140, tk.W),
            ("side", "Side", 70, tk.CENTER),
            ("price", "Price", 80, tk.E),
            ("size", "Size", 80, tk.E),
            ("status", "Status", 120, tk.W),
        ):
            self.orders_tree.heading(col, text=txt)
            self.orders_tree.column(col, width=w, anchor=anchor)
        self.orders_tree.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        self.orders_tree.bind('<Double-1>', self._double_click_order)
        self._make_treeview_sortable(self.orders_tree)

        # Bottom Positions Notebook
        bottom = ttk.LabelFrame(container, text="Positions")
        bottom.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        self.pos_notebook = ttk.Notebook(bottom)
        self.pos_notebook.pack(fill=tk.BOTH, expand=True)

        # Net tab
        self.tab_pos_net = ttk.Frame(self.pos_notebook)
        self.pos_notebook.add(self.tab_pos_net, text="Net Across Accounts")

        controls = ttk.Frame(self.tab_pos_net)
        controls.pack(fill=tk.X, padx=4, pady=4)
        ttk.Button(controls, text="Refresh All", command=self._refresh_all_positions_clicked).pack(side=tk.LEFT)
        # Removed Claim All and Merge All buttons
        # self.btn_claim_all = ttk.Button(controls, text="Claim All Redeemable", command=self._claim_all_clicked)
        # self.btn_claim_all.pack(side=tk.LEFT, padx=10)
        # self.btn_merge_all = ttk.Button(controls, text="Merge All Full Sets", command=self._merge_all_clicked)
        # self.btn_merge_all.pack(side=tk.LEFT)
        # Net refresh indicator
        self.net_pb = ttk.Progressbar(controls, mode='indeterminate', length=100)
        self.net_pb.pack(side=tk.LEFT, padx=8)
        self.net_status_label = ttk.Label(controls, text="", foreground="gray")
        self.net_status_label.pack(side=tk.LEFT)

        self._render_net_tree()  # defaults to pairs

        # By Account tab
        self.tab_pos_by_acct = ttk.Frame(self.pos_notebook)
        self.pos_notebook.add(self.tab_pos_by_acct, text="By Account")

        top1 = ttk.Frame(self.tab_pos_by_acct)
        top1.pack(fill=tk.X, padx=4, pady=4)
        ttk.Label(top1, text="Account:").pack(side=tk.LEFT)
        self.pos_account_var = tk.StringVar()
        self.pos_account_combo = ttk.Combobox(top1, textvariable=self.pos_account_var, state='readonly', width=18)
        self.pos_account_combo.pack(side=tk.LEFT, padx=6)
        self.pos_account_combo.bind('<<ComboboxSelected>>', lambda e: self._on_account_selection_changed())
        ttk.Button(top1, text="Refresh", command=self._refresh_positions_clicked).pack(side=tk.LEFT, padx=4)
        # By-account refresh indicator
        self.pos_by_acct_pb = ttk.Progressbar(top1, mode='indeterminate', length=100)
        self.pos_by_acct_pb.pack(side=tk.LEFT, padx=8)
        self.pos_by_acct_status = ttk.Label(top1, text="", foreground="gray")
        self.pos_by_acct_status.pack(side=tk.LEFT)

        self.tree_pos_by_acct = ttk.Treeview(self.tab_pos_by_acct, columns=(
            "title", "outcome", "asset", "size", "avg_price", "value", "pnl"
        ), show='headings', selectmode='browse')
        for col, txt, w in (
            ("title", "Market", 480),
            ("outcome", "Outcome", 120),
            ("asset", "Token", 180),
            ("size", "Size", 100),
            ("avg_price", "Avg Price", 100),
            ("value", "Value", 100),
            ("pnl", "Cash PnL", 100),
        ):
            self.tree_pos_by_acct.heading(col, text=txt)
            self.tree_pos_by_acct.column(col, width=w, anchor=tk.W if col in ("title", "outcome", "asset") else tk.E)
        self.tree_pos_by_acct.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        self._make_treeview_sortable(self.tree_pos_by_acct)
        # Style for total rows
        self.tree_pos_by_acct.tag_configure('total', background='#f0f0f0')

        # Populate account dropdown
        self._refresh_positions_account_dropdown()

    def _refresh_positions_account_dropdown(self) -> None:
        accounts = [a for a, included in self.included_accounts.items() if included]
        if not accounts:
            accounts = list(self.account_manager.accounts.keys())
        
        current_selection = self.pos_account_var.get()
        self.pos_account_combo['values'] = accounts
        
        if accounts:
            if current_selection not in accounts:
                # Current selection is no longer valid, pick the first account
                self.pos_account_var.set(accounts[0])
                # Immediately update UI with cached data and start refresh like user selection
                self._on_account_selection_changed()
            else:
                # Current selection is still valid, but refresh its positions since account states may have changed
                self._on_account_selection_changed()

    def _refresh_positions_clicked(self) -> None:
        acct = self.pos_account_var.get()
        if not acct:
            messagebox.showinfo("Positions", "No account selected")
            return
        # Ensure the indicator starts immediately for visible feedback
        self._start_by_account_indicator()
        self._queue_by_account_refresh(acct)

    def _queue_by_account_refresh(self, account_id: str) -> None:
        # If a refresh is in-flight, schedule a pending refresh for the latest account
        if self._pos_by_acct_refresh_inflight:
            self._pos_by_acct_pending_account = account_id
            return
        # Start new refresh
        self._run_async(self._refresh_positions_for_account_async(account_id))

    async def _ensure_positions_cache(self, account_id: str) -> UserPositionsCache:
        if account_id in self.positions_caches:
            return self.positions_caches[account_id]
        # Build cache using proxy address
        account_info = self.account_manager.accounts.get(account_id)
        if not account_info:
            raise ValueError(f"Unknown account: {account_id}")
        cache = UserPositionsCache(proxy_address=account_info.proxy_address, cache_duration_minutes=1)
        self.positions_caches[account_id] = cache
        return cache

    async def _refresh_positions_for_account_async(self, account_id: str) -> None:
        # Mark inflight and show indicator
        self._pos_by_acct_refresh_inflight = True
        self.root.after(0, self._start_by_account_indicator)
        try:
            cache = await self._ensure_positions_cache(account_id)
            cache.force_refresh()
            positions = cache.get_all_positions()
        except Exception as e:
            logger.error(f"Error refreshing positions for {account_id}: {e}")
            positions = {}
        finally:
            # Populate UI
            self.root.after(0, lambda: self._populate_positions_by_account(account_id, positions))
            # Stop indicator
            self._pos_by_acct_refresh_inflight = False
            self.root.after(0, self._stop_by_account_indicator)
            # If there is a pending account refresh, process it
            pending = self._pos_by_acct_pending_account
            self._pos_by_acct_pending_account = None
            if pending:
                self.root.after(0, lambda: self._queue_by_account_refresh(pending))

    def _load_positions_for_selected_account(self, show_hint_if_empty: bool = True) -> None:
        acct = self.pos_account_var.get()
        if acct:
            # Show cached positions only; do NOT auto-refresh on toggle (handled by caller when desired)
            async def _show_cached_only():
                try:
                    cache = await self._ensure_positions_cache(acct)
                    cached = cache.get_cached_positions() if hasattr(cache, 'get_cached_positions') else {}
                    self.root.after(0, lambda: self._populate_positions_by_account(acct, cached))
                    # Optionally update status text based on caller preference
                    if show_hint_if_empty:
                        if not cached:
                            self.root.after(0, lambda: self.pos_by_acct_status.configure(text="No cached data. Click Refresh."))
                        else:
                            self.root.after(0, lambda: self.pos_by_acct_status.configure(text=""))
                except Exception:
                    if show_hint_if_empty:
                        self.root.after(0, lambda: self.pos_by_acct_status.configure(text="Error loading cache"))
            self._run_async(_show_cached_only())

    def _get_or_create_positions_cache_sync(self, account_id: str) -> UserPositionsCache:
        """Synchronous helper to get or create a UserPositionsCache for immediate UI updates."""
        if account_id in self.positions_caches:
            return self.positions_caches[account_id]
        account_info = self.account_manager.accounts.get(account_id)
        if not account_info:
            raise ValueError(f"Unknown account: {account_id}")
        cache = UserPositionsCache(proxy_address=account_info.proxy_address, cache_duration_minutes=1)
        self.positions_caches[account_id] = cache
        return cache

    def _on_account_selection_changed(self) -> None:
        acct = self.pos_account_var.get()
        if not acct:
            return
        # Immediately show cached positions synchronously for instant feedback; do not auto-refresh
        try:
            cache = self._get_or_create_positions_cache_sync(acct)
            cached = cache.get_cached_positions() if hasattr(cache, 'get_cached_positions') else {}
            self._populate_positions_by_account(acct, cached)
            # Status hint only if we have no cached data
            if not cached:
                self.pos_by_acct_status.configure(text="No cached data. Click Refresh.")
            else:
                self.pos_by_acct_status.configure(text="")
        except Exception:
            # If anything goes wrong, show a minimal error hint; do not auto-refresh
            try:
                self.pos_by_acct_status.configure(text="Error loading cache")
            except Exception:
                pass
        # Note: No progress indicator and no background refresh here by design

    def _populate_positions_by_account(self, account_id: str, positions: Dict[str, UserPosition]) -> None:
        self.tree_pos_by_acct.delete(*self.tree_pos_by_acct.get_children())
        # positions is dict token_id -> UserPosition
        total_size = 0.0
        total_value = 0.0
        total_pnl = 0.0
        for token_id, pos in positions.items():
            self.tree_pos_by_acct.insert('', tk.END, values=(
                pos.title, pos.outcome, token_id, f"{pos.size:,.2f}", f"{pos.avg_price:.3f}", f"{pos.current_value:,.2f}", f"{pos.cash_pnl:,.2f}"
            ))
            total_size += pos.size
            total_value += pos.current_value
            total_pnl += pos.cash_pnl
        # Total row
        self.tree_pos_by_acct.insert('', tk.END, values=(
            "TOTAL", "", "", f"{total_size:,.2f}", "", f"{total_value:,.2f}", f"{total_pnl:,.2f}"
        ), tags=('total',))
        
        # Add account balance row
        account_info = self.account_manager.get_account_info().get(account_id, {})
        balance = account_info.get('balance_usd', 0.0)
        balance_fetched = account_info.get('balance_fetched', False)
        balance_text = f"${balance:,.2f}" if balance_fetched else "(loading)"
        
        self.tree_pos_by_acct.insert('', tk.END, values=(
            "ACCOUNT BALANCE", "", "", "", "", balance_text, ""
        ), tags=('total',))

    def _refresh_all_positions_clicked(self) -> None:
        # Start net refresh with indicator
        if self._pos_net_refresh_inflight:
            return
        self._run_async(self._refresh_all_positions_async())

    async def _refresh_all_positions_async(self) -> None:
        self._pos_net_refresh_inflight = True
        self.root.after(0, self._start_net_indicator)
        accounts = [a for a, included in self.included_accounts.items() if included]
        aggregated_pairs: Dict[str, Tuple[float, float, float, str]] = {}  # slug -> (pairs_usd, net_yes, net_no, title)
        for account_id in accounts:
            try:
                cache = await self._ensure_positions_cache(account_id)
                cache.force_refresh()
                for token_id, pos in cache.get_all_positions().items():
                    slug = pos.slug or ""
                    if not slug:
                        continue
                    yes = pos.size if str(pos.outcome).lower() == 'yes' else 0.0
                    no = pos.size if str(pos.outcome).lower() == 'no' else 0.0
                    if slug not in aggregated_pairs:
                        aggregated_pairs[slug] = (0.0, 0.0, 0.0, pos.title)
                    pairs_usd, net_yes, net_no, title = aggregated_pairs[slug]
                    # Accumulate raw YES/NO
                    net_yes += yes
                    net_no += no
                    # Recompute pairs and residuals
                    pairs = min(net_yes, net_no)
                    aggregated_pairs[slug] = (pairs, net_yes - pairs, net_no - pairs, title)
            except Exception as e:
                logger.error(f"Error aggregating positions for {account_id}: {e}")
                continue
        
        # Fetch current prices for each market and calculate dollar values
        aggregated_with_prices: Dict[str, Tuple[float, float, float, str, float, float]] = {}  # slug -> (pairs_usd, net_yes, net_no, title, yes_usd, no_usd)
        for slug, (pairs_usd, net_yes, net_no, title) in aggregated_pairs.items():
            try:
                yes_price, no_price = await self._get_market_prices(slug)
                yes_usd = (net_yes * yes_price) if yes_price is not None and net_yes > 0 else 0.0
                no_usd = (net_no * no_price) if no_price is not None and net_no > 0 else 0.0
                aggregated_with_prices[slug] = (pairs_usd, net_yes, net_no, title, yes_usd, no_usd)
            except Exception as e:
                logger.error(f"Error fetching prices for {slug}: {e}")
                # Include without price data
                aggregated_with_prices[slug] = (pairs_usd, net_yes, net_no, title, 0.0, 0.0)
        
        # Update UI and stop indicator
        self.root.after(0, lambda: self._populate_net_positions_pairs_with_prices(aggregated_with_prices))
        self._pos_net_refresh_inflight = False
        self.root.after(0, self._stop_net_indicator)

    def _populate_net_positions(self, aggregated: Dict[str, Tuple[float, float, str, str]]) -> None:
        # kept for compatibility if needed
        if not self.tree_pos_net:
            return
        self.tree_pos_net.delete(*self.tree_pos_net.get_children())
        sum_size = 0.0
        sum_value = 0.0
        for token_id, (net_size, net_value, title, outcome) in aggregated.items():
            self.tree_pos_net.insert('', tk.END, values=(
                title, outcome, token_id, f"{net_size:,.2f}", f"{net_value:,.2f}"
            ))
            sum_size += net_size
            sum_value += net_value
        self.tree_pos_net.insert('', tk.END, values=(
            "TOTAL", "", "", f"{sum_size:,.2f}", f"{sum_value:,.2f}"
        ), tags=('total',))

    def _populate_net_positions_pairs(self, aggregated_pairs: Dict[str, Tuple[float, float, float, str]]) -> None:
        self.tree_pos_net.delete(*self.tree_pos_net.get_children())
        sum_pairs = 0.0
        sum_yes = 0.0
        sum_no = 0.0
        for slug, (pairs_usd, net_yes, net_no, title) in aggregated_pairs.items():
            self.tree_pos_net.insert('', tk.END, values=(
                title, f"{pairs_usd:,.2f}", f"{net_yes:,.2f}", "N/A", f"{net_no:,.2f}", "N/A"
            ))
            sum_pairs += pairs_usd
            sum_yes += net_yes
            sum_no += net_no
        self.tree_pos_net.insert('', tk.END, values=(
            "TOTAL", f"{sum_pairs:,.2f}", f"{sum_yes:,.2f}", "N/A", f"{sum_no:,.2f}", "N/A"
        ), tags=('total',))

    def _populate_net_positions_pairs_with_prices(self, aggregated_with_prices: Dict[str, Tuple[float, float, float, str, float, float]]) -> None:
        """Populate net positions with current market prices"""
        self.tree_pos_net.delete(*self.tree_pos_net.get_children())
        sum_pairs = 0.0
        sum_yes = 0.0
        sum_no = 0.0
        sum_yes_usd = 0.0
        sum_no_usd = 0.0
        
        for slug, (pairs_usd, net_yes, net_no, title, yes_usd, no_usd) in aggregated_with_prices.items():
            yes_usd_text = f"${yes_usd:,.2f}" if yes_usd > 0 else "N/A"
            no_usd_text = f"${no_usd:,.2f}" if no_usd > 0 else "N/A"
            
            self.tree_pos_net.insert('', tk.END, values=(
                title, f"{pairs_usd:,.2f}", f"{net_yes:,.2f}", yes_usd_text, f"{net_no:,.2f}", no_usd_text
            ))
            sum_pairs += pairs_usd
            sum_yes += net_yes
            sum_no += net_no
            sum_yes_usd += yes_usd
            sum_no_usd += no_usd
        
        # Total row
        total_yes_usd_text = f"${sum_yes_usd:,.2f}" if sum_yes_usd > 0 else "N/A"
        total_no_usd_text = f"${sum_no_usd:,.2f}" if sum_no_usd > 0 else "N/A"
        
        self.tree_pos_net.insert('', tk.END, values=(
            "TOTAL", f"{sum_pairs:,.2f}", f"{sum_yes:,.2f}", total_yes_usd_text, f"{sum_no:,.2f}", total_no_usd_text
        ), tags=('total',))

    # ------------------------- Orders logic -------------------------
    def _refresh_orders_clicked(self) -> None:
        self._run_async(self._refresh_orders_async())

    async def _refresh_orders_async(self) -> None:
        if self._orders_refresh_inflight:
            return
        self._orders_refresh_inflight = True
        try:
            included = [a for a, inc in self.included_accounts.items() if inc]
            results: Dict[str, List[dict]] = {}
            token_ids_needed: set = set()
            for account_id in included:
                try:
                    client = self.account_manager.get_client(account_id)
                    if not client:
                        results[account_id] = []
                        continue
                    resp = client.get_orders(OpenOrderParams())
                    orders_list: List[dict] = resp if isinstance(resp, list) else []
                    results[account_id] = orders_list
                    # Collect token ids from orders
                    for order in orders_list:
                        token_id = order.get('asset_id') or order.get('asset') or ''
                        if token_id and token_id not in self._token_slug_outcome_cache:
                            token_ids_needed.add(token_id)
                except Exception as e:
                    logger.error(f"Error fetching orders for {account_id}: {e}")
                    results[account_id] = []

            # Enrich missing token ids with slug/outcome using market metadata
            if token_ids_needed:
                try:
                    async def fetch_one(tid: str):
                        meta = await get_market_metadata(tid)
                        if meta:
                            return tid, (meta.market_slug or '', meta.outcome or '')
                        return tid, ('', '')
                    gathered = await asyncio.gather(*(fetch_one(tid) for tid in token_ids_needed), return_exceptions=True)
                    for item in gathered:
                        try:
                            if isinstance(item, tuple) and len(item) == 2:
                                tid, pair = item
                                if isinstance(tid, str) and isinstance(pair, tuple):
                                    self._token_slug_outcome_cache[tid] = pair  # (slug, outcome)
                        except Exception:
                            continue
                except Exception as e:
                    logger.error(f"Error enriching token metadata: {e}")

            self.open_orders_cache = results
            self.root.after(0, self._populate_orders_table)
        finally:
            self._orders_refresh_inflight = False

    def _populate_orders_table(self) -> None:
        self.orders_tree.delete(*self.orders_tree.get_children())
        for account_id, orders in self.open_orders_cache.items():
            for order in orders:
                order_id = order.get('id') or order.get('order_id') or ''
                token_id = order.get('asset_id') or order.get('asset') or ''
                side = order.get('side', '').upper() if isinstance(order.get('side'), str) else str(order.get('side'))
                price = order.get('price') or order.get('limit_price') or order.get('limitPrice')
                size = order.get('size') or order.get('quantity')
                status = order.get('status', 'OPEN')

                # Map token -> (slug, outcome)
                slug, outcome = self._token_slug_outcome_cache.get(token_id, ('', ''))

                try:
                    price_txt = f"{float(price):.3f}" if price is not None else ""
                except Exception:
                    price_txt = str(price)
                try:
                    size_txt = f"{float(size):,.2f}" if size is not None else ""
                except Exception:
                    size_txt = str(size)
                iid = f"{account_id}::{order_id}"
                self.orders_tree.insert('', tk.END, iid=iid, values=(account_id, slug, outcome, side, price_txt, size_txt, status))

    def _double_click_order(self, event=None) -> None:
        sel = self.orders_tree.selection()
        if not sel:
            return
        self._cancel_orders_by_iids(sel)

    def _cancel_selected_orders_clicked(self) -> None:
        sel = self.orders_tree.selection()
        if not sel:
            messagebox.showinfo("Cancel Orders", "No orders selected")
            return
        self._cancel_orders_by_iids(sel)

    def _cancel_orders_by_iids(self, iids: Tuple[str, ...]) -> None:
        pairs: List[Tuple[str, str]] = []  # (account_id, order_id)
        for iid in iids:
            try:
                account_id, order_id = iid.split("::", 1)
                if order_id:
                    pairs.append((account_id, order_id))
            except ValueError:
                continue
        if not pairs:
            return
        if not messagebox.askyesno("Confirm", f"Cancel {len(pairs)} order(s)?"):
            return
        self._run_async(self._cancel_orders_async(pairs))

    async def _cancel_orders_async(self, pairs: List[Tuple[str, str]]) -> None:
        cancelled = 0
        for account_id, order_id in pairs:
            try:
                client = self.account_manager.get_client(account_id)
                if not client:
                    continue
                resp = client.cancel_orders([order_id])
                ok = False
                if isinstance(resp, dict):
                    ok = True
                elif resp is True:
                    ok = True
                if ok:
                    cancelled += 1
            except Exception as e:
                logger.error(f"Error cancelling order {order_id} for {account_id}: {e}")
        # Refresh orders after a short delay to let backend settle
        time.sleep(0.3)
        await self._refresh_orders_async()
        self.root.after(0, lambda: messagebox.showinfo("Cancel Orders", f"Cancelled {cancelled} order(s)"))

    def _cancel_all_orders_clicked(self) -> None:
        included = [a for a, inc in self.included_accounts.items() if inc]
        if not included:
            messagebox.showinfo("Cancel All", "No included accounts")
            return
        if not messagebox.askyesno("Confirm", f"Cancel all open orders for {len(included)} included account(s)?"):
            return
        self._run_async(self._cancel_all_orders_async(included))

    async def _cancel_all_orders_async(self, accounts: List[str]) -> None:
        total_cancelled = 0
        for account_id in accounts:
            try:
                client = self.account_manager.get_client(account_id)
                if not client:
                    continue
                resp = client.get_orders(OpenOrderParams())
                ids = []
                if isinstance(resp, list):
                    ids = [o.get('id') for o in resp if o.get('id')]
                if ids:
                    client.cancel_orders(ids)
                    total_cancelled += len(ids)
            except Exception as e:
                logger.error(f"Error cancelling all orders for {account_id}: {e}")
        # Refresh orders
        await self._refresh_orders_async()
        self.root.after(0, lambda: messagebox.showinfo("Cancel All", f"Submitted cancellation for {total_cancelled} order(s)"))

    def _schedule_orders_refresh(self) -> None:
        if not self._orders_auto_refresh_enabled:
            return
        # call async refresh and reschedule
        self._run_async(self._refresh_orders_async())
        self.root.after(1000, self._schedule_orders_refresh)

    # ------------------------- Sorting helpers -------------------------
    def _make_treeview_sortable(self, tree: ttk.Treeview) -> None:
        cols = tree['columns']
        for col in cols:
            tree.heading(col, command=lambda c=col, t=tree: self._sort_treeview(t, c))

    def _sort_treeview(self, tree: ttk.Treeview, col: str) -> None:
        # Toggle direction
        key = str(id(tree))
        _, asc_prev = self._sort_state.get(key, (col, True))
        asc = not asc_prev if self._sort_state.get(key) and self._sort_state[key][0] == col else True

        def parse_val(v: str):
            s = (v or '').strip().replace('$', '').replace(',', '')
            try:
                # Numeric values sort before non-numeric
                return (0, float(s))
            except Exception:
                # Non-numeric values like 'N/A' sort after numbers in ascending
                return (1, s.lower())

        all_ids = list(tree.get_children(''))
        normal = []
        totals = []
        for k in all_ids:
            tags = tree.item(k, 'tags')
            (totals if ('total' in tags if tags else False) else normal).append(k)
        items = [(tree.set(k, col), k) for k in normal]
        items.sort(key=lambda x: parse_val(x[0]), reverse=not asc)
        ordered = [k for _, k in items] + totals  # totals always at bottom
        for idx, k in enumerate(ordered):
            tree.move(k, '', idx)
        self._sort_state[key] = (col, asc)

    # ------------------------- Redeemable/Mergeable tab -------------------------
    def _build_redeemable_tab(self) -> None:
        redeemable_frame = ttk.LabelFrame(self.tab_redeemable, text="Redeemable/Mergeable Indicators")
        redeemable_frame.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        # Controls
        controls = ttk.Frame(redeemable_frame)
        controls.pack(fill=tk.X, padx=4, pady=(4, 2))
        ttk.Button(controls, text="Refresh", command=self._refresh_redeemable_mergeable_clicked).pack(side=tk.LEFT)

        # Redeemable
        redeemable_label = ttk.Label(redeemable_frame, text="Redeemable Tokens:")
        redeemable_label.pack(anchor='w', padx=6, pady=(0,2))
        self.redeemable_tree = ttk.Treeview(redeemable_frame, columns=(
            "account", "slug", "size", "outcome", "value"
        ), show='headings', selectmode='browse')
        for col, txt, w in (
            ("account", "Account", 160),
            ("slug", "Market Slug", 300),
            ("size", "Size", 100),
            ("outcome", "Outcome", 120),
            ("value", "Value", 100),
        ):
            self.redeemable_tree.heading(col, text=txt)
            self.redeemable_tree.column(col, width=w, anchor=tk.W if col in ("account", "slug", "outcome") else tk.E)
        self.redeemable_tree.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        self._make_treeview_sortable(self.redeemable_tree)
        self.redeemable_tree.tag_configure('total', background='#f0f0f0')

        # Mergeable
        mergeable_label = ttk.Label(redeemable_frame, text="Mergeable Sets:")
        mergeable_label.pack(anchor='w', padx=6, pady=(0,2))
        self.mergeable_tree = ttk.Treeview(redeemable_frame, columns=(
            "account", "slug", "size", "outcome", "value"
        ), show='headings', selectmode='browse')
        for col, txt, w in (
            ("account", "Account", 160),
            ("slug", "Market Slug", 300),
            ("size", "Size", 100),
            ("outcome", "Outcome", 120),
            ("value", "Value", 100),
        ):
            self.mergeable_tree.heading(col, text=txt)
            self.mergeable_tree.column(col, width=w, anchor=tk.W if col in ("account", "slug", "outcome") else tk.E)
        self.mergeable_tree.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        self._make_treeview_sortable(self.mergeable_tree)
        self.mergeable_tree.tag_configure('total', background='#f0f0f0')

        # Refresh data
        self._run_async(self._refresh_redeemable_async())
        self._run_async(self._refresh_mergeable_async())

    def _refresh_redeemable_mergeable_clicked(self) -> None:
        """Refresh both redeemable and mergeable tables on demand."""
        self._run_async(self._refresh_redeemable_async())
        self._run_async(self._refresh_mergeable_async())

    async def _refresh_redeemable_async(self) -> None:
        self.redeemable_tree.delete(*self.redeemable_tree.get_children())
        redeemable_data: List[Tuple[str, str, float, str, float]] = []  # account, slug, size, outcome, value
        for account_id in self.account_manager.accounts.keys():
            try:
                cache = await self._ensure_positions_cache(account_id)
                cache.force_refresh()
                for token_id, pos in cache.get_all_positions().items():
                    if pos.redeemable and pos.size > 0 and getattr(pos, 'slug', ''):
                        redeemable_data.append((
                            account_id, pos.slug or 'Unknown', pos.size, pos.outcome, pos.current_value
                        ))
            except Exception as e:
                logger.error(f"Error fetching redeemable positions for {account_id}: {e}")

        for item in redeemable_data:
            account, slug, size, outcome, value = item
            self.redeemable_tree.insert('', tk.END, values=(
                account, slug, f"{size:,.2f}", outcome, f"${value:,.2f}"
            ))
        
        # Calculate totals
        total_size = sum(item[2] for item in redeemable_data)
        total_value = sum(item[4] for item in redeemable_data)
        self.redeemable_tree.insert('', tk.END, values=(
            "TOTAL", "", f"{total_size:,.2f}", "", f"${total_value:,.2f}"
        ), tags=('total',))

    async def _refresh_mergeable_async(self) -> None:
        self.mergeable_tree.delete(*self.mergeable_tree.get_children())
        mergeable_data: List[Tuple[str, str, float, str, float]] = []  # account, slug, size, outcome, value
        for account_id in self.account_manager.accounts.keys():
            try:
                cache = await self._ensure_positions_cache(account_id)
                cache.force_refresh()
                for token_id, pos in cache.get_all_positions().items():
                    if pos.mergeable and pos.size > 0 and getattr(pos, 'slug', ''):
                        mergeable_data.append((
                            account_id, pos.slug or 'Unknown', pos.size, pos.outcome, pos.current_value
                        ))
            except Exception as e:
                logger.error(f"Error fetching mergeable positions for {account_id}: {e}")

        for item in mergeable_data:
            account, slug, size, outcome, value = item
            self.mergeable_tree.insert('', tk.END, values=(
                account, slug, f"{size:,.2f}", outcome, f"${value:,.2f}"
            ))
        
        # Calculate totals
        total_size = sum(item[2] for item in mergeable_data)
        total_value = sum(item[4] for item in mergeable_data)
        self.mergeable_tree.insert('', tk.END, values=(
            "TOTAL", "", f"{total_size:,.2f}", "", f"${total_value:,.2f}"
        ), tags=('total',))

    # ------------------------- Claim all -------------------------
    # Removed Claim All functionality

    # ------------------------- Merge all -------------------------
    # Removed Merge All functionality

    def _render_net_tree(self) -> None:
        # Destroy existing tree if any
        if getattr(self, 'tree_pos_net', None):
            self.tree_pos_net.destroy()
        # Always render pairs view in this combined layout with new dollar columns
        cols = ("title", "pairs_usd", "net_yes", "net_yes_usd", "net_no", "net_no_usd")
        headings = {
            "title": ("Market", 420, tk.W),
            "pairs_usd": ("Yes+No Pairs ($)", 140, tk.E),
            "net_yes": ("Net YES", 100, tk.E),
            "net_yes_usd": ("Net YES ($)", 120, tk.E),
            "net_no": ("Net NO", 100, tk.E),
            "net_no_usd": ("Net NO ($)", 120, tk.E),
        }
        self.tree_pos_net = ttk.Treeview(self.tab_pos_net, columns=cols, show='headings')
        for col in cols:
            text, w, anchor = headings[col]
            self.tree_pos_net.heading(col, text=text)
            self.tree_pos_net.column(col, width=w, anchor=anchor)
        self.tree_pos_net.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        self._make_treeview_sortable(self.tree_pos_net)
        self.tree_pos_net.tag_configure('total', background='#f0f0f0')
        # Refresh data in the new view
        self._run_async(self._refresh_all_positions_async())

    def _start_by_account_indicator(self) -> None:
        try:
            self.pos_by_acct_status.configure(text="Refreshing positions...")
            self.pos_by_acct_pb.start(50)
        except Exception:
            pass

    def _stop_by_account_indicator(self) -> None:
        try:
            self.pos_by_acct_pb.stop()
            self.pos_by_acct_status.configure(text="")
        except Exception:
            pass

    def _start_net_indicator(self) -> None:
        try:
            self.net_status_label.configure(text="Refreshing net positions...")
            self.net_pb.start(50)
        except Exception:
            pass

    def _stop_net_indicator(self) -> None:
        try:
            self.net_pb.stop()
            self.net_status_label.configure(text="")
        except Exception:
            pass

    # Removed _start_claim_indicator and _stop_claim_indicator


def main():
    root = tk.Tk()
    app = PositionsOrdersDashboard(root)
    root.mainloop()


if __name__ == "__main__":
    main() 